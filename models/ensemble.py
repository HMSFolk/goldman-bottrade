# models/ensemble.py
"""
Ensemble Trader — รวม XGB + LGBM + LSTM
โหวตสัญญาณด้วย Weighted Soft Voting
รองรับ Regime-Aware Weighting (เพิ่มใหม่)
"""

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ✅ FIX BUG-1: setup_logging ก่อน import อื่น
from bot.setup_logging import setup_logging
setup_logging()

import logging
import json
import time
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

# ✅ FIX BUG-2: ใช้ get_config() แทน yaml.safe_load โดยตรง
from config import get_config
CFG = get_config()

# [1] NEW — Regime imports ─────────────────────────────────────
from features.regime import (
    RegimeState,
    REGIME_MODEL_WEIGHTS,
    REGIME_CONFIDENCE_THRESHOLDS,
)

log = logging.getLogger("models")

# ✅ FIX BUG-3: absolute paths จาก project root
MODELS_DIR  = _ROOT / CFG['paths']['models_saved']
REPORTS_DIR = _ROOT / CFG['paths']['reports']


# ══════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════
@dataclass
class ModelPrediction:
    """ผล predict จาก 1 โมเดล"""
    name:        str
    direction:   int        # -1, 0, 1
    confidence:  float      # max probability
    proba:       np.ndarray # [sell, hold, buy] probabilities
    latency_ms:  float      # เวลาที่ใช้ predict (ms)
    available:   bool = True

    def __str__(self):
        arrow = "↑" if self.direction == 1 \
           else "↓" if self.direction == -1 \
           else "→"
        return (
            f"{self.name}: {arrow} "
            f"conf={self.confidence:.3f} "
            f"[S={self.proba[0]:.3f} "
            f"H={self.proba[1]:.3f} "
            f"B={self.proba[2]:.3f}]"
        )


@dataclass
class EnsembleSignal:
    """สัญญาณรวมจาก Ensemble"""
    direction:      int
    confidence:     float
    raw_proba:      np.ndarray    # weighted average probability
    individual:     list          # ModelPrediction แต่ละตัว
    n_agree:        int           # กี่โมเดลเห็นตรงกัน
    n_models:       int           # โมเดลที่ available ทั้งหมด
    conflict_score: float         # ระดับความขัดแย้ง (0=ตรงกัน 1=ขัดทั้งหมด)
    method:         str           # "soft_voting" | "hard_voting" | "regime_weighted"
    blocked_reason: str = ""

    @property
    def is_actionable(self) -> bool:
        # ✅ FIX BUG-5: ใช้ get_config() ไม่ใช่ module-level CFG (อาจ stale)
        min_conf = get_config()['signal']['min_confidence']
        return (
            self.direction    != 0 and
            self.confidence   >= min_conf and
            self.conflict_score < 0.5 and
            not self.blocked_reason
        )

    @property
    def agreement_pct(self) -> float:
        return self.n_agree / max(self.n_models, 1)

    def __str__(self):
        arrow = "↑ BUY" if self.direction == 1 \
           else "↓ SELL" if self.direction == -1 \
           else "→ HOLD"
        status = "✅ ACTIONABLE" if self.is_actionable else "⛔ BLOCKED"
        return (
            f"{status} | {arrow} | "
            f"conf={self.confidence:.3f} | "
            f"agree={self.n_agree}/{self.n_models} | "
            f"conflict={self.conflict_score:.2f}"
        )


