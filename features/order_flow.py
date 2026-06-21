# features/order_flow.py
"""
Order Flow Features (Proxy-based)
==================================
⚠️ ข้อจำกัดสำคัญ: MT5 retail feed (Exness และ broker ทั่วไป) ให้แค่
`tick_volume` (จำนวนครั้งที่ราคาเปลี่ยน) ไม่ใช่ volume จริง และไม่มี
bid/ask depth หรือ buy/sell volume แยกจริงแบบ Level 2

ฟีเจอร์ในไฟล์นี้จึงเป็น "proxy" ที่ประมาณ order flow จาก OHLCV+tick_volume
ที่มีอยู่ — ไม่ใช่ true order flow แบบ futures/equity ที่มี Level 2 จริง
แต่ยังให้สัญญาณที่มีประโยชน์ เพราะ:
  - ถ้าปิดใกล้ high ของแท่ง = แรงซื้อชนะในแท่งนั้น (proxy ของ buy pressure)
  - volume สูงผิดปกติ + range แคบ = อาจมี absorption (มีคนรับซื้อ/ขายหนัก)
  - volume สูงผิดปกติ + range กว้าง = breakout ยืนยันด้วยแรงจริง

อ้างอิงเทคนิคมาตรฐาน: OBV, VPT, Volume Z-score, Climax Volume
"""
import pandas as pd
import numpy as np
import logging

log = logging.getLogger("features")


