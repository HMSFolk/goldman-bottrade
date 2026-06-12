# scripts/live_monitor.py
import time, os, sqlite3
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timezone
from config import get_config # ✅ 

CFG = get_config()

def get_account():
    mt5.initialize(path=CFG['mt5'].get('terminal_path'))
    mt5.login(CFG['mt5']['login'], password=CFG['mt5']['password'], server=CFG['mt5']['server'])
    acc = mt5.account_info()
    pos = mt5.positions_get() or []
    mt5.shutdown()
    return acc, pos

def get_today_stats():
    db = CFG['paths']['db']
    if not os.path.exists(db): return {}
    conn = sqlite3.connect(db)
    trades = pd.read_sql_query("SELECT profit FROM trades WHERE close_time IS NOT NULL AND date(close_time) = date('now')", conn)
    conn.close()
    if trades.empty: return {'trades': 0, 'pnl': 0, 'win_rate': 0}
    profits = pd.to_numeric(trades['profit'], errors='coerce')
    return {'trades': len(trades), 'pnl': profits.sum(), 'win_rate': (profits > 0).mean() * 100}

def display(acc, positions, stats, cycle):
    os.system('cls' if os.name == 'nt' else 'clear')
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    print(f"╔{'═'*53}╗\n║  🤖 AURUM BOT — Live Monitor  [{now}]  ║\n╠{'═'*53}╣")
    if acc:
        print(f"║  Balance:    ${acc.balance:>10,.2f}                      ║")
        print(f"║  Floating:   ${acc.profit:>+10.2f}  {'📈' if acc.profit >= 0 else '📉'}               ║")
    print(f"╠{'═'*53}╣\n║  Today:  {stats.get('trades',0):3} trades  WR:{stats.get('win_rate',0):5.1f}%  P&L:${stats.get('pnl',0):+8.2f}     ║\n╠{'═'*53}╣")
    
    if positions:
        for pos in positions:
            print(f"║  {'💚' if pos.profit >= 0 else '🔴'} {'↑' if pos.type == 0 else '↓'} {pos.symbol:<8} lot={pos.volume:.2f} P&L=${pos.profit:>+8.2f}         ║")
    else:
        print(f"║  No open positions                                ║")
    print(f"╚{'═'*53}╝")

def run(interval=60):
    cycle = 0
    print("Starting live monitor... Ctrl+C to stop")
    while True:
        try:
            cycle += 1
            acc, pos = get_account()
            stats = get_today_stats()
            display(acc, pos, stats, cycle)
            time.sleep(interval)
        except KeyboardInterrupt: break

if __name__ == "__main__":
    run(interval=60)