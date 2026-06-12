# scripts/analyze_logs.py
import pandas as pd
import json

def load_trade_logs(path: str = "logs/trades.log") -> pd.DataFrame:
    """โหลด JSON trade log เป็น DataFrame วิเคราะห์ได้ทันที"""
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                records.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue

    df = pd.DataFrame(records)
    df['ts'] = pd.to_datetime(df['ts'])
    return df

if __name__ == "__main__":
    df = load_trade_logs()

    # กรองเฉพาะ trade ที่มี pnl
    trades = df[df['pnl'].notna()].copy()

    print(f"Total trades: {len(trades)}")
    print(f"Win rate:     {(trades['pnl'] > 0).mean()*100:.1f}%")
    print(f"Total PnL:    ${trades['pnl'].sum():.2f}")
    print(f"\nBy symbol:")
    print(trades.groupby('symbol')['pnl'].agg(['sum','count','mean']).round(2))