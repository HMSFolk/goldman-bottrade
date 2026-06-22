# models/train_lgbm.py
"""
LightGBM Training Pipeline
- เร็วกว่า XGBoost 3-10x บน CPU
- Leaf-wise growth (vs level-wise ของ XGB)
- รองรับ categorical features โดยตรง
- Walk-Forward Validation เหมือน XGB
"""

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ✅ FIX: setup_logging ก่อน import อื่น — แก้ "No module named 'bot'"
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

import lightgbm as lgb
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import (
    classification_report,
    f1_score,
    accuracy_score,
)
from sklearn.preprocessing import LabelEncoder

# ✅ FIX: get_config() แทน yaml.safe_load โดยตรง
from config import get_config
CFG = get_config()

log = logging.getLogger("models")


# ══════════════════════════════════════════════════════════════
# ✅ FIX BUG-1: NumpyEncoder — แก้ "Object of type bool is not JSON serializable"
# สาเหตุ: np.mean() คืน numpy.float64, การเปรียบเทียบ numpy.float64 >= float
#          คืน numpy.bool_ (ไม่ใช่ Python bool) ซึ่ง json.dumps() ใน Python 3.13
#          ไม่รู้จัก — ต้องแปลงก่อน
# ══════════════════════════════════════════════════════════════
class NumpyEncoder(json.JSONEncoder):
    """แปลง numpy types → Python built-in types ก่อน JSON serialize"""
    def default(self, obj):
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)

# ✅ FIX: absolute paths จาก project root
PROCESSED_DIR = _ROOT / CFG['paths']['data_processed']
MODELS_DIR    = _ROOT / CFG['paths']['models_saved']
REPORTS_DIR   = _ROOT / CFG['paths']['reports']

NON_FEATURE_COLS = {
    'open','high','low','close',
    'tick_volume','real_volume','spread',
    'label','future_return','risk_adj_return',
}


# ══════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════
@dataclass
class LGBMFoldResult:
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
    best_iteration:int
    n_trades:      int


@dataclass
class LGBMTrainResult:
    symbol:       str
    timeframe:    str
    n_features:   int
    n_samples:    int
    folds:        list = field(default_factory=list)
    model_path:   str  = ""
    trained_at:   str  = ""

    @property
    def mean_accuracy(self) -> float:
        return np.mean([f.accuracy for f in self.folds])

    @property
    def mean_f1(self) -> float:
        return np.mean([f.f1_macro for f in self.folds])

    @property
    def std_accuracy(self) -> float:
        return np.std([f.accuracy for f in self.folds])

    @property
    def avg_best_iter(self) -> float:
        return np.mean([f.best_iteration for f in self.folds])

    def is_acceptable(self) -> bool:
        # ✅ FIX: bool() บังคับแปลง numpy.bool_ → Python bool
        return bool(
            self.mean_accuracy >= 0.50 and
            self.mean_f1       >= 0.40 and
            self.std_accuracy  <= 0.10
        )

    def summary(self) -> str:
        return (
            f"{self.symbol} | "
            f"acc={self.mean_accuracy:.3f}±{self.std_accuracy:.3f} | "
            f"f1={self.mean_f1:.3f} | "
            f"avg_iter={self.avg_best_iter:.0f} | "
            f"features={self.n_features}"
        )


