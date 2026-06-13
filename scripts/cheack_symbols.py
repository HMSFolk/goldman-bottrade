# scripts/check_symbols.py — รันก่อนเสมอ
import MetaTrader5 as mt5
from dotenv import load_dotenv
import os, yaml

load_dotenv()
# FIX: เพิ่มโหมด "r" เพื่อความสมบูรณ์ในการเปิดไฟล์
with open("config.yaml", "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

mt5.initialize()
mt5.login(int(os.getenv('MT5_LOGIN')),
          password=os.getenv('MT5_PASSWORD'),
          server=os.getenv('MT5_SERVER'))

# ระบบสืบค้นค่าโครงสร้างอัจฉริยะที่คุณเขียนไว้ (ดีมากอยู่แล้วครับ)
if 'trading' in CFG and isinstance(CFG['trading'], dict) and 'symbols' in CFG['trading']:
    symbols = CFG['trading']['symbols']
elif 'symbols' in CFG:
    symbols = CFG['symbols']
else:
    symbols = ["XAUUSDm"]  # ป้องกันไว้ก่อนถ้าหาไม่เจอจริง ๆ

# บล็อกพิเศษ: ป้องกันในกรณีที่ symbols ดึงออกมาแล้วได้ค่าเป็น Dict (หัวข้อ) ไม่ใช่ List (รายชื่อ)
if isinstance(symbols, dict):
    symbols = list(symbols.keys())

print("Symbol Check:")
for sym in symbols:
    info = mt5.symbol_info(sym)
    if info is None:
        print(f"   ❌ {sym} — ไม่พบ กด Show All ใน MT5 Market Watch")
        continue

    # เปิด symbol
    mt5.symbol_select(sym, True)
    tick = mt5.symbol_info_tick(sym)

    # FIX: บล็อกป้องกันระบบพัง หาก 'tick' ไม่มีข้อมูลเนื่องจากตลาดปิด
    if tick is None:
        print(f"   ⚠️ {sym}")
        print(f"     [WARN] ตลาดปิดทำการ หรือ ดึงข้อมูลราคาเรียลไทม์ไม่ได้ในขณะนี้")
        print(f"     digits={info.digits} lot_min={info.volume_min} lot_step={info.volume_step}")
        print(f"     tick_value={info.trade_tick_value:.4f}")
        continue

    # คำนวณ Spread (จุดนี้จะรันผ่านฉลุยเมื่อตลาดเปิด)
    spread = round((tick.ask - tick.bid) / info.point)
    print(f"   ✅ {sym}")
    print(f"     bid={tick.bid:.5f} ask={tick.ask:.5f}")
    print(f"     spread={spread}pts digits={info.digits}")
    print(f"     lot_min={info.volume_min} lot_step={info.volume_step}")
    print(f"     tick_value={info.trade_tick_value:.4f}")

mt5.shutdown()