<<<<<<< HEAD
# backtest/optimize.py
import optuna
import vectorbt as vbt
import pandas as pd
import numpy as np
from xgboost import XGBClassifier

def optimize_strategy(symbol: str = "XAUUSD", n_trials: int = 100):
    df = pd.read_parquet(f"data/processed/{symbol}_M15_features.parquet")

    def objective(trial):
        # ช่วงค่าที่จะ search
        params = {
            'n_estimators'    : trial.suggest_int('n_estimators', 100, 800),
            'max_depth'       : trial.suggest_int('max_depth', 3, 8),
            'learning_rate'   : trial.suggest_float('lr', 0.01, 0.1, log=True),
            'min_child_weight': trial.suggest_int('mcw', 5, 50),
            'subsample'       : trial.suggest_float('sub', 0.6, 1.0),
        }
        sl = trial.suggest_float('sl', 0.003, 0.02)
        tp = trial.suggest_float('tp', 0.005, 0.03)

        # ต้องแน่ใจว่า TP > SL (RR ≥ 1:1)
        if tp < sl * 1.2:
            return -999

        FEATS = [c for c in df.columns if c not in
                 ['open','high','low','close','label',
                  'tick_volume','spread','real_volume']]

        split     = int(len(df) * 0.7)
        X_tr      = df[FEATS].iloc[:split]
        y_tr      = df['label'].iloc[:split]
        X_te      = df[FEATS].iloc[split:]
        price_te  = df['close'].iloc[split:]

        model = XGBClassifier(**params, tree_method='hist', verbosity=0)
        model.fit(X_tr, y_tr)
        preds = model.predict(X_te)

        entries = pd.Series(preds ==  1, index=price_te.index)
        exits   = pd.Series(preds == -1, index=price_te.index)

        if entries.sum() < 10:   # ต้องมี trade พอ
            return -999

        pf = vbt.Portfolio.from_signals(
            close     = price_te,
            entries   = entries,
            exits     = exits,
            sl_stop   = sl,
            tp_stop   = tp,
            init_cash = 10_000,
            fees      = 0.0001,
            freq      = '15T',
        )
        stats   = pf.stats()
        sharpe  = stats['Sharpe Ratio']
        max_dd  = abs(stats['Max Drawdown [%]'])

        # Objective: maximize Sharpe และ penalize drawdown
        score = sharpe - (max_dd / 100) * 2
        return score if not np.isnan(score) else -999

    study = optuna.create_study(direction='maximize',
                                 sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best = study.best_params
    print(f"\n✅ Best params: {best}")
    print(f"   Best score:  {study.best_value:.4f}")

    # plot optimization history
    optuna.visualization.plot_optimization_history(study).show()
    optuna.visualization.plot_param_importances(study).show()

=======
# backtest/optimize.py
import optuna
import vectorbt as vbt
import pandas as pd
import numpy as np
from xgboost import XGBClassifier

def optimize_strategy(symbol: str = "XAUUSD", n_trials: int = 100):
    df = pd.read_parquet(f"data/processed/{symbol}_M15_features.parquet")

    def objective(trial):
        # ช่วงค่าที่จะ search
        params = {
            'n_estimators'    : trial.suggest_int('n_estimators', 100, 800),
            'max_depth'       : trial.suggest_int('max_depth', 3, 8),
            'learning_rate'   : trial.suggest_float('lr', 0.01, 0.1, log=True),
            'min_child_weight': trial.suggest_int('mcw', 5, 50),
            'subsample'       : trial.suggest_float('sub', 0.6, 1.0),
        }
        sl = trial.suggest_float('sl', 0.003, 0.02)
        tp = trial.suggest_float('tp', 0.005, 0.03)

        # ต้องแน่ใจว่า TP > SL (RR ≥ 1:1)
        if tp < sl * 1.2:
            return -999

        FEATS = [c for c in df.columns if c not in
                 ['open','high','low','close','label',
                  'tick_volume','spread','real_volume']]

        split     = int(len(df) * 0.7)
        X_tr      = df[FEATS].iloc[:split]
        y_tr      = df['label'].iloc[:split]
        X_te      = df[FEATS].iloc[split:]
        price_te  = df['close'].iloc[split:]

        model = XGBClassifier(**params, tree_method='hist', verbosity=0)
        model.fit(X_tr, y_tr)
        preds = model.predict(X_te)

        entries = pd.Series(preds ==  1, index=price_te.index)
        exits   = pd.Series(preds == -1, index=price_te.index)

        if entries.sum() < 10:   # ต้องมี trade พอ
            return -999

        pf = vbt.Portfolio.from_signals(
            close     = price_te,
            entries   = entries,
            exits     = exits,
            sl_stop   = sl,
            tp_stop   = tp,
            init_cash = 10_000,
            fees      = 0.0001,
            freq      = '15T',
        )
        stats   = pf.stats()
        sharpe  = stats['Sharpe Ratio']
        max_dd  = abs(stats['Max Drawdown [%]'])

        # Objective: maximize Sharpe และ penalize drawdown
        score = sharpe - (max_dd / 100) * 2
        return score if not np.isnan(score) else -999

    study = optuna.create_study(direction='maximize',
                                 sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best = study.best_params
    print(f"\n✅ Best params: {best}")
    print(f"   Best score:  {study.best_value:.4f}")

    # plot optimization history
    optuna.visualization.plot_optimization_history(study).show()
    optuna.visualization.plot_param_importances(study).show()

>>>>>>> 98ac82b18ee8d71be376450f278ba77dde0c1c3e
    return best