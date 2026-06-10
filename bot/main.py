# bot/main.py
"""
Trading Bot — Main Loop
════════════════════════════════════════════════════════════
รันเป็น Windows Service ด้วย NSSM
ทำงานทุก 15 นาที: ราคา → features → predict → order

Startup sequence:
  1. connect MT5
  2. init DB
  3. load models
  4. quick data update
  5. schedule loop
  6. run forever
════════════════════════════════════════════════════════════
"""

import logging
import logging.config
import yaml
import time
import json
import signal
import sys
import traceback
from pathlib import Path
from datetime import datetime, timezone

import schedule
import MetaTrader5 as mt5

# โหลด config + logging
with open("logging.yaml", encoding="utf-8") as f:
    logging.config.dictConfig(yaml.safe_load(f))
with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

log = logging.getLogger("bot.main")

# ── Imports ────────────────────────────────────────────────────
from bot.mt5_client      import MT5Client
from bot.risk_manager    import RiskManager
from bot.executor        import OrderExecutor
from bot.metrics_writer  import MetricsWriter, init_db
from bot.notifier        import notify
from features.pipeline   import build_features_live
from models.strategies   import ACTIVE_STRATEGY


# ══════════════════════════════════════════════════════════════
# Bot State
# ══════════════════════════════════════════════════════════════
class BotState:
    """
    State ของบอท — shared ระหว่างทุก function
    ใช้แทน global variables
    """
    def __init__(self):
        self.running        = True
        self.tick_count     = 0
        self.error_count    = 0
        self.last_tick_time = None
        self.symbols        = CFG['symbols']['active']
        self.timeframe      = CFG['symbols']['primary_timeframe']
        self.config_mtime   = Path("config.yaml").stat().st_mtime

        # Components (init ใน startup())
        self.client   : MT5Client     = None
        self.risk     : RiskManager   = None
        self.executor : OrderExecutor = None
        self.writer   : MetricsWriter = None
        self.strategy                 = None

        # Cooldown tracking
        # ป้องกันเทรดซ้ำ symbol เดียวกันถี่เกินไป
        self.last_trade_time: dict = {}

        # Error tracking per symbol
        self.symbol_errors: dict = {s: 0 for s in self.symbols}

    def is_paused(self) -> bool:
        """ตรวจ pause flag — สร้างไฟล์ flags/paused เพื่อหยุดชั่วคราว"""
        return Path("flags/paused").exists()

    def reset_error_count(self):
        self.error_count = 0

    def increment_error(self, symbol: str = None):
        self.error_count += 1
        if symbol:
            self.symbol_errors[symbol] = \
                self.symbol_errors.get(symbol, 0) + 1


# Singleton state
STATE = BotState()


# ══════════════════════════════════════════════════════════════
# Startup
# ══════════════════════════════════════════════════════════════
def startup():
    """
    เริ่มต้น bot ทุกอย่าง
    รันครั้งเดียวตอนเริ่ม service
    """
    log.info("=" * 60)
    log.info("🤖 Trading Bot Starting...")
    log.info(f"   Strategy: {ACTIVE_STRATEGY.VERSION}")
    log.info(f"   Symbols:  {STATE.symbols}")
    log.info(f"   Timeframe:{STATE.timeframe}")
    log.info("=" * 60)

    # ── 1. สร้างโฟลเดอร์ที่ต้องการ ─────────────────────────────
    for folder in ["logs", "logs/daily", "db", "flags", "reports"]:
        Path(folder).mkdir(parents=True, exist_ok=True)

    # ── 2. Init Database ────────────────────────────────────────
    init_db()
    log.info("✅ Database initialized")

    # ── 3. Connect MT5 ─────────────────────────────────────────
    STATE.client = MT5Client()
    if not STATE.client.connect():
        log.critical("❌ MT5 connect ล้มเหลว!")
        notify("🚨 BOT START FAILED — MT5 connect error")
        sys.exit(1)
    log.info("✅ MT5 connected")

    # ── 4. Init Components ─────────────────────────────────────
    STATE.risk     = RiskManager()
    STATE.writer   = MetricsWriter()
    STATE.executor = OrderExecutor(STATE.client, STATE.risk)
    STATE.strategy = ACTIVE_STRATEGY()
    log.info("✅ Components initialized")

    # ── 5. Quick Data Update ───────────────────────────────────
    log.info("📊 Updating market data...")
    try:
        from data.pipeline import run_quick_update
        run_quick_update()
        log.info("✅ Market data updated")
    except Exception as e:
        log.warning(f"Quick update ล้มเหลว (non-fatal): {e}")

    # ── 6. Verify Models ───────────────────────────────────────
    log.info("🧠 Verifying models...")
    missing = _check_models_exist()
    if missing:
        log.critical(f"❌ Models ไม่ครบ: {missing}")
        notify(f"🚨 BOT START FAILED — missing models: {missing}")
        sys.exit(1)
    log.info("✅ All models loaded")

    # ── 7. Log Account Info ────────────────────────────────────
    acc = STATE.client.get_account()
    log.info(
        f"💰 Account: {acc['balance']:.2f} USD | "
        f"Equity: {acc['equity']:.2f} | "
        f"Leverage: 1:{acc['leverage']}"
    )
    STATE.writer.write_account(acc)

    # ── 8. Notify Start ────────────────────────────────────────
    notify(
        f"✅ *Bot Started*\n"
        f"Strategy: v{ACTIVE_STRATEGY.VERSION}\n"
        f"Symbols: {', '.join(STATE.symbols)}\n"
        f"Balance: ${acc['balance']:,.2f}\n"
        f"Time: {_now_str()}"
    )

    log.info("🚀 Bot startup complete — entering main loop")


