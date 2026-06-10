# features/mtf_label.py
"""
Multi-Timeframe Feature Join + Target Label Creation

งาน 1: add_mtf_features()
  รวม H1, H4 indicators เข้ากับ M15
  ให้บอทรู้ทิศทางใหญ่ขณะทำ signal M15

งาน 2: add_target_label()
  สร้าง label: BUY=1, HOLD=0, SELL=-1
  ระวัง look-ahead bias เด็ดขาด
"""

import pandas as pd
import numpy as np
import logging
from typing import Optional

log = logging.getLogger("features")


# ══════════════════════════════════════════════════════════════
# 1. Multi-Timeframe Feature Join
# ══════════════════════════════════════════════════════════════
def add_mtf_features(
    df_primary:  pd.DataFrame,
    htf_dfs:     dict,                    # {"H1": df_h1, "H4": df_h4}
    macro_dfs:   dict  = None,            # {"DXY": df_dxy, ...}
) -> pd.DataFrame:
    """
    รวม features จาก higher timeframe เข้ากับ primary (M15)

    หลักการ:
    - ใช้ .ffill() เติมค่าระหว่าง bar ของ HTF
    - ต้อง shift(1) ก่อน join เสมอ — ป้องกัน look-ahead
      (H1 bar ที่ปิด 14:00 ใช้ได้ตอน 14:15 เท่านั้น)
    """
    df = df_primary.copy()

    # ── Join Higher Timeframe ──────────────────────────────────
    for tf_name, df_htf in htf_dfs.items():
        if df_htf is None or df_htf.empty:
            log.warning(f"HTF {tf_name} ว่างเปล่า — ข้าม")
            continue

        prefix  = tf_name.lower()   # "h1", "h4"
        df_htf  = df_htf.copy()

        # เลือก columns ที่จะเอามาใช้จาก HTF
        htf_cols = _select_htf_columns(df_htf, prefix)

        if htf_cols.empty:
            log.warning(f"ไม่มี column ที่ต้องการจาก {tf_name}")
            continue

        # !! สำคัญมาก: shift(1) ก่อน join !!
        # bar H1 ที่ index 14:00 จะถูก align กับ M15 ที่ 14:15 เป็นต้นไป
        htf_cols = htf_cols.shift(1)

        # Join — reindex ตาม M15 แล้ว forward fill
        htf_reindexed = htf_cols.reindex(
            df.index,
            method='ffill',       # เติมค่าไปข้างหน้าจนถึง bar ถัดไปของ HTF
        )

        df = df.join(htf_reindexed, how='left')
        log.debug(f"Joined {tf_name}: {len(htf_cols.columns)} columns")

    # ── Join Macro Data ────────────────────────────────────────
    if macro_dfs:
        df = _join_macro(df, macro_dfs)

    # ── HTF Alignment Score ────────────────────────────────────
    df = _add_htf_alignment(df)

    return df


def _select_htf_columns(
    df_htf: pd.DataFrame,
    prefix: str,
) -> pd.DataFrame:
    """
    เลือก columns สำคัญจาก HTF — ไม่เอาทุกตัว
    เพราะจะ duplicate กับ M15 และเพิ่ม noise
    """
    selected = {}

    # Trend
    for col in ['ema_20','ema_50','ema_200',
                'ema_cross_9_20','ema_cross_20_50',
                'ema_alignment','macd_hist','macd_above_zero',
                'adx','adx_trending','adx_di_bull',
                'supertrend_dir','ichi_conv','ichi_base']:
        if col in df_htf.columns:
            selected[f'{prefix}_{col}'] = df_htf[col]

    # Momentum
    for col in ['rsi_14','rsi_zone_num',
                'stoch_k','stoch_cross',
                'momentum_score','momentum_confluence']:
        if col in df_htf.columns:
            selected[f'{prefix}_{col}'] = df_htf[col]

    # Volatility
    for col in ['atr_14','atr_pct','bb_pct',
                'squeeze_on','vol_regime_num']:
        if col in df_htf.columns:
            selected[f'{prefix}_{col}'] = df_htf[col]

    # Price Action
    for col in ['pa_score','bos_bull','bos_bear',
                'structure_score','price_pos_20']:
        if col in df_htf.columns:
            selected[f'{prefix}_{col}'] = df_htf[col]

    if not selected:
        return pd.DataFrame()

    return pd.DataFrame(selected, index=df_htf.index)


