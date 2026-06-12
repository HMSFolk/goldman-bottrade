<<<<<<< HEAD
# scripts/paper_trading_monitor.py
"""
Monitor paper trading ทุกวัน
รัน: python scripts/paper_trading_monitor.py
"""
import sqlite3
import pandas as pd
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import yaml

with open("config.yaml") as f:
    CFG = yaml.safe_load(f)

DB_PATH = Path(CFG['paths']['db'])


def daily_report(days: int = 1):
    """สรุปผลรายวัน"""
    if not DB_PATH.exists():
        print("ยังไม่มี trades.db — bot ยังไม่ได้รัน")
        return

    conn = sqlite3.connect(DB_PATH)

    # Trades วันนี้
    trades = pd.read_sql_query(f"""
        SELECT * FROM trades
        WHERE close_time IS NOT NULL
          AND close_time >= datetime('now', '-{days} days')
        ORDER BY close_time DESC
    """, conn)
    conn.close()

    if trades.empty:
        print(f"ไม่มี closed trades ใน {days} วันล่าสุด")
        print("บอทอาจยังไม่เจอสัญญาณที่ผ่าน confidence threshold")
        return

    trades['profit'] = pd.to_numeric(trades['profit'], errors='coerce')
    wins   = trades[trades['profit'] > 0]
    losses = trades[trades['profit'] <= 0]
    gw     = wins['profit'].sum() if not wins.empty else 0
    gl     = abs(losses['profit'].sum()) if not losses.empty else 0

    print(f"\n{'='*55}")
    print(f"  Paper Trading Report — Last {days} day(s)")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*55}")
    print(f"  Total Trades : {len(trades)}")
    print(f"  Wins/Losses  : {len(wins)}W / {len(losses)}L")
    print(f"  Win Rate     : {len(wins)/len(trades)*100:.1f}%")
    print(f"  Total P&L    : ${trades['profit'].sum():+.2f}")
    print(f"  Profit Factor: {gw/gl:.2f}" if gl > 0 else "  Profit Factor: ∞")
    print(f"  Avg Win      : ${wins['profit'].mean():+.2f}" if not wins.empty else "  Avg Win: N/A")
    print(f"  Avg Loss     : ${losses['profit'].mean():+.2f}" if not losses.empty else "  Avg Loss: N/A")
    print(f"{'─'*55}")

    # By symbol
    print(f"  By Symbol:")
    for sym, grp in trades.groupby('symbol'):
        sym_wins = (grp['profit'] > 0).sum()
        print(f"    {sym:<8}: {len(grp)} trades | "
              f"WR={sym_wins/len(grp)*100:.0f}% | "
              f"P&L=${grp['profit'].sum():+.2f}")

    # Recent trades
    print(f"\n  Recent Trades (last 10):")
    cols = ['close_time','symbol','direction','volume',
            'open_price','close_price','profit']
    avail = [c for c in cols if c in trades.columns]
    for _, row in trades.head(10).iterrows():
        icon  = "💚" if row['profit'] > 0 else "🔴"
        arrow = "↑" if row['direction'] == 'BUY' else "↓"
        time_str = str(row.get('close_time',''))[:16]
        print(f"    {icon} {arrow} {row['symbol']} "
              f"${row['profit']:+.2f} | {time_str}")


