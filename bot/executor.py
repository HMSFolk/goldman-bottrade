# bot/executor.py
"""
Order Executor — ส่ง Order จริงไปยัง MT5
════════════════════════════════════════════════════════════
ความรับผิดชอบ:
  - send_order:  เปิด position ใหม่ (BUY/SELL)
  - close_order: ปิด position เดียว
  - close_all:   ปิดทุก position
  - modify_order: แก้ SL/TP
  - retry logic: ลองใหม่อัตโนมัติถ้า order ล้มเหลว
  - slippage guard: ยกเลิกถ้าราคาเบี่ยงมากเกิน
  - บันทึกทุก order ลง DB + Telegram
════════════════════════════════════════════════════════════
"""

import logging
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import MetaTrader5 as mt5

# ✅ FIX BUG-6: ใช้ get_config() แทน open(config.yaml) โดยตรง
from config import get_config
CFG = get_config()

log = logging.getLogger("bot.executor")


# ══════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════
@dataclass
class OrderResult:
    """ผลลัพธ์จากการส่ง order"""
    success:      bool
    ticket:       int   = 0
    symbol:       str   = ""
    direction:    int   = 0
    volume:       float = 0.0
    open_price:   float = 0.0
    sl:           float = 0.0
    tp:           float = 0.0
    retcode:      int   = 0
    comment:      str   = ""
    error_msg:    str   = ""
    latency_ms:   float = 0.0
    attempts:     int   = 1
    slippage_pts: float = 0.0

    def __str__(self):
        if self.success:
            arrow = "↑ BUY" if self.direction == 1 else "↓ SELL"
            return (
                f"✅ {arrow} #{self.ticket} "
                f"{self.symbol} lot={self.volume:.2f} "
                f"@{self.open_price:.5f} "
                f"SL={self.sl:.5f} TP={self.tp:.5f} "
                f"slip={self.slippage_pts:.1f}pts "
                f"({self.latency_ms:.0f}ms)"
            )
        return (
            f"❌ Order FAILED: "
            f"{self.error_msg} "
            f"(retcode={self.retcode})"
        )


@dataclass
class CloseResult:
    """ผลลัพธ์จากการปิด position"""
    success:    bool
    ticket:     int   = 0
    symbol:     str   = ""
    close_price:float = 0.0
    profit:     float = 0.0
    error_msg:  str   = ""

    def __str__(self):
        if self.success:
            icon = "💚" if self.profit >= 0 else "🔴"
            return (
                f"{icon} CLOSE #{self.ticket} "
                f"{self.symbol} "
                f"@{self.close_price:.5f} "
                f"P&L={self.profit:+.2f}"
            )
        return f"❌ CLOSE FAILED: {self.error_msg}"


