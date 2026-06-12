"""
Data Pipeline — จุดรวมดึงข้อมูลทั้งหมด
รันด้วย: python data/pipeline.py
Task Scheduler จะรันอัตโนมัติทุกคืน
"""
# ✅ FIX BUG-1: setup_logging ก่อน import อื่น (ป้องกัน log_formatter crash)
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot.setup_logging import setup_logging
setup_logging()

import logging
import time
import json
from datetime import datetime, timezone

# ✅ FIX BUG-2: ใช้ get_config() เพียงอย่างเดียว — ไม่ใช้ yaml.safe_load โดยตรง
# ✅ FIX BUG-3+4: ลบ import ซ้ำ (time ซ้ำ) และ import schedule ที่ไม่ได้ใช้
from config import get_config
CFG = get_config()

log = logging.getLogger("data")

# ── ลบ module-level variables ที่ทำให้สับสน (BUG-5) ──────────
# bars และ macro ถูกใช้ใน run_full_pipeline โดยตรง ไม่ต้องประกาศที่นี่


# ── Import collectors ─────────────────────────────────────────
from data.collect_mt5   import collect_mt5_data
from data.collect_macro import collect_macro_data
from data.collect_news  import collect_news_sentiment


# ── Helper ────────────────────────────────────────────────────
def _ensure_dirs():
    """สร้างโฟลเดอร์ที่ต้องการถ้ายังไม่มี"""
    # ✅ FIX BUG-6: ใช้ absolute path จาก config แทน relative path
    for key in ['data_raw', 'data_processed', 'logs', 'reports']:
        folder = _ROOT / CFG['paths'].get(key, key.replace('_', '/'))
        folder.mkdir(parents=True, exist_ok=True)


def _log_step(step: str, fn, *args, **kwargs):
    """
    Wrapper รัน step พร้อม log เวลาและ error handling
    ถ้า step ไหน fail จะ log error แต่ไม่หยุด pipeline
    """
    log.info(f"▶ {step}...")
    t0 = time.time()
    try:
        result = fn(*args, **kwargs)
        elapsed = time.time() - t0
        log.info(f"✅ {step} สำเร็จ ({elapsed:.1f}s)")
        return result
    except Exception as e:
        elapsed = time.time() - t0
        log.error(f"❌ {step} ล้มเหลว ({elapsed:.1f}s): {e}", exc_info=True)
        return None


# ── Main Pipeline ─────────────────────────────────────────────
def run_full_pipeline(
    collect_mt5_data_flag:   bool = True,
    collect_macro_flag: bool = True,
    collect_news_flag:  bool = True,
) -> dict:
    """
    รัน pipeline ทั้งหมด — เรียกจาก Task Scheduler ทุกคืน
    คืนค่า dict สรุปผลแต่ละ step
    """
    _ensure_dirs()

    start_time = time.time()
    results    = {}

    log.info("=" * 60)
    log.info(f"Pipeline เริ่ม: {datetime.now(timezone.utc).isoformat()}")
    log.info("=" * 60)

    # ── Step 1: MT5 Price Data ────────────────────────────────
    if collect_mt5_data_flag:
        symbols     = CFG['symbols']['active']
        timeframes  = [CFG['symbols']['primary_timeframe']] + \
                       CFG['symbols']['htf_timeframes']
        bars        = CFG['data']['history_bars']

        results['mt5'] = _log_step(
            "Step 1: ดึงราคาจาก MT5",
            collect_mt5_data,
            symbols    = symbols,
            timeframes = timeframes,
            bars       = bars,
        )

    # ── Step 2: Macro Data ────────────────────────────────────
    if collect_macro_flag:
        macro_syms = CFG['data']['macro_symbols']

        results['macro'] = _log_step(
            "Step 2: ดึง Macro data (DXY, VIX, US10Y)",
            collect_macro_data,
            sources = macro_syms,
        )

    # ── Step 3: News Sentiment ────────────────────────────────
    if collect_news_flag:
        results['news'] = _log_step(
            "Step 3: ดึง News sentiment (GDELT)",
            collect_news_sentiment,
        )

    # ── สรุปผล ────────────────────────────────────────────────
    elapsed = time.time() - start_time
    success = sum(1 for v in results.values() if v is not None)
    total   = len(results)

    log.info("=" * 60)
    log.info(f"Pipeline เสร็จ: {success}/{total} steps สำเร็จ ({elapsed:.1f}s)")
    log.info("=" * 60)

    # บันทึกเวลา update ล่าสุด
    _save_last_update(results)

    return results


def _save_last_update(results: dict):
    """บันทึก timestamp ว่า pipeline รันล่าสุดเมื่อไหร่"""
    status = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "steps"   : {k: "ok" if v else "failed" for k, v in results.items()},
    }
    # ✅ FIX BUG-6: absolute path
    out_path = _ROOT / CFG['paths']['logs'] / "pipeline_status.json"
    out_path.write_text(json.dumps(status, indent=2), encoding="utf-8")


# ── Quick update (รันเร็ว — เฉพาะราคาล่าสุด) ─────────────────
def run_quick_update():
    """
    ดึงข้อมูลล่าสุดก่อนบอทเริ่มรัน
    ใช้ใน bot/main.py ตอนเริ่มต้น
    """
    log.info("Quick update: ดึงราคาล่าสุด...")

    symbols    = CFG['symbols']['active']
    tf_primary = CFG['symbols']['primary_timeframe']

    _log_step(
        "Quick MT5 update",
        collect_mt5_data,
        symbols    = symbols,
        timeframes = [tf_primary],
        bars       = {tf_primary: 500},   # แค่ 500 bars ล่าสุด
    )


# ── Entry point ───────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trading Bot Data Pipeline")
    parser.add_argument("--quick",      action="store_true",
                        help="รันเฉพาะ MT5 ราคาล่าสุด")
    parser.add_argument("--no-mt5",     action="store_true",
                        help="ข้าม MT5 data")
    parser.add_argument("--no-macro",   action="store_true",
                        help="ข้าม macro data")
    parser.add_argument("--no-news",    action="store_true",
                        help="ข้าม news sentiment")
    args = parser.parse_args()

    if args.quick:
        run_quick_update()
    else:
        run_full_pipeline(
            collect_mt5_data_flag = not args.no_mt5,
            collect_macro_flag    = not args.no_macro,
            collect_news_flag     = not args.no_news,
        )