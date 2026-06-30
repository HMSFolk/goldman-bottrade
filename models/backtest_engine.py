# models/backtest_engine.py
"""
Event-Driven Backtest Engine — ตรงกับระบบ live
════════════════════════════════════════════════════════════════════
ทำไมต้องมีไฟล์นี้ (อ่านก่อนใช้):
  models/backtest.py (ตัวเก่า) ใช้ vectorbt แบบ:
    • long-only  → entries=BUY, exits=SELL (SELL = ปิดไม้ ไม่ใช่ short)
    • SL/TP คงที่ 0.5%/1.0% → RR 2.0 (ไม่อ่าน tp_ratio จาก config)
    • min_conf 0.62 (ไม่ใช่ 0.52 ที่ใช้จริง)
  → ตัวเลขที่ได้ไม่ตรงระบบจริง (long+short, ATR-based SL, RR จาก config)

Engine นี้ออกแบบให้ "ตรง live by construction":
    • long + short เต็มรูปแบบ
    • SL/TP มาจาก StrategyV1 ตัวเดียวกับที่ bot ใช้ (ATR-based)
    • จำลอง breakeven / partial-close / time-exit ตาม trade_management ใน config
    • คิด spread + commission
    • รายงาน "expectancy" (กำไรคาดหวังต่อไม้) — ตัวที่ตัวเก่าไม่มี
      expectancy คือคำตอบว่า "ระบบบวกที่ RR จริงหรือเปล่า" ไม่ใช่ WR ลอยๆ

หลักการเลี่ยง look-ahead:
    • สัญญาณคำนวณจากข้อมูลถึง bar i (close[i])
    • เข้าไม้ที่ "open ของ bar ถัดไป" (i+1) — ราคาที่เทรดได้จริง
    • SL/TP เช็คด้วย high/low ของแต่ละ bar หลังเข้า
    • ถ้า bar เดียวแตะทั้ง SL และ TP → ถือว่าโดน SL ก่อน (conservative)

การทดสอบ: ดู __main__ ของ test_backtest_engine.py — มี unit test คณิตครบ
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

log = logging.getLogger("models")


# ══════════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════════
@dataclass
class Trade:
    """1 ไม้ที่จบแล้ว"""
    entry_idx:   int
    exit_idx:    int
    direction:   int        # 1=long -1=short
    entry_price: float
    exit_price:  float
    sl_price:    float
    tp_price:    float
    risk_dist:   float      # ระยะ SL ตอนเข้า (price units) — ใช้ normalize เป็น R
    pnl_price:   float      # กำไร/ขาดทุน หน่วยราคา (สุทธิหลัง cost)
    pnl_R:       float      # กำไร/ขาดทุน หน่วย R (pnl / risk)
    pnl_pct:     float      # กำไร/ขาดทุน % ของราคาเข้า
    bars_held:   int
    exit_reason: str        # "tp" | "sl" | "time" | "be" | "partial+..." | "eod"


@dataclass
class BacktestResult:
    """ผลรวมของทั้ง backtest — เน้น expectancy"""
    symbol:          str
    n_trades:        int
    win_rate:        float          # %
    expectancy_R:    float          # กำไรคาดหวังต่อไม้ (หน่วย R) ← ตัวชี้ขาด
    expectancy_pct:  float          # กำไรคาดหวังต่อไม้ (% ของราคา)
    profit_factor:   float
    avg_win_R:       float
    avg_loss_R:      float
    total_return_pct:float          # ผลตอบแทนสะสม (compounding ตาม risk_per_trade)
    max_drawdown_pct:float
    sharpe_per_trade:float
    rr_used:         float          # RR เฉลี่ยจริงตอนเข้า (sanity check vs config)
    breakeven_wr:    float          # WR คุ้มทุนที่ RR นี้ — เทียบกับ win_rate ได้เลย
    trades:          list = field(default_factory=list)

    def summary(self) -> str:
        verdict = "✅ บวก" if self.expectancy_R > 0 else "❌ ลบ"
        return (
            f"{verdict} {self.symbol} | trades={self.n_trades} | "
            f"WR={self.win_rate:.1f}% (คุ้มทุน {self.breakeven_wr:.1f}%) | "
            f"E={self.expectancy_R:+.3f}R | PF={self.profit_factor:.2f} | "
            f"RR={self.rr_used:.2f} | ret={self.total_return_pct:+.1f}% | "
            f"DD={self.max_drawdown_pct:.1f}%"
        )


# ══════════════════════════════════════════════════════════════════
# Core Simulation (pure + testable)
# ══════════════════════════════════════════════════════════════════
def simulate(
    open_:        np.ndarray,
    high:         np.ndarray,
    low:          np.ndarray,
    close:        np.ndarray,
    direction:    np.ndarray,     # ต่อ bar: 1=long -1=short 0=ไม่เข้า (อ้างอิง info ถึง bar นั้น)
    sl_dist:      np.ndarray,     # ระยะ SL (price units) ต่อ bar สัญญาณ
    tp_dist:      np.ndarray,     # ระยะ TP (price units) ต่อ bar สัญญาณ
    *,
    spread:       float = 0.0,    # spread (price units) — entry/exit ข้ามสเปรด
    commission:   float = 0.0,    # ค่าคอม (price units) ต่อ "ด้าน" (เข้า+ออก = 2 ด้าน)
    max_hold_bars:int   = 32,     # ถือเกินนี้ → time-exit
    cooldown_bars:int   = 1,      # พักกี่ bar หลังปิดก่อนเข้าใหม่
    be_after_R:   float | None = None,   # ขยับ SL เป็นทุนหลังกำไรถึงกี่ R (None=ปิด)
    partial_at_R: float | None = None,   # ปิดบางส่วนเมื่อถึงกี่ R (None=ปิด)
    partial_pct:  float = 0.5,           # ปิดสัดส่วนเท่าไรตอน partial
    trail_start_pct: float | None = None,  # เริ่ม trailing เมื่อกำไร ≥ % ของราคา (None=ปิด)
    trail_dist_pct:  float = 0.002,        # ระยะ trail (% ของราคา)
) -> list[Trade]:
    """
    จำลองการเทรดทีละ bar — long + short, 1 position ต่อครั้ง

    การจัดการ position — "ตรงกับ bot/position_manager.py":
      • SL / TP คงที่จากตอนเข้า — มีเสมอ
      • time-exit เมื่อถือเกิน max_hold_bars
      • breakeven: ขยับ SL→entry เมื่อกำไรถึง be_after_R
      • partial: ปิดบางส่วนเมื่อกำไรถึง partial_at_R (ครั้งเดียว)
      • trailing (pct-based): กำไร ≥ trail_start_pct → trail SL ห่าง trail_dist_pct
        ทั้งหมด ratchet ทางเดียว (SL ขยับเข้าหากำไรเท่านั้น)

    คืน list ของ Trade ที่จบแล้ว
    """
    n = len(close)
    trades: list[Trade] = []
    i = 0

    while i < n - 1:
        d = int(direction[i])
        if d == 0 or sl_dist[i] <= 0 or tp_dist[i] <= 0:
            i += 1
            continue

        # ── เข้าไม้ที่ open ของ bar ถัดไป (เลี่ยง look-ahead) ──
        entry_idx = i + 1
        raw_entry = float(open_[entry_idx])
        # ข้ามสเปรด: long ซื้อที่ ask (สูงกว่า), short ขายที่ bid (ต่ำกว่า)
        entry = raw_entry + (spread / 2.0) * d
        r     = float(sl_dist[i])           # ระยะความเสี่ยง (R)
        tp_d  = float(tp_dist[i])

        if d == 1:
            sl_price = entry - r
            tp_price = entry + tp_d
        else:
            sl_price = entry + r
            tp_price = entry - tp_d

        # ── เดินไปข้างหน้าหา exit ──
        exit_idx    = entry_idx
        exit_price  = float(close[entry_idx])
        exit_reason = "eod"
        realized    = 0.0          # กำไรสะสมจาก partial (price units, ต่อ 1 unit เต็ม)
        remaining   = 1.0          # สัดส่วน position ที่เหลือ
        cur_sl      = sl_price
        moved_be    = False
        did_partial = False
        trailed     = False        # SL เคยถูก trailing ขยับไหม

        j = entry_idx
        last_j = min(entry_idx + max_hold_bars, n - 1)
        while j <= last_j:
            hi = float(high[j]); lo = float(low[j])

            # กำไรปัจจุบัน (วัดจาก high/low สุดทางในทิศที่ได้เปรียบ) เป็น R
            if d == 1:
                fav_price = hi
                adv_R = (fav_price - entry) / r
            else:
                fav_price = lo
                adv_R = (entry - fav_price) / r

            # partial close: ปิดบางส่วนเมื่อถึง partial_at_R (ครั้งเดียว)
            if (partial_at_R is not None and not did_partial
                    and adv_R >= partial_at_R):
                pc_price = entry + d * (partial_at_R * r)   # ปิดที่ระดับ partial
                realized += partial_pct * d * (pc_price - entry)
                remaining -= partial_pct
                did_partial = True

            # breakeven: ขยับ SL เป็นราคาเข้าเมื่อกำไรถึง be_after_R
            if (be_after_R is not None and not moved_be
                    and adv_R >= be_after_R):
                cur_sl   = entry
                moved_be = True

            # เช็คโดน SL ก่อน (conservative ถ้า bar เดียวแตะทั้งคู่)
            hit_sl = (lo <= cur_sl) if d == 1 else (hi >= cur_sl)
            hit_tp = (hi >= tp_price) if d == 1 else (lo <= tp_price)

            if hit_sl:
                exit_price  = cur_sl
                exit_idx    = j
                if trailed and ((d == 1 and cur_sl > sl_price) or
                                (d == -1 and cur_sl < sl_price)):
                    exit_reason = "trail"      # SL ถูก trail แล้วโดน
                elif moved_be and cur_sl == entry:
                    exit_reason = "be"
                else:
                    exit_reason = "sl"
                break
            if hit_tp:
                exit_price  = tp_price
                exit_idx    = j
                exit_reason = "tp"
                break

            # trailing stop (pct-based) — ตรงกับ position_manager:
            # กำไร ≥ trail_start_pct (% ของราคา) → trail SL ห่าง trail_dist_pct
            # ใช้ close ของ bar นี้เป็นฐาน (เลี่ยง intrabar look-ahead)
            if trail_start_pct is not None:
                cp = float(close[j])
                profit_pct = d * (cp - entry) / entry
                if profit_pct >= trail_start_pct:
                    cand = cp - d * (cp * trail_dist_pct)
                    if d == 1 and cand > cur_sl:
                        cur_sl = cand; trailed = True
                    elif d == -1 and cand < cur_sl:
                        cur_sl = cand; trailed = True

            if j == last_j:
                exit_price  = float(close[j])
                exit_idx    = j
                exit_reason = "time"
                break
            j += 1

        # ── คิด PnL (รวม partial + ส่วนที่เหลือ) ──
        # ออกจากตลาดก็ข้ามสเปรดอีกด้าน + commission 2 ด้าน
        exit_fill = exit_price - (spread / 2.0) * d
        pnl_remaining = remaining * d * (exit_fill - entry)
        gross = realized + pnl_remaining
        cost  = commission * 2.0
        pnl_price = gross - cost

        pnl_R   = pnl_price / r if r > 0 else 0.0
        pnl_pct = pnl_price / entry * 100 if entry > 0 else 0.0

        if exit_reason in ("partial", "tp") and did_partial:
            exit_reason = "partial+" + exit_reason

        trades.append(Trade(
            entry_idx=entry_idx, exit_idx=exit_idx, direction=d,
            entry_price=round(entry, 6), exit_price=round(exit_price, 6),
            sl_price=round(sl_price, 6), tp_price=round(tp_price, 6),
            risk_dist=round(r, 6),
            pnl_price=round(pnl_price, 6), pnl_R=round(pnl_R, 4),
            pnl_pct=round(pnl_pct, 4),
            bars_held=exit_idx - entry_idx, exit_reason=exit_reason,
        ))

        # cooldown แล้วหาไม้ถัดไป
        i = exit_idx + cooldown_bars

    return trades


# ══════════════════════════════════════════════════════════════════
# Metrics
# ══════════════════════════════════════════════════════════════════
def compute_metrics(
    trades:         list[Trade],
    symbol:         str,
    risk_per_trade: float = 0.01,
) -> BacktestResult:
    """
    สรุป metrics จาก list[Trade] — เน้น expectancy + breakeven WR

    total_return: จำลอง compounding โดยให้แต่ละไม้เสี่ยง risk_per_trade ของพอร์ต
                  → equity *= (1 + pnl_R * risk_per_trade)
    """
    if not trades:
        return BacktestResult(
            symbol=symbol, n_trades=0, win_rate=0.0,
            expectancy_R=0.0, expectancy_pct=0.0, profit_factor=0.0,
            avg_win_R=0.0, avg_loss_R=0.0, total_return_pct=0.0,
            max_drawdown_pct=0.0, sharpe_per_trade=0.0, rr_used=0.0,
            breakeven_wr=0.0, trades=[],
        )

    pnl_R   = np.array([t.pnl_R   for t in trades], dtype=float)
    pnl_pct = np.array([t.pnl_pct for t in trades], dtype=float)
    wins    = pnl_R[pnl_R > 0]
    losses  = pnl_R[pnl_R <= 0]

    n        = len(trades)
    win_rate = len(wins) / n * 100.0
    gross_w  = float(wins.sum())
    gross_l  = float(-losses.sum())
    pf       = gross_w / gross_l if gross_l > 0 else float("inf")

    avg_win_R  = float(wins.mean())   if len(wins)   else 0.0
    avg_loss_R = float(-losses.mean()) if len(losses) else 0.0

    # RR เฉลี่ยจริง = tp_dist/sl_dist ตอนเข้า (เช็คว่าตรง config tp_ratio ไหม)
    rr = np.array([
        abs(t.tp_price - t.entry_price) / t.risk_dist
        for t in trades if t.risk_dist > 0
    ], dtype=float)
    rr_used = float(rr.mean()) if len(rr) else 0.0
    breakeven_wr = 1.0 / (1.0 + rr_used) * 100.0 if rr_used > 0 else 0.0

    # equity curve (compounding ตาม risk_per_trade)
    equity = np.cumprod(1.0 + pnl_R * risk_per_trade)
    total_return = (equity[-1] - 1.0) * 100.0
    peak = np.maximum.accumulate(equity)
    dd   = (equity - peak) / peak
    max_dd = float(-dd.min()) * 100.0

    sharpe = (
        float(pnl_R.mean() / pnl_R.std() * np.sqrt(n))
        if pnl_R.std() > 0 else 0.0
    )

    return BacktestResult(
        symbol=symbol, n_trades=n, win_rate=round(win_rate, 2),
        expectancy_R=round(float(pnl_R.mean()), 4),
        expectancy_pct=round(float(pnl_pct.mean()), 4),
        profit_factor=round(pf, 3) if np.isfinite(pf) else 999.0,
        avg_win_R=round(avg_win_R, 3), avg_loss_R=round(avg_loss_R, 3),
        total_return_pct=round(total_return, 2),
        max_drawdown_pct=round(max_dd, 2),
        sharpe_per_trade=round(sharpe, 3),
        rr_used=round(rr_used, 3),
        breakeven_wr=round(breakeven_wr, 2),
        trades=trades,
    )


# ══════════════════════════════════════════════════════════════════
# Signal Builder — ใช้ StrategyV1 ตัวเดียวกับ live (faithful)
# ══════════════════════════════════════════════════════════════════
def build_signals_from_strategy(
    df:     pd.DataFrame,
    symbol: str,
    warmup: int = 250,
    apply_regime_block: bool = True,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    เดินทีละ bar เรียก StrategyV1.evaluate() — โค้ดตัดสินใจตัวเดียวกับ bot

    คืน (direction, sl_dist, tp_dist) เป็น array ความยาวเท่า df
    ช้า (เรียก ensemble ต่อ bar) แต่ "ตรง live by construction"

    ✅ FIX (2026-06-29): apply_regime_block=True จะใส่ HARD BLOCK เหมือน main.py
       (ห้าม BUY ใน trending_down, ห้าม SELL ใน trending_up) → ตรง live ขึ้น
       เดิม build_signals ไม่รวม regime block → backtest overcount ไม้ที่ถูกบล็อก
    """
    from models.strategies.strategy_v1 import StrategyV1

    strat = StrategyV1()
    detector = None
    if apply_regime_block:
        try:
            from features.regime import RegimeDetector
            detector = RegimeDetector()
            # ✅ FIX (2026-06-30): ปิด cache ตอน backtest — ต้นตอ regime ค้าง "uncertain"
            #   detect() cache key=symbol + TTL 15 นาที(เวลาจริง) แต่ backtest เดินพันแท่ง
            #   ในไม่กี่วินาที → แท่งแรก window<min_bars ได้ uncertain → cache → ทุกแท่ง
            #   ถัดมาดึง uncertain เดิมไม่คำนวณใหม่ ตั้ง ttl=0 ให้ detect ใหม่ทุก bar
            detector._cache_ttl_minutes = 0
        except Exception as e:
            log.warning(f"RegimeDetector ไม่พร้อม ({e}) — ข้าม regime block")

    n = len(df)
    direction = np.zeros(n, dtype=int)
    sl_dist   = np.zeros(n, dtype=float)
    tp_dist   = np.zeros(n, dtype=float)

    for i in range(warmup, n):
        window = df.iloc[: i + 1]

        # ✅ FIX (2026-06-30): detect regime ครั้งเดียวต่อ bar ด้วย detector ตัวเดียว
        #   (hysteresis สะสมต่อเนื่อง = ตรง live) แล้วส่งเข้า evaluate + ใช้ค่าเดียวกัน
        #   ตอน HARD BLOCK เดิม v1 detect เองอีกตัว + build_signals detect อีกตัว →
        #   คนละ detector → hysteresis ไม่สะสม → regime ค้าง uncertain ทั้ง backtest
        regime_state = None
        if detector is not None:
            try:
                regime_state = detector.detect(window, symbol)
            except Exception:
                pass

        setup = strat.evaluate(window, symbol, regime=regime_state)
        if not setup.is_valid:
            continue

        d = setup.direction
        # ── HARD BLOCK เหมือน main.py (ใช้ regime ตัวเดียวกับที่ส่งเข้า evaluate) ──
        if regime_state is not None:
            regime_name = str(regime_state).lower()
            if d == 1 and 'trending_down' in regime_name:
                continue   # ห้าม BUY ใน trending_down
            if d == -1 and 'trending_up' in regime_name:
                continue   # ห้าม SELL ใน trending_up

        direction[i] = d
        sl_dist[i]   = setup.sl_distance
        tp_dist[i]   = setup.tp_distance

    return direction, sl_dist, tp_dist


