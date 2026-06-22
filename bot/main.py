# bot/main.py
"""
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

Defense layers ทุก tick:
  [0]   Telegram reset flags   → reset CB โดยไม่ restart
  [0.5] Session loss update    → สแกน MT5 history อัปเดต session_losses
  [1]   Manual pause flag
  [2]   MT5 connection         → auto-reconnect (ensure_connected)
  [3]   Circuit breaker        → auto-pause
  [4]   News window            → skip tick
  [4.5] Session filter         → skip tick ถ้าออกนอก London/NY
  [4.7] Regime detection       → skip tick ถ้า volatile/low-conf

Per-symbol (run_tick):
  [1.3] Session loss limit     → stop XAU after N losses / session
  [5.5] Per-symbol conf gate   → XAU ต้องการ 0.75 (global 0.55)  # ⚠️ FIX: comment เดิม 0.65 ไม่ตรง config.yaml
  [6]   Cooldown (loss-aware)  → XAU พัก 60 min หลังแพ้ (global 15 min)
  [8]   Volume multiplier      → XAU lot × 0.50  # ⚠️ FIX: comment เดิม 0.70 ไม่ตรง config.yaml

Session resets: 6:00 / 13:00 / 21:00 UTC (config: daily_tracking.reset_hours_utc)
════════════════════════════════════════════════════════════
"""
import logging
import time
import signal
import sys
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
import traceback
from pathlib import Path
from datetime import datetime, timezone
from data.news_filter import NewsFilter

import schedule
import MetaTrader5 as mt5

# ✅ FIX BUG-1: ใช้ setup_logging() แทน dictConfig โดยตรง
from bot.setup_logging import setup_logging
setup_logging()   # ← ต้องเรียกก่อน import อื่นๆ ทั้งหมด

# ✅ FIX BUG-2+5+7: ใช้ get_config() แทน yaml.safe_load โดยตรง
from config import get_config, validate_config, reload_config, unresolve_symbol
CFG = get_config()

log = logging.getLogger("bot.main")

# ── Flags directory (shared กับ telegram_bot.py) ───────────────
_FLAGS_DIR = Path(CFG.get('paths', {}).get('flags', 'flags'))

# ── Reset flag specs ────────────────────────────────────────────
# (flag_filename, reset_consecutive, reset_daily_tracking, label)
_RESET_SPECS = [
    ("reset_all.flag",   True,  True,  "all"),
    ("reset_daily.flag", False, True,  "daily"),
    ("reset_cb.flag",    True,  False, "consecutive"),
]

# ── Session reset hours UTC (reset per-symbol loss counters) ────
# default: 3 รอบ ตรงกับ London open / NY open / Asia close
_SESSION_RESET_HOURS: list = (
    CFG.get("daily_tracking", {}).get("reset_hours_utc", [6, 13, 21])
)

