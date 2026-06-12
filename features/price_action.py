# features/price_action.py
"""
Price Action Features
- Candle Structure    (body, wick, ratio)
- Candle Patterns     (Doji, Hammer, Engulfing, ฯลฯ)
- Pivot Points        (Classic, Camarilla)
- Support/Resistance  (Rolling High/Low, Round Numbers)
- Price Position      (ราคาอยู่ตรงไหนใน range)
- Market Structure    (Higher High/Low, trend structure)
"""

import pandas as pd
import numpy as np
import logging

log = logging.getLogger("features")


def add_price_action_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    เพิ่ม price action features ทั้งหมดเข้า DataFrame
    input:  df มี columns: open, high, low, close, tick_volume
    output: df เดิม + price action columns ใหม่
    """
    df = df.copy()

    df = _add_candle_structure(df)
    df = _add_candle_patterns(df)
    df = _add_pivot_points(df)
    df = _add_support_resistance(df)
    df = _add_price_position(df)
    df = _add_market_structure(df)
    df = _add_pa_composite(df)

    return df


# ══════════════════════════════════════════════════════════════
# 1. Candle Structure — วิเคราะห์โครงสร้างแท่งเทียน
# ══════════════════════════════════════════════════════════════
def _add_candle_structure(df: pd.DataFrame) -> pd.DataFrame:
    """
    วิเคราะห์แต่ละแท่งเทียนว่ามีลักษณะยังไง
    โมเดล ML อ่านตรงนี้เพื่อเข้าใจ "ความตั้งใจ" ของตลาด
    """
    o = df['open']
    h = df['high']
    l = df['low']
    c = df['close']

    # ── ขนาดพื้นฐาน ───────────────────────────────────────────
    df['candle_body']        = (c - o).abs()
    df['candle_range']       = h - l
    df['candle_upper_wick']  = h - df[['open','close']].max(axis=1)
    df['candle_lower_wick']  = df[['open','close']].min(axis=1) - l

    # ── สัดส่วน (normalize) ───────────────────────────────────
    rng = df['candle_range'] + 1e-9   # ป้องกัน division by zero

    df['body_ratio']        = df['candle_body']       / rng  # 0-1
    df['upper_wick_ratio']  = df['candle_upper_wick'] / rng  # 0-1
    df['lower_wick_ratio']  = df['candle_lower_wick'] / rng  # 0-1

    # ── ทิศทาง ────────────────────────────────────────────────
    df['is_bullish']        = (c > o).astype(int)
    df['is_bearish']        = (c < o).astype(int)
    df['is_neutral']        = (c == o).astype(int)

    # ── ขนาดเทียบค่าเฉลี่ย ────────────────────────────────────
    avg_range = df['candle_range'].rolling(20).mean()
    df['relative_range']    = df['candle_range'] / (avg_range + 1e-9)
    # > 1.5 = แท่งใหญ่กว่าปกติ (momentum แรง)
    # < 0.5 = แท่งเล็กกว่าปกติ (ลังเล)

    df['is_big_candle']     = (df['relative_range'] > 1.5).astype(int)
    df['is_small_candle']   = (df['relative_range'] < 0.5).astype(int)

    # ── Close Position ────────────────────────────────────────
    # ราคาปิดอยู่ตรงไหนของแท่งนี้ (0=ล่าง 1=บน)
    df['close_position']    = (c - l) / rng
    # > 0.7 = ปิดแถวบน (bulls ชนะ)
    # < 0.3 = ปิดแถวล่าง (bears ชนะ)

    return df


# ══════════════════════════════════════════════════════════════
# 2. Candle Patterns
# ══════════════════════════════════════════════════════════════
def _add_candle_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Pattern แท่งเทียนที่สำคัญ — แต่ละตัวบอก bias ซื้อ/ขาย
    ทุก pattern คืนค่า: +1 (bullish), -1 (bearish), 0 (ไม่มี)
    """
    o = df['open']
    h = df['high']
    l = df['low']
    c = df['close']

    body        = df['candle_body']
    rng         = df['candle_range'] + 1e-9
    upper_wick  = df['candle_upper_wick']
    lower_wick  = df['candle_lower_wick']
    avg_body    = body.rolling(10).mean()

    # ── Single Candle Patterns ─────────────────────────────────

    # Doji — body เล็กมาก = ตลาดลังเล
    df['pat_doji'] = (body < avg_body * 0.1).astype(int)

    # Hammer / Hanging Man
    # lower wick ยาว > 2× body, upper wick สั้น
    is_hammer_shape = (
        (lower_wick > body * 2.0) &
        (upper_wick < body * 0.5) &
        (body > 0)
    )
    df['pat_hammer']    = (is_hammer_shape & (c > o)).astype(int) * 1
    df['pat_hang_man']  = (is_hammer_shape & (c < o)).astype(int) * -1

    # Shooting Star / Inverted Hammer
    # upper wick ยาว > 2× body, lower wick สั้น
    is_star_shape = (
        (upper_wick > body * 2.0) &
        (lower_wick < body * 0.5) &
        (body > 0)
    )
    df['pat_shooting_star']     = (is_star_shape & (c < o)).astype(int) * -1
    df['pat_inv_hammer']        = (is_star_shape & (c > o)).astype(int) * 1

    # Marubozu — body เกือบเต็มแท่ง ไม่มี wick
    df['pat_bull_marubozu'] = (
        (c > o) &
        (body / rng > 0.85) &
        (upper_wick / rng < 0.05) &
        (lower_wick / rng < 0.05)
    ).astype(int)

    df['pat_bear_marubozu'] = (
        (c < o) &
        (body / rng > 0.85) &
        (upper_wick / rng < 0.05) &
        (lower_wick / rng < 0.05)
    ).astype(int) * -1

    # Spinning Top — body กลาง wick ทั้งสองยาวพอๆ กัน
    df['pat_spinning_top'] = (
        (body / rng < 0.3) &
        (upper_wick / rng > 0.2) &
        (lower_wick / rng > 0.2)
    ).astype(int)

    # ── Two-Candle Patterns ────────────────────────────────────

    prev_o = o.shift(1)
    prev_c = c.shift(1)
    prev_body = body.shift(1)

    # Bullish Engulfing — แท่งปัจจุบัน bullish กลืนแท่งก่อน
    df['pat_bull_engulf'] = (
        (c > o) &                       # ปัจจุบัน bullish
        (prev_c < prev_o) &             # ก่อนหน้า bearish
        (o <= prev_c) &                 # เปิดต่ำกว่าหรือเท่า close เก่า
        (c >= prev_o) &                 # ปิดสูงกว่าหรือเท่า open เก่า
        (body > prev_body * 0.8)        # body ใหญ่พอ
    ).astype(int)

    # Bearish Engulfing
    df['pat_bear_engulf'] = (
        (c < o) &                       # ปัจจุบัน bearish
        (prev_c > prev_o) &             # ก่อนหน้า bullish
        (o >= prev_c) &                 # เปิดสูงกว่าหรือเท่า close เก่า
        (c <= prev_o) &                 # ปิดต่ำกว่าหรือเท่า open เก่า
        (body > prev_body * 0.8)
    ).astype(int) * -1

    # Bullish Harami — แท่งเล็กอยู่ใน body แท่งใหญ่ bearish
    df['pat_bull_harami'] = (
        (c > o) &
        (prev_c < prev_o) &
        (o > prev_c) & (o < prev_o) &
        (c > prev_c) & (c < prev_o) &
        (body < prev_body * 0.6)
    ).astype(int)

    # Bearish Harami
    df['pat_bear_harami'] = (
        (c < o) &
        (prev_c > prev_o) &
        (o < prev_c) & (o > prev_o) &
        (c < prev_c) & (c > prev_o) &
        (body < prev_body * 0.6)
    ).astype(int) * -1

    # Tweezer Bottom (สัญญาณกลับตัวขาขึ้น)
    df['pat_tweezer_bottom'] = (
        (l.round(1) == l.shift(1).round(1)) &   # low เท่ากัน
        (c < o).shift(1) &                       # แท่งก่อน bearish
        (c > o)                                  # แท่งนี้ bullish
    ).astype(int)

    # Tweezer Top
    df['pat_tweezer_top'] = (
        (h.round(1) == h.shift(1).round(1)) &
        (c > o).shift(1) &
        (c < o)
    ).astype(int) * -1

    # ── Three-Candle Patterns ──────────────────────────────────

    prev2_c = c.shift(2)
    prev2_o = o.shift(2)

    # Three White Soldiers — 3 แท่ง bullish ต่อเนื่อง
    df['pat_3_white_soldiers'] = (
        (c > o) &
        (prev_c > prev_o) &
        (prev2_c > prev2_o) &
        (c > prev_c) &
        (prev_c > prev2_c) &
        (df['body_ratio'] > 0.6) &
        (df['body_ratio'].shift(1) > 0.6) &
        (df['body_ratio'].shift(2) > 0.6)
    ).astype(int)

    # Three Black Crows
    df['pat_3_black_crows'] = (
        (c < o) &
        (prev_c < prev_o) &
        (prev2_c < prev2_o) &
        (c < prev_c) &
        (prev_c < prev2_c) &
        (df['body_ratio'] > 0.6) &
        (df['body_ratio'].shift(1) > 0.6) &
        (df['body_ratio'].shift(2) > 0.6)
    ).astype(int) * -1

    # Morning Star — Doji กลาง (bullish reversal)
    df['pat_morning_star'] = (
        (prev2_c < prev2_o) &           # แท่ง 1: bearish ใหญ่
        (df['pat_doji'].shift(1) == 1) & # แท่ง 2: doji
        (c > o) &                        # แท่ง 3: bullish
        (c > (prev2_o + prev2_c) / 2)   # ปิดเกินกึ่งกลางแท่ง 1
    ).astype(int)

    # Evening Star
    df['pat_evening_star'] = (
        (prev2_c > prev2_o) &
        (df['pat_doji'].shift(1) == 1) &
        (c < o) &
        (c < (prev2_o + prev2_c) / 2)
    ).astype(int) * -1

    # ── Pattern Strength Score ─────────────────────────────────
    # รวมสัญญาณ bullish/bearish ทั้งหมดเป็นคะแนน
    bullish_cols = [
        'pat_hammer', 'pat_inv_hammer', 'pat_bull_marubozu',
        'pat_bull_engulf', 'pat_bull_harami', 'pat_tweezer_bottom',
        'pat_3_white_soldiers', 'pat_morning_star',
    ]
    bearish_cols = [
        'pat_shooting_star', 'pat_bear_marubozu',
        'pat_bear_engulf', 'pat_bear_harami', 'pat_tweezer_top',
        'pat_3_black_crows', 'pat_evening_star',
    ]

    df['pat_bull_score'] = df[[c for c in bullish_cols
                                if c in df.columns]].sum(axis=1)
    df['pat_bear_score'] = df[[c for c in bearish_cols
                                if c in df.columns]].abs().sum(axis=1)
    df['pat_net_score']  = df['pat_bull_score'] - df['pat_bear_score']

    return df