# ══════════════════════════════════════════════════════════════════
# Driver
# ══════════════════════════════════════════════════════════════════
def run_event_backtest(
    df:     pd.DataFrame,
    symbol: str,
    *,
    signals=None,           # (direction, sl_dist, tp_dist) ถ้ามีแล้ว ไม่ต้อง gen ใหม่
    timeframe: str = "M15",
) -> BacktestResult:
    """
    รัน event-driven backtest บน df ที่มี features ครบ

    อ่านพารามิเตอร์ทั้งหมดจาก config (tp_ratio, spread, trade_management, risk)
    → ผลที่ได้สะท้อนระบบ live
    """
    from config import get_config
    CFG = get_config()

    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            raise ValueError(f"df ขาดคอลัมน์ '{col}' — ต้องมี OHLC")

    if signals is None:
        signals = build_signals_from_strategy(df, symbol)
    direction, sl_dist, tp_dist = signals

    # ── พารามิเตอร์จาก config (trade_management) ─────────────────
    # ⚠️ NOTE (2026-06-29): live ยังไม่ wired trade-mgmt (ไม่มี position_manager)
    #   ตอนนี้ config ตั้ง enable_* = false → be_R/part_R/trail = None,
    #   max_hold ใช้ค่า default → engine รันแบบ "pure SL/TP" = ตรงกับ live จริง
    #   เมื่อ wire trade-mgmt เข้า executor แล้ว ค่อยเปิด enable_* ให้ตรงกัน
    tf_min   = {"M1":1,"M5":5,"M15":15,"M30":30,"H1":60,"H4":240}.get(timeframe, 15)
    tm       = CFG.get("trade_management", {})

    max_hold = (int(tm.get("max_hold_hours", 8) * 60 / tf_min)
                if tm.get("enable_time_exit") else 480)
    be_R     = tm.get("be_after_rr") if tm.get("enable_breakeven") else None
    part_R   = tm.get("partial_close_rr") if tm.get("enable_partial_close") else None
    part_pct = float(tm.get("partial_close_pct", 0.5))
    trail_start = tm.get("trailing_start_pct") if tm.get("enable_trailing") else None
    trail_dist  = float(tm.get("trailing_distance_pct", 0.002))

    # spread/commission จาก spread_filter limits (ใช้ครึ่งหนึ่งของ limit เป็น cost จริง)
    sf_lim   = CFG.get("spread_filter", {}).get("limits", {})
    spread   = float(sf_lim.get(symbol, sf_lim.get("default", 0.0))) * 0.5
    risk_pt  = float(CFG.get("risk", {}).get("risk_per_trade", 0.01))

    trades = simulate(
        df["open"].to_numpy(), df["high"].to_numpy(),
        df["low"].to_numpy(),  df["close"].to_numpy(),
        direction, sl_dist, tp_dist,
        spread=spread, commission=0.0,
        max_hold_bars=max_hold, cooldown_bars=1,
        be_after_R=be_R, partial_at_R=part_R, partial_pct=part_pct,
        trail_start_pct=trail_start, trail_dist_pct=trail_dist,
    )

    result = compute_metrics(trades, symbol, risk_per_trade=risk_pt)
    log.info(result.summary())
    return result