# ── Imports ────────────────────────────────────────────────────
from bot.mt5_client      import MT5Client
from bot.risk_manager    import RiskManager
from bot.executor        import OrderExecutor
from bot.metrics_writer  import MetricsWriter, init_db
from bot.notifier        import notify
from features.pipeline   import build_features_live
from models.strategies   import ACTIVE_STRATEGY
from features.regime     import RegimeDetector


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
        self.client          : MT5Client      = None
        self.risk            : RiskManager    = None
        self.executor        : OrderExecutor  = None
        self.writer          : MetricsWriter  = None
        self.strategy                         = None
        self.news_filter     : NewsFilter     = NewsFilter()
        self.regime_detector : RegimeDetector = None

        # Cooldown tracking
        self.last_trade_time: dict = {}

        # Per-symbol session loss tracking (reset _SESSION_RESET_HOURS ต่อวัน)
        # session_losses[symbol] = จำนวน loss ที่ปิดแล้วใน session ปัจจุบัน
        # last_loss_time[symbol] = Unix timestamp ที่ XAU/symbol แพ้ล่าสุด
        self.session_losses    : dict  = {s: 0 for s in self.symbols}
        self.session_start_time: float = time.time()
        self.last_loss_time    : dict  = {}   # symbol → Unix ts ของ loss ล่าสุด

        # Error tracking per symbol
        self.symbol_errors: dict = {s: 0 for s in self.symbols}

        # latch ป้องกัน close_all()+notify() ยิงซ้ำทุก tick เมื่อ circuit
        # breaker.daily trigger — reset อัตโนมัติเมื่อ circuit breaker
        # หมด pause หรือถึงเวลา daily reset
        self.daily_loss_paused: bool = False

    def is_paused(self) -> bool:
        """ตรวจ pause flag — สร้างไฟล์ flags/paused เพื่อหยุดชั่วคราว"""
        return Path("flags/paused").exists()

    def set_pause(self, state: bool):
        """เปิด/ปิด pause flag (ใช้โดย circuit breaker)"""
        flag = Path("flags/paused")
        if state:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
            log.warning("🔴 Bot PAUSED (circuit breaker)")
        else:
            flag.unlink(missing_ok=True)
            log.info("🟢 Bot RESUMED")

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

    # ── 0. Validate Config ──────────────────────────────────────
    validate_config(raise_on_error=True)
    log.info("✅ Config validated")

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

    # ── 4.5 Init RegimeDetector ────────────────────────────────
    try:
        STATE.regime_detector = RegimeDetector()
        log.info("✅ RegimeDetector initialized")
    except Exception as e:
        log.warning(
            f"⚠️  RegimeDetector init failed (non-fatal): {e} "
            f"— regime filter disabled"
        )
        STATE.regime_detector = None

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

    # ── 8. Log Session Status ──────────────────────────────────
    try:
        sessions = STATE.risk.get_all_session_info()
        for s in sessions:
            status = "🟢 OPEN" if s.is_open else f"⏳ opens in {s.opens_in_h:.1f}h"
            log.info(f"   {s.emoji} {s.name} ({s.range_str}): {status}")
    except Exception:
        pass

    # ── 9. Notify Start ────────────────────────────────────────
    notify(
        f"✅ *Bot Started*\n"
        f"Strategy: v{ACTIVE_STRATEGY.VERSION}\n"
        f"Symbols: {', '.join(STATE.symbols)}\n"
        f"Balance: ${acc['balance']:,.2f}\n"
        f"Regime: {'ON' if STATE.regime_detector else 'OFF'}\n"
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
    ราคา → features → session/regime check → predict → order
    """
    t0 = time.time()

    try:
        # ── 0. Per-symbol config ──────────────────────────────
        sym_cfg = get_symbol_config(symbol)

        # ── 1. ตรวจ pause flag ────────────────────────────────
        if STATE.is_paused():
            log.info(f"⏸ Bot paused — skip {symbol}")
            return

        # ── 1.3 Session loss limit (per-symbol) ──────────────
        cur_losses = STATE.session_losses.get(symbol, 0)
        max_losses = sym_cfg["max_session_losses"]
        if cur_losses >= max_losses:
            log.info(
                f"[{symbol}] session losses {cur_losses}/{max_losses} "
                f"→ skip until next session reset"
            )
            return

        # ── 1.5 News Window Check ─────────────────────────────
        if STATE.news_filter.is_news_window(symbol=symbol):
            log.info(f"⚠️  {symbol}: news window — skip")
            return

        # ── 1.6 Session Filter (NEW) ──────────────────────────
        # ใน per-symbol loop — หลัง news filter, ก่อน signal gen
        sess = STATE.risk.check_session(symbol=symbol)
        if not sess.ok:
            log.debug(f"[{symbol}] {sess}")
            return
        if sess.is_overlap:
            log.info(f"[{symbol}] ⚡ London+NY Overlap — prime zone")

        # ── 1.7 Regime Detection ──────────────────────────────
        regime = None
        if STATE.regime_detector is not None:
            try:
                regime = STATE.regime_detector.detect_from_mt5(
                    symbol="XAUUSD", timeframe="H4", bars=300
                    # ⚠️ NOTE: hardcode ใช้ XAU เป็น proxy เช็ค regime
                    # ทุก symbol (ไม่ใช่ bug ของ broker-agnostic แต่เป็น
                    # design เดิม) — ถ้าตั้งใจให้เช็ค regime ของ symbol
                    # นั้นๆ เอง ให้เปลี่ยนเป็น symbol=symbol แทน
                )
                log.info(f"  [{symbol}] Regime: {regime}")

                if not regime.should_trade:
                    log.info(
                        f"  [{symbol}] regime={regime} "
                        f"— skip tick (volatile/low-conf)"
                    )
                    return

            except Exception as e:
                log.warning(
                    f"  [{symbol}] Regime detection error: {e} "
                    f"— continue without regime filter"
                )
                regime = None

        # ── 2. ดึงราคาล่าสุด ─────────────────────────────────
        valid_tfs  = {"M1","M5","M15","M30","H1","H4","D1","W1"}
        primary_tf = STATE.timeframe
        if primary_tf not in valid_tfs:
            log.error(f"Unknown timeframe: {primary_tf}")
            return

        df_primary = STATE.client.get_ohlcv(symbol, primary_tf, n_bars=300)

        # โหลด HTF สำหรับ multi-timeframe
        htf_dfs = {}
        for htf_name in CFG['symbols']['htf_timeframes']:
            if htf_name in valid_tfs:
                htf_dfs[htf_name] = STATE.client.get_ohlcv(
                    symbol, htf_name, n_bars=200
                )
            else:
                log.warning(f"Unknown HTF timeframe skipped: {htf_name}")

        # ── 4. Build Features ─────────────────────────────────
        # ✅ FIX: ส่ง htf_dfs ที่ fetch สดมาแล้วเข้าไปใช้จริง
        # เดิม fetch มาแล้วทิ้งเฉยๆ — build_features_live ไปอ่าน
        # data/raw/ ของรอบ collect ล่าสุดแทน (อาจค้างคืน ไม่ใช่ราคาสด)
        df = build_features_live(
            df_primary, symbol, STATE.timeframe, htf_dfs=htf_dfs
        )

        # ── 5. Strategy Evaluate ──────────────────────────────
        setup = STATE.strategy.evaluate(df, symbol)

        log.info(
            f"{symbol}: {setup} | "
            f"filters={setup.filters_passed}"
        )

        # ── 5.5 Per-symbol confidence gate ───────────────────
        # กรอง BEFORE cooldown — ถ้า confidence ไม่ผ่าน sym threshold
        # ไม่ต้องนับ cooldown หรือ record loss
        if setup.confidence < sym_cfg["min_confidence"]:
            log.info(
                f"[{symbol}] conf {setup.confidence:.3f} < "
                f"sym threshold {sym_cfg['min_confidence']:.3f} → skip"
            )
            return

        # ── 6. Cooldown Check ─────────────────────────────────
        # ถ้า loss ล่าสุดยังอยู่ใน cooldown_after_loss → ใช้ cooldown นานขึ้น
        last_loss      = STATE.last_loss_time.get(symbol, 0)
        elapsed_loss   = time.time() - last_loss
        after_loss_sec = sym_cfg["cooldown_after_loss"] * 60
        cooldown_min   = (
            sym_cfg["cooldown_after_loss"]
            if elapsed_loss < after_loss_sec
            else None   # None = ใช้ global CFG['signal']['cooldown_min']
        )
        if not _check_cooldown(symbol, setup.direction, cooldown_min):
            log.info(f"{symbol}: cooldown active — skip")
            return

        # ── 7. Regime-Aware Signal ────────────────────────────
        reg_result = {}   # init ก่อนเสมอ กัน UnboundLocalError
        if regime is not None and hasattr(STATE.strategy, 'ensemble'):
            reg_result = STATE.strategy.ensemble.predict_with_regime(
                df, regime, symbol
            )
            is_valid = reg_result["should_trade"]

            if not is_valid:
                log.info(
                    f"  [{symbol}] predict_with_regime BLOCKED: "
                    f"{reg_result['block_reason']}"
                )
                return

            # ✅ FIX: บังคับ regime filter แม้ VIP Pass ผ่านมาแล้ว
            # เดิม VIP Pass bypass regime filter → เปิดไม้สวนเทรนด์ขาดทุน
            # ตอนนี้ blocked_combos ใน config ยังทำงานแม้ conf สูงมาก
            sig = reg_result.get("signal")
            if (sig and is_valid and
                    CFG.get('strategy_filters', {}).get('use_regime_filter', True)):
                regime_name = str(regime).lower()
                sig_dir     = sig.direction   # 1=BUY -1=SELL
                for combo in CFG.get('regime_filter', {}).get('blocked_combos', []):
                    c_regime = combo.get('regime', '').replace('_', ' ')
                    c_signal = combo.get('signal', '')
                    if (c_regime in regime_name and
                            ((c_signal == 'BUY'  and sig_dir ==  1) or
                             (c_signal == 'SELL' and sig_dir == -1))):
                        log.info(
                            f"  [{symbol}] VIP Pass blocked by regime filter: "
                            f"{combo['regime']} + {c_signal}"
                        )
                        is_valid = False
                        break

            if is_valid:
                log.info(
                    f"  [{symbol}] predict_with_regime OK "
                    f"conf={reg_result['signal'].confidence:.3f} "
                    f"threshold={reg_result['threshold_used']:.3f}"
                )
        else:
            is_valid = setup.is_valid

        # ── 7.5 Per-symbol confidence gate (regime path) ─────
        # predict_with_regime ใช้ global threshold ภายใน
        # ถ้า sym threshold สูงกว่า → กรองอีกครั้ง
        if is_valid:
            regime_conf = (
                reg_result["signal"].confidence
                if (regime is not None and "signal" in reg_result)
                else setup.confidence
            )
            if regime_conf < sym_cfg["min_confidence"]:
                log.info(
                    f"  [{symbol}] regime conf {regime_conf:.3f} < "
                    f"sym threshold {sym_cfg['min_confidence']:.3f} → blocked"
                )
                is_valid = False

        # ── 8. Send Order ─────────────────────────────────────
        if is_valid:
            # ✅ FIX: ดึง n_agree จาก path ที่ใช้จริง แล้วส่งไป executor
            # เดิมไม่ส่งเลย → default=0 → _can_add_position ข้าม agree check
            if regime is not None and "signal" in reg_result:
                n_agree = getattr(reg_result["signal"], "n_agree", 0)
            else:
                n_agree = 0
                for r in setup.reasons:
                    if "agree=" in r:
                        try:
                            n_agree = int(r.split("agree=")[1].split("/")[0])
                        except Exception:
                            pass

            result = STATE.executor.send_order(
                symbol            = symbol,
                direction         = setup.direction,
                sl_distance       = setup.sl_distance,
                tp_distance       = setup.tp_distance,
                confidence        = setup.confidence,
                n_agree           = n_agree,
                comment           = "bot_trade",
                volume_multiplier = sym_cfg["risk_multiplier"],  # XAU=0.70
            )

            # ⬅️ แก้ไขวิธีการเรียกเช็คผลลัพธ์จาก result['success'] เป็น result.success
            is_success = getattr(result, 'success', False)

            if is_success:
                # บันทึก trade และ update cooldown
                STATE.last_trade_time[symbol] = time.time()
                STATE.symbol_errors[symbol]   = 0
                STATE.writer.write_signal(
                    symbol, setup.direction, setup.confidence
                )
            else:
                err_msg = getattr(result, 'error', getattr(result, 'reason', 'unknown'))
                log.warning(f"{symbol} order failed: {err_msg}")

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
        log.error(f"❌ {symbol} tick error: {e}", exc_info=True)

        if STATE.symbol_errors.get(symbol, 0) >= 5:
            notify(
                f"⚠️ *{symbol} repeated errors*\n"
                f"Error: {str(e)[:100]}\n"
                f"Count: {STATE.symbol_errors[symbol]}"
            )


def _check_cooldown(
    symbol      : str,
    direction   : int,
    cooldown_min: int = None,   # None = อ่านจาก CFG (global default)
) -> bool:
    """
    ป้องกันเปิด order ซ้ำ symbol เดิมถี่เกินไป
    cooldown_min override ได้ต่อ symbol (XAU ใช้ cooldown_after_loss หลังแพ้)
    """
    if direction == 0:
        return True   # HOLD ไม่ต้อง check cooldown

    last_time    = STATE.last_trade_time.get(symbol, 0)
    cooldown_min = cooldown_min if cooldown_min is not None \
                   else CFG['signal']['cooldown_min']
    cooldown     = cooldown_min * 60
    elapsed      = time.time() - last_time

    if elapsed < cooldown:
        remaining = int((cooldown - elapsed) / 60)
        log.debug(f"{symbol}: cooldown {remaining}min remaining")
        return False
    return True


# ══════════════════════════════════════════════════════════════
# Per-Symbol Config & Session Loss Tracking
# ══════════════════════════════════════════════════════════════
def get_symbol_config(symbol: str) -> dict:
    """
    คืน per-symbol config ผสม default + override จาก symbol_settings

    ตัวอย่าง:
        sym_cfg = get_symbol_config("XAUUSD")
        # → {"min_confidence": 0.75, "risk_multiplier": 0.50,
        #     "max_session_losses": 1, "cooldown_after_loss": 60}
    """
    defaults = {
        "min_confidence"     : CFG.get("signal", {}).get("min_confidence", 0.55),
        "risk_multiplier"    : 1.0,
        "max_session_losses" : 99,    # 99 = ปิด feature
        "cooldown_after_loss": CFG.get("signal", {}).get("cooldown_min", 15),
    }
    overrides = dict(CFG.get("symbol_settings", {}).get(symbol, {}))
    overrides.pop("_default", None)   # ลบ key พิเศษออก
    return {**defaults, **overrides}


def _update_session_losses():
    """
    สแกน MT5 position history ตั้งแต่ต้น session
    → อัปเดต STATE.session_losses[symbol] และ STATE.last_loss_time[symbol]
    เรียกต้น run_all_symbols() ทุก tick (1 API call รวมทุก symbol)
    """
    try:
        since = datetime.fromtimestamp(
            STATE.session_start_time, tz=timezone.utc
        )
        now   = datetime.now(timezone.utc)

        deals = mt5.history_deals_get(since, now)
        if not deals:
            return

        magic = CFG.get("order", {}).get("magic_number", 0)

        new_losses   = {s: 0 for s in STATE.symbols}
        last_loss_ts = {}

        for d in deals:
            # ✅ CRITICAL FIX: d.symbol จาก MT5 เป็นชื่อ broker (XAUUSDm)
            # แต่ new_losses ใช้ key ชื่อกลาง (XAUUSD) — ถ้าไม่แปลงก่อน
            # เงื่อนไขข้างล่างจะ False เสมอ → session loss tracking
            # พังเงียบๆ ไม่มี error เลย (นับ loss ไม่ได้สักครั้ง)
            sym = unresolve_symbol(d.symbol)
            if sym not in new_losses:
                continue
            if d.magic != magic:
                continue          # ข้าม deal ของ bot อื่น
            if d.profit < 0:
                new_losses[sym] += 1
                last_loss_ts[sym] = max(
                    last_loss_ts.get(sym, 0), d.time
                )

        STATE.session_losses = new_losses
        STATE.last_loss_time.update(last_loss_ts)

    except Exception as e:
        log.debug(f"_update_session_losses error: {e}")


def _do_session_reset():
    """
    Reset per-symbol session loss counters
    เรียกโดย scheduler 3 ครั้งต่อวัน (6:00 / 13:00 / 21:00 UTC default)
    ตั้งค่าใน config: daily_tracking.reset_hours_utc
    """
    STATE.session_losses     = {s: 0 for s in STATE.symbols}
    STATE.session_start_time = time.time()
    STATE.last_loss_time     = {}

    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    log.info(f"📅 Session reset @ {now_str} — per-symbol loss counters cleared")

    try:
        notify(
            f"📅 *Session Reset* @ `{now_str}`\n"
            f"Loss counters cleared — bot continues trading"
        )
    except Exception:
        pass


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

    # ── [0] Telegram Reset Flags ──────────────────────────────
    # Telegram bot สร้าง flag file → main loop อ่านแล้ว reset in-memory state
    # ทำก่อน CB check เสมอ เพื่อให้ reset มีผลใน tick เดียวกัน
    for fname, reset_consec, reset_daily, label in _RESET_SPECS:
        flag = _FLAGS_DIR / fname
        if flag.exists():
            try:
                STATE.risk.reset_circuit_breaker(
                    reset_consecutive    = reset_consec,
                    reset_daily_tracking = reset_daily,
                )
                if reset_daily:
                    STATE.daily_loss_paused = False  # ✅ FIX: sync กับ reset_daily_tracking
                STATE.set_pause(False)
                flag.unlink(missing_ok=True)
                log.info(f"✅ Bot reset [{label}] by Telegram request")
                notify(
                    f"✅ *Bot Reset [{label.upper()}]*\n"
                    f"Circuit breaker cleared\n"
                    f"Bot กลับมาเทรดแล้ว\n"
                    f"⏰ `{_now_str()}`"
                )
            except Exception as e:
                log.error(f"Reset [{label}] error: {e}", exc_info=True)
                try:
                    flag.unlink(missing_ok=True)  # ลบ flag ทิ้งถึงแม้ error
                except Exception:
                    pass

    # ── [0.5] อัปเดต session losses จาก MT5 history ──────────
    # ตรวจ deals ที่ปิดใน session นี้ → อัปเดต session_losses + last_loss_time
    # 1 API call ต่อ tick รวมทุก symbol (overhead ต่ำ)
    _update_session_losses()

    # ── [2] MT5 connection → auto-reconnect ───────────────────
    if not STATE.client.ensure_connected():
        log.critical(
            f"[tick {STATE.tick_count}] "
            f"MT5 reconnect failed — skip this tick"
        )
        return

    # ── [3] Circuit Breaker ───────────────────────────────────
    cb_result = STATE.risk.check_circuit_breaker()
    if cb_result.triggered:
        log.critical(f"⛔ CIRCUIT BREAKER: {cb_result}")
        # daily loss: ปิด position ทันที + แจ้ง Telegram (ครั้งแรกที่ trigger)
        if cb_result.level == "daily" and not STATE.daily_loss_paused:
            STATE.daily_loss_paused = True
            acc_cb = STATE.client.get_account()
            STATE.executor.close_all(reason="daily_loss_limit")
            notify(
                f"🛑 *Daily Loss Limit Hit*\n"
                f"Balance: ${acc_cb['balance']:,.2f}\n"
                f"All positions closed — resume tomorrow"
            )
        STATE.set_pause(True)
        return
    elif STATE.is_paused():
        STATE.daily_loss_paused = False   # ปลด latch พร้อม circuit breaker
        STATE.set_pause(False)

    # ── [4][4.5][4.7][5] Per-symbol ──────────────────────────
    for sym in CFG['symbols']['active']:
        run_tick(sym)

    # บันทึก account snapshot
    try:
        acc = STATE.client.get_account()
        STATE.writer.write_account(acc)
    except Exception as e:
        log.warning(f"Account write error: {e}")

    # ── Periodic stats (ทุก 10 tick) ─────────────────────────
    if STATE.tick_count % 10 == 0:
        try:
            conn = STATE.client.get_connection_stats()
            log.info(
                f"[conn stats @ tick {STATE.tick_count}] "
                f"MT5={'OK' if conn['connected'] else 'DISCONNECTED'} "
                f"reconnects={conn['total_reconnects']} "
                f"success_rate={conn['reconnect_success_rate']}%"
            )
        except Exception:
            pass


def _daily_summary():
    """ส่ง daily summary ทุกคืน"""
    try:
        acc     = STATE.client.get_account()
        summary = STATE.writer.get_daily_summary()
        conn    = STATE.client.get_connection_stats()

        notify(
            f"📊 *Daily Summary*\n"
            f"Date:    {_now_str()[:10]}\n"
            f"Balance: ${acc['balance']:,.2f}\n"
            f"P&L:     ${acc['profit']:+.2f}\n"
            f"Trades:  {summary.get('trades',0)}\n"
            f"Win Rate:{summary.get('win_rate',0):.1%}\n"
            f"Ticks:   {STATE.tick_count}\n"
            f"MT5 Reconnects: {conn['total_reconnects']} "
            f"(success {conn['reconnect_success_rate']}%)"
        )

        STATE.risk.reset_daily()
        STATE.daily_loss_paused = False  # ✅ FIX: ปลด latch พร้อม daily reset
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
        project_root = Path(__file__).parent.parent
        yaml_path    = project_root / "config.yaml"
        new_mtime    = yaml_path.stat().st_mtime

        if new_mtime != STATE.config_mtime:
            CFG = reload_config()
            STATE.config_mtime = new_mtime
            STATE.symbols   = CFG['symbols']['active']
            STATE.timeframe = CFG['symbols']['primary_timeframe']
            log.info(
                f"🔄 config.yaml reloaded — "
                f"symbols={STATE.symbols} tf={STATE.timeframe}"
            )
    except Exception as e:
        log.warning(f"Config reload error: {e}")


# ══════════════════════════════════════════════════════════════
# Graceful Shutdown
# ══════════════════════════════════════════════════════════════
def _handle_shutdown(signum, frame):
    """จัดการ shutdown อย่างสะอาด"""
    log.info("🛑 Shutdown signal received...")
    STATE.running = False

    try:
        if STATE.client:
            STATE.client.disconnect()

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
    """Entry point — เรียกจาก NSSM service หรือ run_bot.bat"""
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT,  _handle_shutdown)

    try:
        startup()
    except Exception as e:
        log.critical(f"Startup failed: {e}", exc_info=True)
        sys.exit(1)

    interval = CFG['symbols']['primary_timeframe']
    minutes  = int(interval.replace('M','').replace('H','')) \
                if 'M' in interval else \
                int(interval.replace('H','')) * 60

    schedule.every(minutes).minutes.do(run_all_symbols)

    summary_hour = CFG['notifications']['daily_summary_hour']
    schedule.every().day.at(f"{summary_hour:02d}:00").do(_daily_summary)
    schedule.every().sunday.at("02:00").do(_weekly_retrain)

    # ── Session reset 3x/day — clear per-symbol loss counters ─
    # default: 6:00 (London open), 13:00 (NY open), 21:00 (Asia open)
    for h in _SESSION_RESET_HOURS:
        schedule.every().day.at(f"{h:02d}:00").do(_do_session_reset)
    log.info(
        f"📅 Session resets scheduled at: "
        f"{[f'{h:02d}:00' for h in _SESSION_RESET_HOURS]} UTC"
    )

    log.info("▶ Running initial tick...")
    run_all_symbols()

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
            log.error(f"Main loop error #{consecutive_errors}: {e}", exc_info=True)

            if consecutive_errors >= 10:
                log.critical("Too many consecutive errors — requesting restart")
                notify(
                    f"🚨 *Bot Critical Error*\n"
                    f"10 consecutive errors\n"
                    f"Restarting service...\n"
                    f"Last error: {str(e)[:200]}"
                )
                sys.exit(1)

            time.sleep(30)

# ── Helpers ────────────────────────────────────────────────────
def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

if __name__ == "__main__":
    main()