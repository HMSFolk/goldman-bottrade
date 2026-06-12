# scripts/final_checklist.py
import json, os, sqlite3
import MetaTrader5 as mt5
import pandas as pd
from pathlib import Path
from config import get_config # ✅ ใช้ config วงจรหลัก

CFG = get_config()

def check_all() -> bool:
    results = {}
    print("\n" + "="*55)
    print("  FINAL DEPLOY CHECKLIST")
    print("="*55)

    models_ok = True
    for sym in CFG['trading']['symbols']:
        if not (Path(f"models/saved/xgb_{sym}.pkl").exists() and Path(f"models/saved/lgbm_{sym}.pkl").exists()):
            models_ok = False
    results['Models exist (XGB+LGBM)'] = models_ok

    bt_ok = True
    for sym in CFG['trading']['symbols']:
        ck_path = Path(f"reports/deploy_checklist_{sym}.json")
        if ck_path.exists():
            ck = json.loads(ck_path.read_text())
            for k, v in ck.items():
                if k != 'generated_at' and not v.get('pass', False):
                    bt_ok = False
        else:
            bt_ok = False
    results['Backtest deploy checklist'] = bt_ok

    db = Path(CFG['paths']['db'])
    paper_ok = False
    if db.exists():
        conn = sqlite3.connect(db)
        trades = pd.read_sql_query("SELECT profit FROM trades WHERE close_time IS NOT NULL AND close_time >= datetime('now','-14 days')", conn)
        conn.close()
        if len(trades) >= 50:
            profits = pd.to_numeric(trades['profit'], errors='coerce')
            paper_ok = (profits > 0).sum() / len(trades) >= 0.45 and profits.sum() > 0
    results['Paper trading ≥ 2 weeks'] = paper_ok

    try:
        mt5.initialize(path=CFG['mt5'].get('terminal_path'))
        mt5_ok = mt5.login(CFG['mt5']['login'], password=CFG['mt5']['password'], server=CFG['mt5']['server'])
        mt5.shutdown()
    except Exception:
        mt5_ok = False
    results['MT5 connection'] = mt5_ok

    r = CFG['risk']
    results['Risk config safe (≤2%/trade)'] = r['risk_per_trade'] <= 0.02 and r['max_daily_loss_pct'] <= 0.10

    results['Database initialized'] = db.exists()
    results['Logs folder exists'] = Path("logs").exists()

    all_pass = True
    for name, passed in results.items():
        print(f"  {'✅' if passed else '❌'} {name}")
        if not passed: all_pass = False

    print("="*55)
    print("  🚀 READY FOR LIVE!" if all_pass else "  ⚠️ FAILED! แก้ปัญหาก่อน deploy")
    return all_pass

if __name__ == "__main__":
    check_all()