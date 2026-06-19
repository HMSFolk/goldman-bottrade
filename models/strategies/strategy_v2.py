# models/strategies/strategy_v2.py
"""
Strategy V2 — Experimental / Testing
════════════════════════════════════════════════════════════
สถานะ: TESTING — ยังไม่ใช้ production
ความแตกต่างจาก V1:
  + Session filter เข้มขึ้น (overlap only)
  + News blackout window (ก่อน/หลังข่าวสำคัญ)
  + Adaptive RR ตาม session
  + BOS entry confirmation (ต้องมี Break of Structure)
  + Trailing stop แบบ ATR
  - ลด symbols เหลือแค่ XAUUSD (ทดสอบทีละตัว)
════════════════════════════════════════════════════════════

วิธีทดสอบ:
  python models/backtest.py --method ensemble (แก้ strategy ใน code)
  ผ่านทุกข้อใน deploy_checklist แล้วค่อย promote
"""

import logging
import yaml
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger("models")

with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

# ── Version Info ───────────────────────────────────────────────
VERSION    = "2.0.0-beta"
STATUS     = "TESTING"
BASED_ON   = "strategy_v1.py v1.0.0"
CHANGES    = [
    "Overlap session only (12-16 UTC)",
    "BOS entry confirmation required",
    "News blackout ±30min",
    "Adaptive RR: overlap=2.5 london=2.0 ny=1.8",
    "ATR trailing stop",
]


# ══════════════════════════════════════════════════════════════
# Data Classes (เหมือน V1 — ใช้ TradeSetup เดิม)
# ══════════════════════════════════════════════════════════════
@dataclass
class TradeSetup:
    direction:      int   = 0
    confidence:     float = 0.0
    sl_distance:    float = 0.0
    tp_distance:    float = 0.0
    sl_pct:         float = 0.0
    tp_pct:         float = 0.0
    rr_ratio:       float = 0.0
    trailing_atr:   float = 0.0     # ← ใหม่: trailing stop distance
    reasons:        list  = field(default_factory=list)
    filters_passed: list  = field(default_factory=list)
    filters_failed: list  = field(default_factory=list)
    regime:         str   = "normal"
    session:        str   = "unknown"

    @property
    def is_valid(self) -> bool:
        return (
            self.direction  != 0 and
            self.confidence >= CFG['signal']['min_confidence'] and
            self.sl_distance > 0 and
            self.rr_ratio    >= 0.5
        )

    def __str__(self):
        if self.direction == 0:
            return f"HOLD | failed={self.filters_failed}"
        arrow = "↑ BUY" if self.direction == 1 else "↓ SELL"
        return (
            f"{arrow} | conf={self.confidence:.3f} | "
            f"SL={self.sl_pct:.3f}% "
            f"TP={self.tp_pct:.3f}% "
            f"RR={self.rr_ratio:.1f} "
            f"trail={self.trailing_atr:.5f} | "
            f"{self.session}"
        )

