# dashboard/telegram_control.py
"""
Telegram Command Bot — สั่งงาน Trading Bot จากมือถือ
════════════════════════════════════════════════════════════
Commands:
  /status      → ดูสถานะ account + open positions
  /closeall    → ปิดทุก position ทันที (ต้องยืนยัน)
  /pause       → หยุดเทรดชั่วคราว (บอทยังรันอยู่)
  /resume      → เปิดเทรดต่อ
  /pnl         → P&L วันนี้ + สัปดาห์นี้ + เดือนนี้
  /positions   → รายการ open positions
  /trades N    → ดู N trades ล่าสุด
  /balance     → balance + equity + margin
  /risk        → risk settings ปัจจุบัน
  /dd          → drawdown ปัจจุบัน
  /gold        → ราคาทอง real-time
  /spread      → spread ทุก symbol
  /session     → session ตลาดที่เปิดอยู่
  /ping        → ตรวจ bot alive + latency
  /uptime      → bot รันมานานแค่ไหน
  /reset_risk  → Reset ทุกอย่าง: CB counters + ปลดล็อก pause
  /reset_daily → Reset เฉพาะ daily loss counter
  /reset_cb    → Reset เฉพาะ consecutive loss counter
  /quote       → trading wisdom
  /flip        → AI signal 🎲
  /help        → รายการคำสั่งทั้งหมด

Security:
  รับคำสั่งจาก chat_id ที่กำหนดเท่านั้น
  ป้องกันคนอื่น access บอท
════════════════════════════════════════════════════════════
"""

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# setup_logging ก่อน import อื่น
from bot.setup_logging import setup_logging
setup_logging()

import json
import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ใช้ get_config() — merge .env ให้แล้ว credentials อยู่ใน CFG
from config import get_config, resolve_symbol, unresolve_symbol   # ✅ NEW
CFG = get_config()

log = logging.getLogger("dashboard.telegram_bot")

# ── Bot start time (for /uptime) ──────────────────────────────
_BOT_START_TIME = datetime.now(timezone.utc)

# ── Trading wisdom quotes (for /quote) ────────────────────────
_QUOTES = [
    ("The trend is your friend.",                                                            "Jesse Livermore"),
    ("Cut your losses, let your profits run.",                                               "David Ricardo"),
    ("Risk management is the only thing that matters.",                                      "Paul Tudor Jones"),
    ("The market can stay irrational longer than you can stay solvent.",                     "Keynes"),
    ("Plan the trade, trade the plan.",                                                      "Wall St. wisdom"),
    ("In the short run, the market is a voting machine. In the long run, a weighing machine.", "Benjamin Graham"),
    ("The goal of a successful trader is to make the best trades, not to be right.",         "Mark Douglas"),
    ("Markets are never wrong — opinions often are.",                                        "Jesse Livermore"),
    ("Rule No.1: Never lose money. Rule No.2: Never forget Rule No.1.",                     "Warren Buffett"),
    ("Amateurs think about how much they can make. Pros think about how much they can lose.", "Larry Hite"),
    ("Trade what you see, not what you think.",                                              "Unknown"),
    ("Be fearful when others are greedy, and greedy when others are fearful.",               "Warren Buffett"),
    ("Patience is the most important skill in trading.",                                     "Unknown"),
    ("The biggest risk is not taking any risk.",                                             "Mark Zuckerberg"),
    ("Every battle is won before it is fought.",                                             "Sun Tzu"),
]

# Absolute paths จาก project root
DB_PATH      = _ROOT / CFG['paths']['db']
FLAGS_DIR    = _ROOT / CFG['paths']['flags']
ACCOUNT_JSON = _ROOT / CFG['paths']['logs'] / "account.json"

# อ่าน TOKEN/CHAT_ID จาก CFG (merge .env แล้ว)
TOKEN   = CFG.get('notifications', {}).get('telegram_token',   '')
CHAT_ID = CFG.get('notifications', {}).get('telegram_chat_id', '')


# ══════════════════════════════════════════════════════════════
# Security Guard
# ══════════════════════════════════════════════════════════════
def _authorized(update: Update) -> bool:
    """
    ตรวจว่า message มาจาก chat_id ที่อนุญาตเท่านั้น
    ถ้าไม่ใช่ → ไม่ทำอะไร + log warning
    """
    chat_id  = str(update.effective_chat.id)
    username = update.effective_user.username if update.effective_user else "unknown"

    if chat_id != str(CHAT_ID):
        log.warning(
            f"Unauthorized access attempt: "
            f"chat_id={chat_id} user={username}"
        )
        return False
    return True


