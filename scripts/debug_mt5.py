# scripts/debug_mt5.py
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timezone
from config import get_config # ✅

CFG = get_config()

def test_login():
    print(f"\n{'─'*50}\n  2. Exness Login\n{'─'*50}")
    ok = mt5.login(CFG['mt5']['login'], password=CFG['mt5']['password'], server=CFG['mt5']['server'])
    if not ok:
        print(f"  ❌ Login failed: {mt5.last_error()}")
        return False
    acc = mt5.account_info()
    print(f"  ✅ Login success | {acc.name} | Balance: ${acc.balance:,.2f} | Type: {'Demo' if acc.trade_mode == 0 else 'REAL'}")
    return True

def run_all_tests():
    print("\n" + "="*50 + "\n  MT5 DEBUG \n" + "="*50)
    mt5.shutdown()
    mt5.initialize(path=CFG['mt5'].get('terminal_path'))
    test_login()
    mt5.shutdown()

if __name__ == "__main__":
    run_all_tests()