def _check_models_exist() -> list:
    """ตรวจว่าโมเดลทุกตัวมีอยู่"""
    missing = []
    models_dir = Path(CFG['paths']['models_saved'])

    for sym in STATE.symbols:
        if not (models_dir / f"xgb_{sym}.pkl").exists():
            missing.append(f"xgb_{sym}")
        if not (models_dir / f"lgbm_{sym}.pkl").exists():
            missing.append(f"lgbm_{sym}")
        # LSTM optional
    return missing


# ══════════════════════════════════════════════════════════════
# Core Tick Function
# ══════════════════════════════════════════════════════════════
def run_tick(symbol: str):
    """
    1 tick สำหรับ 1 symbol
    ราคา → features → predict → (order ถ้า signal ดี)
    """
    t0 = time.time()

    try:
        # ── 1. ตรวจ pause flag ────────────────────────────────
        if STATE.is_paused():
            log.info(f"⏸ Bot paused — skip {symbol}")
            return

        # ── 2. ตรวจ daily loss limit ──────────────────────────
        acc = STATE.client.get_account()
        STATE.writer.write_account(acc)

        if not STATE.risk.check_daily_loss(acc['balance']):
            log.warning(
                f"🛑 Daily loss limit — stop all trading today"
            )
            STATE.executor.close_all()
            notify(
                f"🛑 *Daily Loss Limit Hit*\n"
                f"Balance: ${acc['balance']:,.2f}\n"
                f"All positions closed"
            )
            return

        # ── 3. ดึงราคาล่าสุด ─────────────────────────────────
        tf_map = {
            "M15": mt5.TIMEFRAME_M15,
            "H1" : mt5.TIMEFRAME_H1,
            "M5" : mt5.TIMEFRAME_M5,
        }
        tf = tf_map.get(STATE.timeframe, mt5.TIMEFRAME_M15)

        df_primary = STATE.client.get_ohlcv(symbol, tf, n_bars=300)

        # โหลด HTF สำหรับ multi-timeframe
        htf_dfs = {}
        for htf_name in CFG['symbols']['htf_timeframes']:
            htf_tf = tf_map.get(htf_name)
            if htf_tf:
                htf_dfs[htf_name] = STATE.client.get_ohlcv(
                    symbol, htf_tf, n_bars=200
                )

        # ── 4. Build Features ─────────────────────────────────
        df = build_features_live(df_primary, symbol, STATE.timeframe)

        # ── 5. Strategy Evaluate ──────────────────────────────
        setup = STATE.strategy.evaluate(df, symbol)

        log.info(
            f"{symbol}: {setup} | "
            f"filters={setup.filters_passed}"
        )

        # ── 6. Cooldown Check ─────────────────────────────────
        if not _check_cooldown(symbol, setup.direction):
            log.info(f"{symbol}: cooldown active — skip")
            return

        # ── 7. Send Order ─────────────────────────────────────
        if setup.is_valid:
            result = STATE.executor.send_order(
                symbol      = symbol,
                direction   = setup.direction,
                sl_distance = setup.sl_distance,
                tp_distance = setup.tp_distance,
                confidence  = setup.confidence,
                comment     = (
                    f"v{ACTIVE_STRATEGY.VERSION}_"
                    f"{setup.session}_"
                    f"c{setup.confidence:.2f}"
                ),
            )

            if result['success']:
                # บันทึก trade และ update cooldown
                STATE.last_trade_time[symbol] = time.time()
                STATE.symbol_errors[symbol]   = 0
                STATE.writer.write_signal(
                    symbol, setup.direction, setup.confidence
                )
            else:
                log.warning(
                    f"{symbol} order failed: "
                    f"{result.get('reason','unknown')}"
                )

        else:
            # Log ว่าทำไมไม่เทรด
            if setup.filters_failed:
                log.debug(
                    f"{symbol} skip: {setup.filters_failed}"
                )

        # ── Reset error count ──────────────────────────────────
        STATE.symbol_errors[symbol] = 0
        elapsed = time.time() - t0
        log.debug(f"{symbol} tick: {elapsed*1000:.0f}ms")

    except Exception as e:
        STATE.increment_error(symbol)
        log.error(
            f"❌ {symbol} tick error: {e}",
            exc_info=True,
        )

        # ถ้า error มากเกิน threshold → notify
        if STATE.symbol_errors.get(symbol, 0) >= 5:
            notify(
                f"⚠️ *{symbol} repeated errors*\n"
                f"Error: {str(e)[:100]}\n"
                f"Count: {STATE.symbol_errors[symbol]}"
            )


