# ══════════════════════════════════════════════════════════════
# CIRCUIT BREAKER — ส่วนที่ต้องเพิ่มใน bot/main.py
# ══════════════════════════════════════════════════════════════
#
# ไฟล์นี้แสดง pattern ที่ต้องเพิ่มใน bot/main.py
# แค่ 1 จุด — ต้น main loop ก่อน process candle/signal ทุกครั้ง
#
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# รูปแบบ main.py ที่มักพบ — เพิ่ม circuit breaker check ต้น loop
# ══════════════════════════════════════════════════════════════
#
# BEFORE (โครงสร้าง main loop เดิม):
# ─────────────────────────────────────────────────────────────
#
#   while True:
#       try:
#           if _is_paused():
#               time.sleep(60)
#               continue
#
#           for symbol in symbols:
#               candle = get_latest_candle(symbol)
#               signal = model.predict(candle)
#               if signal:
#                   executor.execute_order(symbol, signal, ...)
#
#           time.sleep(candle_interval)
#
#       except KeyboardInterrupt:
#           break
#       except Exception as e:
#           log.error(f"Main loop error: {e}")
#           time.sleep(60)
#
# ─────────────────────────────────────────────────────────────
# AFTER (เพิ่ม circuit breaker block ━━━ ดูบรรทัดที่มี ← ━━━):
# ─────────────────────────────────────────────────────────────
#
#   while True:
#       try:
#           if _is_paused():
#               time.sleep(60)
#               continue
#
#           # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#           # ← CIRCUIT BREAKER CHECK — เพิ่มตรงนี้
#           cb_result = risk_manager.check_circuit_breaker()
#           if cb_result.triggered:
#               log.critical(f"⛔ {cb_result}")
#               _set_pause(True)          ← pause bot flag
#               time.sleep(CYCLE_SLEEP)   ← รอ แล้ว loop ต่อ
#               continue                  ← จะ auto-resume ใน loop ถัดไป
#           # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
#           for symbol in symbols:
#               candle = get_latest_candle(symbol)
#               signal = model.predict(candle)
#               if signal:
#                   executor.execute_order(symbol, signal, ...)
#
#           time.sleep(candle_interval)
#
#       except KeyboardInterrupt:
#           break
#       except Exception as e:
#           log.error(f"Main loop error: {e}")
#           time.sleep(60)
#
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# COMPLETE main.py template (ถ้า main.py ยังไม่ได้เขียน)
# ══════════════════════════════════════════════════════════════

import logging
import time
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config         import get_config
from bot.notifier   import notify

log = logging.getLogger("bot.main")
CFG = get_config()

# ── pause flag paths ──────────────────────────────────────────
_FLAGS_DIR  = Path(CFG.get("paths", {}).get("flags_dir", "flags"))
_PAUSE_FLAG = _FLAGS_DIR / "bot_paused.flag"

CYCLE_SLEEP = 60   # วินาที ระหว่าง cycle (M15 candle → 900)


def _is_paused() -> bool:
    return _PAUSE_FLAG.exists()


def _set_pause(state: bool):
    _FLAGS_DIR.mkdir(parents=True, exist_ok=True)
    if state:
        _PAUSE_FLAG.touch()
        log.warning("🔴 Bot PAUSED")
    else:
        _PAUSE_FLAG.unlink(missing_ok=True)
        log.info("🟢 Bot RESUMED")


def run_bot():
    """
    Main trading loop
    ─────────────────
    ลำดับการตรวจสอบทุก cycle:

      1. is_paused?          → skip ถ้าถูก pause (manual หรือ CB)
      2. circuit_breaker?    → auto-pause ถ้า hit limit
      3. news_window?        → skip ถ้าใกล้ข่าวใหญ่
      4. for each symbol:
           a. spread ok?     → skip ถ้า spread กว้างเกิน
           b. signal?        → รัน model
           c. execute?       → ส่ง order
    """
    from bot.risk_manager   import RiskManager
    from bot.executor       import Executor
    from bot.mt5_client     import MT5Client
    from data.news_filter   import NewsFilter

    mt5_client    = MT5Client()
    risk_manager  = RiskManager()
    executor      = Executor(risk_manager=risk_manager)
    news_filter   = NewsFilter()

    symbols = CFG["trading"]["symbols"]

    log.info("=" * 60)
    log.info("AURUM BOT — starting main loop")
    log.info(f"Symbols  : {symbols}")
    log.info(f"Cycle    : {CYCLE_SLEEP}s")
    log.info("=" * 60)

    while True:
        try:
            # ── [1] Pause check ───────────────────────────────
            if _is_paused():
                log.debug("Bot paused — waiting...")
                time.sleep(CYCLE_SLEEP)
                continue

            # ── [2] Circuit Breaker ───────────────────────────
            cb_result = risk_manager.check_circuit_breaker()
            if cb_result.triggered:
                # risk_manager._on_triggered() ถูกเรียกไปแล้วใน check_circuit_breaker
                # ที่นี่แค่ set pause flag ของ bot
                log.critical(f"⛔ CIRCUIT BREAKER: {cb_result}")
                _set_pause(True)
                time.sleep(CYCLE_SLEEP)
                continue   # loop ต่อ → ใน cycle ถัดไปจะ _try_auto_resume()

            # ── [3] Per-symbol processing ─────────────────────
            for symbol in symbols:

                # [3a] News window
                if news_filter.is_news_window(symbol=symbol):
                    log.info(f"[{symbol}] ⚠️  News window — skip")
                    continue

                # [3b] Get latest candle / features
                # candle = mt5_client.get_latest_candle(symbol)

                # [3c] Generate signal
                # signal = model.predict(candle)

                # [3d] Execute (spread check is inside executor)
                # if signal:
                #     executor.execute_order(symbol, signal, ...)

                pass   # ← แทนที่บรรทัดนี้ด้วยโค้ดจริง

            time.sleep(CYCLE_SLEEP)

        except KeyboardInterrupt:
            log.info("KeyboardInterrupt — stopping bot")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            time.sleep(CYCLE_SLEEP)


if __name__ == "__main__":
    from bot.setup_logging import setup_logging
    setup_logging()
    run_bot()
