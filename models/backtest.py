<<<<<<< HEAD
# models/backtest.py
"""
Backtest Engine
- vectorbt: backtest เร็วมาก รองรับ vectorized operations
- Walk-Forward: ทดสอบแบบ out-of-sample หลาย fold
- Optuna: หา SL/TP และ parameter ที่ดีที่สุด
- Deploy Checklist: ตรวจทุกข้อก่อน live
"""

import logging
import logging.config
import yaml
import json
import time
import warnings
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field

import vectorbt as vbt
import optuna
from sklearn.model_selection import TimeSeriesSplit

warnings.filterwarnings('ignore')
optuna.logging.set_verbosity(optuna.logging.WARNING)

with open("logging.yaml", encoding="utf-8") as f:
    logging.config.dictConfig(yaml.safe_load(f))
with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

log = logging.getLogger("models")

PROCESSED_DIR = Path(CFG['paths']['data_processed'])
MODELS_DIR    = Path(CFG['paths']['models_saved'])
REPORTS_DIR   = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)

# Timeframe → pandas freq string
TF_FREQ = {
    "M1": "1T", "M5": "5T", "M15": "15T",
    "H1": "1H", "H4": "4H", "D1":  "1D",
}


# ══════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════
@dataclass
class BacktestMetrics:
    """metrics ครบจาก 1 backtest run"""
    symbol:           str
    timeframe:        str
    period_start:     str
    period_end:       str
    total_return_pct: float
    sharpe_ratio:     float
    sortino_ratio:    float
    max_drawdown_pct: float
    win_rate_pct:     float
    profit_factor:    float
    total_trades:     int
    avg_trade_pct:    float
    best_trade_pct:   float
    worst_trade_pct:  float
    avg_duration_hrs: float
    sl_pct:           float = 0.0
    tp_pct:           float = 0.0

    def is_acceptable(self) -> bool:
        """ผ่านเกณฑ์ขั้นต่ำทุกข้อ"""
        return (
            self.sharpe_ratio      >= 1.0  and
            self.max_drawdown_pct  <= 20.0 and
            self.win_rate_pct      >= 45.0 and
            self.profit_factor     >= 1.3  and
            self.total_trades      >= 50   and
            self.total_return_pct  >  0.0
        )

    def grade(self) -> str:
        """ให้เกรดโดยรวม"""
        score = sum([
            self.sharpe_ratio      >= 1.5,
            self.sharpe_ratio      >= 2.0,
            self.max_drawdown_pct  <= 15.0,
            self.max_drawdown_pct  <= 10.0,
            self.win_rate_pct      >= 50.0,
            self.win_rate_pct      >= 55.0,
            self.profit_factor     >= 1.5,
            self.profit_factor     >= 2.0,
            self.total_return_pct  >= 20.0,
            self.total_return_pct  >= 50.0,
        ])
        if score >= 8: return "A+"
        if score >= 6: return "A"
        if score >= 4: return "B"
        if score >= 2: return "C"
        return "F"

    def summary(self) -> str:
        grade = self.grade()
        ok    = "✅" if self.is_acceptable() else "❌"
        return (
            f"{ok} [{grade}] {self.symbol} | "
            f"ret={self.total_return_pct:+.1f}% | "
            f"sharpe={self.sharpe_ratio:.2f} | "
            f"dd={self.max_drawdown_pct:.1f}% | "
            f"wr={self.win_rate_pct:.1f}% | "
            f"pf={self.profit_factor:.2f} | "
            f"trades={self.total_trades}"
        )


@dataclass
class WalkForwardResult:
    """ผล walk-forward ทุก fold"""
    symbol:    str
    timeframe: str
    folds:     list = field(default_factory=list)
    params:    dict = field(default_factory=dict)

    @property
    def mean_sharpe(self) -> float:
        sharpes = [f.sharpe_ratio for f in self.folds
                   if f.sharpe_ratio != float('nan')]
        return np.mean(sharpes) if sharpes else 0.0

    @property
    def mean_return(self) -> float:
        return np.mean([f.total_return_pct for f in self.folds])

    @property
    def consistency(self) -> float:
        """% ของ fold ที่กำไร — ยิ่งสูงยิ่งดี"""
        profitable = sum(1 for f in self.folds
                         if f.total_return_pct > 0)
        return profitable / max(len(self.folds), 1)

    def is_robust(self) -> bool:
        """โมเดล robust หรือเปล่า"""
        return (
            self.mean_sharpe  >= 0.8 and
            self.consistency  >= 0.6 and    # กำไร ≥ 60% ของ fold
            self.mean_return  >  0.0
        )

    def summary(self) -> str:
        ok = "✅" if self.is_robust() else "⚠️"
        return (
            f"{ok} Walk-Forward {self.symbol}: "
            f"sharpe={self.mean_sharpe:.2f} | "
            f"ret={self.mean_return:+.1f}% | "
            f"consistency={self.consistency:.0%} "
            f"({sum(1 for f in self.folds if f.total_return_pct>0)}"
            f"/{len(self.folds)} folds profitable)"
        )


# ══════════════════════════════════════════════════════════════
# Signal Generator
# ══════════════════════════════════════════════════════════════
def generate_signals(
    df:        pd.DataFrame,
    symbol:    str,
    method:    str = "ensemble",  # "ensemble" | "xgb" | "lgbm" | "rule"
    min_conf:  float = 0.62,
) -> tuple[pd.Series, pd.Series]:
    """
    Generate entry/exit signals จากโมเดล
    คืน (entries, exits) เป็น boolean Series

    vectorbt ต้องการ:
    entries: True = เปิด BUY ที่ bar นี้
    exits:   True = ปิด position ที่ bar นี้
    """
    signals_dir = pd.Series(0, index=df.index)

    if method == "ensemble":
        from models.ensemble import create_ensemble
        ensemble = create_ensemble(symbol)
        for i in range(100, len(df)):     # warmup 100 bars
            signal = ensemble.predict(
                df.iloc[:i+1], method="soft"
            )
            signals_dir.iloc[i] = signal.direction

    elif method == "xgb":
        from models.train_xgb import load_model, predict
        payload = load_model(symbol)
        for i in range(50, len(df)):
            r = predict(payload, df.iloc[:i+1], min_conf=min_conf)
            signals_dir.iloc[i] = r['direction']

    elif method == "lgbm":
        from models.train_lgbm import load_model, predict
        payload = load_model(symbol)
        for i in range(50, len(df)):
            r = predict(payload, df.iloc[:i+1], min_conf=min_conf)
            signals_dir.iloc[i] = r['direction']

    elif method == "rule":
        from models.rule_based import RuleBasedStrategy
        strategy = RuleBasedStrategy()
        for i, (_, row) in enumerate(df.iterrows()):
            sig = strategy.generate_signal(row)
            signals_dir.iloc[i] = sig.direction

    entries = signals_dir ==  1   # BUY signal
    exits   = signals_dir == -1   # SELL/close signal

    n_buy  = entries.sum()
    n_sell = exits.sum()
    log.info(f"Signals: BUY={n_buy} SELL={n_sell}")

    return entries, exits