# ══════════════════════════════════════════════════════════════
# Helper Functions
# ══════════════════════════════════════════════════════════════
def _load_account() -> dict:
    """โหลด account snapshot ล่าสุด"""
    try:
        return json.loads(ACCOUNT_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_trades(days_back: int = 1) -> list:
    """โหลด closed trades จาก SQLite"""
    if not DB_PATH.exists():
        return []

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT symbol, direction, volume,
                   open_price, close_price,
                   profit, close_time, ticket
            FROM   trades
            WHERE  close_time IS NOT NULL
              AND  close_time >= datetime('now', ?)
            ORDER  BY close_time DESC
            LIMIT  50
        """, (f"-{days_back} days",)).fetchall()
    return [dict(r) for r in rows]


def _get_open_positions() -> list:
    """ดึง open positions จาก MT5"""
    try:
        import MetaTrader5 as mt5
        if not mt5.initialize():
            log.error("Telegram Bot: MT5 initialize failed")
            return []

        positions = mt5.positions_get() or []
        return [
            {
                'ticket': p.ticket,
                # ✅ FIX: p.symbol จาก MT5 เป็นชื่อ broker (XAUUSDm)
                # แปลงกลับเป็นชื่อกลางก่อนแสดงผล ให้ตรงกับที่อื่นในระบบ
                'symbol': unresolve_symbol(p.symbol),
                'type'  : 'BUY' if p.type == 0 else 'SELL',
                'volume': p.volume,
                'price' : p.price_open,
                'profit': p.profit,
                'sl'    : p.sl,
                'tp'    : p.tp,
            }
            for p in positions
            if p.magic == CFG['order']['magic_number']
        ]
    except Exception as e:
        log.error(f"get_positions error: {e}")
        return []


def _is_paused() -> bool:
    return (FLAGS_DIR / "paused").exists()


def _set_pause(paused: bool):
    pause_file = FLAGS_DIR / "paused"
    if paused:
        FLAGS_DIR.mkdir(parents=True, exist_ok=True)
        pause_file.touch()
    else:
        if pause_file.exists():
            pause_file.unlink()


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _pnl_summary(days_back: int) -> dict:
    """คำนวณ P&L summary จาก DB"""
    empty = {'trades': 0, 'pnl': 0, 'wins': 0, 'losses': 0}
    if not DB_PATH.exists():
        return empty

    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*)                                        AS trades,
                    COALESCE(SUM(profit), 0)                        AS pnl,
                    SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END)    AS wins,
                    SUM(CASE WHEN profit <= 0 THEN 1 ELSE 0 END)   AS losses
                FROM trades
                WHERE  close_time IS NOT NULL
                  AND  close_time >= datetime('now', ?)
            """, (f"-{days_back} days",)).fetchone()
    except Exception as e:
        log.error(f"pnl_summary error: {e}")
        return empty

    if row is None:
        return empty

    return {
        'trades' : row[0] or 0,
        'pnl'    : round(row[1] or 0, 2),
        'wins'   : row[2] or 0,
        'losses' : row[3] or 0,
    }


def _current_sessions() -> list:
    """ตรวจว่าตอนนี้ session ไหนเปิดอยู่ (UTC)"""
    hour   = datetime.now(timezone.utc).hour
    active = []
    if hour >= 21 or hour < 6:  active.append("🦘 Sydney")
    if 0  <= hour < 9:          active.append("🗼 Tokyo")
    if 7  <= hour < 16:         active.append("🎡 London")
    if 12 <= hour < 21:         active.append("🗽 New York")
    return active if active else ["😴 Off hours"]


# ══════════════════════════════════════════════════════════════
# Command Handlers
# ══════════════════════════════════════════════════════════════