def _join_macro(
    df: pd.DataFrame,
    macro_dfs: dict,
) -> pd.DataFrame:
    """
    Join macro data (daily) เข้ากับ M15
    DXY, US10Y, VIX — reindex และ ffill
    """
    for name, df_macro in macro_dfs.items():
        if df_macro is None or df_macro.empty:
            continue

        # เอาแค่ close ของ macro
        col_name = f'macro_{name.lower()}'
        series   = df_macro['close'].copy()

        # shift 1 day — ค่า macro วันนี้ใช้ได้พรุ่งนี้
        series   = series.shift(1)

        # Reindex ตาม M15 และ ffill
        macro_reindexed = series.reindex(
            df.index, method='ffill'
        ).rename(col_name)

        df = df.join(macro_reindexed, how='left')

        # DXY relationship กับ Gold (inverse correlation)
        if name == 'DXY' and col_name in df.columns:
            df['macro_dxy_pct_5d'] = (
                df[col_name] / df[col_name].shift(5*96) - 1
            ) * 100   # % change ใน 5 วัน (M15: 1วัน=96bars)

        log.debug(f"Joined macro {name}")

    return df


def _add_htf_alignment(df: pd.DataFrame) -> pd.DataFrame:
    """
    คำนวณ alignment score — ทุก timeframe ตรงกันหรือเปล่า
    สัญญาณดีที่สุดเมื่อ M15, H1, H4 ชี้ทิศทางเดียวกัน
    """
    # ── Bullish Alignment ──────────────────────────────────────
    bull_checks = []

    # M15 trend bullish
    if 'ema_alignment' in df.columns:
        bull_checks.append(df['ema_alignment'] >= 3)

    # H1 trend bullish
    if 'h1_ema_alignment' in df.columns:
        bull_checks.append(df['h1_ema_alignment'] >= 3)
    if 'h1_adx_di_bull' in df.columns:
        bull_checks.append(df['h1_adx_di_bull'] == 1)

    # H4 trend bullish
    if 'h4_ema_alignment' in df.columns:
        bull_checks.append(df['h4_ema_alignment'] >= 3)
    if 'h4_supertrend_dir' in df.columns:
        bull_checks.append(df['h4_supertrend_dir'] == 1)

    if bull_checks:
        df['htf_bull_count'] = sum(
            c.astype(int) for c in bull_checks
        )
        df['htf_fully_aligned_bull'] = (
            df['htf_bull_count'] >= len(bull_checks)
        ).astype(int)
    else:
        df['htf_bull_count']         = 0
        df['htf_fully_aligned_bull'] = 0

    # ── Bearish Alignment ──────────────────────────────────────
    bear_checks = []

    if 'ema_alignment' in df.columns:
        bear_checks.append(df['ema_alignment'] <= 1)
    if 'h1_ema_alignment' in df.columns:
        bear_checks.append(df['h1_ema_alignment'] <= 1)
    if 'h1_adx_di_bull' in df.columns:
        bear_checks.append(df['h1_adx_di_bull'] == 0)
    if 'h4_ema_alignment' in df.columns:
        bear_checks.append(df['h4_ema_alignment'] <= 1)
    if 'h4_supertrend_dir' in df.columns:
        bear_checks.append(df['h4_supertrend_dir'] == -1)

    if bear_checks:
        df['htf_bear_count'] = sum(
            c.astype(int) for c in bear_checks
        )
        df['htf_fully_aligned_bear'] = (
            df['htf_bear_count'] >= len(bear_checks)
        ).astype(int)
    else:
        df['htf_bear_count']         = 0
        df['htf_fully_aligned_bear'] = 0

    # ── Overall HTF Score ──────────────────────────────────────
    df['htf_score'] = df['htf_bull_count'] - df['htf_bear_count']

    # Conflict: H1 กับ H4 ขัดกัน = อย่าเทรด
    if 'h1_ema_alignment' in df.columns and \
       'h4_ema_alignment' in df.columns:
        df['htf_conflict'] = (
            ((df['h1_ema_alignment'] >= 3) &
             (df['h4_ema_alignment'] <= 1)) |
            ((df['h1_ema_alignment'] <= 1) &
             (df['h4_ema_alignment'] >= 3))
        ).astype(int)
    else:
        df['htf_conflict'] = 0

    return df


