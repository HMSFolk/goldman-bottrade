# features/trend.py
"""
Trend Indicators
- EMA (Exponential Moving Average)
- MACD (Moving Average Convergence Divergence)
- ADX (Average Directional Index)
- Ichimoku Cloud
- Supertrend
- VWAP (Volume Weighted Average Price)
"""

import pandas as pd
import numpy as np
import logging

log = logging.getLogger("features")


def add_trend_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    เพิ่ม trend features ทั้งหมดเข้า DataFrame
    input:  df มี columns: open, high, low, close, tick_volume
    output: df เดิม + trend columns ใหม่
    """
    df = df.copy()

    df = _add_ema(df)
    df = _add_macd(df)
    df = _add_adx(df)
    df = _add_ichimoku(df)
    df = _add_supertrend(df)
    df = _add_vwap(df)
    df = _add_trend_composite(df)

    return df


# ══════════════════════════════════════════════════════════════
# 1. EMA — Exponential Moving Average
# ══════════════════════════════════════════════════════════════
def _add_ema(df: pd.DataFrame) -> pd.DataFrame:
    """
    EMA หลายช่วง — จับ trend ระยะสั้น กลาง ยาว
    
    ทำไมใช้ EMA ไม่ใช้ SMA:
    EMA ให้น้ำหนักราคาล่าสุดมากกว่า ตอบสนองเร็วกว่า
    เหมาะกับตลาดที่ราคาขยับเร็วอย่าง XAUUSD
    """
    c = df['close']

    # EMA แต่ละช่วง
    for period in [9, 20, 50, 100, 200]:
        df[f'ema_{period}'] = c.ewm(span=period, adjust=False).mean()

    # ── EMA Cross Signals ──────────────────────────────────────
    # บอกว่า trend เพิ่งเปลี่ยนทิศทาง

    # 9/20 = สัญญาณระยะสั้น
    df['ema_9_above_20']    = (df['ema_9'] > df['ema_20']).astype(int)
    df['ema_cross_9_20']    = df['ema_9_above_20'].diff().fillna(0)
    # +1 = EMA9 ข้าม EMA20 ขึ้น (bullish cross)
    # -1 = EMA9 ข้าม EMA20 ลง  (bearish cross)
    #  0 = ไม่มี cross

    # 20/50 = สัญญาณระยะกลาง (Golden/Death Cross เล็ก)
    df['ema_20_above_50']   = (df['ema_20'] > df['ema_50']).astype(int)
    df['ema_cross_20_50']   = df['ema_20_above_50'].diff().fillna(0)

    # 50/200 = Golden Cross / Death Cross ใหญ่
    df['ema_50_above_200']  = (df['ema_50'] > df['ema_200']).astype(int)
    df['ema_cross_50_200']  = df['ema_50_above_200'].diff().fillna(0)

    # ── EMA Alignment Score ────────────────────────────────────
    # นับว่า EMA เรียงตัวแบบ bullish กี่ชั้น (0-4)
    # 4 = bullish ทุก timeframe | 0 = bearish ทุก timeframe
    df['ema_alignment'] = (
        (df['ema_9']  > df['ema_20']).astype(int) +
        (df['ema_20'] > df['ema_50']).astype(int) +
        (df['ema_50'] > df['ema_100']).astype(int) +
        (df['ema_100']> df['ema_200']).astype(int)
    )
    # 4 = strong uptrend | 0 = strong downtrend | 2 = neutral

    # ── ระยะห่างราคาจาก EMA (%) ───────────────────────────────
    # บอกว่าราคา overextend จาก EMA แค่ไหน
    for period in [20, 50, 200]:
        df[f'dist_ema_{period}_pct'] = (
            (df['close'] - df[f'ema_{period}']) /
            df[f'ema_{period}'] * 100
        )

    # ── EMA Slope ─────────────────────────────────────────────
    # ความชัน EMA บอกว่า trend แรงแค่ไหน
    df['ema_20_slope']  = df['ema_20'].diff(3)  / df['ema_20'].shift(3) * 100
    df['ema_50_slope']  = df['ema_50'].diff(5)  / df['ema_50'].shift(5) * 100

    return df


# ══════════════════════════════════════════════════════════════
# 2. MACD — Moving Average Convergence Divergence
# ══════════════════════════════════════════════════════════════
def _add_macd(df: pd.DataFrame) -> pd.DataFrame:
    """
    MACD = EMA12 - EMA26 (MACD line)
    Signal = EMA9 ของ MACD
    Histogram = MACD - Signal (สำคัญที่สุด — บอก momentum)

    ใช้จับ:
    - momentum shift (histogram เปลี่ยนสี)
    - divergence (ราคาขึ้นแต่ histogram ลด = สัญญาณอ่อนแรง)
    """
    c = df['close']

    ema_fast  = c.ewm(span=12, adjust=False).mean()
    ema_slow  = c.ewm(span=26, adjust=False).mean()

    df['macd']        = ema_fast - ema_slow
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist']   = df['macd'] - df['macd_signal']

    # ── MACD Signals ───────────────────────────────────────────
    df['macd_above_signal'] = (df['macd'] > df['macd_signal']).astype(int)
    df['macd_cross']        = df['macd_above_signal'].diff().fillna(0)
    # +1 = MACD ข้าม signal ขึ้น (bullish)
    # -1 = MACD ข้าม signal ลง  (bearish)

    df['macd_above_zero']   = (df['macd'] > 0).astype(int)
    df['macd_hist_growing'] = (df['macd_hist'] > df['macd_hist'].shift(1)).astype(int)

    # ── MACD Divergence ────────────────────────────────────────
    # Bullish divergence: ราคา low ใหม่ แต่ MACD hist สูงขึ้น
    price_lower   = df['close'] < df['close'].shift(5)
    macd_higher   = df['macd_hist'] > df['macd_hist'].shift(5)
    df['macd_bullish_div'] = (price_lower & macd_higher & (df['macd_hist'] < 0)).astype(int)

    # Bearish divergence: ราคา high ใหม่ แต่ MACD hist ต่ำลง
    price_higher  = df['close'] > df['close'].shift(5)
    macd_lower    = df['macd_hist'] < df['macd_hist'].shift(5)
    df['macd_bearish_div'] = (price_higher & macd_lower & (df['macd_hist'] > 0)).astype(int)

    # Normalize MACD ด้วยราคา (เปรียบเทียบข้าม symbol ได้)
    df['macd_pct']      = df['macd']      / df['close'] * 100
    df['macd_hist_pct'] = df['macd_hist'] / df['close'] * 100

    return df


# ══════════════════════════════════════════════════════════════
# 3. ADX — Average Directional Index
# ══════════════════════════════════════════════════════════════
def _add_adx(df: pd.DataFrame,
             period: int = 14) -> pd.DataFrame:
    """
    ADX วัดความแรงของ trend (ไม่บอกทิศทาง)
    +DI = แรง bullish | -DI = แรง bearish

    ADX > 25 = trend ชัดเจน (เหมาะเทรดตาม trend)
    ADX < 20 = sideways (ระวัง — EMA cross ไม่น่าเชื่อถือ)
    ADX > 40 = trend แรงมาก (ระวัง overextend)
    """
    h = df['high']
    l = df['low']
    c = df['close']

    # True Range
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(span=period, adjust=False).mean()

    # Directional Movement
    dm_plus  = h.diff().clip(lower=0)
    dm_minus = (-l.diff()).clip(lower=0)

    # ถ้า +DM > -DM ให้ใช้ +DM ไม่งั้นใช้ 0
    cond     = dm_plus > dm_minus
    dm_plus  = dm_plus.where(cond, 0)
    dm_minus = dm_minus.where(~cond, 0)

    di_plus  = 100 * dm_plus.ewm( span=period, adjust=False).mean() / atr
    di_minus = 100 * dm_minus.ewm(span=period, adjust=False).mean() / atr

    dx       = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus + 1e-9)
    adx      = dx.ewm(span=period, adjust=False).mean()

    df['adx']     = adx
    df['adx_pos'] = di_plus    # +DI
    df['adx_neg'] = di_minus   # -DI

    # ── ADX Signals ────────────────────────────────────────────
    df['adx_trending']  = (df['adx'] > 25).astype(int)
    df['adx_strong']    = (df['adx'] > 40).astype(int)
    df['adx_di_bull']   = (df['adx_pos'] > df['adx_neg']).astype(int)
    df['adx_di_cross']  = df['adx_di_bull'].diff().fillna(0)
    # +1 = +DI ข้าม -DI ขึ้น (bullish trend เริ่ม)
    # -1 = +DI ข้าม -DI ลง  (bearish trend เริ่ม)

    df['adx_growing']   = (df['adx'] > df['adx'].shift(3)).astype(int)

    return df


# ══════════════════════════════════════════════════════════════
# 4. Ichimoku Cloud
# ══════════════════════════════════════════════════════════════
def _add_ichimoku(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ichimoku Kinko Hyo — ระบบ Japanese ที่ดีมากสำหรับ Gold

    5 เส้น:
    - Tenkan-sen  (Conversion, 9)   = ค่ากลาง high/low 9 periods
    - Kijun-sen   (Base, 26)        = ค่ากลาง high/low 26 periods
    - Senkou A    (Leading A)       = ค่ากลาง Tenkan+Kijun ล่วงหน้า 26
    - Senkou B    (Leading B)       = ค่ากลาง high/low 52 ล่วงหน้า 26
    - Chikou Span (Lagging)         = ราคาปัจจุบัน ย้อนหลัง 26

    Cloud (Kumo) = ช่องระหว่าง Senkou A และ B
    """
    h = df['high']
    l = df['low']
    c = df['close']

    # Tenkan-sen (9)
    df['ichi_conv']  = (h.rolling(9, min_periods=1).max()  + l.rolling(9, min_periods=1).min())  / 2

    # Kijun-sen (26)
    df['ichi_base']  = (h.rolling(26, min_periods=1).max() + l.rolling(26, min_periods=1).min()) / 2

    # Senkou Span A — shift ล่วงหน้า 26 periods
    df['ichi_a']     = ((df['ichi_conv'] + df['ichi_base']) / 2).shift(26)

    # Senkou Span B — shift ล่วงหน้า 26 periods
    df['ichi_b']     = ((h.rolling(52, min_periods=1).max() + l.rolling(52, min_periods=1).min()) / 2).shift(26)

    # Chikou Span — ราคาย้อนหลัง 26
    df['ichi_chikou']= c.shift(-26)

    # ── Ichimoku Signals ───────────────────────────────────────

    # ราคาเทียบกับ Cloud
    df['price_above_cloud'] = (
        (c > df['ichi_a']) & (c > df['ichi_b'])
    ).astype(int)
    df['price_below_cloud'] = (
        (c < df['ichi_a']) & (c < df['ichi_b'])
    ).astype(int)
    df['price_in_cloud']    = (
        ~df['price_above_cloud'].astype(bool) &
        ~df['price_below_cloud'].astype(bool)
    ).astype(int)

    # Cloud color (bullish = A > B)
    df['cloud_bullish']  = (df['ichi_a'] > df['ichi_b']).astype(int)

    # Cloud thickness (% ของราคา) — บอก support/resistance แรงแค่ไหน
    df['cloud_thickness']= (df['ichi_a'] - df['ichi_b']).abs() / c * 100

    # TK Cross — Tenkan ข้าม Kijun
    df['tk_above']       = (df['ichi_conv'] > df['ichi_base']).astype(int)
    df['tk_cross']       = df['tk_above'].diff().fillna(0)
    # +1 = bullish TK cross | -1 = bearish TK cross

    # Strong signal: TK cross + ราคาอยู่เหนือ cloud
    df['ichi_strong_bull'] = (
        (df['tk_cross'] == 1) &
        (df['price_above_cloud'] == 1) &
        (df['cloud_bullish'] == 1)
    ).astype(int)

    df['ichi_strong_bear'] = (
        (df['tk_cross'] == -1) &
        (df['price_below_cloud'] == 1) &
        (df['cloud_bullish'] == 0)
    ).astype(int)

    return df


