# models/rule_based.py
"""
Rule-Based Trading Strategy — Baseline
ไม่ต้อง train — ใช้ได้ทันทีหลัง feature engineering

วัตถุประสงค์:
1. Baseline สำหรับเปรียบเทียบกับ ML model
2. Fallback เมื่อ ML model ยังไม่พร้อมหรือ confidence ต่ำ
3. เข้าใจง่าย — debug และ explain ได้ทันที

กลยุทธ์หลัก: Multi-Confluence
BUY  = EMA cross ขึ้น + RSI ไม่ overbought + ADX trending + HTF bullish
SELL = EMA cross ลง  + RSI ไม่ oversold  + ADX trending + HTF bearish
"""

import pandas as pd
import numpy as np
import logging
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("models")

# โหลด config
with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)


# ══════════════════════════════════════════════════════════════
# Data Classes — Signal และ Result
# ══════════════════════════════════════════════════════════════
@dataclass
class Signal:
    """ผลลัพธ์จาก strategy ต่อ 1 bar"""
    direction:   int    = 0       # 1=BUY, -1=SELL, 0=HOLD
    confidence:  float  = 0.0     # 0.0-1.0
    reasons:     list   = field(default_factory=list)   # เหตุผลที่ให้สัญญาณ
    blocked_by:  list   = field(default_factory=list)   # เหตุผลที่บล็อก
    score:       float  = 0.0     # raw score ก่อน threshold

    @property
    def is_actionable(self) -> bool:
        """สัญญาณนี้ควรเทรดหรือเปล่า"""
        return self.direction != 0 and self.confidence >= 0.55

    def __str__(self):
        arrow = "↑ BUY" if self.direction == 1 \
           else "↓ SELL" if self.direction == -1 \
           else "→ HOLD"
        return (
            f"{arrow} | conf={self.confidence:.2f} | "
            f"score={self.score:.1f} | "
            f"reasons={self.reasons}"
        )


@dataclass
class BacktestResult:
    """ผลรวมของ backtest"""
    total_trades:   int   = 0
    wins:           int   = 0
    losses:         int   = 0
    total_pnl:      float = 0.0
    gross_win:      float = 0.0
    gross_loss:     float = 0.0
    max_drawdown:   float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / max(self.total_trades, 1)

    @property
    def profit_factor(self) -> float:
        return self.gross_win / max(abs(self.gross_loss), 1e-9)

    @property
    def avg_trade(self) -> float:
        return self.total_pnl / max(self.total_trades, 1)

    def summary(self) -> str:
        return (
            f"Trades={self.total_trades} "
            f"WinRate={self.win_rate:.1%} "
            f"PF={self.profit_factor:.2f} "
            f"PnL={self.total_pnl:+.2f} "
            f"MaxDD={self.max_drawdown:.2f}"
        )