# ══════════════════════════════════════════════════════════════════
# Walk-Forward (OOS) — ใช้ StrategyV1 ตรง live แทน vectorbt ตัวเก่า
# ══════════════════════════════════════════════════════════════════
def walk_forward_engine(
    df:        pd.DataFrame,
    symbol:    str,
    n_splits:  int = 5,
    timeframe: str = "M15",
) -> dict:
    """
    แบ่ง df เป็น n_splits ช่วงเวลาเรียงตามเวลา (ไม่สุ่ม) แล้วรัน event-backtest
    ทีละช่วง — วัดว่าระบบ "บวกสม่ำเสมอข้ามช่วงเวลา" ไหม (กัน overfit ช่วงเดียว)

    คืน dict: per_fold (list), mean_expectancy_R, consistency (สัดส่วน fold ที่ E>0),
              is_robust (bool)
    """
    n = len(df)
    fold_size = n // (n_splits + 1)
    folds = []

    for k in range(1, n_splits + 1):
        test = df.iloc[k * fold_size : (k + 1) * fold_size]
        if len(test) < 250:        # เล็กไปไม่พอเทรด
            continue
        res = run_event_backtest(test, symbol, timeframe=timeframe)
        folds.append({
            "fold":         k,
            "trades":       res.n_trades,
            "expectancy_R": res.expectancy_R,
            "win_rate":     res.win_rate,
            "return_pct":   res.total_return_pct,
        })

    if not folds:
        return {"per_fold": [], "mean_expectancy_R": 0.0,
                "consistency": 0.0, "is_robust": False}

    pos = sum(1 for f in folds if f["expectancy_R"] > 0)
    mean_e = sum(f["expectancy_R"] for f in folds) / len(folds)
    consistency = pos / len(folds)
    return {
        "per_fold":          folds,
        "mean_expectancy_R": round(mean_e, 4),
        "consistency":       round(consistency, 3),
        "is_robust":         bool(mean_e > 0 and consistency >= 0.6),
    }