# ══════════════════════════════════════════════════════════════
# Data Preparation
# ══════════════════════════════════════════════════════════════
def load_and_prepare(
    symbol:    str,
    timeframe: str = "M15",
) -> tuple[pd.DataFrame, pd.Series, list, list]:
    """
    โหลด processed data เตรียม X, y, features, cat_features

    ข้อแตกต่างจาก XGB:
    LightGBM รองรับ categorical features โดยตรง
    ไม่ต้อง one-hot encode — เร็วกว่าและแม่นกว่า
    """
    path = PROCESSED_DIR / f"{symbol}_{timeframe}_features.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"ไม่พบ {path}\n"
            f"รัน: python features/pipeline.py ก่อน"
        )

    df = pd.read_parquet(path)
    df = df.dropna(subset=['label'])

    # ── ✅ FIX: ลบ duplicate columns ถ้ามี (ป้องกัน LightGBM error) ──────
    if df.columns.duplicated().any():
        dup_cols = df.columns[df.columns.duplicated()].tolist()
        log.warning(f"พบ duplicate columns ใน parquet: {dup_cols} — ลบออก")
        df = df.loc[:, ~df.columns.duplicated(keep='first')]

    # ── Numeric features ───────────────────────────────────────
    num_features = [
        c for c in df.columns
        if c not in NON_FEATURE_COLS
        and not c.startswith('future_')
        and not c.endswith('_enc')   # ✅ FIX BUG-2: exclude _enc จาก num_features
                                     # เพราะ _enc จะถูกสร้างใหม่ข้างล่าง
                                     # ป้องกัน duplicate ใน all_features
        and df[c].dtype in ['float64','float32','int64','int32']
    ]

    # ── Categorical features ───────────────────────────────────
    # LightGBM จัดการ string category ได้โดยตรง
    cat_features_raw = [
        c for c in df.columns
        if c not in NON_FEATURE_COLS
        and df[c].dtype in ['object','category']
        and c in [
            'rsi_zone', 'vol_regime', 'price_zone_20',   # ✅ FIX BUG-1: ลบ vol_regime ซ้ำออก
            'nearest_pivot_level', 'trend_cat',
        ]
    ]

    # แปลง categorical เป็น int (LightGBM ต้องการ int สำหรับ cat)
    cat_encoded = []
    le_map      = {}
    for col in cat_features_raw:
        le               = LabelEncoder()
        df[f'{col}_enc'] = le.fit_transform(df[col].fillna('unknown'))
        le_map[col]      = le
        cat_encoded.append(f'{col}_enc')

    all_features = num_features + cat_encoded

    # ✅ FIX: ลบ duplicate ใน feature list (ป้องกัน edge cases)
    all_features = list(dict.fromkeys(all_features))

    # ── Quality filter ─────────────────────────────────────────
    # ลบ features ที่มี NaN > 20%
    nan_pct      = df[all_features].isna().mean()
    drop_nan     = nan_pct[nan_pct > 0.20].index.tolist()
    all_features = [c for c in all_features if c not in drop_nan]

    # ลบ constant features
    variance     = df[all_features].var()
    drop_const   = variance[variance == 0].index.tolist()
    all_features = [c for c in all_features if c not in drop_const]

    # cat_features ที่ผ่าน filter
    cat_features = [c for c in cat_encoded if c in all_features]

    X = df[all_features].fillna(-999)   # LightGBM จัดการ -999 ได้ดี
    y = df['label'].astype(int)

    log.info(
        f"โหลด {symbol}_{timeframe}: "
        f"{len(all_features)} features "
        f"({len(cat_features)} categorical) | "
        f"{len(X):,} samples"
    )

    return X, y, all_features, cat_features


