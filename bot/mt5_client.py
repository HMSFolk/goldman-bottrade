# bot/mt5_client.py
"""
MT5 Client — Connection Manager
════════════════════════════════════════════════════════════
Singleton pattern: สร้างครั้งเดียว ใช้ทั้งโปรแกรม

ความรับผิดชอบ:
  - connect / disconnect / reconnect อัตโนมัติ
  - get_ohlcv: ดึงราคาทุก symbol + timeframe
  - get_tick: ราคา bid/ask ล่าสุด
  - get_account: balance, equity, margin
  - get_positions: open positions ทั้งหมด
  - get_history: ประวัติ trade
  - symbol_info: spec ของแต่ละ symbol
"""

import logging
import time
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("bot.mt5_client")

# ── Timeframe Mapping ──────────────────────────────────────────
TF_MAP = {
    "M1" : mt5.TIMEFRAME_M1,
    "M5" : mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1" : mt5.TIMEFRAME_H1,
    "H4" : mt5.TIMEFRAME_H4,
    "D1" : mt5.TIMEFRAME_D1,
    "W1" : mt5.TIMEFRAME_W1,
}


# ══════════════════════════════════════════════════════════════
# MT5 Client — Singleton
# ══════════════════════════════════════════════════════════════
class MT5Client:
    """
    Singleton MT5 Client
    สร้างครั้งเดียว ใช้ทั้งโปรแกรม

    การใช้งาน:
        client = MT5Client()
        client.connect()
        df = client.get_ohlcv("XAUUSD", "M15", n_bars=200)
    """

    _instance    = None
    _initialized = False

    def __new__(cls):
        """Singleton — คืน instance เดิมถ้ามีอยู่แล้ว"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return   # ป้องกัน __init__ รันซ้ำ

        self._connected     = False
        self._login         = int(os.getenv("MT5_LOGIN", "0"))
        self._password      = os.getenv("MT5_PASSWORD", "")
        self._server        = os.getenv("MT5_SERVER",   "")
        self._terminal_path = os.getenv(
            "MT5_PATH",
            r"C:\Program Files\MetaTrader 5\terminal64.exe"
        )

        # ✅ FIX BUG-11: อ่าน retry values จาก config แทน hardcode
        from config import get_config as _cfg
        _c = _cfg()
        self._max_retries   = _c.get('mt5', {}).get('max_reconnect_attempts', 5)
        self._retry_delay   = _c.get('mt5', {}).get('reconnect_delay_sec', 10)
        self._ping_interval = _c.get('mt5', {}).get('timeout_sec', 60)
        self._last_ping     = 0

        # Cache symbol info (ไม่ต้อง query ทุกครั้ง)
        self._symbol_cache: dict = {}

        self._initialized = True
        log.info(
            f"MT5Client init: "
            f"login={self._login} "
            f"server={self._server}"
        )

    # ══════════════════════════════════════════════════════════
    # Connection Management
    # ══════════════════════════════════════════════════════════
    def connect(self) -> bool:
        """
        เชื่อมต่อ MT5 และ Login Exness
        คืน True ถ้าสำเร็จ
        """
        if self._connected and self._ping():
            return True   # connected อยู่แล้ว

        log.info(f"Connecting to MT5: {self._server}...")

        # ── Initialize terminal ───────────────────────────────
        init_ok = mt5.initialize(
            path     = self._terminal_path,
            login    = self._login,
            password = self._password,
            server   = self._server,
            timeout  = 30_000,   # ms
        )

        if not init_ok:
            err = mt5.last_error()
            log.error(f"MT5 initialize failed: {err}")
            return False

        # ── Login ─────────────────────────────────────────────
        if not mt5.login(
            self._login,
            password = self._password,
            server   = self._server,
        ):
            err = mt5.last_error()
            log.error(f"MT5 login failed: {err}")
            mt5.shutdown()
            return False

        # ── Verify ────────────────────────────────────────────
        acc = mt5.account_info()
        if acc is None:
            log.error("Cannot get account info after login")
            mt5.shutdown()
            return False

        self._connected  = True
        self._last_ping  = time.time()

        log.info(
            f"✅ MT5 Connected:\n"
            f"   Name:     {acc.name}\n"
            f"   Login:    {acc.login}\n"
            f"   Server:   {acc.server}\n"
            f"   Balance:  ${acc.balance:,.2f}\n"
            f"   Leverage: 1:{acc.leverage}\n"
            f"   Currency: {acc.currency}"
        )

        # Activate symbols
        self._activate_symbols()

        return True

    def disconnect(self):
        """ตัด connection MT5 อย่างสะอาด"""
        if self._connected:
            mt5.shutdown()
            self._connected = False
            log.info("MT5 disconnected")

    def ensure_connected(self) -> bool:
        """
        ตรวจ connection — reconnect อัตโนมัติถ้าหลุด
        เรียกก่อนทุก operation ที่สำคัญ
        """
        # Ping ถ้าถึงเวลา
        now = time.time()
        if now - self._last_ping >= self._ping_interval:
            if not self._ping():
                log.warning("Ping failed — reconnecting...")
                self._connected = False

        if self._connected:
            return True

        # Reconnect
        return self._reconnect()

    def _ping(self) -> bool:
        """
        ตรวจว่า connection ยังอยู่
        ใช้ terminal_info() เพราะเบากว่า account_info()
        """
        try:
            info = mt5.terminal_info()
            if info is None:
                return False

            self._last_ping = time.time()
            return True

        except Exception:
            return False

    def _reconnect(self) -> bool:
        """
        Reconnect พร้อม exponential backoff
        พยายาม max_retries ครั้งก่อน raise error
        """
        from bot.notifier import notify

        for attempt in range(1, self._max_retries + 1):
            delay = self._retry_delay * attempt   # 10, 20, 30, 40, 50 วินาที
            log.warning(
                f"Reconnect attempt {attempt}/{self._max_retries} "
                f"(waiting {delay}s)..."
            )
            time.sleep(delay)

            mt5.shutdown()   # cleanup ก่อน

            if self.connect():
                log.info(
                    f"✅ Reconnected after {attempt} attempt(s)"
                )
                notify(
                    f"🔄 MT5 Reconnected "
                    f"(attempt {attempt})"
                )
                return True

        log.critical(
            f"❌ Cannot reconnect after {self._max_retries} attempts"
        )
        notify(
            f"🚨 *MT5 Connection Lost*\n"
            f"Cannot reconnect after {self._max_retries} attempts\n"
            f"Bot stopping..."
        )
        return False

    def _activate_symbols(self):
        """
        เปิด symbol ใน Market Watch
        MT5 บางตัวต้อง activate ก่อนใช้งาน
        ✅ FIX BUG-9: ใช้ get_config() แทน open(config.yaml) โดยตรง
        """
        from config import get_config
        cfg     = get_config()
        symbols = cfg['symbols']['active']
        for sym in symbols:
            if not mt5.symbol_select(sym, True):
                log.warning(
                    f"Cannot activate symbol: {sym}"
                )
            else:
                log.debug(f"Symbol activated: {sym}")

    # ══════════════════════════════════════════════════════════
    # Market Data
    # ══════════════════════════════════════════════════════════
    def get_ohlcv(
        self,
        symbol:    str,
        timeframe: str,
        n_bars:    int = 500,
    ) -> pd.DataFrame:
        """
        ดึง OHLCV bars จาก MT5
        คืน DataFrame พร้อมใช้งาน

        columns: open, high, low, close, tick_volume,
                 spread, real_volume
        index:   datetime (UTC)
        """
        self.ensure_connected()

        tf = TF_MAP.get(timeframe)
        if tf is None:
            raise ValueError(
                f"Timeframe ไม่รู้จัก: {timeframe} "
                f"(รองรับ: {list(TF_MAP.keys())})"
            )

        rates = mt5.copy_rates_from_pos(symbol, tf, 0, n_bars)

        if rates is None or len(rates) == 0:
            err = mt5.last_error()
            raise ValueError(
                f"ไม่มีข้อมูล {symbol} {timeframe}: {err}"
            )

        df = pd.DataFrame(rates)

        # แปลง timestamp เป็น datetime UTC
        df['time'] = pd.to_datetime(
            df['time'], unit='s', utc=True
        )
        df.set_index('time', inplace=True)
        df.index.name = 'datetime'

        # เรียงตามเวลา (ควรเป็นอยู่แล้ว แต่ยืนยันให้ชัด)
        df.sort_index(inplace=True)

        # ลบ rows ที่ close = 0 (ข้อมูลเสีย)
        df = df[df['close'] > 0]

        log.debug(
            f"get_ohlcv {symbol} {timeframe}: "
            f"{len(df)} bars "
            f"({df.index[0]} → {df.index[-1]})"
        )

        return df

    def get_ohlcv_range(
        self,
        symbol:    str,
        timeframe: str,
        date_from: datetime,
        date_to:   datetime,
    ) -> pd.DataFrame:
        """
        ดึงข้อมูลตาม date range
        ใช้สำหรับ collect historical data
        """
        self.ensure_connected()

        tf = TF_MAP.get(timeframe)
        # ✅ FIX BUG-10: ตรวจ tf=None ก่อนเรียก MT5
        if tf is None:
            raise ValueError(
                f"Timeframe ไม่รู้จัก: {timeframe} "
                f"(รองรับ: {list(TF_MAP.keys())})"
            )
        rates = mt5.copy_rates_range(
            symbol, tf, date_from, date_to
        )

        if rates is None or len(rates) == 0:
            return pd.DataFrame()

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
        df.set_index('time', inplace=True)
        df.sort_index(inplace=True)

        return df

    def get_tick(self, symbol: str) -> dict:
        """
        ราคา bid/ask ล่าสุด (real-time)
        ใช้คำนวณ entry price ก่อนส่ง order
        """
        self.ensure_connected()

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise ValueError(
                f"ไม่มี tick {symbol}: {mt5.last_error()}"
            )

        return {
            'bid'   : tick.bid,
            'ask'   : tick.ask,
            'last'  : tick.last,
            'spread': round(tick.ask - tick.bid, 5),
            'time'  : datetime.fromtimestamp(
                tick.time, tz=timezone.utc
            ),
        }

    # ══════════════════════════════════════════════════════════
    # Account Information
    # ══════════════════════════════════════════════════════════
    def get_account(self) -> dict:
        """
        ข้อมูล account ปัจจุบัน
        เรียกบ่อยได้ — เบามาก
        """
        self.ensure_connected()

        acc = mt5.account_info()
        if acc is None:
            raise RuntimeError(
                f"Cannot get account: {mt5.last_error()}"
            )

        return {
            'login'        : acc.login,
            'name'         : acc.name,
            'server'       : acc.server,
            'balance'      : acc.balance,
            'equity'       : acc.equity,
            'margin'       : acc.margin,
            'free_margin'  : acc.margin_free,
            'margin_level' : acc.margin_level,
            'profit'       : acc.profit,
            'leverage'     : acc.leverage,
            'currency'     : acc.currency,
            'open_trades'  : len(mt5.positions_get() or []),
        }

    # ══════════════════════════════════════════════════════════
    # Position Management
    # ══════════════════════════════════════════════════════════
    def get_positions(
        self, symbol: str = None
    ) -> list[dict]:
        """
        ดู open positions ทั้งหมด
        symbol=None → ทุก position
        symbol="XAUUSD" → เฉพาะ XAUUSD
        """
        self.ensure_connected()

        if symbol:
            raw = mt5.positions_get(symbol=symbol)
        else:
            raw = mt5.positions_get()

        if raw is None:
            return []

        positions = []
        for pos in raw:
            positions.append({
                'ticket'    : pos.ticket,
                'symbol'    : pos.symbol,
                'type'      : 'BUY' if pos.type == 0 else 'SELL',
                'volume'    : pos.volume,
                'open_price': pos.price_open,
                'sl'        : pos.sl,
                'tp'        : pos.tp,
                'profit'    : pos.profit,
                'swap'      : pos.swap,
                'magic'     : pos.magic,
                'comment'   : pos.comment,
                'open_time' : datetime.fromtimestamp(
                    pos.time, tz=timezone.utc
                ),
            })

        return positions

    def get_open_volume(self, symbol: str) -> float:
        """
        คำนวณ total volume ที่เปิดอยู่ใน symbol นี้
        ใช้ตรวจ max position size
        """
        positions = self.get_positions(symbol)
        if not positions:
            return 0.0

        total = sum(p['volume'] for p in positions)
        return round(total, 2)

    def has_position(self, symbol: str) -> bool:
        """มี open position ใน symbol นี้ไหม"""
        return len(self.get_positions(symbol)) > 0

    # ══════════════════════════════════════════════════════════
    # Trade History
    # ══════════════════════════════════════════════════════════
    def get_history(
        self,
        days_back:  int = 30,
        symbol:     str = None,
    ) -> pd.DataFrame:
        """
        ดึงประวัติ trade จาก MT5
        ใช้สำหรับ dashboard และ performance analysis
        """
        self.ensure_connected()

        date_to   = datetime.now(timezone.utc)
        date_from = date_to - timedelta(days=days_back)

        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None or len(deals) == 0:
            return pd.DataFrame()

        df = pd.DataFrame(list(deals), columns=deals[0]._asdict().keys())

        # แปลง timestamp
        df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)

        # กรองเฉพาะ trade จริง (ไม่รวม deposit/withdrawal)
        df = df[df['type'].isin([0, 1])]   # 0=BUY 1=SELL

        # กรอง symbol
        if symbol:
            df = df[df['symbol'] == symbol]

        # กรองเฉพาะ magic number ของบอทนี้
        # ✅ FIX BUG-9: ใช้ get_config() แทน open(config.yaml) โดยตรง
        from config import get_config
        cfg   = get_config()
        magic = cfg['order']['magic_number']
        if 'magic' in df.columns:
            df = df[df['magic'] == magic]

        df.set_index('time', inplace=True)
        return df

    def get_daily_pnl(self) -> float:
        """PnL รวมวันนี้"""
        today    = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        deals    = mt5.history_deals_get(today, datetime.now(timezone.utc))
        if deals is None:
            return 0.0
        return sum(d.profit for d in deals if d.type in [0,1])

    # ══════════════════════════════════════════════════════════
    # Symbol Information
    # ══════════════════════════════════════════════════════════
    def get_symbol_info(self, symbol: str) -> dict:
        """
        ดู specification ของ symbol
        Cache ไว้ไม่ต้อง query ทุกครั้ง

        ใช้ใน RiskManager สำหรับคำนวณ lot size
        """
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]

        self.ensure_connected()

        info = mt5.symbol_info(symbol)
        if info is None:
            raise ValueError(f"Symbol ไม่พบ: {symbol}")

        spec = {
            'symbol'        : symbol,
            'digits'        : info.digits,
            'point'         : info.point,
            'trade_contract_size': info.trade_contract_size,
            'volume_min'    : info.volume_min,
            'volume_max'    : info.volume_max,
            'volume_step'   : info.volume_step,
            'tick_size'     : info.trade_tick_size,
            'tick_value'    : info.trade_tick_value,
            'currency_profit': info.currency_profit,
            'currency_base' : info.currency_base,
            'spread_current': info.spread,
            'swap_long'     : info.swap_long,
            'swap_short'    : info.swap_short,
        }

        self._symbol_cache[symbol] = spec
        log.debug(
            f"Symbol spec {symbol}: "
            f"digits={spec['digits']} "
            f"point={spec['point']} "
            f"tick_value={spec['tick_value']}"
        )
        return spec

    def get_spread_points(self, symbol: str) -> int:
        """Spread ปัจจุบันเป็น points"""
        self.ensure_connected()

        tick  = mt5.symbol_info_tick(symbol)
        info  = mt5.symbol_info(symbol)
        if tick is None or info is None:
            return 999   # ค่า safe สูงๆ ถ้า error

        spread = round(
            (tick.ask - tick.bid) / info.point
        )
        return spread

    def get_atr_points(
        self, symbol: str, period: int = 14
    ) -> float:
        """
        ATR เป็น points (ใช้คำนวณ SL/TP ใน executor)
        ดึงจาก bar data โดยตรง
        """
        df    = self.get_ohlcv(symbol, "M15", n_bars=period+10)
        tr    = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift(1)).abs(),
            (df['low']  - df['close'].shift(1)).abs(),
        ], axis=1).max(axis=1)

        atr      = tr.ewm(span=period, adjust=False).mean().iloc[-1]
        info     = self.get_symbol_info(symbol)
        atr_pts  = atr / info['point']
        return round(atr_pts, 1)

    # ══════════════════════════════════════════════════════════
    # Diagnostic
    # ══════════════════════════════════════════════════════════
    def status(self) -> dict:
        """
        สถานะ connection ทั้งหมด
        ใช้ใน health check และ dashboard
        """
        try:
            terminal = mt5.terminal_info()
            account  = mt5.account_info()

            return {
                'connected'        : self._connected,
                'ping_ok'          : terminal is not None,
                'last_ping'        : datetime.fromtimestamp(
                    self._last_ping, tz=timezone.utc
                ).isoformat() if self._last_ping else None,
                'terminal_version' : (
                    terminal.build        # ✅ FIX BUG-8: .community_version ไม่มีใน MT5 API — ใช้ .build แทน
                    if terminal else None
                ),
                'account_login'    : (
                    account.login if account else None
                ),
                'account_balance'  : (
                    account.balance if account else None
                ),
            }
        except Exception as e:
            return {
                'connected': False,
                'error'    : str(e),
            }

    def __repr__(self) -> str:
        return (
            f"MT5Client("
            f"login={self._login} "
            f"server={self._server} "
            f"connected={self._connected})"
        )