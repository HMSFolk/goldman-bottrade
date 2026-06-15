# ══════════════════════════════════════════════════════════════
# SPREAD FILTER — เพิ่มเข้าไปใน bot/risk_manager.py
# ══════════════════════════════════════════════════════════════
#
# วิธีใช้:
#   1. copy ส่วน IMPORTS ไปเพิ่มที่ด้านบนของ risk_manager.py
#   2. copy ส่วน DATA CLASSES ไปหลัง imports
#   3. copy เมธอดทั้งหมดไปเพิ่มใน class RiskManager
#   4. ใน __init__ ของ RiskManager เพิ่ม: self._spread_history = {}
#
# ══════════════════════════════════════════════════════════════


# ── [1] เพิ่ม imports เหล่านี้ที่ด้านบน risk_manager.py ──────
from collections import deque
from dataclasses import dataclass, field


# ── [2] เพิ่ม dataclasses เหล่านี้ก่อน class RiskManager ──────

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


# ── [3] เพิ่มใน __init__ ของ RiskManager ──────────────────────
#
#   def __init__(self, ...):
#       ...  (โค้ดเดิม)
#       self._spread_history: dict[str, SpreadStats] = {}   ← เพิ่มบรรทัดนี้
#
# ── [4] เพิ่มเมธอดเหล่านี้ในคลาส RiskManager ──────────────────

    def check_spread(self, symbol: str) -> SpreadResult:
        """
        ตรวจว่า spread ปัจจุบันอยู่ในขอบเขตที่ยอมรับได้ไหม

        Returns:
            SpreadResult — ถ้า ok=False ไม่ควรเปิด order

        ตัวอย่าง:
            result = risk.check_spread("XAUUSDm")
            if not result.ok:
                return  # skip trade
        """
        cfg_sf = CFG.get("spread_filter", {})
        if not cfg_sf.get("enabled", True):
            # ปิด filter → ให้เทรดเสมอ
            return SpreadResult(ok=True, spread=0.0, limit=0.0, symbol=symbol)

        fail_open = cfg_sf.get("fail_open", True)
        warn_pct  = cfg_sf.get("warn_pct", 70)    # % ของ limit ที่จะ log warning

        # ── ดึง tick ──────────────────────────────────────────
        try:
            import MetaTrader5 as mt5
            tick = mt5.symbol_info_tick(symbol)
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
                reason=f"No tick data" if not fail_open else "",
            )

        # ── คำนวณ spread ──────────────────────────────────────
        spread = round(tick.ask - tick.bid, 6)

        # ── อัปเดต history ────────────────────────────────────
        self._update_spread_stats(symbol, spread)

        # ── ดึง limit ─────────────────────────────────────────
        limit = self._get_spread_limit(symbol)

        # ── ตรวจ ──────────────────────────────────────────────
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

        # ── warn ถ้าใกล้ limit ────────────────────────────────
        if spread / limit * 100 >= warn_pct:
            log.warning(
                f"⚠️  {symbol}: spread {spread:.5f} "
                f"({spread / limit * 100:.0f}% of limit={limit:.5f}) — approaching limit"
            )

        # ── warn ถ้า spread กำลัง widen ──────────────────────
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

        config ตัวอย่าง:
          spread_filter:
            limits:
              XAUUSDm: 0.80
              EURUSDm: 0.0003
              default: 0.0010
        """
        limits = CFG.get("spread_filter", {}).get("limits", {})

        if isinstance(limits, dict):
            # ลองชื่อ symbol ตรงๆ ก่อน
            if symbol in limits:
                return float(limits[symbol])
            # ลอง strip 'm' suffix (XAUUSDm → XAUUSD)
            base = symbol.rstrip("m")
            if base in limits:
                return float(limits[base])
            # ใช้ default
            return float(limits.get("default", 0.0010))

        # ถ้า limits เป็น single value → ใช้กับทุก symbol
        try:
            return float(limits)
        except (TypeError, ValueError):
            return 0.0010  # safe fallback

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