# ══════════════════════════════════════════════════════════════
# Rule-Based Strategy Class
# ══════════════════════════════════════════════════════════════
class RuleBasedStrategy:
    """
    Multi-Confluence Rule-Based Strategy

    ระบบให้คะแนน: BUY และ SELL แยกกัน
    ถ้าคะแนน BUY ≥ threshold → BUY signal
    ถ้าคะแนน SELL ≥ threshold → SELL signal
    ไม่ถึง → HOLD
    """

    VERSION = "1.0"

    def __init__(self,
                 # EMA
                 ema_fast:        int   = 9,
                 ema_slow:        int   = 20,
                 ema_trend:       int   = 50,
                 # RSI
                 rsi_period:      int   = 14,
                 rsi_ob:          float = 65.0,   # overbought — Gold ใช้ 65 ไม่ใช่ 70
                 rsi_os:          float = 35.0,   # oversold
                 # ADX
                 adx_min:         float = 20.0,   # trend ต้องแรงพอ
                 # Confluence threshold
                 buy_threshold:   float = 4.0,    # ต้องได้คะแนน ≥ 4 ถึง BUY
                 sell_threshold:  float = 4.0,
                 # Confidence scaling
                 max_score:       float = 7.0,
                 ):

        self.ema_fast       = ema_fast
        self.ema_slow       = ema_slow
        self.ema_trend      = ema_trend
        self.rsi_period     = rsi_period
        self.rsi_ob         = rsi_ob
        self.rsi_os         = rsi_os
        self.adx_min        = adx_min
        self.buy_threshold  = buy_threshold
        self.sell_threshold = sell_threshold
        self.max_score      = max_score

        log.info(
            f"RuleBasedStrategy v{self.VERSION} initialized | "
            f"EMA {ema_fast}/{ema_slow}/{ema_trend} | "
            f"RSI {rsi_period} OB={rsi_ob} OS={rsi_os} | "
            f"ADX≥{adx_min}"
        )

    # ── Generate Signal (1 row) ────────────────────────────────
    def generate_signal(self, row: pd.Series) -> Signal:
        """
        คำนวณสัญญาณจาก 1 row ของ DataFrame
        ใช้ใน bot/main.py: signal = strategy.generate_signal(df.iloc[-1])
        """
        buy_score  = 0.0
        sell_score = 0.0
        reasons    = []
        blocked    = []

        # ══ BUY CONDITIONS ════════════════════════════════════

        # ── Condition 1: EMA Fast ข้าม EMA Slow ขึ้น (น้ำหนัก 2.0) ──
        ema_f = row.get(f'ema_{self.ema_fast}')
        ema_s = row.get(f'ema_{self.ema_slow}')
        ema_t = row.get(f'ema_{self.ema_trend}')

        if _valid(ema_f, ema_s, ema_t):
            if ema_f > ema_s:
                buy_score += 2.0
                reasons.append(f"EMA{self.ema_fast}>{self.ema_slow}(+2)")
            else:
                sell_score += 2.0

            # EMA อยู่เหนือ EMA trend
            c = row.get('close', 0)
            if c > ema_t:
                buy_score += 1.0
                reasons.append(f"Price>EMA{self.ema_trend}(+1)")
            else:
                sell_score += 1.0

        # ── Condition 2: RSI Zone (น้ำหนัก 1.5) ──────────────
        rsi = row.get(f'rsi_{self.rsi_period}')
        if _valid(rsi):
            if rsi < self.rsi_ob:           # ยังไม่ overbought
                buy_score += 1.0
                reasons.append(f"RSI={rsi:.0f}<{self.rsi_ob}(+1)")
            else:
                blocked.append(f"RSI overbought ({rsi:.0f})")

            if rsi > self.rsi_os:           # ยังไม่ oversold
                buy_score += 0.5

            if 45 < rsi < 65:               # RSI zone bullish
                buy_score += 0.5
                reasons.append("RSI bullish zone(+0.5)")
            elif 35 < rsi < 55:
                sell_score += 0.5
            elif rsi < self.rsi_os:
                sell_score += 1.5

        # ── Condition 3: ADX — trend แรงพอ (น้ำหนัก 1.0) ────
        adx     = row.get('adx')
        adx_pos = row.get('adx_pos')
        adx_neg = row.get('adx_neg')

        if _valid(adx):
            if adx >= self.adx_min:
                if _valid(adx_pos, adx_neg):
                    if adx_pos > adx_neg:
                        buy_score += 1.0
                        reasons.append(f"ADX={adx:.0f} +DI>{adx:.0f}(+1)")
                    else:
                        sell_score += 1.0
                else:
                    buy_score += 0.5    # ADX trending แต่ไม่รู้ทิศ
            else:
                blocked.append(f"ADX ต่ำ ({adx:.0f}<{self.adx_min})")

        # ── Condition 4: MACD Histogram (น้ำหนัก 1.0) ────────
        macd_hist = row.get('macd_hist')
        if _valid(macd_hist):
            if macd_hist > 0:
                buy_score += 1.0
                reasons.append(f"MACD hist>0(+1)")
            else:
                sell_score += 1.0

            # MACD histogram กำลังโต
            macd_growing = row.get('macd_hist_growing')
            if macd_growing == 1 and macd_hist > 0:
                buy_score += 0.5
                reasons.append("MACD growing(+0.5)")

        # ── Condition 5: HTF Alignment (น้ำหนัก 1.5) ─────────
        htf_bull = row.get('htf_fully_aligned_bull')
        htf_bear = row.get('htf_fully_aligned_bear')
        htf_conf = row.get('htf_conflict', 0)

        if _valid(htf_bull):
            if htf_bull == 1:
                buy_score += 1.5
                reasons.append("HTF fully aligned bull(+1.5)")
            elif htf_bear == 1:
                sell_score += 1.5

        if htf_conf == 1:
            blocked.append("HTF conflict (H1 vs H4 ขัดกัน)")
            buy_score  *= 0.5    # ลดคะแนนลงครึ่งหนึ่ง
            sell_score *= 0.5

        # ── Condition 6: Supertrend (น้ำหนัก 0.5) ────────────
        st_dir = row.get('supertrend_dir')
        if _valid(st_dir):
            if st_dir == 1:
                buy_score += 0.5
                reasons.append("Supertrend up(+0.5)")
            else:
                sell_score += 0.5

        # ── Condition 7: Price Action (น้ำหนัก 0.5) ──────────
        pa_score = row.get('pa_score', 0)
        if _valid(pa_score):
            if pa_score > 1:
                buy_score += 0.5
                reasons.append(f"PA score={pa_score:.1f}(+0.5)")
            elif pa_score < -1:
                sell_score += 0.5

        # ── Condition 8: Candle Pattern (น้ำหนัก 0.5) ────────
        pat_net = row.get('pat_net_score', 0)
        if _valid(pat_net):
            if pat_net > 0:
                buy_score  += min(pat_net * 0.25, 0.5)
                reasons.append(f"Bull pattern(+{min(pat_net*0.25,0.5):.2f})")
            elif pat_net < 0:
                sell_score += min(abs(pat_net) * 0.25, 0.5)

        # ══ FILTER CONDITIONS (block signal) ═════════════════

        # Squeeze ON — ตลาดสะสมพลัง ยังไม่ควรเทรด
        if row.get('squeeze_on', 0) == 1:
            blocked.append(f"Squeeze ON")
            buy_score  = 0
            sell_score = 0

        # Volatility สูงผิดปกติ — อาจมีข่าว
        atr_ratio = row.get('atr_ratio', 1.0)
        if _valid(atr_ratio) and atr_ratio > 2.5:
            blocked.append(f"ATR ratio สูง ({atr_ratio:.1f}x)")
            buy_score  *= 0.3
            sell_score *= 0.3

        # นอก trading session
        in_london = row.get('is_london_session', 1)
        in_ny     = row.get('is_ny_session', 1)
        if _valid(in_london, in_ny):
            if in_london == 0 and in_ny == 0:
                blocked.append("นอก London/NY session")
                buy_score  = 0
                sell_score = 0

        # ══ DETERMINE SIGNAL ══════════════════════════════════
        if buy_score >= self.buy_threshold and buy_score > sell_score:
            direction   = 1
            raw_score   = buy_score
        elif sell_score >= self.sell_threshold and sell_score > buy_score:
            direction   = -1
            raw_score   = sell_score
        else:
            direction   = 0
            raw_score   = max(buy_score, sell_score)

        # Confidence = score / max_score (0-1)
        confidence = min(raw_score / self.max_score, 1.0)

        return Signal(
            direction   = direction,
            confidence  = round(confidence, 3),
            reasons     = reasons,
            blocked_by  = blocked,
            score       = round(raw_score, 2),
        )

    # ── Generate Signals (ทั้ง DataFrame) ─────────────────────
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        """
        Generate signal ทุก row — ใช้สำหรับ backtest
        คืนค่า Series ของ direction: 1, 0, -1
        """
        signals = []
        for _, row in df.iterrows():
            sig = self.generate_signal(row)
            signals.append(sig.direction)
        return pd.Series(signals, index=df.index, name='signal')

    # ── Quick Backtest ─────────────────────────────────────────
    def backtest(
        self,
        df:         pd.DataFrame,
        sl_pct:     float = 0.005,   # SL 0.5%
        tp_pct:     float = 0.010,   # TP 1.0%
        commission: float = 0.0001,  # 0.01%
    ) -> BacktestResult:
        """
        Backtest ง่ายๆ ไม่ใช้ vectorbt
        ใช้ตรวจสอบว่า rule-based ทำงานได้หรือเปล่าก่อน
        """
        result      = BacktestResult()
        position    = 0
        entry_price = 0.0
        equity      = 10_000.0
        peak_equity = equity

        signals = self.generate_signals(df)

        for i, (idx, row) in enumerate(df.iterrows()):
            price  = row['close']
            signal = signals.iloc[i]

            # ── ถือ position อยู่ — ตรวจ exit ──────────────────
            if position != 0:
                pnl_pct = (price - entry_price) / entry_price * position

                exit_reason = None
                if pnl_pct >= tp_pct:
                    exit_reason = "TP"
                elif pnl_pct <= -sl_pct:
                    exit_reason = "SL"
                elif signal == -position:
                    exit_reason = "Signal reverse"

                if exit_reason:
                    pnl = pnl_pct * equity - commission * equity
                    equity += pnl
                    result.total_pnl   += pnl
                    result.total_trades += 1

                    if pnl > 0:
                        result.wins      += 1
                        result.gross_win += pnl
                    else:
                        result.losses     += 1
                        result.gross_loss += pnl

                    peak_equity = max(peak_equity, equity)
                    dd = (peak_equity - equity) / peak_equity
                    result.max_drawdown = max(result.max_drawdown, dd)

                    position    = 0
                    entry_price = 0.0

            # ── ไม่มี position — ตรวจ entry ────────────────────
            if position == 0 and signal != 0:
                sig_obj = self.generate_signal(row)
                if sig_obj.confidence >= 0.55:
                    position    = signal
                    entry_price = price

        log.info(f"Backtest: {result.summary()}")
        return result


