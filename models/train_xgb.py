# models/train_xgb.py
"""
XGBoost Training Pipeline
- Walk-Forward Validation (time-series aware)
- Feature Selection
- Model Persistence
- Performance Evaluation
"""

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ✅ FIX CRITICAL-1: setup_logging ก่อน import อื่น
from bot.setup_logging import setup_logging
setup_logging()

import logging
import json
import time
import joblib
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from dataclasses import dataclass, field

import xgboost as xgb
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import (
    classification_report,
    confusion_matrix,
    accuracy_score,
    f1_score,
)
from sklearn.feature_selection import mutual_info_classif
from sklearn.preprocessing import LabelEncoder

# ✅ FIX CRITICAL-2: get_config() แทน yaml.safe_load
from config import get_config
CFG = get_config()

log = logging.getLogger("models")

# ✅ FIX CRITICAL-3: absolute paths จาก project root
PROCESSED_DIR = _ROOT / CFG['paths']['data_processed']
MODELS_DIR    = _ROOT / CFG['paths']['models_saved']
REPORTS_DIR   = _ROOT / CFG['paths']['reports']

# columns ที่ไม่ใช่ feature
NON_FEATURE_COLS = {
    'open','high','low','close',
    'tick_volume','real_volume','spread',
    'label','future_return','risk_adj_return',
}


# ══════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════
@dataclass
class FoldResult:
    """ผล 1 fold ของ walk-forward"""
    fold:          int
    train_start:   str
    train_end:     str
    test_start:    str
    test_end:      str
    train_size:    int
    test_size:     int
    accuracy:      float
    f1_macro:      float
    buy_precision: float
    buy_recall:    float
    n_trades:      int    # จำนวน BUY signal ใน test


@dataclass
class TrainResult:
    """ผลรวมทั้งหมดหลัง train"""
    symbol:        str
    timeframe:     str
    n_features:    int
    n_samples:     int
    folds:         list = field(default_factory=list)
    model_path:    str  = ""
    trained_at:    str  = ""

    @property
    def mean_accuracy(self) -> float:
        return np.mean([f.accuracy for f in self.folds])

    @property
    def mean_f1(self) -> float:
        return np.mean([f.f1_macro for f in self.folds])

    @property
    def std_accuracy(self) -> float:
        return np.std([f.accuracy for f in self.folds])

    def is_acceptable(self) -> bool:
        """โมเดลผ่านเกณฑ์ขั้นต่ำหรือเปล่า"""
        return (
            self.mean_accuracy >= 0.50 and
            self.mean_f1       >= 0.40 and
            self.std_accuracy  <= 0.10    # ไม่ volatile ข้าม fold
        )

    def summary(self) -> str:
        return (
            f"{self.symbol} | "
            f"acc={self.mean_accuracy:.3f}±{self.std_accuracy:.3f} | "
            f"f1={self.mean_f1:.3f} | "
            f"features={self.n_features} | "
            f"samples={self.n_samples:,}"
        )


