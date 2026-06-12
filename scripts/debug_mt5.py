# scripts/debug_mt5.py
"""
Debug ทุกจุดของ MT5 connection
รันแล้วต้องผ่านทุกข้อ
"""
import MetaTrader5 as mt5
import pandas as pd
import os, yaml
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()
with open("config.yaml") as f:
    CFG = yaml.safe_load(f)

def separator(title=""):
    print(f"\n{'─'*50}")
    if title:
        print(f"  {title}")
    print(f"{'─'*50}")

def test_initialize():
    separator("1. MT5 Initialize")
    path = os.getenv('MT5_PATH',
        r"C:\Program Files\MetaTrader 5\terminal64.exe")

    if not os.path.exists(path):
        print(f"  ❌ ไม่พบ MT5: {path}")
        print(f"     แก้ MT5_PATH ใน .env")
        return False

    ok = mt5.initialize(path=path)
    if not ok:
        err = mt5.last_error()
        print(f"  ❌ initialize failed: {err}")
        print(f"     ลอง: เปิด MT5 ด้วยมือก่อน")
        return False

    info = mt5.terminal_info()
    print(f"  ✅ MT5 initialized")
    print(f"     Terminal: {info.community_version}")
    print(f"     Connected: {info.connected}")
    print(f"     Path: {path}")
    return True


def test_login():
    separator("2. Exness Login")
    login    = int(os.getenv('MT5_LOGIN', '0'))
    password = os.getenv('MT5_PASSWORD', '')
    server   = os.getenv('MT5_SERVER', '')

    print(f"     Login:  {login}")
    print(f"     Server: {server}")

    ok = mt5.login(login, password=password, server=server)
    if not ok:
        err = mt5.last_error()
        print(f"  ❌ Login failed: {err}")
        _debug_login_error(err)
        return False

    acc = mt5.account_info()
    is_demo = "Demo" if acc.trade_mode == 0 else "Real"
    print(f"  ✅ Login success")
    print(f"     Name:     {acc.name}")
    print(f"     Login:    {acc.login}")
    print(f"     Balance:  ${acc.balance:,.2f}")
    print(f"     Equity:   ${acc.equity:,.2f}")
    print(f"     Leverage: 1:{acc.leverage}")
    print(f"     Type:     {is_demo}")
    print(f"     Currency: {acc.currency}")

    if acc.trade_mode != 0:
        print(f"  ⚠️  นี่คือ REAL account — ควรใช้ Demo ก่อน!")
    return True


def _debug_login_error(err):
    code = err[0] if err else 0
    tips = {
        5      : "wrong login/password",
        6      : "no connection to server",
        10018  : "market closed",
    }
    tip = tips.get(code, "ตรวจ login, password, server ใน .env")
    print(f"     💡 {tip}")


def test_symbols():
    separator("3. Symbol Availability")
    symbols = CFG['trading']['symbols']
    all_ok  = True

    for sym in symbols:
        info = mt5.symbol_info(sym)
        if info is None:
            print(f"  ❌ {sym} — ไม่พบ")
            print(f"     MT5 → Market Watch → Show All → ค้น {sym}")
            all_ok = False
            continue

        mt5.symbol_select(sym, True)
        tick = mt5.symbol_info_tick(sym)
        spread = round((tick.ask - tick.bid) / info.point)

        print(f"  ✅ {sym}")
        print(f"     bid={tick.bid:.5f}  ask={tick.ask:.5f}")
        print(f"     spread={spread}pts  digits={info.digits}")
        print(f"     volume_min={info.volume_min}  "
              f"volume_step={info.volume_step}")

    return all_ok


def test_ohlcv():
    separator("4. OHLCV Data Fetch")
    all_ok = True
    tfs = {
        "M15": mt5.TIMEFRAME_M15,
        "H1" : mt5.TIMEFRAME_H1,
        "H4" : mt5.TIMEFRAME_H4,
    }

    for sym in CFG['trading']['symbols'][:1]:   # test XAUUSD
        for tf_name, tf in tfs.items():
            rates = mt5.copy_rates_from_pos(sym, tf, 0, 100)
            if rates is None or len(rates) == 0:
                print(f"  ❌ {sym} {tf_name}: no data")
                all_ok = False
                continue

            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
            last = df.iloc[-1]
            print(f"  ✅ {sym} {tf_name}: {len(df)} bars | "
                  f"last={last['time']} "
                  f"close={last['close']:.5f}")

    return all_ok


def test_risk_manager():
    separator("5. Risk Manager")
    from bot.mt5_client   import MT5Client
    from bot.risk_manager import RiskManager

    client = MT5Client()
    risk   = RiskManager()
    acc    = client.get_account()

    # ทดสอบ lot calculation
    for sym in CFG['trading']['symbols']:
        sl_dist = acc['balance'] * 0.001   # dummy sl

        info  = mt5.symbol_info(sym)
        if info is None:
            continue

        lot = risk.calculate_lot_size(
            sym, acc['balance'], sl_dist
        )
        print(f"  ✅ Lot calc {sym}: {lot:.2f} "
              f"(balance=${acc['balance']:,.0f} "
              f"risk={CFG['risk']['risk_per_trade']:.1%})")

    # ทดสอบ spread check
    for sym in CFG['trading']['symbols']:
        result = risk._check_spread(sym)
        icon   = "✅" if result.passed else "⚠️"
        print(f"  {icon} Spread {sym}: {result.value:.0f}pts "
              f"(limit={result.limit:.0f}pts)")

    # ทดสอบ session check
    result = risk._check_session("XAUUSD")
    now    = datetime.now(timezone.utc)
    print(f"  {'✅' if result.passed else '⚠️'} "
          f"Session: {result.check} "
          f"(UTC {now.hour:02d}:xx)")

    return True