def _grade(expectancy_R: float) -> str:
    """ตัดเกรดจาก expectancy ต่อไม้ (ตัวชี้ขาดว่าระบบบวกจริงไหม)"""
    if expectancy_R >= 0.25: return "A"
    if expectancy_R >= 0.10: return "B"
    if expectancy_R >= 0.00: return "C"
    if expectancy_R >= -0.10: return "D"
    return "F"


def deploy_checklist_v2(
    symbol:    str,
    timeframe: str = "M15",
) -> dict:
    """
    Deploy checklist บน event-engine ล้วน (แทน backtest.run_deploy_checklist เก่า)
    ทุกข้อใช้ StrategyV1 + RR จาก config = ตรง live

    6 ข้อตรวจ:
      1. models_exist     — xgb + lgbm มีครบ
      2. training_metrics — อ่าน train_report (acc≥50% f1≥40%)
      3. backtest         — event-backtest เต็มชุด: expectancy_R > 0 + trades≥30
      4. walk_forward     — OOS: mean E>0 + consistency≥60%
      5. overfit_check    — train(70%) vs test(30%): E ไม่ต่างเกิน 0.30R
      6. risk_params      — risk/trade≤2% + daily_loss≤10%

    คืน dict โครงเดียวกับเดิม (retrain_all อ่าน ['backtest']['grade'] + ['pass'] ได้)
    """
    import sys
    from pathlib import Path
    _R = Path(__file__).resolve().parent.parent
    if str(_R) not in sys.path:
        sys.path.insert(0, str(_R))
    from config import get_config
    CFG = get_config()

    PROCESSED = _R / CFG["paths"]["data_processed"]
    MODELS    = _R / CFG["paths"]["models_saved"]
    REPORTS   = _R / CFG["paths"].get("reports", "reports")

    results = {}
    df = pd.read_parquet(
        PROCESSED / f"{symbol}_{timeframe}_features.parquet"
    ).dropna(subset=["label"])

    # ── 1. โมเดลครบ ──────────────────────────────────────────
    exist = {
        "xgb":  (MODELS / f"xgb_{symbol}.pkl").exists(),
        "lgbm": (MODELS / f"lgbm_{symbol}.pkl").exists(),
        "lstm": (MODELS / f"lstm_{symbol}.pth").exists(),
    }
    results["models_exist"] = {
        "pass": exist["xgb"] and exist["lgbm"],
        "detail": exist, "note": "XGB + LGBM ต้องมีอย่างน้อย",
    }

    # ── 2. Training metrics ──────────────────────────────────
    tr_path = REPORTS / f"train_report_{symbol}_{timeframe}.json"
    if tr_path.exists():
        import json
        rep = json.loads(tr_path.read_text())
        ok  = rep.get("mean_accuracy", 0) >= 0.50 and rep.get("mean_f1", 0) >= 0.40
        results["training_metrics"] = {
            "pass": ok,
            "detail": {"accuracy": rep.get("mean_accuracy", 0),
                       "f1": rep.get("mean_f1", 0)},
            "note": "acc≥50% และ f1≥40%",
        }
    else:
        results["training_metrics"] = {
            "pass": False, "detail": "ไม่พบ training report", "note": "รัน train ก่อน",
        }

    # ── 3. Backtest เต็มชุด (event-driven ตรง live) ──────────
    bt = run_event_backtest(df, symbol, timeframe=timeframe)
    results["backtest"] = {
        "pass":  bool(bt.expectancy_R > 0 and bt.n_trades >= 30),
        "grade": _grade(bt.expectancy_R),
        "detail": {
            "expectancy_R": bt.expectancy_R,
            "win_rate":     bt.win_rate,
            "breakeven_wr": bt.breakeven_wr,
            "profit_factor": bt.profit_factor,
            "return_pct":   bt.total_return_pct,
            "max_dd":       bt.max_drawdown_pct,
            "trades":       bt.n_trades,
        },
        "note": "expectancy_R > 0 (บวกจริงที่ RR นี้) + trades≥30",
    }

    # ── 4. Walk-forward OOS ──────────────────────────────────
    wf = walk_forward_engine(df, symbol, n_splits=5, timeframe=timeframe)
    results["walk_forward"] = {
        "pass": wf["is_robust"],
        "detail": {"mean_expectancy_R": wf["mean_expectancy_R"],
                   "consistency": f"{wf['consistency']:.0%}",
                   "folds": len(wf["per_fold"])},
        "note": "mean E>0 + consistency≥60%",
    }

    # ── 5. Overfit check: train(70%) vs test(30%) ────────────
    split = int(len(df) * 0.70)
    bt_tr = run_event_backtest(df.iloc[:split], symbol, timeframe=timeframe)
    bt_te = run_event_backtest(df.iloc[split:], symbol, timeframe=timeframe)
    e_gap = bt_tr.expectancy_R - bt_te.expectancy_R
    results["overfit_check"] = {
        "pass": bool(e_gap < 0.30),
        "detail": {"train_E": bt_tr.expectancy_R, "test_E": bt_te.expectancy_R,
                   "gap_R": round(e_gap, 4)},
        "note": "train E ไม่ดีกว่า test เกิน 0.30R",
    }

    # ── 6. Risk params ───────────────────────────────────────
    daily_loss = CFG.get("circuit_breaker", {}).get("daily", {}).get("loss_pct", 5.0) / 100
    risk_ok = CFG["risk"]["risk_per_trade"] <= 0.02 and daily_loss <= 0.10
    results["risk_params"] = {
        "pass": bool(risk_ok),
        "detail": {"risk_per_trade": CFG["risk"]["risk_per_trade"],
                   "daily_loss_pct": daily_loss},
        "note": "risk/trade≤2% + daily_loss≤10%",
    }

    # ── สรุป ─────────────────────────────────────────────────
    all_pass = all(v.get("pass") for v in results.values())
    log.info(f"\n{'='*60}\nDeploy Checklist v2: {symbol} — "
             f"{'✅ PASS ALL' if all_pass else '⚠️ บางข้อไม่ผ่าน'}\n{'='*60}")
    for name, r in results.items():
        log.info(f"  {'✅' if r.get('pass') else '⚠️'} {name}: {r.get('note','')}")
    return results


