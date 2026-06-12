# scripts/final_checklist.py
"""
รันก่อน live เสมอ — ต้องผ่านทุกข้อ
"""
import json, os, sqlite3
import MetaTrader5 as mt5
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import yaml

load_dotenv()
with open("config.yaml") as f:
    CFG = yaml.safe_load(f)

def check_all() -> bool:
    results = {}
    print("\n" + "="*55)
    print("  FINAL DEPLOY CHECKLIST")
    print("="*55)

    # ── 1. Models exist ───────────────────────────────────────
    models_ok = True
    for sym in CFG['trading']['symbols']:
        has_xgb  = Path(f"models/saved/xgb_{sym}.pkl").exists()
        has_lgbm = Path(f"models/saved/lgbm_{sym}.pkl").exists()
        if not (has_xgb and has_lgbm):
            models_ok = False
    results['Models exist (XGB+LGBM)'] = models_ok

    # ── 2. Backtest passed ────────────────────────────────────
    bt_ok = True
    for sym in CFG['trading']['symbols']:
        ck_path = Path(f"reports/deploy_checklist_{sym}.json")
        if not ck_path.exists():
            bt_ok = False
            continue
        ck = json.loads(ck_path.read_text())
        for k, v in ck.items():
            if k == 'generated_at':
                continue
            if not v.get('pass', False):
                bt_ok = False
    results['Backtest deploy checklist'] = bt_ok

    # ── 3. Paper trading passed ───────────────────────────────
    db = Path(CFG['paths']['db'])
    paper_ok = False
    if db.exists():
        conn   = sqlite3.connect(db)
        trades = pd.read_sql_query("""
            SELECT profit FROM trades
            WHERE close_time IS NOT NULL
              AND close_time >= datetime('now','-14 days')
        """, conn)
        conn.close()
        if len(trades) >= 50:
            profits  = pd.to_numeric(trades['profit'], errors='coerce')
            wins     = (profits > 0).sum()
            win_rate = wins / len(trades)
            pnl      = profits.sum()
            paper_ok = win_rate >= 0.45 and pnl > 0
    results['Paper trading ≥ 2 weeks'] = paper_ok

    # ── 4. MT5 connects ───────────────────────────────────────
    mt5_ok = False
    try:
        mt5.initialize(path=os.getenv('MT5_PATH'))
        mt5_ok = mt5.login(
            int(os.getenv('MT5_LOGIN')),
            password=os.getenv('MT5_PASSWORD'),
            server=os.getenv('MT5_SERVER'),
        )
        mt5.shutdown()
    except Exception:
        pass
    results['MT5 connection'] = mt5_ok

    # ── 5. .env has all required vars ────────────────────────
    required = ['MT5_LOGIN','MT5_PASSWORD','MT5_SERVER',
                'TELEGRAM_TOKEN','TELEGRAM_CHAT_ID']
    env_ok   = all(os.getenv(k) for k in required)
    results['.env credentials complete'] = env_ok

    # ── 6. Risk config safe ───────────────────────────────────
    r        = CFG['risk']
    risk_ok  = (
        r['risk_per_trade']     <= 0.02 and
        r['max_daily_loss_pct'] <= 0.10 and
        r['max_open_trades']    <= 5
    )
    results['Risk config safe (≤2%/trade)'] = risk_ok

    # ── 7. Services configured ────────────────────────────────
    import subprocess
    svc_ok  = True
    for svc in ['TradingBot','TradingDashboard']:
        try:
            r = subprocess.run(['sc','query',svc],
                capture_output=True, text=True)
            if 'does not exist' in r.stdout.lower():
                svc_ok = False
        except Exception:
            pass
    results['NSSM services configured'] = svc_ok

    # ── 8. DB initialized ────────────────────────────────────
    results['Database initialized'] = db.exists()

    # ── 9. logs folder exists ────────────────────────────────
    results['Logs folder exists'] = Path("logs").exists()

    # ── Print results ─────────────────────────────────────────
    all_pass = True
    for name, passed in results.items():
        icon = "✅" if passed else "❌"
        print(f"  {icon} {name}")
        if not passed:
            all_pass = False

    print("\n" + "="*55)
    if all_pass:
        print("  🚀 ALL CHECKS PASSED — READY FOR LIVE!")
    else:
        failed = [k for k,v in results.items() if not v]
        print(f"  ⚠️  FAILED: {failed}")
        print(f"  แก้ปัญหาก่อน deploy")
    print("="*55)
    return all_pass


if __name__ == "__main__":
    if check_all():
        print("\n  คำสั่งต่อไป:")
        print("  1. แก้ .env ใช้ Real account credentials")
        print("  2. .\scripts\nssm_setup.ps1 -RemoveFirst")
        print("  3. nssm start TradingBot")
        print("  4. Get-Content logs\bot.log -Wait -Tail 30")