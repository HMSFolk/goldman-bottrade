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
    o, h, l, c = df['open'], df['high'], df['low'], df['close']
    features = {}

    # ── ขนาดพื้นฐาน ───────────────────────────────────────────
    features['candle_body']        = (c - o).abs()
    features['candle_range']       = h - l
    features['candle_upper_wick']  = h - df[['open','close']].max(axis=1)
    features['candle_lower_wick']  = df[['open','close']].min(axis=1) - l

    # ── สัดส่วน (normalize) ───────────────────────────────────
    rng = features['candle_range'] + 1e-9   # ป้องกัน division by zero

    features['body_ratio']        = features['candle_body'] / rng
    features['upper_wick_ratio']  = features['candle_upper_wick'] / rng
    features['lower_wick_ratio']  = features['candle_lower_wick'] / rng

    # ── ทิศทาง ────────────────────────────────────────────────
    features['is_bullish']        = (c > o).astype(int)
    features['is_bearish']        = (c < o).astype(int)
    features['is_neutral']        = (c == o).astype(int)

    # ── ขนาดเทียบค่าเฉลี่ย ────────────────────────────────────
    avg_range = features['candle_range'].rolling(20).mean()
    features['relative_range']    = features['candle_range'] / (avg_range + 1e-9)

    features['is_big_candle']     = (features['relative_range'] > 1.5).astype(int)
    features['is_small_candle']   = (features['relative_range'] < 0.5).astype(int)

    # ── Close Position ────────────────────────────────────────
    features['close_position']    = (c - l) / rng

    return pd.concat([df, pd.DataFrame(features, index=df.index)], axis=1)


