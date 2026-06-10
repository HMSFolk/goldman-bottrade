# bot/notifier.py
"""
Telegram Notifier — แจ้งเตือนทุก event สำคัญ
════════════════════════════════════════════════════════════
ออกแบบให้:
  - Non-blocking: ถ้า Telegram ล่มบอทไม่หยุด
  - Rate limiting: ป้องกันส่งมากเกินไป (flood)
  - Priority queue: urgent message ส่งก่อน
  - Format: Markdown สวยงาม อ่านง่ายบนมือถือ
  - Thread-safe: ส่งจาก background thread
════════════════════════════════════════════════════════════
"""

import logging
import os
import time
import threading
import queue
import json
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("bot.notifier")

import yaml
with open("config.yaml", encoding="utf-8") as f:
    _CFG = yaml.safe_load(f)


# ══════════════════════════════════════════════════════════════
# Priority Levels
# ══════════════════════════════════════════════════════════════
class Priority(IntEnum):
    """
    ลำดับความสำคัญของ message
    ยิ่งสูง ยิ่งส่งก่อน และไม่ถูก throttle
    """
    LOW      = 0   # daily summary, debug info
    NORMAL   = 1   # order open/close, signal
    HIGH     = 2   # error, warning
    CRITICAL = 3   # bot crash, connection lost, daily loss hit


# ══════════════════════════════════════════════════════════════
# Message Data Class
# ══════════════════════════════════════════════════════════════
@dataclass(order=True)
class Message:
    """Message ที่รอส่งใน queue"""
    priority:  int   = field(compare=True)
    text:      str   = field(compare=False)
    parse_mode:str   = field(default="Markdown", compare=False)
    created_at:float = field(
        default_factory=time.time, compare=False
    )

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at


