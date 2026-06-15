# scripts/retrain_all.py
"""
Full Retrain Pipeline
data → features → train ทุก symbol → verify → notify

รันโดย:
  - Task Scheduler ทุกอาทิตย์ (อัตโนมัติ)
  - python scripts/retrain_all.py (มือ)
  - run_bot.bat เมนู 9 (debug)
"""

import logging
import logging.config
import yaml
import time
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

# โหลดคอนฟิกสากลพร้อมรองรับภาษาไทยอย่างปลอดภัย
with open("logging.yaml", encoding="utf-8") as f:
    logging.config.dictConfig(yaml.safe_load(f))
with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

log = logging.getLogger("models")


def retrain_all(
    symbols:   list  = None,
    timeframe: str   = None,
    skip_lstm: bool  = False,   # ข้าม LSTM ถ้าเวลาจำกัด
    skip_bt:   bool  = False, ) -> dict:    # ข้าม backtest

    # ✅ ดึงเฉพาะรายชื่อคู่เงินจากหัวข้อ active ใน config.yaml เท่านั้น
    if not symbols:
        if 'symbols' in CFG and 'active' in CFG['symbols']:
            symbols = CFG['symbols']['active']
        else:
            symbols = ["XAUUSDm", "EURUSDm", "GBPUSDm"]

    timeframe = timeframe or CFG.get('symbols', {}).get('primary_timeframe', 'M15')
    results   = {}
    t_start   = time.time()

    log.info("=" * 60)
    log.info(f"🚀 STARTING RETRAIN PIPELINE — {datetime.now(timezone.utc).isoformat()}")
    log.info(f"Symbols to process: {symbols} | Timeframe: {timeframe}")
    log.info("=" * 60)

    # ── Step 1: Backup ────────────────────────────────────────
    log.info("[1/8] Backup current models...")
    try:
        from models.manage_models import backup_models
        backup_models(tag="retrain")
        results['backup'] = "ok"
        log.info("  ✅ Backup completed successfully")
    except Exception as e:
        log.warning(f"  ⚠️ Backup failed (non-fatal): {e}")
        results['backup'] = "skipped"

    # ── Step 2: Update Data ───────────────────────────────────
    log.info("[2/8] Fetching newest market data from MT5...")
    try:
        from data.pipeline import run_full_pipeline
        run_full_pipeline()
        results['data'] = "ok"
        log.info("  ✅ Data pipeline sync completed")
    except Exception as e:
        log.error(f"  ❌ Data update FAILED: {e}", exc_info=True)
        results['data'] = "failed"
        _notify(f"❌ Retrain aborted: data update failed\n{e}")
        return results   # หยุดการทำงานทันทีหากข้อมูลดิบพัง

    # ── Step 3: Build Features ────────────────────────────────
    log.info("[3/8] Extracting features and indicators...")
    try:
        from features.pipeline import build_all_features
        build_all_features(symbols=symbols, timeframe=timeframe)
        results['features'] = "ok"
        log.info("  ✅ Technical features built")
    except Exception as e:
        log.error(f"  ❌ Feature build FAILED: {e}", exc_info=True)
        results['features'] = "failed"
        _notify(f"❌ Retrain aborted: feature build failed\n{e}")
        return results

    # ── Step 4: Train XGBoost ─────────────────────────────────
    log.info("[4/8] Training XGBoost Classifier...")
    xgb_res = {}
    for sym in symbols:
        try:
            from models.train_xgb import train_xgboost
            r = train_xgboost(sym, timeframe)
            xgb_res[sym] = {
                'acc': round(getattr(r, 'mean_accuracy', 0), 3),
                'f1' : round(getattr(r, 'mean_f1', 0), 3),
                'ok' : r.is_acceptable() if hasattr(r, 'is_acceptable') else True,
            }
            icon = "✅" if xgb_res[sym]['ok'] else "⚠️"
            summary_text = r.summary() if hasattr(r, 'summary') else f"Acc={xgb_res[sym]['acc']}"
            log.info(f"  {icon} XGB {sym}: {summary_text}")
        except Exception as e:
            log.error(f"  ❌ XGB {sym} failed to train: {e}")
            xgb_res[sym] = {'ok': False, 'error': str(e)[:100]}
    results['xgb'] = xgb_res

    # ── Step 5: Train LightGBM ────────────────────────────────
    log.info("[5/8] Training LightGBM Classifier...")
    lgbm_res = {}
    for sym in symbols:
        try:
            from models.train_lgbm import train_lightgbm
            r = train_lightgbm(sym, timeframe)
            lgbm_res[sym] = {
                'acc': round(getattr(r, 'mean_accuracy', 0), 3),
                'f1' : round(getattr(r, 'mean_f1', 0), 3),
                'ok' : r.is_acceptable() if hasattr(r, 'is_acceptable') else True,
            }
            icon = "✅" if lgbm_res[sym]['ok'] else "⚠️"
            summary_text = r.summary() if hasattr(r, 'summary') else f"Acc={lgbm_res[sym]['acc']}"
            log.info(f"  {icon} LGBM {sym}: {summary_text}")
        except Exception as e:
            log.error(f"  ❌ LGBM {sym} failed to train: {e}")
            lgbm_res[sym] = {'ok': False, 'error': str(e)[:100]}
    results['lgbm'] = lgbm_res

    # ── Step 6: Train LSTM (optional) ─────────────────────────
    if skip_lstm:
        log.info("[6/8] Skipping Deep Learning LSTM (--skip-lstm)")
        results['lstm'] = "skipped"
    else:
        log.info("[6/8] Training Deep Learning LSTM...")
        lstm_res = {}
        # คัดกรองรันเฉพาะคู่หลักที่มีความต้องการ และป้องกันขอบเขตดึงข้อมูลเกินสเกล
        for sym in [s for s in symbols if "XAU" in s or s == symbols[0]]:
            try:
                from models.train_lstm import train_lstm
                r = train_lstm(sym, timeframe, epochs=30)
                lstm_res[sym] = {
                    'acc': round(getattr(r, 'mean_accuracy', 0), 3),
                    'f1' : round(getattr(r, 'mean_f1', 0), 3),
                    'ok' : r.is_acceptable() if hasattr(r, 'is_acceptable') else True,
                }
                icon = "✅" if lstm_res[sym]['ok'] else "⚠️"
                summary_text = r.summary() if hasattr(r, 'summary') else f"Acc={lstm_res[sym]['acc']}"
                log.info(f"  {icon} LSTM {sym}: {summary_text}")
            except Exception as e:
                log.warning(f"  ⚠️ LSTM {sym}: {e} (non-fatal)")
                lstm_res[sym] = {'ok': False, 'error': str(e)[:100]}
        results['lstm'] = lstm_res

    # ── Step 7: Verify models ─────────────────────────────────
    log.info("[7/8] Verifying structural integrity of new models...")
    try:
        from models.manage_models import verify_model
        verify_res = {}
        for sym in symbols:
            v = verify_model(sym)
            all_ok = all('OK' in str(m.get('status','')) for m in v.values()) if isinstance(v, dict) else True
            verify_res[sym] = "ok" if all_ok else "partial"
            icon = "✅" if all_ok else "⚠️"
            log.info(f"  {icon} Verify {sym}: {verify_res[sym]}")
        results['verify'] = verify_res
    except Exception as e:
        log.warning(f"  ⚠️ Verification module error: {e}")
        results['verify'] = "error"

    # ── Step 8: Backtest + Checklist ──────────────────────────
    if skip_bt:
        log.info("[8/8] Skipping backtest (--skip-bt)")
        results['backtest'] = "skipped"
    else:
        log.info("[8/8] Running OOS Backtest & Deploy Checklist verification...")
        bt_res = {}
        for sym in symbols:
            try:
                from models.backtest import run_deploy_checklist
                checklist = run_deploy_checklist(sym, timeframe)
                passed     = all(v['pass'] for v in checklist.values()) if isinstance(checklist, dict) else True
                bt_res[sym] = {
                    'passed': passed,
                    'grade' : checklist.get('backtest',{}).get('grade','?') if isinstance(checklist, dict) else 'A',
                }
                icon = "✅" if passed else "⚠️"
                log.info(f"  {icon} Checklist {sym}: {'PASS' if passed else 'PARTIAL'} grade={bt_res[sym]['grade']}")
            except Exception as e:
                log.error(f"  ❌ Backtest evaluation failed for {sym}: {e}")
                bt_res[sym] = {'passed': False, 'error': str(e)[:100]}
        results['backtest'] = bt_res

    # ── Summary & Saving Report ────────────────────────────────
    elapsed = round(time.time() - t_start)
    results['duration_sec'] = elapsed
    results['completed_at'] = datetime.now(timezone.utc).isoformat()

    # สร้างโฟลเดอร์สำหรับรายงานผลหากยังไม่มีในเครื่อง
    Path("reports").mkdir(exist_ok=True)
    out = Path("reports/retrain_report.json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    log.info("=" * 60)
    log.info(f"🎉 PIPELINE EXECUTION COMPLETE ({elapsed}s) → Logs saved to reports/retrain_report.json")
    log.info("=" * 60)

    _notify_complete(results, symbols, elapsed)
    return results


def _notify(msg: str):
    try:
        from bot.notifier import notify
        notify(msg)
    except Exception:
        pass


def _notify_complete(results: dict, symbols: list, elapsed: int):
    lines = [f"🔄 *Retrain Complete* ({elapsed}s)\n"]
    for sym in symbols:
        xgb  = results.get('xgb',  {}).get(sym, {})
        lgbm = results.get('lgbm', {}).get(sym, {})
        bt   = results.get('backtest', {}).get(sym, {})
        lines.append(
            f"*{sym}*\n"
            f"  XGB:  f1={xgb.get('f1','?')} "
            f"{'✅' if xgb.get('ok') else '⚠️'}\n"
            f"  LGBM: f1={lgbm.get('f1','?')} "
            f"{'✅' if lgbm.get('ok') else '⚠️'}\n"
            f"  BT:   {'✅ PASS' if bt.get('passed') else '⚠️ FAIL'}"
        )
    _notify("\n".join(lines))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols",   nargs="+")
    parser.add_argument("--skip-lstm", action="store_true")
    parser.add_argument("--skip-bt",   action="store_true")
    args = parser.parse_args()

    retrain_all(
        symbols   = args.symbols,
        skip_lstm = args.skip_lstm,
        skip_bt   = args.skip_bt,
    )