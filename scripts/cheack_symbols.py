# scripts/check_symbols.py — รันก่อนเสมอ
# ✅ FIX (2026-06-29):
#   - เดิม symbols = CFG['symbols'] (dict) → list(keys) ได้ ['active',
#     'primary_timeframe','htf_timeframes'] ไม่ใช่ชื่อ symbol → เช็คผิดหมด
#   - เดิมไม่ resolve ชื่อกลาง→ชื่อ broker (XAUUSD→XAUUSDm) → symbol_info ไม่เจอ
#   - เปลี่ยนมาใช้ get_config() + resolve_symbol แทน yaml.safe_load ดิบ
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import MetaTrader5 as mt5
from config import get_config, resolve_symbol

CFG = get_config()

# ── เชื่อมต่อ MT5 ด้วย credentials จาก config (merge .env แล้ว) ──
mt5.initialize(path=CFG['mt5'].get('terminal_path'))
mt5.login(
    int(CFG['mt5']['login']),
    password=CFG['mt5']['password'],
    server=CFG['mt5']['server'],
)

# ✅ ใช้รายชื่อ canonical จาก symbols.active เสมอ (XAUUSD/EURUSD/GBPUSD)
canonical_symbols = CFG['symbols']['active']

print("Symbol Check:")
for canon in canonical_symbols:
    # ✅ resolve ชื่อกลาง → ชื่อจริงของ broker ก่อนถาม MT5
    sym  = resolve_symbol(canon, CFG)
    info = mt5.symbol_info(sym)
    if info is None:
        print(f"   ❌ {sym} (canonical={canon}) — ไม่พบ กด Show All ใน MT5 Market Watch")
        continue

    # เปิด symbol
    mt5.symbol_select(sym, True)
    tick = mt5.symbol_info_tick(sym)

    # บล็อกป้องกันระบบพัง หาก tick ไม่มีข้อมูล (ตลาดปิด)
    if tick is None:
        print(f"   ⚠️ {sym} (canonical={canon})")
        print(f"     [WARN] ตลาดปิดทำการ หรือดึงราคาเรียลไทม์ไม่ได้ขณะนี้")
        print(f"     digits={info.digits} lot_min={info.volume_min} lot_step={info.volume_step}")
        print(f"     tick_value={info.trade_tick_value:.4f}")
        continue

    spread = round((tick.ask - tick.bid) / info.point)
    print(f"   ✅ {sym} (canonical={canon})")
    print(f"     bid={tick.bid:.5f} ask={tick.ask:.5f}")
    print(f"     spread={spread}pts digits={info.digits}")
    print(f"     lot_min={info.volume_min} lot_step={info.volume_step}")
    print(f"     tick_value={info.trade_tick_value:.4f}")

mt5.shutdown()