# ══════════════════════════════════════════════════════════════
# 5. Supertrend
# ══════════════════════════════════════════════════════════════
def _add_supertrend(df: pd.DataFrame,
                    period: int     = 10,
                    multiplier: float = 3.0) -> pd.DataFrame:
    """
    Supertrend = ATR-based trend follower
    ดีมากสำหรับ Gold เพราะปรับตาม volatility อัตโนมัติ

    +1 = uptrend (ราคาอยู่เหนือ supertrend line)
    -1 = downtrend (ราคาอยู่ใต้ supertrend line)
    """
    h = df['high']
    l = df['low']
    c = df['close']

    # ATR
    tr  = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()

    hl2   = (h + l) / 2
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    # ✅ FIX: Supertrend iterative — ใช้ numpy arrays แทน .iloc
    #    เร็วกว่า ~100x: 10000 bars × 4 .iloc calls → numpy array access
    upper_arr = upper.values.copy()
    lower_arr = lower.values.copy()
    c_arr     = c.values
    st_arr    = np.full(len(df), np.nan)
    trend_arr = np.ones(len(df))

    for i in range(1, len(df)):
        # Upper band — ✅ FIX: ลบ no-op assignment (upper[i] = upper[i])
        upper_arr[i] = upper_arr[i] if (
            upper_arr[i] < upper_arr[i-1] or c_arr[i-1] > upper_arr[i-1]
        ) else upper_arr[i-1]

        # Lower band — ✅ FIX: ลบ no-op assignment (lower[i] = lower[i])
        lower_arr[i] = lower_arr[i] if (
            lower_arr[i] > lower_arr[i-1] or c_arr[i-1] < lower_arr[i-1]
        ) else lower_arr[i-1]

        # Trend direction
        if i > 0 and st_arr[i-1] == upper_arr[i-1]:
            trend_arr[i] = -1 if c_arr[i] > upper_arr[i] else 1
        else:
            trend_arr[i] = 1 if c_arr[i] < lower_arr[i] else -1

        st_arr[i] = lower_arr[i] if trend_arr[i] == 1 else upper_arr[i]

    # แปลงกลับเป็น Series
    st    = pd.Series(st_arr,    index=df.index)
    trend = pd.Series(trend_arr, index=df.index)

    df['supertrend']        = st
    df['supertrend_dir']    = trend    # +1 uptrend | -1 downtrend
    df['supertrend_flip']   = trend.diff().fillna(0) / 2
    # +1 = เปลี่ยนเป็น uptrend | -1 = เปลี่ยนเป็น downtrend

    df['dist_supertrend_pct'] = (c - st) / c * 100

    return df


