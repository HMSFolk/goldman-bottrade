# backtest/walk_forward.py
import pandas as pd
import numpy as np
import vectorbt as vbt # ✅ ใส่เพิ่ม: ของเดิมลืม import ตัวคำนวณพอร์ตมา ระบบจะแครช
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier
import joblib

def walk_forward_test(symbol: str = "XAUUSDm", n_splits: int = 5, train_ratio: float = 0.7):
    """
    Walk-forward: เทรนบนอดีต ทดสอบบนอนาคต
    ทำซ้ำหลายรอบ เหมือนการใช้งานจริง
    """
    df = pd.read_parquet(f"data/processed/{symbol}_M15_features.parquet")

    # ✅ FIX BUG-1: path ผิด — ไฟล์อยู่ใน models/saved/ ไม่ใช่ models/
    model_path = f'models/saved/xgb_{symbol}.pkl'
    model_data = joblib.load(model_path)
    # handle ทั้งกรณี save เป็น dict {'model':..,'features':..} หรือ save model ตรงๆ
    if isinstance(model_data, dict):
        FEATS = model_data['features']
    else:
        # ถ้า save model ตรง — ใช้ทุก feature ที่ไม่ใช่ price/label
        SKIP  = {'open','high','low','close','label',
                 'tick_volume','spread','real_volume'}
        FEATS = [c for c in df.columns if c not in SKIP]

    # ✅ FIX BUG-2: label หลัง LabelEncoder ไม่ใช่ -1,0,1 อีกต่อไป!
    # XGBoost multiclass ต้องการ 0-indexed → LabelEncoder(sort([-1,0,1])):
    #   SELL(-1)→0  HOLD(0)→1  BUY(1)→2
    label_vals = sorted(df['label'].unique())
    BUY_LABEL  = label_vals[-1]   # ค่าสูงสุด = BUY (1 หรือ 2 แล้วแต่ encoding)
    SELL_LABEL = label_vals[0]    # ค่าต่ำสุด  = SELL (-1 หรือ 0)
    print(f"  Label mapping: BUY={BUY_LABEL} SELL={SELL_LABEL} (unique={label_vals})")

    X, y = df[FEATS], df['label']

    tscv    = TimeSeriesSplit(n_splits=n_splits, gap=96)
    results = []

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_tr, X_te = X.iloc[train_idx], X.iloc[test_idx]
        y_tr, y_te = y.iloc[train_idx], y.iloc[test_idx]

        model = XGBClassifier(n_estimators=300, learning_rate=0.05,
                               max_depth=6, min_child_weight=10)
        model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)],
                  early_stopping_rounds=30, verbose=False)

        preds = model.predict(X_te)

        price_test = df['close'].iloc[test_idx]
        # ✅ FIX BUG-2: ใช้ BUY_LABEL/SELL_LABEL แทน hardcode 1/-1
        entries    = pd.Series(preds == BUY_LABEL,  index=price_test.index)
        exits      = pd.Series(preds == SELL_LABEL, index=price_test.index)

        pf = vbt.Portfolio.from_signals(
            close     = price_test,
            entries   = entries,
            exits     = exits,
            sl_stop   = 0.005,
            tp_stop   = 0.010,
            init_cash = 10_000,
            fees      = 0.0001,
            freq      = '15min', # ✅ แก้ไขจาก '15T' เป็น '15min'
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
    
    return results_df