# ══════════════════════════════════════════════════════════════
# Feature Preparation
# ══════════════════════════════════════════════════════════════
def load_and_prepare(
    symbol:    str,
    timeframe: str = "M15",
) -> tuple[pd.DataFrame, pd.Series, list]:
    """
    โหลด processed data และเตรียม X, y, feature_names
    """
    path = PROCESSED_DIR / f"{symbol}_{timeframe}_features.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"ไม่พบ {path}\n"
            f"รัน: python features/pipeline.py ก่อน"
        )

    df = pd.read_parquet(path)
    log.info(f"โหลด {symbol}_{timeframe}: {df.shape}")

    # ── ตรวจสอบ label ─────────────────────────────────────────
    if 'label' not in df.columns:
        raise ValueError("ไม่มี column 'label' — รัน features/pipeline.py ใหม่")

    # ลบ rows ที่ label เป็น NaN (forward bars สุดท้าย)
    df = df.dropna(subset=['label'])

    # ── เลือก Feature Columns ──────────────────────────────────
    feature_cols = [
        c for c in df.columns
        if c not in NON_FEATURE_COLS
        and not c.startswith('future_')
        and df[c].dtype in ['float64','float32','int64','int32']
    ]

    # ลบ features ที่มี NaN มากเกินไป (> 20%)
    nan_pct  = df[feature_cols].isna().mean()
    drop_nan = nan_pct[nan_pct > 0.20].index.tolist()
    if drop_nan:
        log.warning(f"ลบ {len(drop_nan)} features ที่มี NaN > 20%: {drop_nan[:5]}...")
        feature_cols = [c for c in feature_cols if c not in drop_nan]

    # ลบ features ที่มี variance = 0 (constant)
    variance    = df[feature_cols].var()
    drop_const  = variance[variance == 0].index.tolist()
    if drop_const:
        log.warning(f"ลบ {len(drop_const)} constant features")
        feature_cols = [c for c in feature_cols if c not in drop_const]

    X = df[feature_cols].fillna(0)
    y = df['label'].astype(int)

    log.info(
        f"Features: {len(feature_cols)} | "
        f"Samples: {len(X):,} | "
        f"Label dist: BUY={( y==1).mean():.1%} "
        f"HOLD={(y==0).mean():.1%} "
        f"SELL={(y==-1).mean():.1%}"
    )

    return X, y, feature_cols


def select_top_features(
    X: pd.DataFrame,
    y: pd.Series,
    top_n: int = 50,
) -> list:
    """
    เลือก top N features ด้วย Mutual Information
    ลด noise และเพิ่ม generalization

    ใช้ MI เพราะ:
    - จับ non-linear relationship ได้
    - เหมาะกับ tree-based model
    - เร็วกว่า permutation importance
    """
    log.info(f"Feature selection: {len(X.columns)} → {top_n} features...")

    # แปลง label เป็น binary (BUY vs อื่น) สำหรับ MI
    y_binary = (y == 1).astype(int)

    mi_scores = mutual_info_classif(
        X.fillna(0),
        y_binary,
        random_state = 42,
        n_neighbors  = 5,
    )

    mi_df = pd.DataFrame({
        'feature': X.columns,
        'mi_score': mi_scores,
    }).sort_values('mi_score', ascending=False)

    # บันทึก feature importance ranking
    mi_df.to_csv(REPORTS_DIR / "feature_mi_scores.csv", index=False)

    top_features = mi_df.head(top_n)['feature'].tolist()
    log.info(f"Top 5 features: {top_features[:5]}")

    return top_features


