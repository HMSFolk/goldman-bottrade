# scripts/analyze_shadow.py
"""
Shadow-Trade Analyzer — วัด "edge" ของไม้ที่ระบบอยากเทรดแต่โดน gate ตัด
════════════════════════════════════════════════════════════════════════
อ่าน logs/shadow_trades.jsonl (สร้างโดย main._shadow_log เมื่อ
config.logging.shadow_trades = true) แล้วจำลองว่า "ถ้าเทรดไม้นั้นจริง"
ราคาจะชน TP หรือ SL ก่อน — โดยดึงแท่ง M15 ย้อนหลังจาก MT5 มาเดินไปข้างหน้า

จุดประสงค์:
  - วัดว่า gate (conf threshold / hard-block) "ตัดไม้ดีทิ้ง" หรือ "กันไม้แย่"
  - แยกบั๊กออกจาก "ไม่ควรเทรดจริง" โดยไม่ต้องเสียเงิน demo
  - ถ้าไม้ที่ถูกตัดมี win-rate/expectancy เป็นบวกสูง = gate เข้มเกินไป (ตัด edge ทิ้ง)
    ถ้าติดลบ = gate ทำงานถูก (กันไม้แย่)

รัน:  python scripts/analyze_shadow.py [--bars-forward 96] [--reason <substr>]

⚠️ side-channel ล้วน — อ่านอย่างเดียว ไม่แตะ logic เทรด ไม่ส่ง order
"""
import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import MetaTrader5 as mt5
from config import get_config, resolve_symbol

CFG = get_config()
SHADOW_PATH = _ROOT / CFG.get("paths", {}).get("logs", "logs") / "shadow_trades.jsonl"


def _connect() -> bool:
    m = CFG["mt5"]
    if not mt5.initialize(path=m.get("terminal_path", "")):
        print(f"❌ MT5 initialize failed: {mt5.last_error()}")
        return False
    return mt5.login(int(m["login"]), password=str(m["password"]),
                     server=str(m["server"]))


