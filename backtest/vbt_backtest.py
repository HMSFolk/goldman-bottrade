<<<<<<< HEAD
# backtest/vbt_backtest.py
import vectorbt as vbt
import pandas as pd
import numpy as np
import joblib

def run_vbt_backtest(symbol: str = "XAUUSD"):
    # โหลดข้อมูลและ features
    df    = pd.read_parquet(f"data/processed/{symbol}_M15_features.parquet")
    price = df['close']

    # --- วิธีที่ 1: Rule-based Signal ---
    entries_rule = (
        (df['ema_cross_9_20'] == 1) &
        (df['rsi_14'] < 65) &
        (df['adx'] > 20)
    )
    exits_rule = (
        (df['ema_cross_9_20'] == -1) |
        (df['rsi_14'] > 75)
    )

    # --- วิธีที่ 2: ML Signal ---
    data    = joblib.load(f'models/xgb_{symbol}.pkl')
    model   = data['model']
    feats   = data['features']
    signals = model.predict(df[feats])

    entries_ml = pd.Series(signals ==  1, index=df.index)
    exits_ml   = pd.Series(signals == -1, index=df.index)

    # --- รัน Backtest ---
    pf = vbt.Portfolio.from_signals(
        close        = price,
        entries      = entries_ml,
        exits        = exits_ml,
        sl_stop      = 0.005,      # Stop Loss 0.5%
        tp_stop      = 0.010,      # Take Profit 1.0% (RR 1:2)
        init_cash    = 10_000,
        fees         = 0.0001,     # Commission 0.01%
        slippage     = 0.0001,     # Slippage 0.01%
        freq         = '15T',
        size         = 0.02,       # 2% ของ balance ต่อ trade
        size_type    = 'percent',
    )

    # --- ดูผลลัพธ์ ---
    stats = pf.stats()
    print(stats)

=======
# backtest/vbt_backtest.py
import vectorbt as vbt
import pandas as pd
import numpy as np
import joblib

def run_vbt_backtest(symbol: str = "XAUUSD"):
    # โหลดข้อมูลและ features
    df    = pd.read_parquet(f"data/processed/{symbol}_M15_features.parquet")
    price = df['close']

    # --- วิธีที่ 1: Rule-based Signal ---
    entries_rule = (
        (df['ema_cross_9_20'] == 1) &
        (df['rsi_14'] < 65) &
        (df['adx'] > 20)
    )
    exits_rule = (
        (df['ema_cross_9_20'] == -1) |
        (df['rsi_14'] > 75)
    )

    # --- วิธีที่ 2: ML Signal ---
    data    = joblib.load(f'models/xgb_{symbol}.pkl')
    model   = data['model']
    feats   = data['features']
    signals = model.predict(df[feats])

    entries_ml = pd.Series(signals ==  1, index=df.index)
    exits_ml   = pd.Series(signals == -1, index=df.index)

    # --- รัน Backtest ---
    pf = vbt.Portfolio.from_signals(
        close        = price,
        entries      = entries_ml,
        exits        = exits_ml,
        sl_stop      = 0.005,      # Stop Loss 0.5%
        tp_stop      = 0.010,      # Take Profit 1.0% (RR 1:2)
        init_cash    = 10_000,
        fees         = 0.0001,     # Commission 0.01%
        slippage     = 0.0001,     # Slippage 0.01%
        freq         = '15T',
        size         = 0.02,       # 2% ของ balance ต่อ trade
        size_type    = 'percent',
    )

    # --- ดูผลลัพธ์ ---
    stats = pf.stats()
    print(stats)

>>>>>>> 98ac82b18ee8d71be376450f278ba77dde0c1c3e
    return pf, stats