# ══════════════════════════════════════════════════════════════
# Ensemble Trader
# ══════════════════════════════════════════════════════════════
class EnsembleTrader:
    """
    โหลดทุกโมเดลและรวมสัญญาณ

    Weights กำหนดน้ำหนักแต่ละโมเดล
    ปรับได้ใน config.yaml หรือจาก backtest performance

    Regime-Aware Mode (ใหม่):
    ใช้ predict_with_regime() แทน predict() เพื่อให้น้ำหนัก
    โมเดลปรับตาม market regime โดยอัตโนมัติ
    """

    DEFAULT_WEIGHTS = {
        'xgb' : 0.40,   # XGB: เร็ว เสถียร
        'lgbm': 0.35,   # LGBM: แม่นมาก แต่ lag เล็กน้อย
        'lstm': 0.25,   # LSTM: จับ sequential pattern
    }

    def __init__(
        self,
        symbol:     str,
        weights:    dict  = None,
        min_conf:   float = None,
        use_lstm:   bool  = True,
    ):
        self.symbol   = symbol
        self.weights  = weights or self.DEFAULT_WEIGHTS.copy()
        self.min_conf = min_conf or CFG['signal']['min_confidence']
        self.use_lstm = use_lstm

        # Normalize weights ให้รวมเป็น 1.0
        total = sum(self.weights.values())
        self.weights = {k: v/total for k, v in self.weights.items()}

        # โหลดโมเดล
        self._models  = {}
        self._load_all()

        log.info(
            f"EnsembleTrader {symbol}: "
            f"models={list(self._models.keys())} | "
            f"weights={self.weights} | "
            f"min_conf={self.min_conf}"
        )

    # ── Load Models ────────────────────────────────────────────
    def _load_all(self):
        """โหลดทุกโมเดล — ถ้าไม่มีไฟล์ข้ามไป"""

        # ✅ FIX BUG-4: catch Exception ครอบคลุม ImportError, CorruptedModel, PermissionError
        # XGBoost
        try:
            from models.train_xgb import load_model as xgb_load
            self._models['xgb'] = xgb_load(self.symbol)
            log.info(f"  ✅ XGB loaded")
        except Exception as e:
            log.warning(f"  ⚠️ XGB โหลดไม่ได้ ({type(e).__name__}: {e}) — ข้าม")

        # LightGBM
        try:
            from models.train_lgbm import load_model as lgbm_load
            self._models['lgbm'] = lgbm_load(self.symbol)
            log.info(f"  ✅ LGBM loaded")
        except Exception as e:
            log.warning(f"  ⚠️ LGBM โหลดไม่ได้ ({type(e).__name__}: {e}) — ข้าม")

        # LSTM
        if self.use_lstm:
            try:
                from models.train_lstm import load_model as lstm_load
                self._models['lstm'] = lstm_load(self.symbol)
                log.info(f"  ✅ LSTM loaded")
            except Exception as e:
                log.warning(f"  ⚠️ LSTM โหลดไม่ได้ ({type(e).__name__}: {e}) — ข้าม")

        if not self._models:
            raise RuntimeError(
                f"ไม่มีโมเดลเลย! "
                f"รัน: python models/train_xgb.py ก่อน"
            )

        # ✅ FIX BUG-WEIGHT: renormalize weights ตามโมเดลที่โหลดได้จริง
        # ถ้า LSTM ไม่มีไฟล์ → weight รวม xgb+lgbm = 0.75 แทน 1.0
        # ทำให้ confidence ต่ำกว่าจริง 25% → แก้โดย normalize ใหม่
        loaded_keys  = set(self._models.keys())
        active_w     = {k: v for k, v in self.weights.items() if k in loaded_keys}
        total_active = sum(active_w.values())
        if total_active > 0:
            self.weights = {k: v / total_active for k, v in active_w.items()}
            missing = set(self.DEFAULT_WEIGHTS.keys()) - loaded_keys
            if missing:
                log.info(
                    f"  ↻ Weights renormalized (missing: {missing}): "
                    f"{self.weights}"
                )

    # ── Predict Each Model ────────────────────────────────────
    def _predict_xgb(self, df: pd.DataFrame) -> ModelPrediction:
        """XGB predict — รับ single row"""
        from models.train_xgb import predict as xgb_predict

        t0 = time.time()
        payload = self._models['xgb']
        result  = xgb_predict(payload, df, min_conf=0.0)

        return ModelPrediction(
            name       = 'xgb',
            direction  = result['direction'],
            confidence = result['confidence'],
            proba      = np.array([
                result['proba_sell'],
                result['proba_hold'],
                result['proba_buy'],
            ]),
            latency_ms = (time.time() - t0) * 1000,
        )

    def _predict_lgbm(self, df: pd.DataFrame) -> ModelPrediction:
        """LGBM predict — รับ single row"""
        from models.train_lgbm import predict as lgbm_predict

        t0 = time.time()
        payload = self._models['lgbm']
        result  = lgbm_predict(payload, df, min_conf=0.0)

        return ModelPrediction(
            name       = 'lgbm',
            direction  = result['direction'],
            confidence = result['confidence'],
            proba      = np.array([
                result['proba_sell'],
                result['proba_hold'],
                result['proba_buy'],
            ]),
            latency_ms = (time.time() - t0) * 1000,
        )

    def _predict_lstm(self, df: pd.DataFrame) -> ModelPrediction:
        """LSTM predict — ต้องการ sequence"""
        from models.train_lstm import predict as lstm_predict

        t0 = time.time()
        result = lstm_predict(self.symbol, df, min_conf=0.0)

        return ModelPrediction(
            name       = 'lstm',
            direction  = result['direction'],
            confidence = result['confidence'],
            proba      = np.array([
                result['proba_sell'],
                result['proba_hold'],
                result['proba_buy'],
            ]),
            latency_ms = (time.time() - t0) * 1000,
        )

    # ── Soft Voting ────────────────────────────────────────────
    def _soft_vote(
        self,
        predictions: list,
    ) -> np.ndarray:
        """
        Weighted Soft Voting
        เฉลี่ย probability ถ่วงน้ำหนักจากทุกโมเดล

        สูตร:
        P_ensemble[class] = Σ (weight_i × P_i[class])

        ดีกว่า Hard Vote เพราะ:
        - ใช้ข้อมูล probability แทนแค่ direction
        - โมเดลที่ confident มากจะมีอิทธิพลมากกว่า
        """
        weighted_proba = np.zeros(3)   # [sell, hold, buy]
        total_weight   = 0.0

        for pred in predictions:
            w = self.weights.get(pred.name, 0.0)
            if w > 0 and pred.available:
                weighted_proba += w * pred.proba
                total_weight   += w

        if total_weight > 0:
            weighted_proba /= total_weight

        return weighted_proba

    # ── Hard Voting ────────────────────────────────────────────
    def _hard_vote(
        self,
        predictions: list,
    ) -> tuple[int, float]:
        """
        Weighted Hard Voting
        นับคะแนน direction โดยใช้ weight เป็นคะแนน

        ใช้เป็น fallback เมื่อ soft vote ไม่ชัดเจน
        """
        scores = {-1: 0.0, 0: 0.0, 1: 0.0}

        for pred in predictions:
            if pred.available:
                w = self.weights.get(pred.name, 0.0)
                scores[pred.direction] += w

        winner    = max(scores, key=scores.get)
        win_score = scores[winner]
        return winner, win_score

    # ── Conflict Detection ────────────────────────────────────
    def _calc_conflict(
        self,
        predictions: list,
    ) -> float:
        """
        วัดความขัดแย้งระหว่างโมเดล
        0.0 = ทุกตัวเห็นตรงกัน
        1.0 = ขัดกันสูงสุด (บางตัว BUY บางตัว SELL)

        สูตร: conflict = std ของ direction ที่ weight แล้ว
        """
        if len(predictions) <= 1:
            return 0.0

        directions = []
        weights    = []

        for pred in predictions:
            if pred.available:
                directions.append(float(pred.direction))
                weights.append(self.weights.get(pred.name, 0.0))

        if not directions:
            return 1.0

        d_arr = np.array(directions)
        w_arr = np.array(weights)
        w_arr = w_arr / (w_arr.sum() + 1e-9)

        # Weighted std
        mean   = np.average(d_arr, weights=w_arr)
        var    = np.average((d_arr - mean)**2, weights=w_arr)
        std    = np.sqrt(var)

        # normalize: max std เมื่อ BUY vs SELL = std([1,-1]) = 1.0
        return min(float(std), 1.0)

    # ── Main Predict ──────────────────────────────────────────
    def predict(
        self,
        df:            pd.DataFrame,
        method:        str = "soft",   # "soft" | "hard" | "auto"
        require_agree: int = 2,        # ต้องมี n models เห็นตรงกัน
    ) -> EnsembleSignal:
        """
        รวมสัญญาณจากทุกโมเดล (standard — ไม่ปรับตาม regime)

        method:
        - "soft"  = weighted average probability (แนะนำ)
        - "hard"  = weighted majority vote
        - "auto"  = soft ถ้า agree ≥ 2 ไม่งั้นใช้ hard
        """
        # ── 1. Predict แต่ละโมเดล ─────────────────────────────
        predictions = self._get_model_predictions(df)
        avail_preds = [p for p in predictions if p.available]
        n_models    = len(avail_preds)

        if n_models == 0:
            return EnsembleSignal(
                direction=0, confidence=0.0,
                raw_proba=np.array([0.33,0.34,0.33]),
                individual=predictions,
                n_agree=0, n_models=0,
                conflict_score=1.0, method="none",
                blocked_reason="ไม่มีโมเดลที่ใช้งานได้",
            )

        # ── 2. Voting ─────────────────────────────────────────
        raw_proba      = self._soft_vote(avail_preds)
        conflict_score = self._calc_conflict(avail_preds)

        # นับว่ากี่โมเดลเห็นตรงกับ soft vote result
        soft_direction = int(raw_proba.argmax()) - 1  # 0,1,2 → -1,0,1
        n_agree        = sum(
            1 for p in avail_preds
            if p.direction == soft_direction
        )

        # ── 3. Hard Vote Tiebreaker ────────────────────────────
        if method == "auto" and n_agree < require_agree:
            hard_dir, hard_score = self._hard_vote(avail_preds)
            direction  = hard_dir
            confidence = hard_score
            used_method= "hard_voting"
        else:
            direction  = soft_direction
            confidence = float(raw_proba[raw_proba.argmax()])
            used_method= "soft_voting"

        # ── 4. Confidence Boost ────────────────────────────────
        # ถ้าทุกโมเดลเห็นตรงกัน เพิ่ม confidence 10%
        if n_agree == n_models and n_models >= 2:
            confidence = min(confidence * 1.10, 0.99)

        # ── 5. Block Conditions ────────────────────────────────
        block_reason = ""

        # ถ้า AI มั่นใจ >= 65% และเห็นตรงกัน 2 ตัวขึ้นไป ให้เทรดเลย!
        # (ใช้ตัวแปร confidence และ n_agree โดยตรง ไม่ต้องมี signal.)
        # ✅ FIX: เพิ่ม VIP Pass จาก 0.45 → 0.62
        # เหตุผล: 0.45 ต่ำเกิน (baseline random = 0.33) ทำให้เทรดสัญญาณแย่
        # 0.62 = มั่นใจจริงๆ และ n_agree >= 2 → คุณภาพดีขึ้นมาก
        is_strong_signal = (confidence >= 0.62) and (n_agree >= 2) and (direction != 0)

        if is_strong_signal:
            block_reason = ""  # เคลียร์เหตุผลการบล็อกทั้งหมด ให้ผ่านได้เลย
            log.info(f"{self.symbol}: 🚀 บังคับเปิดออเดอร์ (VIP Pass) เพราะความมั่นใจสูง {confidence:.2f}")
        else:
            # ตรวจสอบ block reasons แบบปกติ (ถ้าคะแนนไม่ถึง VIP)
            block_reason = self._check_blocks(
                raw_proba, conflict_score, n_agree, n_models
            )
            if block_reason:
                direction = 0  # บังคับ HOLD

        # ── 6. Log ────────────────────────────────────────────
        signal = EnsembleSignal(
            direction      = direction,
            confidence     = round(confidence, 4),
            raw_proba      = raw_proba,
            individual     = predictions,
            n_agree        = n_agree,
            n_models       = n_models,
            conflict_score = round(conflict_score, 4),
            method         = used_method,
            blocked_reason = block_reason,
        )

        log.info(f"{self.symbol}: {signal}")
        for pred in avail_preds:
            log.debug(f"  {pred}")

        return signal

    # ══════════════════════════════════════════════════════════
    # [2] NEW — Regime-Aware Methods
    # ══════════════════════════════════════════════════════════

    def predict_with_regime(
        self,
        df:     pd.DataFrame,
        regime: "RegimeState",
        symbol: str,
    ) -> dict:
        """
        Regime-Aware Predict — ปรับ weights + threshold ตาม market regime

        แทน self.weights ปกติ ด้วย REGIME_MODEL_WEIGHTS[regime.regime]
        ใช้ REGIME_CONFIDENCE_THRESHOLDS[regime.regime] แทน self.min_conf

        ตัวอย่าง:
            result = ensemble.predict_with_regime(df, regime, "XAUUSDm")
            if result["should_trade"]:
                executor.execute_order(symbol, result["signal"], ...)

        Returns:
            {
                "should_trade"  : bool          — True = ส่ง order ได้
                "signal"        : EnsembleSignal — สัญญาณ (อาจเป็น HOLD ถ้า block)
                "regime"        : RegimeState   — regime ที่ detect ได้
                "weights_used"  : dict          — weights ที่ใช้จริง
                "threshold_used": float         — confidence threshold ที่ใช้
                "block_reason"  : str           — เหตุผลที่ไม่เทรด (ถ้ามี)
            }
        """
        # ── 1. หา regime-specific weights ──────────────────────
        weights_used = self._get_regime_weights(regime)
        log.debug(
            f"{symbol}: regime={regime.regime} "
            f"weights={weights_used}"
        )

        # ── 2. ดึง raw predictions ─────────────────────────────
        predictions = self._get_model_predictions(df)

        # ── 3. รวม predictions ด้วย regime weights ─────────────
        signal = self._weighted_combine(predictions, weights_used, regime)

        # ── 4. หา regime-specific confidence threshold ──────────
        threshold = REGIME_CONFIDENCE_THRESHOLDS.get(
            regime.regime, self.min_conf
        )

        # ── 5. ตัดสินใจ should_trade ──────────────────────────
        block_reason = ""

        if not signal.is_actionable:
            block_reason = f"signal blocked: {signal.blocked_reason}"

        elif signal.confidence < threshold:
            block_reason = (
                f"confidence ต่ำกว่า regime threshold "
                f"({signal.confidence:.3f} < {threshold:.3f})"
            )

        elif hasattr(regime, 'is_volatile_low_conf') and \
             regime.is_volatile_low_conf:
            block_reason = "regime volatile + low confidence"

        should_trade = (block_reason == "")

        if not should_trade:
            log.info(
                f"{symbol}: predict_with_regime BLOCKED "
                f"[{regime.regime}] — {block_reason}"
            )
        else:
            log.info(
                f"{symbol}: predict_with_regime OK "
                f"[{regime.regime}] conf={signal.confidence:.3f} "
                f">= threshold={threshold:.3f}"
            )

        return {
            "should_trade"  : should_trade,
            "signal"        : signal,
            "regime"        : regime,
            "weights_used"  : weights_used,
            "threshold_used": threshold,
            "block_reason"  : block_reason,
        }

    def _get_regime_weights(self, regime: "RegimeState") -> dict:
        """
        หา model weights ตาม market regime

        ดึงจาก REGIME_MODEL_WEIGHTS ที่ import มาจาก features.regime
        ถ้า regime.regime ไม่อยู่ใน map → fallback ไป self.weights (default)

        ตัวอย่าง REGIME_MODEL_WEIGHTS:
            {
                RegimeEnum.TRENDING : {'xgb': 0.50, 'lgbm': 0.30, 'lstm': 0.20},
                RegimeEnum.RANGING  : {'xgb': 0.30, 'lgbm': 0.50, 'lstm': 0.20},
                RegimeEnum.VOLATILE : {'xgb': 0.45, 'lgbm': 0.45, 'lstm': 0.10},
            }
        """
        regime_w = REGIME_MODEL_WEIGHTS.get(regime.regime)

        if regime_w is None:
            log.debug(
                f"regime.regime={regime.regime} ไม่อยู่ใน REGIME_MODEL_WEIGHTS "
                f"— ใช้ default weights"
            )
            return self.weights.copy()

        # Normalize ให้รวมเป็น 1.0
        total = sum(regime_w.values()) + 1e-9
        normalized = {k: v / total for k, v in regime_w.items()}

        log.debug(f"regime weights [{regime.regime}]: {normalized}")
        return normalized

    def _weighted_combine(
        self,
        predictions: list,
        weights:     dict,
        regime:      "RegimeState",
    ) -> EnsembleSignal:
        """
        รวม model predictions ด้วย weights ที่กำหนดจากภายนอก
        (Regime-aware version ของ _soft_vote)

        ต่างจาก predict() ตรงที่ใช้ weights ที่รับมาแทน self.weights
        ทำให้สามารถ override weights ตาม regime ได้

        Parameters:
            predictions : list[ModelPrediction] จาก _get_model_predictions()
            weights     : regime-specific weights จาก _get_regime_weights()
            regime      : RegimeState สำหรับ label ใน EnsembleSignal.method
        """
        avail_preds = [p for p in predictions if p.available]
        n_models    = len(avail_preds)

        if n_models == 0:
            return EnsembleSignal(
                direction=0, confidence=0.0,
                raw_proba=np.array([0.33, 0.34, 0.33]),
                individual=predictions,
                n_agree=0, n_models=0,
                conflict_score=1.0,
                method="none",
                blocked_reason="ไม่มีโมเดลที่ใช้งานได้",
            )

        # ── Weighted Soft Vote ด้วย regime weights ─────────────
        weighted_proba = np.zeros(3)   # [sell, hold, buy]
        total_weight   = 0.0

        for pred in avail_preds:
            w = weights.get(pred.name, 0.0)
            if w > 0:
                weighted_proba += w * pred.proba
                total_weight   += w

        if total_weight > 0:
            weighted_proba /= total_weight

        # ── Conflict score (ใช้ self._calc_conflict เดิม) ───────
        conflict_score = self._calc_conflict(avail_preds)

        # ── Direction + Confidence ──────────────────────────────
        direction  = int(weighted_proba.argmax()) - 1   # 0,1,2 → -1,0,1
        confidence = float(weighted_proba.max())

        n_agree = sum(
            1 for p in avail_preds if p.direction == direction
        )

        # Confidence boost ถ้าทุกตัวเห็นตรงกัน
        if n_agree == n_models and n_models >= 2:
            confidence = min(confidence * 1.10, 0.99)

        # ── Block conditions ────────────────────────────────────
        blocked_reason = self._check_blocks(
            weighted_proba, conflict_score, n_agree, n_models
        )
        if blocked_reason:
            direction = 0

        regime_label = getattr(regime.regime, 'name', str(regime.regime))

        return EnsembleSignal(
            direction      = direction,
            confidence     = round(confidence, 4),
            raw_proba      = weighted_proba,
            individual     = predictions,
            n_agree        = n_agree,
            n_models       = n_models,
            conflict_score = round(conflict_score, 4),
            method         = f"regime_weighted({regime_label})",
            blocked_reason = blocked_reason,
        )

    def _get_model_predictions(
        self,
        df: pd.DataFrame,
    ) -> list:
        """
        ดึง raw predictions จากทุกโมเดลที่โหลดแล้ว

        แยกออกมาจาก predict() เพื่อให้ predict_with_regime()
        และ predict() ใช้ code เดียวกันในการเรียกโมเดล
        ไม่ต้อง duplicate try/except blocks

        Returns:
            list[ModelPrediction] — รวมทั้ง available=True และ False
        """
        predictions = []

        if 'xgb' in self._models:
            try:
                predictions.append(self._predict_xgb(df))
            except Exception as e:
                log.warning(f"XGB predict ล้มเหลว: {e}")
                predictions.append(ModelPrediction(
                    'xgb', 0, 0.0,
                    np.array([0.33, 0.34, 0.33]),
                    0, available=False,
                ))

        if 'lgbm' in self._models:
            try:
                predictions.append(self._predict_lgbm(df))
            except Exception as e:
                log.warning(f"LGBM predict ล้มเหลว: {e}")
                predictions.append(ModelPrediction(
                    'lgbm', 0, 0.0,
                    np.array([0.33, 0.34, 0.33]),
                    0, available=False,
                ))

        if 'lstm' in self._models:
            try:
                predictions.append(self._predict_lstm(df))
            except Exception as e:
                log.warning(f"LSTM predict ล้มเหลว: {e}")
                predictions.append(ModelPrediction(
                    'lstm', 0, 0.0,
                    np.array([0.33, 0.34, 0.33]),
                    0, available=False,
                ))

        return predictions

    # ══════════════════════════════════════════════════════════
    # Block Conditions
    # ══════════════════════════════════════════════════════════
    def _check_blocks(
        self,
        proba:          np.ndarray,
        conflict_score: float,
        n_agree:        int,
        n_models:       int, ) -> str:
        """
        ตรวจเงื่อนไขที่ควรบล็อก signal
        คืน string เหตุผล หรือ "" ถ้าไม่บล็อก
        """
        # Confidence ต่ำกว่า threshold
        max_prob = float(proba.max())
        if max_prob < self.min_conf:
            return f"confidence ต่ำ ({max_prob:.3f} < {self.min_conf})"

        # โมเดลขัดกันสูง (BUY vs SELL)
        if conflict_score > 0.85:
            return f"conflict สูง ({conflict_score:.2f})"

        # ไม่มีโมเดลเลยที่เห็นด้วย
        if n_agree == 0:
            return "ไม่มีโมเดลเห็นด้วยกับ ensemble"

        # HOLD probability สูงกว่า 70%
        if float(proba[1]) > 0.70:
            return f"HOLD probability สูง ({proba[1]:.2f})"

        return ""