# ══════════════════════════════════════════════════════════════
# 3. Pivot Points
# ══════════════════════════════════════════════════════════════
def _add_pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    """
    Pivot Points — แนวรับแนวต้านที่คำนวณจากแท่งก่อนหน้า
    นักเทรดสถาบันใช้ระดับเหล่านี้มาก
    ทองและ Forex ตอบสนองกับ Pivot ดีมาก

    Classic Pivot:
    P  = (H + L + C) / 3
    R1 = 2P - L    | S1 = 2P - H
    R2 = P + (H-L) | S2 = P - (H-L)
    R3 = H + 2(P-L)| S3 = L - 2(H-P)
    """
    # ใช้ค่าของวันก่อนหน้า (shift 1)
    h = df['high'].shift(1)
    l = df['low'].shift(1)
    c = df['close'].shift(1)

    # ── Classic Pivot ──────────────────────────────────────────
    p = (h + l + c) / 3

    df['pivot']   = p
    df['pivot_r1']= 2 * p - l
    df['pivot_r2']= p + (h - l)
    df['pivot_r3']= h + 2 * (p - l)
    df['pivot_s1']= 2 * p - h
    df['pivot_s2']= p - (h - l)
    df['pivot_s3']= l - 2 * (h - p)

    # ── Camarilla Pivot (ดีสำหรับ intraday Gold) ──────────────
    # แนวรับ/ต้านอยู่ใกล้ราคามากกว่า Classic
    rng = h - l
    df['cam_r4'] = c + rng * 1.1 / 2
    df['cam_r3'] = c + rng * 1.1 / 4
    df['cam_s3'] = c - rng * 1.1 / 4
    df['cam_s4'] = c - rng * 1.1 / 2
    # ถ้าราคาออกนอก R4/S4 = breakout แรง เทรดตามได้

    # ── ระยะห่างจาก Pivot Levels (%) ──────────────────────────
    cur = df['close']

    df['dist_pivot_pct']   = (cur - df['pivot'])    / cur * 100
    df['dist_r1_pct']      = (df['pivot_r1'] - cur) / cur * 100
    df['dist_s1_pct']      = (cur - df['pivot_s1']) / cur * 100
    df['dist_r2_pct']      = (df['pivot_r2'] - cur) / cur * 100
    df['dist_s2_pct']      = (cur - df['pivot_s2']) / cur * 100

    # ── Nearest Level ─────────────────────────────────────────
    # ราคาอยู่ใกล้ level ไหนมากที่สุด
    levels = pd.DataFrame({
        'r2': df['pivot_r2'], 'r1': df['pivot_r1'],
        'p' : df['pivot'],
        's1': df['pivot_s1'], 's2': df['pivot_s2'],
    })
    dist_to_levels = levels.sub(cur, axis=0).abs()
    df['nearest_pivot_level'] = dist_to_levels.idxmin(axis=1)
    df['nearest_pivot_dist']  = dist_to_levels.min(axis=1) / cur * 100

    # ── Pivot Bounce ──────────────────────────────────────────
    tolerance = 0.0015   # 0.15% ถือว่า "แตะ" level

    df['near_r1'] = (df['dist_r1_pct'].abs() < tolerance * 100).astype(int)
    df['near_s1'] = (df['dist_s1_pct'].abs() < tolerance * 100).astype(int)
    df['near_r2'] = (df['dist_r2_pct'].abs() < tolerance * 100).astype(int)
    df['near_s2'] = (df['dist_s2_pct'].abs() < tolerance * 100).astype(int)

    return df