# ══════════════════════════════════════════════════════════════
# Core Backtest
# ══════════════════════════════════════════════════════════════
def run_backtest(
    df:           pd.DataFrame,
    entries:      pd.Series,
    exits:        pd.Series,
    symbol:       str,
    timeframe:    str    = "M15",
    sl_pct:       float  = 0.005,
    tp_pct:       float  = 0.010,
    init_cash:    float  = 10_000.0,
    commission:   float  = 0.0001,
    slippage:     float  = 0.0001,
    size_pct:     float  = 0.02,        # 2% ของ balance ต่อ trade
) -> BacktestMetrics:
    """
    รัน backtest ด้วย vectorbt

    vectorbt ทำงานแบบ vectorized — เร็วมาก
    10,000 bars เสร็จใน < 1 วินาที
    """
    freq = TF_FREQ.get(timeframe, "15T")
    price= df['close']

    if entries.sum() == 0:
        log.warning(f"ไม่มี entry signal เลย!")
        return _empty_metrics(symbol, timeframe, df)

    # ── Run vectorbt Portfolio ─────────────────────────────────
    pf = vbt.Portfolio.from_signals(
        close       = price,
        entries     = entries,
        exits       = exits,
        sl_stop     = sl_pct,        # Stop Loss %
        tp_stop     = tp_pct,        # Take Profit %
        init_cash   = init_cash,
        fees        = commission,
        slippage    = slippage,
        size        = size_pct,
        size_type   = 'percent',
        freq        = freq,
    )

    # ── Extract Stats ──────────────────────────────────────────
    stats  = pf.stats()
    trades = pf.trades.records_readable

    if len(trades) == 0:
        return _empty_metrics(symbol, timeframe, df)

    # duration เป็นชั่วโมง
    if 'Duration' in trades.columns:
        dur_hrs = trades['Duration'].dt.total_seconds().mean() / 3600
    else:
        dur_hrs = 0.0

    def safe(key, default=0.0):
        v = stats.get(key, default)
        return float(v) if not pd.isna(v) else default

    metrics = BacktestMetrics(
        symbol            = symbol,
        timeframe         = timeframe,
        period_start      = str(df.index[0])[:10],
        period_end        = str(df.index[-1])[:10],
        total_return_pct  = safe('Total Return [%]'),
        sharpe_ratio      = safe('Sharpe Ratio'),
        sortino_ratio     = safe('Sortino Ratio'),
        max_drawdown_pct  = abs(safe('Max Drawdown [%]')),
        win_rate_pct      = safe('Win Rate [%]'),
        profit_factor     = safe('Profit Factor', 1.0),
        total_trades      = int(safe('Total Trades')),
        avg_trade_pct     = safe('Avg Winning Trade [%]'),
        best_trade_pct    = safe('Max Winning Trade [%]'),
        worst_trade_pct   = safe('Max Losing Trade [%]'),
        avg_duration_hrs  = round(dur_hrs, 2),
        sl_pct            = sl_pct,
        tp_pct            = tp_pct,
    )

    log.info(metrics.summary())
    return metrics, pf


def _empty_metrics(
    symbol: str, timeframe: str, df: pd.DataFrame
) -> BacktestMetrics:
    return BacktestMetrics(
        symbol=symbol, timeframe=timeframe,
        period_start=str(df.index[0])[:10],
        period_end=str(df.index[-1])[:10],
        total_return_pct=0, sharpe_ratio=0,
        sortino_ratio=0, max_drawdown_pct=100,
        win_rate_pct=0, profit_factor=0,
        total_trades=0, avg_trade_pct=0,
        best_trade_pct=0, worst_trade_pct=0,
        avg_duration_hrs=0,
    )


# ══════════════════════════════════════════════════════════════
# Walk-Forward Backtest
# ══════════════════════════════════════════════════════════════
def walk_forward_backtest(
    df:         pd.DataFrame,
    symbol:     str,
    timeframe:  str   = "M15",
    n_splits:   int   = 5,
    method:     str   = "ensemble",
    sl_pct:     float = 0.005,
    tp_pct:     float = 0.010,
) -> WalkForwardResult:
    """
    Walk-Forward Backtest
    แต่ละ fold test บนข้อมูลที่โมเดลไม่เคยเห็น

    |────Train1────|──Test1──|
                  |────Train2────|──Test2──|
                               ...
    """
    tscv   = TimeSeriesSplit(n_splits=n_splits, gap=96)
    result = WalkForwardResult(
        symbol=symbol, timeframe=timeframe,
        params={'sl_pct': sl_pct, 'tp_pct': tp_pct},
    )

    log.info(f"\nWalk-Forward Backtest: {symbol} | {n_splits} folds")
    log.info("=" * 60)

    for fold, (train_idx, test_idx) in enumerate(tscv.split(df)):
        t0 = time.time()

        df_test  = df.iloc[test_idx]
        entries, exits = generate_signals(
            df_test, symbol, method=method
        )

        fold_metrics, _ = run_backtest(
            df_test, entries, exits,
            symbol=symbol, timeframe=timeframe,
            sl_pct=sl_pct, tp_pct=tp_pct,
        )
        result.folds.append(fold_metrics)
        elapsed = time.time() - t0

        log.info(
            f"Fold {fold+1}/{n_splits} "
            f"({fold_metrics.period_start}→{fold_metrics.period_end}): "
            f"{fold_metrics.summary()} ({elapsed:.1f}s)"
        )

    log.info("=" * 60)
    log.info(result.summary())
    return result


