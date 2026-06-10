# data/collect_mt5.py
import MetaTrader5 as mt5
import pandas as pd
import logging
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()
log = logging.getLogger("data")

TF_MAP = {
    "M1" : mt5.TIMEFRAME_M1,
    "M5" : mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1" : mt5.TIMEFRAME_H1,
    "H4" : mt5.TIMEFRAME_H4,
    "D1" : mt5.TIMEFRAME_D1,
}

def _connect() -> bool:
    if not mt5.initialize(
        path=os.getenv("MT5_PATH",
             r"C:\Program Files\MetaTrader 5\terminal64.exe")
    ):
        return False
    return mt5.login(
        int(os.getenv("MT5_LOGIN")),
        password=os.getenv("MT5_PASSWORD"),
        server=os.getenv("MT5_SERVER"),
    )

def collect_mt5_data(
    symbols:    list,
    timeframes: list,
    bars:       dict,
) -> dict:
    """
    ดึงข้อมูล OHLCV จาก MT5 บันทึกลง data/raw/
    คืนค่า dict: {"XAUUSD_M15": DataFrame, ...}
    """
    if not _connect():
        raise ConnectionError(f"MT5 connect failed: {mt5.last_error()}")

    collected = {}
    Path("data/raw").mkdir(exist_ok=True)

    for sym in symbols:
        # ตรวจว่า symbol มีใน MT5
        if mt5.symbol_info(sym) is None:
            log.warning(f"Symbol {sym} ไม่พบใน MT5 — ข้าม")
            continue

        # เปิด symbol (บาง broker ต้องทำก่อน)
        mt5.symbol_select(sym, True)

        for tf_name in timeframes:
            tf     = TF_MAP.get(tf_name)
            n_bars = bars.get(tf_name, 5000)
            key    = f"{sym}_{tf_name}"

            rates  = mt5.copy_rates_from_pos(sym, tf, 0, n_bars)

            if rates is None or len(rates) == 0:
                log.warning(f"ไม่มีข้อมูล {key}: {mt5.last_error()}")
                continue

            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
            df.set_index('time', inplace=True)
            df.index.name = 'datetime'

            # บันทึก
            out = f"data/raw/{key}.parquet"
            df.to_parquet(out)
            collected[key] = df
            log.info(f"  {key}: {len(df):,} bars → {out}")

    mt5.shutdown()
    return collected