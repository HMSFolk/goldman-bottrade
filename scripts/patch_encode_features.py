# scripts/patch_encode_features.py
"""
Patch ไฟล์ processed data — เพิ่ม _enc columns ที่หายไป
แก้ปัญหา: feature mismatch (train=296, predict=291)

รัน: python scripts/patch_encode_features.py
แล้วตามด้วย retrain:
  python models/train_lgbm.py --symbols XAUUSDm EURUSDm GBPUSDm --hyperopt
"""
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import numpy as np
from sklearn.preprocessing import LabelEncoder
from config import get_config

CFG  = get_config()
PROC = _ROOT / CFG['paths']['data_processed']

# Categorical columns ที่ต้อง encode → สร้าง *_enc column
# (ตามที่ diagnostic พบว่าหายไป)
CAT_COLS = [
    'trend_cat',
    'rsi_zone',
    'vol_regime',
    'nearest_pivot_level',
    'price_zone_20',
]

symbols = CFG['symbols']['active']
print(f"Patching {len(symbols)} symbols: {symbols}")
print(f"Adding _enc for: {CAT_COLS}\n")

for sym in symbols:
    # ตรวจทุก timeframe ที่มีใน processed
    pattern = f"{sym}_*_features.parquet"
    pq_files = list(PROC.glob(pattern))

    if not pq_files:
        print(f"  ⚠️  {sym}: ไม่พบ parquet ใน {PROC}")
        continue

    for pq_path in pq_files:
        print(f"  Processing {pq_path.name} ...")
        df = pd.read_parquet(pq_path)
        added = []

        for col in CAT_COLS:
            enc_col = f"{col}_enc"

            if enc_col in df.columns:
                print(f"    ✓ {enc_col} มีอยู่แล้ว — ข้าม")
                continue

            if col not in df.columns:
                print(f"    ⚠️  {col} ไม่มีใน data — ข้าม")
                continue

            # Label Encode: string → integer
            le  = LabelEncoder()
            raw = df[col].fillna('UNKNOWN').astype(str)
            df[enc_col] = le.fit_transform(raw).astype(np.int32)
            added.append(enc_col)

        # Save กลับไป
        df.to_parquet(pq_path, index=True)
        print(f"    ✅ Added: {added}")
        print(f"    Total columns: {len(df.columns)}")

print(f"\n{'='*55}")
print("Patch เสร็จแล้ว!")
print()
print("ขั้นต่อไป — retrain ด้วย features ใหม่:")
print(f"  python models\\train_lgbm.py --symbols {' '.join(symbols)} --hyperopt")
print()
print("หมายเหตุ:")
print("  - _enc columns ใช้ LabelEncoder (consistent ordering)")
print("  - Extra features ใหม่ (above_all_highs, htf_conflict ฯลฯ)")
print("    จะถูก include อัตโนมัติตอน retrain")
print(f"{'='*55}")