# ══════════════════════════════════════════════════════════════
# Optuna Optimization
# ══════════════════════════════════════════════════════════════
def optimize_parameters(
    df:          pd.DataFrame,
    entries_fn,               # function(sl, tp) → entries
    exits_fn,                 # function(sl, tp) → exits
    symbol:      str,
    timeframe:   str   = "M15",
    n_trials:    int   = 200,
    objective_fn:str   = "sharpe",  # "sharpe" | "calmar" | "sortino"
) -> dict:
    """
    หา SL/TP และ parameter ที่ดีที่สุดด้วย Optuna
    ใช้ 80% ของข้อมูลเป็น optimization set
    20% สุดท้ายเป็น final test (ห้ามใช้ตอน optimize)
    """
    split     = int(len(df) * 0.80)
    df_opt    = df.iloc[:split]

    log.info(
        f"\nOptuna optimization: {symbol} | "
        f"{n_trials} trials | objective={objective_fn}"
    )

    def objective(trial: optuna.Trial) -> float:
        # ── Parameters ────────────────────────────────────────
        sl_pct = trial.suggest_float('sl_pct', 0.002, 0.025)
        tp_pct = trial.suggest_float('tp_pct', 0.003, 0.040)
        conf   = trial.suggest_float('min_conf', 0.55, 0.80)

        # TP ต้องมากกว่า SL อย่างน้อย 1.2x (RR ≥ 1.2)
        if tp_pct < sl_pct * 1.2:
            return -999.0

        # Generate signals
        entries, exits = entries_fn(df_opt, min_conf=conf)

        # ต้องมี trade พอ
        if entries.sum() < 20:
            return -999.0

        # Run backtest
        result = run_backtest(
            df_opt, entries, exits,
            symbol=symbol, timeframe=timeframe,
            sl_pct=sl_pct, tp_pct=tp_pct,
        )

        if isinstance(result, tuple):
            metrics, _ = result
        else:
            metrics = result

        if metrics.total_trades < 20:
            return -999.0

        # ── Objective ─────────────────────────────────────────
        if objective_fn == "sharpe":
            score = metrics.sharpe_ratio
        elif objective_fn == "calmar":
            # Calmar = return / max_drawdown
            dd    = metrics.max_drawdown_pct / 100 + 1e-9
            score = (metrics.total_return_pct / 100) / dd
        elif objective_fn == "sortino":
            score = metrics.sortino_ratio
        else:
            score = metrics.sharpe_ratio

        # Penalize high drawdown
        if metrics.max_drawdown_pct > 20:
            score -= (metrics.max_drawdown_pct - 20) * 0.1

        # Penalize too few trades
        if metrics.total_trades < 50:
            score -= (50 - metrics.total_trades) * 0.01

        return score if not np.isnan(score) else -999.0

    # ── Run Optuna ─────────────────────────────────────────────
    sampler = optuna.samplers.TPESampler(seed=42)
    study   = optuna.create_study(
        direction = 'maximize',
        sampler   = sampler,
    )
    study.optimize(
        objective,
        n_trials        = n_trials,
        show_progress_bar = True,
        n_jobs          = 1,          # เพิ่มถ้ามี CPU หลาย core
    )

    best_params = study.best_params
    best_value  = study.best_value

    log.info(f"\n✅ Optuna เสร็จ:")
    log.info(f"   Best {objective_fn}: {best_value:.4f}")
    log.info(f"   Best params: {best_params}")

    # ── Plot Optimization History ──────────────────────────────
    _plot_optuna(study, symbol)

    # ── Final Test บน 20% สุดท้าย ──────────────────────────────
    df_test = df.iloc[split:]
    log.info(f"\nFinal test ({df_test.index[0]}→{df_test.index[-1]}):")

    entries_test, exits_test = entries_fn(
        df_test, min_conf=best_params['min_conf']
    )
    final_metrics, final_pf = run_backtest(
        df_test, entries_test, exits_test,
        symbol    = symbol,
        timeframe = timeframe,
        sl_pct    = best_params['sl_pct'],
        tp_pct    = best_params['tp_pct'],
    )
    log.info(f"Final: {final_metrics.summary()}")

    # บันทึก best params
    _save_best_params(best_params, best_value, symbol)

    return {
        'best_params'   : best_params,
        'best_value'    : best_value,
        'final_metrics' : final_metrics,
        'study'         : study,
    }


