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

with open("logging.yaml", encoding="utf-8") as f:
    logging.config.dictConfig(yaml.safe_load(f))
with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

log = logging.getLogger("models")


def retrain_all(
    symbols:   list  = None,
    timeframe: str   = None,
    skip_lstm: bool  = False,   # ข้าม LSTM ถ้าเวลาจำกัด
    skip_bt:   bool  = False,   # ข้าม backtest
) -> dict:

    # ✅ FIX: config key ผิด — ใช้ CFG['trading']['symbols'] ตรงกับ config.yaml จริง
    # CFG['symbols']['active'] ไม่มี key นี้ → KeyError crash ทันที
    symbols   = symbols   or CFG.get('trading', {}).get('symbols',
                             CFG.get('symbols', {}).get('active', []))
    timeframe = timeframe or CFG.get('trading', {}).get('timeframe',
                             CFG.get('symbols', {}).get('primary_timeframe', 'M15'))
    results   = {}
    t_start   = time.time()

    log.info("=" * 60)
    log.info(f"Retrain ALL — {datetime.now(timezone.utc).isoformat()}")
    log.info(f"Symbols: {symbols} | TF: {timeframe}")
    log.info("=" * 60)

    # ── Step 1: Backup ────────────────────────────────────────
    log.info("[1/8] Backup models...")
    try:
        from models.manage_models import backup_models
        backup_models(tag="retrain")
        results['backup'] = "ok"
        log.info("  ✅ Backup done")
    except Exception as e:
        log.warning(f"  ⚠️ Backup failed (non-fatal): {e}")
        results['backup'] = "skipped"

    # ── Step 2: Update Data ───────────────────────────────────
    log.info("[2/8] Updating market data...")
    try:
        from data.pipeline import run_full_pipeline
        run_full_pipeline()
        results['data'] = "ok"
        log.info("  ✅ Data updated")
    except Exception as e:
        log.error(f"  ❌ Data update FAILED: {e}", exc_info=True)
        results['data'] = "failed"
        _notify(f"❌ Retrain aborted: data update failed\n{e}")
        return results   # หยุดทันที — ไม่มีข้อมูล train ไม่ได้

    # ── Step 3: Build Features ────────────────────────────────
    log.info("[3/8] Building features...")
    try:
        from features.pipeline import build_all_features
        build_all_features(symbols=symbols, timeframe=timeframe)
        results['features'] = "ok"
        log.info("  ✅ Features built")
    except Exception as e:
        log.error(f"  ❌ Feature build FAILED: {e}", exc_info=True)
        results['features'] = "failed"
        _notify(f"❌ Retrain aborted: feature build failed\n{e}")
        return results

    # ── Step 4: Train XGBoost ─────────────────────────────────
    log.info("[4/8] Training XGBoost...")
    xgb_res = {}
    for sym in symbols:
        try:
            from models.train_xgb import train_xgboost
            r = train_xgboost(sym, timeframe)
            xgb_res[sym] = {
                'acc': round(r.mean_accuracy, 3),
                'f1' : round(r.mean_f1, 3),
                'ok' : r.is_acceptable(),
            }
            icon = "✅" if r.is_acceptable() else "⚠️"
            log.info(f"  {icon} XGB {sym}: {r.summary()}")
        except Exception as e:
            log.error(f"  ❌ XGB {sym}: {e}")
            xgb_res[sym] = {'ok': False, 'error': str(e)[:100]}
    results['xgb'] = xgb_res

    # ── Step 5: Train LightGBM ────────────────────────────────
    log.info("[5/8] Training LightGBM...")
    lgbm_res = {}
    for sym in symbols:
        try:
            from models.train_lgbm import train_lightgbm
            r = train_lightgbm(sym, timeframe)
            lgbm_res[sym] = {
                'acc': round(r.mean_accuracy, 3),
                'f1' : round(r.mean_f1, 3),
                'ok' : r.is_acceptable(),
            }
            icon = "✅" if r.is_acceptable() else "⚠️"
            log.info(f"  {icon} LGBM {sym}: {r.summary()}")
        except Exception as e:
            log.error(f"  ❌ LGBM {sym}: {e}")
            lgbm_res[sym] = {'ok': False, 'error': str(e)[:100]}
    results['lgbm'] = lgbm_res

    # ── Step 6: Train LSTM (optional) ─────────────────────────
    if skip_lstm:
        log.info("[6/8] Skipping LSTM (--skip-lstm)")
        results['lstm'] = "skipped"
    else:
        log.info("[6/8] Training LSTM...")
        lstm_res = {}
        for sym in symbols[:1]:   # ทำแค่ XAUUSD (ช้า)
            try:
                from models.train_lstm import train_lstm
                r = train_lstm(sym, timeframe, epochs=30)
                lstm_res[sym] = {
                    'acc': round(r.mean_accuracy, 3),
                    'f1' : round(r.mean_f1, 3),
                    'ok' : r.is_acceptable(),
                }
                icon = "✅" if r.is_acceptable() else "⚠️"
                log.info(f"  {icon} LSTM {sym}: {r.summary()}")
            except Exception as e:
                log.warning(f"  ⚠️ LSTM {sym}: {e} (non-fatal)")
                lstm_res[sym] = {'ok': False, 'error': str(e)[:100]}
        results['lstm'] = lstm_res

    # ── Step 7: Verify models ─────────────────────────────────
    log.info("[7/8] Verifying models...")
    from models.manage_models import verify_model
    verify_res = {}
    for sym in symbols:
        v = verify_model(sym)
        all_ok = all('OK' in str(m.get('status','')) for m in v.values())
        verify_res[sym] = "ok" if all_ok else "partial"
        icon = "✅" if all_ok else "⚠️"
        log.info(f"  {icon} Verify {sym}: {verify_res[sym]}")
    results['verify'] = verify_res

    # ── Step 8: Backtest + Checklist ──────────────────────────
    if skip_bt:
        log.info("[8/8] Skipping backtest (--skip-bt)")
        results['backtest'] = "skipped"
    else:
        log.info("[8/8] Running backtest + deploy checklist...")
        bt_res = {}
        for sym in symbols:
            try:
                from models.backtest import run_deploy_checklist
                checklist = run_deploy_checklist(sym, timeframe)
                passed    = all(v['pass'] for v in checklist.values())
                bt_res[sym] = {
                    'passed': passed,
                    'grade' : checklist.get('backtest',{}).get('grade','?'),
                }
                icon = "✅" if passed else "⚠️"
                log.info(f"  {icon} Checklist {sym}: "
                         f"{'PASS' if passed else 'PARTIAL'} "
                         f"grade={bt_res[sym]['grade']}")
            except Exception as e:
                log.error(f"  ❌ Backtest {sym}: {e}")
                bt_res[sym] = {'passed': False, 'error': str(e)[:100]}
        results['backtest'] = bt_res

    # ── Summary ───────────────────────────────────────────────
    elapsed = round(time.time() - t_start)
    results['duration_sec'] = elapsed
    results['completed_at'] = datetime.now(timezone.utc).isoformat()

    # บันทึก report
    out = Path("reports/retrain_report.json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    log.info("=" * 60)
    log.info(f"Retrain complete ({elapsed}s) → reports/retrain_report.json")
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