# ══════════════════════════════════════════════════════════════
# Walk-Forward Validation
# ══════════════════════════════════════════════════════════════
def walk_forward_train(
    X:           pd.DataFrame,
    y:           pd.Series,
    n_splits:    int   = 5,
    gap_bars:    int   = 96,      # 1 วัน สำหรับ M15
    params:      dict  = None,
) -> tuple[xgb.XGBClassifier, list]:
    """
    Walk-Forward Training + Validation

    วิธีการ:
    |─────Train1────|─gap─|─Test1─|
                    |─────Train2────|─gap─|─Test2─|
                                    ...

    gap_bars: ช่องว่างระหว่าง train และ test
    ป้องกัน data leakage จาก autocorrelation
    """
    params = params or _default_xgb_params()

    tscv    = TimeSeriesSplit(n_splits=n_splits, gap=gap_bars)
    results = []
    models  = []

    log.info(f"Walk-Forward: {n_splits} folds | gap={gap_bars} bars")
    log.info("=" * 60)

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        t0 = time.time()

        X_train = X.iloc[train_idx]
        X_test  = X.iloc[test_idx]
        y_train = y.iloc[train_idx]
        y_test  = y.iloc[test_idx]

        # แปลง label -1,0,1 → 0,1,2 (XGBoost ต้องการ non-negative)
        le     = LabelEncoder()
        y_tr_e = le.fit_transform(y_train)
        y_te_e = le.transform(y_test)

        # ── Train ──────────────────────────────────────────────
        model = xgb.XGBClassifier(
            **params,
            num_class        = 3,
            objective        = 'multi:softprob',
            eval_metric      = 'mlogloss',
            random_state     = 42 + fold,
            verbosity        = 0,
        )

        model.fit(
            X_train, y_tr_e,
            eval_set              = [(X_test, y_te_e)],
            #early_stopping_rounds = 50,
            verbose               = False,
        )

        # ── Evaluate ───────────────────────────────────────────
        y_pred  = model.predict(X_test)
        y_pred_orig = le.inverse_transform(y_pred)

        acc     = accuracy_score(y_test, y_pred_orig)
        f1      = f1_score(y_test, y_pred_orig,
                           average='macro', zero_division=0)

        # BUY precision/recall (สำคัญที่สุด)
        report  = classification_report(
            y_test, y_pred_orig,
            target_names=['SELL','HOLD','BUY'],
            output_dict=True, zero_division=0,
        )
        buy_p   = report.get('BUY',{}).get('precision', 0)
        buy_r   = report.get('BUY',{}).get('recall', 0)
        n_buy   = (y_pred_orig == 1).sum()

        elapsed = time.time() - t0

        fold_result = FoldResult(
            fold          = fold + 1,
            train_start   = str(X_train.index[0])[:10],
            train_end     = str(X_train.index[-1])[:10],
            test_start    = str(X_test.index[0])[:10],
            test_end      = str(X_test.index[-1])[:10],
            train_size    = len(X_train),
            test_size     = len(X_test),
            accuracy      = round(acc, 4),
            f1_macro      = round(f1, 4),
            buy_precision = round(buy_p, 4),
            buy_recall    = round(buy_r, 4),
            n_trades      = int(n_buy),
        )
        results.append(fold_result)
        models.append(model)

        log.info(
            f"Fold {fold+1}/{n_splits}: "
            f"acc={acc:.3f} f1={f1:.3f} "
            f"BUY_prec={buy_p:.3f} BUY_rec={buy_r:.3f} "
            f"trades={n_buy} ({elapsed:.1f}s)"
        )

    # ── Summary ────────────────────────────────────────────────
    accs = [r.accuracy for r in results]
    f1s  = [r.f1_macro for r in results]
    log.info("=" * 60)
    log.info(
        f"Walk-Forward Summary: "
        f"acc={np.mean(accs):.3f}±{np.std(accs):.3f} | "
        f"f1={np.mean(f1s):.3f}±{np.std(f1s):.3f}"
    )

    # เลือกโมเดลที่ดีที่สุด (highest f1)
    best_idx   = np.argmax(f1s)
    best_model = models[best_idx]
    log.info(f"Best model: fold {best_idx+1} (f1={f1s[best_idx]:.3f})")

    return best_model, results