# ══════════════════════════════════════════════════════════════
# Deploy Checklist
# ══════════════════════════════════════════════════════════════
def run_deploy_checklist(
    symbol:    str,
    timeframe: str   = "M15",
    method:    str   = "ensemble",
) -> dict:
    """
    ตรวจสอบทุกข้อก่อน deploy bot จริง
    ต้องผ่านทุกข้อถึงจะ deploy ได้

    คืนค่า dict ของผลแต่ละข้อ
    """
    log.info(f"\n{'='*60}")
    log.info(f"🚀 Deploy Checklist: {symbol}")
    log.info(f"{'='*60}")

    results  = {}
    df       = pd.read_parquet(
        PROCESSED_DIR / f"{symbol}_{timeframe}_features.parquet"
    ).dropna(subset=['label'])

    # ── ตรวจ 1: โมเดลมีอยู่ครบ ────────────────────────────────
    models_exist = {
        'xgb' : (MODELS_DIR / f"xgb_{symbol}.pkl").exists(),
        'lgbm': (MODELS_DIR / f"lgbm_{symbol}.pkl").exists(),
        'lstm': (MODELS_DIR / f"lstm_{symbol}.pth").exists(),
    }
    results['models_exist'] = {
        'pass'  : models_exist['xgb'] and models_exist['lgbm'],
        'detail': models_exist,
        'note'  : "XGB + LGBM ต้องมีอย่างน้อย",
    }

    # ── ตรวจ 2: Training report ────────────────────────────────
    train_report_path = REPORTS_DIR / \
        f"train_report_{symbol}_{timeframe}.json"

    if train_report_path.exists():
        report    = json.loads(train_report_path.read_text())
        train_ok  = (
            report.get('mean_accuracy', 0) >= 0.50 and
            report.get('mean_f1', 0)       >= 0.40
        )
        results['training_metrics'] = {
            'pass'  : train_ok,
            'detail': {
                'accuracy': report.get('mean_accuracy', 0),
                'f1'      : report.get('mean_f1',       0),
            },
            'note'  : "acc≥50% และ f1≥40%",
        }
    else:
        results['training_metrics'] = {
            'pass'  : False,
            'detail': "ไม่พบ training report",
            'note'  : "รัน train ก่อน",
        }

    # ── ตรวจ 3: Backtest Metrics ───────────────────────────────
    entries, exits = generate_signals(df, symbol, method=method)
    bt_result      = run_backtest(
        df, entries, exits,
        symbol=symbol, timeframe=timeframe,
    )

    if isinstance(bt_result, tuple):
        metrics, pf = bt_result
    else:
        metrics = bt_result
        pf      = None

    results['backtest'] = {
        'pass'  : metrics.is_acceptable(),
        'grade' : metrics.grade(),
        'detail': {
            'return'      : metrics.total_return_pct,
            'sharpe'      : metrics.sharpe_ratio,
            'max_dd'      : metrics.max_drawdown_pct,
            'win_rate'    : metrics.win_rate_pct,
            'pf'          : metrics.profit_factor,
            'trades'      : metrics.total_trades,
        },
        'note'  : (
            "sharpe≥1 | dd≤20% | wr≥45% | "
            "pf≥1.3 | trades≥50"
        ),
    }

    # ── ตรวจ 4: Walk-Forward ───────────────────────────────────
    wf = walk_forward_backtest(
        df, symbol, timeframe,
        n_splits=5, method=method,
    )
    results['walk_forward'] = {
        'pass'  : wf.is_robust(),
        'detail': {
            'mean_sharpe' : round(wf.mean_sharpe, 3),
            'consistency' : f"{wf.consistency:.0%}",
            'mean_return' : round(wf.mean_return, 2),
        },
        'note'  : "sharpe≥0.8 | consistency≥60%",
    }

    # ── ตรวจ 5: Overfitting Check ──────────────────────────────
    split      = int(len(df) * 0.70)
    df_train   = df.iloc[:split]
    df_test    = df.iloc[split:]

    en_tr, ex_tr = generate_signals(df_train, symbol, method)
    en_te, ex_te = generate_signals(df_test,  symbol, method)

    bt_tr = run_backtest(df_train, en_tr, ex_tr,
                         symbol=symbol, timeframe=timeframe)
    bt_te = run_backtest(df_test,  en_te, ex_te,
                         symbol=symbol, timeframe=timeframe)

    if isinstance(bt_tr, tuple): bt_tr = bt_tr[0]
    if isinstance(bt_te, tuple): bt_te = bt_te[0]

    ret_diff    = bt_tr.total_return_pct - bt_te.total_return_pct
    not_overfit = ret_diff < 30.0   # train ไม่ควรดีกว่า test เกิน 30%

    results['overfit_check'] = {
        'pass'  : not_overfit,
        'detail': {
            'train_return': bt_tr.total_return_pct,
            'test_return' : bt_te.total_return_pct,
            'gap'         : ret_diff,
        },
        'note'  : "gap < 30% (train vs test)",
    }

    # ── ตรวจ 6: Risk Parameters ────────────────────────────────
    risk_ok = (
        CFG['risk']['risk_per_trade']   <= 0.02 and
        CFG['risk']['max_daily_loss_pct']<= 0.10 and
        CFG['risk']['max_open_trades']   <= 5
    )
    results['risk_params'] = {
        'pass'  : risk_ok,
        'detail': {
            'risk_per_trade'  : CFG['risk']['risk_per_trade'],
            'max_daily_loss'  : CFG['risk']['max_daily_loss_pct'],
            'max_open_trades' : CFG['risk']['max_open_trades'],
        },
        'note'  : "risk≤2% | daily_loss≤10% | max_trades≤5",
    }

    # ── ตรวจ 7: .env มีค่าครบ ─────────────────────────────────
    import os
    from dotenv import load_dotenv
    load_dotenv()

    env_ok = all([
        os.getenv('MT5_LOGIN'),
        os.getenv('MT5_PASSWORD'),
        os.getenv('MT5_SERVER'),
    ])
    results['env_config'] = {
        'pass'  : env_ok,
        'detail': {
            'MT5_LOGIN'   : bool(os.getenv('MT5_LOGIN')),
            'MT5_PASSWORD': bool(os.getenv('MT5_PASSWORD')),
            'MT5_SERVER'  : bool(os.getenv('MT5_SERVER')),
        },
        'note'  : ".env ต้องมีครบ",
    }

    # ── สรุปผล Checklist ──────────────────────────────────────
    _print_checklist(results)
    _save_checklist(results, symbol)

    all_pass = all(v['pass'] for v in results.values())
    if all_pass:
        log.info(f"\n🚀 {symbol} ผ่านทุกข้อ — พร้อม DEPLOY!")
    else:
        failed = [k for k, v in results.items() if not v['pass']]
        log.warning(
            f"\n⚠️ ยังไม่พร้อม deploy\n"
            f"   ข้อที่ไม่ผ่าน: {failed}"
        )

    return results


def _print_checklist(results: dict):
    """แสดงผล checklist แบบ readable"""
    log.info("\n── Deploy Checklist Results ──")
    for check_name, result in results.items():
        icon    = "✅" if result['pass'] else "❌"
        detail  = json.dumps(result['detail'])[:60]
        log.info(
            f"  {icon} {check_name:<25} | "
            f"{detail}"
        )


# ══════════════════════════════════════════════════════════════
# Helper Functions
# ══════════════════════════════════════════════════════════════
def _plot_optuna(study: optuna.Study, symbol: str):
    """บันทึก Optuna plots"""
    try:
        import matplotlib
        matplotlib.use('Agg')

        fig = optuna.visualization.matplotlib.plot_optimization_history(
            study
        )
        fig.figure.savefig(
            REPORTS_DIR / f"optuna_history_{symbol}.png",
            dpi=120, bbox_inches='tight',
        )
        import matplotlib.pyplot as plt
        plt.close('all')

    except Exception as e:
        log.warning(f"ไม่สามารถ plot Optuna: {e}")


def _save_best_params(
    params: dict, score: float, symbol: str
):
    """บันทึก best params ลงไฟล์"""
    path = REPORTS_DIR / f"best_params_{symbol}.json"
    path.write_text(
        json.dumps({
            'symbol'    : symbol,
            'params'    : params,
            'score'     : score,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }, indent=2),
        encoding="utf-8",
    )
    log.info(f"💾 บันทึก best params → {path}")


def _save_checklist(results: dict, symbol: str):
    """บันทึก checklist report"""
    out = REPORTS_DIR / f"deploy_checklist_{symbol}.json"
    serializable = {}
    for k, v in results.items():
        serializable[k] = {
            'pass'  : v['pass'],
            'note'  : v['note'],
            'detail': str(v['detail']),
        }
    serializable['generated_at'] = datetime.now(
        timezone.utc
    ).isoformat()
    out.write_text(
        json.dumps(serializable, indent=2),
        encoding="utf-8",
    )


# ══════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols",   nargs="+",
                        default=CFG['symbols']['active'])
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--method",    default="ensemble",
                        choices=["ensemble","xgb","lgbm","rule"])
    parser.add_argument("--optimize",  action="store_true",
                        help="รัน Optuna optimization")
    parser.add_argument("--trials",    type=int, default=200)
    parser.add_argument("--checklist", action="store_true",
                        help="รัน deploy checklist")
    args = parser.parse_args()

    for sym in args.symbols:
        df = pd.read_parquet(
            PROCESSED_DIR /
            f"{sym}_{args.timeframe}_features.parquet"
        ).dropna(subset=['label'])

        if args.checklist:
            run_deploy_checklist(sym, args.timeframe, args.method)

        elif args.optimize:
            def _entries_fn(df, min_conf=0.62):
                return generate_signals(
                    df, sym, method=args.method,
                    min_conf=min_conf,
                )

            optimize_parameters(
                df, _entries_fn, _entries_fn,
                symbol    = sym,
                timeframe = args.timeframe,
                n_trials  = args.trials,
            )

        else:
            entries, exits = generate_signals(
                df, sym, method=args.method
            )
            metrics, pf = run_backtest(
                df, entries, exits,
                symbol=sym, timeframe=args.timeframe,
            )
