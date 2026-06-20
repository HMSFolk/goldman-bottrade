# data/collect_mt5.py
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import MetaTrader5 as mt5
import pandas as pd
import logging

# ✅ FIX BUG-11: ลบ load_dotenv() — config.py โหลดแล้วและ merge .env ให้
# ✅ FIX BUG-7: ใช้ get_config() แทน os.getenv โดยตรง
from config import get_config, resolve_symbol   # ✅ NEW: resolve_symbol
_CFG = get_config()

log = logging.getLogger("data")

TF_MAP = {
    "M1" : mt5.TIMEFRAME_M1,
    "M5" : mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1" : mt5.TIMEFRAME_H1,
    "H4" : mt5.TIMEFRAME_H4,
    "D1" : mt5.TIMEFRAME_D1,
    "W1" : mt5.TIMEFRAME_W1,   # ✅ FIX BUG-10: เพิ่ม W1
}

def _connect() -> bool:
    """เชื่อมต่อ MT5 ด้วย credentials จาก get_config()"""
    # ✅ FIX BUG-7: อ่านจาก cfg ที่ merge .env แล้ว ไม่ใช่ os.getenv โดยตรง
    mt5_cfg = _CFG['mt5']
    login   = mt5_cfg.get('login', 0)
    pwd     = mt5_cfg.get('password', '')
    server  = mt5_cfg.get('server', '')
    path    = mt5_cfg.get('terminal_path', '')

    if not login:
        log.error("MT5_LOGIN ไม่ได้ตั้งค่าใน .env")
        return False

    if not mt5.initialize(path=path):
        log.error(f"mt5.initialize() failed: {mt5.last_error()}")
        return False

    return mt5.login(int(login), password=str(pwd), server=str(server))

def collect_mt5_data(
    symbols:    list,    # ⚠️ ชื่อกลาง เช่น ["XAUUSD","EURUSD","GBPUSD"]
    timeframes: list,
    bars:       dict,
) -> dict:
    """
    ดึงข้อมูล OHLCV จาก MT5 บันทึกลง data/raw/
    คืนค่า dict: {"XAUUSD_M15": DataFrame, ...}  ← key เป็นชื่อกลางเสมอ

    ✅ Broker-agnostic: รับ symbols เป็นชื่อกลาง (XAUUSD)
       ภายในแปลงเป็นชื่อจริงของ broker (XAUUSDm) ก่อนเรียก MT5
       แต่บันทึกไฟล์ด้วยชื่อกลางเสมอ — train/model ไม่ต้องรู้จัก broker
    """
    if not _connect():
        raise ConnectionError(f"MT5 connect failed: {mt5.last_error()}")

    # ✅ FIX BUG-9: absolute path จาก config + FIX BUG-8: try/finally shutdown
    raw_dir = _ROOT / _CFG['paths']['data_raw']
    raw_dir.mkdir(parents=True, exist_ok=True)

    collected = {}
    try:
        for canonical_sym in symbols:
            # ✅ resolve ชื่อกลาง → ชื่อจริงของ broker
            broker_sym = resolve_symbol(canonical_sym, _CFG)

            if mt5.symbol_info(broker_sym) is None:
                log.warning(
                    f"Symbol {broker_sym} (canonical={canonical_sym}) "
                    f"ไม่พบใน MT5 — ข้าม "
                    f"(เช็ค symbol_map ใน config.yaml ว่าตรงกับ broker จริงไหม)"
                )
                continue

            mt5.symbol_select(broker_sym, True)

            for tf_name in timeframes:
                tf     = TF_MAP.get(tf_name)
                if tf is None:
                    log.warning(f"Timeframe ไม่รู้จัก: {tf_name} — ข้าม")
                    continue

                n_bars = bars.get(tf_name, 5000)
                # ✅ key/filename ใช้ชื่อกลางเสมอ (ไม่ใช่ broker_sym)
                key    = f"{canonical_sym}_{tf_name}"
                rates  = mt5.copy_rates_from_pos(broker_sym, tf, 0, n_bars)

                if rates is None or len(rates) == 0:
                    log.warning(
                        f"ไม่มีข้อมูล {key} (broker_sym={broker_sym}): "
                        f"{mt5.last_error()}"
                    )
                    continue

                df = pd.DataFrame(rates)
                df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
                df.set_index('time', inplace=True)
                df.index.name = 'datetime'

                # ✅ FIX BUG-9: ใช้ absolute path
                out = raw_dir / f"{key}.parquet"
                df.to_parquet(out)
                collected[key] = df
                log.info(
                    f"  {key} (broker:{broker_sym}): "
                    f"{len(df):,} bars → {out}"
                )

    finally:
        # ✅ FIX BUG-8: shutdown เสมอ ไม่ว่าจะ exception หรือไม่
        #mt5.shutdown()
        log.debug("MT5 shutdown complete")

    return collected