# ══════════════════════════════════════════════════════════════
# 2. Target Label Creation
# ══════════════════════════════════════════════════════════════
def add_target_label(
    df:              pd.DataFrame,
    forward_bars:    int   = 4,
    min_return_pct:  float = 0.0015,
    label_method:    str   = "simple",   # "simple" | "risk_adjusted" | "triple_barrier"
) -> pd.DataFrame:
    """
    สร้าง target label สำหรับ supervised learning

    3 วิธี:
    simple          = ดูแค่ return หลัง n bars
    risk_adjusted   = คิด return หารด้วย volatility
    triple_barrier  = ดูว่าราคาถึง TP หรือ SL ก่อน
    """
    df = df.copy()

    if label_method == "simple":
        df = _label_simple(df, forward_bars, min_return_pct)
    elif label_method == "risk_adjusted":
        df = _label_risk_adjusted(df, forward_bars, min_return_pct)
    elif label_method == "triple_barrier":
        df = _label_triple_barrier(df, forward_bars)
    else:
        raise ValueError(f"label_method ไม่รู้จัก: {label_method}")

    _validate_labels(df)
    return df


# ── วิธีที่ 1: Simple Return ───────────────────────────────────
def _label_simple(
    df:             pd.DataFrame,
    forward_bars:   int,
    min_return_pct: float,
) -> pd.DataFrame:
    """
    ดูว่าหลัง n bars ราคาขึ้นหรือลงเท่าไหร่
    ถ้าเกิน threshold = BUY/SELL ไม่เกิน = HOLD

    ข้อดี: เรียบง่าย เร็ว
    ข้อเสีย: ไม่สนใจ path ราคาระหว่างทาง
    """
    c = df['close']

    # Future return หลัง n bars
    # !! shift(-n) = look ahead !!
    # ยอมรับได้เฉพาะตอนสร้าง label สำหรับ train
    # ห้ามใช้ตอน predict real-time
    future_close  = c.shift(-forward_bars)
    future_return = (future_close - c) / c

    df['future_return'] = future_return   # เก็บไว้ debug

    # สร้าง label
    df['label'] = 0   # HOLD default
    df.loc[future_return >  min_return_pct, 'label'] =  1   # BUY
    df.loc[future_return < -min_return_pct, 'label'] = -1   # SELL

    # ลบ rows สุดท้าย (ไม่มี future data)
    df.loc[df.index[-forward_bars:], 'label'] = np.nan

    return df


# ── วิธีที่ 2: Risk-Adjusted Return ───────────────────────────
def _label_risk_adjusted(
    df:             pd.DataFrame,
    forward_bars:   int,
    min_return_pct: float,
) -> pd.DataFrame:
    """
    หาร return ด้วย volatility (ATR)
    label จะสะท้อน quality ของ move ไม่ใช่แค่ขนาด

    trade ที่ได้ 1% ตอน volatility สูง
    ไม่ดีเท่า trade ที่ได้ 1% ตอน volatility ต่ำ
    """
    c   = df['close']
    atr = df.get('atr_14', pd.Series(c * 0.001, index=df.index))

    future_close  = c.shift(-forward_bars)
    future_return = (future_close - c) / c

    # Normalize ด้วย ATR
    risk_adj_return = future_return / (atr / c + 1e-9)

    df['future_return']    = future_return
    df['risk_adj_return']  = risk_adj_return

    # Threshold ปรับตาม risk-adjusted scale
    threshold = min_return_pct / (atr / c).mean()

    df['label'] = 0
    df.loc[risk_adj_return >  threshold, 'label'] =  1
    df.loc[risk_adj_return < -threshold, 'label'] = -1
    df.loc[df.index[-forward_bars:], 'label'] = np.nan

    return df


# ── วิธีที่ 3: Triple Barrier (ดีที่สุด แต่ช้า) ───────────────
def _label_triple_barrier(
    df:           pd.DataFrame,
    forward_bars: int,
    sl_atr_mult:  float = 1.5,
    tp_atr_mult:  float = 2.0,
) -> pd.DataFrame:
    """
    จำลองการเทรดจริง: ดูว่าราคาถึง TP หรือ SL ก่อน
    ถ้าไม่ถึงทั้งคู่ใน n bars = HOLD

    TP ถูกแตะก่อน = BUY label
    SL ถูกแตะก่อน = SELL label
    หมดเวลา n bars = HOLD label

    ข้อดี: สะท้อนการเทรดจริงมากที่สุด
    ข้อเสีย: ช้า O(n²) ต้อง loop ทุก row
    """
    if 'atr_14' not in df.columns:
        log.warning("ไม่มี atr_14 — ใช้ simple label แทน")
        return _label_simple(df, forward_bars, 0.0015)

    c   = df['close'].values
    atr = df['atr_14'].values
    labels = np.zeros(len(df))

    for i in range(len(df) - forward_bars):
        entry  = c[i]
        tp     = entry + atr[i] * tp_atr_mult
        sl     = entry - atr[i] * sl_atr_mult

        label  = 0   # default HOLD
        for j in range(i+1, min(i+1+forward_bars, len(df))):
            high_j = df['high'].iloc[j]
            low_j  = df['low'].iloc[j]

            if high_j >= tp:
                label = 1    # TP ถูกแตะก่อน = BUY
                break
            if low_j <= sl:
                label = -1   # SL ถูกแตะก่อน = SELL
                break

        labels[i] = label

    df['label'] = labels
    df.loc[df.index[-forward_bars:], 'label'] = np.nan

    # log สัดส่วน label
    log.info(
        f"Triple Barrier: "
        f"TP={sum(labels==1)} "
        f"SL={sum(labels==-1)} "
        f"Hold={sum(labels==0)}"
    )
    return df