# ══════════════════════════════════════════════════════════════
# Strategy Variants
# ══════════════════════════════════════════════════════════════
class ConservativeStrategy(RuleBasedStrategy):
    """
    เวอร์ชัน conservative — เทรดน้อยลงแต่คุณภาพสูง
    เหมาะตอนตลาดผันผวนหรือยังไม่มั่นใจ
    """
    def __init__(self):
        super().__init__(
            rsi_ob         = 60.0,    # OB เข้มขึ้น
            rsi_os         = 40.0,    # OS เข้มขึ้น
            adx_min        = 25.0,    # ต้องการ trend แรงกว่า
            buy_threshold  = 5.0,     # threshold สูงกว่า
            sell_threshold = 5.0,
        )


class AggressiveStrategy(RuleBasedStrategy):
    """
    เวอร์ชัน aggressive — เทรดบ่อยขึ้น
    ใช้ตอนตลาดมี trend ชัดเจน
    """
    def __init__(self):
        super().__init__(
            rsi_ob         = 70.0,
            rsi_os         = 30.0,
            adx_min        = 15.0,
            buy_threshold  = 3.0,
            sell_threshold = 3.0,
        )


class GoldStrategy(RuleBasedStrategy):
    """
    ปรับสำหรับ XAUUSD โดยเฉพาะ
    Gold ต้องการ ADX สูงกว่า Forex ปกติ
    และ RSI ที่ extreme กว่า
    """
    def __init__(self):
        super().__init__(
            ema_fast       = 9,
            ema_slow       = 21,    # 21 แทน 20 (classic Gold)
            ema_trend      = 55,    # 55 แทน 50
            rsi_ob         = 68.0,  # Gold มักวิ่ง overbought นาน
            rsi_os         = 32.0,
            adx_min        = 22.0,
            buy_threshold  = 4.5,
            sell_threshold = 4.5,
        )