if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

    from config import get_config
    CFG = get_config()
    PROCESSED = _ROOT / CFG["paths"]["data_processed"]

    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=CFG["symbols"]["active"])
    ap.add_argument("--timeframe", default="M15")
    ap.add_argument("--checklist", action="store_true",
                    help="รัน deploy checklist v2 (6 ข้อ ตรง live)")
    ap.add_argument("--walk-forward", action="store_true",
                    help="รัน walk-forward OOS อย่างเดียว")
    args = ap.parse_args()

    if args.checklist:
        for sym in args.symbols:
            print("\n" + "=" * 70)
            print(f"DEPLOY CHECKLIST v2 — {sym}")
            print("=" * 70)
            res = deploy_checklist_v2(sym, args.timeframe)
            for name, r in res.items():
                icon = "✅" if r.get("pass") else "⚠️"
                print(f"  {icon} {name:18} {r.get('detail')}")
        sys.exit(0)

    if args.walk_forward:
        for sym in args.symbols:
            path = PROCESSED / f"{sym}_{args.timeframe}_features.parquet"
            if not path.exists():
                print(f"⚠️  ไม่พบ {path} — ข้าม {sym}"); continue
            df = pd.read_parquet(path).dropna(subset=["label"])
            print(f"\n=== Walk-Forward OOS: {sym} ===")
            wf = walk_forward_engine(df, sym, n_splits=5, timeframe=args.timeframe)
            for f in wf["per_fold"]:
                print(f"  Fold {f['fold']}: E={f['expectancy_R']:+.3f}R "
                      f"WR={f['win_rate']:.0f}% trades={f['trades']} "
                      f"ret={f['return_pct']:+.1f}%")
            print(f"  → mean E={wf['mean_expectancy_R']:+.3f}R | "
                  f"consistency={wf['consistency']:.0%} | "
                  f"{'✅ robust' if wf['is_robust'] else '⚠️ ไม่ robust'}")
        sys.exit(0)

    print("\n" + "=" * 70)
    print("EVENT-DRIVEN BACKTEST (ตรงระบบ live: long+short, RR จาก config)")
    print("=" * 70)
    for sym in args.symbols:
        path = PROCESSED / f"{sym}_{args.timeframe}_features.parquet"
        if not path.exists():
            print(f"⚠️  ไม่พบ {path} — ข้าม {sym}")
            continue
        df = pd.read_parquet(path).dropna(subset=["label"])
        res = run_event_backtest(df, sym, timeframe=args.timeframe)
        print(res.summary())