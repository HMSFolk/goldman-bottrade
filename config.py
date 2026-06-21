# ============================================================
# config.py  —  Production-ready Config Loader
# โหลด config.yaml + .env แล้ว merge เข้าด้วยกัน
# ใช้ cfg = get_config() ในทุกไฟล์
# ============================================================

import os
import copy
import logging
import yaml
from functools import lru_cache
from pathlib import Path
from typing import Any

# โหลด .env ก่อนอื่นเลย (ก่อน import อื่นๆ ใช้ค่าจาก .env)
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    load_dotenv(dotenv_path=_env_path, override=True)
except ImportError:
    # ถ้าไม่มี python-dotenv ให้เตือน แต่ไม่ crash
    print("[CONFIG WARNING] python-dotenv not installed. Run: pip install python-dotenv")

logger = logging.getLogger(__name__)

# ── Validation Schema ────────────────────────────────────────
# (key_path, type, required)
_REQUIRED_ENV = [
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "MT5_PATH",
    "TELEGRAM_TOKEN",
    "TELEGRAM_CHAT_ID",
]

_REQUIRED_YAML_KEYS = [
    ("mt5",),
    ("symbols", "active"),
    ("risk", "risk_per_trade"),
    # ✅ CONSOLIDATED: risk.max_daily_loss_pct ถูกลบออกจาก config.yaml
    # daily-loss limit เหลือจุดเดียวคือ circuit_breaker.daily.loss_pct
    ("circuit_breaker", "daily", "loss_pct"),
    ("order", "magic_number"),
    ("signal", "active_model"),
    ("paths", "models_saved"),
    ("paths", "db"),
]


def _validate_env() -> list[str]:
    """ตรวจว่า .env มีครบไหม — return list of missing keys"""
    missing = [k for k in _REQUIRED_ENV if not os.getenv(k)]
    return missing


def _validate_yaml(cfg: dict) -> list[str]:
    """ตรวจว่า config.yaml มี key ครบไหม"""
    errors = []
    for key_path in _REQUIRED_YAML_KEYS:
        node = cfg
        for k in key_path:
            if not isinstance(node, dict) or k not in node:
                errors.append(".".join(key_path))
                break
            node = node[k]
    return errors


def _get_nested(d: dict, *keys: str, default: Any = None) -> Any:
    """ดึงค่าจาก nested dict อย่างปลอดภัย"""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


def _merge_env_into_config(cfg: dict) -> dict:
    """
    Inject ค่าจาก .env เข้า config dict
    ทำให้ทุกไฟล์ในโปรเจกต์ใช้ cfg เดียวโดยไม่ต้องอ่าน os.getenv เอง
    """
    # MT5 credentials
    cfg.setdefault("mt5", {})
    cfg["mt5"]["login"]    = int(os.getenv("MT5_LOGIN", 0))
    cfg["mt5"]["password"] = os.getenv("MT5_PASSWORD", "")
    cfg["mt5"]["server"]   = os.getenv("MT5_SERVER", "")

    # MT5_PATH จาก .env override terminal_path ใน yaml
    mt5_path = os.getenv("MT5_PATH", "")
    if mt5_path:
        cfg["mt5"]["terminal_path"] = mt5_path

    # Telegram credentials
    cfg.setdefault("notifications", {})
    cfg["notifications"]["telegram_token"]   = os.getenv("TELEGRAM_TOKEN", "")
    cfg["notifications"]["telegram_chat_id"] = os.getenv("TELEGRAM_CHAT_ID", "")

    # Optional overrides จาก .env (ถ้ามีก็ใช้)
    if os.getenv("BOT_ENV"):
        cfg["env"] = os.getenv("BOT_ENV", "production")  # production | staging | dev

    return cfg