# ══════════════════════════════════════════════════════════════
# Order Executor
# ══════════════════════════════════════════════════════════════
class OrderExecutor:
    """
    จัดการส่ง order ทุกประเภทไปยัง MT5 Exness

    วิธีใช้:
        executor = OrderExecutor(client, risk)
        result   = executor.send_order(
            symbol="XAUUSD",
            direction=1,
            sl_distance=0.015,
            tp_distance=0.030,
            confidence=0.73,
        )
    """

    # MT5 Return Codes ที่ควร retry
    RETRYABLE_CODES = {
        mt5.TRADE_RETCODE_REQUOTE,        # 10004: requote
        mt5.TRADE_RETCODE_CONNECTION,     # 10006: no connection
        mt5.TRADE_RETCODE_PRICE_CHANGED,  # 10015: price changed
        mt5.TRADE_RETCODE_TIMEOUT,        # 10010: timeout
        mt5.TRADE_RETCODE_PRICE_OFF,      # 10021: price off
        mt5.TRADE_RETCODE_REJECT,         # 10006: rejected
    }

    # Return codes ที่ห้าม retry (error ถาวร)
    FATAL_CODES = {
        mt5.TRADE_RETCODE_INVALID,        # 10014: invalid params
        mt5.TRADE_RETCODE_INVALID_VOLUME, # 10014: invalid volume
        mt5.TRADE_RETCODE_INVALID_PRICE,  # 10015: invalid price
        mt5.TRADE_RETCODE_INVALID_STOPS,  # 10016: invalid stops
        mt5.TRADE_RETCODE_TRADE_DISABLED, # 10017: trade disabled
        mt5.TRADE_RETCODE_MARKET_CLOSED,  # 10018: market closed
        mt5.TRADE_RETCODE_NO_MONEY,       # 10019: not enough money
        mt5.TRADE_RETCODE_FROZEN,         # 10023: frozen
    }

    def __init__(self, client, risk_manager):
        from bot.mt5_client   import MT5Client
        from bot.risk_manager import RiskManager

        self.client  = client
        self.risk    = risk_manager
        self.magic   = CFG['order']['magic_number']
        self.comment = CFG['order'].get('order_comment', 'bot')

        # Retry config
        self.max_retries   = 3
        self.retry_delay   = 1.0   # วินาที
        self.max_slippage  = CFG['order']['max_slippage']  # points

        log.info(
            f"OrderExecutor initialized | "
            f"magic={self.magic} | "
            f"max_slippage={self.max_slippage}pts"
        )

    # ══════════════════════════════════════════════════════════
    # Send Order — เปิด Position ใหม่
    # ══════════════════════════════════════════════════════════
    def send_order(
        self,
        symbol:      str,
        direction:   int,           # 1=BUY -1=SELL
        sl_distance: float,         # ระยะ SL (price units)
        tp_distance: float,         # ระยะ TP (price units)
        confidence:  float = 0.0,
        comment:     str   = "",
    ) -> OrderResult:
        """
        ส่ง Market Order ไปยัง MT5

        Pipeline:
        1. Risk check ทุกข้อ
        2. คำนวณ lot size
        3. คำนวณ SL/TP price
        4. ตรวจ spread อีกรอบก่อนส่ง
        5. ส่ง order พร้อม retry
        6. ตรวจ slippage
        7. บันทึกและ notify
        """
        t0 = time.time()

        # ── Step 1: Risk Check ────────────────────────────────
        acc    = self.client.get_account()
        report = self.risk.check_all(
            symbol       = symbol,
            direction    = direction,
            balance      = acc['balance'],
            equity       = acc['equity'],
            sl_distance  = sl_distance,
            margin_level = acc['margin_level'],
        )

        if not report.all_passed:
            reason = str(report.failed_checks[0])
            log.info(f"Order blocked: {reason}")
            return OrderResult(
                success   = False,
                symbol    = symbol,
                direction = direction,
                error_msg = reason,
            )

        lot = report.lot_size

        # ── Step 2: คำนวณ SL/TP Price ─────────────────────────
        sl_price, tp_price = self.risk.calculate_sl_tp_price(
            symbol, direction, sl_distance, tp_distance
        )

        # ── Step 3: ราคา entry ปัจจุบัน ──────────────────────
        tick   = mt5.symbol_info_tick(symbol)
        info   = mt5.symbol_info(symbol)

        if tick is None or info is None:
            return OrderResult(
                success   = False,
                symbol    = symbol,
                error_msg = "ไม่มีข้อมูล tick/symbol",
            )

        intended_price = tick.ask if direction == 1 else tick.bid
        digits         = info.digits
        order_type     = (
            mt5.ORDER_TYPE_BUY if direction == 1
            else mt5.ORDER_TYPE_SELL
        )

        # ── Step 4: ตรวจ spread ก่อนส่ง (final check) ────────
        spread_pts = round(
            (tick.ask - tick.bid) / info.point
        )
        max_spread = CFG['risk']['max_spread_points']
        if isinstance(max_spread, dict):
            sp_limit = max_spread.get(symbol, max_spread.get('default', 30))
        else:
            sp_limit = int(max_spread)

        if spread_pts > sp_limit:
            return OrderResult(
                success   = False,
                symbol    = symbol,
                error_msg = f"Spread {spread_pts}pts > {sp_limit}pts",
            )

        # ── Step 5: Build Request ──────────────────────────────
        full_comment = (
            f"{self.comment}_{comment}"
            if comment else self.comment
        )[:31]   # MT5 limit 31 chars

        # ✅ FIX BUG-8 CRITICAL: ORDER_FILLING_IOC ใช้ไม่ได้กับ Exness XAUUSD
        #    ต้องตรวจ filling_mode ที่ symbol รองรับก่อนส่ง order
        #    Exness ส่วนใหญ่ใช้ FOK หรือ RETURN ไม่ใช่ IOC
        filling = self._get_filling_mode(symbol)

        request = {
            "action"      : mt5.TRADE_ACTION_DEAL,
            "symbol"      : symbol,
            "volume"      : lot,
            "type"        : order_type,
            "price"       : intended_price,
            "sl"          : sl_price,
            "tp"          : tp_price,
            "deviation"   : self.max_slippage,
            "magic"       : self.magic,
            "comment"     : full_comment,
            "type_time"   : mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        # ── Step 6: Send พร้อม Retry ──────────────────────────
        result = self._send_with_retry(request)

        if not result.success:
            self._notify_failure(symbol, direction, result.error_msg)
            return result

        # ── Step 7: ตรวจ Slippage ─────────────────────────────
        actual_price   = result.open_price
        slippage_pts   = abs(
            actual_price - intended_price
        ) / info.point

        result.slippage_pts = slippage_pts
        result.symbol       = symbol
        result.direction    = direction
        result.volume       = lot
        result.sl           = sl_price
        result.tp           = tp_price
        result.latency_ms   = (time.time() - t0) * 1000

        if slippage_pts > self.max_slippage * 2:
            log.warning(
                f"High slippage: {slippage_pts:.1f}pts "
                f"(limit={self.max_slippage}pts)"
            )

        # ── Step 8: บันทึกและ Notify ──────────────────────────
        self._record_order(result, acc, confidence)
        self._notify_success(result, confidence)

        log.info(str(result))
        return result

    def _send_with_retry(
        self, request: dict
    ) -> OrderResult:
        """
        ส่ง order พร้อม retry logic
        Requote และ timeout สามารถ retry ได้
        """
        for attempt in range(1, self.max_retries + 1):
            # อัพเดตราคาก่อน retry (ราคาเปลี่ยนตลอดเวลา)
            if attempt > 1:
                tick = mt5.symbol_info_tick(request['symbol'])
                if tick:
                    request['price'] = (
                        tick.ask
                        if request['type'] == mt5.ORDER_TYPE_BUY
                        else tick.bid
                    )
                    log.debug(
                        f"Retry {attempt}: "
                        f"updated price={request['price']:.5f}"
                    )
                # ✅ FIX BUG-9: exponential backoff แทน linear (1,2,3s → 1,2,4s)
                delay = self.retry_delay * (2 ** (attempt - 2))
                time.sleep(delay)

            # ส่ง order
            raw = mt5.order_send(request)

            if raw is None:
                err = mt5.last_error()
                log.error(f"order_send returned None: {err}")
                continue

            # ตรวจ retcode
            if raw.retcode == mt5.TRADE_RETCODE_DONE:
                return OrderResult(
                    success    = True,
                    ticket     = raw.order,
                    open_price = raw.price,
                    retcode    = raw.retcode,
                    comment    = raw.comment,
                    attempts   = attempt,
                )

            # Fatal error — ไม่ retry
            if raw.retcode in self.FATAL_CODES:
                msg = self._retcode_msg(raw.retcode)
                log.error(
                    f"Fatal error (no retry): "
                    f"retcode={raw.retcode} {msg}"
                )
                return OrderResult(
                    success   = False,
                    retcode   = raw.retcode,
                    error_msg = f"{msg} (retcode={raw.retcode})",
                    attempts  = attempt,
                )

            # Retryable error
            if raw.retcode in self.RETRYABLE_CODES:
                log.warning(
                    f"Retryable error attempt {attempt}: "
                    f"retcode={raw.retcode} "
                    f"{self._retcode_msg(raw.retcode)}"
                )
                continue

            # Unknown error
            log.error(
                f"Unknown retcode={raw.retcode}: "
                f"{raw.comment}"
            )
            return OrderResult(
                success   = False,
                retcode   = raw.retcode,
                error_msg = f"{raw.comment} ({raw.retcode})",
                attempts  = attempt,
            )

        # หมด retry
        return OrderResult(
            success   = False,
            error_msg = (
                f"ล้มเหลวหลัง {self.max_retries} attempts"
            ),
            attempts  = self.max_retries,
        )

    # ══════════════════════════════════════════════════════════
    # Close Position
    # ══════════════════════════════════════════════════════════
    def close_order(
        self,
        ticket:  int,
        reason:  str = "manual",
    ) -> CloseResult:
        """
        ปิด position เดียวตาม ticket number

        ใช้เมื่อ:
        - Signal กลับทิศ
        - Trailing stop ถูกแตะ
        - Manual close จาก Telegram
        """
        # หา position
        positions = mt5.positions_get()
        if positions is None:
            return CloseResult(
                success   = False,
                error_msg = "ไม่มี open positions",
            )

        pos = next(
            (p for p in positions if p.ticket == ticket),
            None
        )
        if pos is None:
            return CloseResult(
                success   = False,
                ticket    = ticket,
                error_msg = f"ไม่พบ ticket #{ticket}",
            )

        # ราคาปัจจุบัน
        tick  = mt5.symbol_info_tick(pos.symbol)
        close_price = (
            tick.bid if pos.type == 0   # BUY ปิดที่ bid
            else tick.ask               # SELL ปิดที่ ask
        )
        close_type  = (
            mt5.ORDER_TYPE_SELL if pos.type == 0
            else mt5.ORDER_TYPE_BUY
        )

        request = {
            "action"      : mt5.TRADE_ACTION_DEAL,
            "symbol"      : pos.symbol,
            "volume"      : pos.volume,
            "type"        : close_type,
            "position"    : ticket,
            "price"       : close_price,
            "deviation"   : self.max_slippage,
            "magic"       : self.magic,
            "comment"     : f"close_{reason}"[:31],
            "type_time"   : mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(pos.symbol),  # ✅ FIX BUG-8
        }

        raw = mt5.order_send(request)

        if raw is None or raw.retcode != mt5.TRADE_RETCODE_DONE:
            err = mt5.last_error() if raw is None else raw.retcode
            return CloseResult(
                success   = False,
                ticket    = ticket,
                error_msg = f"Close failed: {err}",
            )

        # คำนวณ P&L
        profit = pos.profit

        result = CloseResult(
            success     = True,
            ticket      = ticket,
            symbol      = pos.symbol,
            close_price = raw.price,
            profit      = profit,
        )

        log.info(str(result))

        # บันทึก trade ที่ปิดแล้ว
        self._record_close(result, pos)
        self.risk.record_trade(profit)

        # Notify
        from bot.notifier import notify
        icon = "💚" if profit >= 0 else "🔴"
        notify(
            f"{icon} *Trade Closed*\n"
            f"#{ticket} {pos.symbol} "
            f"{'BUY' if pos.type==0 else 'SELL'}\n"
            f"P&L: ${profit:+.2f}\n"
            f"Reason: {reason}"
        )

        return result

    def close_all(
        self,
        symbol: str  = None,
        reason: str  = "close_all",
    ) -> list[CloseResult]:
        """
        ปิดทุก position (หรือเฉพาะ symbol)

        ใช้เมื่อ:
        - Daily loss limit เตะ
        - Emergency stop จาก Telegram /closeall
        - Signal กลับทิศแรงมาก
        """
        if symbol:
            positions = mt5.positions_get(symbol=symbol) or []
        else:
            positions = mt5.positions_get() or []

        if not positions:
            log.info("close_all: ไม่มี open positions")
            return []

        log.warning(
            f"🛑 Closing {len(positions)} positions | "
            f"reason={reason}"
        )

        results = []
        for pos in positions:
            result = self.close_order(pos.ticket, reason=reason)
            results.append(result)

            if not result.success:
                log.error(
                    f"Failed to close #{pos.ticket}: "
                    f"{result.error_msg}"
                )

        success_count = sum(1 for r in results if r.success)
        total_pnl     = sum(r.profit for r in results if r.success)

        log.info(
            f"close_all complete: "
            f"{success_count}/{len(results)} closed | "
            f"total P&L=${total_pnl:+.2f}"
        )

        return results

    # ══════════════════════════════════════════════════════════
    # Modify Order — แก้ SL/TP
    # ══════════════════════════════════════════════════════════
    def modify_sl_tp(
        self,
        ticket:    int,
        new_sl:    Optional[float] = None,
        new_tp:    Optional[float] = None,
    ) -> bool:
        """
        แก้ SL/TP ของ position ที่เปิดอยู่
        ใช้สำหรับ trailing stop

        ถ้า new_sl=None จะใช้ค่าเดิม (ไม่แก้)
        """
        positions = mt5.positions_get()
        if positions is None:
            return False

        pos = next(
            (p for p in positions if p.ticket == ticket),
            None
        )
        if pos is None:
            log.warning(f"ไม่พบ ticket #{ticket} สำหรับ modify")
            return False

        request = {
            "action"  : mt5.TRADE_ACTION_SLTP,
            "symbol"  : pos.symbol,
            "position": ticket,
            "sl"      : new_sl if new_sl is not None else pos.sl,
            "tp"      : new_tp if new_tp is not None else pos.tp,
        }

        raw = mt5.order_send(request)

        if raw is None or raw.retcode != mt5.TRADE_RETCODE_DONE:
            err = mt5.last_error() if raw is None else raw.retcode
            log.error(f"Modify #{ticket} failed: {err}")
            return False

        log.info(
            f"✅ Modified #{ticket}: "
            # ✅ FIX BUG-7: new_sl/new_tp อาจเป็น None → ใช้ pos.sl/tp แทน
            f"SL={new_sl if new_sl is not None else pos.sl:.5f} "
            f"TP={new_tp if new_tp is not None else pos.tp:.5f}"
        )
        return True

    def update_trailing_stop(
        self,
        symbol:    str,
        atr_mult:  float = 1.5,
    ):
        """
        อัพเดต trailing stop ทุก tick
        ขยับ SL ตาม ATR เมื่อราคาไปในทิศที่ดี

        เรียกจาก bot/main.py ในทุก tick
        """
        positions = mt5.positions_get(symbol=symbol) or []

        for pos in positions:
            if pos.magic != self.magic:
                continue   # ไม่ใช่ order ของบอทนี้

            tick    = mt5.symbol_info_tick(symbol)
            info    = mt5.symbol_info(symbol)
            if tick is None or info is None:
                continue

            current = tick.bid if pos.type == 0 else tick.ask
            atr_pts = self.client.get_atr_points(symbol, period=14)
            trail   = atr_pts * atr_mult * info.point

            if pos.type == 0:   # BUY — ขยับ SL ขึ้น
                new_sl = current - trail
                if new_sl > pos.sl + info.point * 5:
                    # ขยับได้ถ้า SL ใหม่สูงกว่าเดิมอย่างน้อย 5 points
                    self.modify_sl_tp(pos.ticket, new_sl=round(new_sl, info.digits))
                    log.debug(
                        f"Trail BUY #{pos.ticket}: "
                        f"SL {pos.sl:.5f}→{new_sl:.5f}"
                    )

            else:               # SELL — ขยับ SL ลง
                new_sl = current + trail
                if new_sl < pos.sl - info.point * 5:
                    self.modify_sl_tp(pos.ticket, new_sl=round(new_sl, info.digits))
                    log.debug(
                        f"Trail SELL #{pos.ticket}: "
                        f"SL {pos.sl:.5f}→{new_sl:.5f}"
                    )

    # ══════════════════════════════════════════════════════════
    # Record & Notify
    # ══════════════════════════════════════════════════════════
    def _record_order(
        self,
        result:     OrderResult,
        acc:        dict,
        confidence: float,
    ):
        """บันทึก order ที่ส่งสำเร็จลง DB"""
        try:
            from bot.metrics_writer import write_trade
            write_trade({
                'open_time'  : datetime.now(timezone.utc).isoformat(),
                'close_time' : None,
                'symbol'     : result.symbol,
                'direction'  : 'BUY' if result.direction == 1 else 'SELL',
                'lot'        : result.volume,
                'open_price' : result.open_price,
                'close_price': None,
                'pnl'        : None,
                'sl'         : result.sl,
                'tp'         : result.tp,
                'confidence' : confidence,
                'comment'    : result.comment,
                'ticket'     : result.ticket,
            })
        except Exception as e:
            log.warning(f"Record order error: {e}")

    def _record_close(self, result: CloseResult, pos):
        """บันทึก trade ที่ปิดแล้ว"""
        try:
            from bot.metrics_writer import write_trade
            write_trade({
                'open_time'  : datetime.fromtimestamp(
                    pos.time, tz=timezone.utc
                ).isoformat(),
                'close_time' : datetime.now(timezone.utc).isoformat(),
                'symbol'     : result.symbol,
                'direction'  : 'BUY' if pos.type == 0 else 'SELL',
                'lot'        : pos.volume,
                'open_price' : pos.price_open,
                'close_price': result.close_price,
                'pnl'        : result.profit,
                'sl'         : pos.sl,
                'tp'         : pos.tp,
                'confidence' : 0.0,
                'comment'    : f"closed",
                'ticket'     : result.ticket,
            })
        except Exception as e:
            log.warning(f"Record close error: {e}")

    def _notify_success(self, result: OrderResult, conf: float):
        """Telegram แจ้งเตือนเมื่อ order สำเร็จ"""
        if not CFG['notifications'].get('notify_on', {}).get(
            'order_open', True
        ):
            return

        from bot.notifier import notify
        arrow = "↑ BUY" if result.direction == 1 else "↓ SELL"
        notify(
            f"📊 *Order Opened*\n"
            f"{arrow} {result.symbol}\n"
            f"Ticket: #{result.ticket}\n"
            f"Price:  {result.open_price:.5f}\n"
            f"Lot:    {result.volume:.2f}\n"
            f"SL:     {result.sl:.5f}\n"
            f"TP:     {result.tp:.5f}\n"
            f"Conf:   {conf:.1%}\n"
            f"Slip:   {result.slippage_pts:.1f}pts"
        )

    def _notify_failure(
        self, symbol: str, direction: int, reason: str
    ):
        """Telegram แจ้งเตือนเมื่อ order ล้มเหลว"""
        if not CFG['notifications'].get('notify_on', {}).get(
            'order_failed', True
        ):
            return

        from bot.notifier import notify
        notify(
            f"⚠️ *Order Failed*\n"
            f"{'BUY' if direction==1 else 'SELL'} "
            f"{symbol}\n"
            f"Reason: {reason}"
        )

    @staticmethod
    def _get_filling_mode(symbol: str) -> int:
        """
        ✅ FIX BUG-8: ตรวจ filling mode ที่ broker/symbol รองรับจริง
        แทนที่จะ hardcode ORDER_FILLING_IOC ซึ่ง Exness ไม่รองรับ

        MT5 filling_mode เป็น bitmask:
          1 = ORDER_FILLING_FOK   (Fill or Kill)
          2 = ORDER_FILLING_IOC   (Immediate or Cancel)
          4 = ORDER_FILLING_RETURN (Return remaining as pending)

        Exness XAUUSD: ส่วนใหญ่รองรับ FOK หรือ RETURN
        """
        info = mt5.symbol_info(symbol)
        if info is None:
            # fallback ปลอดภัย
            return mt5.ORDER_FILLING_FOK

        filling_mode = info.filling_mode  # bitmask

        # ลำดับความสำคัญ: FOK > IOC > RETURN
        if filling_mode & 1:    # FOK supported
            return mt5.ORDER_FILLING_FOK
        if filling_mode & 2:    # IOC supported
            return mt5.ORDER_FILLING_IOC
        if filling_mode & 4:    # RETURN supported
            return mt5.ORDER_FILLING_RETURN

        # ถ้าไม่ match เลย — ลอง FOK (Exness default)
        log.warning(
            f"{symbol}: filling_mode={filling_mode} ไม่รู้จัก — "
            f"fallback to FOK"
        )
        return mt5.ORDER_FILLING_FOK

    @staticmethod
    def _retcode_msg(retcode: int) -> str:
        """แปลง retcode เป็นข้อความภาษาคน"""
        messages = {
            mt5.TRADE_RETCODE_DONE          : "Success",
            mt5.TRADE_RETCODE_REQUOTE       : "Requote",
            mt5.TRADE_RETCODE_REJECT        : "Rejected",
            mt5.TRADE_RETCODE_CANCEL        : "Cancelled",
            mt5.TRADE_RETCODE_PLACED        : "Placed",
            mt5.TRADE_RETCODE_DONE_PARTIAL  : "Partial fill",
            mt5.TRADE_RETCODE_ERROR         : "General error",
            mt5.TRADE_RETCODE_TIMEOUT       : "Timeout",
            mt5.TRADE_RETCODE_INVALID       : "Invalid params",
            mt5.TRADE_RETCODE_INVALID_VOLUME: "Invalid volume",
            mt5.TRADE_RETCODE_INVALID_PRICE : "Invalid price",
            mt5.TRADE_RETCODE_INVALID_STOPS : "Invalid SL/TP",
            mt5.TRADE_RETCODE_TRADE_DISABLED: "Trade disabled",
            mt5.TRADE_RETCODE_MARKET_CLOSED : "Market closed",
            mt5.TRADE_RETCODE_NO_MONEY      : "Insufficient funds",
            mt5.TRADE_RETCODE_PRICE_CHANGED : "Price changed",
            mt5.TRADE_RETCODE_PRICE_OFF     : "Price off",
            mt5.TRADE_RETCODE_CONNECTION    : "No connection",
            mt5.TRADE_RETCODE_TOO_MANY_REQUESTS: "Too many requests",
            mt5.TRADE_RETCODE_FROZEN        : "Order frozen",
        }
        return messages.get(retcode, f"Unknown({retcode})")