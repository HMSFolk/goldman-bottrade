# features/pipeline.py
"""
Feature Engineering Pipeline
รับข้อมูลดิบ → เพิ่ม features ทุกกลุ่ม → บันทึก processed data
รันด้วย: python features/pipeline.py
"""
# features/pipeline.py
"""
Feature Engineering Pipeline
รับข้อมูลดิบ → เพิ่ม features ทุกกลุ่ม → บันทึก processed data

รันด้วย: python features/pipeline.py
หรือเรียกจาก scripts/retrain_all.py อัตโนมัติ
"""

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ✅ FIX BUG-2: setup_logging ก่อน import อื่น
from bot.setup_logging import setup_logging
setup_logging()

import logging
import time
import json
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ✅ FIX BUG-2: ใช้ get_config() แทน yaml.safe_load
from config import get_config
CFG = get_config()

log = logging.getLogger("features")

# Import feature modules
from features.trend        import add_trend_features
from features.momentum     import add_momentum_features
from features.volatility   import add_volatility_features
from features.price_action import add_price_action_features
from features.mtf_and_label import add_mtf_features, add_target_label


# ── Constants ─────────────────────────────────────────────────
FORWARD_BARS  = CFG['training']['forward_bars']       # 4
MIN_RETURN    = CFG['training']['min_return_pct']     # 0.0015
RAW_DIR       = _ROOT / CFG['paths']['data_raw']
print(f"[DEBUG] โฟลเดอร์ RAW อยู่ที่: {RAW_DIR}")

PROCESSED_DIR = _ROOT / CFG['paths']['data_processed']

# columns ที่ไม่ใช่ feature (ไม่ส่งให้โมเดล)
NON_FEATURE_COLS = {
    'open', 'high', 'low', 'close',
    'tick_volume', 'real_volume', 'spread',
    'label', 'future_return',
}

# ── Categorical Encoder ────────────────────────────────────────
# columns เหล่านี้ถูกสร้างเป็น string → ต้อง encode เป็น int ก่อนส่ง model
_CAT_COLS = [
    'trend_cat',
    'rsi_zone',
    'vol_regime',
    'nearest_pivot_level',
    'price_zone_20',
]

def _encode_categoricals(df: pd.DataFrame) -> pd.DataFrame:
    """
    สร้าง *_enc columns จาก categorical string columns
    เรียกใน build_features() และ build_features_live()
    เพื่อให้ train และ predict ใช้ features เดียวกันเสมอ
    """
    from sklearn.preprocessing import LabelEncoder
    import numpy as np

    for col in _CAT_COLS:
        enc_col = f"{col}_enc"
        if col in df.columns and enc_col not in df.columns:
            le = LabelEncoder()
            df[enc_col] = le.fit_transform(
                df[col].fillna('UNKNOWN').astype(str)
            ).astype(np.int32)
    return df


# ── Loader ────────────────────────────────────────────────────
def load_raw(symbol: str, timeframe: str) -> pd.DataFrame:
    """
    โหลดข้อมูลดิบจาก data/raw/
    ตรวจสอบว่าไฟล์มีอยู่และไม่ว่างเปล่า
    """
    path = RAW_DIR / f"{symbol}_{timeframe}.parquet"

    if not path.exists():
        raise FileNotFoundError(
            f"ไม่พบ {path}\n"
            f"รัน: python data/pipeline.py ก่อน"
        )

    df = pd.read_parquet(path)

    if df.empty:
        raise ValueError(f"ข้อมูล {path} ว่างเปล่า")

    # ตรวจ columns ที่จำเป็น
    required = {'open', 'high', 'low', 'close'}
    missing  = required - set(df.columns)
    if missing:
        raise ValueError(f"ขาด columns: {missing}")

    log.debug(f"Loaded {symbol}_{timeframe}: {len(df):,} rows")
    return df