# ── /status ───────────────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """แสดงสถานะรวม: account + open positions + pause status + today P&L"""
    if not _authorized(update):
        return

    acc       = _load_account()
    positions = _get_open_positions()
    paused    = _is_paused()
    today     = _pnl_summary(days_back=1)

    status_icon = "⏸ PAUSED" if paused else "▶️ RUNNING"
    balance     = acc.get('balance',     0)
    equity      = acc.get('equity',      0)
    profit      = acc.get('profit',      0)
    free_margin = acc.get('free_margin', 0)

    if positions:
        pos_lines = []
        for p in positions:
            icon      = "↑" if p['type'] == "BUY" else "↓"
            pnl_color = "🟢" if p['profit'] >= 0 else "🔴"
            pos_lines.append(
                f"  {icon} `{p['symbol']}` "
                f"lot={p['volume']:.2f} "
                f"{pnl_color}${p['profit']:+.2f}"
            )
        pos_text = "\n".join(pos_lines)
    else:
        pos_text = "  _ไม่มี open position_"

    msg = (
        f"📊 *Bot Status*\n"
        f"Status:   `{status_icon}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Balance:  `${balance:,.2f}`\n"
        f"Equity:   `${equity:,.2f}`\n"
        f"Floating: `${profit:+.2f}`\n"
        f"Free Margin: `${free_margin:,.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Open Positions ({len(positions)}):*\n"
        f"{pos_text}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Today P&L:*\n"
        f"  Trades: `{today['trades']}` "
        f"({today['wins']}W/{today['losses']}L)\n"
        f"  P&L:    `${today['pnl']:+.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏰ `{_now_str()}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /closeall ─────────────────────────────────────────────────
async def cmd_closeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ปิด position ทั้งหมด (ต้องยืนยันด้วย /confirmclose ภายใน 30 วิ)"""
    if not _authorized(update):
        return

    positions = _get_open_positions()
    if not positions:
        await update.message.reply_text("ℹ️ ไม่มี open position ที่จะปิด")
        return

    total_profit = sum(p['profit'] for p in positions)
    lines = [
        f"  {'↑' if p['type']=='BUY' else '↓'} "
        f"`{p['symbol']}` lot={p['volume']:.2f} "
        f"${p['profit']:+.2f}"
        for p in positions
    ]

    context.user_data['pending_closeall'] = True
    context.user_data['pending_timeout']  = (
        datetime.now(timezone.utc) + timedelta(seconds=30)
    )

    await update.message.reply_text(
        f"⚠️ *Confirm Close All*\n\n"
        f"จะปิด *{len(positions)} positions*:\n"
        f"{chr(10).join(lines)}\n\n"
        f"Floating P&L: `${total_profit:+.2f}`\n\n"
        f"ยืนยันด้วยคำสั่ง `/confirmclose`",
        parse_mode="Markdown",
    )


async def cmd_confirmclose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ยืนยันการปิด position ทั้งหมด"""
    if not _authorized(update):
        return

    pending = context.user_data.get('pending_closeall', False)
    timeout = context.user_data.get('pending_timeout')

    if not pending:
        await update.message.reply_text(
            "❌ ไม่มีคำสั่ง closeall ที่รอยืนยัน\n"
            "ใช้ /closeall ก่อน"
        )
        return

    if datetime.now(timezone.utc) > timeout:
        context.user_data['pending_closeall'] = False
        await update.message.reply_text(
            "⏰ หมดเวลายืนยัน (30 วินาที)\n"
            "ใช้ /closeall อีกครั้ง"
        )
        return

    await update.message.reply_text("🔄 กำลังปิด positions...")

    try:
        from bot.executor     import OrderExecutor
        from bot.mt5_client   import MT5Client
        from bot.risk_manager import RiskManager

        client   = MT5Client()
        risk     = RiskManager()
        executor = OrderExecutor(client, risk)
        results  = executor.close_all(reason="telegram_closeall")

        success_count = sum(1 for r in results if r.success)
        total_pnl     = sum(r.profit for r in results if r.success)

        msg = (
            f"{'✅' if success_count == len(results) else '⚠️'} "
            f"*Close All Complete*\n"
            f"Closed: `{success_count}/{len(results)}`\n"
            f"Total P&L: `${total_pnl:+.2f}`\n"
            f"⏰ `{_now_str()}`"
        )
        context.user_data['pending_closeall'] = False

    except Exception as e:
        log.error(f"closeall error: {e}", exc_info=True)
        msg = f"❌ Error: `{str(e)[:200]}`"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /reset_risk ───────────────────────────────────────────────
async def cmd_reset_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔄 Reset ทุกอย่าง: CB + consecutive + daily — ส่ง flag ให้ main loop"""
    if not _authorized(update):
        return

    try:
        FLAGS_DIR.mkdir(parents=True, exist_ok=True)
        (FLAGS_DIR / "reset_all.flag").touch()
        await update.message.reply_text(
            f"🔄 *Full Reset Requested*\n\n"
            f"✅ ส่งคำสั่งให้ main loop แล้ว\n"
            f"Bot จะ reset + resume ใน tick ถัดไป\n\n"
            f"สิ่งที่จะถูก reset:\n"
            f"  • Circuit breaker trigger\n"
            f"  • Consecutive losses counter\n"
            f"  • Daily tracking balance\n\n"
            f"⏰ `{_now_str()}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


# ── /reset_daily ──────────────────────────────────────────────
async def cmd_reset_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📅 Reset เฉพาะ daily loss counter — ส่ง flag ให้ main loop"""
    if not _authorized(update):
        return

    try:
        FLAGS_DIR.mkdir(parents=True, exist_ok=True)
        (FLAGS_DIR / "reset_daily.flag").touch()
        await update.message.reply_text(
            f"📅 *Daily Reset Requested*\n\n"
            f"✅ ส่งคำสั่งให้ main loop แล้ว\n"
            f"Bot จะ reset daily counter ใน tick ถัดไป\n\n"
            f"สิ่งที่จะถูก reset:\n"
            f"  • Circuit breaker trigger\n"
            f"  • Daily tracking balance\n\n"
            f"⚠️ Consecutive losses และ Weekly limit ยังคงอยู่\n"
            f"⏰ `{_now_str()}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


# ── /reset_cb ─────────────────────────────────────────────────
async def cmd_reset_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔢 Reset เฉพาะ consecutive loss counter — ส่ง flag ให้ main loop"""
    if not _authorized(update):
        return

    try:
        FLAGS_DIR.mkdir(parents=True, exist_ok=True)
        (FLAGS_DIR / "reset_cb.flag").touch()
        await update.message.reply_text(
            f"🔢 *Consecutive Reset Requested*\n\n"
            f"✅ ส่งคำสั่งให้ main loop แล้ว\n"
            f"Bot จะ reset consecutive losses ใน tick ถัดไป\n\n"
            f"สิ่งที่จะถูก reset:\n"
            f"  • Circuit breaker trigger\n"
            f"  • Consecutive losses counter\n\n"
            f"⚠️ Daily loss ยังคำนวณจาก DB อยู่\n"
            f"⏰ `{_now_str()}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


# ── /pause ────────────────────────────────────────────────────
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """หยุดเทรดชั่วคราว — บอทยังรันอยู่แต่ไม่เปิด order ใหม่"""
    if not _authorized(update):
        return

    if _is_paused():
        await update.message.reply_text(
            "ℹ️ Bot ถูก pause อยู่แล้ว\n"
            "ใช้ /resume เพื่อเปิดใหม่"
        )
        return

    _set_pause(True)
    positions = _get_open_positions()
    open_info = (
        f"⚠️ ยังมี {len(positions)} open positions อยู่"
        if positions else "ไม่มี open positions"
    )

    await update.message.reply_text(
        f"⏸ *Bot Paused*\n\n"
        f"บอทหยุดเปิด order ใหม่แล้ว\n"
        f"{open_info}\n\n"
        f"ใช้ /resume เพื่อเปิดเทรดต่อ\n"
        f"⏰ `{_now_str()}`",
        parse_mode="Markdown",
    )
    log.info("Bot paused via Telegram")


# ── /resume ───────────────────────────────────────────────────
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """เปิดเทรดต่อหลัง pause"""
    if not _authorized(update):
        return

    if not _is_paused():
        await update.message.reply_text("ℹ️ Bot ทำงานอยู่แล้ว ไม่ได้ถูก pause")
        return

    _set_pause(False)
    acc = _load_account()

    await update.message.reply_text(
        f"▶️ *Bot Resumed*\n\n"
        f"บอทกลับมาเทรดแล้ว\n"
        f"Balance: `${acc.get('balance', 0):,.2f}`\n"
        f"⏰ `{_now_str()}`",
        parse_mode="Markdown",
    )
    log.info("Bot resumed via Telegram")


# ── /pnl ──────────────────────────────────────────────────────
async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """แสดง P&L หลายช่วงเวลา"""
    if not _authorized(update):
        return

    today = _pnl_summary(days_back=1)
    week  = _pnl_summary(days_back=7)
    month = _pnl_summary(days_back=30)
    total = _pnl_summary(days_back=3650)

    def _wr(d: dict) -> str:
        t = d['trades']
        return "N/A" if t == 0 else f"{d['wins']/t*100:.0f}%"

    def _icon(pnl: float) -> str:
        return "📈" if pnl >= 0 else "📉"

    await update.message.reply_text(
        f"💰 *P&L Summary*\n\n"
        f"*วันนี้* {_icon(today['pnl'])}\n"
        f"  P&L:    `${today['pnl']:+.2f}`\n"
        f"  Trades: `{today['trades']}` WR={_wr(today)}\n\n"
        f"*สัปดาห์นี้* {_icon(week['pnl'])}\n"
        f"  P&L:    `${week['pnl']:+.2f}`\n"
        f"  Trades: `{week['trades']}` WR={_wr(week)}\n\n"
        f"*เดือนนี้* {_icon(month['pnl'])}\n"
        f"  P&L:    `${month['pnl']:+.2f}`\n"
        f"  Trades: `{month['trades']}` WR={_wr(month)}\n\n"
        f"*ทั้งหมด* {_icon(total['pnl'])}\n"
        f"  P&L:    `${total['pnl']:+.2f}`\n"
        f"  Trades: `{total['trades']}` WR={_wr(total)}\n\n"
        f"⏰ `{_now_str()}`",
        parse_mode="Markdown",
    )


# ── /positions ────────────────────────────────────────────────
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """รายละเอียด open positions ทุกตัว"""
    if not _authorized(update):
        return

    positions = _get_open_positions()
    if not positions:
        await update.message.reply_text("ℹ️ ไม่มี open positions")
        return

    lines        = ["📋 *Open Positions*\n"]
    total_profit = 0

    for p in positions:
        arrow  = "↑" if p['type'] == "BUY" else "↓"
        icon   = "🟢" if p['profit'] >= 0 else "🔴"
        total_profit += p['profit']
        lines.append(
            f"{icon} {arrow} *{p['symbol']}* #{p['ticket']}\n"
            f"  Lot:    `{p['volume']:.2f}`\n"
            f"  Entry:  `{p['price']:.5f}`\n"
            f"  SL:     `{p['sl']:.5f}`\n"
            f"  TP:     `{p['tp']:.5f}`\n"
            f"  P&L:    `${p['profit']:+.2f}`\n"
        )

    lines.append(
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Total Floating: `${total_profit:+.2f}`\n"
        f"⏰ `{_now_str()}`"
    )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /trades ───────────────────────────────────────────────────
async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    ดู trades ล่าสุด
    /trades    → 5 trades ล่าสุด
    /trades 10 → 10 trades ล่าสุด (max 20)
    """
    if not _authorized(update):
        return

    try:
        n = int(context.args[0]) if context.args else 5
        n = min(max(n, 1), 20)
    except (ValueError, IndexError):
        n = 5

    trades = _load_trades(days_back=30)[:n]
    if not trades:
        await update.message.reply_text("ℹ️ ยังไม่มี trade ใน 30 วันล่าสุด")
        return

    lines = [f"📋 *{n} Trades ล่าสุด*\n"]
    for t in trades:
        icon     = "💚" if (t['profit'] or 0) >= 0 else "🔴"
        arrow    = "↑" if t['direction'] == "BUY" else "↓"
        time_str = t['close_time'][:16] if t['close_time'] else "?"
        lines.append(
            f"{icon} {arrow} `{t['symbol']}` "
            f"lot={t['volume']:.2f} "
            f"*${(t['profit'] or 0):+.2f}*\n"
            f"  `{time_str}`"
        )

    total = sum(t['profit'] or 0 for t in trades)
    lines.append(f"\nTotal: `${total:+.2f}`\n⏰ `{_now_str()}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /balance ──────────────────────────────────────────────────
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Balance, equity, margin details"""
    if not _authorized(update):
        return

    acc          = _load_account()
    balance      = acc.get('balance',      0)
    equity       = acc.get('equity',       0)
    margin       = acc.get('margin',       0)
    free_margin  = acc.get('free_margin',  0)
    margin_level = acc.get('margin_level', 0)
    leverage     = acc.get('leverage',     0)

    margin_icon = (
        "🔴" if margin_level < 200
        else "🟡" if margin_level < 500
        else "🟢"
    )

    await update.message.reply_text(
        f"💰 *Balance Details*\n\n"
        f"Balance:      `${balance:,.2f}`\n"
        f"Equity:       `${equity:,.2f}`\n"
        f"Floating P&L: `${acc.get('profit', 0):+.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Margin Used:  `${margin:,.2f}`\n"
        f"Free Margin:  `${free_margin:,.2f}`\n"
        f"{margin_icon} Margin Level: `{margin_level:.0f}%`\n"
        f"Leverage:     `1:{leverage}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏰ `{_now_str()}`",
        parse_mode="Markdown",
    )


# ── /risk ─────────────────────────────────────────────────────
async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Risk settings ปัจจุบัน"""
    if not _authorized(update):
        return

    r           = CFG['risk']
    s           = CFG['signal']
    paused_text = "⏸ PAUSED" if _is_paused() else "▶️ ACTIVE"

    await update.message.reply_text(
        f"⚙️ *Risk Settings*\n\n"
        f"Bot Status:     `{paused_text}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Risk/Trade:     `{r['risk_per_trade']:.1%}`\n"
        f"Max Daily Loss: `{r['max_daily_loss_pct']:.1%}`\n"
        f"Max Trades:     `{r['max_open_trades']}`\n"
        f"Min Confidence: `{s['min_confidence']:.0%}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        # ✅ FIX: header เดิมเขียนว่า "SL Points" แต่ค่าที่โชว์จริงคือ
        # r['max_spread_points'] (spread limit) ไม่ใช่ r ที่เกี่ยวกับ SL เลย
        # ส่วน SL distance จริงอยู่ที่ order.sl_points (CFG['order']['sl_points'])
        # ซึ่งไม่เคยถูกโชว์ที่นี่มาก่อน — แก้ label ให้ตรง + เพิ่ม SL points จริง
        f"Max Spread (pts):\n"
        f"  XAUUSD: `{r['max_spread_points'].get('XAUUSD', 30)}`\n"
        f"  EURUSD: `{r['max_spread_points'].get('EURUSD', 15)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"SL Points:\n"
        f"  XAUUSD: `{CFG['order']['sl_points'].get('XAUUSD', 100)}`\n"
        f"  EURUSD: `{CFG['order']['sl_points'].get('EURUSD', 80)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Strategy: `{CFG.get('signal', {}).get('active_model', '?')}`\n"
        f"⏰ `{_now_str()}`",
        parse_mode="Markdown",
    )


# ── /dd ───────────────────────────────────────────────────────
async def cmd_dd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Drawdown ปัจจุบัน + เทียบ daily limit"""
    if not _authorized(update):
        return

    acc     = _load_account()
    balance = acc.get('balance', 0)
    equity  = acc.get('equity',  0)

    if balance <= 0:
        await update.message.reply_text("❌ ไม่พบข้อมูล account (bot อาจยังไม่ได้รัน)")
        return

    floating_dd = max(0.0, balance - equity)
    dd_pct      = (floating_dd / balance * 100) if balance > 0 else 0
    today_pnl   = _pnl_summary(days_back=1)['pnl']
    limit_pct   = CFG.get('risk', {}).get('max_daily_loss_pct', 0.05) * 100

    dd_icon = (
        "🔴" if dd_pct >= limit_pct
        else "🟡" if dd_pct >= limit_pct / 2
        else "🟢"
    )
    status = "⚠️ WARNING — ใกล้ถึง limit!" if dd_pct >= limit_pct * 0.8 else "✅ OK"

    await update.message.reply_text(
        f"{dd_icon} *Drawdown Report*\n\n"
        f"Balance:         `${balance:,.2f}`\n"
        f"Equity:          `${equity:,.2f}`\n"
        f"Floating DD:     `${floating_dd:,.2f}` ({dd_pct:.1f}%)\n"
        f"Today P&L:       `${today_pnl:+.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Daily DD Limit:  `{limit_pct:.0f}%`\n"
        f"Status:          `{status}`\n"
        f"⏰ `{_now_str()}`",
        parse_mode="Markdown",
    )


# ── /ping ─────────────────────────────────────────────────────
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ตรวจว่า bot ยังมีชีวิตอยู่ + latency"""
    if not _authorized(update):
        return

    t_recv     = datetime.now(timezone.utc)
    latency_ms = int(
        (t_recv - update.message.date.replace(tzinfo=timezone.utc)).total_seconds() * 1000
    )
    sessions    = _current_sessions()
    paused_text = "⏸ PAUSED" if _is_paused() else "▶️ RUNNING"

    await update.message.reply_text(
        f"🏓 *Pong!*\n\n"
        f"Latency:  `{latency_ms} ms`\n"
        f"Session:  `{' + '.join(sessions)}`\n"
        f"Bot:      `{paused_text}`\n"
        f"⏰ `{_now_str()}`",
        parse_mode="Markdown",
    )


# ── /uptime ───────────────────────────────────────────────────
async def cmd_uptime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot process ทำงานมานานแค่ไหนแล้ว"""
    if not _authorized(update):
        return

    delta   = datetime.now(timezone.utc) - _BOT_START_TIME
    days    = delta.days
    hours   = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    seconds = delta.seconds % 60
    started = _BOT_START_TIME.strftime("%Y-%m-%d %H:%M UTC")

    await update.message.reply_text(
        f"⏱ *Bot Uptime*\n\n"
        f"Running: `{days}d {hours:02d}h {minutes:02d}m {seconds:02d}s`\n"
        f"Started: `{started}`\n"
        f"⏰ `{_now_str()}`",
        parse_mode="Markdown",
    )


# ── /gold ─────────────────────────────────────────────────────
async def cmd_gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ราคาทองแบบ real-time จาก MT5"""
    if not _authorized(update):
        return

    try:
        import MetaTrader5 as mt5
        # ✅ FIX: ใช้ resolve_symbol() แทน hardcode "XAUUSDm" — เปลี่ยน
        # broker แล้วจะยังหา symbol เจอ ไม่ต้องแก้ไฟล์นี้
        mt5_symbol = resolve_symbol("XAUUSD")
        tick = mt5.symbol_info_tick(mt5_symbol)
        if tick is None:
            raise ValueError(f"{mt5_symbol} tick not available")

        spread_usd = round(tick.ask - tick.bid, 2)
        mid        = (tick.bid + tick.ask) / 2

        await update.message.reply_text(
            f"💛 *XAU/USD (Gold)*\n\n"
            f"Bid:    `${tick.bid:,.2f}`\n"
            f"Ask:    `${tick.ask:,.2f}`\n"
            f"Mid:    `${mid:,.2f}`\n"
            f"Spread: `${spread_usd:.2f}`\n"
            f"⏰ `{_now_str()}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ ดึงราคาไม่ได้\n`{str(e)[:120]}`\n"
            f"_(MT5 ต้องเชื่อมต่ออยู่)_",
            parse_mode="Markdown",
        )


# ── /spread ───────────────────────────────────────────────────
async def cmd_spread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Spread ปัจจุบันของทุก symbol"""
    if not _authorized(update):
        return

    # ✅ FIX: path เดิม CFG['trading']['symbols'] ไม่มีจริงใน config.yaml
    # (ไม่มี key 'trading' เลย) → เคย fallback ไป ['XAUUSDm'] ตัวเดียว
    # เสมอ ไม่สนใจว่า config มี EURUSD/GBPUSD ด้วย แก้ path ให้ถูก
    symbols = CFG.get('symbols', {}).get('active', ['XAUUSD'])
    try:
        import MetaTrader5 as mt5
        lines = ["📊 *Current Spreads*\n"]
        for sym in symbols:
            # ✅ FIX: ต้อง resolve เป็นชื่อ broker ก่อนถาม MT5 เสมอ
            mt5_symbol = resolve_symbol(sym)
            tick = mt5.symbol_info_tick(mt5_symbol)
            if tick:
                spread = round(tick.ask - tick.bid, 5)
                lines.append(f"  `{sym:<10}` spread = `{spread:.5f}`")
            else:
                lines.append(f"  `{sym:<10}` — N/A")
        lines.append(f"\n⏰ `{_now_str()}`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(
            f"❌ ดึง spread ไม่ได้\n`{str(e)[:120]}`",
            parse_mode="Markdown",
        )


# ── /session ──────────────────────────────────────────────────
async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Session ตลาดที่เปิดอยู่ตอนนี้"""
    if not _authorized(update):
        return

    # ✅ FIX: เดิม hardcode session hours เองในไฟล์นี้ (Sydney/Tokyo/London/
    # New York คงที่ ไม่ปรับ DST เลย) ทำให้ช่วง EST (ฤดูหนาว) NY จริงๆ เปิด
    # 13:00-22:00 UTC แต่คำสั่งนี้ยังโชว์ 12:00-21:00 ตลอดปีเหมือนเดิม
    # risk_manager.get_all_session_info() ถูกเขียนมาเพื่อ /session นี้
    # โดยเฉพาะอยู่แล้ว (ดู docstring ของมัน) แต่ไม่เคยถูกเรียกจากที่นี่เลย —
    # เปลี่ยนมาใช้แหล่งเดียวกับที่ risk_manager ใช้ตัดสินใจจริง กัน /session
    # โชว์ข้อมูลไม่ตรงกับพฤติกรรมบอทจริงในช่วง DST เปลี่ยน
    from bot.risk_manager import RiskManager
    risk     = RiskManager()
    sessions = risk.get_all_session_info()
    by_name  = {s.name: s for s in sessions}

    _PAIRS = {
        "Sydney"  : "AUD NZD",
        "Tokyo"   : "JPY AUD CHF",
        "London"  : "EUR GBP Gold",
        "New York": "USD Gold",
    }

    lines = ["🌍 *Market Sessions (UTC)*\n"]
    for s in sessions:
        status = "🟢 OPEN " if s.is_open else "⚫ closed"
        lines.append(f"  {s.emoji} *{s.name}*  {status}")
        lines.append(f"     {s.range_str} | {_PAIRS.get(s.name, '')}")

    # Overlap คำนวณจากเวลาเปิด/ปิดจริง (DST-adjusted) แทนเลข 12/16/0/9 hardcode
    hour   = datetime.now(timezone.utc).hour
    london = by_name.get("London")
    tokyo  = by_name.get("Tokyo")
    ny     = by_name.get("New York")
    if (london and ny
            and london.open_utc <= hour < london.close_utc
            and ny.open_utc     <= hour < ny.close_utc):
        lines.append(f"\n⚡ *London + NY Overlap* — liquidity สูงสุด (Gold ชอบช่วงนี้)")
    elif (tokyo and london
            and tokyo.open_utc  <= hour < tokyo.close_utc
            and london.open_utc <= hour < london.close_utc):
        lines.append(f"\n⚡ *Tokyo + London Overlap*")

    lines.append(f"\n⏰ `{_now_str()}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /quote ────────────────────────────────────────────────────
async def cmd_quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """สุ่ม trading wisdom quote"""
    if not _authorized(update):
        return

    text, author = random.choice(_QUOTES)
    await update.message.reply_text(
        f"💭 *Trading Wisdom*\n\n"
        f"_{text}_\n\n"
        f"— {author}",
        parse_mode="Markdown",
    )


# ── /flip ─────────────────────────────────────────────────────
async def cmd_flip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """AI coin flip BUY/SELL (เพื่อความสนุก เท่านั้น)"""
    if not _authorized(update):
        return

    result     = random.choice(["BUY  📈", "SELL 📉"])
    confidence = random.randint(51, 99)
    reasons    = [
        "RSI divergence ชัดมาก",
        "MACD crossover",
        "Price action เด่นมาก",
        "Volume spike ผิดปกติ",
        "Fibonacci 61.8% retest",
        "Moving average crossover",
        "Bollinger Band squeeze",
        "Order block พอดีเป๊ะ",
        "เห็นในฝัน",
        "ตับบอก",
    ]

    await update.message.reply_text(
        f"🪙 *AI Signal Generator™*\n\n"
        f"Signal:     `{result}`\n"
        f"Confidence: `{confidence}%`\n"
        f"Reason:     _{random.choice(reasons)}_\n\n"
        f"⚠️ _สัญญาณนี้ไม่มีความหมายทางการเงินใดๆ_\n"
        f"_เป็นแค่ coin flip เพื่อความสนุก_ 🎲",
        parse_mode="Markdown",
    )


# ── /help ─────────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """รายการคำสั่งทั้งหมด"""
    if not _authorized(update):
        return

    await update.message.reply_text(
        f"🤖 *AURUM BOT Commands*\n\n"
        f"*ดูข้อมูล:*\n"
        f"  /status      → สถานะรวมทั้งหมด\n"
        f"  /balance     → balance + margin\n"
        f"  /positions   → open positions\n"
        f"  /trades N    → N trades ล่าสุด\n"
        f"  /pnl         → P&L วัน/สัปดาห์/เดือน\n"
        f"  /risk        → risk settings\n"
        f"  /dd          → drawdown ปัจจุบัน\n\n"
        f"*ตลาด:*\n"
        f"  /gold        → ราคาทอง real-time\n"
        f"  /spread      → spread ทุก symbol\n"
        f"  /session     → session ที่เปิดอยู่\n\n"
        f"*ควบคุม:*\n"
        f"  /pause       → หยุดเทรดชั่วคราว\n"
        f"  /resume      → เปิดเทรดต่อ\n"
        f"  /closeall    → ปิดทุก position\n\n"
        f"*Reset (ใช้เมื่อ circuit breaker ทำงาน):*\n"
        f"  /reset\\_risk  → Reset ทุกอย่าง + ปลดล็อก\n"
        f"  /reset\\_daily → Reset เฉพาะ daily loss\n"
        f"  /reset\\_cb    → Reset เฉพาะ consecutive losses\n\n"
        f"*เบ็ดเตล็ด:*\n"
        f"  /ping        → ตรวจ bot alive + latency\n"
        f"  /uptime      → bot รันมานานแค่ไหน\n"
        f"  /quote       → trading wisdom\n"
        f"  /flip        → AI signal 🎲\n"
        f"  /help        → แสดง menu นี้\n\n"
        f"⚠️ คำสั่งทำงานเฉพาะ chat นี้เท่านั้น",
        parse_mode="Markdown",
    )


# ── Unknown command ───────────────────────────────────────────
async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        "❓ ไม่รู้จักคำสั่งนี้\n"
        "ใช้ /help เพื่อดูรายการคำสั่ง"
    )


# ══════════════════════════════════════════════════════════════
# Bot Setup & Run
# ══════════════════════════════════════════════════════════════
def run_telegram_bot():
    """
    เริ่มต้น Telegram Bot
    รันเป็น background thread ใน bot/main.py
    หรือรันแยกต่างหากเป็น service
    """
    if not TOKEN or not CHAT_ID:
        log.warning(
            "Telegram bot disabled: "
            "TOKEN หรือ CHAT_ID ไม่ได้ตั้งค่า"
        )
        return

    log.info("Starting Telegram command bot...")

    app = Application.builder().token(TOKEN).build()

    handlers = [
        # ── ดูข้อมูล ──────────────────────────────
        ("status",       cmd_status),
        ("balance",      cmd_balance),
        ("positions",    cmd_positions),
        ("trades",       cmd_trades),
        ("pnl",          cmd_pnl),
        ("risk",         cmd_risk),
        ("dd",           cmd_dd),
        # ── ตลาด ──────────────────────────────────
        ("gold",         cmd_gold),
        ("spread",       cmd_spread),
        ("session",      cmd_session),
        # ── ควบคุม ────────────────────────────────
        ("pause",        cmd_pause),
        ("resume",       cmd_resume),
        ("closeall",     cmd_closeall),
        ("confirmclose", cmd_confirmclose),
        # ── Reset ─────────────────────────────────
        ("reset_risk",   cmd_reset_risk),
        ("reset_daily",  cmd_reset_daily),
        ("reset_cb",     cmd_reset_cb),
        # ── เบ็ดเตล็ด ──────────────────────────────
        ("ping",         cmd_ping),
        ("uptime",       cmd_uptime),
        ("quote",        cmd_quote),
        ("flip",         cmd_flip),
        ("help",         cmd_help),
    ]

    for cmd, handler in handlers:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    async def _set_commands(application):
        await application.bot.set_my_commands([
            BotCommand("status",      "สถานะรวม account + positions"),
            BotCommand("balance",     "Balance + margin details"),
            BotCommand("positions",   "Open positions"),
            BotCommand("trades",      "Trades ล่าสุด"),
            BotCommand("pnl",         "P&L วัน/สัปดาห์/เดือน"),
            BotCommand("risk",        "Risk settings"),
            BotCommand("dd",          "Drawdown ปัจจุบัน"),
            BotCommand("gold",        "ราคาทอง real-time"),
            BotCommand("spread",      "Spread ทุก symbol"),
            BotCommand("session",     "Session ตลาดที่เปิดอยู่"),
            BotCommand("pause",       "หยุดเทรดชั่วคราว"),
            BotCommand("resume",      "เปิดเทรดต่อ"),
            BotCommand("closeall",    "ปิดทุก position"),
            BotCommand("reset_risk",  "Reset ทุกอย่าง + ปลดล็อก"),
            BotCommand("reset_daily", "Reset daily loss counter"),
            BotCommand("reset_cb",    "Reset consecutive loss counter"),
            BotCommand("ping",        "ตรวจ bot alive + latency"),
            BotCommand("uptime",      "Bot รันมานานแค่ไหน"),
            BotCommand("quote",       "Trading wisdom"),
            BotCommand("flip",        "AI signal (เล่นๆ)"),
            BotCommand("help",        "รายการคำสั่ง"),
        ])

    app.post_init = _set_commands

    log.info("✅ Telegram bot ready — polling...")
    app.run_polling(
        allowed_updates      = ["message"],
        drop_pending_updates = True,
    )


if __name__ == "__main__":
    run_telegram_bot()