# ══════════════════════════════════════════════════════════════
# 4. Support / Resistance
# ══════════════════════════════════════════════════════════════
def _add_support_resistance(df: pd.DataFrame) -> pd.DataFrame:
    """
    แนวรับแนวต้านจากราคาย้อนหลัง

    Rolling High/Low — fractal S/R
    Round Numbers    — 2300, 2350, 2400 (Gold)
    Breakout Level   — ราคาทะลุ high/low เก่า
    """
    h = df['high']
    l = df['low']
    c = df['close']

    # ── Rolling High/Low ───────────────────────────────────────
    for period in [20, 50, 100, 200]:
        df[f'rolling_high_{period}'] = h.rolling(period).max()
        df[f'rolling_low_{period}']  = l.rolling(period).min()

        # ระยะห่างจาก high/low (%)
        df[f'dist_high_{period}_pct'] = (
            df[f'rolling_high_{period}'] - c
        ) / c * 100
        df[f'dist_low_{period}_pct']  = (
            c - df[f'rolling_low_{period}']
        ) / c * 100

        # ราคาใกล้ high/low ไหม
        df[f'near_high_{period}'] = (
            c >= df[f'rolling_high_{period}'] * 0.998
        ).astype(int)
        df[f'near_low_{period}'] = (
            c <= df[f'rolling_low_{period}'] * 1.002
        ).astype(int)

    # ── Breakout Detection ─────────────────────────────────────
    # ทะลุ high/low เก่า = momentum แรง
    df['breakout_up_20']  = (
        (c > df['rolling_high_20'].shift(1)) &
        (c.shift(1) <= df['rolling_high_20'].shift(2))
    ).astype(int)

    df['breakout_down_20'] = (
        (c < df['rolling_low_20'].shift(1)) &
        (c.shift(1) >= df['rolling_low_20'].shift(2))
    ).astype(int)

    df['breakout_up_50']  = (
        c > df['rolling_high_50'].shift(1)
    ).astype(int)
    df['breakout_down_50'] = (
        c < df['rolling_low_50'].shift(1)
    ).astype(int)

    # ── Round Number Proximity (Gold) ─────────────────────────
    # XAUUSD: นักเทรดสนใจ 2300, 2350, 2400, 2450, 2500
    # ราคาใกล้เลขกลม = แนวรับ/ต้านทางจิตวิทยา
    round_level = 50   # ทุก $50 สำหรับ Gold

    nearest_round = (c / round_level).round() * round_level
    df['dist_round_num_pct'] = (c - nearest_round).abs() / c * 100
    df['near_round_number']  = (
        df['dist_round_num_pct'] < 0.2
    ).astype(int)   # ภายใน 0.2% ของเลขกลม

    # ── Price vs Key Levels ────────────────────────────────────
    # อยู่เหนือ/ใต้ ทุก rolling level หรือเปล่า
    df['above_all_highs'] = (
        (c > df['rolling_high_20']) &
        (c > df['rolling_high_50']) &
        (c > df['rolling_high_100'])
    ).astype(int)   # all-time breakout range

    df['below_all_lows'] = (
        (c < df['rolling_low_20']) &
        (c < df['rolling_low_50']) &
        (c < df['rolling_low_100'])
    ).astype(int)

    return df


