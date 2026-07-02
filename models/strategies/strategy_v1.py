# models/strategies/strategy_v1.py
"""
Strategy V1 — Production
════════════════════════════════════════════════════════════
สถานะ: PRODUCTION — ห้ามแก้ไขขณะบอทรันอยู่
ผ่าน backtest: 2023-01-01 → 2024-12-31
Sharpe: 1.43 | MaxDD: 12.1% | WinRate: 52.3% | PF: 1.47

⚠️ AUDIT WARNING (2026-06-28): ตัวเลข backtest ด้านบน "ไม่ได้" วัดระบบที่ deploy จริง
   models/backtest.py วัดแบบ:
     - long-only (entries=BUY, exits=SELL → SELL = ปิดไม้ ไม่ใช่ short)
     - SL/TP คงที่ 0.5%/1.0% → RR = 2.0  (ไม่ใช่ tp_ratio จาก config)
     - min_conf = 0.62  (ไม่ใช่ 0.52 ที่ใช้จริง)
   ระบบจริงเป็น long+short, ATR-based SL, conf 0.52
   → WR 52.3% นั้นวัดที่ RR 2.0 (คุ้มทุนที่ 33%) จึงเป็นบวก
     แต่พอ deploy ที่ RR 0.7 (คุ้มทุน 59%) กลายเป็นขาดทุน
   อย่าใช้ตัวเลขชุดนี้ตัดสินใจ deploy จนกว่าจะมี backtest ที่ตรงกับระบบจริง
Deploy date: 2025-05-25
════════════════════════════════════════════════════════════

Logic:
  สัญญาณ BUY/SELL มาจาก Ensemble (XGB+LGBM+LSTM)
  กรองด้วย rule-based conditions หลายชั้น
  ปรับ SL/TP ตาม ATR และ volatility regime
  บล็อก trade เมื่อ HTF ขัดแย้งหรือ session ไม่เหมาะ
"""
import logging
import yaml
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from pathlib import Path
from config import get_config
CFG = get_config()   # ใช้ config.py (มี env injection + validate) — อย่าอ่าน yaml ตรง

log = logging.getLogger("models")

_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Version Control ────────────────────────────────────────────
VERSION       = "2.1.3"
DEPLOY_DATE   = "2026-07-2"
BACKTEST_SHARPE    = 1.43
BACKTEST_MAX_DD    = 12.1
BACKTEST_WIN_RATE  = 52.3
BACKTEST_PF        = 1.47

# ══════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════
@dataclass
class TradeSetup:
    """
    ผลลัพธ์จาก strategy — ส่งไปยัง executor
    ถ้า direction == 0 หมายถึงไม่เทรด
    """
    direction:     int    = 0       # 1=BUY -1=SELL 0=HOLD
    confidence:    float  = 0.0
    sl_distance:   float  = 0.0     # ระยะ SL เป็น price units
    tp_distance:   float  = 0.0     # ระยะ TP เป็น price units
    sl_pct:        float  = 0.0     # SL เป็น % ของราคา
    tp_pct:        float  = 0.0     # TP เป็น % ของราคา
    rr_ratio:      float  = 0.0     # Risk:Reward ratio
    reasons:       list   = field(default_factory=list)
    filters_passed:list   = field(default_factory=list)
    filters_failed:list   = field(default_factory=list)
    regime:        str    = "normal"    # volatility regime
    session:       str    = "unknown"   # trading session

    @property
    def is_valid(self) -> bool:
        return (
            self.direction  != 0 and
            self.confidence >= CFG['signal']['min_confidence'] and
            self.sl_distance > 0 and
            self.tp_distance > 0 and
            self.rr_ratio    >= 0.5
        )

    def __str__(self):
        if self.direction == 0:
            return f"HOLD | failed={self.filters_failed}"
        arrow = "↑ BUY" if self.direction == 1 else "↓ SELL"
        return (
            f"{arrow} | conf={self.confidence:.3f} | "
            f"SL={self.sl_pct:.3f}% TP={self.tp_pct:.3f}% "
            f"RR={self.rr_ratio:.1f} | "
            f"regime={self.regime} session={self.session}"
        )

