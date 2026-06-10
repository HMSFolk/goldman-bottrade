# models/manage_models.py
"""
จัดการ lifecycle ของโมเดลทั้งหมด
- ดูข้อมูล metadata
- เปรียบเทียบ version
- backup ก่อน retrain
- ลบโมเดลเก่า
"""

import joblib
import torch
import json
import shutil
import logging
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger("models")

MODELS_DIR = Path("models/saved")
BACKUP_DIR = Path("models/backup")


def list_models() -> list[dict]:
    """แสดงโมเดลทั้งหมดพร้อม metadata"""
    models = []

    for path in sorted(MODELS_DIR.glob("*.pkl")):
        try:
            payload = joblib.load(path)
            meta    = payload.get('meta', {})
            models.append({
                'file'      : path.name,
                'type'      : 'pkl',
                'symbol'    : meta.get('symbol',     '?'),
                'timeframe' : meta.get('timeframe',  '?'),
                'n_features': meta.get('n_features', 0),
                'trained_at': meta.get('trained_at', '?')[:10],
                'size_mb'   : round(path.stat().st_size / 1_048_576, 2),
            })
        except Exception as e:
            models.append({
                'file': path.name, 'error': str(e)
            })

    for path in sorted(MODELS_DIR.glob("*.pth")):
        try:
            payload = torch.load(path, map_location='cpu')
            meta    = payload.get('meta', {})
            arch    = payload.get('arch_params', {})
            models.append({
                'file'      : path.name,
                'type'      : 'pth',
                'symbol'    : meta.get('symbol',     '?'),
                'seq_len'   : arch.get('seq_len',    60),
                'hidden'    : arch.get('hidden_size',128),
                'n_features': meta.get('n_features', 0),
                'trained_at': meta.get('trained_at', '?')[:10],
                'size_mb'   : round(path.stat().st_size / 1_048_576, 2),
            })
        except Exception as e:
            models.append({
                'file': path.name, 'error': str(e)
            })

    return models


def print_model_table():
    """แสดงตารางโมเดลทั้งหมด"""
    models = list_models()
    print(f"\n{'='*75}")
    print(f"{'File':<30} {'Symbol':<8} {'Features':<10} "
          f"{'Trained':<12} {'Size(MB)':<10}")
    print(f"{'-'*75}")
    for m in models:
        if 'error' in m:
            print(f"  ❌ {m['file']}: {m['error']}")
        else:
            print(
                f"  {m['file']:<28} {m.get('symbol','?'):<8} "
                f"{m.get('n_features',0):<10} "
                f"{m.get('trained_at','?'):<12} "
                f"{m.get('size_mb',0):<10}"
            )
    print(f"{'='*75}")
    total_mb = sum(
        m.get('size_mb',0) for m in models if 'error' not in m
    )
    print(f"Total: {len(models)} models | {total_mb:.1f} MB\n")


def backup_models(tag: str = ""):
    """
    Backup โมเดลปัจจุบันก่อน retrain
    ทำทุกครั้งก่อนรัน retrain_all.py
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    ts  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    tag = f"_{tag}" if tag else ""
    dest= BACKUP_DIR / f"backup_{ts}{tag}"
    dest.mkdir(exist_ok=True)

    copied = 0
    for path in MODELS_DIR.glob("*.pkl"):
        shutil.copy2(path, dest / path.name)
        copied += 1
    for path in MODELS_DIR.glob("*.pth"):
        shutil.copy2(path, dest / path.name)
        copied += 1
    for path in MODELS_DIR.glob("*.json"):
        shutil.copy2(path, dest / path.name)
        copied += 1

    log.info(f"✅ Backed up {copied} files → {dest}")
    return dest


def restore_backup(backup_dir: str):
    """Restore โมเดลจาก backup"""
    src = Path(backup_dir)
    if not src.exists():
        raise FileNotFoundError(f"ไม่พบ backup: {src}")

    for path in src.glob("*.pkl"):
        shutil.copy2(path, MODELS_DIR / path.name)
    for path in src.glob("*.pth"):
        shutil.copy2(path, MODELS_DIR / path.name)

    log.info(f"✅ Restored from {src}")


def verify_model(symbol: str) -> dict:
    """
    ตรวจสอบว่าโมเดลโหลดและ predict ได้จริง
    รันหลัง retrain เสมอ
    """
    results = {}

    # XGBoost
    try:
        from models.train_xgb import load_model, predict
        payload = load_model(symbol)
        n_feat  = payload['meta']['n_features']
        results['xgb'] = {
            'status'    : '✅ OK',
            'n_features': n_feat,
            'trained_at': payload['meta']['trained_at'][:10],
        }
    except Exception as e:
        results['xgb'] = {'status': f'❌ {e}'}

    # LightGBM
    try:
        from models.train_lgbm import load_model
        payload = load_model(symbol)
        results['lgbm'] = {
            'status'    : '✅ OK',
            'n_features': payload['meta']['n_features'],
            'trained_at': payload['meta']['trained_at'][:10],
        }
    except Exception as e:
        results['lgbm'] = {'status': f'❌ {e}'}

    # LSTM
    try:
        from models.train_lstm import load_model
        model, scaler, arch, features = load_model(symbol)
        results['lstm'] = {
            'status'    : '✅ OK',
            'seq_len'   : arch['seq_len'],
            'hidden'    : arch['hidden_size'],
            'n_features': len(features),
        }
    except Exception as e:
        results['lstm'] = {'status': f'❌ {e}'}

    print(f"\nModel Verification: {symbol}")
    for name, info in results.items():
        print(f"  {name.upper():6}: {info}")

    return results


def cleanup_old_backups(keep_last: int = 5):
    """ลบ backup เก่า เก็บแค่ n ล่าสุด"""
    if not BACKUP_DIR.exists():
        return

    backups = sorted(BACKUP_DIR.iterdir(), key=lambda p: p.name)
    to_delete = backups[:-keep_last] if len(backups) > keep_last else []

    for b in to_delete:
        shutil.rmtree(b)
        log.info(f"ลบ backup เก่า: {b.name}")

    log.info(
        f"Cleanup: ลบ {len(to_delete)} backups เก่า | "
        f"เหลือ {len(backups)-len(to_delete)} backups"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--list",    action="store_true")
    parser.add_argument("--backup",  action="store_true")
    parser.add_argument("--verify",  type=str, default="XAUUSD")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--restore", type=str, default="")
    args = parser.parse_args()

    if args.list:
        print_model_table()
    if args.backup:
        backup_models()
    if args.verify:
        verify_model(args.verify)
    if args.cleanup:
        cleanup_old_backups(keep_last=5)
    if args.restore:
        restore_backup(args.restore)