# ══════════════════════════════════════════════════════════════
# Main Training Function
# ══════════════════════════════════════════════════════════════
def train_xgboost(
    symbol:         str,
    timeframe:      str   = "M15",
    n_splits:       int   = 5,
    top_features:   int   = 50,
    use_hyperopt:   bool  = False,
) -> TrainResult:
    """
    Train XGBoost สำหรับ 1 symbol
    บันทึกโมเดลลง models/saved/
    """
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(exist_ok=True)

    log.info(f"\n{'='*60}")
    log.info(f"Training XGBoost: {symbol}_{timeframe}")
    log.info(f"{'='*60}")

    t_start = time.time()

    # ── 1. Load & Prepare ──────────────────────────────────────
    X, y, all_features = load_and_prepare(symbol, timeframe)

    # ── 2. Feature Selection ───────────────────────────────────
    if len(all_features) > top_features:
        selected = select_top_features(X, y, top_n=top_features)
        X = X[selected]
    else:
        selected = all_features

    # ── 3. Hyperparameter ──────────────────────────────────────
    if use_hyperopt:
        params = _hyperopt_params(X, y)
    else:
        params = _default_xgb_params()

    # ── 4. Walk-Forward Training ───────────────────────────────
    gap_bars = CFG['training']['wf_gap_bars']
    model, fold_results = walk_forward_train(
        X, y, n_splits=n_splits, gap_bars=gap_bars, params=params
    )

    # ── 5. Final Evaluation (Out-of-Sample) ───────────────────
    _final_evaluation(model, X, y, symbol)

    # ── 6. Save Model ──────────────────────────────────────────
    model_path = _save_model(model, selected, params, symbol, timeframe)

    # ── 7. Build Result ────────────────────────────────────────
    result = TrainResult(
        symbol     = symbol,
        timeframe  = timeframe,
        n_features = len(selected),
        n_samples  = len(X),
        folds      = fold_results,
        model_path = str(model_path),
        trained_at = datetime.now(timezone.utc).isoformat(),
    )

    elapsed = time.time() - t_start
    log.info(f"\n✅ Training เสร็จ ({elapsed:.1f}s)")
    log.info(f"   {result.summary()}")

    if not result.is_acceptable():
        log.warning(
            "⚠️ โมเดลยังไม่ผ่านเกณฑ์!\n"
            "   acc < 50% หรือ std สูงเกินไป\n"
            "   แนะนำ: เพิ่มข้อมูล / ปรับ feature / ปรับ label threshold"
        )

    # บันทึก report
    _save_report(result, symbol, timeframe)

    return result


# ══════════════════════════════════════════════════════════════
# Helper Functions
# ══════════════════════════════════════════════════════════════
def _default_xgb_params() -> dict:
    """โหลด params จาก config.yaml"""
    p = CFG['training']['xgb_params'].copy()
    return {
        'n_estimators'    : p.get('n_estimators',     500),
        'learning_rate'   : p.get('learning_rate',   0.03),
        'max_depth'       : p.get('max_depth',           6),
        'min_child_weight': p.get('min_child_weight',   10),
        'subsample'       : p.get('subsample',         0.8),
        'colsample_bytree': p.get('colsample_bytree',  0.8),
        'reg_alpha'       : 0.1,    # L1 regularization
        'reg_lambda'      : 1.0,    # L2 regularization
        'tree_method'     : 'hist', # เร็วที่สุด
        'device'          : 'cpu',  # เปลี่ยน 'cuda' ถ้ามี GPU
    }


def _hyperopt_params(X: pd.DataFrame, y: pd.Series) -> dict:
    """หา hyperparameter ที่ดีที่สุดด้วย Optuna"""
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def objective(trial):
        params = {
            'n_estimators'    : trial.suggest_int('n', 200, 800),
            'learning_rate'   : trial.suggest_float('lr', 0.01, 0.1, log=True),
            'max_depth'       : trial.suggest_int('depth', 3, 8),
            'min_child_weight': trial.suggest_int('mcw', 5, 50),
            'subsample'       : trial.suggest_float('sub', 0.6, 1.0),
            'colsample_bytree': trial.suggest_float('col', 0.6, 1.0),
            'num_class'       : 3,
            'objective'       : 'multi:softprob',
            'eval_metric'     : 'mlogloss',
            'verbosity'       : 0,
            'tree_method'     : 'hist',
        }
        split   = int(len(X) * 0.8)
        le      = LabelEncoder()
        y_e     = le.fit_transform(y)
        model   = xgb.XGBClassifier(**params, random_state=42)
        model.fit(
            X.iloc[:split], y_e[:split],
            eval_set             = [(X.iloc[split:], y_e[split:])],
            early_stopping_rounds = 30,
            verbose              = False,
        )
        pred = le.inverse_transform(model.predict(X.iloc[split:]))
        return f1_score(y.iloc[split:], pred, average='macro', zero_division=0)

    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=50, show_progress_bar=False)

    log.info(f"Optuna best f1: {study.best_value:.4f}")
    log.info(f"Best params: {study.best_params}")

    best = study.best_params
    return {
        'n_estimators'    : best['n'],
        'learning_rate'   : best['lr'],
        'max_depth'       : best['depth'],
        'min_child_weight': best['mcw'],
        'subsample'       : best['sub'],
        'colsample_bytree': best['col'],
        'reg_alpha'       : 0.1,
        'reg_lambda'      : 1.0,
        'tree_method'     : 'hist',
    }