# ══════════════════════════════════════════════════════════════
# Walk-Forward Training
# ══════════════════════════════════════════════════════════════
def walk_forward_train(
    X:            pd.DataFrame,
    y:            pd.Series,
    cat_features: list,
    n_splits:     int  = 5,
    gap_bars:     int  = 96,
    params:       dict = None,
) -> tuple[lgb.Booster, LabelEncoder, list]:
    """
    Walk-Forward Training สำหรับ LightGBM

    ข้อแตกต่างจาก XGB:
    - ใช้ lgb.Dataset แทน numpy array โดยตรง
    - early_stopping เป็น callback
    - num_boost_round กำหนดนอก model
    - best_iteration track อัตโนมัติ
    """
    params   = params or _default_lgbm_params()
    tscv     = TimeSeriesSplit(n_splits=n_splits, gap=gap_bars)
    results  = []
    models   = []
    f1_scores= []

    # Label encoder: -1,0,1 → 0,1,2
    le = LabelEncoder()
    le.fit(y)

    log.info(f"Walk-Forward LGBM: {n_splits} folds | gap={gap_bars} bars")
    log.info("=" * 60)

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        t0 = time.time()

        X_tr = X.iloc[train_idx]
        X_te = X.iloc[test_idx]
        y_tr = le.transform(y.iloc[train_idx])
        y_te = le.transform(y.iloc[test_idx])

        # ── LightGBM Dataset ───────────────────────────────────
        # คำนวณ sample_weight เพื่อ balance BUY/SELL/HOLD
        # weight แต่ละ sample = total / (n_classes * count_of_its_class)
        class_counts = np.bincount(y_tr.astype(int))
        total        = len(y_tr)
        n_classes    = len(class_counts)
        weights_per_class = total / (n_classes * np.maximum(class_counts, 1))
        # ❌ BUG-FIX: เดิม boost BUY (index 2) เพิ่ม 1.5x ทำให้ BUY bias แย่ขึ้น
        # ลบออก — ให้ทุก class ได้ weight สมดุลจริงๆ
        sample_weight = weights_per_class[y_tr.astype(int)]

        train_ds = lgb.Dataset(
            X_tr, label=y_tr,
            categorical_feature = cat_features,
            weight              = sample_weight,   # ✅ ใส่ weight
            free_raw_data       = False,
        )
        val_ds   = lgb.Dataset(
            X_te, label=y_te,
            reference           = train_ds,   # ต้องอ้างอิง train
            categorical_feature = cat_features,
            free_raw_data       = False,
        )

        # ── Callbacks ─────────────────────────────────────────
        callbacks = [
            lgb.early_stopping(
                stopping_rounds = 50,
                verbose         = False,
            ),
            lgb.log_evaluation(period=-1),   # -1 = silent
        ]

        # ── Train ──────────────────────────────────────────────
        model = lgb.train(
            params          = params,
            train_set       = train_ds,
            num_boost_round = 1000,
            valid_sets      = [val_ds],
            callbacks       = callbacks,
        )

        # ── Evaluate ───────────────────────────────────────────
        y_pred_raw = model.predict(X_te)
        # y_pred_raw shape: (n_samples, n_classes)
        y_pred_enc = y_pred_raw.argmax(axis=1).astype(int)
        y_pred     = le.inverse_transform(y_pred_enc)
        y_test_orig= le.inverse_transform(y_te)

        acc = accuracy_score(y_test_orig, y_pred)
        f1  = f1_score(
            y_test_orig, y_pred,
            average    = 'macro',
            zero_division = 0,
        )

        report  = classification_report(
            y_test_orig, y_pred,
            target_names  = ['SELL','HOLD','BUY'],
            output_dict   = True,
            zero_division = 0,
        )
        buy_p   = report.get('BUY',{}).get('precision', 0)
        buy_r   = report.get('BUY',{}).get('recall',    0)
        n_buy   = (y_pred == 1).sum()
        best_it = model.best_iteration

        f1_scores.append(f1)
        models.append(model)
        elapsed = time.time() - t0

        fold_result = LGBMFoldResult(
            fold           = fold + 1,
            train_start    = str(X_tr.index[0])[:10],
            train_end      = str(X_tr.index[-1])[:10],
            test_start     = str(X_te.index[0])[:10],
            test_end       = str(X_te.index[-1])[:10],
            train_size     = len(X_tr),
            test_size      = len(X_te),
            accuracy       = round(acc, 4),
            f1_macro       = round(f1,  4),
            buy_precision  = round(buy_p, 4),
            buy_recall     = round(buy_r, 4),
            best_iteration = best_it,
            n_trades       = int(n_buy),
        )
        results.append(fold_result)

        log.info(
            f"Fold {fold+1}/{n_splits}: "
            f"acc={acc:.3f} f1={f1:.3f} "
            f"BUY_p={buy_p:.3f} BUY_r={buy_r:.3f} "
            f"iter={best_it} trades={n_buy} ({elapsed:.1f}s)"
        )

    # สรุป
    accs = [r.accuracy  for r in results]
    f1s  = [r.f1_macro  for r in results]
    iters= [r.best_iteration for r in results]

    log.info("=" * 60)
    log.info(
        f"Summary: "
        f"acc={np.mean(accs):.3f}±{np.std(accs):.3f} | "
        f"f1={np.mean(f1s):.3f}±{np.std(f1s):.3f} | "
        f"avg_iter={np.mean(iters):.0f}"
    )

    # เลือก best model
    best_idx   = int(np.argmax(f1_scores))
    best_model = models[best_idx]
    log.info(f"Best model: fold {best_idx+1} (f1={f1_scores[best_idx]:.4f})")

    return best_model, le, results


