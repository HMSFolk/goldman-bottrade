# scripts/live_monitor.py
"""
Monitor ตลอดเวลา — รันบน terminal แยก
"""
import time, os, json, sqlite3
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv
import yaml

load_dotenv()
with open("config.yaml") as f:
    CFG = yaml.safe_load(f)


def get_account():
    mt5.initialize(path=os.getenv('MT5_PATH'))
    mt5.login(int(os.getenv('MT5_LOGIN')),
              password=os.getenv('MT5_PASSWORD'),
              server=os.getenv('MT5_SERVER'))
    acc  = mt5.account_info()
    pos  = mt5.positions_get() or []
    mt5.shutdown()
    return acc, pos


def get_today_stats():
    db = CFG['paths']['db']
    if not os.path.exists(db):
        return {}
    conn   = sqlite3.connect(db)
    trades = pd.read_sql_query("""
        SELECT profit FROM trades
        WHERE close_time IS NOT NULL
          AND date(close_time) = date('now')
    """, conn)
    conn.close()

    if trades.empty:
        return {'trades': 0, 'pnl': 0, 'win_rate': 0}

    profits = pd.to_numeric(trades['profit'], errors='coerce')
    return {
        'trades'  : len(trades),
        'pnl'     : profits.sum(),
        'win_rate': (profits > 0).mean() * 100,
    }


def display(acc, positions, stats, cycle):
    os.system('cls')
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    print(f"╔{'═'*53}╗")
    print(f"║  🤖 AURUM BOT — Live Monitor  [{now}]  ║")
    print(f"╠{'═'*53}╣")
    print(f"║  Balance:    ${acc.balance:>10,.2f}                      ║")
    print(f"║  Equity:     ${acc.equity:>10,.2f}                      ║")
    pnl_icon = "📈" if acc.profit >= 0 else "📉"
    print(f"║  Floating:   ${acc.profit:>+10.2f}  {pnl_icon}               ║")
    print(f"║  Free Margin:${acc.margin_free:>10,.2f}                      ║")
    print(f"╠{'═'*53}╣")
    print(f"║  Today:  {stats.get('trades',0):3} trades  "
          f"WR:{stats.get('win_rate',0):5.1f}%  "
          f"P&L:${stats.get('pnl',0):+8.2f}     ║")
    print(f"╠{'═'*53}╣")

    if positions:
        print(f"║  Open Positions ({len(positions)}):                         ║")
        for pos in positions:
            arrow = "↑" if pos.type == 0 else "↓"
            icon  = "💚" if pos.profit >= 0 else "🔴"
            print(f"║  {icon} {arrow} {pos.symbol:<8} "
                  f"lot={pos.volume:.2f}  "
                  f"P&L=${pos.profit:>+8.2f}         ║")
    else:
        print(f"║  No open positions                                ║")

    print(f"╠{'═'*53}╣")

    # Service status
    import subprocess
    for svc in ['TradingBot','TradingDashboard']:
        try:
            r = subprocess.run(['sc','query',svc],
                capture_output=True, text=True)
            running = 'RUNNING' in r.stdout
            icon    = "✅" if running else "❌"
            status  = "RUNNING" if running else "STOPPED"
        except Exception:
            icon, status = "❓", "UNKNOWN"
        print(f"║  {icon} {svc:<22} {status:<12}           ║")

    print(f"╠{'═'*53}╣")
    print(f"║  Dashboard: http://localhost:8501"
          f"                ║")
    print(f"║  Refresh #{cycle:04d} — Ctrl+C to exit"
          f"              ║")
    print(f"╚{'═'*53}╝")


def run(interval=60):
    cycle = 0
    print("Starting live monitor... Ctrl+C to stop")
    time.sleep(2)

    while True:
        try:
            cycle += 1
            acc, pos = get_account()
            stats    = get_today_stats()
            display(acc, pos, stats, cycle)
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nMonitor stopped")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    run(interval=60)