def weekly_stats():
    """สรุปสถิติ 2 สัปดาห์"""
    if not DB_PATH.exists():
        return

    conn   = sqlite3.connect(DB_PATH)
    trades = pd.read_sql_query("""
        SELECT * FROM trades
        WHERE close_time IS NOT NULL
          AND close_time >= datetime('now', '-14 days')
    """, conn)
    conn.close()

    if trades.empty or len(trades) < 10:
        print(f"\nยังมี trades น้อยเกิน ({len(trades)}) — รอให้ครบ 50+ trades")
        return

    trades['profit']  = pd.to_numeric(trades['profit'], errors='coerce')
    profits           = trades['profit'].dropna()
    wins              = profits[profits > 0]
    losses            = profits[profits <= 0]

    # Sharpe Ratio (simplified)
    if profits.std() > 0:
        sharpe = (profits.mean() / profits.std()) * (252**0.5)
    else:
        sharpe = 0

    # Max Drawdown
    cumsum     = profits.cumsum()
    peak       = cumsum.cummax()
    drawdown   = (peak - cumsum)
    max_dd     = drawdown.max()

    # Profit Factor
    pf = wins.sum() / abs(losses.sum()) if not losses.empty else float('inf')

    print(f"\n{'='*55}")
    print(f"  2-Week Paper Trading Statistics")
    print(f"{'='*55}")
    print(f"  Total Trades   : {len(trades)}")
    print(f"  Win Rate       : {len(wins)/len(trades)*100:.1f}%")
    print(f"  Sharpe Ratio   : {sharpe:.2f}")
    print(f"  Profit Factor  : {pf:.2f}")
    print(f"  Total P&L      : ${profits.sum():+.2f}")
    print(f"  Max Drawdown   : ${max_dd:.2f}")
    print(f"  Avg Trade      : ${profits.mean():+.2f}")
    print(f"{'─'*55}")

    # Pass/Fail criteria
    criteria = {
        "Total trades ≥ 50"      : len(trades) >= 50,
        "Win rate ≥ 45%"         : len(wins)/len(trades) >= 0.45,
        "Sharpe ratio ≥ 0.8"     : sharpe >= 0.8,
        "Profit factor ≥ 1.2"    : pf >= 1.2,
        "Max DD ≤ $500"          : max_dd <= 500,
        "Total P&L > 0"          : profits.sum() > 0,
    }

    print(f"\n  Go-Live Criteria:")
    all_pass = True
    for name, passed in criteria.items():
        icon = "✅" if passed else "❌"
        print(f"    {icon} {name}")
        if not passed:
            all_pass = False

    print(f"\n{'='*55}")
    if all_pass:
        print(f"  🚀 READY FOR LIVE TRADING!")
    else:
        failed = [k for k,v in criteria.items() if not v]
        print(f"  ⚠️  ยังไม่พร้อม: {failed}")
    print(f"{'='*55}")

    return all_pass


def live_positions():
    """ดู open positions ปัจจุบัน"""
    try:
        import MetaTrader5 as mt5
        from dotenv import load_dotenv
        import os
        load_dotenv()

        mt5.initialize(path=os.getenv('MT5_PATH'))
        mt5.login(int(os.getenv('MT5_LOGIN')),
                  password=os.getenv('MT5_PASSWORD'),
                  server=os.getenv('MT5_SERVER'))

        positions = mt5.positions_get() or []
        acc       = mt5.account_info()
        mt5.shutdown()

        print(f"\n{'─'*55}")
        print(f"  Live Account Snapshot")
        print(f"  Balance:  ${acc.balance:,.2f}")
        print(f"  Equity:   ${acc.equity:,.2f}")
        print(f"  Profit:   ${acc.profit:+.2f}")
        print(f"  Open pos: {len(positions)}")

        if positions:
            for pos in positions:
                arrow = "↑" if pos.type == 0 else "↓"
                icon  = "💚" if pos.profit >= 0 else "🔴"
                print(f"  {icon} {arrow} {pos.symbol} "
                      f"lot={pos.volume:.2f} "
                      f"P&L=${pos.profit:+.2f}")
        print(f"{'─'*55}")

    except Exception as e:
        print(f"  ❌ ไม่สามารถดู positions: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily",  type=int, default=1)
    parser.add_argument("--weekly", action="store_true")
    parser.add_argument("--live",   action="store_true")
    args = parser.parse_args()

    live_positions()
    daily_report(days=args.daily)
    if args.weekly:
=======
# scripts/paper_trading_monitor.py
"""
Monitor paper trading ทุกวัน
รัน: python scripts/paper_trading_monitor.py
"""
import sqlite3
import pandas as pd
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import yaml

with open("config.yaml") as f:
    CFG = yaml.safe_load(f)

DB_PATH = Path(CFG['paths']['db'])


def daily_report(days: int = 1):
    """สรุปผลรายวัน"""
    if not DB_PATH.exists():
        print("ยังไม่มี trades.db — bot ยังไม่ได้รัน")
        return

    conn = sqlite3.connect(DB_PATH)

    # Trades วันนี้
    trades = pd.read_sql_query(f"""
        SELECT * FROM trades
        WHERE close_time IS NOT NULL
          AND close_time >= datetime('now', '-{days} days')
        ORDER BY close_time DESC
    """, conn)
    conn.close()

    if trades.empty:
        print(f"ไม่มี closed trades ใน {days} วันล่าสุด")
        print("บอทอาจยังไม่เจอสัญญาณที่ผ่าน confidence threshold")
        return

    trades['profit'] = pd.to_numeric(trades['profit'], errors='coerce')
    wins   = trades[trades['profit'] > 0]
    losses = trades[trades['profit'] <= 0]
    gw     = wins['profit'].sum() if not wins.empty else 0
    gl     = abs(losses['profit'].sum()) if not losses.empty else 0

    print(f"\n{'='*55}")
    print(f"  Paper Trading Report — Last {days} day(s)")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*55}")
    print(f"  Total Trades : {len(trades)}")
    print(f"  Wins/Losses  : {len(wins)}W / {len(losses)}L")
    print(f"  Win Rate     : {len(wins)/len(trades)*100:.1f}%")
    print(f"  Total P&L    : ${trades['profit'].sum():+.2f}")
    print(f"  Profit Factor: {gw/gl:.2f}" if gl > 0 else "  Profit Factor: ∞")
    print(f"  Avg Win      : ${wins['profit'].mean():+.2f}" if not wins.empty else "  Avg Win: N/A")
    print(f"  Avg Loss     : ${losses['profit'].mean():+.2f}" if not losses.empty else "  Avg Loss: N/A")
    print(f"{'─'*55}")

    # By symbol
    print(f"  By Symbol:")
    for sym, grp in trades.groupby('symbol'):
        sym_wins = (grp['profit'] > 0).sum()
        print(f"    {sym:<8}: {len(grp)} trades | "
              f"WR={sym_wins/len(grp)*100:.0f}% | "
              f"P&L=${grp['profit'].sum():+.2f}")

    # Recent trades
    print(f"\n  Recent Trades (last 10):")
    cols = ['close_time','symbol','direction','volume',
            'open_price','close_price','profit']
    avail = [c for c in cols if c in trades.columns]
    for _, row in trades.head(10).iterrows():
        icon  = "💚" if row['profit'] > 0 else "🔴"
        arrow = "↑" if row['direction'] == 'BUY' else "↓"
        time_str = str(row.get('close_time',''))[:16]
        print(f"    {icon} {arrow} {row['symbol']} "
              f"${row['profit']:+.2f} | {time_str}")