# ══════════════════════════════════════════════════════════════
# Main Training Function
# ══════════════════════════════════════════════════════════════
def train_lightgbm(
    symbol:       str,
    timeframe:    str  = "M15",
    n_splits:     int  = 5,
    use_hyperopt: bool = False,
) -> LGBMTrainResult:
    """Train LightGBM สำหรับ 1 symbol"""

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(exist_ok=True)

    log.info(f"\n{'='*60}")
    log.info(f"Training LightGBM: {symbol}_{timeframe}")
    log.info(f"{'='*60}")

    t_start = time.time()

    # ── 1. Load ────────────────────────────────────────────────
    X, y, features, cat_features = load_and_prepare(symbol, timeframe)

    # ── 2. Params ──────────────────────────────────────────────
    if use_hyperopt:
        params = _hyperopt_params(X, y, cat_features)
    else:
        params = _default_lgbm_params()

    # ── 3. Walk-Forward ────────────────────────────────────────
    gap_bars = CFG['training']['wf_gap_bars']
    model, le, fold_results = walk_forward_train(
        X, y, cat_features,
        n_splits = n_splits,
        gap_bars = gap_bars,
        params   = params,
    )

    # ── 4. Feature Importance ──────────────────────────────────
    _save_feature_importance(model, features, symbol)

    # ── 5. Final Evaluation ────────────────────────────────────
    _final_evaluation(model, le, X, y, symbol)

    # ── 6. Save ────────────────────────────────────────────────
    model_path = _save_model(
        model, le, features, cat_features, params, symbol, timeframe
    )

    # ── 7. Result ──────────────────────────────────────────────
    result = LGBMTrainResult(
        symbol     = symbol,
        timeframe  = timeframe,
        n_features = len(features),
        n_samples  = len(X),
        folds      = fold_results,
        model_path = str(model_path),
        trained_at = datetime.now(timezone.utc).isoformat(),
    )

    elapsed = time.time() - t_start
    log.info(f"\n✅ เสร็จ ({elapsed:.1f}s): {result.summary()}")

    if not result.is_acceptable():
        log.warning("⚠️ โมเดลยังไม่ผ่านเกณฑ์")

    _save_report(result, symbol, timeframe)
    return result


# ══════════════════════════════════════════════════════════════
# Helper Functions
# ══════════════════════════════════════════════════════════════
def _default_lgbm_params() -> dict:
    """
    params สำหรับ multi-class classification

    ข้อสำคัญ:
    - num_class ต้องตรงกับจำนวน class (3: SELL/HOLD/BUY)
    - objective ต้องเป็น multiclass
    - num_leaves ควร < 2^max_depth
    """
    p = CFG['training']['lgbm_params']
    return {
        # Task
        'objective'          : 'multiclass',
        'num_class'          : 3,
        'metric'             : 'multi_logloss',
        'verbose'            : -1,          # ปิด output

        # Tree structure (Leaf-wise — ต่างจาก XGB)
        'num_leaves'         : p.get('num_leaves',       63),
        'max_depth'          : -1,          # -1 = ไม่จำกัด
        # ✅ FIX BUG-2: ลด min_data_in_leaf 50→20 เพื่อให้ BUY/SELL มีโอกาสสร้าง leaf ได้มากขึ้น
        # BUY มักมี sample น้อย ถ้า min=50 โมเดลไม่ยอมแตก leaf สำหรับ BUY
        'min_data_in_leaf'   : p.get('min_data_in_leaf', 20),

        # Learning
        'learning_rate'      : p.get('learning_rate',  0.03),
        'n_estimators'       : p.get('num_boost_round',1000),

        # Sampling (ป้องกัน overfitting)
        'feature_fraction'   : p.get('feature_fraction', 0.8),
        'bagging_fraction'   : p.get('bagging_fraction', 0.8),
        'bagging_freq'       : 5,

        # Regularization
        'reg_alpha'          : 0.1,         # L1
        'reg_lambda'         : 1.0,         # L2
        'min_gain_to_split'  : 0.01,

        # ✅ FIX BUG-2: ปิด is_unbalance แล้วใช้ sample_weight แทน
        # is_unbalance=True + sample_weight พร้อมกันจะขัดแย้งกัน
        'is_unbalance'       : False,       # ใช้ sample_weight แทน (ใน Dataset)

        # Speed
        'num_threads'        : 4,
        'force_col_wise'     : True,
    }