def _check_cooldown(symbol: str, direction: int) -> bool:
    """
    ป้องกันเปิด order ซ้ำ symbol เดิมถี่เกินไป
    cooldown_min จาก config
    """
    if direction == 0:
        return True   # HOLD ไม่ต้อง check cooldown

    last_time = STATE.last_trade_time.get(symbol, 0)
    cooldown  = CFG['signal']['cooldown_min'] * 60
    elapsed   = time.time() - last_time

    if elapsed < cooldown:
        remaining = int((cooldown - elapsed) / 60)
        log.debug(
            f"{symbol}: cooldown {remaining}min remaining"
        )
        return False
    return True


# ══════════════════════════════════════════════════════════════
# Scheduled Jobs
# ══════════════════════════════════════════════════════════════
def run_all_symbols():
    """รัน tick ทุก symbol — เรียกจาก scheduler"""
    STATE.tick_count    += 1
    STATE.last_tick_time = datetime.now(timezone.utc)

    log.info(
        f"─── Tick #{STATE.tick_count} | "
        f"{_now_str()} ───"
    )

    # ตรวจ config reload (hot-reload)
    _check_config_reload()

    # ตรวจ MT5 connection
    STATE.client.ensure_connected()

    # รัน tick แต่ละ symbol
    for sym in CFG['symbols']['active']:
        run_tick(sym)

    # บันทึก account snapshot
    try:
        acc = STATE.client.get_account()
        STATE.writer.write_account(acc)
    except Exception as e:
        log.warning(f"Account write error: {e}")


def _daily_summary():
    """ส่ง daily summary ทุกคืน"""
    try:
        acc     = STATE.client.get_account()
        summary = STATE.writer.get_daily_summary()

        notify(
            f"📊 *Daily Summary*\n"
            f"Date:    {_now_str()[:10]}\n"
            f"Balance: ${acc['balance']:,.2f}\n"
            f"P&L:     ${acc['profit']:+.2f}\n"
            f"Trades:  {summary.get('trades',0)}\n"
            f"Win Rate:{summary.get('win_rate',0):.1%}\n"
            f"Ticks:   {STATE.tick_count}"
        )

        # Reset daily tracking
        STATE.risk.reset_daily()
        STATE.tick_count = 0

    except Exception as e:
        log.error(f"Daily summary error: {e}")


def _weekly_retrain():
    """Retrain โมเดลอัตโนมัติทุกอาทิตย์"""
    log.info("🔄 Weekly retrain starting...")
    notify("🔄 *Weekly Retrain Starting*")

    try:
        from scripts.retrain_all import retrain_all
        results = retrain_all()
        notify(
            f"✅ *Retrain Complete*\n"
            f"Models: {list(results.keys())}\n"
            f"Time: {_now_str()}"
        )
    except Exception as e:
        log.error(f"Retrain error: {e}")
        notify(f"❌ *Retrain Failed*\n{str(e)[:200]}")