def add_order_flow_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    เพิ่ม order-flow-proxy features ทั้งหมดเข้า DataFrame
    input:  df มี columns: open, high, low, close, tick_volume
    output: df เดิม + order flow columns ใหม่
    """
    df = df.copy()

    if 'tick_volume' not in df.columns or df['tick_volume'].sum() == 0:
        log.debug("ไม่มี volume — ข้าม order flow features ทั้งหมด")
        return df

    df = _add_volume_basic(df)
    df = _add_directional_volume(df)
    df = _add_obv(df)
    df = _add_vpt(df)
    df = _add_climax_volume(df)
    df = _add_bar_pressure(df)
    df = _add_order_flow_composite(df)

    return df


# ══════════════════════════════════════════════════════════════
# 1. Volume Basic — relative volume เทียบ baseline
# ══════════════════════════════════════════════════════════════
def _add_volume_basic(df: pd.DataFrame) -> pd.DataFrame:
    """
    Volume เทียบค่าเฉลี่ย — ใช้ z-score แทนแค่ "สูงกว่า SMA ไหม"
    z-score บอกว่า "สูงกว่าปกติแค่ไหน" ไม่ใช่แค่ใช่/ไม่ใช่
    """
    vol = df['tick_volume']

    df['volume_sma_20'] = vol.rolling(20).mean()
    df['volume_std_20'] = vol.rolling(20).std()

    # z-score: (vol ปัจจุบัน - ค่าเฉลี่ย) / std
    df['volume_zscore'] = (
        (vol - df['volume_sma_20']) / (df['volume_std_20'] + 1e-9)
    )

    df['is_high_volume']   = (df['volume_zscore'] > 1.0).astype(int)
    df['is_low_volume']    = (df['volume_zscore'] < -1.0).astype(int)
    df['is_extreme_volume']= (df['volume_zscore'] > 2.5).astype(int)

    # Volume percentile rank ใน 100 แท่งล่าสุด
    df['volume_pct_rank'] = vol.rolling(100).rank(pct=True) * 100

    return df


# ══════════════════════════════════════════════════════════════
# 2. Directional Volume — ประมาณ buy/sell pressure ต่อแท่ง
# ══════════════════════════════════════════════════════════════
def _add_directional_volume(df: pd.DataFrame) -> pd.DataFrame:
    """
    แบ่ง volume เป็น "buy-side" / "sell-side" แบบประมาณ
    (ไม่มี bid/ask จริง — ใช้ตำแหน่ง close ในแท่งเป็นตัวประมาณ)

    หลักการ (คล้าย Chaikin Money Flow):
    close ใกล้ high  → volume ส่วนใหญ่ถือเป็นแรงซื้อ
    close ใกล้ low   → volume ส่วนใหญ่ถือเป็นแรงขาย
    close ตรงกลาง    → แบ่งครึ่ง
    """
    h, l, c, o = df['high'], df['low'], df['close'], df['open']
    vol = df['tick_volume']

    # ตำแหน่ง close ในช่วง high-low ของแท่ง (0=low, 1=high)
    bar_range = (h - l).replace(0, np.nan)
    close_loc = ((c - l) / bar_range).clip(0, 1).fillna(0.5)

    # แบ่ง volume ตามตำแหน่ง close (-1 ถึง +1 แทน sell→buy)
    df['close_location']  = close_loc
    df['buy_volume_est']  = vol * close_loc
    df['sell_volume_est'] = vol * (1 - close_loc)

    # Volume Delta = buy - sell (บวก = แรงซื้อชนะ, ลบ = แรงขายชนะ)
    df['volume_delta']        = df['buy_volume_est'] - df['sell_volume_est']
    df['volume_delta_pct']    = df['volume_delta'] / (vol + 1e-9)

    # Cumulative Volume Delta (CVD) — สะสมไปเรื่อยๆ บอก bias สะสม
    df['cvd'] = df['volume_delta'].cumsum()
    df['cvd_slope_5'] = df['cvd'].diff(5)   # CVD กำลังเร่งขึ้น/ลงไหม

    return df


# ══════════════════════════════════════════════════════════════
# 3. OBV — On-Balance Volume
# ══════════════════════════════════════════════════════════════
def _add_obv(df: pd.DataFrame) -> pd.DataFrame:
    """
    OBV = สะสม volume ตามทิศทางราคา (close เทียบ close ก่อนหน้า)
    คลาสสิกที่สุดของ volume indicator — Granville (1963)

    OBV ขึ้นพร้อมราคา = ยืนยัน trend (volume สนับสนุน)
    OBV ไม่ตามราคา    = divergence (trend อาจอ่อนแรง)
    """
    c   = df['close']
    vol = df['tick_volume']

    direction = np.sign(c.diff().fillna(0))
    df['obv'] = (direction * vol).cumsum()

    # OBV slope — เทียบ momentum ของ OBV เอง
    df['obv_slope_5'] = df['obv'].diff(5)
    df['obv_rising']  = (df['obv'] > df['obv'].shift(3)).astype(int)

    # Divergence: ราคาทำ high ใหม่ แต่ OBV ไม่ทำ high ใหม่ (อ่อนแรง)
    price_new_high = c > c.rolling(20).max().shift(1)
    obv_not_high   = df['obv'] < df['obv'].rolling(20).max().shift(1)
    df['obv_bearish_div'] = (price_new_high & obv_not_high).astype(int)

    price_new_low  = c < c.rolling(20).min().shift(1)
    obv_not_low    = df['obv'] > df['obv'].rolling(20).min().shift(1)
    df['obv_bullish_div'] = (price_new_low & obv_not_low).astype(int)

    return df


# ══════════════════════════════════════════════════════════════
# 4. VPT — Volume Price Trend
# ══════════════════════════════════════════════════════════════
def _add_vpt(df: pd.DataFrame) -> pd.DataFrame:
    """
    VPT = สะสม volume × % เปลี่ยนแปลงราคา
    ต่างจาก OBV ตรงที่ชั่งน้ำหนักด้วย "ขนาด" การเปลี่ยนแปลง ไม่ใช่แค่ทิศทาง
    เคลื่อนไหวเปลี่ยนแปลงแรง = น้ำหนักมากกว่าเปลี่ยนแปลงเบาๆ
    """
    c   = df['close']
    vol = df['tick_volume']

    pct_change = c.pct_change().fillna(0)
    df['vpt'] = (vol * pct_change).cumsum()
    df['vpt_slope_5'] = df['vpt'].diff(5)

    return df


# ══════════════════════════════════════════════════════════════
# 5. Climax Volume — จุดกลับตัวที่มาพร้อม volume มหาศาล
# ══════════════════════════════════════════════════════════════
def _add_climax_volume(df: pd.DataFrame) -> pd.DataFrame:
    """
    Climax = volume สูงผิดปกติ + range ผิดปกติ
    มักเกิดตอนตลาด "หมดแรง" — ฝั่งที่ไล่ราคาหมดกระสุน

    Buying Climax  : volume พุ่ง + แท่งเขียวยาว + close ใกล้ high
                      → อาจกลับตัวลง (ใครจะซื้อก็ซื้อไปหมดแล้ว)
    Selling Climax : volume พุ่ง + แท่งแดงยาว + close ใกล้ low
                      → อาจกลับตัวขึ้น (panic sell จบรอบ)
    """
    h, l, c, o = df['high'], df['low'], df['close'], df['open']

    if 'volume_zscore' not in df.columns:
        return df   # ต้องรัน _add_volume_basic ก่อน

    bar_range_pct = (h - l) / c * 100
    range_zscore  = (
        (bar_range_pct - bar_range_pct.rolling(20).mean())
        / (bar_range_pct.rolling(20).std() + 1e-9)
    )

    high_vol_wide_range = (df['volume_zscore'] > 2.0) & (range_zscore > 1.5)

    close_near_high = (c - l) / (h - l + 1e-9) > 0.75
    close_near_low  = (c - l) / (h - l + 1e-9) < 0.25

    df['buying_climax']  = (high_vol_wide_range & close_near_high & (c > o)).astype(int)
    df['selling_climax'] = (high_vol_wide_range & close_near_low  & (c < o)).astype(int)

    return df


# ══════════════════════════════════════════════════════════════
# 6. Bar Pressure — แรงซื้อ/ขายภายในแท่งเดียว (ไม่สะสม)
# ══════════════════════════════════════════════════════════════
def _add_bar_pressure(df: pd.DataFrame) -> pd.DataFrame:
    """
    วัดว่าแท่งนี้ "เปิดแล้วโดนไล่ไปทางไหน" โดยไม่สนใจแท่งก่อนหน้า
    ต่างจาก CVD ที่สะสม — อันนี้ดู snapshot ของแท่งเดียว
    """
    h, l, c, o = df['high'], df['low'], df['close'], df['open']

    upper_wick = h - np.maximum(o, c)
    lower_wick = np.minimum(o, c) - l
    body       = (c - o).abs()
    full_range = (h - l).replace(0, np.nan)

    # Body ratio สูง = แรงเดียวตลอดแท่ง (ไม่มีใครต้าน)
    df['body_to_range']  = (body / full_range).fillna(0)

    # Wick ยาวด้านบน = มีแรงขายเข้ามาต้านตอนราคาขึ้นไปสูงสุด
    df['upper_wick_pressure'] = (upper_wick / full_range).fillna(0)
    df['lower_wick_pressure'] = (lower_wick / full_range).fillna(0)

    # Rejection: wick ยาว (>50% ของ range) ฝั่งใดฝั่งหนึ่ง = โดนปฏิเสธแรง
    df['upper_rejection'] = (df['upper_wick_pressure'] > 0.5).astype(int)
    df['lower_rejection'] = (df['lower_wick_pressure'] > 0.5).astype(int)

    return df


# ══════════════════════════════════════════════════════════════
# 7. Order Flow Composite Score
# ══════════════════════════════════════════════════════════════
def _add_order_flow_composite(df: pd.DataFrame) -> pd.DataFrame:
    """
    รวมสัญญาณ order flow ทั้งหมดเป็นคะแนนเดียว
    บวก = แรงซื้อสะสมเด่นชัด | ลบ = แรงขายสะสมเด่นชัด
    """
    score = pd.Series(0.0, index=df.index)

    if 'volume_delta_pct' in df.columns:
        score += df['volume_delta_pct'] * 2   # -2 ถึง +2

    if 'obv_rising' in df.columns:
        score += df['obv_rising'] * 2 - 1      # +1 / -1

    if 'cvd_slope_5' in df.columns:
        cvd_std = df['cvd_slope_5'].rolling(50).std() + 1e-9
        score += (df['cvd_slope_5'] / cvd_std).clip(-1, 1)

    if 'buying_climax' in df.columns:
        score -= df['buying_climax'] * 1.5     # climax ซื้อ = สัญญาณกลับตัวลง
    if 'selling_climax' in df.columns:
        score += df['selling_climax'] * 1.5    # climax ขาย = สัญญาณกลับตัวขึ้น

    df['order_flow_score'] = score.clip(-5, 5)

    return df