# ══════════════════════════════════════════════════════════════
# Weight Optimizer
# ══════════════════════════════════════════════════════════════
class WeightOptimizer:
    """
    หา weights ที่ดีที่สุดด้วย Optuna
    รันหลังจาก train ทุกโมเดลแล้ว

    เปรียบเทียบ accuracy บน validation set
    แล้วหา weight ที่ให้ ensemble accuracy สูงสุด
    """

    def __init__(self, symbol: str):
        self.symbol = symbol

    def optimize(
        self,
        df:       pd.DataFrame,
        n_trials: int = 100,
    ) -> dict:
        """
        หา optimal weights ด้วย Optuna
        df: DataFrame ที่มี features + label (validation set)
        """
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)

        # โหลด predictions แต่ละโมเดลก่อน
        preds_xgb  = self._batch_predict('xgb',  df)
        preds_lgbm = self._batch_predict('lgbm', df)
        preds_lstm = self._batch_predict('lstm', df)
        y_true     = df['label'].map({-1:0, 0:1, 1:2}).values

        def objective(trial):
            w_xgb  = trial.suggest_float('w_xgb',  0.1, 0.8)
            w_lgbm = trial.suggest_float('w_lgbm', 0.1, 0.8)
            w_lstm = trial.suggest_float('w_lstm', 0.0, 0.5)

            total  = w_xgb + w_lgbm + w_lstm + 1e-9
            w_xgb  /= total
            w_lgbm /= total
            w_lstm /= total

            # Soft vote
            ensemble_proba = (
                w_xgb  * preds_xgb  +
                w_lgbm * preds_lgbm +
                w_lstm * preds_lstm
            )
            y_pred = ensemble_proba.argmax(axis=1)

            from sklearn.metrics import f1_score
            return f1_score(
                y_true, y_pred,
                average='macro', zero_division=0,
            )

        study = optuna.create_study(direction='maximize')
        study.optimize(objective, n_trials=n_trials,
                       show_progress_bar=False)

        best = study.best_params
        total= best['w_xgb'] + best['w_lgbm'] + best['w_lstm']
        optimal_weights = {
            'xgb' : round(best['w_xgb']  / total, 3),
            'lgbm': round(best['w_lgbm'] / total, 3),
            'lstm': round(best['w_lstm'] / total, 3),
        }

        log.info(
            f"Optimal weights {self.symbol}: "
            f"{optimal_weights} | "
            f"f1={study.best_value:.4f}"
        )

        # บันทึกลง config
        self._save_weights(optimal_weights)
        return optimal_weights

    def _batch_predict(
        self,
        model_name: str,
        df:         pd.DataFrame,
    ) -> np.ndarray:
        """Predict ทุก row ใน df สำหรับ weight optimization"""
        probas = []

        if model_name == 'xgb':
            from models.train_xgb import load_model, predict
            payload = load_model(self.symbol)
            # ✅ FIX BUG-6: df.iloc[[i]] แทน df.iloc[:i+1]
            #    XGB/LGBM ใช้แค่ features ของ row นั้น ไม่ต้องส่ง history ทั้งหมด
            #    เร็วกว่า O(n²) → O(n)
            for i in range(len(df)):
                r = predict(payload, df.iloc[[i]], min_conf=0.0)
                probas.append([r['proba_sell'], r['proba_hold'],
                               r['proba_buy']])

        elif model_name == 'lgbm':
            from models.train_lgbm import load_model, predict
            payload = load_model(self.symbol)
            for i in range(len(df)):
                r = predict(payload, df.iloc[[i]], min_conf=0.0)
                probas.append([r['proba_sell'], r['proba_hold'],
                               r['proba_buy']])

        elif model_name == 'lstm':
            from models.train_lstm import predict as lstm_pred
            for i in range(len(df)):
                try:
                    # LSTM ต้องการ sequence — ส่ง history ที่จำเป็น
                    seq_len = 50   # ปรับตาม model ที่ train
                    r = lstm_pred(self.symbol,
                                  df.iloc[max(0, i-seq_len+1):i+1],
                                  min_conf=0.0)
                    probas.append([r['proba_sell'], r['proba_hold'],
                                   r['proba_buy']])
                except Exception:
                    probas.append([0.33, 0.34, 0.33])

        return np.array(probas)

    def _save_weights(self, weights: dict):
        """บันทึก optimal weights ลงไฟล์"""
        MODELS_DIR.mkdir(parents=True, exist_ok=True)   # ✅ สร้าง dir ถ้าไม่มี
        path = MODELS_DIR / f"ensemble_weights_{self.symbol}.json"
        path.write_text(
            json.dumps({
                'symbol'     : self.symbol,
                'weights'    : weights,
                'updated_at' : datetime.now(timezone.utc).isoformat(),
            }, indent=2),
            encoding="utf-8",
        )
        log.info(f"💾 บันทึก weights → {path}")


