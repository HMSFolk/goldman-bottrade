# features/regime.py
"""
Market Regime Detection — แยกตลาดว่ากำลัง trending หรือ ranging
=================================================================
ใช้ 4 indicator ร่วมกัน เพื่อลด false signal:

  1. ADX  — ความแรงของ trend (>25 = trending, <20 = ranging)
  2. Hurst Exponent — persistence ทางสถิติ (>0.55 trending, <0.45 mean-revert)
  3. ATR Ratio — ระดับ volatility เทียบ historical
  4. EMA Direction — ทิศทาง (price vs EMA20/50/200)

Regime Output:
  trending_up   → ใช้ trend-following strategy, weight LGBM/XGB สูง
  trending_down → ใช้ trend-following strategy, weight LGBM/XGB สูง
  ranging       → ใช้ mean-reversion, weight rule-based สูง
  volatile      → high uncertainty, require higher confidence threshold
  uncertain     → neutral, ใช้ default weights

ใช้งาน:
  from features.regime import RegimeDetector
  detector = RegimeDetector()
  regime   = detector.detect(df_ohlcv)
  print(regime)   # trending_up | strength=72 | hurst=0.63 | adx=31.4
"""

import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import get_config, resolve_symbol   # ✅ NEW: broker resolver

CFG = get_config()


# ══════════════════════════════════════════════════════════════
# Data Structures
# ══════════════════════════════════════════════════════════════

@dataclass
class RegimeState:
    """
    สถานะ regime ของตลาด ณ เวลาหนึ่ง

    Attributes:
        regime     : "trending_up" | "trending_down" | "ranging"
                     "volatile" | "uncertain"
        strength   : 0-100 (ADX value — ยิ่งสูงยิ่ง trend ชัด)
        direction  : "up" | "down" | "flat"
        volatility : "low" | "medium" | "high"
        hurst      : 0.0-1.0  (>0.55 = persistent/trending)
        adx        : ADX value (0-100)
        confidence : 0.0-1.0  (ความมั่นใจของ classification)
        strategy   : recommended strategy for this regime
        symbol     : symbol ที่ detect
        timeframe  : timeframe ที่ใช้ detect
        detected_at: UTC timestamp
    """
    regime      : str   = "uncertain"
    strength    : float = 0.0
    direction   : str   = "flat"
    volatility  : str   = "medium"
    hurst       : float = 0.5
    adx         : float = 0.0
    confidence  : float = 0.0
    strategy    : str   = "default"    # "trend_follow"|"mean_revert"|"high_conf_only"|"default"
    symbol      : str   = ""
    timeframe   : str   = "M15"
    detected_at : str   = ""

    @property
    def is_trending(self) -> bool:
        return self.regime in ("trending_up", "trending_down")

    @property
    def is_ranging(self) -> bool:
        return self.regime == "ranging"

    @property
    def is_volatile(self) -> bool:
        return self.regime == "volatile"

    @property
    def should_trade(self) -> bool:
        """False ถ้าควร skip การเทรด (uncertain + low confidence)"""
        if self.regime == "volatile" and self.confidence < 0.3:
            return False
        return True

    def __str__(self) -> str:
        icons = {
            "trending_up"  : "📈",
            "trending_down": "📉",
            "ranging"      : "↔️",
            "volatile"     : "⚡",
            "uncertain"    : "❓",
        }
        return (
            f"{icons.get(self.regime,'?')} {self.regime} | "
            f"strength={self.strength:.0f} | "
            f"hurst={self.hurst:.3f} | "
            f"adx={self.adx:.1f} | "
            f"vol={self.volatility} | "
            f"conf={self.confidence:.2f}"
        )

    def to_dict(self) -> dict:
        return {
            "regime"     : self.regime,
            "strength"   : round(self.strength,   2),
            "direction"  : self.direction,
            "volatility" : self.volatility,
            "hurst"      : round(self.hurst,      4),
            "adx"        : round(self.adx,        2),
            "confidence" : round(self.confidence, 3),
            "strategy"   : self.strategy,
            "symbol"     : self.symbol,
            "timeframe"  : self.timeframe,
            "detected_at": self.detected_at,
        }