def _hyperopt_params(
    X:            pd.DataFrame,
    y:            pd.Series,
    cat_features: list,
) -> dict:
    """Optuna hyperparameter search สำหรับ LightGBM"""
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    le = LabelEncoder()
    le.fit(y)

    def objective(trial):
        params = {
            'objective'       : 'multiclass',
            'num_class'       : 3,
            'metric'          : 'multi_logloss',
            'verbose'         : -1,
            'num_leaves'      : trial.suggest_int('leaves',  20, 200),
            'min_data_in_leaf': trial.suggest_int('mdl',     10, 100),
            'learning_rate'   : trial.suggest_float('lr', 0.01, 0.1, log=True),
            'feature_fraction': trial.suggest_float('ff',  0.5, 1.0),
            'bagging_fraction': trial.suggest_float('bf',  0.5, 1.0),
            'bagging_freq'    : trial.suggest_int('bfreq',   1, 10),
            'reg_alpha'       : trial.suggest_float('ra', 0.0, 2.0),
            'reg_lambda'      : trial.suggest_float('rl', 0.0, 2.0),
            'is_unbalance'    : False,       # ✅ FIX: ใช้ sample_weight แทน (ไม่ conflict)
            'num_threads'     : 4,
            'force_col_wise'  : True,
        }

        split   = int(len(X) * 0.80)
        y_e     = le.transform(y)

        tr_ds   = lgb.Dataset(
            X.iloc[:split], y_e[:split],
            categorical_feature=cat_features, free_raw_data=False
        )
        vl_ds   = lgb.Dataset(
            X.iloc[split:], y_e[split:],
            reference=tr_ds, free_raw_data=False
        )

        model   = lgb.train(
            params, tr_ds,
            num_boost_round = 500,
            valid_sets      = [vl_ds],
            callbacks       = [
                lgb.early_stopping(30, verbose=False),
                lgb.log_evaluation(-1),
            ],
        )

        y_pred  = model.predict(X.iloc[split:]).argmax(axis=1)
        y_pred  = le.inverse_transform(y_pred.astype(int))
        return f1_score(y.iloc[split:], y_pred,
                        average='macro', zero_division=0)

    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=50, show_progress_bar=False)

    log.info(f"Optuna best f1={study.best_value:.4f}")
    best = study.best_params
    return {
        'objective'       : 'multiclass',
        'num_class'       : 3,
        'metric'          : 'multi_logloss',
        'verbose'         : -1,
        'num_leaves'      : best['leaves'],
        'min_data_in_leaf': best['mdl'],
        'learning_rate'   : best['lr'],
        'feature_fraction': best['ff'],
        'bagging_fraction': best['bf'],
        'bagging_freq'    : best['bfreq'],
        'reg_alpha'       : best['ra'],
        'reg_lambda'      : best['rl'],
        'is_unbalance'    : False,       # ✅ FIX: ใช้ sample_weight แทน
        'num_threads'     : 4,
        'force_col_wise'  : True,
    }


def _save_feature_importance(
    model:    lgb.Booster,
    features: list,
    symbol:   str,
):
    """บันทึก feature importance 2 แบบ: gain และ split"""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        for imp_type in ['gain', 'split']:
            imp = pd.Series(
                model.feature_importance(importance_type=imp_type),
                index=features,
            ).nlargest(20)

            fig, ax = plt.subplots(figsize=(10, 8))
            imp.sort_values().plot(
                kind='barh', ax=ax, color='#1D9E75'
            )
            ax.set_title(
                f"Top 20 Feature Importance ({imp_type}) — {symbol}"
            )
            ax.set_xlabel(f"Importance ({imp_type})")
            plt.tight_layout()
            out = REPORTS_DIR / f"lgbm_importance_{imp_type}_{symbol}.png"
            plt.savefig(out, dpi=120)
            plt.close()

        log.info(f"บันทึก feature importance → reports/")
    except Exception as e:
        log.warning(f"ไม่สามารถ plot: {e}")


def _final_evaluation(
    model:  lgb.Booster,
    le:     LabelEncoder,
    X:      pd.DataFrame,
    y:      pd.Series,
    symbol: str,
):
    """ประเมินผลบน 20% สุดท้าย"""
    split      = int(len(X) * 0.80)
    X_test     = X.iloc[split:]
    y_test     = y.iloc[split:]

    y_pred_raw = model.predict(X_test)
    y_pred_enc = y_pred_raw.argmax(axis=1).astype(int)
    y_pred     = le.inverse_transform(y_pred_enc)

    log.info("\n── Final Out-of-Sample (last 20%) ──")
    log.info(f"\n{classification_report(y_test, y_pred, target_names=['SELL','HOLD','BUY'], zero_division=0)}")


