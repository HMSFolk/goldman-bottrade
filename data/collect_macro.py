# data/collect_macro.py
import yfinance as yf
import pandas as pd
import logging
from pathlib import Path

log = logging.getLogger("data")

def collect_macro_data(sources: dict) -> dict:
    """
    ดึง macro data ผ่าน yfinance
    sources = {"DXY": "DX-Y.NYB", "VIX": "^VIX", ...}
    """
    collected = {}
    Path("data/raw").mkdir(exist_ok=True)

    for name, ticker in sources.items():
        try:
            df = yf.download(
                ticker,
                period   = "2y",
                interval = "1d",
                progress = False,
                auto_adjust = True,
            )

            if df.empty:
                log.warning(f"Macro {name} ({ticker}) ว่างเปล่า")
                continue

            # เก็บแค่ Close และ Volume
            df = df[['Close', 'Volume']].copy()
            df.columns = ['close', 'volume']
            df.index = pd.to_datetime(df.index, utc=True)

            out = f"data/raw/macro_{name}.parquet"
            df.to_parquet(out)
            collected[name] = df
            log.info(f"  Macro {name}: {len(df)} วัน → {out}")

        except Exception as e:
            log.error(f"Macro {name} ล้มเหลว: {e}")

    return collected