# ══════════════════════════════════════════════════════════════
# 5. Price Position
# ══════════════════════════════════════════════════════════════
def _add_price_position(df: pd.DataFrame) -> pd.DataFrame:
    """
    ราคาอยู่ตรงไหนใน range ต่างๆ
    บอก context ของราคาปัจจุบัน
    """
    c = df['close']
    h = df['high']
    l = df['low']

    # Position ใน rolling range (0=ล่างสุด 1=บนสุด)
    for period in [20, 50, 100]:
        period_h = h.rolling(period).max()
        period_l = l.rolling(period).min()
        period_r = period_h - period_l + 1e-9

        df[f'price_pos_{period}'] = (c - period_l) / period_r
        # 0.0 = ราคาอยู่ที่ low สุดของ period นั้น
        # 0.5 = ราคาอยู่กลาง range
        # 1.0 = ราคาอยู่ที่ high สุด

    # Zone (แบ่งเป็น 5 ส่วน)
    df['price_zone_20'] = pd.cut(
        df['price_pos_20'],
        bins   = [0, 0.2, 0.4, 0.6, 0.8, 1.0],
        labels = ['very_low','low','mid','high','very_high'],
    ).astype(str)

    # ── Intraday Range ─────────────────────────────────────────
    # ราคาเดินทางไปแล้วกี่ % ของ ATR วันนั้น
    if 'atr_14' in df.columns:
        daily_move      = (c - c.shift(1)).abs()
        df['range_used_pct'] = daily_move / (df['atr_14'] + 1e-9) * 100
        # > 100% = ราคาเดินมากกว่า ATR แล้ว (อาจหยุดพัก)
        # < 50%  = ยังมีแรงเหลืออยู่

    # ── Gap Detection ──────────────────────────────────────────
    prev_c = c.shift(1)
    prev_h = h.shift(1)
    prev_l = l.shift(1)

    df['gap_up']   = (
        (l > prev_h) & ((l - prev_h) / prev_h > 0.001)
    ).astype(int)
    df['gap_down'] = (
        (h < prev_l) & ((prev_l - h) / prev_l > 0.001)
    ).astype(int)

    df['gap_size_pct'] = pd.Series(np.where(
        df['gap_up']   == 1, (l - prev_h) / prev_h * 100,
        np.where(
        df['gap_down'] == 1, (prev_l - h) / prev_l * -100,
        0)
    ), index=df.index)

    return df


