"""
check_labels.py — ตรวจ label distribution ก่อน retrain
รัน: python check_labels.py
"""
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import numpy as np

PROCESSED = _ROOT / "data" / "processed"
SYMBOLS   = ["XAUUSD", "EURUSD", "GBPUSD"]

# ── ANSI colors ────────────────────────────────────────────
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def bar(pct, width=30):
    filled = int(pct * width)
    return "█" * filled + "░" * (width - filled)

def check_symbol(sym: str):
    path = PROCESSED / f"{sym}_M15_features.parquet"
    if not path.exists():
        print(f"{RED}❌ ไม่พบ {path.name}{RESET}")
        return None

    df = pd.read_parquet(path)
    if 'label' not in df.columns:
        print(f"{RED}❌ {sym}: ไม่มี column 'label'{RESET}")
        return None

    # ── ข้อมูลพื้นฐาน ────────────────────────────────────
    n_total    = len(df)
    date_start = df.index[0].strftime("%Y-%m-%d")
    date_end   = df.index[-1].strftime("%Y-%m-%d")
    n_days     = (df.index[-1] - df.index[0]).days

    # ── Label distribution ───────────────────────────────
    counts = df['label'].value_counts().sort_index()
    # label: -1=SELL, 0=HOLD, 1=BUY
    n_sell = counts.get(-1, 0)
    n_hold = counts.get(0,  0)
    n_buy  = counts.get(1,  0)

    pct_sell = n_sell / n_total
    pct_hold = n_hold / n_total
    pct_buy  = n_buy  / n_total

    # ── ประเมินความสมดุล ─────────────────────────────────
    # สมดุลดี: BUY/SELL ห่างกันไม่เกิน 15pp, HOLD < 60%
    buy_sell_gap = abs(pct_buy - pct_sell)
    imbalance_ok = buy_sell_gap < 0.15 and pct_hold < 0.60
    data_ok      = n_days >= 365

    # ── Print ────────────────────────────────────────────
    print(f"\n{BOLD}{CYAN}{'─'*55}")
    print(f"  {sym}")
    print(f"{'─'*55}{RESET}")
    print(f"  📅 ช่วงข้อมูล : {date_start} → {date_end}")
    print(f"  🗓  ระยะเวลา  : {n_days} วัน ({n_days/365:.1f} ปี)  {'✅' if data_ok else RED+'⚠️  ต้องการ >= 1 ปี'+RESET}")
    print(f"  📊 จำนวนแท่ง : {n_total:,} bars (M15)")
    print()
    print(f"  Label Distribution:")

    sell_color = GREEN if pct_sell > 0.25 else RED
    hold_color = GREEN if pct_hold < 0.55 else YELLOW
    buy_color  = GREEN if pct_buy  > 0.25 else RED

    print(f"  {sell_color}SELL (-1): {bar(pct_sell)} {pct_sell:5.1%}  ({n_sell:,}){RESET}")
    print(f"  {hold_color}HOLD ( 0): {bar(pct_hold)} {pct_hold:5.1%}  ({n_hold:,}){RESET}")
    print(f"  {buy_color}BUY  (+1): {bar(pct_buy)}  {pct_buy:5.1%}  ({n_buy:,}){RESET}")
    print()

    if imbalance_ok:
        print(f"  {GREEN}✅ Label สมดุลดี (BUY/SELL gap={buy_sell_gap:.1%}){RESET}")
    else:
        print(f"  {RED}⚠️  Label ไม่สมดุล (BUY/SELL gap={buy_sell_gap:.1%}){RESET}")
        if pct_buy > pct_sell + 0.15:
            print(f"  {RED}   → BUY มากกว่า SELL — โมเดลจะ bias BUY{RESET}")
        if pct_hold > 0.60:
            print(f"  {YELLOW}   → HOLD > 60% — สัญญาณน้อยเกินไป ลอง min_return_pct ลง{RESET}")

    # ── Session distribution (ถ้ามี) ────────────────────
    if 'is_london_session' in df.columns:
        london_pct = df['is_london_session'].mean()
        ny_pct     = df['is_ny_session'].mean() if 'is_ny_session' in df.columns else 0
        print(f"\n  Session coverage:")
        print(f"  London: {london_pct:.0%}  |  New York: {ny_pct:.0%}")
    else:
        print(f"\n  {YELLOW}⚠️  ไม่มี session features → ต้องรัน pipeline.py ใหม่{RESET}")

    return {
        'symbol': sym, 'n_days': n_days, 'n_total': n_total,
        'pct_buy': pct_buy, 'pct_sell': pct_sell, 'pct_hold': pct_hold,
        'data_ok': data_ok, 'label_ok': imbalance_ok,
    }

# ── Main ─────────────────────────────────────────────────
print(f"\n{BOLD}{'='*55}")
print("  Goldman Bot — Label Distribution Check")
print(f"{'='*55}{RESET}")

results = [check_symbol(s) for s in SYMBOLS]
results = [r for r in results if r]

# ── สรุป ────────────────────────────────────────────────
print(f"\n{BOLD}{'='*55}")
print("  สรุป")
print(f"{'='*55}{RESET}")
for r in results:
    data_icon  = "✅" if r['data_ok']  else "❌"
    label_icon = "✅" if r['label_ok'] else "⚠️"
    print(f"  {r['symbol']:8} | ข้อมูล {data_icon} {r['n_days']:>4}วัน | "
          f"Label {label_icon} BUY={r['pct_buy']:.0%} SELL={r['pct_sell']:.0%}")

# ── คำแนะนำ ──────────────────────────────────────────────
any_short = any(not r['data_ok'] for r in results)
any_imbal = any(not r['label_ok'] for r in results)

print()
if any_short:
    print(f"{RED}❌ ข้อมูลสั้นเกินไป:{RESET}")
    print("   แก้ config.yaml → history_bars: M15: 50000")
    print("   แล้วรัน: python data/collect_mt5.py")
    print("   แล้วรัน: python features/pipeline.py")

if any_imbal:
    print(f"{YELLOW}⚠️  Label ไม่สมดุล:{RESET}")
    print("   ตรวจสอบว่าได้แก้ train_lgbm.py (ลบ BUY boost) แล้วหรือยัง")
    print("   ลอง ลด min_return_pct: 0.0010 (ได้ SELL label มากขึ้น)")

if not any_short and not any_imbal:
    print(f"{GREEN}✅ พร้อม retrain!{RESET}")
    print("   scp data/processed/ ไปยัง cloud แล้ว docker compose up -d")