@lru_cache(maxsize=1)
def _load_raw_config() -> dict:
    """โหลด config.yaml (cached) — internal use only"""
    path = Path(__file__).parent / "config.yaml"

    if not path.exists():
        raise FileNotFoundError(
            f"[CONFIG ERROR] ไม่พบ config.yaml ที่ {path}\n"
            "กรุณา copy config.yaml.example → config.yaml แล้วปรับค่า"
        )

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError("[CONFIG ERROR] config.yaml ต้องเป็น YAML dict (mapping)")

    return data


def get_config() -> dict:
    """
    โหลด config พร้อม .env — ใช้ทั่วทั้งโปรเจกต์
    Return deep copy เพื่อป้องกัน mutation ของ cache

    Usage:
        from config import get_config
        cfg = get_config()
        login = cfg["mt5"]["login"]
    """
    raw = _load_raw_config()

    # deep copy ก่อน merge เพื่อไม่ให้ mutation กระทบ cache
    cfg = copy.deepcopy(raw)

    # inject .env values
    cfg = _merge_env_into_config(cfg)

    return cfg


def reload_config() -> dict:
    """
    Force reload config.yaml + .env (ใช้หลัง hot-reload หรือ restart)
    Usage: from config import reload_config; reload_config()
    """
    _load_raw_config.cache_clear()
    logger.info("[CONFIG] Config reloaded")
    return get_config()


# ════════════════════════════════════════════════════════════
# Broker-Agnostic Symbol Resolver
# ════════════════════════════════════════════════════════════
# ทั้งระบบ (config, model, database, log) ใช้ชื่อ "กลาง" เดียวกัน
# เช่น "XAUUSD" ไม่ว่าจะเทรดกับ broker ไหน
#
# มีแค่ 2 ไฟล์ที่ต้องรู้จักชื่อจริงของ broker:
#   - data/collect_mt5.py  (ดึงข้อมูล)
#   - bot/mt5_client.py    (ราคา/position/order จริง)
#
# ย้าย broker ใหม่ → แก้ config.yaml ที่เดียว (broker.name + symbol_map)
# ════════════════════════════════════════════════════════════

def resolve_symbol(canonical: str, cfg: dict = None) -> str:
    """
    แปลงชื่อกลาง → ชื่อจริงของ broker ปัจจุบัน

    Example (broker=exness):
        resolve_symbol("XAUUSD") → "XAUUSDm"
    Example (broker=icmarkets):
        resolve_symbol("XAUUSD") → "XAUUSD"  (ไม่มี m)

    ถ้าไม่เจอใน symbol_map → คืนชื่อเดิม (fail-safe เผื่อ symbol
    ที่ไม่ได้อยู่ใน 3 คู่หลัก เช่น USDJPY ที่ยังไม่ได้เพิ่ม mapping)
    """
    cfg     = cfg or get_config()
    broker  = cfg.get('broker', {}).get('name', 'exness')
    mapping = cfg.get('symbol_map', {}).get(canonical, {})

    broker_symbol = mapping.get(broker)
    if not broker_symbol:
        logger.warning(
            f"[resolve_symbol] '{canonical}' ไม่มี mapping สำหรับ "
            f"broker='{broker}' — ใช้ชื่อเดิม"
        )
        return canonical
    return broker_symbol


def unresolve_symbol(broker_symbol: str, cfg: dict = None) -> str:
    """
    แปลงชื่อจริงของ broker → ชื่อกลาง (reverse lookup)
    ใช้ตอนรับ event จาก MT5 (เช่น position.symbol == "XAUUSDm")
    แล้วต้องการ map กลับเป็น "XAUUSD" เพื่อ lookup config/model

    Example (broker=exness):
        unresolve_symbol("XAUUSDm") → "XAUUSD"
    """
    cfg    = cfg or get_config()
    broker = cfg.get('broker', {}).get('name', 'exness')

    for canon, brokers in cfg.get('symbol_map', {}).items():
        if brokers.get(broker) == broker_symbol:
            return canon
    return broker_symbol   # ไม่เจอ → คืนชื่อเดิม