# ══════════════════════════════════════════════════════════════
# 6. Market Structure
# ══════════════════════════════════════════════════════════════
def _add_market_structure(df: pd.DataFrame) -> pd.DataFrame:
    """
    Market Structure — ลำดับ High/Low บอก trend ระยะกลาง

    Uptrend    = Higher High (HH) + Higher Low (HL)
    Downtrend  = Lower High (LH)  + Lower Low (LL)
    Sideways   = Mixed

    Break of Structure (BOS) = ราคาทะลุ swing high/low เก่า
    → สัญญาณ trend เปลี่ยน ที่ Smart Money ใช้
    """
    h = df['high']
    l = df['low']
    c = df['close']

    # ── Swing High/Low ─────────────────────────────────────────
    # Swing High = high สูงกว่าทั้ง 2 ข้าง (n bars)
    n = 5   # lookback สำหรับ swing

    swing_high_mask = (h == h.rolling(n*2+1, center=True).max())
    swing_low_mask  = (l == l.rolling(n*2+1, center=True).min())

    df['is_swing_high'] = swing_high_mask.astype(int)
    df['is_swing_low']  = swing_low_mask.astype(int)

    # ── HH/HL/LH/LL ───────────────────────────────────────────
    swing_highs = h[swing_high_mask]
    swing_lows  = l[swing_low_mask]

    # Higher High
    prev_sh = swing_highs.shift(1).reindex(df.index).ffill()
    df['is_hh'] = (
        swing_high_mask & (h > prev_sh)
    ).astype(int)

    # Lower Low
    prev_sl = swing_lows.shift(1).reindex(df.index).ffill()
    df['is_ll'] = (
        swing_low_mask & (l < prev_sl)
    ).astype(int)

    # Higher Low
    prev_sl2 = swing_lows.shift(1).reindex(df.index).ffill()
    df['is_hl'] = (
        swing_low_mask & (l > prev_sl2)
    ).astype(int)

    # Lower High
    prev_sh2 = swing_highs.shift(1).reindex(df.index).ffill()
    df['is_lh'] = (
        swing_high_mask & (h < prev_sh2)
    ).astype(int)

    # ── Market Structure Score ─────────────────────────────────
    # นับ HH/HL ใน 20 bars ล่าสุด
    df['hh_count_20'] = df['is_hh'].rolling(20).sum()
    df['hl_count_20'] = df['is_hl'].rolling(20).sum()
    df['ll_count_20'] = df['is_ll'].rolling(20).sum()
    df['lh_count_20'] = df['is_lh'].rolling(20).sum()

    df['structure_bull'] = df['hh_count_20'] + df['hl_count_20']
    df['structure_bear'] = df['ll_count_20'] + df['lh_count_20']
    df['structure_score']= df['structure_bull'] - df['structure_bear']

    # ── Break of Structure (BOS) ───────────────────────────────
    last_swing_high = h[swing_high_mask].reindex(df.index).ffill()
    last_swing_low  = l[swing_low_mask].reindex(df.index).ffill()

    df['bos_bull'] = (
        (c > last_swing_high.shift(1)) &
        (c.shift(1) <= last_swing_high.shift(2))
    ).astype(int)   # ทะลุ swing high เก่า = bullish BOS

    df['bos_bear'] = (
        (c < last_swing_low.shift(1)) &
        (c.shift(1) >= last_swing_low.shift(2))
    ).astype(int)   # ทะลุ swing low เก่า = bearish BOS

    return df