=======
# models/backtest.py
"""
Backtest Engine
- vectorbt: backtest เร็วมาก รองรับ vectorized operations
- Walk-Forward: ทดสอบแบบ out-of-sample หลาย fold
- Optuna: หา SL/TP และ parameter ที่ดีที่สุด
- Deploy Checklist: ตรวจทุกข้อก่อน live
"""

import logging
import logging.config
import yaml
import json
import time
import warnings
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field

import vectorbt as vbt
import optuna
from sklearn.model_selection import TimeSeriesSplit

warnings.filterwarnings('ignore')
optuna.logging.set_verbosity(optuna.logging.WARNING)

with open("logging.yaml", encoding="utf-8") as f:
    logging.config.dictConfig(yaml.safe_load(f))
with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

log = logging.getLogger("models")

PROCESSED_DIR = Path(CFG['paths']['data_processed'])
MODELS_DIR    = Path(CFG['paths']['models_saved'])
REPORTS_DIR   = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)

# Timeframe → pandas freq string
TF_FREQ = {
    "M1": "1T", "M5": "5T", "M15": "15T",
    "H1": "1H", "H4": "4H", "D1":  "1D",
}


# ══════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════
@dataclass
class BacktestMetrics:
    """metrics ครบจาก 1 backtest run"""
    symbol:           str
    timeframe:        str
    period_start:     str
    period_end:       str
    total_return_pct: float
    sharpe_ratio:     float
    sortino_ratio:    float
    max_drawdown_pct: float
    win_rate_pct:     float
    profit_factor:    float
    total_trades:     int
    avg_trade_pct:    float
    best_trade_pct:   float
    worst_trade_pct:  float
    avg_duration_hrs: float
    sl_pct:           float = 0.0
    tp_pct:           float = 0.0

    def is_acceptable(self) -> bool:
        """ผ่านเกณฑ์ขั้นต่ำทุกข้อ"""
        return (
            self.sharpe_ratio      >= 1.0  and
            self.max_drawdown_pct  <= 20.0 and
            self.win_rate_pct      >= 45.0 and
            self.profit_factor     >= 1.3  and
            self.total_trades      >= 50   and
            self.total_return_pct  >  0.0
        )

    def grade(self) -> str:
        """ให้เกรดโดยรวม"""
        score = sum([
            self.sharpe_ratio      >= 1.5,
            self.sharpe_ratio      >= 2.0,
            self.max_drawdown_pct  <= 15.0,
            self.max_drawdown_pct  <= 10.0,
            self.win_rate_pct      >= 50.0,
            self.win_rate_pct      >= 55.0,
            self.profit_factor     >= 1.5,
            self.profit_factor     >= 2.0,
            self.total_return_pct  >= 20.0,
            self.total_return_pct  >= 50.0,
        ])
        if score >= 8: return "A+"
        if score >= 6: return "A"
        if score >= 4: return "B"
        if score >= 2: return "C"
        return "F"

    def summary(self) -> str:
        grade = self.grade()
        ok    = "✅" if self.is_acceptable() else "❌"
        return (
            f"{ok} [{grade}] {self.symbol} | "
            f"ret={self.total_return_pct:+.1f}% | "
            f"sharpe={self.sharpe_ratio:.2f} | "
            f"dd={self.max_drawdown_pct:.1f}% | "
            f"wr={self.win_rate_pct:.1f}% | "
            f"pf={self.profit_factor:.2f} | "
            f"trades={self.total_trades}"
        )


@dataclass
class WalkForwardResult:
    """ผล walk-forward ทุก fold"""
    symbol:    str
    timeframe: str
    folds:     list = field(default_factory=list)
    params:    dict = field(default_factory=dict)

    @property
    def mean_sharpe(self) -> float:
        sharpes = [f.sharpe_ratio for f in self.folds
                   if f.sharpe_ratio != float('nan')]
        return np.mean(sharpes) if sharpes else 0.0

    @property
    def mean_return(self) -> float:
        return np.mean([f.total_return_pct for f in self.folds])

    @property
    def consistency(self) -> float:
        """% ของ fold ที่กำไร — ยิ่งสูงยิ่งดี"""
        profitable = sum(1 for f in self.folds
                         if f.total_return_pct > 0)
        return profitable / max(len(self.folds), 1)

    def is_robust(self) -> bool:
        """โมเดล robust หรือเปล่า"""
        return (
            self.mean_sharpe  >= 0.8 and
            self.consistency  >= 0.6 and    # กำไร ≥ 60% ของ fold
            self.mean_return  >  0.0
        )

    def summary(self) -> str:
        ok = "✅" if self.is_robust() else "⚠️"
        return (
            f"{ok} Walk-Forward {self.symbol}: "
            f"sharpe={self.mean_sharpe:.2f} | "
            f"ret={self.mean_return:+.1f}% | "
            f"consistency={self.consistency:.0%} "
            f"({sum(1 for f in self.folds if f.total_return_pct>0)}"
            f"/{len(self.folds)} folds profitable)"
        )


# ══════════════════════════════════════════════════════════════
# Signal Generator
# ══════════════════════════════════════════════════════════════
def generate_signals(
    df:        pd.DataFrame,
    symbol:    str,
    method:    str = "ensemble",  # "ensemble" | "xgb" | "lgbm" | "rule"
    min_conf:  float = 0.62,
) -> tuple[pd.Series, pd.Series]:
    """
    Generate entry/exit signals จากโมเดล
    คืน (entries, exits) เป็น boolean Series

    vectorbt ต้องการ:
    entries: True = เปิด BUY ที่ bar นี้
    exits:   True = ปิด position ที่ bar นี้
    """
    signals_dir = pd.Series(0, index=df.index)

    if method == "ensemble":
        from models.ensemble import create_ensemble
        ensemble = create_ensemble(symbol)
        for i in range(100, len(df)):     # warmup 100 bars
            signal = ensemble.predict(
                df.iloc[:i+1], method="soft"
            )
            signals_dir.iloc[i] = signal.direction

    elif method == "xgb":
        from models.train_xgb import load_model, predict
        payload = load_model(symbol)
        for i in range(50, len(df)):
            r = predict(payload, df.iloc[:i+1], min_conf=min_conf)
            signals_dir.iloc[i] = r['direction']

    elif method == "lgbm":
        from models.train_lgbm import load_model, predict
        payload = load_model(symbol)
        for i in range(50, len(df)):
            r = predict(payload, df.iloc[:i+1], min_conf=min_conf)
            signals_dir.iloc[i] = r['direction']

    elif method == "rule":
        from models.rule_based import RuleBasedStrategy
        strategy = RuleBasedStrategy()
        for i, (_, row) in enumerate(df.iterrows()):
            sig = strategy.generate_signal(row)
            signals_dir.iloc[i] = sig.direction

    entries = signals_dir ==  1   # BUY signal
    exits   = signals_dir == -1   # SELL/close signal

    n_buy  = entries.sum()
    n_sell = exits.sum()
    log.info(f"Signals: BUY={n_buy} SELL={n_sell}")

    return entries, exits