def _final_evaluation(
    model:  xgb.XGBClassifier,
    X:      pd.DataFrame,
    y:      pd.Series,
    symbol: str,
):
    """ประเมินผลบน 20% สุดท้าย (out-of-sample)"""
    split    = int(len(X) * 0.80)
    X_test   = X.iloc[split:]
    y_test   = y.iloc[split:]

    le       = LabelEncoder()
    le.fit(y)
    y_test_e = le.transform(y_test)

    y_pred_e = model.predict(X_test)
    y_pred   = le.inverse_transform(y_pred_e)

    log.info("\n── Final Out-of-Sample Evaluation (last 20%) ──")
    log.info(f"\n{classification_report(y_test, y_pred, target_names=['SELL','HOLD','BUY'], zero_division=0)}")

    # Confusion matrix
    cm = confusion_matrix(y_test, y_pred, labels=[-1, 0, 1])
    log.info(f"Confusion Matrix (SELL/HOLD/BUY):\n{cm}")

    # บันทึกรูป feature importance
    _plot_feature_importance(model, X.columns.tolist(), symbol)


def _plot_feature_importance(
    model:    xgb.XGBClassifier,
    features: list,
    symbol:   str,
):
    """บันทึก feature importance ลง reports/"""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        imp  = pd.Series(
            model.feature_importances_,
            index=features
        ).nlargest(20)

        fig, ax = plt.subplots(figsize=(10, 8))
        imp.sort_values().plot(kind='barh', ax=ax, color='#f0b429')
        ax.set_title(f"Top 20 Feature Importance — {symbol}")
        ax.set_xlabel("Importance Score")
        plt.tight_layout()
        plt.savefig(REPORTS_DIR / f"importance_{symbol}.png", dpi=120)
        plt.close()
        log.info(f"บันทึก feature importance → reports/importance_{symbol}.png")
    except Exception as e:
        log.warning(f"ไม่สามารถ plot feature importance: {e}")


def _save_model(
    model:     xgb.XGBClassifier,
    features:  list,
    params:    dict,
    symbol:    str,
    timeframe: str,
) -> Path:
    """
    บันทึกโมเดลพร้อม metadata
    รูปแบบ: {"model": xgb, "features": [...], "params": {...}, "meta": {...}}
    """
    payload = {
        'model'    : model,
        'features' : features,
        'params'   : params,
        'meta'     : {
            'symbol'      : symbol,
            'timeframe'   : timeframe,
            'n_features'  : len(features),
            'trained_at'  : datetime.now(timezone.utc).isoformat(),
            'xgb_version' : xgb.__version__,
        },
    }

    path = MODELS_DIR / f"xgb_{symbol}.pkl"
    joblib.dump(payload, path)
    log.info(f"💾 บันทึกโมเดล → {path}")
    return path