# ══════════════════════════════════════════════════════════════
# 2. Candle Patterns
# ══════════════════════════════════════════════════════════════
def _add_candle_patterns(df: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c = df['open'], df['high'], df['low'], df['close']
    body        = df['candle_body']
    rng         = df['candle_range'] + 1e-9
    upper_wick  = df['candle_upper_wick']
    lower_wick  = df['candle_lower_wick']
    avg_body    = body.rolling(10).mean()

    features = {}

    # ── Single Candle Patterns ─────────────────────────────────
    features['pat_doji'] = (body < avg_body * 0.1).astype(int)

    is_hammer_shape = (lower_wick > body * 2.0) & (upper_wick < body * 0.5) & (body > 0)
    features['pat_hammer']    = (is_hammer_shape & (c > o)).astype(int) * 1
    features['pat_hang_man']  = (is_hammer_shape & (c < o)).astype(int) * -1

    is_star_shape = (upper_wick > body * 2.0) & (lower_wick < body * 0.5) & (body > 0)
    features['pat_shooting_star'] = (is_star_shape & (c < o)).astype(int) * -1
    features['pat_inv_hammer']    = (is_star_shape & (c > o)).astype(int) * 1

    features['pat_bull_marubozu'] = ((c > o) & (body / rng > 0.85) & 
                                     (upper_wick / rng < 0.05) & (lower_wick / rng < 0.05)).astype(int)
    features['pat_bear_marubozu'] = ((c < o) & (body / rng > 0.85) & 
                                     (upper_wick / rng < 0.05) & (lower_wick / rng < 0.05)).astype(int) * -1

    features['pat_spinning_top'] = ((body / rng < 0.3) & (upper_wick / rng > 0.2) & 
                                    (lower_wick / rng > 0.2)).astype(int)

    # ── Two-Candle Patterns ────────────────────────────────────
    prev_o, prev_c, prev_body = o.shift(1), c.shift(1), body.shift(1)

    features['pat_bull_engulf'] = ((c > o) & (prev_c < prev_o) & (o <= prev_c) & 
                                   (c >= prev_o) & (body > prev_body * 0.8)).astype(int)
    features['pat_bear_engulf'] = ((c < o) & (prev_c > prev_o) & (o >= prev_c) & 
                                   (c <= prev_o) & (body > prev_body * 0.8)).astype(int) * -1

    features['pat_bull_harami'] = ((c > o) & (prev_c < prev_o) & (o > prev_c) & (o < prev_o) & 
                                   (c > prev_c) & (c < prev_o) & (body < prev_body * 0.6)).astype(int)
    features['pat_bear_harami'] = ((c < o) & (prev_c > prev_o) & (o < prev_c) & (o > prev_o) & 
                                   (c < prev_c) & (c > prev_o) & (body < prev_body * 0.6)).astype(int) * -1

    features['pat_tweezer_bottom'] = ((l.round(1) == l.shift(1).round(1)) & (c < o).shift(1) & (c > o)).astype(int)
    features['pat_tweezer_top']    = ((h.round(1) == h.shift(1).round(1)) & (c > o).shift(1) & (c < o)).astype(int) * -1

    # ── Three-Candle Patterns ──────────────────────────────────
    prev2_c, prev2_o = c.shift(2), o.shift(2)

    features['pat_3_white_soldiers'] = ((c > o) & (prev_c > prev_o) & (prev2_c > prev2_o) & 
                                        (c > prev_c) & (prev_c > prev2_c) & (df['body_ratio'] > 0.6) & 
                                        (df['body_ratio'].shift(1) > 0.6) & (df['body_ratio'].shift(2) > 0.6)).astype(int)

    features['pat_3_black_crows'] = ((c < o) & (prev_c < prev_o) & (prev2_c < prev2_o) & 
                                     (c < prev_c) & (prev_c < prev2_c) & (df['body_ratio'] > 0.6) & 
                                     (df['body_ratio'].shift(1) > 0.6) & (df['body_ratio'].shift(2) > 0.6)).astype(int) * -1

    features['pat_morning_star'] = ((prev2_c < prev2_o) & (features['pat_doji'].shift(1) == 1) & 
                                    (c > o) & (c > (prev2_o + prev2_c) / 2)).astype(int)
    features['pat_evening_star'] = ((prev2_c > prev2_o) & (features['pat_doji'].shift(1) == 1) & 
                                    (c < o) & (c < (prev2_o + prev2_c) / 2)).astype(int) * -1

    # ── Pattern Strength Score ─────────────────────────────────
    bullish_cols = ['pat_hammer', 'pat_inv_hammer', 'pat_bull_marubozu', 'pat_bull_engulf', 'pat_bull_harami', 'pat_tweezer_bottom', 'pat_3_white_soldiers', 'pat_morning_star']
    bearish_cols = ['pat_shooting_star', 'pat_bear_marubozu', 'pat_bear_engulf', 'pat_bear_harami', 'pat_tweezer_top', 'pat_3_black_crows', 'pat_evening_star']

    features['pat_bull_score'] = sum(features[col] for col in bullish_cols if col in features)
    features['pat_bear_score'] = sum(features[col].abs() for col in bearish_cols if col in features)
    features['pat_net_score']  = features['pat_bull_score'] - features['pat_bear_score']

    return pd.concat([df, pd.DataFrame(features, index=df.index)], axis=1)


# ══════════════════════════════════════════════════════════════
# 3. Pivot Points
# ══════════════════════════════════════════════════════════════
def _add_pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    h, l, c = df['high'].shift(1), df['low'].shift(1), df['close'].shift(1)
    cur = df['close']
    features = {}

    p = (h + l + c) / 3
    features['pivot']    = p
    features['pivot_r1'] = 2 * p - l
    features['pivot_r2'] = p + (h - l)
    features['pivot_r3'] = h + 2 * (p - l)
    features['pivot_s1'] = 2 * p - h
    features['pivot_s2'] = p - (h - l)
    features['pivot_s3'] = l - 2 * (h - p)

    rng = h - l
    features['cam_r4'] = c + rng * 1.1 / 2
    features['cam_r3'] = c + rng * 1.1 / 4
    features['cam_s3'] = c - rng * 1.1 / 4
    features['cam_s4'] = c - rng * 1.1 / 2

    features['dist_pivot_pct'] = (cur - p) / cur * 100
    features['dist_r1_pct']    = (features['pivot_r1'] - cur) / cur * 100
    features['dist_s1_pct']    = (cur - features['pivot_s1']) / cur * 100
    features['dist_r2_pct']    = (features['pivot_r2'] - cur) / cur * 100
    features['dist_s2_pct']    = (cur - features['pivot_s2']) / cur * 100

    levels = pd.DataFrame({
        'r2': features['pivot_r2'], 'r1': features['pivot_r1'],
        'p' : features['pivot'],
        's1': features['pivot_s1'], 's2': features['pivot_s2'],
    })
    dist_to_levels = levels.sub(cur, axis=0).abs()
    
    # แก้อาการ FutureWarning
    features['nearest_pivot_level'] = dist_to_levels.idxmin(axis=1, skipna=True)
    features['nearest_pivot_dist']  = dist_to_levels.min(axis=1) / cur * 100

    tolerance = 0.0015
    features['near_r1'] = (features['dist_r1_pct'].abs() < tolerance * 100).astype(int)
    features['near_s1'] = (features['dist_s1_pct'].abs() < tolerance * 100).astype(int)
    features['near_r2'] = (features['dist_r2_pct'].abs() < tolerance * 100).astype(int)
    features['near_s2'] = (features['dist_s2_pct'].abs() < tolerance * 100).astype(int)

    return pd.concat([df, pd.DataFrame(features, index=df.index)], axis=1)


# ══════════════════════════════════════════════════════════════
# 4. Support / Resistance
# ══════════════════════════════════════════════════════════════
def _add_support_resistance(df: pd.DataFrame) -> pd.DataFrame:
    h, l, c = df['high'], df['low'], df['close']
    features = {}

    for period in [20, 50, 100, 200]:
        roll_h = h.rolling(period).max()
        roll_l = l.rolling(period).min()
        features[f'rolling_high_{period}'] = roll_h
        features[f'rolling_low_{period}']  = roll_l

        features[f'dist_high_{period}_pct'] = (roll_h - c) / c * 100
        features[f'dist_low_{period}_pct']  = (c - roll_l) / c * 100

        features[f'near_high_{period}'] = (c >= roll_h * 0.998).astype(int)
        features[f'near_low_{period}']  = (c <= roll_l * 1.002).astype(int)

    features['breakout_up_20']   = ((c > features['rolling_high_20'].shift(1)) & (c.shift(1) <= features['rolling_high_20'].shift(2))).astype(int)
    features['breakout_down_20'] = ((c < features['rolling_low_20'].shift(1)) & (c.shift(1) >= features['rolling_low_20'].shift(2))).astype(int)
    features['breakout_up_50']   = (c > features['rolling_high_50'].shift(1)).astype(int)
    features['breakout_down_50'] = (c < features['rolling_low_50'].shift(1)).astype(int)

    round_level = 50
    nearest_round = (c / round_level).round() * round_level
    features['dist_round_num_pct'] = (c - nearest_round).abs() / c * 100
    features['near_round_number']  = (features['dist_round_num_pct'] < 0.2).astype(int)

    features['above_all_highs'] = ((c > features['rolling_high_20']) & (c > features['rolling_high_50']) & (c > features['rolling_high_100'])).astype(int)
    features['below_all_lows']  = ((c < features['rolling_low_20']) & (c < features['rolling_low_50']) & (c < features['rolling_low_100'])).astype(int)

    return pd.concat([df, pd.DataFrame(features, index=df.index)], axis=1)


# ══════════════════════════════════════════════════════════════
# 5. Price Position
# ══════════════════════════════════════════════════════════════
def _add_price_position(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l = df['close'], df['high'], df['low']
    features = {}

    for period in [20, 50, 100]:
        period_h, period_l = h.rolling(period).max(), l.rolling(period).min()
        period_r = period_h - period_l + 1e-9
        features[f'price_pos_{period}'] = (c - period_l) / period_r

    features['price_zone_20'] = pd.cut(
        features['price_pos_20'],
        bins   = [0, 0.2, 0.4, 0.6, 0.8, 1.0],
        labels = ['very_low','low','mid','high','very_high'],
    ).astype(str)

    if 'atr_14' in df.columns:
        daily_move = (c - c.shift(1)).abs()
        features['range_used_pct'] = daily_move / (df['atr_14'] + 1e-9) * 100

    prev_c, prev_h, prev_l = c.shift(1), h.shift(1), l.shift(1)

    features['gap_up']   = ((l > prev_h) & ((l - prev_h) / prev_h > 0.001)).astype(int)
    features['gap_down'] = ((h < prev_l) & ((prev_l - h) / prev_l > 0.001)).astype(int)

    features['gap_size_pct'] = pd.Series(np.where(
        features['gap_up'] == 1, (l - prev_h) / prev_h * 100,
        np.where(features['gap_down'] == 1, (prev_l - h) / prev_l * -100, 0)
    ), index=df.index)

    return pd.concat([df, pd.DataFrame(features, index=df.index)], axis=1)


# ══════════════════════════════════════════════════════════════
# 6. Market Structure
# ══════════════════════════════════════════════════════════════
def _add_market_structure(df: pd.DataFrame) -> pd.DataFrame:
    h, l, c = df['high'], df['low'], df['close']
    features = {}

    n = 5
    swing_high_mask = (h == h.rolling(n*2+1, center=True).max())
    swing_low_mask  = (l == l.rolling(n*2+1, center=True).min())

    features['is_swing_high'] = swing_high_mask.astype(int)
    features['is_swing_low']  = swing_low_mask.astype(int)

    swing_highs = h[swing_high_mask]
    swing_lows  = l[swing_low_mask]

    prev_sh = swing_highs.shift(1).reindex(df.index).ffill()
    features['is_hh'] = (swing_high_mask & (h > prev_sh)).astype(int)

    prev_sl = swing_lows.shift(1).reindex(df.index).ffill()
    features['is_ll'] = (swing_low_mask & (l < prev_sl)).astype(int)

    prev_sl2 = swing_lows.shift(1).reindex(df.index).ffill()
    features['is_hl'] = (swing_low_mask & (l > prev_sl2)).astype(int)

    prev_sh2 = swing_highs.shift(1).reindex(df.index).ffill()
    features['is_lh'] = (swing_high_mask & (h < prev_sh2)).astype(int)

    features['hh_count_20'] = pd.Series(features['is_hh']).rolling(20).sum()
    features['hl_count_20'] = pd.Series(features['is_hl']).rolling(20).sum()
    features['ll_count_20'] = pd.Series(features['is_ll']).rolling(20).sum()
    features['lh_count_20'] = pd.Series(features['is_lh']).rolling(20).sum()

    features['structure_bull'] = features['hh_count_20'] + features['hl_count_20']
    features['structure_bear'] = features['ll_count_20'] + features['lh_count_20']
    features['structure_score']= features['structure_bull'] - features['structure_bear']

    last_swing_high = h[swing_high_mask].reindex(df.index).ffill()
    last_swing_low  = l[swing_low_mask].reindex(df.index).ffill()

    features['bos_bull'] = ((c > last_swing_high.shift(1)) & (c.shift(1) <= last_swing_high.shift(2))).astype(int)
    features['bos_bear'] = ((c < last_swing_low.shift(1)) & (c.shift(1) >= last_swing_low.shift(2))).astype(int)

    return pd.concat([df, pd.DataFrame(features, index=df.index)], axis=1)


# ══════════════════════════════════════════════════════════════
# 7. Price Action Composite
# ══════════════════════════════════════════════════════════════
def _add_pa_composite(df: pd.DataFrame) -> pd.DataFrame:
    score = pd.Series(0.0, index=df.index)

    if 'pat_net_score' in df.columns:
        score += df['pat_net_score'].clip(-2, 2) * 1.5

    if 'structure_score' in df.columns:
        score += df['structure_score'].clip(-3, 3) / 3

    if 'bos_bull' in df.columns:
        score += df['bos_bull'] * 2
        score -= df['bos_bear'] * 2

    if 'breakout_up_20' in df.columns:
        score += df['breakout_up_20']
        score -= df['breakout_down_20']

    if 'close_position' in df.columns:
        score += (df['close_position'] - 0.5) * 0.5

    features = {'pa_score': score.clip(-5, 5)}
    return pd.concat([df, pd.DataFrame(features, index=df.index)], axis=1)