def _save_model(
    model:        lgb.Booster,
    le:           LabelEncoder,
    features:     list,
    cat_features: list,
    params:       dict,
    symbol:       str,
    timeframe:    str,
) -> Path:
    """บันทึกโมเดลพร้อม metadata"""
    payload = {
        'model'       : model,
        'label_encoder': le,
        'features'    : features,
        'cat_features': cat_features,
        'params'      : params,
        'meta'        : {
            'symbol'     : symbol,
            'timeframe'  : timeframe,
            'n_features' : len(features),
            'trained_at' : datetime.now(timezone.utc).isoformat(),
            'lgbm_version': lgb.__version__,
        },
    }

    path = MODELS_DIR / f"lgbm_{symbol}.pkl"
    joblib.dump(payload, path)
    log.info(f"💾 บันทึก → {path}")
    return path


def _save_report(result: LGBMTrainResult, symbol: str, timeframe: str):
    report = {
        'symbol'        : result.symbol,
        'timeframe'     : result.timeframe,
        'n_features'    : result.n_features,
        'n_samples'     : result.n_samples,
        'trained_at'    : result.trained_at,
        'is_acceptable' : result.is_acceptable(),
        'mean_accuracy' : round(result.mean_accuracy, 4),
        'mean_f1'       : round(result.mean_f1,       4),
        'std_accuracy'  : round(result.std_accuracy,  4),
        'avg_best_iter' : round(result.avg_best_iter,  1),
        'folds': [
            {
                'fold'          : f.fold,
                'train_period'  : f"{f.train_start}→{f.train_end}",
                'test_period'   : f"{f.test_start}→{f.test_end}",
                'accuracy'      : f.accuracy,
                'f1_macro'      : f.f1_macro,
                'buy_precision' : f.buy_precision,
                'buy_recall'    : f.buy_recall,
                'best_iteration': f.best_iteration,
                'n_trades'      : f.n_trades,
            }
            for f in result.folds
        ],
    }
    out = REPORTS_DIR / f"train_report_lgbm_{symbol}_{timeframe}.json"
    # ✅ FIX: cls=NumpyEncoder แปลง numpy.bool_/float64/int64 → Python types
    out.write_text(json.dumps(report, indent=2, cls=NumpyEncoder), encoding="utf-8")
    log.info(f"📄 บันทึก report → {out}")


# ══════════════════════════════════════════════════════════════
# Load & Predict
# ══════════════════════════════════════════════════════════════
def load_model(symbol: str) -> dict:
    """โหลดโมเดลที่ train แล้ว"""
    path = MODELS_DIR / f"lgbm_{symbol}.pkl"
    if not path.exists():
        raise FileNotFoundError(
            f"ไม่พบ {path}\n"
            f"รัน: python models/train_lgbm.py ก่อน"
        )
    payload = joblib.load(path)
    log.info(
        f"โหลด LGBM {symbol}: "
        f"{payload['meta']['n_features']} features | "
        f"trained {payload['meta']['trained_at'][:10]}"
    )
    return payload


def predict(
    payload:   dict,
    df:        pd.DataFrame,
    min_conf:  float = 0.60,
) -> dict:
    """Predict signal จาก row ล่าสุด"""
    model    = payload['model']
    le       = payload['label_encoder']
    features = payload['features']

    avail    = [f for f in features if f in df.columns]
    X_live   = df[avail].tail(1).fillna(-999)

    proba    = model.predict(X_live)[0]   # shape: (3,)
    pred_idx = int(proba.argmax())
    conf     = float(proba[pred_idx])

    direction_map = {0: -1, 1: 0, 2: 1}
    direction     = direction_map[pred_idx]

    if conf < min_conf:
        direction = 0

    return {
        'direction'  : direction,
        'confidence' : round(conf,         4),
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
    parser.add_argument("--symbols",   nargs="+",
                        default=CFG['symbols']['active'])
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--splits",    type=int, default=5)
    parser.add_argument("--hyperopt",  action="store_true")
    args = parser.parse_args()

    for sym in args.symbols:
        try:
            train_lightgbm(
                symbol       = sym,
                timeframe    = args.timeframe,
                n_splits     = args.splits,
                use_hyperopt = args.hyperopt,
            )
        except Exception as e:
            log.error(f"❌ {sym}: {e}", exc_info=True)