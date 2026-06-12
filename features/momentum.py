# features/momentum.py
"""
Momentum Indicators
- RSI   (Relative Strength Index)
- Stochastic Oscillator
- CCI   (Commodity Channel Index)
- Williams %R
- ROC   (Rate of Change)
- MFI   (Money Flow Index)
- Momentum composite score
"""

import pandas as pd
import numpy as np
import logging

log = logging.getLogger("features")


def add_momentum_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    เพิ่ม momentum features ทั้งหมดเข้า DataFrame
    input:  df มี columns: open, high, low, close, tick_volume
    output: df เดิม + momentum columns ใหม่
    """
    df = df.copy()

    df = _add_rsi(df)
    df = _add_stochastic(df)
    df = _add_cci(df)
    df = _add_williams_r(df)
    df = _add_roc(df)
    df = _add_mfi(df)
    df = _add_momentum_composite(df)

    return df


# ══════════════════════════════════════════════════════════════
# 1. RSI — Relative Strength Index
# ══════════════════════════════════════════════════════════════
def _add_rsi(df: pd.DataFrame) -> pd.DataFrame:
    """
    RSI วัดความแข็งแกร่งของ price movement
    range: 0-100

    > 70 = Overbought (ระวังราคาจะกลับลง)
    < 30 = Oversold   (ระวังราคาจะกลับขึ้น)
    50   = แนวกลาง (เหนือ = bullish, ใต้ = bearish)

    สำหรับ Gold/Forex ที่มี strong trend:
    > 80 = overbought จริง (ปกติ 70 อาจยังขึ้นต่อได้)
    < 20 = oversold จริง
    """
    c = df['close']

    for period in [7, 14, 21]:
        delta     = c.diff()
        gain      = delta.clip(lower=0)
        loss      = (-delta).clip(lower=0)

        # Wilder's smoothing (แม่นกว่า EMA ธรรมดา)
        avg_gain  = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss  = loss.ewm(alpha=1/period, adjust=False).mean()

        rs        = avg_gain / (avg_loss + 1e-9)
        rsi       = 100 - (100 / (1 + rs))

        df[f'rsi_{period}'] = rsi

    # ── RSI Zones ─────────────────────────────────────────────
    rsi14 = df['rsi_14']

    df['rsi_zone'] = pd.cut(
        rsi14,
        bins   = [0,  20,  35,  50,  65,  80, 100],
        labels = ['extreme_os', 'oversold', 'bearish',
                  'bullish', 'overbought', 'extreme_ob'],
        right  = True,
    ).astype(str)

    # Numeric zone (-2 ถึง +2)
    zone_map = {
        'extreme_os': -2, 'oversold': -1, 'bearish': -0.5,
        'bullish':  0.5, 'overbought': 1, 'extreme_ob': 2
    }
    df['rsi_zone_num'] = df['rsi_zone'].map(zone_map).fillna(0)

    # ── RSI Cross 50 ───────────────────────────────────────────
    # RSI ข้าม 50 = momentum shift สำคัญ
    df['rsi_above_50']  = (rsi14 > 50).astype(int)
    df['rsi_cross_50']  = df['rsi_above_50'].diff().fillna(0)
    # +1 = RSI ข้าม 50 ขึ้น | -1 = RSI ข้าม 50 ลง

    # ── RSI Divergence ─────────────────────────────────────────
    # Bullish div: ราคา low ใหม่ แต่ RSI สูงขึ้น
    price_lower_5    = df['close'] < df['close'].shift(5)
    rsi_higher_5     = rsi14       > rsi14.shift(5)
    df['rsi_bull_div'] = (
        price_lower_5 & rsi_higher_5 & (rsi14 < 40)
    ).astype(int)

    # Bearish div: ราคา high ใหม่ แต่ RSI ต่ำลง
    price_higher_5   = df['close'] > df['close'].shift(5)
    rsi_lower_5      = rsi14       < rsi14.shift(5)
    df['rsi_bear_div'] = (
        price_higher_5 & rsi_lower_5 & (rsi14 > 60)
    ).astype(int)

    # ── RSI Slope ──────────────────────────────────────────────
    df['rsi_slope_3']  = rsi14.diff(3)    # เปลี่ยน 3 bars ล่าสุด
    df['rsi_slope_dir']= (df['rsi_slope_3'] > 0).astype(int)

    # ── Hidden Divergence (ยืนยัน trend continuation) ─────────
    # Hidden bullish: ราคา higher low แต่ RSI lower low (trend up ยังคงอยู่)
    df['rsi_hidden_bull'] = (
        (df['close'] > df['close'].shift(5)) &
        (rsi14 < rsi14.shift(5)) &
        (rsi14 > 30)
    ).astype(int)

    return df


# ══════════════════════════════════════════════════════════════
# 2. Stochastic Oscillator
# ══════════════════════════════════════════════════════════════
def _add_stochastic(df: pd.DataFrame,
                    k_period: int  = 14,
                    d_period: int  = 3,
                    smooth_k: int  = 3) -> pd.DataFrame:
    """
    Stochastic วัดว่าราคาปัจจุบันอยู่ตรงไหนใน range ของ n periods
    range: 0-100

    %K = (close - lowest low) / (highest high - lowest low) × 100
    %D = SMA3 ของ %K (signal line)

    > 80 = Overbought
    < 20 = Oversold

    ดีกว่า RSI ตอนตลาด sideways/ranging
    ใช้คู่กับ RSI: ถ้าทั้งคู่ oversold = สัญญาณแรงมาก
    """
    h = df['high']
    l = df['low']
    c = df['close']

    lowest_low    = l.rolling(k_period).min()
    highest_high  = h.rolling(k_period).max()

    hl_range      = highest_high - lowest_low + 1e-9
    raw_k         = 100 * (c - lowest_low) / hl_range

    # Smooth %K
    stoch_k       = raw_k.rolling(smooth_k).mean()
    stoch_d       = stoch_k.rolling(d_period).mean()

    df['stoch_k'] = stoch_k
    df['stoch_d'] = stoch_d

    # ── Stochastic Signals ─────────────────────────────────────
    df['stoch_ob']    = (stoch_k > 80).astype(int)   # overbought
    df['stoch_os']    = (stoch_k < 20).astype(int)   # oversold

    # K cross D (สัญญาณซื้อ/ขาย)
    df['stoch_k_above_d'] = (stoch_k > stoch_d).astype(int)
    df['stoch_cross']     = df['stoch_k_above_d'].diff().fillna(0)
    # +1 = K ข้าม D ขึ้น (bullish) | -1 = K ข้าม D ลง (bearish)

    # สัญญาณแรง: cross + อยู่ใน oversold/overbought zone
    df['stoch_bull_signal'] = (
        (df['stoch_cross'] ==  1) & (stoch_k < 30)
    ).astype(int)
    df['stoch_bear_signal'] = (
        (df['stoch_cross'] == -1) & (stoch_k > 70)
    ).astype(int)

    # ── Stochastic Divergence ──────────────────────────────────
    df['stoch_bull_div'] = (
        (df['close']   < df['close'].shift(5)) &
        (stoch_k       > stoch_k.shift(5)) &
        (stoch_k < 30)
    ).astype(int)

    df['stoch_bear_div'] = (
        (df['close']   > df['close'].shift(5)) &
        (stoch_k       < stoch_k.shift(5)) &
        (stoch_k > 70)
    ).astype(int)

    # Normalize เป็น -1 ถึง +1
    df['stoch_k_norm'] = (stoch_k - 50) / 50

    return df


# ══════════════════════════════════════════════════════════════
# 3. CCI — Commodity Channel Index
# ══════════════════════════════════════════════════════════════
def _add_cci(df: pd.DataFrame,
             period: int = 20) -> pd.DataFrame:
    """
    CCI วัดว่าราคาเบี่ยงเบนจากค่าเฉลี่ยแค่ไหน
    range: ไม่จำกัด (ปกติ -300 ถึง +300)

    > +100 = เข้า overbought zone (trend แรง ยังไปต่อได้)
    < -100 = เข้า oversold zone
    > +200 = extreme overbought (ระวัง reversal)
    < -200 = extreme oversold

    เหมาะกับ Gold มากเพราะ Gold ถูก develop มาสำหรับ commodity
    """
    h  = df['high']
    l  = df['low']
    c  = df['close']

    tp = (h + l + c) / 3   # Typical Price
    tp_ma   = tp.rolling(period).mean()

    # Mean Absolute Deviation
    mad     = tp.rolling(period).apply(
        lambda x: np.abs(x - x.mean()).mean(),
        raw=True
    )

    df['cci_20'] = (tp - tp_ma) / (0.015 * mad + 1e-9)

    # CCI สั้น (10) สำหรับสัญญาณเร็ว
    tp_ma_10 = tp.rolling(10).mean()
    mad_10   = tp.rolling(10).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    )
    df['cci_10'] = (tp - tp_ma_10) / (0.015 * mad_10 + 1e-9)

    # ── CCI Signals ────────────────────────────────────────────
    cci = df['cci_20']

    df['cci_ob']       = (cci >  100).astype(int)
    df['cci_os']       = (cci < -100).astype(int)
    df['cci_extreme_ob']= (cci >  200).astype(int)
    df['cci_extreme_os']= (cci < -200).astype(int)

    # CCI cross ±100 = สัญญาณ entry
    df['cci_above_100']  = (cci > 100).astype(int)
    df['cci_cross_100']  = df['cci_above_100'].diff().fillna(0)
    # +1 = CCI ข้าม +100 ขึ้น (momentum เพิ่ง buildup)
    # -1 = CCI ข้าม +100 ลง  (momentum เริ่มอ่อน)

    df['cci_below_n100'] = (cci < -100).astype(int)
    df['cci_cross_n100'] = df['cci_below_n100'].diff().fillna(0)

    # CCI cross 0
    df['cci_above_zero'] = (cci > 0).astype(int)
    df['cci_cross_zero'] = df['cci_above_zero'].diff().fillna(0)

    # Normalize เป็น -1 ถึง +1 (clip ที่ ±300)
    df['cci_norm']       = (cci / 300).clip(-1, 1)

    return df


# ══════════════════════════════════════════════════════════════
# 4. Williams %R
# ══════════════════════════════════════════════════════════════
def _add_williams_r(df: pd.DataFrame,
                    period: int = 14) -> pd.DataFrame:
    """
    Williams %R = Stochastic กลับหัว
    range: -100 ถึง 0

    -80 ถึง -100 = Oversold  (ซื้อได้)
    -20 ถึง   0  = Overbought (ขายได้)
    -50          = แนวกลาง

    ตอบสนองเร็วกว่า RSI เหมาะจับ short-term reversal
    ใช้ยืนยัน RSI และ Stochastic
    """
    h  = df['high']
    l  = df['low']
    c  = df['close']

    highest_high = h.rolling(period).max()
    lowest_low   = l.rolling(period).min()

    df['williams_r'] = -100 * (highest_high - c) / (
        highest_high - lowest_low + 1e-9
    )

    wr = df['williams_r']

    # ── Williams %R Signals ────────────────────────────────────
    df['wr_ob']     = (wr > -20).astype(int)    # overbought
    df['wr_os']     = (wr < -80).astype(int)    # oversold

    df['wr_above_50'] = (wr > -50).astype(int)
    df['wr_cross_50'] = df['wr_above_50'].diff().fillna(0)

    # Exit from OB/OS zone = สัญญาณ reversal
    df['wr_exit_ob']  = (
        (wr.shift(1) > -20) & (wr <= -20)
    ).astype(int)   # ออกจาก overbought → ขาย

    df['wr_exit_os']  = (
        (wr.shift(1) < -80) & (wr >= -80)
    ).astype(int)   # ออกจาก oversold → ซื้อ

    # Normalize เป็น -1 ถึง +1
    # Williams -100 = -1 (oversold) | 0 = +1 (overbought)
    df['wr_norm']     = (wr + 50) / 50

    return df


# ══════════════════════════════════════════════════════════════
# 5. ROC — Rate of Change
# ══════════════════════════════════════════════════════════════
def _add_roc(df: pd.DataFrame) -> pd.DataFrame:
    """
    ROC = % การเปลี่ยนแปลงราคาใน n periods
    บอกว่า momentum เร็วหรือช้าแค่ไหน

    บวก = ราคาขึ้น | ลบ = ราคาลง
    ยิ่งสูง = momentum แรง
    """
    c = df['close']

    for period in [5, 10, 20]:
        df[f'roc_{period}'] = (
            (c - c.shift(period)) / c.shift(period) * 100
        )

    # ROC acceleration (momentum ของ momentum)
    df['roc_accel_5']  = df['roc_5'].diff(3)

    # ROC signals
    df['roc_positive']  = (df['roc_10'] > 0).astype(int)
    df['roc_cross_zero']= df['roc_positive'].diff().fillna(0)
    df['roc_accel_pos'] = (df['roc_accel_5'] > 0).astype(int)

    return df


# ══════════════════════════════════════════════════════════════
# 6. MFI — Money Flow Index
# ══════════════════════════════════════════════════════════════
def _add_mfi(df: pd.DataFrame,
             period: int = 14) -> pd.DataFrame:
    """
    MFI = RSI ที่ใช้ Volume ด้วย (Volume-weighted RSI)
    range: 0-100

    > 80 = Overbought (เงินไหลออก)
    < 20 = Oversold   (เงินไหลเข้า)

    ถ้า RSI overbought แต่ MFI ยังไม่ถึง 80
    = เงินยังไหลเข้าอยู่ — อาจขึ้นต่อได้
    """
    if 'tick_volume' not in df.columns or df['tick_volume'].sum() == 0:
        log.debug("ไม่มี volume — ข้าม MFI")
        df['mfi_14'] = 50.0   # ค่ากลาง neutral
        return df

    h   = df['high']
    l   = df['low']
    c   = df['close']
    vol = df['tick_volume'].replace(0, 1)

    tp  = (h + l + c) / 3   # Typical Price
    rmf = tp * vol            # Raw Money Flow

    # Positive/Negative Money Flow
    tp_prev   = tp.shift(1)
    pos_flow  = rmf.where(tp > tp_prev, 0)
    neg_flow  = rmf.where(tp < tp_prev, 0)

    pos_sum   = pos_flow.rolling(period).sum()
    neg_sum   = neg_flow.rolling(period).sum()

    mfr       = pos_sum / (neg_sum + 1e-9)
    df['mfi_14'] = 100 - (100 / (1 + mfr))

    mfi = df['mfi_14']

    df['mfi_ob']          = (mfi > 80).astype(int)
    df['mfi_os']          = (mfi < 20).astype(int)

    # RSI/MFI divergence — สัญญาณ hidden
    if 'rsi_14' in df.columns:
        df['rsi_mfi_bull'] = (
            (df['rsi_14'] < 40) & (mfi > df['rsi_14'])
        ).astype(int)  # volume ยังสนับสนุน แม้ RSI ต่ำ

    return df


# ══════════════════════════════════════════════════════════════
# 7. Momentum Composite Score
# ══════════════════════════════════════════════════════════════
def _add_momentum_composite(df: pd.DataFrame) -> pd.DataFrame:
    """
    รวมสัญญาณ momentum ทุกตัวเป็นคะแนนเดียว
    range: -5 ถึง +5
    ยิ่งสูง = momentum bullish แรง
    """
    score = pd.Series(0.0, index=df.index)

    # RSI (น้ำหนัก 1.5)
    if 'rsi_zone_num' in df.columns:
        score += df['rsi_zone_num'] * 1.5

    # Stochastic (น้ำหนัก 1.0)
    if 'stoch_k_norm' in df.columns:
        score += df['stoch_k_norm']

    # Williams %R (น้ำหนัก 0.5)
    if 'wr_norm' in df.columns:
        score += df['wr_norm'] * 0.5

    # CCI (น้ำหนัก 1.0)
    if 'cci_norm' in df.columns:
        score += df['cci_norm']

    # ROC direction (น้ำหนัก 0.5)
    if 'roc_positive' in df.columns:
        score += (df['roc_positive'] * 2 - 1) * 0.5

    df['momentum_score'] = score.clip(-5, 5)

    # Confluence: นับว่า indicator กี่ตัวเห็นตรงกัน
    if all(c in df.columns for c in
           ['rsi_above_50','stoch_k_above_d','cci_above_zero','wr_above_50']):

        df['momentum_confluence'] = (
            df['rsi_above_50']    +
            df['stoch_k_above_d'] +
            df['cci_above_zero']  +
            df['wr_above_50']
        )
        # 4 = ทุกตัว bullish | 0 = ทุกตัว bearish

    return df