# ══════════════════════════════════════════════════════════════
# Factory Function
# ══════════════════════════════════════════════════════════════
def create_ensemble(symbol: str) -> EnsembleTrader:
    """
    สร้าง EnsembleTrader พร้อมโหลด weights ที่ optimize แล้ว
    ใช้ใน bot/main.py
    """
    # ลอง load optimal weights ก่อน
    weights_path = MODELS_DIR / f"ensemble_weights_{symbol}.json"
    weights      = None

    if weights_path.exists():
        try:
            data    = json.loads(weights_path.read_text())
            weights = data['weights']
            log.info(
                f"โหลด optimal weights {symbol}: {weights}"
            )
        except Exception:
            log.warning("โหลด weights ล้มเหลว — ใช้ default")

    return EnsembleTrader(symbol=symbol, weights=weights)


# ══════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    from features.pipeline import load_processed

    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols",  nargs="+",
                        default=CFG['symbols']['active'])
    parser.add_argument("--optimize_weights", action="store_true",
                        help="หา optimal weights ด้วย Optuna")
    parser.add_argument("--trials",   type=int, default=100)
    args   = parser.parse_args()

    for sym in args.symbols:
        log.info(f"\n{'='*50}")
        log.info(f"Testing Ensemble: {sym}")

        try:
            ensemble = create_ensemble(sym)
            df       = load_processed(sym)

            # ทดสอบ predict บน 5 rows ล่าสุด
            log.info("\nทดสอบ predict 5 rows ล่าสุด:")
            for i in range(-5, 0):
                signal = ensemble.predict(df.iloc[:i] if i < -1 else df)
                log.info(f"  [{i}] {signal}")

            # Optimize weights
            if args.optimize_weights:
                log.info("\nOptimizing weights...")
                optimizer = WeightOptimizer(sym)
                val_df    = df.iloc[int(len(df)*0.8):]
                weights   = optimizer.optimize(val_df, n_trials=args.trials)
                log.info(f"Optimal: {weights}")

        except Exception as e:
            log.error(f"❌ {sym}: {e}", exc_info=True)