# scripts/find_missing_features.py
"""
วินิจฉัย feature mismatch ระหว่าง model (train) vs live data (predict)
รัน: python scripts/find_missing_features.py

Error ที่เห็น:
  [LightGBM] The number of features in data (291) != training data (296)
  → ขาด 5 features
"""
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import joblib
import pandas as pd
from config import get_config

CFG     = get_config()
ROOT    = _ROOT
MODELS  = ROOT / CFG['paths']['models_saved']
PROC    = ROOT / CFG['paths']['data_processed']

# ── ตรวจทุก symbol ─────────────────────────────────────────
for sym in CFG['symbols']['active']:
    print(f"\n{'='*55}")
    print(f"  Symbol: {sym}")
    print(f"{'='*55}")

    # โหลด model feature list
    model_path = MODELS / f"lgbm_{sym}.pkl"
    if not model_path.exists():
        print(f"  ⚠️  ไม่พบ {model_path.name}")
        continue

    payload       = joblib.load(model_path)
    train_features = payload.get('features', [])
    print(f"  Model trained with: {len(train_features)} features")

    # โหลด processed data
    data_path = PROC / f"{sym}_M15_features.parquet"
    if not data_path.exists():
        print(f"  ⚠️  ไม่พบ {data_path.name}")
        continue

    df             = pd.read_parquet(data_path)
    live_features  = [c for c in train_features if c in df.columns]
    missing        = [c for c in train_features if c not in df.columns]
    extra          = [c for c in df.columns if c not in train_features
                     and c not in ('label','open','high','low','close',
                                   'tick_volume','real_volume','spread')]

    print(f"  Live data has:      {len(live_features)}/{len(train_features)} features")

    if missing:
        print(f"\n  ❌ MISSING {len(missing)} features (in model but not in data):")
        for f in missing:
            print(f"     - {f}")

    if extra:
        print(f"\n  ℹ️  Extra {len(extra)} features (in data but not in model):")
        for f in extra[:10]:  # แสดงแค่ 10 ตัวแรก
            print(f"     + {f}")

    if not missing:
        print(f"\n  ✅ Features match perfectly!")

print(f"\n{'='*55}")
print("วิธีแก้:")
print("  Option A (เร็ว): retrain ใหม่ด้วย data ปัจจุบัน")
print("    python models/train_lgbm.py --symbols XAUUSD EURUSD GBPUSD")
print()
print("  Option B (ดีกว่า): หา feature ที่หายแล้วเพิ่มใน pipeline")
print("    ส่วนใหญ่เกิดจาก macro/news features ที่ไม่ได้โหลดตอน predict")
print(f"{'='*55}")
