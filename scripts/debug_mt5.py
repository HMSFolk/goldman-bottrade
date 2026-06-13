# scripts/debug_mt5.py
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timezone
from config import get_config  # ✅ ใช้คอนฟิกวงจรหลัก

CFG = get_config()

def test_login():
    print(f"\n{'─'*50}\n  2. Exness Login\n{'─'*50}")
    
    # ดึงค่าล็อกอินจากก้อน Config หลักอย่างปลอดภัย
    ok = mt5.login(
        int(CFG['mt5']['login']), 
        password=CFG['mt5']['password'], 
        server=CFG['mt5']['server']
    )
    
    if not ok:
        print(f"  ❌ Login failed: {mt5.last_error()}")
        return False
        
    # ดึงข้อมูลบัญชี
    acc = mt5.account_info()
    
    # ✅ FIX: บล็อกป้องกันระบบพัง (Crash Guard) กรณีเซิร์ฟเวอร์ไม่ส่งค่าบัญชีกลับมา
    if acc is None:
        print("  ❌ Login สำเร็จ แต่ไม่สามารถดึงข้อมูล Account Info ได้ (เซิร์ฟเวอร์โบรกเกอร์อาจปิดปรับปรุงชั่วคราว)")
        return False
        
    # แสดงผลลัพธ์พอร์ตลงทุนอย่างปลอดภัยเมื่อตรวจเช็กแล้วว่า acc มีตัวตนอยู่จริง
    trade_type = 'Demo' if acc.trade_mode == 0 else 'REAL'
    print(f"  ✅ Login success | {acc.name} | Balance: ${acc.balance:,.2f} | Type: {trade_type}")
    return True

def run_all_tests():
    print("\n" + "="*50 + "\n  MT5 DEBUG \n" + "="*50)
    mt5.shutdown()
    
    # สั่งเปิด Terminal ตามตำแหน่งพาร์ทที่ตั้งค่าไว้
    init_ok = mt5.initialize(path=CFG['mt5'].get('terminal_path'))
    if not init_ok:
        print(f"  ❌ ไม่สามารถ Initialize MT5 ได้: {mt5.last_error()}")
        return
        
    test_login()
    mt5.shutdown()

if __name__ == "__main__":
    run_all_tests()