# ══════════════════════════════════════════════════════════════
# 6. VWAP — Volume Weighted Average Price
# ══════════════════════════════════════════════════════════════
def _add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    VWAP = ราคาเฉลี่ยถ่วงน้ำหนักด้วย volume
    เป็น fair value ของ session

    ราคาเหนือ VWAP = bullish bias
    ราคาใต้  VWAP = bearish bias

    หมายเหตุ: VWAP reset ทุกวัน
    """
    if 'tick_volume' not in df.columns or df['tick_volume'].sum() == 0:
        log.debug("ไม่มี volume — ข้าม VWAP")
        return df

    tp  = (df['high'] + df['low'] + df['close']) / 3
    vol = df['tick_volume'].replace(0, 1)   # ป้องกัน division by zero

    # Group by date (VWAP reset ทุกวัน)
    date_group = df.index.date if hasattr(df.index, 'date') else \
                 pd.to_datetime(df.index).date

    cum_tp_vol = (tp * vol).groupby(date_group).cumsum()
    cum_vol    = vol.groupby(date_group).cumsum()

    df['vwap']           = cum_tp_vol / cum_vol
    df['price_vs_vwap']  = (df['close'] > df['vwap']).astype(int)
    df['dist_vwap_pct']  = (df['close'] - df['vwap']) / df['vwap'] * 100

    return df

# ══════════════════════════════════════════════════════════════
# 7. Composite Trend Score
# ══════════════════════════════════════════════════════════════
def _add_trend_composite(df: pd.DataFrame) -> pd.DataFrame:
    """
    รวมสัญญาณ trend ทุกตัวเป็นคะแนนเดียว
    ยิ่งสูง = trend bullish ยิ่งชัด
    range: -7 ถึง +7
    """
    score = pd.Series(0.0, index=df.index)

    # EMA alignment (0-4 → normalize เป็น -2 ถึง +2)
    if 'ema_alignment' in df.columns:
        score += (df['ema_alignment'] - 2)

    # MACD
    if 'macd_above_signal' in df.columns:
        score += df['macd_above_signal'] * 2 - 1   # +1 or -1
    if 'macd_above_zero' in df.columns:
        score += df['macd_above_zero'] * 2 - 1

    # ADX (เพิ่มน้ำหนักเมื่อ trend แรง)
    if 'adx_di_bull' in df.columns:
        adx_weight = df.get('adx_trending', pd.Series(1, index=df.index))
        score += (df['adx_di_bull'] * 2 - 1) * adx_weight

    # Ichimoku
    if 'price_above_cloud' in df.columns:
        score += df['price_above_cloud']
        score -= df['price_below_cloud']

    # Supertrend
    if 'supertrend_dir' in df.columns:
        score += df['supertrend_dir']

    df['trend_score'] = score

    # Trend category
    df['trend_cat'] = pd.cut(
        score,
        bins   = [-np.inf, -3, -1, 1, 3, np.inf],
        labels = ['strong_down', 'weak_down', 'neutral',
                  'weak_up', 'strong_up']
    ).astype(str)
    return df