# ── Quality Check ──────────────────────────────────────────────
def check_data_quality(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """
    ตรวจคุณภาพข้อมูลก่อน feature engineering
    แก้ปัญหาที่พบบ่อย: NaN, duplicates, timezone
    """
    n_before = len(df)

    # 1. ลบ duplicate index
    df = df[~df.index.duplicated(keep='last')]

    # 2. เรียงตามเวลา (สำคัญมาก — ป้องกัน look-ahead)
    df.sort_index(inplace=True)

    # 3. ลบ rows ที่ราคาเป็น 0 หรือ NaN (ข้อมูลเสีย)
    df = df[(df['close'] > 0) & df['close'].notna()]

    # 4. ลบช่วงหยุด (spread = 0 มักเป็น weekend/holiday)
    if 'spread' in df.columns:
        weekend_mask = df['spread'] == 0
        if weekend_mask.sum() > 0:
            df = df[~weekend_mask]

    n_after = len(df)
    if n_before != n_after:
        log.info(f"  {name}: ลบ {n_before - n_after} rows เสีย "
                 f"({n_before:,} → {n_after:,})")

    return df


# ── Core Build Function ────────────────────────────────────────
def build_features(
    symbol:     str,
    timeframe:  str = "M15",
    for_live:   bool = False,   # True = ไม่สร้าง label (ใช้ใน bot จริง)
) -> pd.DataFrame:
    """
    สร้าง features ครบทุกกลุ่มสำหรับ 1 symbol + timeframe

    for_live=False → สร้าง label ด้วย (สำหรับ train)
    for_live=True  → ไม่สร้าง label  (สำหรับ predict real-time)
    """
    t0 = time.time()
    log.info(f"Building features: {symbol}_{timeframe}...")

    # ── Load ──────────────────────────────────────────────────
    df     = load_raw(symbol, timeframe)
    print(f"[DEBUG] โหลดไฟล์สำเร็จ! จำนวนแถวคือ: {len(df)}")
    if len(df) == 0:
        print("[DEBUG] ข้อมูลว่างเปล่า! ตรวจสอบไฟล์ใน data/raw อีกที")

    df     = check_data_quality(df, f"{symbol}_{timeframe}")
    n_raw  = len(df)

    # ── Load HTF สำหรับ multi-timeframe ───────────────────────
    htf_dfs = {}
    for tf in CFG['symbols']['htf_timeframes']:   # H1, H4
        try:
            htf_dfs[tf] = check_data_quality(
                load_raw(symbol, tf), f"{symbol}_{tf}"
            )
        except FileNotFoundError:
            log.warning(f"  ไม่มี {symbol}_{tf} — ข้าม HTF features")

    # ── Load Macro (join กับ price data) ──────────────────────
    macro_dfs = {}
    for name in CFG['data']['macro_symbols']:
        try:
            macro_dfs[name] = pd.read_parquet(
                RAW_DIR / f"macro_{name}.parquet"
            )
        except FileNotFoundError:
            pass   # macro optional

    # ── Feature Groups ────────────────────────────────────────
    steps = [
        ("Trend (EMA/MACD/ADX/Ichimoku)",  add_trend_features),
        ("Momentum (RSI/Stoch/CCI)",        add_momentum_features),
        ("Volatility (ATR/BB/Keltner)",     add_volatility_features),
        ("Price Action (candle/pivot/S&R)", add_price_action_features),
    ]

    for step_name, fn in steps:
        try:
            
            if df is None:
                raise ValueError(f"df เป็น None ก่อนเข้า {step_name}")
            df = fn(df)
            if df is None:
                raise ValueError(f"ฟังก์ชัน {step_name} คืนค่ากลับมาเป็น None")
        except Exception as e:
            log.error(f"  ❌ {step_name}: {e}", exc_info=True)
            raise

    # ── Multi-timeframe features ──────────────────────────────
    if htf_dfs:
        try:
            df = add_mtf_features(df, htf_dfs, macro_dfs)
        except Exception as e:
            log.warning(f"  MTF features ข้ามไป: {e}")

    # ── Label Encode categoricals ──────────────────────────────
    # ✅ แก้ feature mismatch: สร้าง *_enc columns ให้ตรงกับตอน train เสมอ
    df = _encode_categoricals(df)

    # ── Target Label (เฉพาะตอน train) ───────────────────────
    if not for_live:
        # ✅ FIX BUG-4: ส่ง label_method จาก config (triple_barrier) ไม่ใช่ default "simple"
        label_method = CFG.get('training', {}).get('label_method', 'triple_barrier')
        df = add_target_label(
            df,
            forward_bars = FORWARD_BARS,
            min_return_pct = MIN_RETURN,
            label_method = label_method,
        )

    # ── Cleanup ───────────────────────────────────────────────
    # ลบ NaN ที่เกิดจาก rolling window ช่วงต้น
    n_before = len(df)
    df.dropna(subset=_get_key_features(df), inplace=True)
    n_after  = len(df)

    if n_before != n_after:
        log.info(f"  ลบ {n_before - n_after} rows จาก NaN "
                 f"(rolling warmup)")

    # ── Summary ───────────────────────────────────────────────
    n_features = len(_get_feature_cols(df))
    elapsed    = time.time() - t0

    log.info(
        f"  ✅ {symbol}_{timeframe}: "
        f"{n_raw:,} raw → {len(df):,} rows × {n_features} features "
        f"({elapsed:.1f}s)"
    )

    if not for_live:
        _log_label_distribution(df, symbol)

    return df


# ── Batch Build (ทุก symbol) ──────────────────────────────────
def build_all_features(
    symbols:   list = None,
    timeframe: str  = "M15",) -> dict:
    """
    Build features ทุก symbol แล้วบันทึกลง data/processed/
    คืนค่า dict: {"XAUUSD": DataFrame, "EURUSDm": DataFrame, ...}
    """
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    symbols = symbols or CFG['symbols']['active']
    print(f"[DEBUG] รายชื่อ Symbols ที่ต้องทำ: {symbols}")
    results = {}
    errors  = []

    log.info(f"Building features: {symbols}")
    log.info(f"Timeframe: {timeframe} | Forward bars: {FORWARD_BARS} | Min return: {MIN_RETURN}")

    for sym in symbols:
        sym = sym.strip()
        try:
            df = build_features(sym, timeframe, for_live=False)
            print(f"[DEBUG] build_features สำเร็จ! จำนวนแถวที่ได้: {len(df)}")

            # แก้จุดที่ 3: เพิ่มเงื่อนไขให้แน่ใจว่า df มีข้อมูลจริง
            if df.empty:
                print(f"❌ [WARN] {sym} ข้อมูลว่างเปล่า!")
                continue

            # บันทึก
            out = PROCESSED_DIR / f"{sym}_{timeframe}_features.parquet"
            df.to_parquet(out)
            log.info(f"  💾 บันทึก → {out}")

            results[sym] = df

        except Exception as e:
            import traceback
            print(f"❌ [CRITICAL ERROR] เกิดข้อผิดพลาดตอนทำ {sym}: {str(e)}")
            log.error(f"❌ {sym} ล้มเหลว: {e}", exc_info=True)
            traceback.print_exc()
            errors.append(sym)

    # สรุป
    log.info(f"\n{'='*50}")
    log.info(f"Feature build เสร็จ: "
             f"{len(results)}/{len(symbols)} symbols สำเร็จ")
    if errors:
        log.warning(f"ล้มเหลว: {errors}")

    # บันทึก feature list (ใช้ตอน load model)
    if results:
        sample_df    = next(iter(results.values()))
        feature_cols = _get_feature_cols(sample_df)
        _save_feature_list(feature_cols, timeframe)

    return results


# ── Live Feature Builder (ใช้ใน bot/main.py) ─────────────────
def build_features_live(
    df_raw: pd.DataFrame,
    symbol: str,
    timeframe: str = "M15",
) -> pd.DataFrame:
    """
    สร้าง features สำหรับ predict real-time
    รับ DataFrame จาก mt5_client.get_ohlcv() โดยตรง
    ไม่บันทึกไฟล์ — คืนค่า DataFrame พร้อม predict ทันที
    """
    df = df_raw.copy()
    df = check_data_quality(df, f"live_{symbol}")

    # โหลด HTF จากไฟล์ที่มีอยู่
    htf_dfs = {}
    for tf in CFG['symbols']['htf_timeframes']:
        path = RAW_DIR / f"{symbol}_{tf}.parquet"
        if path.exists():
            htf_dfs[tf] = pd.read_parquet(path)

    # Build features (ไม่สร้าง label)
    df = add_trend_features(df)
    df = add_momentum_features(df)
    df = add_volatility_features(df)
    df = add_price_action_features(df)

    if htf_dfs:
        df = add_mtf_features(df, htf_dfs, {})

    # ── Label Encode categoricals ──────────────────────────────
    # ✅ ต้องเรียกในที่นี้ด้วย ไม่งั้น live predict ไม่มี _enc columns
    df = _encode_categoricals(df)

    return df


# ── Helpers ───────────────────────────────────────────────────
def _get_feature_cols(df: pd.DataFrame) -> list:
    """คืน list ของ feature columns (ไม่รวม raw OHLCV และ label)"""
    return [c for c in df.columns if c not in NON_FEATURE_COLS]


def _get_key_features(df: pd.DataFrame) -> list:
    """features สำคัญที่ต้องไม่มี NaN — ใช้กรอง warmup rows"""
    candidates = ['ema_50', 'rsi_14', 'atr_14', 'adx']
    return [c for c in candidates if c in df.columns]


def _log_label_distribution(df: pd.DataFrame, symbol: str):
    """log สัดส่วน BUY/HOLD/SELL — ควรใกล้เคียงกัน"""
    if 'label' not in df.columns:
        return
    dist = df['label'].value_counts(normalize=True).round(3)
    log.info(
        f"  Label distribution {symbol}: "
        f"BUY={dist.get(1,0):.1%} "
        f"HOLD={dist.get(0,0):.1%} "
        f"SELL={dist.get(-1,0):.1%}"
    )
    # แจ้งเตือนถ้า imbalanced มากเกินไป
    if dist.get(1, 0) < 0.15 or dist.get(-1, 0) < 0.15:
        log.warning(
            f"  ⚠️ Label imbalanced สูง — "
            f"ลองปรับ min_return_pct ใน config.yaml"
        )


def _save_feature_list(features: list, timeframe: str):
    """บันทึก feature list ไว้ให้ model โหลดตอน predict"""
    import json
    out  = PROCESSED_DIR / f"feature_list_{timeframe}.json"
    data = {
        "timeframe" : timeframe,
        "n_features": len(features),
        "features"  : features,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info(f"  💾 Feature list ({len(features)} features) → {out}")


def load_feature_list(timeframe: str = "M15") -> list:
    """โหลด feature list ที่บันทึกไว้ — ใช้ใน model predict"""
    import json
    path = PROCESSED_DIR / f"feature_list_{timeframe}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"ไม่พบ feature list\n"
            f"รัน: python features/pipeline.py ก่อน"
        )
    return json.loads(path.read_text())['features']


def load_processed(
    symbol: str,
    timeframe: str = "M15",
) -> pd.DataFrame:
    """โหลด processed data — ใช้ใน train_xgb.py"""
    path = PROCESSED_DIR / f"{symbol}_{timeframe}_features.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"ไม่พบ {path}\n"
            f"รัน: python features/pipeline.py ก่อน"
        )
    return pd.read_parquet(path)


# ── Entry Point ───────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Feature Engineering Pipeline"
    )
    parser.add_argument(
        "--symbols", nargs="+",
        help="ระบุ symbol เช่น XAUUSD EURUSDm (default: ทุกตัวใน config)"
    )
    parser.add_argument(
        "--timeframe", default="M15",
        help="timeframe (default: M15)"
    )
    args = parser.parse_args()

    results = build_all_features(
        symbols   = args.symbols,
        timeframe = args.timeframe,
    )

    # แสดงสรุป feature ที่สร้างได้
    if results:
        sample  = next(iter(results.values()))
        feats   = _get_feature_cols(sample)
        print(f"\n{'='*50}")
        print(f"Features ทั้งหมด ({len(feats)} ตัว):")
        for i, f in enumerate(feats, 1):
            print(f"  {i:3d}. {f}")