# ══════════════════════════════════════════════════════════════
# Notifier Class
# ══════════════════════════════════════════════════════════════
class TelegramNotifier:
    """
    Telegram Bot Notifier พร้อม background sender

    Pattern: Producer-Consumer
    - Bot code เรียก notify() → เพิ่มใน queue (ทันที non-blocking)
    - Background thread อ่าน queue → ส่ง Telegram (async)
    """

    # Rate limits (Telegram API limits)
    MAX_MESSAGES_PER_MINUTE = 20
    MAX_MESSAGE_LENGTH      = 4096

    def __init__(self):
        self._token    = os.getenv("TELEGRAM_TOKEN",   "")
        self._chat_id  = os.getenv("TELEGRAM_CHAT_ID", "")
        self._enabled  = bool(self._token and self._chat_id)

        if not self._enabled:
            log.warning(
                "Telegram disabled: "
                "TELEGRAM_TOKEN หรือ TELEGRAM_CHAT_ID ไม่ได้ตั้งค่า"
            )
            return

        # Priority queue (max_priority ส่งก่อน)
        self._queue    = queue.PriorityQueue(maxsize=100)

        # Rate limiting
        self._sent_times: list = []   # timestamps ที่ส่งแล้ว
        self._lock = threading.Lock()

        # Deduplication (ป้องกันส่งซ้ำ)
        self._recent_hashes: dict = {}   # hash → timestamp
        self._dedup_window  = 60         # วินาที

        # Throttle per message type
        self._last_sent: dict = {}        # type → timestamp
        self._throttle_sec: dict = {
            'order_open'    : 0,    # ไม่ throttle
            'order_close'   : 0,
            'daily_loss'    : 0,
            'error'         : 30,   # error เดียวกัน ส่งได้ทุก 30 วินาที
            'warning'       : 60,
            'signal'        : 300,  # signal ส่งได้ทุก 5 นาที
            'daily_summary' : 0,
        }

        # Retry config
        self._max_retries = 3
        self._retry_delay = 2.0

        # Start background thread
        self._thread = threading.Thread(
            target = self._sender_loop,
            name   = "TelegramSender",
            daemon = True,   # daemon thread หยุดพร้อม main thread
        )
        self._thread.start()

        self._base_url = (
            f"https://api.telegram.org/bot{self._token}"
        )

        log.info(
            f"TelegramNotifier started | "
            f"chat_id={self._chat_id}"
        )

    # ── Public API ────────────────────────────────────────────
    def send(
        self,
        text:      str,
        priority:  Priority = Priority.NORMAL,
        msg_type:  str = "general",
    ):
        """
        ส่ง message ไป Telegram (non-blocking)
        เพิ่มลง queue แล้ว return ทันที
        """
        if not self._enabled:
            log.debug(f"Telegram disabled — skip: {text[:50]}")
            return

        # Throttle check
        if not self._check_throttle(msg_type, priority):
            log.debug(f"Throttled msg_type={msg_type}")
            return

        # Deduplication
        if self._is_duplicate(text):
            log.debug(f"Duplicate message — skip")
            return

        # Truncate ถ้ายาวเกิน
        if len(text) > self.MAX_MESSAGE_LENGTH:
            text = text[:self.MAX_MESSAGE_LENGTH - 3] + "..."

        msg = Message(
            priority   = -int(priority),   # negated เพราะ Python queue min-heap
            text       = text,
        )

        try:
            self._queue.put_nowait(msg)
        except queue.Full:
            log.warning("Telegram queue full — dropping message")

    def send_urgent(self, text: str):
        """Shortcut สำหรับ CRITICAL message — ไม่ throttle"""
        self.send(text, Priority.CRITICAL, "critical")

    # ── Background Sender ─────────────────────────────────────
    def _sender_loop(self):
        """
        Background thread ที่ส่ง message จาก queue
        รันตลอดเวลาที่ bot ทำงาน
        """
        log.debug("Telegram sender thread started")

        while True:
            try:
                # ดึง message (block 1 วินาที)
                msg = self._queue.get(timeout=1.0)

                # ตรวจ rate limit
                self._wait_for_rate_limit()

                # ส่ง message พร้อม retry
                success = self._send_with_retry(msg.text)

                if success:
                    self._record_sent()
                    log.debug(
                        f"Telegram sent: {msg.text[:50]}... "
                        f"(age={msg.age_seconds:.1f}s)"
                    )

                self._queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                log.error(f"Sender loop error: {e}")
                time.sleep(5)

    def _send_with_retry(self, text: str) -> bool:
        """ส่ง HTTP request ไป Telegram API พร้อม retry"""
        url     = f"{self._base_url}/sendMessage"
        payload = {
            "chat_id"                  : self._chat_id,
            "text"                     : text,
            "parse_mode"               : "Markdown",
            "disable_web_page_preview" : True,
        }

        for attempt in range(1, self._max_retries + 1):
            try:
                resp = requests.post(
                    url,
                    json    = payload,
                    timeout = 10,
                )

                if resp.status_code == 200:
                    return True

                # Too Many Requests — รอตาม retry_after
                if resp.status_code == 429:
                    retry_after = int(
                        resp.json().get('parameters', {})
                        .get('retry_after', 5)
                    )
                    log.warning(
                        f"Telegram rate limit — "
                        f"waiting {retry_after}s"
                    )
                    time.sleep(retry_after)
                    continue

                log.warning(
                    f"Telegram error {resp.status_code}: "
                    f"{resp.text[:100]}"
                )

            except requests.exceptions.Timeout:
                log.warning(
                    f"Telegram timeout attempt {attempt}"
                )
            except requests.exceptions.ConnectionError:
                log.warning(
                    f"Telegram connection error attempt {attempt}"
                )
            except Exception as e:
                log.error(f"Telegram unexpected error: {e}")
                return False

            if attempt < self._max_retries:
                time.sleep(self._retry_delay * attempt)

        return False

    # ── Rate Limiting ─────────────────────────────────────────
    def _wait_for_rate_limit(self):
        """รอถ้าส่งเร็วเกินไป (Telegram limit: 30 msg/วินาที)"""
        with self._lock:
            now = time.time()
            # ลบ timestamps เก่ากว่า 60 วินาที
            self._sent_times = [
                t for t in self._sent_times
                if now - t < 60
            ]

            if len(self._sent_times) >= self.MAX_MESSAGES_PER_MINUTE:
                oldest = self._sent_times[0]
                wait   = 60 - (now - oldest) + 0.1
                if wait > 0:
                    log.debug(f"Rate limit wait: {wait:.1f}s")
                    time.sleep(wait)

    def _record_sent(self):
        """บันทึก timestamp ที่ส่งแล้ว"""
        with self._lock:
            self._sent_times.append(time.time())

    def _check_throttle(
        self, msg_type: str, priority: Priority
    ) -> bool:
        """
        ตรวจว่าควรส่งหรือ throttle
        CRITICAL และ HIGH ไม่ถูก throttle เสมอ
        """
        if priority >= Priority.HIGH:
            return True   # urgent ส่งเสมอ

        throttle_sec = self._throttle_sec.get(msg_type, 0)
        if throttle_sec == 0:
            return True

        last = self._last_sent.get(msg_type, 0)
        if time.time() - last >= throttle_sec:
            self._last_sent[msg_type] = time.time()
            return True

        return False

    def _is_duplicate(self, text: str) -> bool:
        """ตรวจ message ซ้ำใน dedup_window วินาที"""
        import hashlib
        h   = hashlib.md5(text.encode()).hexdigest()
        now = time.time()

        # ลบ hash เก่า
        self._recent_hashes = {
            k: v for k, v in self._recent_hashes.items()
            if now - v < self._dedup_window
        }

        if h in self._recent_hashes:
            return True

        self._recent_hashes[h] = now
        return False

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def is_healthy(self) -> bool:
        return (
            self._enabled and
            self._thread.is_alive() and
            self._queue.qsize() < 90
        )


