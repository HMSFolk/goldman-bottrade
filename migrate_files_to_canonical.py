# migrate_files_to_canonical.py
"""
รันครั้งเดียวก่อนเริ่มใช้ broker-agnostic config
เปลี่ยนชื่อไฟล์เก่าทั้งหมดจาก "XAUUSDm" → "XAUUSD" (ชื่อกลาง)

ทำไมต้องรัน:
  หลัง config.yaml เปลี่ยน symbols.active เป็น ["XAUUSD","EURUSD","GBPUSD"]
  (ไม่มี m) แต่ไฟล์เก่าที่เคย collect/train ไว้ยังชื่อ "...XAUUSDm..."
  → train script หา data/processed/XAUUSD_M15_features.parquet ไม่เจอ
  → _check_models_exist() หา models/saved/xgb_XAUUSD.pkl ไม่เจอ → bot ไม่ start

ครอบคลุม:
  data/raw/XAUUSDm_*.parquet        → XAUUSD_*.parquet
  data/processed/XAUUSDm_*.parquet  → XAUUSD_*.parquet
  models/saved/xgb_XAUUSDm.pkl      → xgb_XAUUSD.pkl
  models/saved/lgbm_XAUUSDm.pkl     → lgbm_XAUUSD.pkl
  models/saved/lstm_XAUUSDm.*       → lstm_XAUUSD.*
  reports/*_XAUUSDm_*                → *_XAUUSD_*

วิธีรัน:
    python migrate_files_to_canonical.py            # dry-run (แสดงแผนแต่ไม่แก้จริง)
    python migrate_files_to_canonical.py --apply     # รันจริง
"""
import sys
import shutil
from pathlib import Path
from config import get_config

CFG = get_config()
_ROOT = Path(__file__).resolve().parent

# ── สร้าง reverse map จาก symbol_map: broker_name → canonical ──
def _build_rename_map() -> dict:
    broker  = CFG.get('broker', {}).get('name', 'exness')
    sym_map = CFG.get('symbol_map', {})
    rename  = {}
    for canonical, brokers in sym_map.items():
        broker_name = brokers.get(broker)
        if broker_name and broker_name != canonical:
            rename[broker_name] = canonical
    return rename


RENAME_MAP = _build_rename_map()   # เช่น {"XAUUSDm": "XAUUSD", ...}

# ── โฟลเดอร์ที่ต้องสแกน ─────────────────────────────────────
SCAN_DIRS = [
    CFG['paths'].get('data_raw', 'data/raw'),
    CFG['paths'].get('data_processed', 'data/processed'),
    CFG['paths'].get('models_saved', 'models/saved'),
    CFG['paths'].get('reports', 'reports'),
]


def find_renames() -> list:
    """หาไฟล์ทั้งหมดที่ต้อง rename — คืน list of (old_path, new_path)"""
    plan = []

    for dir_str in SCAN_DIRS:
        d = _ROOT / dir_str
        if not d.exists():
            continue

        for f in d.iterdir():
            if not f.is_file():
                continue

            new_name = f.name
            for broker_sym, canonical in RENAME_MAP.items():
                if broker_sym in new_name:
                    new_name = new_name.replace(broker_sym, canonical)

            if new_name != f.name:
                plan.append((f, f.parent / new_name))

    return plan


def run(apply: bool = False):
    if not RENAME_MAP:
        print("❌ symbol_map ว่างเปล่าใน config.yaml — ไม่มีอะไร rename")
        return

    print(f"Rename map (broker={CFG.get('broker',{}).get('name')}):")
    for old, new in RENAME_MAP.items():
        print(f"  {old} → {new}")
    print()

    plan = find_renames()

    if not plan:
        print("✅ ไม่มีไฟล์ที่ต้อง rename — ทุกอย่างเป็นชื่อกลางอยู่แล้ว")
        return

    print(f"พบ {len(plan)} ไฟล์ที่ต้อง rename:\n")
    for old, new in plan:
        print(f"  {old.relative_to(_ROOT)}")
        print(f"    → {new.relative_to(_ROOT)}")

    if not apply:
        print(f"\n⚠️  DRY RUN — ไม่มีการแก้ไขจริง")
        print(f"รัน: python migrate_files_to_canonical.py --apply  เพื่อทำจริง")
        return

    print(f"\nกำลัง rename {len(plan)} ไฟล์...")
    conflicts = []
    for old, new in plan:
        if new.exists():
            conflicts.append(new)
            continue
        shutil.move(str(old), str(new))

    if conflicts:
        print(f"\n⚠️  ข้าม {len(conflicts)} ไฟล์ (ปลายทางมีอยู่แล้ว):")
        for c in conflicts:
            print(f"    {c.relative_to(_ROOT)}")

    print(f"\n✅ Migration complete: {len(plan)-len(conflicts)} ไฟล์ถูก rename")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    run(apply=apply)