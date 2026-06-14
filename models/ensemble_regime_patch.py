# ══════════════════════════════════════════════════════════════
# REGIME-AWARE WEIGHTING — เพิ่มเข้าไปใน models/ensemble.py
# ══════════════════════════════════════════════════════════════
#
# วิธีติดตั้ง:
#   [1] imports → เพิ่มที่ด้านบน ensemble.py
#   [2] เมธอด 3 ตัว → เพิ่มใน class EnsembleModel (หรือชื่อ class ที่มีอยู่)
#   [3] แก้เมธอด predict() เดิม → รับ regime parameter เพิ่ม
#
# ══════════════════════════════════════════════════════════════


# ── [1] เพิ่ม imports ─────────────────────────────────────────
from features.regime import (
    RegimeState,
    REGIME_MODEL_WEIGHTS,
    REGIME_CONFIDENCE_THRESHOLDS,
)


# ── [2] เพิ่มเมธอดเหล่านี้ใน class EnsembleModel ─────────────
# (copy ทุก def ด้านล่างเข้าไปใน class body โดย indent 4 spaces)

class _EnsembleAdditions:   # dummy wrapper สำหรับ validation เท่านั้น ลบออกตอน paste จริง

    def predict_with_regime(
        self,
        features : "pd.DataFrame",
        regime   : "RegimeState",
        symbol   : str = "",
    ) -> dict:
        """
        Predict signal พร้อม regime-aware weighting

        ต่างจาก predict() เดิมตรงที่:
          - weight แต่ละโมเดลแตกต่างกันตาม regime
          - confidence threshold ต่างกันตาม regime
          - log regime ไว้ใน result สำหรับ analysis

        Args:
            features: DataFrame ของ ML features
            regime  : RegimeState จาก features/regime.py
            symbol  : symbol สำหรับ logging

        Returns dict:
          signal    : "BUY" | "SELL" | "HOLD"
          confidence: float 0-1
          regime    : regime string
          weights   : weights ที่ใช้จริง
          should_trade: bool (False ถ้า confidence < threshold)
        """
        # ── ดึง weights ตาม regime ─────────────────────────────
        weights = self._get_regime_weights(regime.regime)

        # ── ดึง predictions จากแต่ละโมเดล ──────────────────────
        raw_preds = self._get_model_predictions(features)

        # ── Weighted ensemble ─────────────────────────────────
        signal, confidence = self._weighted_combine(raw_preds, weights)

        # ── Confidence threshold (ต่างกันตาม regime) ─────────
        threshold    = REGIME_CONFIDENCE_THRESHOLDS.get(regime.regime, 0.60)
        should_trade = (
            confidence >= threshold
            and regime.should_trade
            and signal != "HOLD"
        )

        # ── Log ──────────────────────────────────────────────
        log.info(
            f"[{symbol or 'ensemble'}] regime={regime.regime} "
            f"signal={signal} conf={confidence:.3f} "
            f"thr={threshold:.2f} "
            f"trade={'✅' if should_trade else '❌'} | "
            f"lgbm={weights['lgbm']:.0%} "
            f"xgb={weights['xgb']:.0%} "
            f"lstm={weights.get('lstm',0):.0%}"
        )

        return {
            "signal"      : signal,
            "confidence"  : round(confidence, 4),
            "regime"      : regime.regime,
            "regime_conf" : regime.confidence,
            "weights"     : weights,
            "threshold"   : threshold,
            "should_trade": should_trade,
            "raw_preds"   : raw_preds,
        }

    def _get_regime_weights(self, regime: str) -> dict:
        """
        ดึง model weights ตาม regime จาก config หรือ default table

        Config override (ใน config.yaml):
          regime_weights:
            trending_up:
              lgbm: 0.45
              xgb:  0.35
              ...

        Returns dict: {"lgbm": float, "xgb": float, "lstm": float, "rule": float}
        """
        # ลองดู config ก่อน (ให้ user tune ได้)
        cfg_weights = CFG.get("regime_weights", {}).get(regime)
        if cfg_weights:
            return dict(cfg_weights)

        # fallback ไป default table
        base = REGIME_MODEL_WEIGHTS.get(regime, REGIME_MODEL_WEIGHTS["uncertain"])

        # กรอง models ที่ไม่มีจริง
        available = {}
        for name, w in base.items():
            if name == "lgbm" and hasattr(self, "lgbm_model") and self.lgbm_model:
                available[name] = w
            elif name == "xgb" and hasattr(self, "xgb_model") and self.xgb_model:
                available[name] = w
            elif name == "lstm" and hasattr(self, "lstm_model") and self.lstm_model:
                available[name] = w
            elif name == "rule" and hasattr(self, "rule_model") and self.rule_model:
                available[name] = w

        if not available:
            # ไม่มีโมเดลเลย → return uniform
            names = [k for k in base if hasattr(self, f"{k}_model")]
            return {n: 1.0 / len(names) for n in names} if names else base

        # Re-normalize weights ให้รวมเป็น 1.0
        total = sum(available.values())
        if total == 0:
            return base
        return {k: round(v / total, 4) for k, v in available.items()}

    def _weighted_combine(
        self,
        raw_preds: dict,
        weights  : dict,
    ) -> tuple:
        """
        รวม predictions ด้วย weights

        raw_preds format: {
            "lgbm": {"BUY": 0.3, "HOLD": 0.5, "SELL": 0.2},
            "xgb" : {"BUY": 0.4, "HOLD": 0.4, "SELL": 0.2},
            ...
        }

        Returns: (signal: str, confidence: float)
        """
        import numpy as np

        combined = {"BUY": 0.0, "HOLD": 0.0, "SELL": 0.0}

        for model_name, probs in raw_preds.items():
            w = weights.get(model_name, 0.0)
            if w == 0 or not probs:
                continue
            for label, prob in probs.items():
                if label in combined:
                    combined[label] += prob * w

        if not any(v > 0 for v in combined.values()):
            return "HOLD", 0.0

        # signal = label ที่มี weighted prob สูงสุด
        signal     = max(combined, key=combined.get)
        confidence = float(combined[signal])

        return signal, confidence

    def _get_model_predictions(
        self,
        features: "pd.DataFrame",
    ) -> dict:
        """
        ดึง probability predictions จากทุกโมเดล

        Returns dict: {
            "lgbm": {"BUY": 0.3, "HOLD": 0.5, "SELL": 0.2},
            "xgb" : {...},
            ...
        }
        """
        preds = {}

        # LGBM
        try:
            if hasattr(self, "lgbm_model") and self.lgbm_model:
                proba = self.lgbm_model.predict_proba(
                    features[self.lgbm_features]
                )[0]
                classes = self.lgbm_model.classes_
                preds["lgbm"] = dict(zip(
                    [str(c) for c in classes], proba.tolist()
                ))
        except Exception as e:
            log.warning(f"LGBM predict error: {e}")

        # XGB
        try:
            if hasattr(self, "xgb_model") and self.xgb_model:
                proba = self.xgb_model.predict_proba(
                    features[self.xgb_features]
                )[0]
                classes = self.xgb_model.classes_
                preds["xgb"] = dict(zip(
                    [str(c) for c in classes], proba.tolist()
                ))
        except Exception as e:
            log.warning(f"XGB predict error: {e}")

        # LSTM (ถ้ามี)
        try:
            if hasattr(self, "lstm_model") and self.lstm_model:
                lstm_out = self._lstm_predict(features)
                if lstm_out:
                    preds["lstm"] = lstm_out
        except Exception as e:
            log.warning(f"LSTM predict error: {e}")

        # Rule-based (ถ้ามี)
        try:
            if hasattr(self, "rule_model") and self.rule_model:
                rule_out = self.rule_model.predict(features)
                if rule_out:
                    preds["rule"] = rule_out
        except Exception as e:
            log.warning(f"Rule predict error: {e}")

        return preds