def all_broker_symbols(cfg: dict = None) -> list:
    """
    คืนรายชื่อ broker symbols ทั้งหมดที่ active
    ใช้ตอน MT5 connect เพื่อ symbol_select() ทุกตัว
    Example: ["XAUUSDm", "EURUSDm", "GBPUSDm"]
    """
    cfg    = cfg or get_config()
    active = cfg.get('symbols', {}).get('active', [])
    return [resolve_symbol(s, cfg) for s in active]


def validate_config(raise_on_error: bool = True) -> bool:
    """
    ตรวจ config ครบก่อน bot start — เรียกใน main.py
    Args:
        raise_on_error: ถ้า True จะ raise RuntimeError ถ้า config ผิด
    Returns:
        True ถ้าผ่านทุก check
    """
    errors = []

    # 1. ตรวจ .env
    missing_env = _validate_env()
    if missing_env:
        errors.append(f"[.env] Missing keys: {', '.join(missing_env)}")

    # 2. ตรวจ yaml keys
    try:
        cfg = get_config()
        missing_yaml = _validate_yaml(cfg)
        if missing_yaml:
            errors.append(f"[config.yaml] Missing keys: {', '.join(missing_yaml)}")
    except Exception as e:
        errors.append(str(e))
        cfg = {}

    # 3. ตรวจค่า logic
    if cfg:
        risk_per_trade = _get_nested(cfg, "risk", "risk_per_trade", default=0)
        if not (0 < risk_per_trade <= 0.05):
            errors.append(
                f"[risk.risk_per_trade] ค่า {risk_per_trade} ผิดปกติ "
                "(แนะนำ 0.005–0.02 สำหรับ production)"
            )

        # ✅ CONSOLIDATED: risk.max_daily_loss_pct ถูกลบออก — daily-loss
        # limit อ่านจาก circuit_breaker.daily.loss_pct แทน
        # ⚠️ หน่วยต่างกัน! risk.max_daily_loss_pct เดิมเป็น fraction (0.05 = 5%)
        # ส่วน circuit_breaker.daily.loss_pct เป็น percent เต็ม (5.0 = 5%)
        # ถ้าใช้ range เดิม (0–0.20) เทียบกับค่าใหม่ (5.0) จะ false-positive
        # ทุกครั้ง ต้องปรับ range ให้เป็นหน่วย percent ด้วย (3–10 ไม่ใช่ 0.03–0.10)
        max_dd = _get_nested(cfg, "circuit_breaker", "daily", "loss_pct", default=0)
        if not (0 < max_dd <= 20):
            errors.append(
                f"[circuit_breaker.daily.loss_pct] ค่า {max_dd} ผิดปกติ "
                "(แนะนำ 3–10 หน่วย %, ไม่ใช่ fraction)"
            )

        active_model = _get_nested(cfg, "signal", "active_model", default="")
        valid_models = {"rule_based", "xgb", "lgbm", "lstm", "ensemble"}
        if active_model not in valid_models:
            errors.append(
                f"[signal.active_model] '{active_model}' ไม่ถูกต้อง "
                f"(ต้องเป็นหนึ่งใน {valid_models})"
            )

    # 4. สรุปผล
    if errors:
        error_msg = "\n".join(f"  ✗ {e}" for e in errors)
        full_msg = f"\n[CONFIG VALIDATION FAILED]\n{error_msg}\n"
        logger.error(full_msg)
        if raise_on_error:
            raise RuntimeError(full_msg)
        return False

    logger.info("[CONFIG] Validation passed ✓")
    return True

# ── Module-level shortcut ────────────────────────────────────
# ใช้ cfg["mt5"]["login"] ได้เลยในทุกไฟล์
# แต่ถ้า import ตอนที่ .env/yaml ยังไม่มี จะ raise error ชัดเจน
try:
    cfg = get_config()
except Exception as e:
    # ไม่ crash ทั้งโปรเจกต์ตอน import — log แล้วให้ main.py จัดการ
    import sys
    print(f"[CONFIG ERROR] {e}", file=sys.stderr)
    cfg = {}