# ══════════════════════════════════════════════════════════════
# Core Backtest
# ══════════════════════════════════════════════════════════════
def run_backtest(
    df:           pd.DataFrame,
    entries:      pd.Series,
    exits:        pd.Series,
    symbol:       str,
    timeframe:    str    = "M15",
    sl_pct:       float  = 0.005,
    tp_pct:       float  = 0.010,
    init_cash:    float  = 10_000.0,
    commission:   float  = 0.0001,
    slippage:     float  = 0.0001,
    size_pct:     float  = 0.02,        # 2% ของ balance ต่อ trade
) -> BacktestMetrics:
    """
    รัน backtest ด้วย vectorbt

    vectorbt ทำงานแบบ vectorized — เร็วมาก
    10,000 bars เสร็จใน < 1 วินาที
    """
    freq = TF_FREQ.get(timeframe, "15T")
    price= df['close']

    if entries.sum() == 0:
        log.warning(f"ไม่มี entry signal เลย!")
        return _empty_metrics(symbol, timeframe, df)

    # ── Run vectorbt Portfolio ─────────────────────────────────
    pf = vbt.Portfolio.from_signals(
        close       = price,
        entries     = entries,
        exits       = exits,
        sl_stop     = sl_pct,        # Stop Loss %
        tp_stop     = tp_pct,        # Take Profit %
        init_cash   = init_cash,
        fees        = commission,
        slippage    = slippage,
        size        = size_pct,
        size_type   = 'percent',
        freq        = freq,
    )

    # ── Extract Stats ──────────────────────────────────────────
    stats  = pf.stats()
    trades = pf.trades.records_readable

    if len(trades) == 0:
        return _empty_metrics(symbol, timeframe, df)

    # duration เป็นชั่วโมง
    if 'Duration' in trades.columns:
        dur_hrs = trades['Duration'].dt.total_seconds().mean() / 3600
    else:
        dur_hrs = 0.0

    def safe(key, default=0.0):
        v = stats.get(key, default)
        return float(v) if not pd.isna(v) else default

    metrics = BacktestMetrics(
        symbol            = symbol,
        timeframe         = timeframe,
        period_start      = str(df.index[0])[:10],
        period_end        = str(df.index[-1])[:10],
        total_return_pct  = safe('Total Return [%]'),
        sharpe_ratio      = safe('Sharpe Ratio'),
        sortino_ratio     = safe('Sortino Ratio'),
        max_drawdown_pct  = abs(safe('Max Drawdown [%]')),
        win_rate_pct      = safe('Win Rate [%]'),
        profit_factor     = safe('Profit Factor', 1.0),
        total_trades      = int(safe('Total Trades')),
        avg_trade_pct     = safe('Avg Winning Trade [%]'),
        best_trade_pct    = safe('Max Winning Trade [%]'),
        worst_trade_pct   = safe('Max Losing Trade [%]'),
        avg_duration_hrs  = round(dur_hrs, 2),
        sl_pct            = sl_pct,
        tp_pct            = tp_pct,
    )

    log.info(metrics.summary())
    return metrics, pf


def _empty_metrics(
    symbol: str, timeframe: str, df: pd.DataFrame
) -> BacktestMetrics:
    return BacktestMetrics(
        symbol=symbol, timeframe=timeframe,
        period_start=str(df.index[0])[:10],
        period_end=str(df.index[-1])[:10],
        total_return_pct=0, sharpe_ratio=0,
        sortino_ratio=0, max_drawdown_pct=100,
        win_rate_pct=0, profit_factor=0,
        total_trades=0, avg_trade_pct=0,
        best_trade_pct=0, worst_trade_pct=0,
        avg_duration_hrs=0,
    )


# ══════════════════════════════════════════════════════════════
# Walk-Forward Backtest
# ══════════════════════════════════════════════════════════════
def walk_forward_backtest(
    df:         pd.DataFrame,
    symbol:     str,
    timeframe:  str   = "M15",
    n_splits:   int   = 5,
    method:     str   = "ensemble",
    sl_pct:     float = 0.005,
    tp_pct:     float = 0.010,
) -> WalkForwardResult:
    """
    Walk-Forward Backtest
    แต่ละ fold test บนข้อมูลที่โมเดลไม่เคยเห็น

    |────Train1────|──Test1──|
                  |────Train2────|──Test2──|
                               ...
    """
    tscv   = TimeSeriesSplit(n_splits=n_splits, gap=96)
    result = WalkForwardResult(
        symbol=symbol, timeframe=timeframe,
        params={'sl_pct': sl_pct, 'tp_pct': tp_pct},
    )

    log.info(f"\nWalk-Forward Backtest: {symbol} | {n_splits} folds")
    log.info("=" * 60)

    for fold, (train_idx, test_idx) in enumerate(tscv.split(df)):
        t0 = time.time()

        df_test  = df.iloc[test_idx]
        entries, exits = generate_signals(
            df_test, symbol, method=method
        )

        fold_metrics, _ = run_backtest(
            df_test, entries, exits,
            symbol=symbol, timeframe=timeframe,
            sl_pct=sl_pct, tp_pct=tp_pct,
        )
        result.folds.append(fold_metrics)
        elapsed = time.time() - t0

        log.info(
            f"Fold {fold+1}/{n_splits} "
            f"({fold_metrics.period_start}→{fold_metrics.period_end}): "
            f"{fold_metrics.summary()} ({elapsed:.1f}s)"
        )

    log.info("=" * 60)
    log.info(result.summary())
    return result


