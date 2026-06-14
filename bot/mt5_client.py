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

── Auto-Reconnect Layer (added) ──────────────────────────
  - health_check()        : ตรวจ 3 ชั้น (terminal / server / account)
  - reconnect()           : exponential backoff จาก config
  - ensure_connected()    : throttled check → reconnect ถ้าจำเป็น
  - get_connection_stats(): stats สำหรับ /status และ dashboard
"""

import logging
import time
import os
import threading                            # [1] NEW
from dataclasses import dataclass           # [1] NEW
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
# [2] NEW — ConnectionStats dataclass (เพิ่มก่อน class MT5Client)
# ══════════════════════════════════════════════════════════════
@dataclass
class ConnectionStats:
    """สถิติ connection ของ MT5"""
    is_connected       : bool  = False
    total_disconnects  : int   = 0     # จำนวนครั้งที่หลุด
    total_reconnects   : int   = 0     # จำนวนครั้งที่ reconnect สำเร็จ
    failed_reconnects  : int   = 0     # ครั้งที่ reconnect ไม่สำเร็จ
    last_connected_at  : str   = ""    # ISO UTC
    last_disconnect_at : str   = ""    # ISO UTC
    last_reconnect_at  : str   = ""    # ISO UTC
    uptime_seconds     : float = 0.0   # วินาทีที่ connect ต่อเนื่องล่าสุด

    @property
    def reconnect_success_rate(self) -> float:
        total = self.total_reconnects + self.failed_reconnects
        return (self.total_reconnects / total * 100) if total > 0 else 100.0


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

        # [3] NEW — Reconnect state ────────────────────────────
        self._conn_stats    = ConnectionStats()
        self._conn_lock     = threading.Lock()   # thread-safe reconnect
        self._connected_at  = None               # datetime ที่ connect สำเร็จล่าสุด
        self._hc_last_check = None               # datetime ที่ health_check ครั้งล่าสุด

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
        เชื่อมต่อ MT5 และ Login
        คืน True ถ้าสำเร็จ
        เรียกครั้งแรกตอน startup เท่านั้น
        """
        if self._connected and self._conn_stats.is_connected:
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

        # ── Update state (ทั้ง legacy และ ConnectionStats) ────
        self._connected                     = True
        self._last_ping                     = time.time()
        self._connected_at                  = datetime.now(timezone.utc)
        self._conn_stats.is_connected       = True
        self._conn_stats.last_connected_at  = self._connected_at.isoformat()

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
            self._connected               = False
            self._conn_stats.is_connected = False
            log.info("MT5 disconnected")

    # ══════════════════════════════════════════════════════════
    # [4] NEW METHODS — Auto-Reconnect Layer
    # ══════════════════════════════════════════════════════════

    def health_check(self, symbol: str = "XAUUSDm") -> bool:
        """
        ตรวจว่า MT5 ยังเชื่อมต่ออยู่หรือไม่ (เร็ว < 100ms)

        ตรวจ 3 ชั้น:
          1. terminal_info() — MT5 app ยังเปิดอยู่ไหม
          2. terminal.connected — server connection ยังอยู่ไหม
          3. account_info() — login session ยังใช้งานได้ไหม

        Returns:
            True = ทุกอย่าง OK, False = มีปัญหา → ควร reconnect
        """
        try:
            # ชั้น 1: terminal process
            info = mt5.terminal_info()
            if info is None:
                log.debug("health_check: terminal_info() is None")
                self._mark_disconnected()
                return False

            # ชั้น 2: server connection flag
            if not info.connected:
                log.debug("health_check: terminal not connected to server")
                self._mark_disconnected()
                return False

            # ชั้น 3: active account session
            acc = mt5.account_info()
            if acc is None:
                log.debug("health_check: account_info() is None")
                self._mark_disconnected()
                return False

            # ทุกอย่าง OK — อัปเดต uptime
            if self._connected_at:
                self._conn_stats.uptime_seconds = (
                    datetime.now(timezone.utc) - self._connected_at
                ).total_seconds()

            self._conn_stats.is_connected = True
            self._connected               = True
            self._hc_last_check           = datetime.now(timezone.utc)
            return True

        except Exception as e:
            log.warning(f"health_check error: {e}")
            self._mark_disconnected()
            return False

    def reconnect(self) -> bool:
        """
        พยายาม reconnect กลับไปยัง MT5

        Strategy: exponential backoff (ค่าจาก config.yaml → mt5.reconnect)
          retry 1: sleep 5s
          retry 2: sleep 10s
          retry 3: sleep 20s   ← ส่ง Telegram alert ที่ครั้งนี้
          retry N: sleep min(5 × 2^(N-1), 300s)

        Returns:
            True = reconnect สำเร็จ
            False = หมด retry ทั้งหมด → Bot ควร pause
        """
        from config import get_config
        cfg_rc   = get_config().get("mt5", {}).get("reconnect", {})
        max_try  = int(cfg_rc.get("max_retries",           10))
        base_s   = float(cfg_rc.get("base_sleep_sec",       5))
        max_s    = float(cfg_rc.get("max_sleep_sec",      300))
        alert_at = int(cfg_rc.get("alert_after_retries",    3))

        with self._conn_lock:
            log.warning(
                f"MT5 disconnected — starting reconnect "
                f"(max {max_try} attempts)"
            )
            self._mark_disconnected()

            for attempt in range(1, max_try + 1):
                sleep_s = min(base_s * (2 ** (attempt - 1)), max_s)
                log.info(
                    f"  Reconnect [{attempt}/{max_try}] "
                    f"— waiting {sleep_s:.0f}s..."
                )
                time.sleep(sleep_s)

                if self._try_connect():
                    # ── สำเร็จ ──────────────────────────────
                    self._conn_stats.total_reconnects += 1
                    self._conn_stats.last_reconnect_at = (
                        datetime.now(timezone.utc).isoformat()
                    )
                    now_str = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M UTC"
                    )
                    log.info(
                        f"✅ MT5 reconnected (attempt {attempt}) "
                        f"@ {now_str}"
                    )
                    self._notify(
                        f"✅ *MT5 Reconnected*\n"
                        f"Attempt: {attempt}/{max_try}\n"
                        f"Time: `{now_str}`"
                    )
                    return True

                # ── ล้มเหลว ──────────────────────────────────
                log.warning(f"  Attempt {attempt} failed")
                self._conn_stats.failed_reconnects += 1

                if attempt == alert_at:
                    self._notify(
                        f"⚠️ *MT5 Reconnect Warning*\n"
                        f"Failed {alert_at} times — still trying...\n"
                        f"Check MT5 terminal & network."
                    )

            # ── หมด retry ─────────────────────────────────────
            log.critical(
                f"❌ MT5 reconnect FAILED after {max_try} attempts"
            )
            self._notify(
                f"🚨 *MT5 Reconnect FAILED*\n"
                f"Gave up after {max_try} attempts.\n"
                f"Bot is PAUSED — manual intervention required.\n"
                f"Check MT5 terminal, broker server, network."
            )
            return False

    def ensure_connected(self) -> bool:
        """
        One-liner ให้ main.py เรียกทุก cycle

        Logic:
          1. throttle — ถ้าเพิ่งเช็คไปไม่ถึง health_check_interval วินาที
             → คืนค่าสถานะล่าสุดเลย (ไม่เปลืองเวลา)
          2. health_check() → ถ้า OK คืน True
          3. ถ้า health_check ล้มเหลว → reconnect()

        Returns:
            True  = connected พร้อมเทรด
            False = reconnect ล้มเหลว → ควร skip cycle นี้
        """
        from config import get_config
        cfg_rc      = get_config().get("mt5", {}).get("reconnect", {})
        hc_interval = int(cfg_rc.get("health_check_interval", 60))

        now = datetime.now(timezone.utc)
        if self._hc_last_check is not None:
            elapsed = (now - self._hc_last_check).total_seconds()
            if elapsed < hc_interval:
                # เพิ่งเช็คไปหยกๆ → ถือว่าสถานะเดิมยังถูกต้อง
                return self._conn_stats.is_connected

        # ถึงเวลาเช็คจริง
        if self.health_check():
            return True

        # health_check ล้มเหลว → พยายาม reconnect
        return self.reconnect()

    def get_connection_stats(self) -> dict:
        """
        สรุป connection statistics
        ใช้สำหรับ /status ใน Telegram หรือ dashboard

        Example:
            conn = client.get_connection_stats()
            print(f"reconnects={conn['total_reconnects']} "
                  f"rate={conn['reconnect_success_rate']}%")
        """
        stats = self._conn_stats
        return {
            "connected"             : stats.is_connected,
            "total_disconnects"     : stats.total_disconnects,
            "total_reconnects"      : stats.total_reconnects,
            "failed_reconnects"     : stats.failed_reconnects,
            "reconnect_success_rate": round(stats.reconnect_success_rate, 1),
            "last_connected_at"     : stats.last_connected_at,
            "last_disconnect_at"    : stats.last_disconnect_at,
            "last_reconnect_at"     : stats.last_reconnect_at,
            "uptime_seconds"        : round(stats.uptime_seconds, 0),
        }

    # ══════════════════════════════════════════════════════════
    # PRIVATE HELPERS (Reconnect Layer)
    # ══════════════════════════════════════════════════════════

    def _try_connect(self) -> bool:
        """
        1 attempt: shutdown → initialize → login → health_check
        คืน True ถ้าสำเร็จ ใช้โดย reconnect() เท่านั้น
        """
        mt5_cfg = {}
        try:
            from config import get_config
            mt5_cfg = get_config().get("mt5", {})
        except Exception:
            pass   # fallback ไปใช้ env vars

        path   = mt5_cfg.get("terminal_path") or self._terminal_path
        login  = int(mt5_cfg.get("login")     or self._login)
        pw     = mt5_cfg.get("password")      or self._password
        server = mt5_cfg.get("server")        or self._server

        try:
            # Step 1: cleanup ก่อน
            mt5.shutdown()
            time.sleep(2)

            # Step 2: initialize terminal
            init_ok = (
                mt5.initialize(path=path) if path
                else mt5.initialize()
            )
            if not init_ok:
                log.debug(f"_try_connect: initialize() failed — {mt5.last_error()}")
                return False

            # Step 3: login
            if not mt5.login(login, password=pw, server=server):
                log.debug(f"_try_connect: login() failed — {mt5.last_error()}")
                mt5.shutdown()
                return False

            # Step 4: verify (ผ่าน health_check ซึ่งเช็ค 3 ชั้น)
            if not self.health_check():
                log.debug("_try_connect: health_check failed after login")
                return False

            # สำเร็จ — อัปเดต state
            self._connected                    = True
            self._connected_at                 = datetime.now(timezone.utc)
            self._conn_stats.is_connected      = True
            self._conn_stats.last_connected_at = self._connected_at.isoformat()
            return True

        except Exception as e:
            log.debug(f"_try_connect exception: {e}")
            return False

    def _mark_disconnected(self):
        """
        บันทึก disconnect event (เรียกโดย health_check เมื่อตรวจพบ)
        Log เฉพาะครั้งแรกที่หลุด (ไม่ spam log ซ้ำ)
        """
        if self._conn_stats.is_connected:
            self._conn_stats.is_connected       = False
            self._connected                     = False
            self._conn_stats.total_disconnects += 1
            now = datetime.now(timezone.utc).isoformat()
            self._conn_stats.last_disconnect_at = now
            self._conn_stats.uptime_seconds     = 0.0
            log.warning(
                f"⚡ MT5 disconnection detected "
                f"(total: {self._conn_stats.total_disconnects})"
            )

    def _notify(self, msg: str):
        """ส่ง Telegram notification (ถ้า notify_telegram: true ใน config)"""
        try:
            from config import get_config
            if get_config().get("mt5", {}).get(
                "reconnect", {}
            ).get("notify_telegram", True):
                from bot.notifier import notify
                notify(msg)
        except Exception:
            pass   # notification ล้มเหลวไม่กระทบ reconnect logic

    # ══════════════════════════════════════════════════════════
    # Private Helpers (original)
    # ══════════════════════════════════════════════════════════
    # NOTE: _ping() และ _reconnect() ถูกแทนที่ด้วย
    #       health_check() และ reconnect() ด้านบนแล้ว

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
                log.warning(f"Cannot activate symbol: {sym}")
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
        self, symbol: str = None ) -> list[dict]:
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
            'symbol'             : symbol,
            'digits'             : info.digits,
            'point'              : info.point,
            'trade_contract_size': info.trade_contract_size,
            'volume_min'         : info.volume_min,
            'volume_max'         : info.volume_max,
            'volume_step'        : info.volume_step,
            'tick_size'          : info.trade_tick_size,
            'tick_value'         : info.trade_tick_value,
            'currency_profit'    : info.currency_profit,
            'currency_base'      : info.currency_base,
            'spread_current'     : info.spread,
            'swap_long'          : info.swap_long,
            'swap_short'         : info.swap_short,
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
        รวม ConnectionStats ใหม่ด้วย
        """
        try:
            terminal = mt5.terminal_info()
            account  = mt5.account_info()
            conn_s   = self.get_connection_stats()   # NEW

            return {
                'connected'        : self._connected,
                'ping_ok'          : terminal is not None,
                'last_ping'        : datetime.fromtimestamp(
                    self._last_ping, tz=timezone.utc
                ).isoformat() if self._last_ping else None,
                'terminal_version' : (
                    terminal.build        # ✅ FIX BUG-8: .build แทน .community_version
                    if terminal else None
                ),
                'account_login'    : (
                    account.login if account else None
                ),
                'account_balance'  : (
                    account.balance if account else None
                ),
                # ── Reconnect stats (NEW) ──────────────────
                'total_reconnects' : conn_s['total_reconnects'],
                'total_disconnects': conn_s['total_disconnects'],
                'reconnect_rate'   : conn_s['reconnect_success_rate'],
                'uptime_seconds'   : conn_s['uptime_seconds'],
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
            f"connected={self._connected} "
            f"reconnects={self._conn_stats.total_reconnects})"
        )
