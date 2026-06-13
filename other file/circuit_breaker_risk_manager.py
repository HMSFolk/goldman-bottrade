# ══════════════════════════════════════════════════════════════
# CIRCUIT BREAKER — เพิ่มเข้าไปใน bot/risk_manager.py
# ══════════════════════════════════════════════════════════════
#
# วิธีติดตั้ง:
#   [1] imports → เพิ่มที่ด้านบนของ risk_manager.py
#   [2] dataclasses → เพิ่มก่อน class RiskManager
#   [3] __init__ → เพิ่ม 2 บรรทัดในเมธอด __init__
#   [4] methods → เพิ่มทุกเมธอดเข้าใน class RiskManager
#
# ══════════════════════════════════════════════════════════════


# ── [1] เพิ่ม imports ที่ด้านบน risk_manager.py ───────────────
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


# ── [2] เพิ่ม dataclasses ก่อน class RiskManager ─────────────

@dataclass
class CircuitBreakerResult:
    """ผลการตรวจ circuit breaker ใน 1 รอบ"""
    triggered   : bool
    reason      : str  = ""
    level       : str  = ""    # "daily" | "weekly" | "consecutive" | "floating_dd"
    value       : float = 0.0  # ค่าจริงที่วัดได้ (loss หรือ DD)
    limit       : float = 0.0  # limit ที่ถูก hit

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
    triggered_at     : Optional[str]   = None   # ISO datetime UTC
    auto_resume_at   : Optional[str]   = None   # ISO datetime UTC (ถ้า None = manual only)
    consecutive_count: int             = 0      # จำนวน consecutive losses ปัจจุบัน


