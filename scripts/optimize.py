# backtest/optimize.py
import optuna
import vectorbt as vbt
import pandas as pd
import numpy as np
from xgboost import XGBClassifier
from sklearn.preprocessing import LabelEncoder # ✅ เพิ่มเพื่อจัดระเบียบคลาส [0, 1, 2]

def optimize_strategy(symbol: str = "XAUUSDm", n_trials: int = 20): # ปรับเป็น 20 รอบก่อนเพื่อให้รันเช็กได้ไวขึ้นครับ
    df = pd.read_parquet(f"data/processed/{symbol}_M15_features.parquet")

    # ✅ FIX: ล้างค่า NaN ในช่อง label ออกก่อนป้องกันระบบรวน
    df = df.dropna(subset=['label'])

    #แปลงทุกคอลัมน์ที่เป็น 'object' (ข้อความ) ให้เป็น 'category' เพื่อให้ XGBoost รู้จัก
    for col in df.columns:
        if df[col].dtype == 'object':
            df[col] = df[col].astype('category')

    # ✅ FIX: แปลงคลาสจาก [-1.0, 0.0, 1.0] ให้เป็น [0, 1, 2] เพื่อให้ตรงตามกฎของ XGBoost ตัวใหม่
    le = LabelEncoder()
    df['label'] = le.fit_transform(df['label'])

    # คำนวณคลาสหลังจากแปลงเสร็จสิ้นอย่างปลอดภัย
    label_vals = sorted(df['label'].unique())
    BUY_LABEL  = label_vals[-1]   # จะได้เลข 2
    SELL_LABEL = label_vals[0]    # จะได้เลข 0
    print(f"   XGBoost Optimized Mapping → BUY={BUY_LABEL}, SELL={SELL_LABEL} (unique={label_vals})")

    SKIP  = {'open','high','low','close','label',
             'tick_volume','spread','real_volume'}
    FEATS = [c for c in df.columns if c not in SKIP]

    def objective(trial):
        params = {
            'n_estimators'    : trial.suggest_int('n_estimators', 100, 500), # บีบสเกลเล็กน้อยให้ประมวลผลเร็วขึ้น
            'max_depth'       : trial.suggest_int('max_depth', 3, 7),
            'learning_rate'   : trial.suggest_float('lr', 0.01, 0.1, log=True),
            'min_child_weight': trial.suggest_int('mcw', 5, 50),
            'subsample'       : trial.suggest_float('sub', 0.6, 1.0),
        }
        sl = trial.suggest_float('sl', 0.003, 0.02)
        tp = trial.suggest_float('tp', 0.005, 0.03)

        if tp < sl * 1.2:
            return -999

        split     = int(len(df) * 0.7)
        X_tr      = df[FEATS].iloc[:split]
        y_tr      = df['label'].iloc[:split]
        X_te      = df[FEATS].iloc[split:]
        price_te  = df['close'].iloc[split:]

# เพิ่ม enable_categorical=True เพื่อให้รองรับคอลัมน์ข้อความที่เราแปลงเป็น category แล้ว
        model = XGBClassifier(**params, tree_method='hist', enable_categorical=True, verbosity=0)
        
        model.fit(X_tr, y_tr)
        preds = model.predict(X_te)

        entries = pd.Series(preds == BUY_LABEL,  index=price_te.index)
        exits   = pd.Series(preds == SELL_LABEL, index=price_te.index)

        if entries.sum() < 10:
            return -999

        pf = vbt.Portfolio.from_signals(
            close     = price_te,
            entries   = entries,
            exits     = exits,
            sl_stop   = sl,
            tp_stop   = tp,
            init_cash = 10_000,
            fees      = 0.0001,
            freq      = '15min', 
        )
        stats   = pf.stats()
        sharpe  = stats['Sharpe Ratio']
        max_dd  = abs(stats['Max Drawdown [%]'])

        score = sharpe - (max_dd / 100) * 2
        return score if not np.isnan(score) else -999

    study = optuna.create_study(direction='maximize',
                                 sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best = study.best_params
    print(f"\n✅ Best params: {best}")
    print(f"   Best score:  {study.best_value:.4f}")

    # หมายเหตุ: ถ้ารันบน Command Line แนะนำให้เปิดดูสรุปข้อความ หากหน้าต่างกราฟ .show() เด้งแล้วค้าง ให้เอาเครื่องหมาย # ออกเพื่อปิดกราฟได้ครับ
    # optuna.visualization.plot_optimization_history(study).show()
    # optuna.visualization.plot_param_importances(study).show()
    
    return best

# ========================================================
#  ✅ เพิ่มบล็อกสั่งเปิดทำงานฟังก์ชันท้ายไฟล์อย่างเป็นทางการ
# ========================================================
if __name__ == "__main__":
    print("⏳ Starting SL/TP Hyperparameter Tuning with Optuna...")
    try:
        optimize_strategy(symbol="XAUUSDm", n_trials=20)
    except Exception as e:
        print(f"❌ ระบบ Optuna หยุดทำงานเนื่องจาก: {e}")