# ══════════════════════════════════════════════════════════════
# Strategy V1 Class
# ══════════════════════════════════════════════════════════════
class StrategyV1:
    """
    Production Strategy V1

    Pipeline:
    1. ML Ensemble Signal   → direction + confidence
    2. HTF Filter           → block ถ้า H1/H4 ขัดแย้ง
    3. Volatility Filter    → block ถ้า squeeze ON / ATR สูงผิดปกติ
    4. Session Filter       → เทรดเฉพาะ London+NY
    5. Pattern Filter       → ยืนยันด้วย candle pattern
    6. SL/TP Calculation    → ATR-based dynamic SL/TP
    7. RR Check             → ต้องได้ RR ≥ 1.5
    """

    VERSION = VERSION

    def __init__(self):
        self._ensemble  = None   # lazy load
        self._loaded_for: dict = {}
        self._regime_detector = None   # lazy load RegimeDetector (ใช้ใน _step_ensemble)

        log.info(
            f"StrategyV1 v{VERSION} | "
            f"deploy={DEPLOY_DATE} | "
            f"backtest: sharpe={BACKTEST_SHARPE} "
            f"dd={BACKTEST_MAX_DD}% "
            f"wr={BACKTEST_WIN_RATE}%"
        )

    # ── Lazy Load Ensemble ────────────────────────────────────
    def _get_ensemble(self, symbol: str):
        """โหลด ensemble ครั้งเดียวต่อ symbol"""
        if symbol not in self._loaded_for:
            from models.ensemble import create_ensemble
            self._loaded_for[symbol] = create_ensemble(symbol)
            log.info(f"Loaded ensemble for {symbol}")
        return self._loaded_for[symbol]

    # ══════════════════════════════════════════════════════════
    # Main Entry Point
    # ══════════════════════════════════════════════════════════
    def evaluate(
        self,
        df:     pd.DataFrame,
        symbol: str,
        regime = None,) -> TradeSetup:
        """
        ประเมินว่าควรเทรดหรือเปล่า

        ใช้ใน bot/main.py:
            setup = strategy.evaluate(df, symbol)
            if setup.is_valid:
                executor.send_order(...)

        regime: ถ้าส่ง RegimeState มา จะใช้เลย (ไม่ detect ซ้ำ) — backtest ส่งมา
                เพื่อให้ detector ตัวเดียวกัน + hysteresis สะสมต่อเนื่อง = ตรง live
                ถ้า None → _step_ensemble detect เอง (main.py เรียกแบบนี้)
        """
        setup = TradeSetup()
        row   = df.iloc[-1]

        # ── Step 1: ML Ensemble Signal ─────────────────────────
        ml_signal = self._step_ensemble(df, symbol, regime=regime)
        if ml_signal is None:
            setup.filters_failed.append("ensemble_unavailable")
            return setup

        setup.direction  = ml_signal['direction']
        setup.confidence = ml_signal['confidence']
        setup.reasons.append(
            f"ensemble:{ml_signal['direction']} "
            f"conf={ml_signal['confidence']:.3f} "
            f"agree={ml_signal.get('n_agree',0)}/"
            f"{ml_signal.get('n_models',0)}"
        )

        if setup.direction == 0:
            setup.filters_failed.append("ensemble_hold")
            return setup

        setup.filters_passed.append("ensemble_signal")

        # ── Step 2: Confidence Threshold ──────────────────────
        passed, reason = self._check_confidence(setup.confidence)
        if not passed:
            setup.direction = 0
            setup.filters_failed.append(reason)
            return setup
        setup.filters_passed.append("confidence")

        # ── Step 3: HTF Alignment ──────────────────────────────
        passed, reason = self._check_htf_alignment(row, setup.direction)
        if not passed:
            setup.direction = 0
            setup.filters_failed.append(reason)
            return setup
        setup.filters_passed.append("htf_alignment")

        # ── Step 4: Volatility Filter ──────────────────────────
        passed, reason, regime = self._check_volatility(row)
        if not passed:
            setup.direction = 0
            setup.filters_failed.append(reason)
            return setup
        setup.regime = regime
        setup.filters_passed.append(f"volatility:{regime}")

        # ── Step 5: Session Filter ─────────────────────────────
        passed, reason, session = self._check_session(row)
        if not passed:
            setup.direction = 0
            setup.filters_failed.append(reason)
            return setup
        setup.session = session
        setup.filters_passed.append(f"session:{session}")

        # ── Step 6: Pattern Confirmation ──────────────────────
        passed, reason = self._check_pattern(row, setup.direction)
        if not passed:
            # pattern ไม่ยืนยัน — ลด confidence แต่ไม่บล็อก
            setup.confidence *= 0.85
            setup.reasons.append(f"pattern_warn:{reason}")
        else:
            setup.confidence = min(setup.confidence * 1.05, 0.99)
            setup.filters_passed.append("pattern_confirm")

        # ── Step 7: Market Structure ───────────────────────────
        passed, reason = self._check_structure(row, setup.direction)
        if not passed:
            setup.confidence *= 0.90
            setup.reasons.append(f"structure_warn:{reason}")
        else:
            setup.filters_passed.append("structure_ok")

        # ── Step 8: SL/TP Calculation ──────────────────────────
        sl_dist, tp_dist = self._calc_sl_tp(row, setup.direction, regime, symbol)
        if sl_dist <= 0 or tp_dist <= 0:
            setup.direction = 0
            setup.filters_failed.append("sl_tp_invalid")
            return setup

        price          = float(row['close'])
        setup.sl_distance = sl_dist
        setup.tp_distance = tp_dist
        setup.sl_pct   = sl_dist / price * 100
        setup.tp_pct   = tp_dist / price * 100
        setup.rr_ratio = round(tp_dist / sl_dist, 2)

        # ── Step 9: RR Check ───────────────────────────────────
        # ✅ FIX: อ่าน min_rr จาก config แทน hardcode 1.0
        # ถ้า tp_ratio=0.5 → RR=0.5 → ต้องตั้ง min_rr=0.5 ใน config ด้วย
        min_rr = CFG['order'].get('min_rr', 1.0)
        if setup.rr_ratio < min_rr:
            setup.direction = 0
            setup.filters_failed.append(
                f"rr_too_low ({setup.rr_ratio:.2f} < {min_rr})"
            )
            return setup
        setup.filters_passed.append(f"rr:{setup.rr_ratio:.1f}")

        # ── Step 10: Final Confidence Check ────────────────────
        # ✅ FIX: อ่าน use_final_conf_check จาก config
        # config: use_final_conf_check: false → ข้ามได้เลย
        use_final_conf = CFG.get('strategy_filters', {}).get(
            'use_final_conf_check', True
        )
        if use_final_conf and setup.confidence < CFG['signal']['min_confidence']:
            setup.direction = 0
            setup.filters_failed.append(
                f"final_conf_low ({setup.confidence:.3f})"
            )
            return setup

        log.info(f"{symbol}: {setup}")
        return setup

    # ══════════════════════════════════════════════════════════
    # Filter Steps
    # ══════════════════════════════════════════════════════════
    def _step_ensemble(
        self, df: pd.DataFrame, symbol: str, regime = None ) -> dict | None:
        """
        รัน ML Ensemble และคืน signal dict

        ✅ FIX (2026-06-30): ใช้ predict_with_regime (regime-weighted) ให้ตรงกับ
           live (main.py) ที่เรียก ensemble.predict_with_regime() เดิม _step_ensemble
           ใช้ predict(method="soft") → backtest/v1 คนละ "สมอง" กับ live →
           backtest ไม่สะท้อน live จริง ตอนนี้ทั้ง live/backtest/v1 ใช้ path เดียวกัน

           regime: ถ้าส่งมา (จาก backtest) ใช้เลย — detector ตัวเดียว hysteresis ต่อเนื่อง
                   ถ้า None detect เอง (lazy-load) fallback soft ถ้า detect ไม่ได้
        """
        try:
            ensemble = self._get_ensemble(symbol)

            # ถ้า caller ไม่ส่ง regime มา → detect เอง (เหมือน main.py path)
            if regime is None:
                try:
                    if self._regime_detector is None:
                        from features.regime import RegimeDetector
                        self._regime_detector = RegimeDetector()
                    regime = self._regime_detector.detect(df, symbol)
                except Exception as e:
                    log.debug(f"{symbol}: regime detect ไม่ได้ ({e}) — fallback soft")

            if regime is not None:
                reg = ensemble.predict_with_regime(df, regime, symbol)
                signal = reg["signal"]
                # ถ้า regime path block → คืน HOLD (direction=0) ให้ evaluate หยุด
                if not reg["should_trade"]:
                    return {
                        'direction': 0, 'confidence': signal.confidence,
                        'n_agree': signal.n_agree, 'n_models': signal.n_models,
                        'conflict': signal.conflict_score,
                    }
            else:
                signal = ensemble.predict(df, method="soft")

            return {
                'direction' : signal.direction,
                'confidence': signal.confidence,
                'n_agree'   : signal.n_agree,
                'n_models'  : signal.n_models,
                'conflict'  : signal.conflict_score,
            }
        except Exception as e:
            log.error(f"Ensemble error: {e}", exc_info=True)
            return None

    def _check_confidence(
        self, confidence: float ) -> tuple[bool, str]:
        """ตรวจ confidence ขั้นต่ำ"""
        min_conf = CFG['signal']['min_confidence']
        if confidence < min_conf:
            return False, f"conf_low({confidence:.3f}<{min_conf})"
        return True, ""

    def _check_htf_alignment(
        self, row: pd.Series, direction: int ) -> tuple[bool, str]:
        """ตรวจไทม์เฟรมใหญ่ (H1, H4) รองรับการเปิด/ปิดผ่าน config.yaml"""
        # ดึงค่าสวิตช์จาก config.yaml
        use_htf = CFG.get('strategy_filters', {}).get('use_htf_filter', True)
        
        # ถ้าตั้งค่า false ไว้ใน config ให้ปล่อยผ่านทันที
        if not use_htf:
            return True, ""

        conflict = row.get('htf_conflict', 0)
        if conflict == 1:
            return False, "htf_conflict(H1vsH4)"

        h4_ema = row.get('h4_ema_alignment', 2)
        if direction == 1 and h4_ema <= 1:
            return False, f"h4_strong_bear(align={h4_ema})"
        if direction == -1 and h4_ema >= 3:
            return False, f"h4_strong_bull(align={h4_ema})"

        h1_rsi = row.get('h1_rsi_14', 50)
        if direction == 1 and h1_rsi > 75:
            return False, f"h1_overbought(rsi={h1_rsi:.0f})"
        if direction == -1 and h1_rsi < 25:
            return False, f"h1_oversold(rsi={h1_rsi:.0f})"

        return True, ""

    def _check_volatility(
        self, row: pd.Series ) -> tuple[bool, str, str]:
        """ตรวจ volatility conditions รองรับการเปิด/ปิดผ่าน config.yaml"""
        # ดึงค่าสวิตช์จาก config.yaml
        filters_cfg = CFG.get('strategy_filters', {})
        use_squeeze = filters_cfg.get('use_squeeze_filter', True)
        use_atr_low = filters_cfg.get('use_atr_low_filter', True)

        squeeze = row.get('squeeze_on', 0)
        
        # ถ้าสวิตช์เป็น True และกราฟบีบตัว -> บล็อก
        if use_squeeze and squeeze == 1:
            bars = row.get('squeeze_bars', 0)
            return False, f"squeeze_on({int(bars)}bars)", "squeeze"

        # ATR สูงผิดปกติ (กันพอร์ตแตกตอนข่าว อันนี้บังคับเปิดไว้เสมอ)
        atr_ratio = row.get('atr_ratio', 1.0)
        if atr_ratio > 2.5:
            return False, f"atr_spike({atr_ratio:.1f}x)", "high"

        # ถ้าสวิตช์เป็น True และวอลุ่มต่ำ -> บล็อก
        if use_atr_low and atr_ratio < 0.3:
            return False, f"atr_too_low({atr_ratio:.1f}x)", "low"

        regime = row.get('vol_regime', 'normal')
        if isinstance(regime, float):
            regime = 'normal'
            
        if squeeze == 1:
            regime = "squeeze"

        return True, "", regime

    def _check_session(
        self, row: pd.Series) -> tuple[bool, str, str]:
        """
        เช็ค session ตาม config.yaml symbol_sessions
        รองรับทุก session (Sydney/Tokyo/London/NY) ผ่าน config
        """
        is_london  = row.get('is_london_session',  0)
        is_ny      = row.get('is_ny_session',      0)
        is_overlap = row.get('is_overlap_session', 0)
        is_tokyo   = row.get('is_tokyo_session',   0)
        is_sydney  = row.get('is_sydney_session',  0)

        # อ่าน allowed sessions จาก config (ถ้าไม่มี default = London+NY)
        sf_cfg = CFG.get('session_filter', {})
        allowed = sf_cfg.get('allowed_sessions',
                             ['London', 'New York'])

        session_map = {
            'London'  : is_london,
            'New York': is_ny,
            'Tokyo'   : is_tokyo,
            'Sydney'  : is_sydney,
        }

        in_session = any(
            session_map.get(s, 0) == 1 for s in allowed
        )

        if not in_session:
            return False, "outside_session", "closed"

        # ระบุชื่อ session ปัจจุบัน
        if is_overlap == 1:
            session = "overlap"
        elif is_london == 1:
            session = "london"
        elif is_ny == 1:
            session = "newyork"
        elif is_tokyo == 1:
            session = "tokyo"
        elif is_sydney == 1:
            session = "sydney"
        else:
            session = "open"

        return True, "", session

    def _check_pattern(
        self, row: pd.Series, direction: int ) -> tuple[bool, str]:
        """
        ตรวจ candle pattern ยืนยัน signal
        ไม่ block แต่ปรับ confidence
        """
        pa_score  = row.get('pa_score', 0)
        pat_net   = row.get('pat_net_score', 0)

        if direction == 1:
            # BUY: อยากเห็น bullish pattern
            if pa_score < -2:
                return False, f"bearish_pa({pa_score:.1f})"
            if pat_net < -1:
                return False, f"bearish_pattern({pat_net})"
        else:
            # SELL
            if pa_score > 2:
                return False, f"bullish_pa({pa_score:.1f})"
            if pat_net > 1:
                return False, f"bullish_pattern({pat_net})"

        return True, ""

    def _check_structure(
        self, row: pd.Series, direction: int ) -> tuple[bool, str]:
        """
        ตรวจ market structure
        เทรดตาม trend ไม่เทรดสวน structure
        """
        struct_score = row.get('structure_score', 0)
        bos_bull     = row.get('bos_bull', 0)
        bos_bear     = row.get('bos_bear', 0)

        if direction == 1:
            if struct_score < -3:
                return False, f"bearish_structure({struct_score})"
            if bos_bear == 1:
                return False, "bearish_bos"
        else:
            if struct_score > 3:
                return False, f"bullish_structure({struct_score})"
            if bos_bull == 1:
                return False, "bullish_bos"

        return True, ""

    def _calc_sl_tp(
        self,
        row:       pd.Series,
        direction: int,
        regime:    str,
        symbol:    str = "default", ) -> tuple[float, float]:
        """
        คำนวณ SL/TP แบบ ATR-based dynamic

        Regime adjustments:
        - high   : SL กว้างขึ้น 1.5x เพราะ noisy
        - normal : SL ปกติ 1.5x ATR
        - low    : SL แคบลง 1.0x เพราะ range เล็ก

        TP = SL × RR_ratio (default 2.0 จาก config)
        """
        atr14    = row.get('atr_14',    0)
        atr7     = row.get('atr_7',     0)
        price    = row.get('close',     0)

        if atr14 <= 0 or price <= 0:
            # Fallback: ใช้ % จาก config
            sl_dist = price * CFG['order']['sl_points'].get(
                'default', 130
            ) * 0.00001
            tp_dist = sl_dist * CFG['order']['tp_ratio']
            return sl_dist, tp_dist

        # ใช้ ATR เฉลี่ย 2 ช่วง (เสถียรกว่า)
        atr_avg  = (atr14 * 0.7 + atr7 * 0.3)

        # SL multiplier ตาม regime
        sl_mult  = {
            'high'  : 2.0,
            'normal': 1.5,
            'low'   : 1.0,
            'squeeze': 1.2,
        }.get(regime, 1.5)

        # Overlap session — ขยาย SL นิดหน่อยเพราะ volatile
        session  = row.get('is_overlap_session', 0)
        if session == 1:
            sl_mult *= 1.1

        sl_dist  = atr_avg * sl_mult
        tp_ratio = CFG['order']['tp_ratio']   # อ่านจาก config (ปัจจุบัน 1.8)
        tp_dist  = sl_dist * tp_ratio

        # Sanity check — SL ไม่ควรเกิน 3% ของราคา
        max_sl   = price * 0.03
        if sl_dist > max_sl:
            sl_dist = max_sl
            tp_dist = sl_dist * tp_ratio

        # ดึง spread จาก config มาคุม min SL
        # ดึง spread จาก config มาคุม min SL (ใช้ symbol param ที่ส่งจาก evaluate)
        sf_lim = CFG.get("spread_filter", {}).get("limits", {})
        spread = float(sf_lim.get(symbol, sf_lim.get("default", 0.0)))

        # SL ไม่ควรน้อยกว่า 0.1% ของราคา (ถูก stop ง่ายเกินไป) และครอบ spread 2 เท่า
        min_sl   = max(price * 0.001, spread * 2)
        if sl_dist < min_sl:
            sl_dist = min_sl
            tp_dist = sl_dist * tp_ratio

        return round(sl_dist, 5), round(tp_dist, 5)

    # ══════════════════════════════════════════════════════════
    # Diagnostic Tools
    # ══════════════════════════════════════════════════════════
    def explain(
        self, df: pd.DataFrame, symbol: str ) -> str:
        """
        อธิบายว่าทำไมถึงให้ signal นั้น
        ใช้ debug และ review
        """
        setup = self.evaluate(df, symbol)
        lines = [
            f"Strategy V1 Explanation — {symbol}",
            f"{'='*50}",
            f"Signal:   {setup}",
            f"",
            f"✅ Passed filters ({len(setup.filters_passed)}):",
        ]
        for f in setup.filters_passed:
            lines.append(f"   • {f}")

        if setup.filters_failed:
            lines.append(f"")
            lines.append(
                f"❌ Failed filters ({len(setup.filters_failed)}):"
            )
            for f in setup.filters_failed:
                lines.append(f"   • {f}")

        lines += [
            f"",
            f"📝 Reasons:",
        ]
        for r in setup.reasons:
            lines.append(f"   • {r}")

        return "\n".join(lines)

    def get_version_info(self) -> dict:
        """ข้อมูล version และ backtest performance"""
        return {
            'version'          : VERSION,
            'deploy_date'      : DEPLOY_DATE,
            'backtest_sharpe'  : BACKTEST_SHARPE,
            'backtest_max_dd'  : BACKTEST_MAX_DD,
            'backtest_win_rate': BACKTEST_WIN_RATE,
            'backtest_pf'      : BACKTEST_PF,
        }
# ══════════════════════════════════════════════════════════════
# __init__.py ของ strategies/
# ══════════════════════════════════════════════════════════════