# ── [3] เพิ่มใน __init__ ของ RiskManager ─────────────────────
#
#   def __init__(self, ...):
#       ...  (โค้ดเดิม)
#       # ── Circuit Breaker state ──
#       self._cb_state_path = Path(CFG.get('paths', {}).get(
#           'flags_dir', 'flags')) / 'circuit_breaker_state.json'
#       self._cb_state = self._load_cb_state()
#
# ── [4] เพิ่มเมธอดทั้งหมดนี้ใน class RiskManager ─────────────

    # ══════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════

    def check_circuit_breaker(self) -> CircuitBreakerResult:
        """
        ตรวจ circuit breaker ทุก condition

        เรียกต้น loop ทุก cycle ของ bot/main.py
        ถ้า triggered=True → pause bot ทันที

        Returns:
            CircuitBreakerResult — .triggered=True = ให้ pause
        """
        cfg_cb = CFG.get("circuit_breaker", {})
        if not cfg_cb.get("enabled", True):
            return CircuitBreakerResult(triggered=False)

        # ── ลอง auto-resume ก่อน (ถ้าถึงเวลาแล้ว) ──────────
        if self._cb_state.is_triggered:
            resumed = self._try_auto_resume()
            if resumed:
                log.info("✅ Circuit breaker auto-resumed")
            else:
                # ยังอยู่ใน pause → return triggered
                return CircuitBreakerResult(
                    triggered   = True,
                    reason      = self._cb_state.trigger_reason,
                    level       = self._cb_state.trigger_level,
                )

        # ── ตรวจทุก condition ────────────────────────────────
        checks = [
            ("floating_dd",  self._check_floating_dd),
            ("daily",        self._check_daily_loss),
            ("consecutive",  self._check_consecutive_losses),
            ("weekly",       self._check_weekly_loss),
        ]

        for level, fn in checks:
            try:
                triggered, value, limit, reason = fn()
                if triggered:
                    result = CircuitBreakerResult(
                        triggered=True, reason=reason,
                        level=level, value=value, limit=limit,
                    )
                    self._on_triggered(result)
                    return result
            except Exception as e:
                log.error(f"Circuit breaker check [{level}] error: {e}", exc_info=True)

        return CircuitBreakerResult(triggered=False)

    def get_circuit_breaker_status(self) -> dict:
        """
        สรุปสถานะ circuit breaker ทั้งหมด
        ใช้สำหรับ /status ใน Telegram bot หรือ dashboard

        Returns dict:
          {
            "triggered": False,
            "daily_loss": -45.20,
            "daily_limit": -300.00,
            "weekly_loss": -120.00,
            "consecutive": 2,
            "floating_dd": -0.8,
          }
        """
        status = {
            "triggered"       : self._cb_state.is_triggered,
            "trigger_level"   : self._cb_state.trigger_level,
            "trigger_reason"  : self._cb_state.trigger_reason,
            "triggered_at"    : self._cb_state.triggered_at,
            "auto_resume_at"  : self._cb_state.auto_resume_at,
        }

        # เพิ่มค่าจริงสำหรับแสดงใน dashboard
        try:
            status["daily_loss_usd"]   = round(self._get_pnl(days_back=1), 2)
            status["weekly_loss_usd"]  = round(self._get_pnl(days_back=7), 2)
            status["consecutive_count"]= self._get_consecutive_loss_count()

            acc = self._get_account_info()
            if acc:
                bal = acc.get("balance", 0)
                eq  = acc.get("equity",  0)
                status["balance"]      = bal
                status["equity"]       = eq
                status["floating_dd_usd"] = round(bal - eq, 2)
                status["floating_dd_pct"] = round((bal - eq) / bal * 100, 2) if bal > 0 else 0
        except Exception:
            pass

        return status

    # ══════════════════════════════════════════════════════════
    # Individual Checks
    # ══════════════════════════════════════════════════════════

    def _check_daily_loss(self) -> tuple:
        """
        ตรวจว่าขาดทุนวันนี้เกิน limit หรือไม่
        Returns: (triggered, value, limit, reason)
        """
        cfg = CFG.get("circuit_breaker", {}).get("daily", {})
        if not cfg.get("enabled", True):
            return False, 0.0, 0.0, ""

        daily_pnl = self._get_pnl(days_back=1)   # negative = loss
        if daily_pnl >= 0:
            return False, 0.0, 0.0, ""            # กำไรหรือ break-even → OK

        loss = abs(daily_pnl)

        # ── limit แบบ USD ─────────────────────────────────────
        loss_usd = cfg.get("loss_usd")
        if loss_usd and loss >= float(loss_usd):
            return (
                True, -loss, -float(loss_usd),
                f"Daily loss ${loss:.2f} hit USD limit ${loss_usd:.2f}",
            )

        # ── limit แบบ % ของ balance ───────────────────────────
        loss_pct = cfg.get("loss_pct", 3.0)
        acc = self._get_account_info()
        if acc and acc.get("balance", 0) > 0:
            balance      = acc["balance"]
            limit_usd    = balance * loss_pct / 100
            if loss >= limit_usd:
                return (
                    True, -loss, -limit_usd,
                    f"Daily loss ${loss:.2f} ({loss/balance*100:.1f}%) "
                    f"hit limit {loss_pct:.1f}% (${limit_usd:.2f})",
                )

        return False, 0.0, 0.0, ""

    def _check_weekly_loss(self) -> tuple:
        """ตรวจขาดทุนสะสม 7 วัน"""
        cfg = CFG.get("circuit_breaker", {}).get("weekly", {})
        if not cfg.get("enabled", True):
            return False, 0.0, 0.0, ""

        weekly_pnl = self._get_pnl(days_back=7)
        if weekly_pnl >= 0:
            return False, 0.0, 0.0, ""

        loss     = abs(weekly_pnl)
        loss_pct = cfg.get("loss_pct", 8.0)
        acc = self._get_account_info()

        if acc and acc.get("balance", 0) > 0:
            balance   = acc["balance"]
            limit_usd = balance * loss_pct / 100
            if loss >= limit_usd:
                return (
                    True, -loss, -limit_usd,
                    f"Weekly loss ${loss:.2f} ({loss/balance*100:.1f}%) "
                    f"hit limit {loss_pct:.1f}% (${limit_usd:.2f})",
                )

        return False, 0.0, 0.0, ""

    def _check_consecutive_losses(self) -> tuple:
        """ตรวจ consecutive losses (แพ้ติดกัน N ครั้ง)"""
        cfg = CFG.get("circuit_breaker", {}).get("consecutive_losses", {})
        if not cfg.get("enabled", True):
            return False, 0.0, 0.0, ""

        max_count = int(cfg.get("count", 5))
        count     = self._get_consecutive_loss_count()

        # อัปเดต state
        self._cb_state.consecutive_count = count

        if count >= max_count:
            return (
                True, float(count), float(max_count),
                f"Consecutive losses: {count} hits limit {max_count}",
            )

        return False, 0.0, 0.0, ""

    def _check_floating_dd(self) -> tuple:
        """
        ตรวจ floating drawdown (ขาดทุน unrealized ของ open positions)
        ไม่ต้องรอ close — เป็น real-time check
        """
        cfg = CFG.get("circuit_breaker", {}).get("floating_dd", {})
        if not cfg.get("enabled", True):
            return False, 0.0, 0.0, ""

        acc = self._get_account_info()
        if not acc:
            return False, 0.0, 0.0, ""

        balance = acc.get("balance", 0)
        equity  = acc.get("equity",  0)
        if balance <= 0:
            return False, 0.0, 0.0, ""

        floating_loss = balance - equity   # positive = loss
        if floating_loss <= 0:
            return False, 0.0, 0.0, ""

        max_pct   = cfg.get("max_pct", 5.0)
        limit_usd = balance * max_pct / 100
        dd_pct    = floating_loss / balance * 100

        if floating_loss >= limit_usd:
            return (
                True, -floating_loss, -limit_usd,
                f"Floating DD ${floating_loss:.2f} ({dd_pct:.1f}%) "
                f"hit limit {max_pct:.1f}% (${limit_usd:.2f})",
            )

        # warn ถ้าใกล้ limit (70%)
        warn_pct = cfg.get("warn_pct", 70)
        if dd_pct >= max_pct * warn_pct / 100:
            log.warning(
                f"⚠️  Floating DD ${floating_loss:.2f} ({dd_pct:.1f}%) "
                f"approaching limit {max_pct:.1f}%"
            )

        return False, 0.0, 0.0, ""

    # ══════════════════════════════════════════════════════════
    # Trigger / Resume
    # ══════════════════════════════════════════════════════════

    def _on_triggered(self, result: CircuitBreakerResult):
        """เรียกเมื่อ circuit breaker trigger — บันทึก state + notify"""
        now = datetime.now(timezone.utc)
        cfg_cb = CFG.get("circuit_breaker", {})

        # ── คำนวณ auto-resume time ───────────────────────────
        auto_resume_at = None

        if result.level == "daily":
            # auto-resume ตี 00:05 UTC ของวันถัดไป
            if cfg_cb.get("daily", {}).get("auto_resume", True):
                tomorrow = (now + timedelta(days=1)).replace(
                    hour=0, minute=5, second=0, microsecond=0
                )
                auto_resume_at = tomorrow.isoformat()

        elif result.level == "consecutive":
            # auto-resume หลัง N ชั่วโมง
            cooldown_h = cfg_cb.get("consecutive_losses", {}).get("cooldown_hours", 4)
            auto_resume_at = (now + timedelta(hours=cooldown_h)).isoformat()

        elif result.level in ("weekly", "floating_dd"):
            # manual only — ให้ user /resume เอง
            auto_resume_at = None

        # ── บันทึก state ─────────────────────────────────────
        self._cb_state.is_triggered  = True
        self._cb_state.trigger_level  = result.level
        self._cb_state.trigger_reason = result.reason
        self._cb_state.triggered_at   = now.isoformat()
        self._cb_state.auto_resume_at = auto_resume_at
        self._save_cb_state()

        # ── Log ───────────────────────────────────────────────
        log.critical(str(result))
        if auto_resume_at:
            log.info(f"  Auto-resume scheduled at: {auto_resume_at}")
        else:
            log.info("  Manual /resume required via Telegram")

        # ── Telegram notify ───────────────────────────────────
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
        """
        ตรวจว่าถึงเวลา auto-resume หรือยัง
        คืน True ถ้า resume แล้ว
        """
        if not self._cb_state.auto_resume_at:
            return False   # manual only

        now        = datetime.now(timezone.utc)
        resume_at  = datetime.fromisoformat(self._cb_state.auto_resume_at)
        if resume_at.tzinfo is None:
            resume_at = resume_at.replace(tzinfo=timezone.utc)

        if now >= resume_at:
            self._cb_state.is_triggered      = False
            self._cb_state.trigger_level     = ""
            self._cb_state.trigger_reason    = ""
            self._cb_state.triggered_at      = None
            self._cb_state.auto_resume_at    = None
            self._cb_state.consecutive_count = 0
            self._save_cb_state()

            try:
                from bot.notifier import notify
                notify("✅ *Circuit Breaker Auto-Resumed*\nBot กลับมาเทรดตามปกติ")
            except Exception:
                pass

            return True

        return False

    # ══════════════════════════════════════════════════════════
    # Data Helpers
    # ══════════════════════════════════════════════════════════

    def _get_pnl(self, days_back: int = 1) -> float:
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
            log.error(f"_get_pnl error: {e}")
            return 0.0

    def _get_consecutive_loss_count(self) -> int:
        """
        นับจำนวน consecutive losses ล่าสุด
        หยุดนับเมื่อเจอ trade ที่กำไร
        """
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
                rows  = cur.fetchall()
                count = 0
                for (profit,) in rows:
                    p = float(profit) if profit is not None else 0.0
                    if p < 0:
                        count += 1
                    else:
                        break   # เจอ winner → หยุดนับ
                return count
            finally:
                conn.close()
        except Exception as e:
            log.error(f"_get_consecutive_loss_count error: {e}")
            return 0

    def _get_account_info(self) -> Optional[dict]:
        """ดึง account info จาก MT5"""
        try:
            import MetaTrader5 as mt5
            acc = mt5.account_info()
            if acc is None:
                return None
            return {"balance": acc.balance, "equity": acc.equity, "profit": acc.profit}
        except Exception:
            return None

    # ══════════════════════════════════════════════════════════
    # State Persistence (flags/circuit_breaker_state.json)
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
