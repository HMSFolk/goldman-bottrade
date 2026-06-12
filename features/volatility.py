# features/volatility.py
"""
Volatility Indicators
- ATR          (Average True Range)
- Bollinger Bands
- Keltner Channel
- Squeeze Momentum (BB อยู่ใน Keltner = กำลังสะสมพลัง)
- Historical Volatility
- Volatility Regime
"""

import pandas as pd
import numpy as np
import logging

log = logging.getLogger("features")


def add_volatility_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    เพิ่ม volatility features ทั้งหมดเข้า DataFrame
    input:  df มี columns: open, high, low, close, tick_volume
    output: df เดิม + volatility columns ใหม่
    """
    df = df.copy()

    df = _add_atr(df)
    df = _add_bollinger_bands(df)
    df = _add_keltner_channel(df)
    df = _add_squeeze(df)
    df = _add_historical_volatility(df)
    df = _add_volatility_regime(df)
    df = _add_volatility_composite(df)

    return df


# ══════════════════════════════════════════════════════════════
# 1. ATR — Average True Range
# ══════════════════════════════════════════════════════════════
def _add_atr(df: pd.DataFrame) -> pd.DataFrame:
    """
    ATR วัดความผันผวนเฉลี่ยของแต่ละแท่งเทียน
    ไม่บอกทิศทาง — บอกแค่ขนาดการเคลื่อนที่

    ใช้ทำอะไร:
    1. คำนวณ SL/TP ที่เหมาะกับ volatility ปัจจุบัน
       SL = 1.5 × ATR | TP = 3.0 × ATR
    2. กรองช่วง volatility ต่ำ (ATR เล็กมาก = spread กินกำไร)
    3. วัด position size ให้ risk คงที่ทุก trade
    """
    h = df['high']
    l = df['low']
    c = df['close']

    # True Range = max ของ 3 ค่า
    tr = pd.concat([
        h - l,                    # High - Low
        (h - c.shift(1)).abs(),   # High - Previous Close
        (l - c.shift(1)).abs(),   # Low  - Previous Close
    ], axis=1).max(axis=1)

    df['tr'] = tr

    # ATR หลายช่วง (Wilder's smoothing)
    for period in [7, 14, 21]:
        df[f'atr_{period}'] = tr.ewm(
            alpha=1/period, adjust=False
        ).mean()

    atr14 = df['atr_14']

    # ── ATR % (normalize ข้าม symbol ได้) ─────────────────────
    # XAUUSD ATR อาจ = $15 แต่ EURUSD = 0.0008
    # ATR% ทำให้เปรียบเทียบกันได้
    df['atr_pct']       = atr14 / c * 100

    # ── Dynamic SL/TP จาก ATR ─────────────────────────────────
    # ใช้ใน executor.py แทนค่า fixed points
    df['sl_atr_1x']     = atr14 * 1.0   # conservative SL
    df['sl_atr_1_5x']   = atr14 * 1.5   # standard SL
    df['sl_atr_2x']     = atr14 * 2.0   # wide SL (volatile market)
    df['tp_atr_2x']     = atr14 * 2.0   # TP สำหรับ RR 1:2
    df['tp_atr_3x']     = atr14 * 3.0   # TP สำหรับ RR 1:3

    # ── ATR Ratio (volatility เทียบกับอดีต) ───────────────────
    # > 1.5 = volatile กว่าปกติ (ระวัง, อาจเป็นข่าว)
    # < 0.5 = เงียบกว่าปกติ (กำลังสะสมพลัง?)
    atr_sma20         = atr14.rolling(20).mean()
    df['atr_ratio']   = atr14 / (atr_sma20 + 1e-9)

    # ── ATR Trend ─────────────────────────────────────────────
    df['atr_expanding'] = (atr14 > atr14.shift(3)).astype(int)
    # 1 = volatility กำลังเพิ่ม | 0 = กำลังลด

    # ── ATR Percentile (อยู่ใน % ไหนของ 100 วันล่าสุด) ────────
    df['atr_pct_rank']  = atr14.rolling(100).rank(pct=True) * 100

    return df


# ══════════════════════════════════════════════════════════════
# 2. Bollinger Bands
# ══════════════════════════════════════════════════════════════
def _add_bollinger_bands(df: pd.DataFrame,
                         period: int   = 20,
                         std_dev: float = 2.0) -> pd.DataFrame:
    """
    Bollinger Bands = EMA ± (std × 2)
    วัดว่าราคาอยู่นอก "ปกติ" แค่ไหน

    Upper Band = ราคาสูงกว่าปกติ (overbought)
    Lower Band = ราคาต่ำกว่าปกติ (oversold)
    Width แคบ  = volatility ต่ำ กำลังสะสมพลัง (Squeeze)
    Width กว้าง = volatility สูง หลัง breakout

    %B บอกว่าราคาอยู่ตรงไหนใน Band:
    1.0 = อยู่บน upper | 0.5 = อยู่กลาง | 0.0 = อยู่บน lower
    """
    c  = df['close']

    bb_mid   = c.rolling(period).mean()
    bb_std   = c.rolling(period).std()

    df['bb_upper']  = bb_mid + std_dev * bb_std
    df['bb_mid']    = bb_mid
    df['bb_lower']  = bb_mid - std_dev * bb_std

    # ── %B (ราคาอยู่ตรงไหนใน Band) ────────────────────────────
    band_width      = df['bb_upper'] - df['bb_lower'] + 1e-9
    df['bb_pct']    = (c - df['bb_lower']) / band_width
    # > 1.0 = ราคาพุ่งออกนอก upper band (breakout หรือ reversal)
    # < 0.0 = ราคาหลุดออกนอก lower band

    # ── Band Width (วัด volatility) ────────────────────────────
    df['bb_width']  = band_width / bb_mid * 100   # เป็น %

    # ── BB Width Percentile ────────────────────────────────────
    df['bb_width_rank'] = df['bb_width'].rolling(100).rank(pct=True) * 100
    # < 10 = squeeze รุนแรง | > 90 = expansion รุนแรง

    # ── BB Signals ─────────────────────────────────────────────

    # ราคาแตะ Band
    df['price_touch_upper'] = (c >= df['bb_upper']).astype(int)
    df['price_touch_lower'] = (c <= df['bb_lower']).astype(int)

    # Bollinger Bounce — ราคาสะท้อนกลับจาก band
    df['bb_bounce_up']   = (
        (df['price_touch_lower'].shift(1) == 1) &
        (c > c.shift(1))
    ).astype(int)

    df['bb_bounce_down'] = (
        (df['price_touch_upper'].shift(1) == 1) &
        (c < c.shift(1))
    ).astype(int)

    # Bollinger Breakout — ราคาออกนอก band แล้วยืน
    df['bb_breakout_up'] = (
        (c > df['bb_upper']) &
        (c.shift(1) <= df['bb_upper'].shift(1))
    ).astype(int)

    df['bb_breakout_down'] = (
        (c < df['bb_lower']) &
        (c.shift(1) >= df['bb_lower'].shift(1))
    ).astype(int)

    # Walking the Band — ราคาเกาะ upper/lower band ต่อเนื่อง
    df['bb_walk_upper'] = (
        df['price_touch_upper'].rolling(3).sum() >= 2
    ).astype(int)
    df['bb_walk_lower'] = (
        df['price_touch_lower'].rolling(3).sum() >= 2
    ).astype(int)

    # ── BB เพิ่มเติม (สำหรับ Gold) ────────────────────────────
    # Bandwidth หดลงติดต่อกัน = Squeeze กำลังเกิด
    df['bb_width_shrinking'] = (
        df['bb_width'] < df['bb_width'].shift(3)
    ).astype(int)

    # ราคา mean-revert กลับ mid เมื่อ %B สูงมาก
    df['bb_overextended'] = (
        (df['bb_pct'] > 0.95) | (df['bb_pct'] < 0.05)
    ).astype(int)

    return df


# ══════════════════════════════════════════════════════════════
# 3. Keltner Channel
# ══════════════════════════════════════════════════════════════
def _add_keltner_channel(df: pd.DataFrame,
                          ema_period: int  = 20,
                          atr_mult: float  = 2.0) -> pd.DataFrame:
    """
    Keltner Channel = EMA ± (ATR × 2)
    ใช้ ATR แทน std — จึงเรียบกว่า Bollinger

    ใช้หลักๆ คู่กับ Bollinger เพื่อหา Squeeze
    ถ้า BB อยู่ใน Keltner = ตลาดกำลังสะสมพลัง
    """
    h  = df['high']
    l  = df['low']
    c  = df['close']

    ema20    = c.ewm(span=ema_period, adjust=False).mean()

    # True Range
    tr       = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr      = tr.ewm(alpha=1/ema_period, adjust=False).mean()

    df['kc_mid']    = ema20
    df['kc_upper']  = ema20 + atr_mult * atr
    df['kc_lower']  = ema20 - atr_mult * atr
    df['kc_width']  = (df['kc_upper'] - df['kc_lower']) / ema20 * 100

    # ── Keltner Signals ────────────────────────────────────────
    df['price_above_kc_upper'] = (c > df['kc_upper']).astype(int)
    df['price_below_kc_lower'] = (c < df['kc_lower']).astype(int)

    # Strong trend: ราคาเกาะนอก Keltner
    df['kc_strong_bull'] = (
        df['price_above_kc_upper'].rolling(3).sum() >= 2
    ).astype(int)
    df['kc_strong_bear'] = (
        df['price_below_kc_lower'].rolling(3).sum() >= 2
    ).astype(int)

    # %K ของ Keltner (เทียบกับ channel)
    kc_range         = df['kc_upper'] - df['kc_lower'] + 1e-9
    df['kc_pct']     = (c - df['kc_lower']) / kc_range

    return df


# ══════════════════════════════════════════════════════════════
# 4. Squeeze Momentum
# ══════════════════════════════════════════════════════════════
def _add_squeeze(df: pd.DataFrame) -> pd.DataFrame:
    """
    Squeeze = BB อยู่ภายใน Keltner Channel
    พัฒนาโดย John Carter (TTM Squeeze)

    Squeeze ON  = BB inside KC = ตลาดสะสมพลัง (coiling)
                  รอ breakout — ยังไม่เทรด
    Squeeze OFF = BB ออกนอก KC = พลังระเบิด = เทรดได้!

    เป็นหนึ่งใน feature ที่ดีที่สุดสำหรับ Gold
    เพราะ Gold มักสะสมแล้ววิ่งแรงมาก
    """
    if not all(c in df.columns for c in
               ['bb_upper','bb_lower','kc_upper','kc_lower']):
        log.warning("ต้องรัน BB และ Keltner ก่อน Squeeze")
        return df

    # Squeeze ON: BB อยู่ใน KC ทั้งคู่
    df['squeeze_on']  = (
        (df['bb_upper'] < df['kc_upper']) &
        (df['bb_lower'] > df['kc_lower'])
    ).astype(int)

    # Squeeze OFF: BB อยู่นอก KC ทั้งคู่
    df['squeeze_off'] = (
        (df['bb_upper'] >= df['kc_upper']) |
        (df['bb_lower'] <= df['kc_lower'])
    ).astype(int)

    # Squeeze Fire = เพิ่งเปลี่ยนจาก ON → OFF (จุดระเบิด!)
    df['squeeze_fire'] = (
        (df['squeeze_on'].shift(1) == 1) &
        (df['squeeze_off'] == 1)
    ).astype(int)

    # นับว่า Squeeze ON ต่อเนื่องกี่ bars (ยิ่งนาน ยิ่งแรง)
    sq_count = []
    count    = 0
    for sq in df['squeeze_on']:
        count = count + 1 if sq == 1 else 0
        sq_count.append(count)
    df['squeeze_bars'] = sq_count

    # ── Momentum Direction ตอน Squeeze ────────────────────────
    # ใช้ Linear Regression Slope บอกว่าจะระเบิดขึ้นหรือลง
    c   = df['close']
    mid = df['bb_mid'] if 'bb_mid' in df.columns else c.rolling(20).mean()

    # Momentum = ราคา - midpoint (บอก bias)
    delta = c - mid
    df['squeeze_momentum']     = delta
    df['squeeze_momentum_dir'] = (delta > delta.shift(1)).astype(int)
    # 1 = momentum กำลังเพิ่ม (จะระเบิดขึ้น)
    # 0 = momentum กำลังลด   (จะระเบิดลง)

    # Strong squeeze: ON นาน + momentum ชัดเจน
    df['squeeze_strong_bull'] = (
        (df['squeeze_fire'] == 1) &
        (df['squeeze_bars'].shift(1) >= 5) &
        (df['squeeze_momentum'] > 0)
    ).astype(int)

    df['squeeze_strong_bear'] = (
        (df['squeeze_fire'] == 1) &
        (df['squeeze_bars'].shift(1) >= 5) &
        (df['squeeze_momentum'] < 0)
    ).astype(int)

    return df


# ══════════════════════════════════════════════════════════════
# 5. Historical Volatility
# ══════════════════════════════════════════════════════════════
def _add_historical_volatility(df: pd.DataFrame) -> pd.DataFrame:
    """
    HV = std ของ log return × annualization factor
    บอกว่าตลาดผันผวนแค่ไหนเทียบกับ annualized basis

    ใช้:
    - เปรียบเทียบ volatility ข้าม symbol
    - ตัดสินใจ position size
    - กรองช่วง low volatility ที่ไม่คุ้มเทรด
    """
    c       = df['close']
    log_ret = np.log(c / c.shift(1))

    # M15: 1 วัน = 96 bars | annualized = √(252 × 96 bars/day)
    # แต่ใช้ √252 เป็น convention ง่ายกว่า
    ann     = np.sqrt(252)

    for period in [10, 20]:
        df[f'hv_{period}'] = log_ret.rolling(period).std() * ann * 100

    # HV Ratio (ปัจจุบัน vs เฉลี่ย) — บอก regime
    df['hv_ratio'] = df['hv_10'] / (df['hv_20'] + 1e-9)
    # > 1.2 = volatility กำลังขยาย (เพิ่งเกิด event?)
    # < 0.8 = volatility กำลังหด   (กำลังสะสม)

    # Log return features (ใช้เป็น feature เพิ่มเติม)
    df['log_return_1']  = log_ret
    df['log_return_5']  = np.log(c / c.shift(5))
    df['log_return_abs']= log_ret.abs()   # ขนาดการเคลื่อนที่ไม่ว่าทิศไหน

    return df


# ══════════════════════════════════════════════════════════════
# 6. Volatility Regime Detection
# ══════════════════════════════════════════════════════════════
def _add_volatility_regime(df: pd.DataFrame) -> pd.DataFrame:
    """
    แบ่งตลาดเป็น 3 regime ตาม volatility
    โมเดลที่ดีควร behave ต่างกันในแต่ละ regime

    LOW    = ตลาดเงียบ — spread กิน profit ง่าย ระวัง
    NORMAL = ปกติ — เทรดได้ตามกลยุทธ์
    HIGH   = ผันผวนสูง — ขยาย SL หรือลด position
    """
    if 'atr_pct_rank' not in df.columns:
        return df

    rank = df['atr_pct_rank']

    df['vol_regime'] = pd.cut(
        rank,
        bins   = [0,  25,  75, 100],
        labels = ['low', 'normal', 'high'],
    ).astype(str)

    # Numeric: low=0, normal=1, high=2
    regime_map = {'low': 0, 'normal': 1, 'high': 2}
    df['vol_regime_num'] = df['vol_regime'].map(regime_map).fillna(1)

    # ── Regime Transition ──────────────────────────────────────
    df['vol_regime_change'] = (
        df['vol_regime_num'] != df['vol_regime_num'].shift(1)
    ).astype(int)

    # ขยาย SL เมื่อ high volatility
    if 'sl_atr_1_5x' in df.columns:
        df['sl_dynamic'] = df.apply(
            lambda r: r['sl_atr_2x']   if r['vol_regime'] == 'high'
                 else r['sl_atr_1x']   if r['vol_regime'] == 'low'
                 else r['sl_atr_1_5x'],
            axis=1
        )

    # ── Intraday Volatility Pattern ────────────────────────────
    # Gold ผันผวนสูงช่วง London (07-16 UTC) และ NY (12-21 UTC)
    if hasattr(df.index, 'hour'):
        hour = df.index.hour
    else:
        hour = pd.to_datetime(df.index).hour

    df['is_london_session']  = pd.Series(
        ((hour >= 7)  & (hour < 16)).astype(int), index=df.index
    )
    df['is_ny_session']      = pd.Series(
        ((hour >= 12) & (hour < 21)).astype(int), index=df.index
    )
    df['is_overlap_session'] = pd.Series(
        ((hour >= 12) & (hour < 16)).astype(int), index=df.index
    )
    # Overlap (12-16 UTC) = ผันผวนสูงสุด เทรดง่ายที่สุด

    return df


# ══════════════════════════════════════════════════════════════
# 7. Volatility Composite
# ══════════════════════════════════════════════════════════════
def _add_volatility_composite(df: pd.DataFrame) -> pd.DataFrame:
    """
    สรุปภาพรวม volatility สำหรับ risk management
    """
    # Favorable trading conditions score (0-4)
    score = pd.Series(0, index=df.index)

    if 'vol_regime' in df.columns:
        score += (df['vol_regime'] == 'normal').astype(int) * 2
        score += (df['vol_regime'] == 'low').astype(int)    * 0
        score += (df['vol_regime'] == 'high').astype(int)   * 1

    if 'is_overlap_session' in df.columns:
        score += df['is_overlap_session']                    * 2

    if 'squeeze_on' in df.columns:
        # Squeeze OFF ดีกว่า (มีทิศทาง)
        score += (df['squeeze_on'] == 0).astype(int)

    df['vol_trade_quality'] = score
    # >= 3 = เงื่อนไข volatility เหมาะสมสำหรับเทรด