# ══════════════════════════════════════════════════════════════
# SPREAD FILTER — ส่วนที่ต้องเพิ่มใน bot/executor.py
# ══════════════════════════════════════════════════════════════
#
# ไฟล์นี้แสดง pattern ที่ต้องเพิ่มใน executor.py
# โดยใช้ check_spread() ของ RiskManager ก่อนส่ง order
#
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# PATTERN A: ถ้า executor มี execute_order() / send_order()
# ══════════════════════════════════════════════════════════════
#
# หา function ที่ส่ง order ไป MT5 แล้วเพิ่มก่อน mt5.order_send()
#
# ตัวอย่าง ก่อนแก้:
# ─────────────────────────────────────────────────────────────
#
#   def execute_order(self, symbol, direction, volume, sl, tp):
#       request = {
#           "action"  : mt5.TRADE_ACTION_DEAL,
#           "symbol"  : symbol,
#           ...
#       }
#       result = mt5.order_send(request)
#       return result
#
# ─────────────────────────────────────────────────────────────
# ตัวอย่าง หลังแก้ (เพิ่ม 6 บรรทัด):
# ─────────────────────────────────────────────────────────────
#
#   def execute_order(self, symbol, direction, volume, sl, tp):
#
#       # ── [SPREAD CHECK] ────────────────────────────────────
#       spread_result = self.risk_manager.check_spread(symbol)
#       if not spread_result.ok:
#           log.warning(
#               f"Order blocked [{symbol}]: {spread_result.reason}"
#           )
#           return None           # ← ไม่ส่ง order
#       # ─────────────────────────────────────────────────────
#
#       request = {
#           "action"  : mt5.TRADE_ACTION_DEAL,
#           "symbol"  : symbol,
#           ...
#       }
#       result = mt5.order_send(request)
#       return result
#
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# PATTERN B: ถ้า executor มี open_trade() หรือ place_order()
# ══════════════════════════════════════════════════════════════
#
#   def open_trade(self, signal: dict) -> bool:
#       symbol = signal["symbol"]
#
#       # ── [SPREAD CHECK] ────────────────────────────────────
#       spread_result = self.risk_manager.check_spread(symbol)
#       if not spread_result.ok:
#           log.warning(f"[{symbol}] spread too wide — skip: {spread_result.reason}")
#           return False
#       # ─────────────────────────────────────────────────────
#
#       # ... โค้ดเปิด order เดิม
#
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# PATTERN C (แนะนำ): ถ้าต้องการ log spread ทุก order พร้อมกัน
# ══════════════════════════════════════════════════════════════

import logging
import MetaTrader5 as mt5

log = logging.getLogger("bot.executor")


def _execute_order_with_spread_check(
    self,
    symbol    : str,
    direction : str,      # "BUY" หรือ "SELL"
    volume    : float,
    sl_price  : float,
    tp_price  : float,
) -> dict | None:
    """
    Execute order พร้อม spread check
    คืน order result dict หรือ None ถ้าถูกบล็อก
    """

    # ── Step 1: Spread Check ───────────────────────────────────
    spread_result = self.risk_manager.check_spread(symbol)
    if not spread_result.ok:
        log.warning(
            f"⛔ Order BLOCKED [{symbol} {direction}]: {spread_result.reason}"
        )
        return None

    log.info(
        f"[{symbol}] spread OK: "
        f"{spread_result.spread:.5f} / limit {spread_result.limit:.5f} "
        f"({spread_result.spread_pct:.0f}%)"
    )

    # ── Step 2: Build MT5 request ──────────────────────────────
    action = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL

    # ดึงราคาปัจจุบัน
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.error(f"execute_order: no tick for {symbol}")
        return None

    price = tick.ask if direction == "BUY" else tick.bid

    request = {
        "action"        : mt5.TRADE_ACTION_DEAL,
        "symbol"        : symbol,
        "volume"        : volume,
        "type"          : action,
        "price"         : price,
        "sl"            : sl_price,
        "tp"            : tp_price,
        "deviation"     : 10,
        "magic"         : 234000,
        "comment"       : f"AurumBot {direction}",
        "type_time"     : mt5.ORDER_TIME_GTC,
        "type_filling"  : mt5.ORDER_FILLING_IOC,
    }

    # ── Step 3: Send ───────────────────────────────────────────
    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        retcode = result.retcode if result else "None"
        comment = result.comment if result else "unknown"
        log.error(f"Order failed [{symbol}]: retcode={retcode} — {comment}")
        return None

    log.info(
        f"✅ Order filled [{symbol} {direction}]: "
        f"#{result.order} vol={volume} @ {price:.5f} "
        f"SL={sl_price:.5f} TP={tp_price:.5f} "
        f"spread_at_fill={spread_result.spread:.5f}"
    )

    return {
        "order_id"   : result.order,
        "symbol"     : symbol,
        "direction"  : direction,
        "volume"     : volume,
        "price"      : price,
        "sl"         : sl_price,
        "tp"         : tp_price,
        "spread"     : spread_result.spread,
    }


# ══════════════════════════════════════════════════════════════
# MINIMAL VERSION — ถ้าอยากเพิ่มแค่ 5 บรรทัดใน executor เดิม
# ══════════════════════════════════════════════════════════════
#
# วาง 5 บรรทัดนี้ต้นฟังก์ชันที่ส่ง order ใน executor.py:
#
#   spread_result = self.risk_manager.check_spread(symbol)
#   if not spread_result.ok:
#       log.warning(f"Spread blocked: {spread_result}")
#       return None
#
# ══════════════════════════════════════════════════════════════
