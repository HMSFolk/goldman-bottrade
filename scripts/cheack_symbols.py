<<<<<<< HEAD
# scripts/check_symbols.py — รันก่อนเสมอ
import MetaTrader5 as mt5
from dotenv import load_dotenv
import os, yaml

load_dotenv()
with open("config.yaml") as f:
    CFG = yaml.safe_load(f)

mt5.initialize()
mt5.login(int(os.getenv('MT5_LOGIN')),
          password=os.getenv('MT5_PASSWORD'),
          server=os.getenv('MT5_SERVER'))

symbols = CFG['trading']['symbols']
print("Symbol Check:")
for sym in symbols:
    info = mt5.symbol_info(sym)
    if info is None:
        print(f"  ❌ {sym} — ไม่พบ กด Show All ใน MT5 Market Watch")
        continue

    # เปิด symbol
    mt5.symbol_select(sym, True)
    tick = mt5.symbol_info_tick(sym)

    spread = round((tick.ask - tick.bid) / info.point)
    print(f"  ✅ {sym}")
    print(f"     bid={tick.bid:.5f} ask={tick.ask:.5f}")
    print(f"     spread={spread}pts digits={info.digits}")
    print(f"     lot_min={info.volume_min} lot_step={info.volume_step}")
    print(f"     tick_value={info.trade_tick_value:.4f}")

=======
# scripts/check_symbols.py — รันก่อนเสมอ
import MetaTrader5 as mt5
from dotenv import load_dotenv
import os, yaml

load_dotenv()
with open("config.yaml") as f:
    CFG = yaml.safe_load(f)

mt5.initialize()
mt5.login(int(os.getenv('MT5_LOGIN')),
          password=os.getenv('MT5_PASSWORD'),
          server=os.getenv('MT5_SERVER'))

symbols = CFG['trading']['symbols']
print("Symbol Check:")
for sym in symbols:
    info = mt5.symbol_info(sym)
    if info is None:
        print(f"  ❌ {sym} — ไม่พบ กด Show All ใน MT5 Market Watch")
        continue

    # เปิด symbol
    mt5.symbol_select(sym, True)
    tick = mt5.symbol_info_tick(sym)

    spread = round((tick.ask - tick.bid) / info.point)
    print(f"  ✅ {sym}")
    print(f"     bid={tick.bid:.5f} ask={tick.ask:.5f}")
    print(f"     spread={spread}pts digits={info.digits}")
    print(f"     lot_min={info.volume_min} lot_step={info.volume_step}")
    print(f"     tick_value={info.trade_tick_value:.4f}")

>>>>>>> 98ac82b18ee8d71be376450f278ba77dde0c1c3e
mt5.shutdown()