# ══════════════════════════════════════════════════════════════
# Optuna Optimization
# ══════════════════════════════════════════════════════════════
def optimize_parameters(
    df:          pd.DataFrame,
    entries_fn,               # function(sl, tp) → entries
    exits_fn,                 # function(sl, tp) → exits
    symbol:      str,
    timeframe:   str   = "M15",
    n_trials:    int   = 200,
    objective_fn:str   = "sharpe",  # "sharpe" | "calmar" | "sortino"
) -> dict:
    """
    หา SL/TP และ parameter ที่ดีที่สุดด้วย Optuna
    ใช้ 80% ของข้อมูลเป็น optimization set
    20% สุดท้ายเป็น final test (ห้ามใช้ตอน optimize)
    """
    split     = int(len(df) * 0.80)
    df_opt    = df.iloc[:split]

    log.info(
        f"\nOptuna optimization: {symbol} | "
        f"{n_trials} trials | objective={objective_fn}"
    )

    def objective(trial: optuna.Trial) -> float:
        # ── Parameters ────────────────────────────────────────
        sl_pct = trial.suggest_float('sl_pct', 0.002, 0.025)
        tp_pct = trial.suggest_float('tp_pct', 0.003, 0.040)
        conf   = trial.suggest_float('min_conf', 0.55, 0.80)

        # TP ต้องมากกว่า SL อย่างน้อย 1.2x (RR ≥ 1.2)
        if tp_pct < sl_pct * 1.2:
            return -999.0

        # Generate signals
        entries, exits = entries_fn(df_opt, min_conf=conf)

        # ต้องมี trade พอ
        if entries.sum() < 20:
            return -999.0

        # Run backtest
        result = run_backtest(
            df_opt, entries, exits,
            symbol=symbol, timeframe=timeframe,
            sl_pct=sl_pct, tp_pct=tp_pct,
        )

        if isinstance(result, tuple):
            metrics, _ = result
        else:
            metrics = result

        if metrics.total_trades < 20:
            return -999.0

        # ── Objective ─────────────────────────────────────────
        if objective_fn == "sharpe":
            score = metrics.sharpe_ratio
        elif objective_fn == "calmar":
            # Calmar = return / max_drawdown
            dd    = metrics.max_drawdown_pct / 100 + 1e-9
            score = (metrics.total_return_pct / 100) / dd
        elif objective_fn == "sortino":
            score = metrics.sortino_ratio
        else:
            score = metrics.sharpe_ratio

        # Penalize high drawdown
        if metrics.max_drawdown_pct > 20:
            score -= (metrics.max_drawdown_pct - 20) * 0.1

        # Penalize too few trades
        if metrics.total_trades < 50:
            score -= (50 - metrics.total_trades) * 0.01

        return score if not np.isnan(score) else -999.0

    # ── Run Optuna ─────────────────────────────────────────────
    sampler = optuna.samplers.TPESampler(seed=42)
    study   = optuna.create_study(
        direction = 'maximize',
        sampler   = sampler,
    )
    study.optimize(
        objective,
        n_trials        = n_trials,
        show_progress_bar = True,
        n_jobs          = 1,          # เพิ่มถ้ามี CPU หลาย core
    )

    best_params = study.best_params
    best_value  = study.best_value

    log.info(f"\n✅ Optuna เสร็จ:")
    log.info(f"   Best {objective_fn}: {best_value:.4f}")
    log.info(f"   Best params: {best_params}")

    # ── Plot Optimization History ──────────────────────────────
    _plot_optuna(study, symbol)

    # ── Final Test บน 20% สุดท้าย ──────────────────────────────
    df_test = df.iloc[split:]
    log.info(f"\nFinal test ({df_test.index[0]}→{df_test.index[-1]}):")

    entries_test, exits_test = entries_fn(
        df_test, min_conf=best_params['min_conf']
    )
    final_metrics, final_pf = run_backtest(
        df_test, entries_test, exits_test,
        symbol    = symbol,
        timeframe = timeframe,
        sl_pct    = best_params['sl_pct'],
        tp_pct    = best_params['tp_pct'],
    )
    log.info(f"Final: {final_metrics.summary()}")

    # บันทึก best params
    _save_best_params(best_params, best_value, symbol)

    return {
        'best_params'   : best_params,
        'best_value'    : best_value,
        'final_metrics' : final_metrics,
        'study'         : study,
    }


# ══════════════════════════════════════════════════════════════
# Deploy Checklist
# ══════════════════════════════════════════════════════════════
def run_deploy_checklist(
    symbol:    str,
    timeframe: str   = "M15",
    method:    str   = "ensemble",
) -> dict:
    """
    ตรวจสอบทุกข้อก่อน deploy bot จริง
    ต้องผ่านทุกข้อถึงจะ deploy ได้

    คืนค่า dict ของผลแต่ละข้อ
    """
    log.info(f"\n{'='*60}")
    log.info(f"🚀 Deploy Checklist: {symbol}")
    log.info(f"{'='*60}")

    results  = {}
    df       = pd.read_parquet(
        PROCESSED_DIR / f"{symbol}_{timeframe}_features.parquet"
    ).dropna(subset=['label'])

    # ── ตรวจ 1: โมเดลมีอยู่ครบ ────────────────────────────────
    models_exist = {
        'xgb' : (MODELS_DIR / f"xgb_{symbol}.pkl").exists(),
        'lgbm': (MODELS_DIR / f"lgbm_{symbol}.pkl").exists(),
        'lstm': (MODELS_DIR / f"lstm_{symbol}.pth").exists(),
    }
    results['models_exist'] = {
        'pass'  : models_exist['xgb'] and models_exist['lgbm'],
        'detail': models_exist,
        'note'  : "XGB + LGBM ต้องมีอย่างน้อย",
    }

    # ── ตรวจ 2: Training report ────────────────────────────────
    train_report_path = REPORTS_DIR / \
        f"train_report_{symbol}_{timeframe}.json"

    if train_report_path.exists():
        report    = json.loads(train_report_path.read_text())
        train_ok  = (
            report.get('mean_accuracy', 0) >= 0.50 and
            report.get('mean_f1', 0)       >= 0.40
        )
        results['training_metrics'] = {
            'pass'  : train_ok,
            'detail': {
                'accuracy': report.get('mean_accuracy', 0),
                'f1'      : report.get('mean_f1',       0),
            },
            'note'  : "acc≥50% และ f1≥40%",
        }
    else:
        results['training_metrics'] = {
            'pass'  : False,
            'detail': "ไม่พบ training report",
            'note'  : "รัน train ก่อน",
        }

    # ── ตรวจ 3: Backtest Metrics ───────────────────────────────
    entries, exits = generate_signals(df, symbol, method=method)
    bt_result      = run_backtest(
        df, entries, exits,
        symbol=symbol, timeframe=timeframe,
    )

    if isinstance(bt_result, tuple):
        metrics, pf = bt_result
    else:
        metrics = bt_result
        pf      = None

    results['backtest'] = {
        'pass'  : metrics.is_acceptable(),
        'grade' : metrics.grade(),
        'detail': {
            'return'      : metrics.total_return_pct,
            'sharpe'      : metrics.sharpe_ratio,
            'max_dd'      : metrics.max_drawdown_pct,
            'win_rate'    : metrics.win_rate_pct,
            'pf'          : metrics.profit_factor,
            'trades'      : metrics.total_trades,
        },
        'note'  : (
            "sharpe≥1 | dd≤20% | wr≥45% | "
            "pf≥1.3 | trades≥50"
        ),
    }

    # ── ตรวจ 4: Walk-Forward ───────────────────────────────────
    wf = walk_forward_backtest(
        df, symbol, timeframe,
        n_splits=5, method=method,
    )
    results['walk_forward'] = {
        'pass'  : wf.is_robust(),
        'detail': {
            'mean_sharpe' : round(wf.mean_sharpe, 3),
            'consistency' : f"{wf.consistency:.0%}",
            'mean_return' : round(wf.mean_return, 2),
        },
        'note'  : "sharpe≥0.8 | consistency≥60%",
    }

    # ── ตรวจ 5: Overfitting Check ──────────────────────────────
    split      = int(len(df) * 0.70)
    df_train   = df.iloc[:split]
    df_test    = df.iloc[split:]

    en_tr, ex_tr = generate_signals(df_train, symbol, method)
    en_te, ex_te = generate_signals(df_test,  symbol, method)

    bt_tr = run_backtest(df_train, en_tr, ex_tr,
                         symbol=symbol, timeframe=timeframe)
    bt_te = run_backtest(df_test,  en_te, ex_te,
                         symbol=symbol, timeframe=timeframe)

    if isinstance(bt_tr, tuple): bt_tr = bt_tr[0]
    if isinstance(bt_te, tuple): bt_te = bt_te[0]

    ret_diff    = bt_tr.total_return_pct - bt_te.total_return_pct
    not_overfit = ret_diff < 30.0   # train ไม่ควรดีกว่า test เกิน 30%

    results['overfit_check'] = {
        'pass'  : not_overfit,
        'detail': {
            'train_return': bt_tr.total_return_pct,
            'test_return' : bt_te.total_return_pct,
            'gap'         : ret_diff,
        },
        'note'  : "gap < 30% (train vs test)",
    }

    # ── ตรวจ 6: Risk Parameters ────────────────────────────────
    risk_ok = (
        CFG['risk']['risk_per_trade']   <= 0.02 and
        CFG['risk']['max_daily_loss_pct']<= 0.10 and
        CFG['risk']['max_open_trades']   <= 5
    )
    results['risk_params'] = {
        'pass'  : risk_ok,
        'detail': {
            'risk_per_trade'  : CFG['risk']['risk_per_trade'],
            'max_daily_loss'  : CFG['risk']['max_daily_loss_pct'],
            'max_open_trades' : CFG['risk']['max_open_trades'],
        },
        'note'  : "risk≤2% | daily_loss≤10% | max_trades≤5",
    }

    # ── ตรวจ 7: .env มีค่าครบ ─────────────────────────────────
    import os
    from dotenv import load_dotenv
    load_dotenv()

    env_ok = all([
        os.getenv('MT5_LOGIN'),
        os.getenv('MT5_PASSWORD'),
        os.getenv('MT5_SERVER'),
    ])
    results['env_config'] = {
        'pass'  : env_ok,
        'detail': {
            'MT5_LOGIN'   : bool(os.getenv('MT5_LOGIN')),
            'MT5_PASSWORD': bool(os.getenv('MT5_PASSWORD')),
            'MT5_SERVER'  : bool(os.getenv('MT5_SERVER')),
        },
        'note'  : ".env ต้องมีครบ",
    }

    # ── สรุปผล Checklist ──────────────────────────────────────
    _print_checklist(results)
    _save_checklist(results, symbol)

    all_pass = all(v['pass'] for v in results.values())
    if all_pass:
        log.info(f"\n🚀 {symbol} ผ่านทุกข้อ — พร้อม DEPLOY!")
    else:
        failed = [k for k, v in results.items() if not v['pass']]
        log.warning(
            f"\n⚠️ ยังไม่พร้อม deploy\n"
            f"   ข้อที่ไม่ผ่าน: {failed}"
        )

    return results