def _load_records() -> list[dict]:
    if not SHADOW_PATH.exists():
        print(f"❌ ไม่พบ {SHADOW_PATH}")
        print("   เปิด config.logging.shadow_trades = true แล้วรันบอทสักพักก่อน")
        return []
    recs = []
    with open(SHADOW_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def _simulate_one(rec: dict, bars_forward: int) -> str | None:
    """
    จำลองไม้เดียว: ดึงแท่ง M15 หลังเวลา signal แล้วเดินไปข้างหน้า
    คืน 'tp' | 'sl' | 'none' (ไม่ชนใน bars_forward) | None (ดึงข้อมูลไม่ได้)
    """
    direction = 1 if rec.get("direction") == "BUY" else -1
    entry     = rec.get("price")
    sl_dist   = rec.get("sl_dist", 0.0)
    tp_dist   = rec.get("tp_dist", 0.0)
    if entry is None or sl_dist <= 0 or tp_dist <= 0:
        return None

    ts = pd.to_datetime(rec["ts"], utc=True)
    broker_sym = resolve_symbol(rec["symbol"], CFG)

    # ดึงแท่งตั้งแต่เวลา signal เป็นต้นไป
    rates = mt5.copy_rates_from(broker_sym, mt5.TIMEFRAME_M15,
                                ts.to_pydatetime(), bars_forward + 2)
    if rates is None or len(rates) < 2:
        return None

    df = pd.DataFrame(rates)

    if direction == 1:   # BUY: TP = entry+tp_dist, SL = entry-sl_dist
        tp_price, sl_price = entry + tp_dist, entry - sl_dist
    else:                # SELL: TP = entry-tp_dist, SL = entry+sl_dist
        tp_price, sl_price = entry - tp_dist, entry + sl_dist

    # เดินทีละแท่ง (ข้ามแท่ง signal เอง เริ่มแท่งถัดไป) — นับ SL ก่อนถ้าชนทั้งคู่ (conservative)
    for _, bar in df.iloc[1:].iterrows():
        hi, lo = bar["high"], bar["low"]
        if direction == 1:
            if lo <= sl_price:
                return "sl"
            if hi >= tp_price:
                return "tp"
        else:
            if hi >= sl_price:
                return "sl"
            if lo <= tp_price:
                return "tp"
    return "none"


def analyze(bars_forward: int = 96, reason_filter: str = ""):
    recs = _load_records()
    if not recs:
        return
    if reason_filter:
        recs = [r for r in recs if reason_filter in r.get("skip_reason", "")]
        print(f"กรองเฉพาะ skip_reason มี '{reason_filter}': {len(recs)} ไม้")

    if not _connect():
        return

    # group ตาม (symbol, skip_reason)
    buckets = defaultdict(lambda: {"tp": 0, "sl": 0, "none": 0, "skip": 0, "R": []})
    try:
        for rec in recs:
            res = _simulate_one(rec, bars_forward)
            key = (rec["symbol"], rec.get("skip_reason", "?"))
            b   = buckets[key]
            if res is None:
                b["skip"] += 1
                continue
            b[res] += 1
            rr = (rec.get("tp_dist", 0) / rec.get("sl_dist", 1)) if rec.get("sl_dist") else 0
            if res == "tp":
                b["R"].append(rr)
            elif res == "sl":
                b["R"].append(-1.0)
            # 'none' = ไม่ชนใน window → ไม่นับ R
    finally:
        mt5.shutdown()

    # รายงาน
    print(f"\n{'='*72}")
    print(f"  SHADOW-TRADE EDGE ANALYSIS  (เดินหน้า {bars_forward} แท่ง M15)")
    print(f"  ไม้ที่ระบบ 'อยากเทรด' แต่โดน gate ตัด — ถ้าเทรดจริงจะเป็นยังไง")
    print(f"{'='*72}")
    print(f"  {'Symbol/Reason':<42} {'TP':>4} {'SL':>4} {'WR%':>6} {'E[R]':>7}")
    print(f"  {'-'*70}")

    grand = {"tp": 0, "sl": 0, "R": []}
    for (sym, reason), b in sorted(buckets.items()):
        decided = b["tp"] + b["sl"]
        if decided == 0:
            continue
        wr  = b["tp"] / decided * 100
        eR  = sum(b["R"]) / len(b["R"]) if b["R"] else 0.0
        label = f"{sym} | {reason}"[:42]
        flag  = " ⚠️EDGE" if eR > 0.15 else (" ✓gate-ok" if eR < -0.1 else "")
        print(f"  {label:<42} {b['tp']:>4} {b['sl']:>4} {wr:>5.0f}% {eR:>+6.2f}R{flag}")
        grand["tp"] += b["tp"]; grand["sl"] += b["sl"]; grand["R"] += b["R"]

    print(f"  {'-'*70}")
    g_dec = grand["tp"] + grand["sl"]
    if g_dec:
        g_wr = grand["tp"] / g_dec * 100
        g_eR = sum(grand["R"]) / len(grand["R"]) if grand["R"] else 0.0
        print(f"  {'รวมทั้งหมด':<42} {grand['tp']:>4} {grand['sl']:>4} "
              f"{g_wr:>5.0f}% {g_eR:>+6.2f}R")
    print(f"{'='*72}")
    print("  อ่านผล:")
    print("   E[R] > +0.15  = gate ตัดไม้ 'ดี' ทิ้ง (เข้มเกินไป — ควรผ่อน)")
    print("   E[R] < -0.10  = gate กันไม้ 'แย่' ได้ถูกต้อง (อย่าผ่อน)")
    print("   ~0            = ไม่มี edge ชัด (HOLD ถูกแล้ว)")
    print("  ⚠️ ตัวเลขนี้ยังไม่หักสเปรด/คอมมิชชัน — edge จริงต่ำกว่านี้เล็กน้อย")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--bars-forward", type=int, default=96,
                   help="เดินหน้าดูกี่แท่ง M15 (default 96 = 1 วัน)")
    p.add_argument("--reason", type=str, default="",
                   help="กรองเฉพาะ skip_reason ที่มี substring นี้")
    args = p.parse_args()
    analyze(bars_forward=args.bars_forward, reason_filter=args.reason)