# ══════════════════════════════════════════════════════════════
# Strategy V2 Class
# ══════════════════════════════════════════════════════════════
class StrategyV2:
    """
    Experimental Strategy V2

    Pipeline เพิ่มเติมจาก V1:
    1. ML Ensemble Signal
    2. Overlap Session Only    ← เข้มกว่า V1
    3. News Blackout Filter    ← ใหม่
    4. HTF Alignment
    5. BOS Confirmation        ← ใหม่ — ต้อง Break of Structure
    6. Volatility Filter
    7. Pattern Filter
    8. Adaptive SL/TP          ← ปรับ RR ตาม session
    9. ATR Trailing Stop       ← ใหม่
    """

    VERSION = VERSION
    STATUS  = STATUS

    # Adaptive RR ตาม session
    SESSION_RR = {
        "overlap" : 2.5,   # ดีที่สุด — RR สูงสุด
        "london"  : 2.0,
        "newyork" : 1.8,
        "asian"   : 0.0,   # ไม่เทรด
    }

    def __init__(self):
        self._loaded_for: dict = {}
        log.info(
            f"StrategyV2 v{VERSION} [{STATUS}] | "
            f"changes={len(CHANGES)}"
        )
        for c in CHANGES:
            log.info(f"  + {c}")

    def _get_ensemble(self, symbol: str):
        if symbol not in self._loaded_for:
            from models.ensemble import create_ensemble
            self._loaded_for[symbol] = create_ensemble(symbol)
        return self._loaded_for[symbol]

    # ── Main Evaluate ─────────────────────────────────────────
    def evaluate(
        self,
        df:     pd.DataFrame,
        symbol: str, ) -> TradeSetup:
        setup = TradeSetup()
        row   = df.iloc[-1]

        # ── Step 1: Session Filter (ก่อน ML — เร็วกว่า) ────────
        passed, reason, session = self._check_session_v2(row)
        if not passed:
            setup.filters_failed.append(reason)
            return setup
        setup.session = session
        setup.filters_passed.append(f"session:{session}")

        # ── Step 2: News Blackout ─────────────────────────────
        passed, reason = self._check_news_blackout(row)
        if not passed:
            setup.filters_failed.append(reason)
            return setup
        setup.filters_passed.append("news_clear")

        # ── Step 3: ML Ensemble ───────────────────────────────
        try:
            ensemble = self._get_ensemble(symbol)
            signal   = ensemble.predict(df, method="soft")
        except Exception as e:
            log.error(f"Ensemble error: {e}")
            setup.filters_failed.append("ensemble_error")
            return setup

        setup.direction  = signal.direction
        setup.confidence = signal.confidence

        if setup.direction == 0:
            setup.filters_failed.append("ensemble_hold")
            return setup
        setup.filters_passed.append(
            f"ensemble:{signal.direction} "
            f"conf={signal.confidence:.3f} "
            f"agree={signal.n_agree}/{signal.n_models}"
        )

        # ── Step 4: Confidence ────────────────────────────────
        if setup.confidence < CFG['signal']['min_confidence']:
            setup.direction = 0
            setup.filters_failed.append(
                f"conf_low({setup.confidence:.3f})"
            )
            return setup
        setup.filters_passed.append("confidence")

        # ── Step 5: HTF Alignment (เหมือน V1) ─────────────────
        passed, reason = self._check_htf_alignment(row, setup.direction)
        if not passed:
            setup.direction = 0
            setup.filters_failed.append(reason)
            return setup
        setup.filters_passed.append("htf_alignment")

        # ── Step 6: BOS Confirmation (ใหม่) ────────────────────
        passed, reason = self._check_bos(row, setup.direction)
        if not passed:
            # V2: BOS ไม่ผ่าน = ลด confidence (ไม่ block)
            setup.confidence *= 0.80
            setup.reasons.append(f"no_bos:{reason}")
        else:
            setup.confidence = min(setup.confidence * 1.08, 0.99)
            setup.filters_passed.append("bos_confirmed")

        # ── Step 7: Volatility ────────────────────────────────
        passed, reason, regime = self._check_volatility(row)
        if not passed:
            setup.direction = 0
            setup.filters_failed.append(reason)
            return setup
        setup.regime = regime
        setup.filters_passed.append(f"vol:{regime}")

        # ── Step 8: Pattern ───────────────────────────────────
        passed, reason = self._check_pattern(row, setup.direction)
        if not passed:
            setup.confidence *= 0.85
            setup.reasons.append(f"pattern_warn:{reason}")
        else:
            setup.filters_passed.append("pattern_ok")

        # ── Step 9: Adaptive SL/TP ────────────────────────────
        rr_target = self.SESSION_RR.get(session, 2.0)
        sl_dist, tp_dist, trail = self._calc_sl_tp_v2(
            row, setup.direction, regime, rr_target
        )

        if sl_dist <= 0:
            setup.direction = 0
            setup.filters_failed.append("sl_invalid")
            return setup

        price              = float(row['close'])
        setup.sl_distance  = sl_dist
        setup.tp_distance  = tp_dist
        setup.sl_pct       = sl_dist / price * 100
        setup.tp_pct       = tp_dist / price * 100
        setup.rr_ratio     = round(tp_dist / max(sl_dist, 1e-9), 2)
        setup.trailing_atr = trail

        # ── Step 10: RR Check ─────────────────────────────────
        min_rr = 1.0
        if setup.rr_ratio < min_rr:
            setup.direction = 0
            setup.filters_failed.append(
                f"rr_low({setup.rr_ratio:.2f}<{min_rr})"
            )
            return setup
        setup.filters_passed.append(f"rr:{setup.rr_ratio:.1f}")

        # ── Final confidence check ────────────────────────────
        if setup.confidence < CFG['signal']['min_confidence']:
            setup.direction = 0
            setup.filters_failed.append(
                f"final_conf({setup.confidence:.3f})"
            )
            return setup

        log.info(f"{symbol} V2: {setup}")
        return setup

    # ══════════════════════════════════════════════════════════
    # V2-Specific Filters
    # ══════════════════════════════════════════════════════════
    def _check_session_v2(
        self, row: pd.Series ) -> tuple[bool, str, str]:
        """
        V2 (Modified): อนุญาตให้เทรดทั้ง London, NY และช่วง Overlap
        บล็อกเฉพาะช่วง Asian session ที่กราฟมักจะไซด์เวย์
        """
        is_overlap = row.get('is_overlap_session', 0)
        is_london  = row.get('is_london_session', 0)
        is_ny      = row.get('is_ny_session', 0)

        # ถ้าเป็นช่วง Overlap (ลอนดอนซ้อนนิวยอร์ก) ให้ผ่าน
        if is_overlap == 1:
            return True, "", "overlap"
            
        # ถ้าเป็นช่วงตลาดยุโรปเปิด ให้ผ่าน
        elif is_london == 1:
            return True, "", "london"
            
        # ถ้าเป็นช่วงตลาดอเมริกาเปิด ให้ผ่าน
        elif is_ny == 1:
            return True, "", "newyork"
            
        # ถ้าไม่ใช่ทั้ง 3 ตลาดด้านบน (เช่น ตลาดเอเชีย) ให้บล็อก
        else:
            return False, "asian_session_blocked", "asian"

    def _check_news_blackout(
        self, row: pd.Series ) -> tuple[bool, str]:
        """
        บล็อกการเทรด ±30 นาทีรอบข่าวสำคัญ
        V2 เพิ่ม feature นี้ — V1 ไม่มี

        ใช้ economic_calendar flag ที่เพิ่มใน data pipeline
        (ถ้ายังไม่มี feature นี้ให้ return True ไปก่อน)
        """
        # ตรวจ feature จาก data pipeline
        near_news = row.get('near_high_impact_news', 0)

        if near_news == 1:
            mins = row.get('mins_to_news', 0)
            return False, f"news_blackout({int(mins)}min)"

        return True, ""

    def _check_bos(
        self, row: pd.Series, direction: int ) -> tuple[bool, str]:
        """
        Break of Structure confirmation
        V2 ต้องการ BOS ยืนยันก่อนเข้า

        Bullish BOS = ราคาทะลุ swing high เก่า
        Bearish BOS = ราคาทะลุ swing low เก่า
        """
        bos_bull = row.get('bos_bull', 0)
        bos_bear = row.get('bos_bear', 0)

        # BOS ใน direction เดียวกัน = ยืนยัน
        if direction == 1:
            if bos_bull == 1:
                return True, "bullish_bos_confirmed"
            return False, "no_bullish_bos"

        if direction == -1:
            if bos_bear == 1:
                return True, "bearish_bos_confirmed"
            return False, "no_bearish_bos"

        return False, "direction_zero"

    # ── V2 Helpers (คล้าย V1 แต่ปรับนิดหน่อย) ─────────────────
    def _check_htf_alignment(
        self, row: pd.Series, direction: int ) -> tuple[bool, str]:
        conflict = row.get('htf_conflict', 0)
        if conflict == 1:
            return False, "htf_conflict"

        h4_ema = row.get('h4_ema_alignment', 2)
        if direction ==  1 and h4_ema <= 1:
            return False, f"h4_bear(align={h4_ema})"
        if direction == -1 and h4_ema >= 3:
            return False, f"h4_bull(align={h4_ema})"

        return True, ""

    def _check_volatility(
        self, row: pd.Series ) -> tuple[bool, str, str]:
        if row.get('squeeze_on', 0) == 1:
            return False, "squeeze_on", "squeeze"

        atr_ratio = row.get('atr_ratio', 1.0)
        if atr_ratio > 2.5:
            return False, f"atr_spike({atr_ratio:.1f}x)", "high"
        if atr_ratio < 0.3:
            return False, f"atr_low({atr_ratio:.1f}x)", "low"

        regime = row.get('vol_regime', 'normal')
        if isinstance(regime, float):
            regime = 'normal'

        return True, "", regime

    def _check_pattern(
        self, row: pd.Series, direction: int ) -> tuple[bool, str]:
        pa_score = row.get('pa_score', 0)
        if direction ==  1 and pa_score < -2:
            return False, f"bearish_pa({pa_score:.1f})"
        if direction == -1 and pa_score > 2:
            return False, f"bullish_pa({pa_score:.1f})"
        return True, ""

    def _calc_sl_tp_v2(
        self,
        row:       pd.Series,
        direction: int,
        regime:    str,
        rr_target: float, ) -> tuple[float, float, float]:
        """
        V2: Adaptive SL/TP + ATR Trailing Stop

        SL = ATR × regime_mult
        TP = SL × rr_target (adaptive ตาม session)
        Trailing = ATR × 1.0 (ขยับตาม price)
        """
        atr14 = row.get('atr_14', 0)
        atr7  = row.get('atr_7',  0)
        price = row.get('close',  0)

        if atr14 <= 0 or price <= 0:
            return 0.0, 0.0, 0.0

        atr_avg  = atr14 * 0.7 + atr7 * 0.3

        sl_mult  = {'high':2.0,'normal':1.5,'low':1.0}.get(regime, 1.5)
        sl_dist  = atr_avg * sl_mult
        tp_dist  = sl_dist * rr_target
        trailing = atr_avg * 1.0    # trailing = 1× ATR

        # Sanity checks
        sl_dist  = min(sl_dist, price * 0.03)
        sl_dist  = max(sl_dist, price * 0.001)
        tp_dist  = sl_dist * rr_target
        trailing = max(trailing, sl_dist * 0.5)

        return (
            round(sl_dist, 5),
            round(tp_dist, 5),
            round(trailing, 5),
        )

    def compare_with_v1(
        self, df: pd.DataFrame, symbol: str ) -> dict:
        """เปรียบเทียบ signal V1 vs V2 บน row เดียวกัน"""
        from models.strategies.strategy_v1 import StrategyV1
        v1     = StrategyV1()
        setup1 = v1.evaluate(df, symbol)
        setup2 = self.evaluate(df, symbol)

        return {
            'v1': str(setup1),
            'v2': str(setup2),
            'agree': setup1.direction == setup2.direction,
            'v1_filters': setup1.filters_passed,
            'v2_filters': setup2.filters_passed,
            'v1_failed' : setup1.filters_failed,
            'v2_failed' : setup2.filters_failed,
        }

    def get_version_info(self) -> dict:
        return {
            'version'  : VERSION,
            'status'   : STATUS,
            'based_on' : BASED_ON,
            'changes'  : CHANGES,
        }