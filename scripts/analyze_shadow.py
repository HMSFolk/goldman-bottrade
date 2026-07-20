# scripts/analyze_shadow.py
"""
วิเคราะห์ shadow trades — ไม้ที่โดน gate ตัด (บันทึกโดย bot/main.py::_shadow_log)
═══════════════════════════════════════════════════════════════════════
อ่าน  : logs/shadow_trades.jsonl
        (fields: ts, symbol, direction, confidence, price, sl_dist,
                 tp_dist, regime, skip_reason)
เทียบ : data/raw/{symbol}_M15.parquet — จำลองว่าไม้ที่โดนตัด "ถ้าปล่อยเข้า"
        จะชน TP หรือ SL ก่อน (เดินแท่งไปข้างหน้าแบบ triple-barrier)
ออก   : สรุปต่อ skip_reason / conf bucket / regime / direction
        + reports/shadow_analysis.csv

ตอบคำถาม:
  1. gate ตัดไม้ "ดี" ทิ้งไหม → ถ้า expectancy ของไม้ที่ตัด > 0 = threshold แน่นไป
  2. สัญญาณเอียงข้างไหม → BUY vs SELL ratio ของสัญญาณดิบ (ใช้เฝ้า bias โมเดล)

⚠️ Timezone: ts ใน shadow = UTC จริง แต่ index ใน parquet = server time
   ของ broker (UTC+2/+3) ที่ติดป้าย UTC — ต้อง shift ก่อน match ไม่งั้น
   analyzer จะแอบเห็น "แท่งในอดีต" เป็นอนาคต (lookahead ปลอม)
   → คำนวณ offset อัตโนมัติจากแท่งล่าสุดเทียบเวลาจริง

รัน: python scripts/analyze_shadow.py [--max-bars 96]
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from config import get_config
CFG = get_config()

SHADOW_PATH = _ROOT / CFG.get("paths", {}).get("logs", "logs") / "shadow_trades.jsonl"
RAW_DIR     = _ROOT / CFG.get("paths", {}).get("data_raw", "data/raw")
REPORTS_DIR = _ROOT / CFG.get("paths", {}).get("reports", "reports")


def _load_shadow() -> pd.DataFrame:
    if not SHADOW_PATH.exists():
        print(f"ไม่พบ {SHADOW_PATH} — ยังไม่มี shadow log "
              f"(เช็คว่า config logging.shadow_trades: true และบอทรันมาสักพัก)")
        return pd.DataFrame()
    rows = []
    with open(SHADOW_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue   # บรรทัดเสีย (เช่นเขียนค้าง) — ข้าม
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts", "price", "sl_dist", "tp_dist"])
    df = df[(df["sl_dist"] > 0) & (df["tp_dist"] > 0)]
    return df.reset_index(drop=True)


def _broker_offset_hours(bars: pd.DataFrame) -> int:
    """
    ประมาณ offset ของ server time เทียบ UTC จริง (ชั่วโมงเต็ม)
    หลัก: แท่ง M15 ล่าสุดในไฟล์ควรปิดไม่เกิน ~ไม่กี่สิบนาทีก่อน "ตอนนี้"
    ถ้า index ล่าสุดล้ำหน้าเวลาจริง = server time นำอยู่เท่านั้นชั่วโมง
    (ใช้ได้เมื่อไฟล์เพิ่งถูก update — quick update ทำให้จริงเสมอตอนบอทรัน)
    """
    last = bars.index[-1]
    now  = pd.Timestamp.now(tz="UTC")
    diff_h = (last - now).total_seconds() / 3600.0
    # server นำ UTC: diff เป็นบวก ~1.5–3.5 ชม. → ปัดเป็นชั่วโมงเต็มที่ใกล้สุด
    off = int(round(diff_h))
    return max(min(off, 12), -12)   # กันค่าหลุดโลก


def _simulate(rec, bars: pd.DataFrame, offset_h: int, max_bars: int):
    """
    เดินแท่งหลังสัญญาณ ดูว่าโดน TP หรือ SL ก่อน
    คืน (outcome, r_multiple): outcome ∈ {win, loss, ambiguous, open, no_data}
    """
    sig_time = rec["ts"] + timedelta(hours=offset_h)   # แปลงเป็น "เวลาป้าย" ของ parquet

    # ✅ FIX ALIGN-GUARD (2026-07-20): ผลรัน 2 รอบให้ outcome ตรงข้ามกัน
    #   (แพ้→ชนะ ซึ่งเป็นไปไม่ได้) เพราะป้ายเวลา parquet เลื่อนได้ตามเส้นทาง
    #   update แต่ simulate เดิมจับคู่แท่งแบบเชื่อป้าย 100% ไม่เช็คอะไรเลย
    #   ตอนนี้ตรวจ 2 ชั้นก่อนเดินแท่ง:
    #   (1) สัญญาณต้องอยู่ในช่วงข้อมูล (ไม่หลุดหน้าต่าง 499 แท่ง)
    #   (2) ANCHOR: ราคา entry ต้องอยู่ในช่วง low-high (±0.15%) ของแท่ง
    #       ณ เวลาสัญญาณ — ถ้าไม่อยู่ = ป้ายเวลาเพี้ยน → 'misaligned'
    #       ห้ามเดา ห้ามรายงานผล
    if sig_time <= bars.index[0]:
        return "no_data", 0.0            # หลุดหน้าต่างข้อมูล
    entry = float(rec["price"])
    pos = bars.index.searchsorted(sig_time)
    anchor = bars.iloc[max(pos - 1, 0)]  # แท่งที่ครอบเวลาสัญญาณ
    tol = entry * 0.0015                 # เผื่อ spread/ขอบแท่ง
    if not (float(anchor["low"]) - tol <= entry <= float(anchor["high"]) + tol):
        return "misaligned", 0.0

    fwd = bars[bars.index > sig_time]
    if fwd.empty:
        return "no_data", 0.0
    fwd = fwd.iloc[:max_bars]

    rr    = float(rec["tp_dist"]) / float(rec["sl_dist"])
    if rec["direction"] == "BUY":
        tp, sl = entry + rec["tp_dist"], entry - rec["sl_dist"]
        for _, b in fwd.iterrows():
            hit_tp, hit_sl = b["high"] >= tp, b["low"] <= sl
            if hit_tp and hit_sl:
                return "ambiguous", 0.0     # แท่งเดียวชนทั้งคู่ — ไม่เดา
            if hit_tp:
                return "win", rr
            if hit_sl:
                return "loss", -1.0
    else:  # SELL
        tp, sl = entry - rec["tp_dist"], entry + rec["sl_dist"]
        for _, b in fwd.iterrows():
            hit_tp, hit_sl = b["low"] <= tp, b["high"] >= sl
            if hit_tp and hit_sl:
                return "ambiguous", 0.0
            if hit_tp:
                return "win", rr
            if hit_sl:
                return "loss", -1.0
    # หมดหน้าต่างไม่ชนอะไร — ปิดที่ close สุดท้าย (mark-to-market เป็น R)
    last_close = float(fwd["close"].iloc[-1])
    pnl = (last_close - entry) if rec["direction"] == "BUY" else (entry - last_close)
    return "open", round(pnl / float(rec["sl_dist"]), 3)


def _summary(df: pd.DataFrame, by: str) -> pd.DataFrame:
    """สรุป WR / expectancy ต่อกลุ่ม (นับเฉพาะไม้ที่ตัดสินได้ win/loss)"""
    rows = []
    for key, g in df.groupby(by, observed=False):
        dec = g[g["outcome"].isin(["win", "loss"])]
        n_dec = len(dec)
        wr  = (dec["outcome"] == "win").mean() if n_dec else float("nan")
        exp = dec["r"].mean() if n_dec else float("nan")
        rows.append({
            by: key, "signals": len(g), "decided": n_dec,
            "win_rate": round(wr * 100, 1) if n_dec else None,
            "expectancy_R": round(exp, 3) if n_dec else None,
        })
    return pd.DataFrame(rows).sort_values("signals", ascending=False)


def main(max_bars: int = 96, offset_override: int | None = None):
    df = _load_shadow()
    if df.empty:
        return
    print(f"โหลด shadow: {len(df)} สัญญาณ "
          f"({df['ts'].min():%Y-%m-%d} → {df['ts'].max():%Y-%m-%d})")

    # ── จำลองผลรายไม้ ────────────────────────────────────────
    outcomes, rs = [], []
    bars_cache, offset_cache = {}, {}
    for _, rec in df.iterrows():
        sym = rec["symbol"]
        if sym not in bars_cache:
            p = RAW_DIR / f"{sym}_M15.parquet"
            if p.exists():
                b = pd.read_parquet(p).sort_index()
                bars_cache[sym] = b
                if offset_override is not None:
                    offset_cache[sym] = offset_override
                    print(f"  {sym}: {len(b)} bars | ใช้ offset ที่ระบุ {offset_override:+d}h")
                else:
                    off = _broker_offset_hours(b)
                    offset_cache[sym] = off
                    print(f"  {sym}: {len(b)} bars | server-time offset ≈ {off:+d}h (auto)")
                    # ✅ auto ใช้ได้เฉพาะไฟล์สด (quick update ตอนบอท start)
                    #   ถ้าไฟล์เก่า ค่าที่ได้จะปน "ความเก่า" จน match แท่งผิดช่วง
                    if not (-1 <= off <= 4):
                        print(f"  ⚠️ {sym}: offset {off:+d}h ผิดปกติ (ควร +2/+3) — "
                              f"raw parquet น่าจะเก่า → ผล simulate ของ {sym} เชื่อไม่ได้!\n"
                              f"     ทางแก้: restart bot (ให้ quick update รีเฟรชไฟล์) แล้วรันใหม่ "
                              f"หรือระบุ --offset 3 เอง")
            else:
                bars_cache[sym] = None
        if bars_cache[sym] is None:
            outcomes.append("no_data"); rs.append(0.0)
            continue
        o, r = _simulate(rec, bars_cache[sym], offset_cache[sym], max_bars)
        outcomes.append(o); rs.append(r)
    n_mis = outcomes.count("misaligned")
    if n_mis:
        print(f"  ⚠️ {n_mis}/{len(outcomes)} สัญญาณ MISALIGNED — ป้ายเวลา parquet "
              f"ไม่ตรงราคาสัญญาณ (ไฟล์ถูกเขียนคนละ convention?) — "
              f"แถวพวกนี้ถูกตัดออกจากทุกสถิติ ห้ามตีความแทนกัน")

    df["outcome"], df["r"] = outcomes, rs
    df["conf_bucket"] = pd.cut(
        df["confidence"],
        bins=[0, 0.45, 0.50, 0.52, 0.55, 0.60, 1.01],
        labels=["<0.45", "0.45-0.50", "0.50-0.52", "0.52-0.55", "0.55-0.60", ">0.60"],
    )

    # ── รายงาน ───────────────────────────────────────────────
    dec = df[df["outcome"].isin(["win", "loss"])]
    print("\n════════ ภาพรวมไม้ที่โดนตัด (นับเฉพาะที่ตัดสินได้) ════════")
    if len(dec):
        print(f"decided {len(dec)}/{len(df)} | WR={(dec['outcome']=='win').mean():.1%} "
              f"| expectancy={dec['r'].mean():+.3f}R")
        print("→ expectancy บวก = gate กำลังตัดไม้ที่โดยรวม 'ชนะ' ทิ้ง (threshold อาจแน่นไป)")
        print("→ expectancy ลบ = gate ทำหน้าที่ถูกแล้ว (ตัดไม้แพ้)")
    else:
        print("ยังไม่มีไม้ที่ตัดสินผลได้ — รอเก็บข้อมูลเพิ่ม")

    for col, title in [("skip_reason", "ตาม skip_reason"),
                       ("conf_bucket", "ตาม confidence"),
                       ("regime",      "ตาม regime"),
                       ("direction",   "ตามทิศทาง")]:
        print(f"\n──── {title} ────")
        print(_summary(df, col).to_string(index=False))

    # ── ตามตลาดจริงไหม: regime × direction ──────────────────
    #   อ่านแนวนอน: ใน regime นั้น สัญญาณไปทางไหน
    #   trending_down ควรเอียง SELL / trending_up ควรเอียง BUY
    #   ถ้าทิศเดียวท่วม "ทุก regime" = อคติไม่ขึ้นกับตลาด (ครูยังเอียง)
    if df["regime"].notna().any():
        print("\n──── ตามตลาดไหม? (regime × direction) ────")
        ct = pd.crosstab(df["regime"].fillna("?"), df["direction"], margins=True)
        print(ct.to_string())
        # ธงอัตโนมัติ: regime มีเทรนด์ชัดแต่สัญญาณส่วนใหญ่สวนทาง
        for reg, want in [("trending_down", "SELL"), ("trending_up", "BUY")]:
            g = df[df["regime"] == reg]
            if len(g) >= 5:
                with_trend = (g["direction"] == want).mean()
                if with_trend < 0.5:
                    print(f"⚠️ {reg}: สัญญาณตามเทรนด์แค่ {with_trend:.0%} "
                          f"(ควร >50% มาก) — โมเดลกำลังสวนตลาด")

    # ── เฝ้า bias ทิศทาง (คำถามเรื่อง balance) ───────────────
    n_buy  = int((df["direction"] == "BUY").sum())
    n_sell = int((df["direction"] == "SELL").sum())
    print("\n──── Signal balance (สัญญาณดิบที่มีทิศ) ────")
    print(f"BUY={n_buy}  SELL={n_sell}", end="  ")
    if min(n_buy, n_sell) > 0:
        ratio = max(n_buy, n_sell) / min(n_buy, n_sell)
        heavy = "SELL" if n_sell > n_buy else "BUY"
        flag  = "⚠️ เอียงผิดปกติ — เช็คว่าตลาดช่วงนี้ trend ทางนั้นจริงไหม ถ้าไม่ = ครูยัง bias" \
                if ratio > 1.8 else "ok (ต่างกันได้ตาม trend ตลาด)"
        print(f"| ratio={ratio:.2f} เอียง {heavy} → {flag}")
    else:
        print("| ยังมีทิศเดียว — เก็บข้อมูลเพิ่มก่อนสรุป")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / "shadow_analysis.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\nบันทึกรายไม้ → {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-bars", type=int, default=96,
                    help="เดินหน้ากี่แท่ง M15 ก่อนถือว่า open (default 96 = 1 วัน)")
    ap.add_argument("--offset", type=int, default=None,
                    help="server-time offset ชั่วโมง (เช่น 3 สำหรับ Exness ช่วง DST) — "
                         "ระบุเองเมื่อ raw parquet ไม่สด")
    args = ap.parse_args()
    main(max_bars=args.max_bars, offset_override=args.offset)