# ══════════════════════════════════════════════════════════════
# Utility Functions
# ══════════════════════════════════════════════════════════════
def _valid(*values) -> bool:
    """ตรวจว่าทุกค่าไม่ใช่ None และไม่ใช่ NaN"""
    return all(
        v is not None and not (isinstance(v, float) and np.isnan(v))
        for v in values
    )


def compare_strategies(
    df:        pd.DataFrame,
    strategies: dict = None,
) -> pd.DataFrame:
    """
    เปรียบเทียบหลาย strategy พร้อมกัน
    ใช้หา config ที่ดีที่สุดก่อนไป ML
    """

    if strategies is None:
        strategies = {
            "Default"      : RuleBasedStrategy(),
            "Conservative" : ConservativeStrategy(),
            "Aggressive"   : AggressiveStrategy(),
            "Gold"         : GoldStrategy(),
        }

    rows = []
    for name, strat in strategies.items():
        result = strat.backtest(df)
        rows.append({
            "strategy"     : name,
            "trades"       : result.total_trades,
            "win_rate"     : f"{result.win_rate:.1%}",
            "profit_factor": f"{result.profit_factor:.2f}",
            "total_pnl"    : f"{result.total_pnl:+.2f}",
            "max_dd"       : f"{result.max_drawdown:.2%}",
        })

    df_result = pd.DataFrame(rows)
    print("\n" + df_result.to_string(index=False))
    return df_result