def _print_checklist(results: dict):
    """แสดงผล checklist แบบ readable"""
    log.info("\n── Deploy Checklist Results ──")
    for check_name, result in results.items():
        icon    = "✅" if result['pass'] else "❌"
        detail  = json.dumps(result['detail'])[:60]
        log.info(
            f"  {icon} {check_name:<25} | "
            f"{detail}"
        )


# ══════════════════════════════════════════════════════════════
# Helper Functions
# ══════════════════════════════════════════════════════════════
def _plot_optuna(study: optuna.Study, symbol: str):
    """บันทึก Optuna plots"""
    try:
        import matplotlib
        matplotlib.use('Agg')

        fig = optuna.visualization.matplotlib.plot_optimization_history(
            study
        )
        fig.figure.savefig(
            REPORTS_DIR / f"optuna_history_{symbol}.png",
            dpi=120, bbox_inches='tight',
        )
        import matplotlib.pyplot as plt
        plt.close('all')

    except Exception as e:
        log.warning(f"ไม่สามารถ plot Optuna: {e}")


def _save_best_params(
    params: dict, score: float, symbol: str
):
    """บันทึก best params ลงไฟล์"""
    path = REPORTS_DIR / f"best_params_{symbol}.json"
    path.write_text(
        json.dumps({
            'symbol'    : symbol,
            'params'    : params,
            'score'     : score,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }, indent=2),
        encoding="utf-8",
    )
    log.info(f"💾 บันทึก best params → {path}")


def _save_checklist(results: dict, symbol: str):
    """บันทึก checklist report"""
    out = REPORTS_DIR / f"deploy_checklist_{symbol}.json"
    serializable = {}
    for k, v in results.items():
        serializable[k] = {
            'pass'  : v['pass'],
            'note'  : v['note'],
            'detail': str(v['detail']),
        }
    serializable['generated_at'] = datetime.now(
        timezone.utc
    ).isoformat()
    out.write_text(
        json.dumps(serializable, indent=2),
        encoding="utf-8",
    )


# ══════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols",   nargs="+",
                        default=CFG['symbols']['active'])
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--method",    default="ensemble",
                        choices=["ensemble","xgb","lgbm","rule"])
    parser.add_argument("--optimize",  action="store_true",
                        help="รัน Optuna optimization")
    parser.add_argument("--trials",    type=int, default=200)
    parser.add_argument("--checklist", action="store_true",
                        help="รัน deploy checklist")
    args = parser.parse_args()

    for sym in args.symbols:
        df = pd.read_parquet(
            PROCESSED_DIR /
            f"{sym}_{args.timeframe}_features.parquet"
        ).dropna(subset=['label'])

        if args.checklist:
            run_deploy_checklist(sym, args.timeframe, args.method)

        elif args.optimize:
            def _entries_fn(df, min_conf=0.62):
                return generate_signals(
                    df, sym, method=args.method,
                    min_conf=min_conf,
                )

            optimize_parameters(
                df, _entries_fn, _entries_fn,
                symbol    = sym,
                timeframe = args.timeframe,
                n_trials  = args.trials,
            )

        else:
            entries, exits = generate_signals(
                df, sym, method=args.method
            )
            metrics, pf = run_backtest(
                df, entries, exits,
                symbol=sym, timeframe=args.timeframe,
            )
>>>>>>> 98ac82b18ee8d71be376450f278ba77dde0c1c3e
            log.info(metrics.summary())