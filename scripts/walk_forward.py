# scripts/walk_forward.py
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import numpy as np
import vectorbt as vbt 
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import LabelEncoder  # ✅ ใส่เพิ่ม: เพื่อแปลงคลาส [-1, 0, 1] เป็น [0, 1, 2]
from xgboost import XGBClassifier
import joblib

from config import get_config   # ✅ NEW: ใช้ canonical symbol จาก config
CFG = get_config()


def walk_forward_test(symbol: str = None, n_splits: int = 5, train_ratio: float = 0.7):
    """
    Walk-forward: เทรนบนอดีต ทดสอบบนอนาคต
    ทำซ้ำหลายรอบ เหมือนการใช้งานจริง

    ✅ FIX: symbol default เป็น None → ใช้ตัวแรกใน config.symbols.active
       (canonical, ไม่มี m) แทนการ hardcode "XAUUSDm"
    """
    symbol = symbol or CFG['symbols']['active'][0]
    df = pd.read_parquet(f"data/processed/{symbol}_M15_features.parquet")

    # ✅ ล้างแถวข้อมูลที่ label เป็น NaN ทิ้ง 
    df = df.dropna(subset=['label'])

    # ✅ path ผิด — ไฟล์อยู่ใน models/saved/ ไม่ใช่ models/
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

    X = df[FEATS]

    # ✅ FIX BUG: แปลงคลาสด้วย LabelEncoder เพื่อป้องกันปัญหาคลาสติดลบสำหรับ XGBoost
    # ค่าเดิม [-1.0, 0.0, 1.0] จะถูกแมปใหม่เป็น [0, 1, 2] เสมอ
    le = LabelEncoder()
    y = pd.Series(le.fit_transform(df['label']), index=df.index)

    # กำหนดตัวแปรซื้อขายจากคลาสใหม่
    label_vals = sorted(y.unique())
    BUY_LABEL  = label_vals[-1]   # ค่าสูงสุดคือเลข 2 (BUY)
    SELL_LABEL = label_vals[0]    # ค่าต่ำสุดคือเลข 0 (SELL)
    print(f"   XGBoost Optimized Mapping: BUY={BUY_LABEL} SELL={SELL_LABEL} (unique={label_vals})")

    tscv    = TimeSeriesSplit(n_splits=n_splits, gap=96)
    results = []

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_tr, X_te = X.iloc[train_idx], X.iloc[test_idx]
        y_tr, y_te = y.iloc[train_idx], y.iloc[test_idx]

        # ✅ ย้าย early_stopping_rounds มาใส่ในตัวแปรต้นทาง
        model = XGBClassifier(n_estimators=300, learning_rate=0.05,
                              max_depth=6, min_child_weight=10,
                              early_stopping_rounds=30)
        
        # ✅ เอา early_stopping_rounds ออกจาก .fit()
        model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)

        preds = model.predict(X_te)

        price_test = df['close'].iloc[test_idx]
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
            freq      = '15min', 
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
    # ✅ FIX: ใส่ symbol ในชื่อไฟล์ — เดิมไฟล์เดียวทับกันถ้ารันหลาย symbol
    results_df.to_csv(f"reports/walk_forward_{symbol}.csv", index=False)
    
    return results_df


def run_all_symbols(symbols: list = None, n_splits: int = 5) -> dict:
    """
    รัน walk-forward ทุก symbol ที่ active ใน config (เรียกจาก docker-compose ได้)
    symbol ไหน fail จะถูกข้าม ไม่ทำให้ตัวอื่นพังตาม
    """
    import os
    os.makedirs("reports", exist_ok=True)
    symbols = symbols or CFG['symbols']['active']
    all_results = {}
    for sym in symbols:
        try:
            all_results[sym] = walk_forward_test(symbol=sym, n_splits=n_splits)
        except Exception as e:
            print(f"❌ Walk-Forward {sym} ล้มเหลว: {e}")
            all_results[sym] = None
    return all_results

if __name__ == "__main__":
    import os
    print("⏳ Starting Walk-Forward Out-Of-Sample Validation...")
    os.makedirs("reports", exist_ok=True)

    # ✅ FIX: รันทุก symbol ใน config แทนการ hardcode XAUUSDm ตัวเดียว
    run_all_symbols()