# ══════════════════════════════════════════════════════════════
# Message Formatters
# ══════════════════════════════════════════════════════════════
class BotMessages:
    """
    Template สำหรับ message แต่ละประเภท
    ใช้ Markdown formatting ของ Telegram
    """

    @staticmethod
    def order_opened(
        symbol:    str,
        direction: str,
        ticket:    int,
        price:     float,
        lot:       float,
        sl:        float,
        tp:        float,
        confidence:float,
        session:   str = "",
    ) -> str:
        arrow  = "↑" if direction == "BUY" else "↓"
        rr     = round(abs(tp - price) / abs(sl - price), 1) if sl != price else 0
        return (
            f"📊 *Order Opened*\n"
            f"{arrow} `{direction}` {symbol}\n"
            f"Ticket:  `#{ticket}`\n"
            f"Price:   `{price:.5f}`\n"
            f"Lot:     `{lot:.2f}`\n"
            f"SL:      `{sl:.5f}`\n"
            f"TP:      `{tp:.5f}`\n"
            f"RR:      `1:{rr}`\n"
            f"Conf:    `{confidence:.1%}`\n"
            f"Session: `{session}`\n"
            f"⏰ {_now_thai()}"
        )

    @staticmethod
    def order_closed(
        symbol:      str,
        direction:   str,
        ticket:      int,
        open_price:  float,
        close_price: float,
        profit:      float,
        reason:      str = "",
    ) -> str:
        icon   = "💚" if profit >= 0 else "🔴"
        result = "WIN" if profit >= 0 else "LOSS"
        pips   = abs(close_price - open_price) * (
            10 if "JPY" not in symbol else 100
        )
        return (
            f"{icon} *Trade {result}*\n"
            f"{'↑' if direction=='BUY' else '↓'} "
            f"`{direction}` {symbol}\n"
            f"Ticket:  `#{ticket}`\n"
            f"Entry:   `{open_price:.5f}`\n"
            f"Exit:    `{close_price:.5f}`\n"
            f"P&L:     `${profit:+.2f}`\n"
            f"Reason:  `{reason}`\n"
            f"⏰ {_now_thai()}"
        )

    @staticmethod
    def daily_loss_hit(
        balance_start: float,
        balance_now:   float,
        loss_pct:      float,
    ) -> str:
        return (
            f"🛑 *Daily Loss Limit Hit*\n"
            f"Start:    `${balance_start:,.2f}`\n"
            f"Now:      `${balance_now:,.2f}`\n"
            f"Loss:     `{loss_pct:.1%}`\n"
            f"Action:   All positions closed\n"
            f"Resume:   Tomorrow 00:00 UTC\n"
            f"⏰ {_now_thai()}"
        )

    @staticmethod
    def daily_summary(
        date:    str,
        balance: float,
        pnl:     float,
        trades:  int,
        wins:    int,
        losses:  int,
    ) -> str:
        win_rate = wins / max(trades, 1) * 100
        icon     = "📈" if pnl >= 0 else "📉"
        return (
            f"{icon} *Daily Summary — {date}*\n"
            f"Balance:  `${balance:,.2f}`\n"
            f"P&L:      `${pnl:+.2f}`\n"
            f"Trades:   `{trades}` "
            f"({wins}W/{losses}L)\n"
            f"Win Rate: `{win_rate:.1f}%`\n"
            f"⏰ {_now_thai()}"
        )

    @staticmethod
    def bot_error(
        error_msg: str,
        symbol:    str = "",
        count:     int = 1,
    ) -> str:
        return (
            f"⚠️ *Bot Error*\n"
            f"Symbol:  `{symbol or 'N/A'}`\n"
            f"Count:   `{count}`\n"
            f"Error:   `{error_msg[:200]}`\n"
            f"⏰ {_now_thai()}"
        )

    @staticmethod
    def connection_lost(attempts: int) -> str:
        return (
            f"🚨 *MT5 Connection Lost*\n"
            f"Reconnect attempts: `{attempts}`\n"
            f"Bot may be stopping...\n"
            f"⏰ {_now_thai()}"
        )

    @staticmethod
    def bot_started(
        strategy_ver: str,
        symbols:      list,
        balance:      float,
    ) -> str:
        return (
            f"✅ *Bot Started*\n"
            f"Strategy: `v{strategy_ver}`\n"
            f"Symbols:  `{', '.join(symbols)}`\n"
            f"Balance:  `${balance:,.2f}`\n"
            f"⏰ {_now_thai()}"
        )

    @staticmethod
    def bot_stopped(
        ticks:     int,
        reason:    str = "manual",
    ) -> str:
        return (
            f"🛑 *Bot Stopped*\n"
            f"Reason: `{reason}`\n"
            f"Ticks:  `{ticks}`\n"
            f"⏰ {_now_thai()}"
        )

    @staticmethod
    def retrain_complete(
        symbols:  list,
        duration: float,
    ) -> str:
        return (
            f"🔄 *Retrain Complete*\n"
            f"Symbols:  `{', '.join(symbols)}`\n"
            f"Duration: `{duration:.0f}s`\n"
            f"⏰ {_now_thai()}"
        )