def _save_report(
    result:    TrainResult,
    symbol:    str,
    timeframe: str,
):
    """บันทึก training report เป็น JSON"""
    report = {
        'symbol'    : result.symbol,
        'timeframe' : result.timeframe,
        'n_features': result.n_features,
        'n_samples' : result.n_samples,
        'trained_at': result.trained_at,
        'model_path': result.model_path,
        'is_acceptable': result.is_acceptable(),
        'mean_accuracy': round(result.mean_accuracy, 4),
        'mean_f1'      : round(result.mean_f1,       4),
        'std_accuracy' : round(result.std_accuracy,  4),
        'folds': [
            {
                'fold'         : f.fold,
                'train_period' : f"{f.train_start} → {f.train_end}",
                'test_period'  : f"{f.test_start} → {f.test_end}",
                'accuracy'     : f.accuracy,
                'f1_macro'     : f.f1_macro,
                'buy_precision': f.buy_precision,
                'buy_recall'   : f.buy_recall,
                'n_trades'     : f.n_trades,
            }
            for f in result.folds
        ],
    }

    out = REPORTS_DIR / f"train_report_{symbol}_{timeframe}.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    log.info(f"📄 บันทึก report → {out}")


# ══════════════════════════════════════════════════════════════
# Load & Predict (ใช้ใน bot/main.py)
# ══════════════════════════════════════════════════════════════
def load_model(symbol: str) -> dict:
    """โหลดโมเดลที่ train แล้ว"""
    path = MODELS_DIR / f"xgb_{symbol}.pkl"
    if not path.exists():
        raise FileNotFoundError(
            f"ไม่พบ {path}\n"
            f"รัน: python models/train_xgb.py ก่อน"
        )
    payload = joblib.load(path)
    log.info(
        f"โหลด XGB {symbol}: "
        f"{payload['meta']['n_features']} features | "
        f"trained {payload['meta']['trained_at'][:10]}"
    )
    return payload


def predict(
    payload:    dict,
    df:         pd.DataFrame,
    min_conf:   float = 0.60,
) -> dict:
    """
    Predict signal จาก 1 row ล่าสุด
    คืนค่า direction + confidence + probabilities
    """
    model    = payload['model']
    features = payload['features']

    # เอาเฉพาะ feature ที่โมเดลรู้จัก
    avail    = [f for f in features if f in df.columns]
    X_live   = df[avail].tail(1).fillna(0)

    proba    = model.predict_proba(X_live)[0]   # [sell, hold, buy]
    pred_idx = proba.argmax()
    conf     = proba[pred_idx]

    # ✅ FIX MEDIUM: ลบ LabelEncoder ที่สร้างแล้วไม่ได้ใช้ (dead code)
    #    direction_map ทำหน้าที่ mapping แทนอยู่แล้ว
    direction_map = {0: -1, 1: 0, 2: 1}
    direction     = direction_map[pred_idx]

    # ถ้า confidence ต่ำกว่า threshold → HOLD
    if conf < min_conf:
        direction = 0

    return {
        'direction'  : direction,
        'confidence' : round(float(conf), 4),
        'proba_sell' : round(float(proba[0]), 4),
        'proba_hold' : round(float(proba[1]), 4),
        'proba_buy'  : round(float(proba[2]), 4),
    }


# ══════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols",    nargs="+",
                        default=CFG['symbols']['active'])
    parser.add_argument("--timeframe",  default="M15")
    parser.add_argument("--splits",     type=int, default=5)
    parser.add_argument("--features",   type=int, default=50)
    parser.add_argument("--hyperopt",   action="store_true")
    args = parser.parse_args()

    all_results = {}
    for sym in args.symbols:
        try:
            result = train_xgboost(
                symbol       = sym,
                timeframe    = args.timeframe,
                n_splits     = args.splits,
                top_features = args.features,
                use_hyperopt = args.hyperopt,
            )
            all_results[sym] = result
        except Exception as e:
            log.error(f"❌ Train {sym} ล้มเหลว: {e}", exc_info=True)

    # สรุปทุก symbol
    log.info("\n" + "="*60)
    log.info("Training Summary:")
    for sym, r in all_results.items():
        status = "✅" if r.is_acceptable() else "⚠️"
        log.info(f"  {status} {r.summary()}")