def weekly_stats():
    """สรุปสถิติ 2 สัปดาห์"""
    if not DB_PATH.exists():
        return

    conn   = sqlite3.connect(DB_PATH)
    trades = pd.read_sql_query("""
        SELECT * FROM trades
        WHERE close_time IS NOT NULL
          AND close_time >= datetime('now', '-14 days')
    """, conn)
    conn.close()

    if trades.empty or len(trades) < 10:
        print(f"\nยังมี trades น้อยเกิน ({len(trades)}) — รอให้ครบ 50+ trades")
        return

    trades['profit']  = pd.to_numeric(trades['profit'], errors='coerce')
    profits           = trades['profit'].dropna()
    wins              = profits[profits > 0]
    losses            = profits[profits <= 0]

    # Sharpe Ratio (simplified)
    if profits.std() > 0:
        sharpe = (profits.mean() / profits.std()) * (252**0.5)
    else:
        sharpe = 0

    # Max Drawdown
    cumsum     = profits.cumsum()
    peak       = cumsum.cummax()
    drawdown   = (peak - cumsum)
    max_dd     = drawdown.max()

    # Profit Factor
    pf = wins.sum() / abs(losses.sum()) if not losses.empty else float('inf')

    print(f"\n{'='*55}")
    print(f"  2-Week Paper Trading Statistics")
    print(f"{'='*55}")
    print(f"  Total Trades   : {len(trades)}")
    print(f"  Win Rate       : {len(wins)/len(trades)*100:.1f}%")
    print(f"  Sharpe Ratio   : {sharpe:.2f}")
    print(f"  Profit Factor  : {pf:.2f}")
    print(f"  Total P&L      : ${profits.sum():+.2f}")
    print(f"  Max Drawdown   : ${max_dd:.2f}")
    print(f"  Avg Trade      : ${profits.mean():+.2f}")
    print(f"{'─'*55}")

    # Pass/Fail criteria
    criteria = {
        "Total trades ≥ 50"      : len(trades) >= 50,
        "Win rate ≥ 45%"         : len(wins)/len(trades) >= 0.45,
        "Sharpe ratio ≥ 0.8"     : sharpe >= 0.8,
        "Profit factor ≥ 1.2"    : pf >= 1.2,
        "Max DD ≤ $500"          : max_dd <= 500,
        "Total P&L > 0"          : profits.sum() > 0,
    }

    print(f"\n  Go-Live Criteria:")
    all_pass = True
    for name, passed in criteria.items():
        icon = "✅" if passed else "❌"
        print(f"    {icon} {name}")
        if not passed:
            all_pass = False

    print(f"\n{'='*55}")
    if all_pass:
        print(f"  🚀 READY FOR LIVE TRADING!")
    else:
        failed = [k for k,v in criteria.items() if not v]
        print(f"  ⚠️  ยังไม่พร้อม: {failed}")
    print(f"{'='*55}")

    return all_pass


def live_positions():
    """ดู open positions ปัจจุบัน"""
    try:
        import MetaTrader5 as mt5
        from dotenv import load_dotenv
        import os
        load_dotenv()

        mt5.initialize(path=os.getenv('MT5_PATH'))
        mt5.login(int(os.getenv('MT5_LOGIN')),
                  password=os.getenv('MT5_PASSWORD'),
                  server=os.getenv('MT5_SERVER'))

        positions = mt5.positions_get() or []
        acc       = mt5.account_info()
        mt5.shutdown()

        print(f"\n{'─'*55}")
        print(f"  Live Account Snapshot")
        print(f"  Balance:  ${acc.balance:,.2f}")
        print(f"  Equity:   ${acc.equity:,.2f}")
        print(f"  Profit:   ${acc.profit:+.2f}")
        print(f"  Open pos: {len(positions)}")

        if positions:
            for pos in positions:
                arrow = "↑" if pos.type == 0 else "↓"
                icon  = "💚" if pos.profit >= 0 else "🔴"
                print(f"  {icon} {arrow} {pos.symbol} "
                      f"lot={pos.volume:.2f} "
                      f"P&L=${pos.profit:+.2f}")
        print(f"{'─'*55}")

    except Exception as e:
        print(f"  ❌ ไม่สามารถดู positions: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily",  type=int, default=1)
    parser.add_argument("--weekly", action="store_true")
    parser.add_argument("--live",   action="store_true")
    args = parser.parse_args()

    live_positions()
    daily_report(days=args.daily)
    if args.weekly:
>>>>>>> 98ac82b18ee8d71be376450f278ba77dde0c1c3e
        weekly_stats()