# ══════════════════════════════════════════════════════════════
# Singleton + Public API
# ══════════════════════════════════════════════════════════════
_notifier: Optional[TelegramNotifier] = None

def _get_notifier() -> TelegramNotifier:
    """Lazy singleton"""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier


def notify(
    text:     str,
    priority: Priority = Priority.NORMAL,
    msg_type: str = "general",
):
    """
    ฟังก์ชันหลักที่ทุกไฟล์เรียกใช้

    ตัวอย่าง:
        from bot.notifier import notify, Priority

        notify("Order opened!")
        notify("CRITICAL ERROR", priority=Priority.CRITICAL)
    """
    try:
        _get_notifier().send(text, priority, msg_type)
    except Exception as e:
        log.error(f"notify() error: {e}")


def notify_order_opened(**kwargs):
    """Shortcut: แจ้งเมื่อ order เปิด"""
    msg = BotMessages.order_opened(**kwargs)
    notify(msg, Priority.NORMAL, "order_open")


def notify_order_closed(**kwargs):
    """Shortcut: แจ้งเมื่อ order ปิด"""
    msg = BotMessages.order_closed(**kwargs)
    notify(msg, Priority.NORMAL, "order_close")


def notify_daily_loss(**kwargs):
    """Shortcut: แจ้งเมื่อ daily loss limit เตะ"""
    msg = BotMessages.daily_loss_hit(**kwargs)
    notify(msg, Priority.CRITICAL, "daily_loss")


def notify_error(
    error_msg: str,
    symbol:    str = "",
    count:     int = 1,
):
    """Shortcut: แจ้ง error"""
    msg = BotMessages.bot_error(error_msg, symbol, count)
    notify(msg, Priority.HIGH, "error")


def _now_thai() -> str:
    """เวลาปัจจุบัน UTC"""
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )