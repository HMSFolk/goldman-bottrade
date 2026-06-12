# data/collect_macro.py
"""
ดึง Macro data ผ่าน yfinance
DXY, VIX, US10Y, Silver, Oil → data/raw/macro_*.parquet
"""
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
import pandas as pd
import yfinance as yf

from config import get_config
_CFG = get_config()

log = logging.getLogger("data")


def collect_macro_data(sources: dict) -> dict:
    """
    ดึง macro data ผ่าน yfinance
    sources = {"DXY": "DX-Y.NYB", "VIX": "^VIX", ...}
    คืน dict: {"DXY": DataFrame, ...}
    """
    # ✅ FIX BUG-2: absolute path จาก config
    raw_dir = _ROOT / _CFG['paths']['data_raw']
    raw_dir.mkdir(parents=True, exist_ok=True)

    # ดึงย้อนหลังตาม config (default 2 ปี)
    history_days = _CFG.get('data', {}).get('macro_history_days', 730)
    period       = f"{history_days}d"

    collected = {}

    for name, ticker in sources.items():
        try:
            df = yf.download(
                ticker,
                period      = period,
                interval    = "1d",
                progress    = False,
                auto_adjust = True,
            )

            if df is None or df.empty:
                log.warning(f"Macro {name} ({ticker}) ว่างเปล่า")
                continue

            # ✅ FIX BUG-3: yfinance 0.2+ อาจคืน MultiIndex columns
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # ✅ FIX BUG-4: บาง symbol ไม่มี Volume (DXY, US10Y) — รับแค่ Close
            available_cols = [c for c in df.columns
                              if c.lower() in ('close', 'volume')]
            if 'Close' not in df.columns and 'close' not in df.columns:
                log.warning(f"Macro {name}: ไม่มีคอลัมน์ Close")
                continue

            # รวม close เสมอ, volume ถ้ามี
            cols = {'close': df.get('Close', df.get('close'))}
            if 'Volume' in df.columns:
                vol = df['Volume']
                # ป้องกัน volume = NaN ทั้งหมด (เช่น Index)
                if vol.notna().any() and vol.sum() > 0:
                    cols['volume'] = vol

            result_df = pd.DataFrame(cols)
            result_df.index = pd.to_datetime(df.index, utc=True)
            result_df.index.name = 'datetime'

            # ลบแถวที่ close เป็น NaN
            result_df = result_df.dropna(subset=['close'])

            if result_df.empty:
                log.warning(f"Macro {name}: ข้อมูลทั้งหมดเป็น NaN หลัง dropna")
                continue

            # ✅ FIX BUG-2: absolute path
            out = raw_dir / f"macro_{name}.parquet"
            result_df.to_parquet(out)
            collected[name] = result_df
            log.info(
                f"  Macro {name} ({ticker}): "
                f"{len(result_df)} วัน → {out}"
            )

        except Exception as e:
            log.error(f"Macro {name} ({ticker}) ล้มเหลว: {e}", exc_info=True)
            # ไม่หยุด pipeline — ข้ามไปตัวถัดไป

    log.info(
        f"Macro collection: {len(collected)}/{len(sources)} สำเร็จ "
        f"({list(collected.keys())})"
    )
    return collected