# ══════════════════════════════════════════════════════════════
# 3. Label Validation
# ══════════════════════════════════════════════════════════════
def _validate_labels(df: pd.DataFrame):
    """
    ตรวจสอบ label ก่อนนำไป train
    แจ้งเตือนถ้ามีปัญหา
    """
    labels = df['label'].dropna()
    total  = len(labels)

    if total == 0:
        raise ValueError("ไม่มี label เลย — ตรวจสอบ forward_bars และข้อมูล")

    dist = labels.value_counts(normalize=True)
    buy  = dist.get( 1, 0)
    sell = dist.get(-1, 0)
    hold = dist.get( 0, 0)

    log.info(
        f"Label distribution: "
        f"BUY={buy:.1%} HOLD={hold:.1%} SELL={sell:.1%} "
        f"(total={total:,})"
    )

    # ── ตรวจ Imbalance ─────────────────────────────────────────
    if buy < 0.10 or sell < 0.10:
        log.warning(
            f"⚠️ Label imbalanced: "
            f"BUY={buy:.1%} SELL={sell:.1%}\n"
            f"   แนะนำ: ลด min_return_pct ใน config.yaml"
        )

    if hold > 0.80:
        log.warning(
            f"⚠️ HOLD มากเกินไป ({hold:.1%}) "
            f"โมเดลจะ predict HOLD ตลอด\n"
            f"   แนะนำ: ลด min_return_pct หรือเพิ่ม forward_bars"
        )

    # ── ตรวจ Look-ahead Bias ───────────────────────────────────
    # label ควรเป็น NaN ใน rows สุดท้าย
    last_labels = df['label'].tail(10)
    if last_labels.notna().all():
        log.warning(
            "⚠️ Label ไม่มี NaN ท้าย DataFrame "
            "อาจมี look-ahead bias!"
        )


# ══════════════════════════════════════════════════════════════
# 4. Live Feature Builder (ใช้ใน bot/main.py)
# ══════════════════════════════════════════════════════════════
def join_htf_for_live(
    df_m15:   pd.DataFrame,
    htf_dfs:  dict,
    macro_dfs: dict = None,
) -> pd.DataFrame:
    """
    Version สำหรับ bot จริง — ไม่สร้าง label
    join เฉพาะ HTF ล่าสุด 1 row
    """
    df = add_mtf_features(df_m15, htf_dfs, macro_dfs)
    return df   # ไม่เรียก add_target_label


# ── Utility: ดู feature list ที่เพิ่มเข้ามา ──────────────────
def get_htf_feature_names(
    htf_timeframes: list = None,
    macro_sources:  list = None,
) -> list:
    """คืน list ของ feature names ที่ MTF เพิ่มมา"""
    htf_timeframes = htf_timeframes or ['h1', 'h4']
    macro_sources  = macro_sources  or ['dxy', 'vix', 'us10y']

    htf_base = [
        'ema_20','ema_50','ema_alignment',
        'macd_hist','macd_above_zero',
        'adx','adx_trending','adx_di_bull',
        'rsi_14','rsi_zone_num',
        'momentum_score','pa_score',
        'bos_bull','bos_bear','structure_score',
        'supertrend_dir','squeeze_on',
        'vol_regime_num','bb_pct',
    ]

    names = []
    for tf in htf_timeframes:
        names += [f'{tf}_{col}' for col in htf_base]

    names += ['htf_bull_count','htf_bear_count',
              'htf_score','htf_conflict',
              'htf_fully_aligned_bull','htf_fully_aligned_bear']

    for src in macro_sources:
        names += [f'macro_{src}']
    names += ['macro_dxy_pct_5d']

    return names