# ══════════════════════════════════════════════════════════════
# Regime Detector
# ══════════════════════════════════════════════════════════════

class RegimeDetector:
    """
    ตรวจจับ market regime จาก OHLCV DataFrame

    Input DataFrame ต้องมี columns:
      open, high, low, close (ตัวพิมพ์เล็ก)
      index: DatetimeIndex
    """

    def __init__(self):
        cfg_r = CFG.get("regime_detection", {})

        # ── ADX settings ───────────────────────────────────────
        self.adx_period       = cfg_r.get("adx_period",        14)
        self.adx_trend_thresh = cfg_r.get("adx_trend_thresh",  22)   # > = trending
        self.adx_range_thresh = cfg_r.get("adx_range_thresh",  18)   # < = ranging

        # ── Hurst settings ─────────────────────────────────────
        self.hurst_max_lag     = cfg_r.get("hurst_max_lag",    20)
        self.hurst_trend_thresh= cfg_r.get("hurst_trend",      0.52) # > = persistent
        self.hurst_range_thresh= cfg_r.get("hurst_range",      0.48) # < = mean-revert

        # ── Volatility settings ────────────────────────────────
        self.atr_period    = cfg_r.get("atr_period",       14)
        self.atr_lookback  = cfg_r.get("atr_lookback",    100)   # historical ATR window
        self.vol_high_mult = cfg_r.get("vol_high_mult",   1.5)   # current/hist > 1.5 = high vol
        self.vol_low_mult  = cfg_r.get("vol_low_mult",    0.7)   # current/hist < 0.7 = low vol

        # ── EMA settings ───────────────────────────────────────
        self.ema_fast   = cfg_r.get("ema_fast",   20)
        self.ema_slow   = cfg_r.get("ema_slow",   50)
        self.ema_trend  = cfg_r.get("ema_trend", 200)

        # ── Min data requirement ───────────────────────────────
        self.min_bars = max(self.atr_lookback, self.ema_trend) + 20

        # ── Cache ──────────────────────────────────────────────
        self._cache: dict[str, tuple] = {}   # symbol → (timestamp, RegimeState)
        self._cache_ttl_minutes = cfg_r.get("cache_ttl_minutes", 15)

        # ── Hysteresis (กัน regime กระพริบ) ────────────────────
        # ต้องเห็น regime ใหม่ติดกัน N ครั้งก่อนยอมเปลี่ยน label
        self._hysteresis_n      = max(1, int(cfg_r.get("hysteresis_confirm", 3)))
        self._regime_history: dict[str, list[str]]   = {}   # symbol → raw regimes ล่าสุด
        self._smoothed_regime: dict[str, RegimeState] = {}   # symbol → regime ที่ใช้อยู่จริง

    # ══════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════

    def detect(
        self,
        df        : pd.DataFrame,
        symbol    : str = "",
        timeframe : str = "M15",
    ) -> RegimeState:
        """
        ตรวจ regime จาก OHLCV DataFrame

        Args:
            df       : DataFrame ที่มี open/high/low/close columns
            symbol   : ชื่อ symbol (สำหรับ cache key และ logging)
            timeframe: M15, H1, H4, D1 (สำหรับ reference เท่านั้น)

        Returns:
            RegimeState — สถานะ regime ปัจจุบัน
        """
        # ── Cache check ────────────────────────────────────────
        cached = self._get_from_cache(symbol)
        if cached:
            return cached

        # ── Validate data ──────────────────────────────────────
        if df is None or len(df) < self.min_bars:
            state = RegimeState(
                regime    = "uncertain",
                confidence= 0.0,
                symbol    = symbol,
                timeframe = timeframe,
                detected_at = datetime.now(timezone.utc).isoformat(),
            )
            self._set_cache(symbol, state)
            return state

        # ── ต้องมี column ที่ถูกต้อง ──────────────────────────
        df = self._normalize_columns(df)

        # ── คำนวณ indicators ──────────────────────────────────
        adx       = self._calc_adx(df)
        di_plus, di_minus = self._calc_di(df)
        direction = self._calc_direction(df)
        hurst     = self._calc_hurst(df["close"].values)
        vol_label, vol_ratio = self._calc_volatility(df)

        # ── Classify ───────────────────────────────────────────
        regime, confidence = self._classify(
            adx=adx, hurst=hurst,
            direction=direction, vol_label=vol_label,
            di_plus=di_plus, di_minus=di_minus,
        )

        # ── Strategy recommendation ────────────────────────────
        strategy = self._recommend_strategy(regime, confidence, vol_label)

        state = RegimeState(
            regime      = regime,
            strength    = round(float(adx), 2),
            direction   = direction,
            volatility  = vol_label,
            hurst       = round(float(hurst), 4),
            adx         = round(float(adx), 2),
            confidence  = round(float(confidence), 3),
            strategy    = strategy,
            symbol      = symbol,
            timeframe   = timeframe,
            detected_at = datetime.now(timezone.utc).isoformat(),
        )

        # ── Hysteresis: กัน label กระพริบก่อนส่งออก/cache ──────
        state = self._apply_hysteresis(symbol, state)

        self._set_cache(symbol, state)
        return state

    def _apply_hysteresis(
        self, symbol: str, new_state: RegimeState
    ) -> RegimeState:
        """
        กัน regime กระพริบ (เช่น trending_down ↔ uncertain ทุก tick)

        หลักการ: คง regime เดิม (sticky) จนกว่าจะเห็น regime ใหม่
        ติดกันครบ N ครั้ง (hysteresis_confirm) จึงยอมเปลี่ยน label
        — ป้องกัน HARD BLOCK รั่วตอน label เด้งเป็น uncertain ชั่วขณะ

        ตัวเลข indicator (adx/hurst/strength) ยัง refresh เป็นค่าสดเสมอ
        เปลี่ยนแค่ field `regime`/`confidence` ที่ถูกถือไว้
        """
        hist = self._regime_history.setdefault(symbol, [])
        hist.append(new_state.regime)
        if len(hist) > self._hysteresis_n:
            hist.pop(0)

        prev = self._smoothed_regime.get(symbol)

        # ครั้งแรก หรือ regime ใหม่ตรงกับที่ใช้อยู่ → ยอมรับเลย (refresh ค่าสด)
        if prev is None or new_state.regime == prev.regime:
            self._smoothed_regime[symbol] = new_state
            return new_state

        # regime ต่างจากเดิม → เปลี่ยนได้ต่อเมื่อเห็นค่าใหม่ติดกันครบ N ครั้ง
        if len(hist) >= self._hysteresis_n and all(
            r == new_state.regime for r in hist
        ):
            self._smoothed_regime[symbol] = new_state
            return new_state

        # ยังไม่ครบ → คง regime เดิม แต่ refresh ตัวเลข indicator ให้สด
        held = RegimeState(
            regime      = prev.regime,
            strength    = new_state.strength,
            direction   = new_state.direction,
            volatility  = new_state.volatility,
            hurst       = new_state.hurst,
            adx         = new_state.adx,
            confidence  = prev.confidence,
            strategy    = prev.strategy,
            symbol      = symbol,
            timeframe   = new_state.timeframe,
            detected_at = new_state.detected_at,
        )
        return held

    def detect_from_mt5(
        self,
        symbol    : str,
        timeframe : str = "H4",
        bars      : int = 300, ) -> RegimeState:
        """
        ดึงข้อมูลจาก MT5 แล้ว detect (ใช้ H4 เพื่อ big picture)

        Args:
            timeframe: แนะนำ H4 หรือ D1 สำหรับ regime detection
                       (M15 อาจ noisy เกินไป)
        """
        try:
            import MetaTrader5 as mt5

            tf_map = {
                "M15" : mt5.TIMEFRAME_M15,
                "H1"  : mt5.TIMEFRAME_H1,
                "H4"  : mt5.TIMEFRAME_H4,
                "D1"  : mt5.TIMEFRAME_D1,
            }
            tf = tf_map.get(timeframe, mt5.TIMEFRAME_H4)

            # ✅ FIX CRITICAL: symbol เป็นชื่อกลาง (XAUUSD) ต้อง resolve
            # เป็นชื่อ broker (XAUUSDm) ก่อนเรียก MT5 — เดิมส่ง symbol
            # ตรงๆ ทำให้ MT5 หาไม่เจอ rates=None → regime ออกมา
            # "uncertain conf=0.00" ทุกครั้ง (ไม่ error ให้เห็นเลยด้วย)
            mt5_symbol = resolve_symbol(symbol)
            rates = mt5.copy_rates_from_pos(mt5_symbol, tf, 0, bars)
            if rates is None or len(rates) == 0:
                return RegimeState(symbol=symbol, regime="uncertain",
                                   confidence=0.0)

            df = pd.DataFrame(rates)
            df["time"]  = pd.to_datetime(df["time"], unit="s")
            df = df.rename(columns={
                "time": "datetime", "tick_volume": "volume"
            }).set_index("datetime")

            return self.detect(df, symbol=symbol, timeframe=timeframe)

        except Exception as e:
            import logging
            logging.getLogger("features.regime").error(
                f"detect_from_mt5 error [{symbol}]: {e}"
            )
            return RegimeState(symbol=symbol, regime="uncertain", confidence=0.0)

    def detect_from_parquet(
        self,
        symbol   : str,
        timeframe: str = "M15",
    ) -> RegimeState:
        """โหลดจาก processed parquet แล้ว detect"""
        try:
            path = (
                _ROOT / "data" / "processed"
                / f"{symbol}_{timeframe}_features.parquet"
            )
            if not path.exists():
                return RegimeState(symbol=symbol, regime="uncertain", confidence=0.0)

            df = pd.read_parquet(path)
            return self.detect(df, symbol=symbol, timeframe=timeframe)

        except Exception as e:
            import logging
            logging.getLogger("features.regime").error(
                f"detect_from_parquet error [{symbol}]: {e}"
            )
            return RegimeState(symbol=symbol, regime="uncertain", confidence=0.0)

    # ══════════════════════════════════════════════════════════
    # Indicator Calculations
    # ══════════════════════════════════════════════════════════

    def _calc_adx(self, df: pd.DataFrame) -> float:
        """
        Average Directional Index (ADX)
        วัดความแรงของ trend โดยไม่บอกทิศทาง
        > 25 = trending strongly
        20-25 = weak trend / transitioning
        < 20  = ranging / no trend
        """
        try:
            import ta
            adx_ind = ta.trend.ADXIndicator(
                high=df["high"], 
                low=df["low"], 
                close=df["close"], 
                window=self.adx_period,
                fillna=True
            )
            adx_series = adx_ind.adx()
            return float(adx_series.iloc[-1]) if not adx_series.empty else 0.0
        except ImportError:
            # Fallback หากไม่มี ta library ให้ return ค่า 0
            import logging
            logging.getLogger("features.regime").error(
                "Missing 'ta' library for ADX calculation. Run: pip install ta"
            )
            return 0.0

    def _calc_di(self, df: pd.DataFrame) -> tuple:
        """คืน (DI+, DI-) สำหรับ confirm direction"""
        high  = df["high"].values.astype(float)
        low   = df["low"].values.astype(float)
        close = df["close"].values.astype(float)
        n     = self.adx_period

        tr       = np.maximum(high[1:] - low[1:],
                   np.maximum(np.abs(high[1:] - close[:-1]),
                              np.abs(low[1:]  - close[:-1])))
        up_move  = high[1:] - high[:-1]
        dn_move  = low[:-1] - low[1:]
        plus_dm  = np.where((up_move > dn_move) & (up_move > 0),   up_move,  0.0)
        minus_dm = np.where((dn_move > up_move) & (dn_move > 0),   dn_move,  0.0)

        def _ws(a, p):
            out = np.zeros(len(a))
            out[p - 1] = a[:p].sum()
            for i in range(p, len(a)):
                out[i] = out[i - 1] - out[i - 1] / p + a[i]
            return out

        atr_s    = _ws(tr,       n)
        pdi_s    = _ws(plus_dm,  n)
        mdi_s    = _ws(minus_dm, n)

        with np.errstate(divide="ignore", invalid="ignore"):
            di_plus  = float(np.where(atr_s > 0, 100 * pdi_s / atr_s, 0.0)[-1])
            di_minus = float(np.where(atr_s > 0, 100 * mdi_s / atr_s, 0.0)[-1])

        return di_plus, di_minus

    def _calc_hurst(self, close_arr: np.ndarray) -> float:
        """
        Hurst Exponent (Variance Scaling Method)

        H > 0.55: persistent / trending
        H ≈ 0.50: random walk (efficient market)
        H < 0.45: anti-persistent / mean-reverting (ranging)

        Method: slope of log(std(diff_tau)) vs log(tau)
        """
        try:
            close = np.log(close_arr[-200:] + 1e-10)  # log prices, last 200 bars
            lags  = range(2, min(self.hurst_max_lag, len(close) // 4))

            tau = [
                np.std(close[lag:] - close[:-lag])
                for lag in lags
            ]
            tau = np.array(tau)
            valid = tau > 0
            if valid.sum() < 3:
                return 0.5

            poly = np.polyfit(
                np.log(np.array(list(lags))[valid]),
                np.log(tau[valid]),
                1,
            )
            return float(np.clip(poly[0], 0.0, 1.0))

        except Exception:
            return 0.5   # neutral / unknown

    def _calc_direction(self, df: pd.DataFrame) -> str:
        """
        ทิศทาง price ตาม EMA stack
        up  : price > EMA20 > EMA50 > EMA200
        down: price < EMA20 < EMA50 < EMA200
        flat: otherwise
        """
        close = df["close"]
        ema20  = close.ewm(span=self.ema_fast,  adjust=False).mean()
        ema50  = close.ewm(span=self.ema_slow,  adjust=False).mean()
        ema200 = close.ewm(span=self.ema_trend, adjust=False).mean()

        c   = float(close.iloc[-1])
        e20 = float(ema20.iloc[-1])
        e50 = float(ema50.iloc[-1])
        e200= float(ema200.iloc[-1])

        if c > e20 > e50 > e200:
            return "up"
        if c < e20 < e50 < e200:
            return "down"
        # Partial alignment → check majority
        up_signals = sum([c > e20, e20 > e50, e50 > e200])
        if up_signals >= 3:
            return "up"
        if up_signals == 0:
            return "down"
        return "flat"

    def _calc_volatility(self, df: pd.DataFrame) -> tuple:
        """
        Volatility regime ผ่าน ATR ratio

        คืน (label, ratio):
          label: "low" | "medium" | "high"
          ratio: current_atr / historical_avg_atr
        """
        high  = df["high"].astype(float)
        low   = df["low"].astype(float)
        close = df["close"].astype(float)

        # True Range
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)

        atr_current = tr.rolling(self.atr_period).mean().iloc[-1]
        atr_hist    = tr.rolling(self.atr_lookback).mean().iloc[-1]

        if atr_hist == 0 or np.isnan(atr_hist):
            return "medium", 1.0

        ratio = float(atr_current / atr_hist)

        if ratio >= self.vol_high_mult:
            return "high", ratio
        if ratio <= self.vol_low_mult:
            return "low", ratio
        return "medium", ratio

    # ══════════════════════════════════════════════════════════
    # Classification
    # ══════════════════════════════════════════════════════════

    def _classify(
        self,
        adx      : float,
        hurst    : float,
        direction: str,
        vol_label: str,
        di_plus  : float,
        di_minus : float,
    ) -> tuple:
        """
        รวม signals จากทุก indicator → regime + confidence

        Logic:
          trending = ADX สูง AND Hurst สูง AND direction ชัด
          ranging  = ADX ต่ำ OR Hurst ต่ำ (mean-reverting)
          volatile = vol สูง AND direction flat (chaos)
          uncertain= สัญญาณขัดแย้งกัน
        """
        # ── Votes from each indicator ──────────────────────────
        # 1. ADX vote
        if adx > self.adx_trend_thresh:
            adx_vote = "trending"
        elif adx < self.adx_range_thresh:
            adx_vote = "ranging"
        else:
            adx_vote = "neutral"

        # 2. Hurst vote
        if hurst > self.hurst_trend_thresh:
            hurst_vote = "trending"
        elif hurst < self.hurst_range_thresh:
            hurst_vote = "ranging"
        else:
            hurst_vote = "neutral"

        # 3. Direction vote
        dir_vote = "trending" if direction != "flat" else "flat"

        # 4. DI alignment (DI+ vs DI- confirms direction)
        di_gap     = abs(di_plus - di_minus)
        di_aligned = di_gap > 3  # DI+ and DI- well separated

        # ── Classify ──────────────────────────────────────────
        trending_votes = sum([
            adx_vote  == "trending",
            hurst_vote == "trending",
            dir_vote  == "trending",
            di_aligned,
        ])

        ranging_votes = sum([
            adx_vote  == "ranging",
            hurst_vote == "ranging",
        ])

        # Confident trending
        if trending_votes >= 3 and direction != "flat":
            regime     = f"trending_{direction}"
            confidence = min(0.5 + (trending_votes - 3) * 0.15 + adx / 200, 0.95)

        # Clear ranging
        elif ranging_votes >= 2:
            regime     = "ranging"
            confidence = min(0.5 + ranging_votes * 0.15, 0.85)

        # High vol + flat direction = volatile/chaotic
        elif vol_label == "high" and direction == "flat":
            regime     = "volatile"
            confidence = 0.6

        # Weak trend signals
        elif trending_votes >= 2 and direction != "flat":
            regime     = f"trending_{direction}"
            confidence = 0.45

        # Otherwise uncertain
        else:
            regime     = "uncertain"
            confidence = 0.3

        return regime, float(confidence)

    def _recommend_strategy(
        self,
        regime    : str,
        confidence: float,
        vol_label : str,
    ) -> str:
        """Strategy recommendation ตาม regime"""
        if "trending" in regime:
            if confidence >= 0.6:
                return "trend_follow"
            return "trend_follow_cautious"
        if regime == "ranging":
            return "mean_revert"
        if regime == "volatile":
            return "high_conf_only"  # เทรดได้แต่ต้องมั่นใจมากขึ้น
        return "default"

    # ══════════════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════════════

    def _normalize_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normalize column names to lowercase"""
        rename = {c: c.lower() for c in df.columns}
        df = df.rename(columns=rename)
        required = ["open", "high", "low", "close"]
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"RegimeDetector: missing columns {missing}")
        return df

    def _get_from_cache(self, symbol: str) -> Optional[RegimeState]:
        if symbol not in self._cache:
            return None
        ts, state = self._cache[symbol]
        now = datetime.now(timezone.utc)
        age_minutes = (now - ts).total_seconds() / 60
        if age_minutes < self._cache_ttl_minutes:
            return state
        return None

    def _set_cache(self, symbol: str, state: RegimeState):
        if symbol:
            self._cache[symbol] = (datetime.now(timezone.utc), state)


# ══════════════════════════════════════════════════════════════
# Regime Weight Tables (ใช้ใน ensemble.py)
# ══════════════════════════════════════════════════════════════

REGIME_MODEL_WEIGHTS: dict[str, dict[str, float]] = {
    # trending: LGBM + XGB แม่นกว่า LSTM สำหรับ directional bias
    "trending_up"  : {"lgbm": 0.40, "xgb": 0.35, "lstm": 0.15, "rule": 0.10},
    "trending_down": {"lgbm": 0.40, "xgb": 0.35, "lstm": 0.15, "rule": 0.10},

    # ranging: rule-based (support/resistance) ทำงานได้ดีกว่า ML
    "ranging"      : {"lgbm": 0.20, "xgb": 0.20, "lstm": 0.10, "rule": 0.50},

    # volatile: spread weights อาจ noisy → ใช้ average + require confidence
    "volatile"     : {"lgbm": 0.33, "xgb": 0.33, "lstm": 0.34, "rule": 0.00},

    # uncertain: fallback to equal weight
    "uncertain"    : {"lgbm": 0.33, "xgb": 0.33, "lstm": 0.34, "rule": 0.00},
}

# Confidence threshold ที่ต้อง pass ก่อน execute (per regime)
REGIME_CONFIDENCE_THRESHOLDS: dict[str, float] = {
    "trending_up"  : 0.55,   # trend ชัด → threshold ปกติ
    "trending_down": 0.55,
    "ranging"      : 0.60,   # ranging → ต้องมั่นใจมากขึ้นเพราะ ML ไม่ถนัด
    "volatile"     : 0.70,   # volatile → threshold สูง (เสี่ยงมาก)
    "uncertain"    : 0.65,
}


# ══════════════════════════════════════════════════════════════
# Convenience functions
# ══════════════════════════════════════════════════════════════

def get_regime(
    symbol    : str,
    timeframe : str = "H4",
    source    : str = "mt5",   # "mt5" | "parquet"
) -> RegimeState:
    """One-liner สำหรับ bot/main.py"""
    detector = RegimeDetector()
    if source == "mt5":
        return detector.detect_from_mt5(symbol=symbol, timeframe=timeframe)
    return detector.detect_from_parquet(symbol=symbol, timeframe=timeframe)


# ══════════════════════════════════════════════════════════════
# CLI Test
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Market Regime Detection")
    parser.add_argument("--symbols",   nargs="+",
                        default=CFG.get("symbols", {}).get("active", ["XAUUSD"]))
    parser.add_argument("--timeframe", default="H4",
                        choices=["M15", "H1", "H4", "D1"])
    parser.add_argument("--source",    default="mt5",
                        choices=["mt5", "parquet"])
    args = parser.parse_args()

    detector = RegimeDetector()

    print(f"\n{'='*65}")
    print(f"  Market Regime Detection")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Source: {args.source} | Timeframe: {args.timeframe}")
    print(f"{'='*65}")

    for sym in args.symbols:
        if args.source == "mt5":
            state = detector.detect_from_mt5(sym, args.timeframe)
        else:
            state = detector.detect_from_parquet(sym)

        print(f"\n  {sym}")
        print(f"  {state}")
        print(f"  Strategy  : {state.strategy}")
        weights = REGIME_MODEL_WEIGHTS.get(state.regime, {})
        print(f"  Weights   : lgbm={weights.get('lgbm',0):.0%} "
              f"xgb={weights.get('xgb',0):.0%} "
              f"lstm={weights.get('lstm',0):.0%} "
              f"rule={weights.get('rule',0):.0%}")
        thr = REGIME_CONFIDENCE_THRESHOLDS.get(state.regime, 0.6)
        print(f"  Conf thr  : {thr:.0%}")
        print(f"  Should trade: {'✅' if state.should_trade else '❌'}")