def _check_config_reload():
    """Hot-reload config.yaml ถ้าไฟล์เปลี่ยน"""
    global CFG
    try:
        new_mtime = Path("config.yaml").stat().st_mtime
        if new_mtime != STATE.config_mtime:
            with open("config.yaml", encoding="utf-8") as f:
                CFG = yaml.safe_load(f)
            STATE.config_mtime = new_mtime
            log.info("🔄 config.yaml reloaded")
    except Exception as e:
        log.warning(f"Config reload error: {e}")


# ══════════════════════════════════════════════════════════════
# Graceful Shutdown
# ══════════════════════════════════════════════════════════════
def _handle_shutdown(signum, frame):
    """
    จัดการ shutdown อย่างสะอาด
    ปิด position ทั้งหมดและ disconnect MT5
    """
    log.info("🛑 Shutdown signal received...")
    STATE.running = False

    try:
        # ปิด pending orders (optional — comment ถ้าไม่ต้องการ)
        # STATE.executor.close_all()

        # Disconnect MT5
        if STATE.client:
            STATE.client.disconnect()

        # Final account snapshot
        notify(
            f"🛑 *Bot Stopped*\n"
            f"Ticks: {STATE.tick_count}\n"
            f"Time: {_now_str()}"
        )

    except Exception as e:
        log.error(f"Shutdown error: {e}")
    finally:
        log.info("Bot stopped cleanly")
        sys.exit(0)


# ══════════════════════════════════════════════════════════════
# Main Entry Point
# ══════════════════════════════════════════════════════════════
def main():
    """
    Entry point — เรียกจาก NSSM service หรือ run_bot.bat
    """
    # ── Graceful shutdown handlers ────────────────────────────
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT,  _handle_shutdown)

    # ── Startup ───────────────────────────────────────────────
    try:
        startup()
    except Exception as e:
        log.critical(f"Startup failed: {e}", exc_info=True)
        sys.exit(1)

    # ── Schedule Jobs ─────────────────────────────────────────
    interval = CFG['symbols']['primary_timeframe']
    minutes  = int(interval.replace('M','').replace('H','')) \
                if 'M' in interval else \
                int(interval.replace('H','')) * 60

    # Main tick — ทุก 15 นาที
    schedule.every(minutes).minutes.do(run_all_symbols)

    # Daily summary — ทุกเที่ยงคืน UTC
    summary_hour = CFG['notifications']['daily_summary_hour']
    schedule.every().day.at(f"{summary_hour:02d}:00").do(
        _daily_summary
    )

    # Weekly retrain — ทุกวันอาทิตย์ ตี 2
    schedule.every().sunday.at("02:00").do(_weekly_retrain)

    # รัน tick แรกทันที (ไม่รอรอบถัดไป)
    log.info("▶ Running initial tick...")
    run_all_symbols()

    # ── Main Loop ─────────────────────────────────────────────
    log.info(
        f"⏱  Scheduler: every {minutes} min | "
        f"daily summary: {summary_hour:02d}:00 UTC"
    )

    consecutive_errors = 0

    while STATE.running:
        try:
            schedule.run_pending()
            time.sleep(1)
            consecutive_errors = 0

        except KeyboardInterrupt:
            log.info("KeyboardInterrupt — stopping")
            _handle_shutdown(None, None)

        except Exception as e:
            consecutive_errors += 1
            log.error(
                f"Main loop error #{consecutive_errors}: {e}",
                exc_info=True,
            )

            # ถ้า error ติดต่อกัน 10 ครั้ง → restart service
            if consecutive_errors >= 10:
                log.critical(
                    "Too many consecutive errors — "
                    "requesting restart"
                )
                notify(
                    f"🚨 *Bot Critical Error*\n"
                    f"10 consecutive errors\n"
                    f"Restarting service...\n"
                    f"Last error: {str(e)[:200]}"
                )
                sys.exit(1)   # NSSM จะ restart อัตโนมัติ

            time.sleep(30)    # รอก่อน retry


# ── Helpers ────────────────────────────────────────────────────
def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


if __name__ == "__main__":
    main()