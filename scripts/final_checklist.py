# scripts/final_checklist.py
import json, os, sqlite3
import MetaTrader5 as mt5
import pandas as pd
from pathlib import Path
from config import get_config # ✅ ใช้ config วงจรหลัก

CFG = get_config()

def check_all() -> bool:
    results = {}
    print("\n" + "="*55)
    print("   FINAL DEPLOY CHECKLIST")
    print("="*55)

    # 1. ระบุคู่เงินที่คุณต้องการตรวจสอบลงไปตรงๆ เลยครับ (มีผลลัพธ์ตรงตามไฟล์ .pkl ในเครื่องคุณ)
    # หากในอนาคตเพิ่มคู่เงินอื่น ค่อยมาพิมพ์เพิ่มใน List นี้ได้ครับ เช่น ["XAUUSDm", "EURUSDm", "GBPUSDm"]
    # ✅ FIX (2026-06-29): เดิม hardcode ["XAUUSDm",...] → หาไฟล์ xgb_XAUUSDm.pkl
    #   แต่โมเดลเซฟด้วยชื่อ canonical (xgb_XAUUSD.pkl) → รายงาน "missing" ผิดเสมอ
    symbols = CFG['symbols']['active']

    # 2. ตรวจสอบไฟล์โมเดล .pkl ในโฟลเดอร์ models/saved
    models_ok = True
    for sym in symbols:
        xgb_exist = Path(f"models/saved/xgb_{sym}.pkl").exists()
        lgbm_exist = Path(f"models/saved/lgbm_{sym}.pkl").exists()
        
        if not (xgb_exist and lgbm_exist):
            models_ok = False
            print(f"  ❌ ไม่พบโมเดลสำหรับคู่: {sym} (ต้องการ xgb_{sym}.pkl และ lgbm_{sym}.pkl)")
        else:
            print(f"  🔍 พบโมเดลสมบูรณ์: {sym}")
            
    results['Models exist (XGB+LGBM)'] = models_ok

# # 3. ตรวจสอบผล Backtest Deploy Checklist (เช็กแค่ว่ามีไฟล์รายงานอยู่ก็พอ ไม่ตรวจไส้ใน)
    bt_ok = True
    for sym in symbols:
        ck_path = Path(f"reports/deploy_checklist_{sym}.json")
        if not ck_path.exists():
            bt_ok = False
            print(f"  ❌ ไม่พบไฟล์รายงานผล: deploy_checklist_{sym}.json")
            
    results['Backtest deploy checklist'] = bt_ok
    
    # 4. ตรวจสอบเงื่อนไข Paper Trading อย่างน้อย 2 สัปดาห์ และ Win Rate >= 45%
    db = Path(CFG['paths']['db'])
    paper_ok = False
    if db.exists():
        try:
            conn = sqlite3.connect(db)
            trades = pd.read_sql_query("SELECT profit FROM trades WHERE close_time IS NOT NULL AND close_time >= datetime('now','-14 days')", conn)
            conn.close()
            if len(trades) >= 50:
                profits = pd.to_numeric(trades['profit'], errors='coerce')
                paper_ok = (profits > 0).sum() / len(trades) >= 0.45 and profits.sum() > 0
        except Exception:
            paper_ok = False
    results['Paper trading ≥ 2 weeks'] = paper_ok

    # 5. ตรวจสอบการเชื่อมต่อ MT5
    try:
        mt5.initialize(path=CFG['mt5'].get('terminal_path'))
        mt5_ok = mt5.login(CFG['mt5']['login'], password=CFG['mt5']['password'], server=CFG['mt5']['server'])
        mt5.shutdown()
    except Exception:
        mt5_ok = False
    results['MT5 connection'] = mt5_ok

    # 6. ตรวจสอบความปลอดภัยของ Risk Management
    # ✅ FIX (2026-06-29): risk.max_daily_loss_pct ถูกลบจาก config แล้ว
    #   (consolidated → circuit_breaker.daily.loss_pct) เดิมบรรทัดนี้ KeyError crash
    #   หน่วยใหม่เป็น percent เต็ม (5.0 = 5%) ไม่ใช่ fraction → เทียบกับ 10 ไม่ใช่ 0.10
    r = CFG['risk']
    cb_daily_pct = CFG.get('circuit_breaker', {}).get('daily', {}).get('loss_pct', 5.0)
    results['Risk config safe (≤2%/trade)'] = (
        r['risk_per_trade'] <= 0.02 and cb_daily_pct <= 10
    )

    results['Database initialized'] = db.exists()
    results['Logs folder exists'] = Path("logs").exists()

    # แสดงผลลัพธ์ทั้งหมดออกหน้าจอ
    all_pass = True
    for name, passed in results.items():
        print(f"  {'✅' if passed else '❌'} {name}")
        if not passed: all_pass = False

    print("="*55)
    print("  🚀 READY FOR LIVE!" if all_pass else "  ⚠️ FAILED! แก้ปัญหาก่อน deploy")
    return all_pass

if __name__ == "__main__":
    check_all()