def test_send_order_dry_run():
    separator("6. Order Validation (Dry Run — ไม่ส่งจริง)")
    from bot.mt5_client   import MT5Client
    from bot.risk_manager import RiskManager

    client = MT5Client()
    risk   = RiskManager()
    acc    = client.get_account()

    sym = "XAUUSD"
    info = mt5.symbol_info(sym)
    tick = mt5.symbol_info_tick(sym)

    # สร้าง request แต่ไม่ส่ง
    sl_dist = info.point * 150
    tp_dist = sl_dist * 2.0
    lot     = risk.calculate_lot_size(sym, acc['balance'], sl_dist)
    sl, tp  = risk.calculate_sl_tp_price(sym, 1, sl_dist, tp_dist)

    price = tick.ask

    request = {
        "action"   : mt5.TRADE_ACTION_DEAL,
        "symbol"   : sym,
        "volume"   : lot,
        "type"     : mt5.ORDER_TYPE_BUY,
        "price"    : price,
        "sl"       : sl,
        "tp"       : tp,
        "deviation": 10,
        "magic"    : CFG['order']['magic_number'],
        "comment"  : "dry_run_test",
    }

    # ตรวจ request validity (ไม่ส่ง)
    result = mt5.order_check(request)

    if result is None:
        print(f"  ❌ order_check failed: {mt5.last_error()}")
        return False

    print(f"  Order request:")
    print(f"    BUY {sym} lot={lot:.2f}")
    print(f"    price={price:.5f}")
    print(f"    SL={sl:.5f} ({sl_dist/info.point:.0f}pts)")
    print(f"    TP={tp:.5f} (RR 1:{tp_dist/sl_dist:.1f})")

    if result.retcode == 0:
        print(f"  ✅ Order validation passed")
        print(f"     margin_required={result.margin:.2f}")
        print(f"     equity_after={result.equity:.2f}")
    else:
        print(f"  ❌ Validation failed: "
              f"retcode={result.retcode} {result.comment}")
        return False

    return True


def test_send_order_real():
    separator("7. Send REAL Order (Demo) — ยืนยันก่อน")
    print("  ⚠️  จะส่ง order จริงบน Demo account")
    print("  ⚠️  lot=0.01 (minimum)")
    confirm = input("  พิมพ์ YES เพื่อยืนยัน: ")

    if confirm.strip().upper() != "YES":
        print("  ข้ามการทดสอบนี้")
        return True

    from bot.mt5_client   import MT5Client
    from bot.risk_manager import RiskManager
    from bot.executor     import OrderExecutor

    client   = MT5Client()
    risk     = RiskManager()
    executor = OrderExecutor(client, risk)

    # ส่ง order จริง
    result = executor.send_order(
        symbol      = "XAUUSD",
        direction   = 1,        # BUY
        sl_distance = mt5.symbol_info("XAUUSD").point * 150,
        tp_distance = mt5.symbol_info("XAUUSD").point * 300,
        confidence  = 0.99,     # bypass confidence check
        comment     = "debug_test",
    )

    if result.success:
        print(f"  ✅ Order sent: #{result.ticket}")
        print(f"     {result}")

        # รอแล้วปิด
        import time
        print("     รอ 3 วินาทีแล้วปิด...")
        time.sleep(3)
        close = executor.close_order(result.ticket, reason="debug_test")
        print(f"  ✅ Closed: P&L={close.profit:+.2f}")
    else:
        print(f"  ❌ Order failed: {result.error_msg}")
        return False

    return True


def run_all_tests():
    print("\n" + "="*50)
    print("  MT5 DEBUG — Phase 4")
    print("="*50)

    mt5.shutdown()   # clean start

    results = {}

    results['initialize'] = test_initialize()
    if not results['initialize']:
        print("\n❌ ไม่สามารถเชื่อม MT5 ได้ หยุดที่นี่")
        return

    results['login']      = test_login()
    if not results['login']:
        mt5.shutdown()
        return

    results['symbols']    = test_symbols()
    results['ohlcv']      = test_ohlcv()
    results['risk']       = test_risk_manager()
    results['dry_run']    = test_send_order_dry_run()

    # ถามก่อนทดสอบ order จริง
    print("\n" + "="*50)
    do_real = input("  ทดสอบส่ง order จริงบน Demo? (y/n): ")
    if do_real.lower() == 'y':
        results['real_order'] = test_send_order_real()

    mt5.shutdown()

    # สรุป
    print("\n" + "="*50)
    print("  DEBUG SUMMARY")
    print("="*50)
    all_pass = True
    for name, passed in results.items():
        icon = "✅" if passed else "❌"
        print(f"  {icon} {name}")
        if not passed:
            all_pass = False

    print("\n" + "="*50)
    if all_pass:
        print("🚀 Phase 4 PASSED — พร้อมไป Phase 5 (Paper Trading)")
    else:
        failed = [k for k,v in results.items() if not v]
        print(f"⚠️  ยังไม่ผ่าน: {failed}")
    print("="*50)


if __name__ == "__main__":
    run_all_tests()