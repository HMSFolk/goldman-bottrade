# bot/risk_manager.py
"""
Risk Manager — ระบบป้องกันความเสี่ยง
════════════════════════════════════════════════════════════
ทุก order ต้องผ่านการตรวจทุกข้อก่อนส่ง

ความรับผิดชอบ:
  - คำนวณ lot size ตาม % risk ที่กำหนด
  - ตรวจ spread (ไม่เทรดถ้า spread กว้างเกิน)
  - ตรวจ session (เทรดเฉพาะ London + NY)
  - ตรวจ daily loss limit (หยุดถ้าเสียเกิน X%)
  - ตรวจ max open trades
  - ตรวจ margin level
  - ตรวจ news blackout
  - บันทึก risk metrics ทุก check
════════════════════════════════════════════════════════════
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

import MetaTrader5 as mt5

# ✅ FIX BUG-1: ใช้ get_config() แทน open(config.yaml) โดยตรง
from config import get_config
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
    """

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
        margin_level: float = 999,
    ) -> RiskReport:
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
        sl_distance: float,
    ) -> float:
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
        info = mt5.symbol_info(symbol)
        if info is None:
            log.error(f"ไม่พบ symbol: {symbol}")
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
        tick   = mt5.symbol_info_tick(symbol)
        info   = mt5.symbol_info(symbol)

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
        self, current_balance: float
    ) -> RiskCheckResult:
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
        sym_positions = mt5.positions_get(symbol=symbol) or []
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
        tick  = mt5.symbol_info_tick(symbol)
        info  = mt5.symbol_info(symbol)

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
        self, symbol: str
    ) -> RiskCheckResult:
        """
        เทรดเฉพาะ London + NY session

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
        self, margin_level: float
    ) -> RiskCheckResult:
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
        equity:  float,
    ) -> RiskCheckResult:
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
        ไม่เทรดช่วงสุดสัปดาห์

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