# bot/risk_manager.py
"""
Risk Manager — ระบบป้องกันความเสี่ยง
════════════════════════════════════════════════════════════
ทุก order ต้องผ่านการตรวจทุกข้อก่อนส่ง

ความรับผิดชอบ:
  - คำนวณ lot size ตาม % risk ที่กำหนด
  - ตรวจ spread (ไม่เทรดถ้า spread กว้างเกิน)
  - ตรวจ session (เทรดเฉพาะ London + NY)     ← รองรับ DST ใหม่
  - ตรวจ daily loss limit (หยุดถ้าเสียเกิน X%)
  - ตรวจ max open trades
  - ตรวจ margin level
  - ตรวจ news blackout
  - บันทึก risk metrics ทุก check
════════════════════════════════════════════════════════════
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from collections import deque

import MetaTrader5 as mt5

# ✅ FIX BUG-1: ใช้ get_config() แทน open(config.yaml) โดยตรง
from config import get_config, resolve_symbol   # ✅ NEW: broker resolver
CFG = get_config()

log = logging.getLogger("bot.risk_manager")


# ══════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════
@dataclass
class RiskCheckResult:
    """ผลการตรวจ risk แต่ละข้อ"""
    passed:  bool
    check:   str
    reason:  str  = ""
    value:   float = 0.0    # ค่าที่ตรวจ
    limit:   float = 0.0    # ค่า limit

    def __str__(self):
        icon = "✅" if self.passed else "❌"
        if self.passed:
            return f"{icon} {self.check}"
        return (
            f"{icon} {self.check}: "
            f"{self.reason} "
            f"(value={self.value:.4f} limit={self.limit:.4f})"
        )


@dataclass
class RiskReport:
    """รายงาน risk ทั้งหมดก่อนส่ง order"""
    symbol:    str
    direction: int
    checks:    list = field(default_factory=list)
    lot_size:  float = 0.0
    sl_price:  float = 0.0
    tp_price:  float = 0.0

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> list:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> str:
        ok     = "✅ APPROVED" if self.all_passed else "❌ BLOCKED"
        failed = [c.check for c in self.failed_checks]
        return (
            f"{ok} | {self.symbol} | "
            f"lot={self.lot_size:.2f} | "
            f"failed={failed if failed else 'none'}"
        )


@dataclass
class SpreadResult:
    """ผลการตรวจ spread"""
    ok      : bool           # True = spread ปกติ, ให้เทรดได้
    spread  : float          # spread ปัจจุบัน (หน่วยเดียวกับราคา)
    limit   : float          # spread สูงสุดที่ยอมรับได้
    symbol  : str
    reason  : str = ""       # อธิบายถ้า ok=False

    @property
    def spread_pct(self) -> float:
        """spread เป็น % ของ limit"""
        return (self.spread / self.limit * 100) if self.limit > 0 else 0

    def __str__(self) -> str:
        status = "✅ OK" if self.ok else "🚫 WIDE"
        return (
            f"{status} | {self.symbol} "
            f"spread={self.spread:.5f} "
            f"limit={self.limit:.5f} "
            f"({self.spread_pct:.0f}%)"
        )


@dataclass
class SpreadStats:
    """ประวัติ spread ของ symbol นึง (rolling window)"""
    symbol  : str
    history : deque = field(default_factory=lambda: deque(maxlen=20))

    @property
    def avg(self) -> float:
        return sum(self.history) / len(self.history) if self.history else 0.0

    @property
    def max_recent(self) -> float:
        return max(self.history) if self.history else 0.0

    @property
    def is_widening(self) -> bool:
        """True ถ้า spread กำลังกว้างขึ้น (3 readings ล่าสุด > avg)"""
        if len(self.history) < 5:
            return False
        last3 = list(self.history)[-3:]
        return all(s > self.avg * 1.3 for s in last3)


@dataclass
class CircuitBreakerResult:
    """ผลการตรวจ circuit breaker ใน 1 รอบ"""
    triggered   : bool
    reason      : str  = ""
    level       : str  = ""    # "daily" | "weekly" | "consecutive" | "floating_dd"
    value       : float = 0.0
    limit       : float = 0.0

    def __str__(self) -> str:
        if not self.triggered:
            return "✅ Circuit breaker: All OK"
        return (
            f"🚨 Circuit breaker [{self.level.upper()}]: {self.reason} "
            f"| value={self.value:.2f} limit={self.limit:.2f}"
        )


@dataclass
class CircuitBreakerState:
    """สถานะ persistent ของ circuit breaker (บันทึกใน flags/)"""
    is_triggered     : bool            = False
    trigger_level    : str             = ""
    trigger_reason   : str             = ""
    triggered_at     : Optional[str]   = None
    auto_resume_at   : Optional[str]   = None
    consecutive_count: int             = 0


# ══════════════════════════════════════════════════════════════
# [2] NEW — Session Dataclasses (เพิ่มก่อน class RiskManager)
# ══════════════════════════════════════════════════════════════

@dataclass
class SessionInfo:
    """ข้อมูล session เดียว — ใช้แสดงใน /session Telegram และ dashboard"""
    name       : str
    open_utc   : int    # ชั่วโมงเปิด (UTC, 0-23)
    close_utc  : int    # ชั่วโมงปิด  (UTC, 0-23)
    is_open    : bool   = False
    opens_in_h : float  = 0.0   # ชั่วโมงที่ session นี้จะเปิด (ถ้าปิดอยู่)
    emoji      : str    = ""

    @property
    def range_str(self) -> str:
        return f"{self.open_utc:02d}:00–{self.close_utc:02d}:00 UTC"


@dataclass
class SessionResult:
    """ผลการตรวจ session filter"""
    ok              : bool
    active_sessions : list = field(default_factory=list)   # ["London","New York"]
    blocked_reason  : str  = ""
    next_open_utc   : Optional[datetime] = None     # เมื่อไหร่จะเทรดได้อีก
    is_overlap      : bool = False  # True = London + NY พร้อมกัน (prime zone)
    is_weekend      : bool = False

    def __str__(self) -> str:
        if self.ok:
            overlap = " ⚡OVERLAP" if self.is_overlap else ""
            return f"✅ Sessions: {', '.join(self.active_sessions)}{overlap}"
        if self.next_open_utc:
            hrs = (self.next_open_utc - datetime.now(timezone.utc)
                   ).total_seconds() / 3600
            return f"🚫 {self.blocked_reason} (opens in {hrs:.1f}h)"
        return f"🚫 {self.blocked_reason}"


# ══════════════════════════════════════════════════════════════
# Risk Manager
# ══════════════════════════════════════════════════════════════
class RiskManager:
    """
    ระบบจัดการความเสี่ยงครบวงจร

    วิธีใช้:
        risk   = RiskManager()
        report = risk.check_all(symbol, direction, balance, sl_dist)
        if report.all_passed:
            executor.send_order(lot=report.lot_size, ...)

    Session filter (ใหม่):
        result = risk.check_session(symbol="XAUUSD")
        if not result.ok:
            continue   # ออกนอก session
        if result.is_overlap:
            log.info("⚡ London+NY Overlap — prime zone")
    """

    # [3] NEW — Session definitions (UTC hours, DST-adjusted for NY)
    _SESSIONS = {
        "Sydney"  : {"open": 21, "close":  6, "overnight": True},
        "Tokyo"   : {"open":  0, "close":  9, "overnight": False},
        "London"  : {"open":  7, "close": 16, "overnight": False},
        "New York": {"open": 12, "close": 21, "overnight": False},
    }
    # NY: EDT (UTC-4) open=12 / EST (UTC-5) open=13 — ปรับอัตโนมัติตาม DST
    # London: 07:00-16:00 UTC ตลอดปี (BST/GMT adjust ให้ตรง UTC เอง)

    def __init__(self):
        # ดึงค่าจาก config
        r = CFG['risk']
        self.risk_per_trade    = r['risk_per_trade']       # 0.01 = 1%
        self.max_daily_loss    = r['max_daily_loss_pct']   # 0.05 = 5%
        self.max_open_trades   = r['max_open_trades']      # 3
        self.max_spread        = r['max_spread_points']    # dict
        self.min_lot           = r['min_lot']              # 0.01
        self.max_lot           = r['max_lot']              # 0.50

        # Daily tracking
        self._daily_start_balance: Optional[float] = None   # ✅ FIX BUG-4
        self._daily_start_date:    Optional[str]   = None   # ✅ FIX BUG-4
        self._trades_today:        int             = 0
        self._pnl_today:           float           = 0.0

        # Margin limit
        self.min_margin_level  = 200.0   # % ไม่เทรดถ้า margin < 200%

        # Spread history (rolling stats per symbol)
        self._spread_history: dict[str, SpreadStats] = {}

        # Circuit Breaker state (persistent across restarts)
        self._cb_state_path = Path(
            CFG.get('paths', {}).get('flags', 'flags')
        ) / 'circuit_breaker_state.json'
        self._cb_state = self._load_cb_state()

        log.info(
            f"RiskManager initialized:\n"
            f"  risk_per_trade   = {self.risk_per_trade:.1%}\n"
            f"  max_daily_loss   = {self.max_daily_loss:.1%}\n"
            f"  max_open_trades  = {self.max_open_trades}\n"
            f"  min_lot / max_lot= {self.min_lot} / {self.max_lot}"
        )

    # ══════════════════════════════════════════════════════════
    # Main Check — รวมทุก check
    # ══════════════════════════════════════════════════════════
    def check_all(
        self,
        symbol:     str,
        direction:  int,
        balance:    float,
        equity:     float,
        sl_distance:float,          # ระยะ SL เป็น price units
        margin_level: float = 999,) -> RiskReport:
        """
        รัน risk checks ทั้งหมด
        คืน RiskReport ที่บอกว่า approved หรือ blocked

        เรียกใช้:
            acc    = client.get_account()
            report = risk.check_all(
                symbol     = "XAUUSD",
                direction  = 1,
                balance    = acc['balance'],
                equity     = acc['equity'],
                sl_distance= setup.sl_distance,
                margin_level= acc['margin_level'],
            )
        """
        report = RiskReport(symbol=symbol, direction=direction)

        # ── 1. Daily Reset ────────────────────────────────────
        self._update_daily_tracking(balance)

        # ── 2. ตรวจทีละข้อ ───────────────────────────────────
        checks = [
            self._check_daily_loss(balance),
            self._check_max_open_trades(symbol),
            self._check_spread(symbol),
            self._check_session(symbol),
            self._check_margin_level(margin_level),
            self._check_equity_drawdown(balance, equity),
            self._check_weekend(),
        ]

        report.checks.extend(checks)

        # ถ้าผ่านทุกข้อ — คำนวณ lot size
        if report.all_passed:
            lot = self.calculate_lot_size(symbol, balance, sl_distance)
            report.lot_size = lot

            # ตรวจ lot valid
            lot_check = self._check_lot_size(lot)
            report.checks.append(lot_check)

        log.info(report.summary())
        for c in report.checks:
            if not c.passed:
                log.warning(f"  {c}")

        return report

    # ══════════════════════════════════════════════════════════
    # Lot Size Calculation
    # ══════════════════════════════════════════════════════════
    def calculate_lot_size(
        self,
        symbol:      str,
        balance:     float,
        sl_distance: float, ) -> float:
        """
        คำนวณ lot size ตาม Fixed Fractional Position Sizing

        สูตร:
          risk_amount = balance × risk_per_trade
          pip_value   = tick_value / tick_size
          lot         = risk_amount / (sl_distance × pip_value)

        ตัวอย่าง XAUUSD:
          balance       = $10,000
          risk_per_trade= 1%  → risk_amount = $100
          sl_distance   = 15 pips (0.15 สำหรับ XAUUSD 5-digit)
          tick_value    = $1 per 0.01 lot per pip
          lot           = 100 / (15 × 1/0.01) = 0.07 lot

        ตัวอย่าง EURUSD:
          sl_distance   = 20 pips (0.0020)
          pip_value     = $10 per standard lot per pip
          lot           = 100 / (20 × 10) = 0.5 lot
        """
        # ดึง symbol spec จาก MT5
        # ✅ NEW: symbol เป็นชื่อกลาง resolve ก่อนเรียก MT5
        broker_symbol = resolve_symbol(symbol)
        info = mt5.symbol_info(broker_symbol)
        if info is None:
            log.error(f"ไม่พบ symbol: {symbol} ({broker_symbol})")
            return self.min_lot

        tick_value  = info.trade_tick_value   # $ per tick per 1 lot
        tick_size   = info.trade_tick_size    # ขนาด 1 tick
        lot_step    = info.volume_step        # ขนาด step ของ lot
        lot_min     = max(info.volume_min, self.min_lot)
        lot_max     = min(info.volume_max, self.max_lot)

        if tick_value <= 0 or tick_size <= 0 or sl_distance <= 0:
            log.warning(
                f"Invalid values: "
                f"tick_value={tick_value} "
                f"tick_size={tick_size} "
                f"sl_distance={sl_distance}"
            )
            return lot_min

        # คำนวณ risk amount
        risk_amount = balance * self.risk_per_trade

        # คำนวณ pip value ต่อ 1 lot
        pip_value   = tick_value / tick_size

        # คำนวณ lot
        lot = risk_amount / (sl_distance * pip_value)

        # ปัดให้ตรงกับ lot_step ของ broker
        lot = round(lot / lot_step) * lot_step
        lot = round(lot, 2)

        # Clamp ให้อยู่ใน range
        lot = max(lot_min, min(lot_max, lot))

        log.debug(
            f"Lot calc {symbol}: "
            f"balance=${balance:.0f} "
            f"risk={self.risk_per_trade:.1%} "
            f"risk_amount=${risk_amount:.2f} "
            f"sl_dist={sl_distance:.5f} "
            f"pip_val={pip_value:.4f} "
            f"→ lot={lot:.2f}"
        )

        # ตรวจ actual risk หลัง clamp — ถ้าเกิน 1.5× คืน 0 (ไม่ส่ง order)
        actual_risk = lot * sl_distance * pip_value
        if actual_risk > risk_amount * 1.5:
            log.warning(
                f"Lot clamp risk ${actual_risk:.2f} > intended "
                f"${risk_amount:.2f} × 1.5 — skip order"
                )
            return 0.0

        return lot

    def calculate_sl_tp_price(
        self,
        symbol:      str,
        direction:   int,
        sl_distance: float,
        tp_distance: float,
    ) -> tuple[float, float]:
        """
        คำนวณ SL และ TP price จาก distance
        ใช้ราคา ask/bid ปัจจุบัน

        BUY:  entry=ask  SL=ask-sl_dist  TP=ask+tp_dist
        SELL: entry=bid  SL=bid+sl_dist  TP=bid-tp_dist
        """
        # ✅ NEW: symbol เป็นชื่อกลาง resolve ก่อนเรียก MT5
        tick   = mt5.symbol_info_tick(resolve_symbol(symbol))
        info   = mt5.symbol_info(resolve_symbol(symbol))

        if tick is None or info is None:
            raise ValueError(f"ไม่มีข้อมูล {symbol}")

        digits = info.digits

        if direction == 1:    # BUY
            entry  = tick.ask
            sl     = entry - sl_distance
            tp     = entry + tp_distance
        else:                 # SELL
            entry  = tick.bid
            sl     = entry + sl_distance
            tp     = entry - tp_distance

        sl = round(sl, digits)
        tp = round(tp, digits)

        return sl, tp

    # ══════════════════════════════════════════════════════════
    # Individual Checks
    # ══════════════════════════════════════════════════════════
    def _check_daily_loss(
        self, current_balance: float ) -> RiskCheckResult:
        """
        หยุดเทรดถ้าขาดทุนเกิน max_daily_loss% ของ balance เริ่มวัน

        เหตุผล:
        ป้องกัน revenge trading — ยิ่งเสียยิ่งอยากได้คืน
        จนเสียหมดพอร์ตได้ใน 1 วัน
        """
        if self._daily_start_balance is None:
            self._daily_start_balance = current_balance

        start = self._daily_start_balance

        # ✅ FIX BUG-2: ป้องกัน ZeroDivisionError ถ้า start=0
        if start <= 0:
            log.warning("daily_start_balance = 0 — skip daily loss check")
            return RiskCheckResult(
                passed = True,
                check  = "daily_loss",
                reason = "start_balance=0 (skip)",
            )

        # คำนวณ loss %
        loss_pct = (start - current_balance) / start

        limit   = self.max_daily_loss

        if loss_pct >= limit:
            return RiskCheckResult(
                passed = False,
                check  = "daily_loss",
                reason = (
                    f"เสียวันนี้ {loss_pct:.1%} ≥ "
                    f"limit {limit:.1%} "
                    f"(start=${start:.0f} "
                    f"now=${current_balance:.0f})"
                ),
                value  = loss_pct,
                limit  = limit,
            )

        # เตือนถ้าใกล้ limit
        if loss_pct >= limit * 0.80:
            log.warning(
                f"⚠️ Daily loss {loss_pct:.1%} "
                f"ใกล้ limit {limit:.1%}"
            )

        return RiskCheckResult(
            passed = True,
            check  = "daily_loss",
            value  = loss_pct,
            limit  = limit,
        )

    def _check_max_open_trades(
        self, symbol: str
    ) -> RiskCheckResult:
        """
        จำกัดจำนวน open position พร้อมกัน

        เหตุผล:
        ถ้าเปิดมากเกินไป margin ถูกใช้หมด
        และ risk รวมสูงเกินควบคุม
        """
        positions = mt5.positions_get() or []
        n_open    = len(positions)
        limit     = self.max_open_trades

        if n_open >= limit:
            return RiskCheckResult(
                passed = False,
                check  = "max_open_trades",
                reason = f"open={n_open} ≥ limit={limit}",
                value  = n_open,
                limit  = limit,
            )

        # ตรวจว่า symbol นี้มี position อยู่แล้วไหม
        # ✅ NEW: symbol เป็นชื่อกลาง resolve ก่อนถาม MT5
        sym_positions = mt5.positions_get(symbol=resolve_symbol(symbol)) or []
        if len(sym_positions) > 0:
            return RiskCheckResult(
                passed = False,
                check  = "symbol_already_open",
                reason = (
                    f"{symbol} มี position อยู่แล้ว "
                    f"{len(sym_positions)} รายการ"
                ),
                value  = len(sym_positions),
                limit  = 1,
            )

        return RiskCheckResult(
            passed = True,
            check  = "max_open_trades",
            value  = n_open,
            limit  = limit,
        )

    def _check_spread(self, symbol: str) -> RiskCheckResult:
        """
        ตรวจ spread ปัจจุบัน

        เหตุผล:
        Spread กว้าง = เสียค่าธรรมเนียมมาก
        เกิดตอนข่าว, ตลาดเปิด/ปิด, liquidity ต่ำ
        XAUUSD ปกติ 20-30 points — ถ้า > 60 อย่าเทรด
        """
        # ✅ NEW: symbol เป็นชื่อกลาง resolve ก่อนเรียก MT5
        broker_symbol = resolve_symbol(symbol)
        tick  = mt5.symbol_info_tick(broker_symbol)
        info  = mt5.symbol_info(broker_symbol)

        if tick is None or info is None:
            return RiskCheckResult(
                passed = False,
                check  = "spread",
                reason = f"ไม่มีข้อมูล {symbol}",
            )

        # คำนวณ spread เป็น points
        spread_pts = round((tick.ask - tick.bid) / info.point)

        # ดึง limit จาก config (แต่ละ symbol ต่างกัน)
        max_spread_cfg = CFG['risk']['max_spread_points']
        if isinstance(max_spread_cfg, dict):
            limit = max_spread_cfg.get(
                symbol,
                max_spread_cfg.get('default', 30)
            )
        else:
            limit = int(max_spread_cfg)

        if spread_pts > limit:
            return RiskCheckResult(
                passed = False,
                check  = "spread",
                reason = (
                    f"spread={spread_pts}pts > "
                    f"limit={limit}pts"
                ),
                value  = spread_pts,
                limit  = limit,
            )

        return RiskCheckResult(
            passed = True,
            check  = "spread",
            value  = spread_pts,
            limit  = limit,
        )

    def _check_session(
        self, symbol: str ) -> RiskCheckResult:
        """
        เทรดเฉพาะ London + NY session (simple check ใน check_all)
        สำหรับ full session check ใช้ check_session() แทน

        Gold (XAUUSD): ดีที่สุดช่วง 07:00-20:00 UTC
        Forex majors: London 07-16 | NY 12-21

        เหตุผล:
        ช่วง Asian session (00:00-07:00 UTC):
        - Volume ต่ำ
        - Spread กว้าง
        - False signal มากกว่า
        """
        hour_utc = datetime.now(timezone.utc).hour

        # กำหนดชั่วโมงที่อนุญาตจาก config
        session_cfg = CFG['session']
        start       = session_cfg['allowed_hours_utc']['start']  # 7
        end         = session_cfg['allowed_hours_utc']['end']     # 20

        in_session = start <= hour_utc <= end

        if not in_session:
            # คำนวณว่าอีกกี่ชั่วโมงจะเปิด session
            if hour_utc < start:
                hours_to_open = start - hour_utc
            else:
                hours_to_open = 24 - hour_utc + start

            return RiskCheckResult(
                passed = False,
                check  = "session",
                reason = (
                    f"outside session "
                    f"({hour_utc:02d}:xx UTC | "
                    f"allowed={start:02d}-{end:02d}) "
                    f"opens in {hours_to_open}h"
                ),
                value  = hour_utc,
                limit  = start,
            )

        # ระบุ session ที่กำลังรัน
        if 12 <= hour_utc <= 16:
            session_name = "overlap"      # ดีที่สุด
        elif 7 <= hour_utc < 16:
            session_name = "london"
        else:
            session_name = "newyork"

        return RiskCheckResult(
            passed = True,
            check  = f"session:{session_name}",
            value  = hour_utc,
            limit  = end,
        )

    def _check_margin_level(
        self, margin_level: float ) -> RiskCheckResult:
        """
        ตรวจ margin level

        เหตุผล:
        Margin level ต่ำ = ใกล้ margin call
        ถ้า margin level < 200% ควรหยุดเปิด position ใหม่

        Margin Level = (Equity / Used Margin) × 100
        < 100% = Margin Call (broker ปิด position อัตโนมัติ)
        < 200% = อันตราย ไม่ควรเปิดเพิ่ม
        """
        limit = self.min_margin_level

        # ✅ FIX BUG-5: MT5 คืน 0.0 หรือตัวเลขใหญ่มาก (เช่น 100000)
        #    เมื่อไม่มี open position — ทั้งหมดนี้หมายความว่า "safe"
        if margin_level <= 0 or margin_level >= 10000:
            return RiskCheckResult(
                passed = True,
                check  = "margin_level",
                reason = "no open positions",
                value  = margin_level,
                limit  = limit,
            )

        if margin_level < limit:
            return RiskCheckResult(
                passed = False,
                check  = "margin_level",
                reason = (
                    f"margin={margin_level:.0f}% < "
                    f"limit={limit:.0f}%"
                ),
                value  = margin_level,
                limit  = limit,
            )

        return RiskCheckResult(
            passed = True,
            check  = "margin_level",
            value  = margin_level,
            limit  = limit,
        )

    def _check_equity_drawdown(
        self,
        balance: float,
        equity:  float, ) -> RiskCheckResult:
        """
        ตรวจว่า floating loss ไม่เกินเกณฑ์

        เหตุผล:
        ถ้า open positions กำลังขาดทุนอยู่มาก
        ไม่ควรเปิด position ใหม่เพิ่ม

        Floating DD = (Balance - Equity) / Balance
        > 5% = ระวัง, > 10% = หยุดเปิดใหม่
        """
        if balance <= 0:
            # ✅ FIX BUG-3: ใช้ check name "equity_drawdown" ให้สอดคล้อง
            return RiskCheckResult(passed=True, check="equity_drawdown")

        floating_dd  = (balance - equity) / balance
        limit        = 0.10   # 10% floating loss

        if floating_dd >= limit:
            return RiskCheckResult(
                passed = False,
                check  = "equity_drawdown",
                reason = (
                    f"floating DD={floating_dd:.1%} ≥ "
                    f"limit={limit:.1%} "
                    f"(balance=${balance:.0f} "
                    f"equity=${equity:.0f})"
                ),
                value  = floating_dd,
                limit  = limit,
            )

        # เตือนถ้าใกล้ limit
        if floating_dd >= limit * 0.70:
            log.warning(
                f"⚠️ Floating DD {floating_dd:.1%} "
                f"ใกล้ limit {limit:.1%}"
            )

        return RiskCheckResult(
            passed = True,
            check  = "equity_drawdown",
            value  = floating_dd,
            limit  = limit,
        )

    def _check_weekend(self) -> RiskCheckResult:
        """
        ไม่เทรดช่วงสุดสัปดาห์ (simple check ใน check_all)
        สำหรับ full weekend check พร้อม next_open_utc ใช้ _weekend_session_block()

        เหตุผล:
        ตลาด Forex/Gold ปิด Friday 22:00 UTC → Sunday 22:00 UTC
        ถ้าเปิด position ข้ามสุดสัปดาห์จะโดน swap และ gap risk
        """
        if not CFG['session'].get('skip_weekends', True):
            return RiskCheckResult(passed=True, check="weekend")

        now     = datetime.now(timezone.utc)
        weekday = now.weekday()   # 0=Mon 4=Fri 5=Sat 6=Sun

        # Friday 22:00 UTC ถึง Sunday 22:00 UTC
        is_weekend = (
            weekday == 5 or   # Saturday ทั้งวัน
            weekday == 6 or   # Sunday ทั้งวัน
            (weekday == 4 and now.hour >= 22)   # Fri หลัง 22:00
        )

        if is_weekend:
            return RiskCheckResult(
                passed = False,
                check  = "weekend",
                reason = (
                    f"ตลาดปิด weekend "
                    f"({now.strftime('%A %H:%M UTC')})"
                ),
                value  = weekday,
                limit  = 4,
            )

        return RiskCheckResult(
            passed = True,
            check  = "weekend",
            value  = weekday,
            limit  = 4,
        )

    def _check_lot_size(self, lot: float) -> RiskCheckResult:
        """ตรวจ lot ที่คำนวณได้ว่าอยู่ใน range"""
        if lot < self.min_lot:
            return RiskCheckResult(
                passed = False,
                check  = "lot_size",
                reason = f"lot={lot} < min={self.min_lot}",
                value  = lot,
                limit  = self.min_lot,
            )

        if lot > self.max_lot:
            return RiskCheckResult(
                passed = False,
                check  = "lot_size",
                reason = f"lot={lot} > max={self.max_lot}",
                value  = lot,
                limit  = self.max_lot,
            )

        return RiskCheckResult(
            passed = True,
            check  = f"lot_size:{lot:.2f}",
            value  = lot,
            limit  = self.max_lot,
        )

    # ══════════════════════════════════════════════════════════
    # Daily Tracking
    # ══════════════════════════════════════════════════════════
    def _update_daily_tracking(self, balance: float):
        """
        อัพเดต daily tracking
        Reset ทุกวันตอนเที่ยงคืน UTC
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if self._daily_start_date != today:
            # วันใหม่ — reset ทุกค่า
            log.info(
                f"📅 New trading day: {today} | "
                f"Previous balance: "
                f"${self._daily_start_balance or 0:,.2f} | "
                f"Trades yesterday: {self._trades_today}"
            )
            self._daily_start_balance = balance
            self._daily_start_date    = today
            self._trades_today        = 0
            self._pnl_today           = 0.0

    def reset_daily(self):
        """Reset daily tracking — เรียกจาก daily_summary job"""
        self._daily_start_balance = None
        self._daily_start_date    = None
        self._trades_today        = 0
        self._pnl_today           = 0.0
        log.info("Daily tracking reset")

    def record_trade(self, pnl: float):
        """บันทึก trade ที่เพิ่งปิด"""
        self._trades_today += 1
        self._pnl_today    += pnl

    # ══════════════════════════════════════════════════════════
    # Utility
    # ══════════════════════════════════════════════════════════
    def check_daily_loss(self, balance: float) -> bool:
        """
        Shortcut ตรวจ daily loss เดียว
        ใช้ใน bot/main.py ก่อนทุก tick
        """
        if self._daily_start_balance is None:
            self._daily_start_balance = balance
            return True

        loss_pct = (
            self._daily_start_balance - balance
        ) / self._daily_start_balance

        return loss_pct < self.max_daily_loss

    # ══════════════════════════════════════════════════════════
    # Spread Filter
    # ══════════════════════════════════════════════════════════
    def check_spread(self, symbol: str) -> SpreadResult:
        """
        ตรวจว่า spread ปัจจุบันอยู่ในขอบเขตที่ยอมรับได้ไหม

        Returns:
            SpreadResult — ถ้า ok=False ไม่ควรเปิด order

        ตัวอย่าง:
            result = risk.check_spread("XAUUSD")
            if not result.ok:
                return  # skip trade
        """
        cfg_sf = CFG.get("spread_filter", {})
        if not cfg_sf.get("enabled", True):
            return SpreadResult(ok=True, spread=0.0, limit=0.0, symbol=symbol)

        fail_open = cfg_sf.get("fail_open", True)
        warn_pct  = cfg_sf.get("warn_pct", 70)

        try:
            # ✅ NEW: symbol เป็นชื่อกลาง resolve ก่อนเรียก MT5
            tick = mt5.symbol_info_tick(resolve_symbol(symbol))
        except Exception as e:
            log.error(f"check_spread: MT5 error for {symbol}: {e}")
            return SpreadResult(
                ok=fail_open, spread=0.0, limit=0.0,
                symbol=symbol,
                reason="MT5 unavailable" if not fail_open else "",
            )

        if tick is None:
            log.warning(f"check_spread: no tick for {symbol}")
            return SpreadResult(
                ok=fail_open, spread=0.0, limit=0.0,
                symbol=symbol,
                reason="No tick data" if not fail_open else "",
            )

        spread = round(tick.ask - tick.bid, 6)
        self._update_spread_stats(symbol, spread)
        limit  = self._get_spread_limit(symbol)

        if spread > limit:
            reason = (
                f"Spread {spread:.5f} > limit {limit:.5f} "
                f"({spread / limit * 100:.0f}% of limit)"
            )
            log.warning(f"🚫 {symbol}: {reason}")
            return SpreadResult(
                ok=False, spread=spread, limit=limit,
                symbol=symbol, reason=reason,
            )

        if spread / limit * 100 >= warn_pct:
            log.warning(
                f"⚠️  {symbol}: spread {spread:.5f} "
                f"({spread / limit * 100:.0f}% of limit={limit:.5f}) — approaching limit"
            )

        stats = self._spread_history.get(symbol)
        if stats and stats.is_widening:
            log.warning(
                f"⚠️  {symbol}: spread widening "
                f"(avg={stats.avg:.5f} → current={spread:.5f}) — possible news coming"
            )

        return SpreadResult(ok=True, spread=spread, limit=limit, symbol=symbol)

    def _get_spread_limit(self, symbol: str) -> float:
        """
        ดึง max spread สำหรับ symbol จาก config.yaml

        config ตัวอย่าง (key เป็นชื่อกลางแล้ว ไม่มี m):
          spread_filter:
            limits:
              XAUUSD: 0.80
              EURUSD: 0.0003
              default: 0.0010
        """
        limits = CFG.get("spread_filter", {}).get("limits", {})

        if isinstance(limits, dict):
            if symbol in limits:
                return float(limits[symbol])
            return float(limits.get("default", 0.0010))

        try:
            return float(limits)
        except (TypeError, ValueError):
            return 0.0010

    def _update_spread_stats(self, symbol: str, spread: float):
        """อัปเดต rolling history ของ spread"""
        if symbol not in self._spread_history:
            self._spread_history[symbol] = SpreadStats(symbol=symbol)
        self._spread_history[symbol].history.append(spread)

    def get_spread_summary(self) -> dict:
        """
        สรุป spread stats ทุก symbol
        ใช้สำหรับ /spread command ใน Telegram bot หรือ dashboard
        """
        summary = {}
        for sym, stats in self._spread_history.items():
            limit = self._get_spread_limit(sym)
            summary[sym] = {
                "current"     : stats.history[-1] if stats.history else 0.0,
                "avg_20"      : round(stats.avg, 6),
                "max_recent"  : round(stats.max_recent, 6),
                "limit"       : limit,
                "is_widening" : stats.is_widening,
            }
        return summary

    # ══════════════════════════════════════════════════════════
    # Circuit Breaker — Public API
    # ══════════════════════════════════════════════════════════

    def check_circuit_breaker(self) -> CircuitBreakerResult:
        """
        ตรวจ circuit breaker ทุก condition
        เรียกต้น loop ทุก cycle ของ bot/main.py
        ถ้า triggered=True → pause bot ทันที
        """
        cfg_cb = CFG.get("circuit_breaker", {})
        if not cfg_cb.get("enabled", True):
            return CircuitBreakerResult(triggered=False)

        # ลอง auto-resume ก่อน (ถ้าถึงเวลาแล้ว)
        if self._cb_state.is_triggered:
            resumed = self._try_auto_resume()
            if resumed:
                log.info("✅ Circuit breaker auto-resumed")
            else:
                return CircuitBreakerResult(
                    triggered = True,
                    reason    = self._cb_state.trigger_reason,
                    level     = self._cb_state.trigger_level,
                )

        # ตรวจทุก condition (floating_dd ก่อน — real-time)
        checks = [
            ("floating_dd",  self._cb_check_floating_dd),
            ("daily",        self._cb_check_daily_loss),
            ("consecutive",  self._cb_check_consecutive_losses),
            ("weekly",       self._cb_check_weekly_loss),
        ]

        for level, fn in checks:
            try:
                triggered, value, limit, reason = fn()
                if triggered:
                    result = CircuitBreakerResult(
                        triggered=True, reason=reason,
                        level=level, value=value, limit=limit,
                    )
                    self._on_cb_triggered(result)
                    return result
            except Exception as e:
                log.error(f"CB check [{level}] error: {e}", exc_info=True)

        return CircuitBreakerResult(triggered=False)

    def get_circuit_breaker_status(self) -> dict:
        """สรุปสถานะ CB ทั้งหมด — ใช้สำหรับ /status Telegram หรือ dashboard"""
        status = {
            "triggered"      : self._cb_state.is_triggered,
            "trigger_level"  : self._cb_state.trigger_level,
            "trigger_reason" : self._cb_state.trigger_reason,
            "triggered_at"   : self._cb_state.triggered_at,
            "auto_resume_at" : self._cb_state.auto_resume_at,
        }
        try:
            status["daily_loss_usd"]    = round(self._cb_get_pnl(days_back=1), 2)
            status["weekly_loss_usd"]   = round(self._cb_get_pnl(days_back=7), 2)
            status["consecutive_count"] = self._cb_get_consecutive_loss_count()
            acc = self._cb_get_account_info()
            if acc:
                bal = acc.get("balance", 0)
                eq  = acc.get("equity",  0)
                status["balance"]         = bal
                status["equity"]          = eq
                status["floating_dd_usd"] = round(bal - eq, 2)
                status["floating_dd_pct"] = round((bal - eq) / bal * 100, 2) if bal > 0 else 0
        except Exception:
            pass
        return status

    def resume_circuit_breaker(self):
        """Manual resume — เรียกจาก Telegram /resume command"""
        self._cb_state.is_triggered      = False
        self._cb_state.trigger_level     = ""
        self._cb_state.trigger_reason    = ""
        self._cb_state.triggered_at      = None
        self._cb_state.auto_resume_at    = None
        self._cb_state.consecutive_count = 0
        self._save_cb_state()
        log.info("✅ Circuit breaker manually resumed")

    def reset_circuit_breaker(
        self,
        reset_consecutive   : bool = True,
        reset_daily_tracking: bool = False,
    ) -> None:
        """
        Reset circuit breaker state ทั้ง in-memory และ disk
        โดยไม่ต้อง restart bot — เรียกจาก main loop เมื่อตรวจพบ flag file

        Args:
            reset_consecutive:    ล้าง consecutive_count (default True)
            reset_daily_tracking: ล้าง _daily_start_balance ด้วย (default False)

        Note:
            CB daily check อ่าน P&L จาก DB โดยตรง — losses ที่เกิดไปแล้ว
            ยังคงอยู่ใน DB แต่ bot จะ resume ได้โดยไม่ trigger ซ้ำทันที
            (trigger จะเกิดอีกครั้งถ้ายังขาดทุนเกิน limit และไม่ได้แก้ limit)
        """
        self._cb_state.is_triggered   = False
        self._cb_state.trigger_level  = ""
        self._cb_state.trigger_reason = ""
        self._cb_state.triggered_at   = None
        self._cb_state.auto_resume_at = None

        if reset_consecutive:
            self._cb_state.consecutive_count = 0

        self._save_cb_state()

        if reset_daily_tracking:
            self.reset_daily()   # ล้าง _daily_start_balance / _pnl_today

        log.info(
            "✅ Circuit breaker reset "
            f"(reset_consecutive={reset_consecutive}, "
            f"reset_daily_tracking={reset_daily_tracking})"
        )

    # ══════════════════════════════════════════════════════════
    # Circuit Breaker — Individual Checks
    # ══════════════════════════════════════════════════════════

    def _cb_check_daily_loss(self) -> tuple:
        """ตรวจว่าขาดทุนวันนี้เกิน limit หรือไม่"""
        cfg = CFG.get("circuit_breaker", {}).get("daily", {})
        if not cfg.get("enabled", True):
            return False, 0.0, 0.0, ""

        daily_pnl = self._cb_get_pnl(days_back=1)
        if daily_pnl == 0.0:
            acc = self._cb_get_account_info()
            if acc:
                daily_pnl = acc.get("profit", 0.0)
            return False, 0.0, 0.0, ""

        loss = abs(daily_pnl)

        # ตรวจ USD limit
        loss_usd = cfg.get("loss_usd")
        if loss_usd and loss >= float(loss_usd):
            return (True, -loss, -float(loss_usd),
                    f"Daily loss ${loss:.2f} hit USD limit ${float(loss_usd):.2f}")

        # ตรวจ % ของ balance
        loss_pct = cfg.get("loss_pct", 3.0)
        acc = self._cb_get_account_info()
        if acc and acc.get("balance", 0) > 0:
            balance   = acc["balance"]
            limit_usd = balance * loss_pct / 100
            if loss >= limit_usd:
                return (True, -loss, -limit_usd,
                        f"Daily loss ${loss:.2f} ({loss/balance*100:.1f}%) "
                        f"hit limit {loss_pct:.1f}% (${limit_usd:.2f})")

        return False, 0.0, 0.0, ""

    def _cb_check_weekly_loss(self) -> tuple:
        """ตรวจขาดทุนสะสม 7 วัน"""
        cfg = CFG.get("circuit_breaker", {}).get("weekly", {})
        if not cfg.get("enabled", True):
            return False, 0.0, 0.0, ""

        weekly_pnl = self._cb_get_pnl(days_back=7)
        if weekly_pnl >= 0:
            return False, 0.0, 0.0, ""

        loss     = abs(weekly_pnl)
        loss_pct = cfg.get("loss_pct", 8.0)
        acc      = self._cb_get_account_info()

        if acc and acc.get("balance", 0) > 0:
            balance   = acc["balance"]
            limit_usd = balance * loss_pct / 100
            if loss >= limit_usd:
                return (True, -loss, -limit_usd,
                        f"Weekly loss ${loss:.2f} ({loss/balance*100:.1f}%) "
                        f"hit limit {loss_pct:.1f}% (${limit_usd:.2f})")

        return False, 0.0, 0.0, ""

    def _cb_check_consecutive_losses(self) -> tuple:
        """ตรวจ consecutive losses"""
        cfg = CFG.get("circuit_breaker", {}).get("consecutive_losses", {})
        if not cfg.get("enabled", True):
            return False, 0.0, 0.0, ""

        max_count = int(cfg.get("count", 5))
        count     = self._cb_get_consecutive_loss_count()
        self._cb_state.consecutive_count = count

        if count >= max_count:
            return (True, float(count), float(max_count),
                    f"Consecutive losses: {count} hits limit {max_count}")

        return False, 0.0, 0.0, ""

    def _cb_check_floating_dd(self) -> tuple:
        """ตรวจ floating drawdown (real-time, open positions)"""
        cfg = CFG.get("circuit_breaker", {}).get("floating_dd", {})
        if not cfg.get("enabled", True):
            return False, 0.0, 0.0, ""

        acc = self._cb_get_account_info()
        if not acc:
            return False, 0.0, 0.0, ""

        balance = acc.get("balance", 0)
        equity  = acc.get("equity",  0)
        if balance <= 0:
            return False, 0.0, 0.0, ""

        floating_loss = balance - equity
        if floating_loss <= 0:
            return False, 0.0, 0.0, ""

        max_pct   = cfg.get("max_pct", 5.0)
        limit_usd = balance * max_pct / 100
        dd_pct    = floating_loss / balance * 100

        if floating_loss >= limit_usd:
            return (True, -floating_loss, -limit_usd,
                    f"Floating DD ${floating_loss:.2f} ({dd_pct:.1f}%) "
                    f"hit limit {max_pct:.1f}% (${limit_usd:.2f})")

        warn_pct = cfg.get("warn_pct", 70)
        if dd_pct >= max_pct * warn_pct / 100:
            log.warning(
                f"⚠️  Floating DD ${floating_loss:.2f} ({dd_pct:.1f}%) "
                f"approaching limit {max_pct:.1f}%"
            )

        return False, 0.0, 0.0, ""

    # ══════════════════════════════════════════════════════════
    # Circuit Breaker — Trigger / Resume
    # ══════════════════════════════════════════════════════════

    def _on_cb_triggered(self, result: CircuitBreakerResult):
        """เรียกเมื่อ CB trigger — บันทึก state + notify Telegram"""
        now    = datetime.now(timezone.utc)
        cfg_cb = CFG.get("circuit_breaker", {})

        auto_resume_at = None
        if result.level == "daily":
            if cfg_cb.get("daily", {}).get("auto_resume", True):
                tomorrow = (now + timedelta(days=1)).replace(
                    hour=0, minute=5, second=0, microsecond=0
                )
                auto_resume_at = tomorrow.isoformat()
        elif result.level == "consecutive":
            cooldown_h = cfg_cb.get("consecutive_losses", {}).get("cooldown_hours", 4)
            auto_resume_at = (now + timedelta(hours=cooldown_h)).isoformat()
        # weekly + floating_dd → manual only

        self._cb_state.is_triggered   = True
        self._cb_state.trigger_level  = result.level
        self._cb_state.trigger_reason = result.reason
        self._cb_state.triggered_at   = now.isoformat()
        self._cb_state.auto_resume_at = auto_resume_at
        self._save_cb_state()

        log.critical(str(result))
        if auto_resume_at:
            log.info(f"  Auto-resume scheduled: {auto_resume_at}")
        else:
            log.info("  Manual /resume required via Telegram")

        if cfg_cb.get("notify_telegram", True):
            try:
                from bot.notifier import notify
                resume_msg = (
                    f"\n⏰ Auto-resume at: {auto_resume_at}"
                    if auto_resume_at
                    else "\n🔑 Manual /resume required"
                )
                notify(
                    f"🚨 *Circuit Breaker Triggered*\n\n"
                    f"Level: `{result.level.upper()}`\n"
                    f"Reason: {result.reason}\n"
                    f"Time: `{now.strftime('%Y-%m-%d %H:%M UTC')}`"
                    f"{resume_msg}"
                )
            except Exception as e:
                log.warning(f"CB notify error: {e}")

    def _try_auto_resume(self) -> bool:
        """ตรวจว่าถึงเวลา auto-resume หรือยัง — คืน True ถ้า resume แล้ว"""
        if not self._cb_state.auto_resume_at:
            return False

        now       = datetime.now(timezone.utc)
        resume_at = datetime.fromisoformat(self._cb_state.auto_resume_at)
        if resume_at.tzinfo is None:
            resume_at = resume_at.replace(tzinfo=timezone.utc)

        if now >= resume_at:
            self.resume_circuit_breaker()
            try:
                from bot.notifier import notify
                notify("✅ *Circuit Breaker Auto-Resumed*\nBot กลับมาเทรดตามปกติ")
            except Exception:
                pass
            return True

        return False

    # ══════════════════════════════════════════════════════════
    # Circuit Breaker — Data Helpers
    # ══════════════════════════════════════════════════════════

    def _cb_get_pnl(self, days_back: int = 1) -> float:
        """ดึง P&L จาก SQLite ย้อนหลัง N วัน (negative = loss)"""
        try:
            db_path = CFG.get("paths", {}).get("db", "db/trades.db")
            if not Path(db_path).exists():
                return 0.0
            conn = sqlite3.connect(db_path)
            try:
                cur = conn.execute(
                    """
                    SELECT COALESCE(SUM(profit), 0)
                    FROM   trades
                    WHERE  close_time IS NOT NULL
                      AND  close_time >= datetime('now', ? || ' days')
                    """,
                    (f"-{days_back}",),
                )
                return float(cur.fetchone()[0])
            finally:
                conn.close()
        except Exception as e:
            log.error(f"_cb_get_pnl error: {e}")
            return 0.0

    def _cb_get_consecutive_loss_count(self) -> int:
        """นับ consecutive losses ล่าสุด (หยุดเมื่อเจอ trade กำไร)"""
        try:
            db_path = CFG.get("paths", {}).get("db", "db/trades.db")
            if not Path(db_path).exists():
                return 0
            conn = sqlite3.connect(db_path)
            try:
                cur = conn.execute(
                    """
                    SELECT profit FROM trades
                    WHERE  close_time IS NOT NULL
                    ORDER  BY close_time DESC
                    LIMIT  30
                    """
                )
                count = 0
                for (profit,) in cur.fetchall():
                    if float(profit or 0) < 0:
                        count += 1
                    else:
                        break
                return count
            finally:
                conn.close()
        except Exception as e:
            log.error(f"_cb_get_consecutive_loss_count error: {e}")
            return 0

    def _cb_get_account_info(self) -> Optional[dict]:
        """ดึง account info จาก MT5"""
        try:
            acc = mt5.account_info()
            if acc is None:
                return None
            return {"balance": acc.balance, "equity": acc.equity, "profit": acc.profit}
        except Exception:
            return None

    # ══════════════════════════════════════════════════════════
    # Circuit Breaker — State Persistence
    # ══════════════════════════════════════════════════════════

    def _load_cb_state(self) -> CircuitBreakerState:
        """โหลด state จาก disk (ทนต่อ bot restart)"""
        try:
            if self._cb_state_path.exists():
                data = json.loads(
                    self._cb_state_path.read_text(encoding="utf-8")
                )
                return CircuitBreakerState(**data)
        except Exception as e:
            log.warning(f"CB: cannot load state: {e}")
        return CircuitBreakerState()

    def _save_cb_state(self):
        """บันทึก state ลง disk"""
        try:
            self._cb_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._cb_state_path.write_text(
                json.dumps(self._cb_state.__dict__, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            log.error(f"CB: cannot save state: {e}")

    def get_risk_summary(self) -> dict:
        """สรุปสถานะ risk ปัจจุบัน"""
        return {
            'daily_start_balance': self._daily_start_balance,
            'daily_start_date'   : self._daily_start_date,
            'trades_today'       : self._trades_today,
            'pnl_today'          : self._pnl_today,
            'risk_per_trade'     : self.risk_per_trade,
            'max_daily_loss'     : self.max_daily_loss,
            'max_open_trades'    : self.max_open_trades,
        }

    # ══════════════════════════════════════════════════════════
    # [4] NEW — Session Filter (Public API)
    # ══════════════════════════════════════════════════════════

    def check_session(
        self,
        symbol    : Optional[str] = None,
        now_utc   : Optional[datetime] = None,
    ) -> SessionResult:
        """
        ตรวจว่าตอนนี้อยู่ใน trading session ที่อนุญาตไหม
        (DST-aware, per-symbol config, overlap detection)

        Args:
            symbol  : "XAUUSD" ฯลฯ — ใช้ per-symbol override ใน config
            now_utc : inject เวลา (ถ้า None ใช้เวลาจริง) — ช่วย unit test

        Returns:
            SessionResult
              .ok=True  → เทรดได้
              .ok=False → ควร skip (พร้อม next_open_utc บอกเวลาที่จะเปิดอีก)
              .is_overlap=True → London+NY พร้อมกัน (prime zone สำหรับทอง)

        ตัวอย่าง:
            sess = risk.check_session(symbol="XAUUSD")
            if not sess.ok:
                log.debug(f"[{symbol}] {sess}")
                continue
            if sess.is_overlap:
                log.info(f"[{symbol}] ⚡ London+NY Overlap — prime zone")
        """
        cfg_sf = CFG.get("session_filter", {})
        if not cfg_sf.get("enabled", True):
            return SessionResult(ok=True, active_sessions=["all"])

        now = now_utc or datetime.now(timezone.utc)

        # ── Weekend check ──────────────────────────────────────
        if cfg_sf.get("skip_weekend", True):
            wr = self._weekend_session_block(now, cfg_sf)
            if not wr.ok:
                return wr

        # ── หา allowed sessions สำหรับ symbol นี้ ─────────────
        allowed = self._get_allowed_sessions(symbol, cfg_sf)

        # ── ดึง session times (DST-aware) ─────────────────────
        session_hours = self._build_session_hours(now, cfg_sf)

        # ── ตรวจว่า session ไหนเปิดอยู่ ───────────────────────
        active = [
            name for name in allowed
            if name in session_hours
            and self._is_open(session_hours[name], now.hour, now.minute)
        ]

        # ── Overlap detection (London + NY พร้อมกัน) ──────────
        is_overlap = "London" in active and "New York" in active

        # ── Overlap-only mode ──────────────────────────────────
        if cfg_sf.get("overlap_only", False):
            if not is_overlap:
                return SessionResult(
                    ok             = False,
                    active_sessions= active,
                    blocked_reason = "Overlap-only mode (London+NY not both open)",
                    next_open_utc  = self._next_overlap_open(now, session_hours),
                    is_weekend     = False,
                )
            return SessionResult(
                ok=True, active_sessions=active, is_overlap=True
            )

        # ── ปกติ: อย่างน้อย 1 session ต้องเปิด ───────────────
        if active:
            return SessionResult(
                ok              = True,
                active_sessions = active,
                is_overlap      = is_overlap,
            )

        # ── ไม่มี session เปิด ─────────────────────────────────
        next_open = self._next_allowed_open(now, allowed, session_hours, cfg_sf)
        return SessionResult(
            ok             = False,
            active_sessions= [],
            blocked_reason = (
                f"No allowed session open "
                f"(allowed: {', '.join(allowed)})"
            ),
            next_open_utc  = next_open,
        )

    def get_all_session_info(
        self,
        now_utc: Optional[datetime] = None,
    ) -> list:
        """
        คืน SessionInfo ทุก session
        ใช้สำหรับ /session ใน Telegram และ dashboard

        Returns:
            list[SessionInfo] — ทุก session พร้อม is_open และ opens_in_h
        """
        now           = now_utc or datetime.now(timezone.utc)
        cfg_sf        = CFG.get("session_filter", {})
        session_hours = self._build_session_hours(now, cfg_sf)

        _EMOJIS = {
            "Sydney"  : "🦘",
            "Tokyo"   : "🗼",
            "London"  : "🎡",
            "New York": "🗽",
        }

        results = []
        for name, (open_h, close_h) in session_hours.items():
            is_open  = self._is_open((open_h, close_h), now.hour, now.minute)
            opens_in = 0.0
            if not is_open:
                opens_in = self._hours_until(open_h, now.hour, now.minute)

            results.append(SessionInfo(
                name      = name,
                open_utc  = open_h,
                close_utc = close_h,
                is_open   = is_open,
                opens_in_h= round(opens_in, 1),
                emoji     = _EMOJIS.get(name, "🌐"),
            ))
        return results

    # ══════════════════════════════════════════════════════════
    # [4] NEW — Weekend Handling (Session Module)
    # ══════════════════════════════════════════════════════════
    # NOTE: ใช้ชื่อ _weekend_session_block เพื่อไม่ conflict กับ
    #       _check_weekend(self) เดิมที่ใช้ใน check_all()

    def _weekend_session_block(
        self,
        now   : datetime,
        cfg_sf: dict,
    ) -> SessionResult:
        """
        ตรวจว่าเป็นช่วง weekend ที่ตลาดปิดไหม (session-aware version)

        ตลาด Forex ปิด:
          Friday  >= friday_close_hour UTC   (default 21:00)
          Saturday (ทั้งวัน)
          Sunday  < monday_open_hour UTC     (default 07:00)

        Returns:
            SessionResult(ok=True)  = เทรดได้
            SessionResult(ok=False) = weekend, พร้อม next_open_utc
        """
        weekday      = now.weekday()   # 0=Mon, 4=Fri, 5=Sat, 6=Sun
        friday_close = int(cfg_sf.get("friday_close_hour", 21))
        monday_open  = int(cfg_sf.get("monday_open_hour",   7))

        # Saturday — ตลาดปิดทั้งวัน
        if weekday == 5:
            next_mon  = now.replace(hour=monday_open, minute=0,
                                    second=0, microsecond=0)
            next_mon += timedelta(days=(7 - weekday))
            return SessionResult(
                ok            = False,
                is_weekend    = True,
                blocked_reason= "Saturday — Forex market closed",
                next_open_utc = next_mon,
            )

        # Sunday — ปิดจนถึง monday_open
        if weekday == 6 and now.hour < monday_open:
            next_open = now.replace(hour=monday_open, minute=0,
                                    second=0, microsecond=0)
            return SessionResult(
                ok            = False,
                is_weekend    = True,
                blocked_reason= f"Sunday before {monday_open:02d}:00 UTC",
                next_open_utc = next_open,
            )

        # Friday หลัง friday_close
        if weekday == 4 and now.hour >= friday_close:
            days_to_mon = 3   # Fri → Mon = 3 days
            next_open   = (now + timedelta(days=days_to_mon)).replace(
                hour=monday_open, minute=0, second=0, microsecond=0
            )
            return SessionResult(
                ok            = False,
                is_weekend    = True,
                blocked_reason= f"Friday close (after {friday_close:02d}:00 UTC)",
                next_open_utc = next_open,
            )

        return SessionResult(ok=True)  # ไม่ใช่ weekend

    # ══════════════════════════════════════════════════════════
    # [4] NEW — DST-Aware Session Times
    # ══════════════════════════════════════════════════════════

    def _build_session_hours(
        self,
        now   : datetime,
        cfg_sf: dict,
    ) -> dict:
        """
        คืน session hours (UTC) ที่ปรับตาม US DST แล้ว

        Returns:
            {
                "Sydney"  : (21,  6),
                "Tokyo"   : ( 0,  9),
                "London"  : ( 7, 16),
                "New York": (12, 21),   # EDT / (13, 22) ถ้า EST
            }
        """
        use_dst    = cfg_sf.get("use_dst", True)
        us_dst_now = self._is_us_dst(now) if use_dst else False

        # New York: EDT (UTC-4) vs EST (UTC-5)
        ny_open  = 12 if us_dst_now else 13
        ny_close = 21 if us_dst_now else 22

        # London: 07:00-16:00 UTC ตลอดปี
        # (BST = UTC+1 แต่ตลาดยังเปิด 8am local = 07:00 UTC ฤดูร้อน)

        # Custom overrides จาก config (ถ้ามี)
        custom = cfg_sf.get("session_hours_utc", {})

        return {
            "Sydney"  : custom.get("Sydney",   (21,  6)),
            "Tokyo"   : custom.get("Tokyo",    ( 0,  9)),
            "London"  : custom.get("London",   ( 7, 16)),
            "New York": custom.get("New York", (ny_open, ny_close)),
        }

    def _is_us_dst(self, dt: datetime) -> bool:
        """
        ตรวจว่าตอนนี้ US อยู่ใน DST (EDT) หรือไม่
        DST: 2nd Sunday March → 1st Sunday November
        """
        year = dt.year

        # Second Sunday of March (spring forward)
        mar1        = datetime(year, 3, 1, tzinfo=timezone.utc)
        wday_mar1   = mar1.weekday()          # 0=Mon … 6=Sun
        days_to_sun = (6 - wday_mar1) % 7
        spring      = mar1 + timedelta(days=days_to_sun + 7)  # +7 = 2nd Sunday
        spring      = spring.replace(hour=7)  # 02:00 ET ≈ 07:00 UTC

        # First Sunday of November (fall back)
        nov1        = datetime(year, 11, 1, tzinfo=timezone.utc)
        wday_nov1   = nov1.weekday()
        days_to_sun = (6 - wday_nov1) % 7
        fall        = nov1 + timedelta(days=days_to_sun)
        fall        = fall.replace(hour=6)    # 02:00 ET ≈ 06:00 UTC (now EST)

        now_utc = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return spring <= now_utc < fall

    # ══════════════════════════════════════════════════════════
    # [4] NEW — Session Helpers
    # ══════════════════════════════════════════════════════════

    def _get_allowed_sessions(
        self,
        symbol: Optional[str],
        cfg_sf: dict,
    ) -> list:
        """ดึง allowed sessions สำหรับ symbol (per-symbol override ถ้ามี)"""
        default = cfg_sf.get("allowed_sessions", ["London", "New York"])

        # Per-symbol override
        # ✅ FIX: symbol เป็นชื่อกลางแล้ว (XAUUSD) ไม่ต้อง strip 'm' อีก
        # config.symbol_sessions ใช้ key ชื่อกลางตรงกันพอดี
        sym_override = cfg_sf.get("symbol_sessions", {})
        if symbol and symbol in sym_override:
            return list(sym_override[symbol])

        return list(default)

    def _is_open(
        self,
        hours     : tuple,
        current_h : int,
        current_m : int = 0,
    ) -> bool:
        """
        ตรวจว่า session เปิดอยู่ไหม
        รองรับ session ที่ข้ามเที่ยงคืน (เช่น Sydney 21:00-06:00)
        """
        open_h, close_h = hours
        t = current_h + current_m / 60.0

        if open_h < close_h:
            # ปกติ (เช่น London 07-16)
            return open_h <= t < close_h
        else:
            # ข้ามเที่ยงคืน (เช่น Sydney 21-06)
            return t >= open_h or t < close_h

    def _hours_until(
        self, open_h: int, current_h: int, current_m: int
    ) -> float:
        """ชั่วโมงที่เหลือก่อน session จะเปิด"""
        t      = current_h + current_m / 60.0
        target = float(open_h)
        if target <= t:
            target += 24  # วันถัดไป
        return target - t

    def _next_allowed_open(
        self,
        now          : datetime,
        allowed      : list,
        session_hours: dict,
        cfg_sf       : dict,
    ) -> Optional[datetime]:
        """
        หาเวลา UTC ที่ session ถัดไปจะเปิด
        คืน datetime UTC หรือ None ถ้าหาไม่ได้
        """
        min_wait = float("inf")
        for name in allowed:
            if name not in session_hours:
                continue
            hours_away = self._hours_until(
                session_hours[name][0], now.hour, now.minute
            )
            min_wait = min(min_wait, hours_away)

        if min_wait == float("inf"):
            return None

        return (now + timedelta(hours=min_wait)).replace(
            minute=0, second=0, microsecond=0
        )

    def _next_overlap_open(
        self,
        now          : datetime,
        session_hours: dict, ) -> Optional[datetime]:
        """เวลาที่ London+NY overlap จะเริ่ม (12:00 EDT หรือ 13:00 EST)"""
        ny_open    = session_hours.get("New York", (12, 21))[0]
        hours_away = self._hours_until(ny_open, now.hour, now.minute)
        return (now + timedelta(hours=hours_away)).replace(
            minute=0, second=0, microsecond=0
        )