# ══════════════════════════════════════════════════════════════
# 7. Price Action Composite
# ══════════════════════════════════════════════════════════════
def _add_pa_composite(df: pd.DataFrame) -> pd.DataFrame:
    """
    รวมสัญญาณ price action ทั้งหมดเป็นคะแนนเดียว
    range: -5 ถึง +5
    """
    score = pd.Series(0.0, index=df.index)

    # Pattern score (น้ำหนัก 1.5)
    if 'pat_net_score' in df.columns:
        score += df['pat_net_score'].clip(-2, 2) * 1.5

    # Market structure (น้ำหนัก 1.0)
    if 'structure_score' in df.columns:
        score += df['structure_score'].clip(-3, 3) / 3

    # BOS (น้ำหนัก 2.0 — สำคัญมาก)
    if 'bos_bull' in df.columns:
        score += df['bos_bull'] * 2
        score -= df['bos_bear'] * 2

    # Breakout (น้ำหนัก 1.0)
    if 'breakout_up_20' in df.columns:
        score += df['breakout_up_20']
        score -= df['breakout_down_20']

    # Close position (น้ำหนัก 0.5)
    if 'close_position' in df.columns:
        score += (df['close_position'] - 0.5) * 0.5

    df['pa_score'] = score.clip(-5, 5)

    return df