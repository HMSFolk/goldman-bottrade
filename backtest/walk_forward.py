<<<<<<< HEAD
# backtest/walk_forward.py
import pandas as pd
import numpy as np
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier
import joblib

def walk_forward_test(symbol: str = "XAUUSD",
                      n_splits: int = 5,
                      train_ratio: float = 0.7):
    """
    Walk-forward: เทรนบนอดีต ทดสอบบนอนาคต
    ทำซ้ำหลายรอบ เหมือนการใช้งานจริง

    |--Train1--|--Test1--|
              |--Train2--|--Test2--|
                        |--Train3--|--Test3--|
    """
    df      = pd.read_parquet(f"data/processed/{symbol}_M15_features.parquet")
    FEATS   = joblib.load(f'models/xgb_{symbol}.pkl')['features']
    X, y    = df[FEATS], df['label']

    tscv    = TimeSeriesSplit(n_splits=n_splits, gap=96)  # gap 1 วัน ป้องกัน leak
    results = []

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_tr, X_te = X.iloc[train_idx], X.iloc[test_idx]
        y_tr, y_te = y.iloc[train_idx], y.iloc[test_idx]

        # เทรนโมเดลใหม่ทุก fold
        model = XGBClassifier(n_estimators=300, learning_rate=0.05,
                               max_depth=6, min_child_weight=10)
        model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)],
                  early_stopping_rounds=30, verbose=False)

        preds = model.predict(X_te)
        proba = model.predict_proba(X_te)

        # คำนวณ metrics ด้วย vectorbt
        price_test = df['close'].iloc[test_idx]
        entries    = pd.Series(preds ==  1, index=price_test.index)
        exits      = pd.Series(preds == -1, index=price_test.index)

        pf = vbt.Portfolio.from_signals(
            close     = price_test,
            entries   = entries,
            exits     = exits,
            sl_stop   = 0.005,
            tp_stop   = 0.010,
            init_cash = 10_000,
            fees      = 0.0001,
            freq      = '15T',
        )
        stats = pf.stats()

        fold_result = {
            'fold'         : fold + 1,
            'train_size'   : len(train_idx),
            'test_size'    : len(test_idx),
            'test_start'   : df.index[test_idx[0]],
            'test_end'     : df.index[test_idx[-1]],
            'return_pct'   : stats['Total Return [%]'],
            'sharpe'       : stats['Sharpe Ratio'],
            'max_dd'       : stats['Max Drawdown [%]'],
            'win_rate'     : stats['Win Rate [%]'],
            'profit_factor': stats['Profit Factor'],
            'total_trades' : stats['Total Trades'],
        }
        results.append(fold_result)

        print(f"Fold {fold+1}: Return={fold_result['return_pct']:.2f}% "
              f"Sharpe={fold_result['sharpe']:.3f} "
              f"WinRate={fold_result['win_rate']:.1f}%")

    results_df = pd.DataFrame(results)
    print("\n=== Walk-Forward Summary ===")
    print(results_df.describe().round(3))
    results_df.to_csv("reports/walk_forward_results.csv", index=False)
=======
# backtest/walk_forward.py
import pandas as pd
import numpy as np
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier
import joblib

def walk_forward_test(symbol: str = "XAUUSD",
                      n_splits: int = 5,
                      train_ratio: float = 0.7):
    """
    Walk-forward: เทรนบนอดีต ทดสอบบนอนาคต
    ทำซ้ำหลายรอบ เหมือนการใช้งานจริง

    |--Train1--|--Test1--|
              |--Train2--|--Test2--|
                        |--Train3--|--Test3--|
    """
    df      = pd.read_parquet(f"data/processed/{symbol}_M15_features.parquet")
    FEATS   = joblib.load(f'models/xgb_{symbol}.pkl')['features']
    X, y    = df[FEATS], df['label']

    tscv    = TimeSeriesSplit(n_splits=n_splits, gap=96)  # gap 1 วัน ป้องกัน leak
    results = []

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_tr, X_te = X.iloc[train_idx], X.iloc[test_idx]
        y_tr, y_te = y.iloc[train_idx], y.iloc[test_idx]

        # เทรนโมเดลใหม่ทุก fold
        model = XGBClassifier(n_estimators=300, learning_rate=0.05,
                               max_depth=6, min_child_weight=10)
        model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)],
                  early_stopping_rounds=30, verbose=False)

        preds = model.predict(X_te)
        proba = model.predict_proba(X_te)

        # คำนวณ metrics ด้วย vectorbt
        price_test = df['close'].iloc[test_idx]
        entries    = pd.Series(preds ==  1, index=price_test.index)
        exits      = pd.Series(preds == -1, index=price_test.index)

        pf = vbt.Portfolio.from_signals(
            close     = price_test,
            entries   = entries,
            exits     = exits,
            sl_stop   = 0.005,
            tp_stop   = 0.010,
            init_cash = 10_000,
            fees      = 0.0001,
            freq      = '15T',
        )
        stats = pf.stats()

        fold_result = {
            'fold'         : fold + 1,
            'train_size'   : len(train_idx),
            'test_size'    : len(test_idx),
            'test_start'   : df.index[test_idx[0]],
            'test_end'     : df.index[test_idx[-1]],
            'return_pct'   : stats['Total Return [%]'],
            'sharpe'       : stats['Sharpe Ratio'],
            'max_dd'       : stats['Max Drawdown [%]'],
            'win_rate'     : stats['Win Rate [%]'],
            'profit_factor': stats['Profit Factor'],
            'total_trades' : stats['Total Trades'],
        }
        results.append(fold_result)

        print(f"Fold {fold+1}: Return={fold_result['return_pct']:.2f}% "
              f"Sharpe={fold_result['sharpe']:.3f} "
              f"WinRate={fold_result['win_rate']:.1f}%")

    results_df = pd.DataFrame(results)
    print("\n=== Walk-Forward Summary ===")
    print(results_df.describe().round(3))
    results_df.to_csv("reports/walk_forward_results.csv", index=False)
>>>>>>> 98ac82b18ee8d71be376450f278ba77dde0c1c3e
    return results_df