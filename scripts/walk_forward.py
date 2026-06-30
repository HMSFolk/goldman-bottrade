# scripts/walk_forward.py
"""
Walk-Forward Out-Of-Sample Validation
═════════════════════════════════════════════════════════════════════
✅ C-1 (2026-06-29): เขียนใหม่ให้ใช้ backtest_engine.walk_forward_engine
   (event-driven, StrategyV1 ตัวเดียวกับ bot, RR จาก config) แทนของเดิม
   ที่เป็น vectorbt long-only + SL/TP คงที่ 0.5%/1% ไม่อ่าน config →
   ผล OOS ไม่ตรง live (หลอกได้)

วัดว่าระบบ "บวกสม่ำเสมอข้ามช่วงเวลา" ไหม — กัน overfit ช่วงเดียว
รายงานเป็น expectancy_R ต่อ fold (ตัวชี้ขาด ไม่ใช่ return ลอยๆ)

รัน:  python scripts/walk_forward.py [--symbols XAUUSD ...] [--splits 5]
"""
import sys
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
from config import get_config
from models.backtest_engine import walk_forward_engine

CFG = get_config()
PROCESSED = _ROOT / CFG["paths"]["data_processed"]


def walk_forward_test(symbol: str = None, n_splits: int = 5,
                      timeframe: str = "M15") -> dict:
    """รัน walk-forward 1 symbol — คืน dict ผลรวม + per-fold"""
    symbol = symbol or CFG["symbols"]["active"][0]
    path   = PROCESSED / f"{symbol}_{timeframe}_features.parquet"
    if not path.exists():
        print(f"WARN: ไม่พบ {path} — ข้าม {symbol}")
        return {}

    df = pd.read_parquet(path).dropna(subset=["label"])
    wf = walk_forward_engine(df, symbol, n_splits=n_splits, timeframe=timeframe)

    print(f"\n=== Walk-Forward OOS: {symbol} ===")
    for f in wf["per_fold"]:
        print(f"  Fold {f['fold']}: E={f['expectancy_R']:+.3f}R "
              f"WR={f['win_rate']:.0f}% trades={f['trades']} "
              f"ret={f['return_pct']:+.1f}%")
    verdict = "robust" if wf["is_robust"] else "ไม่ robust (E<=0 หรือ consistency<60%)"
    print(f"  -> mean E={wf['mean_expectancy_R']:+.3f}R | "
          f"consistency={wf['consistency']:.0%} | {verdict}")

    if wf["per_fold"]:
        os.makedirs("reports", exist_ok=True)
        pd.DataFrame(wf["per_fold"]).to_csv(
            f"reports/walk_forward_{symbol}.csv", index=False)
    return wf


def run_all_symbols(symbols: list = None, n_splits: int = 5) -> dict:
    """รัน walk-forward ทุก symbol — ตัวไหน fail ข้าม ไม่พังตัวอื่น"""
    os.makedirs("reports", exist_ok=True)
    symbols = symbols or CFG["symbols"]["active"]
    out = {}
    for sym in symbols:
        try:
            out[sym] = walk_forward_test(symbol=sym, n_splits=n_splits)
        except Exception as e:
            print(f"ERROR: Walk-Forward {sym} ล้มเหลว: {e}")
            out[sym] = None
    return out


if __name__ == "__main__":
    import argparse
    print("Starting Walk-Forward OOS Validation (event-engine ตรง live)...")
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=None)
    ap.add_argument("--splits", type=int, default=5)
    args = ap.parse_args()
    run_all_symbols(symbols=args.symbols, n_splits=args.splits)