# ══════════════════════════════════════════════════════════════
# bot/main.py — วิธีเรียก RegimeDetector
# ══════════════════════════════════════════════════════════════
#
# เพิ่มใน run_bot() ใน bot/main.py:
#
# [1] import (บนสุดของ main.py):
#     from features.regime import RegimeDetector, get_regime
#
# [2] init ก่อน while loop:
#     regime_detector = RegimeDetector()
#
# [3] ใน while loop — ตรวจ regime ทุก candle (cache 15 นาที):
#
#     # ── [4.5] Market Regime Detection ─────────────────────
#     # ใช้ H4 เพื่อดู big picture (cache 15 min → ไม่ช้า)
#     regime = regime_detector.detect_from_mt5(
#         symbol    = "XAUUSDm",   # หรือ loop แต่ละ symbol
#         timeframe = "H4",
#         bars      = 300,
#     )
#     log.info(f"Regime: {regime}")
#
#     # Optional: skip ถ้า regime ไม่ดี
#     if not regime.should_trade:
#         log.info(f"Regime {regime.regime} (conf={regime.confidence:.2f}) → skip")
#         time.sleep(CYCLE_SLEEP)
#         continue
#     # ─────────────────────────────────────────────────────
#
# [4] ส่ง regime ไป ensemble:
#     result = ensemble.predict_with_regime(
#         features = current_features,
#         regime   = regime,
#         symbol   = symbol,
#     )
#     if result["should_trade"]:
#         executor.execute_order(symbol, result["signal"], ...)
#
# ══════════════════════════════════════════════════════════════
