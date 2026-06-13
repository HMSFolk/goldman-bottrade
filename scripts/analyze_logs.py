# scripts/analyze_logs.py
import pandas as pd
import sqlite3
import json
from pathlib import Path
import yaml

with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

DB_PATH = Path(CFG['paths']['db'])


def load_trade_logs_db() -> pd.DataFrame:
    """
    ✅ FIX: อ่านจาก SQLite trades.db แทน logs/trades.log
    สาเหตุ: trades.log อาจไม่มีหรือ format ไม่ใช่ JSON Lines
    Bot บันทึก trade จริงลง SQLite เสมอ
    """
    if not DB_PATH.exists():
        print(f"❌ ไม่พบ {DB_PATH} — bot ยังไม่ได้รันหรือยังไม่มี trade")
        return pd.DataFrame()

    conn   = sqlite3.connect(DB_PATH)
    trades = pd.read_sql_query(
        "SELECT * FROM trades WHERE close_time IS NOT NULL ORDER BY close_time DESC",
        conn
    )
    conn.close()
    return trades


def load_trade_logs_json(path: str = "logs/trades.log") -> pd.DataFrame:
    """fallback: อ่าน JSON Lines log (ถ้ามี)"""
    log_file = Path(path)
    if not log_file.exists():
        print(f"❌ ไม่พบ {path}")
        return pd.DataFrame()

    records = []
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            try:
                records.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    return pd.DataFrame(records)


if __name__ == "__main__":
    # ✅ ลองอ่าน DB ก่อน fallback ไป JSON log
    df = load_trade_logs_db()
    if df.empty:
        print("ลอง fallback → JSON log...")
        df = load_trade_logs_json()
    if df.empty:
        print("ไม่มีข้อมูล trade เลย — รัน paper trading ก่อนแล้วค่อยวิเคราะห์")
        exit()

    df['profit'] = pd.to_numeric(df.get('profit', df.get('pnl', 0)), errors='coerce')
    trades = df[df['profit'].notna()].copy()

    print(f"Total trades : {len(trades)}")
    print(f"Win rate     : {(trades['profit'] > 0).mean()*100:.1f}%")
    print(f"Total PnL    : ${trades['profit'].sum():.2f}")
    print(f"Avg trade    : ${trades['profit'].mean():.2f}")

    if 'symbol' in trades.columns:
        print(f"\nBy symbol:")
        print(trades.groupby('symbol')['profit']
              .agg(['sum','count','mean']).round(2))