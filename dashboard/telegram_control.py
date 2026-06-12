# dashboard/telegram_bot.py
"""
Telegram Command Bot — สั่งงาน Trading Bot จากมือถือ
════════════════════════════════════════════════════════════
Commands:
  /status      → ดูสถานะ account + open positions
  /closeall    → ปิดทุก position ทันที
  /pause       → หยุดเทรดชั่วคราว (บอทยังรันอยู่)
  /resume      → เปิดเทรดต่อ
  /pnl         → P&L วันนี้ + สัปดาห์นี้ + เดือนนี้
  /positions   → รายการ open positions
  /trades N    → ดู N trades ล่าสุด
  /balance     → balance + equity + margin
  /risk        → risk settings ปัจจุบัน
  /help        → รายการคำสั่งทั้งหมด

Security:
  รับคำสั่งจาก chat_id ที่กำหนดเท่านั้น
  ป้องกันคนอื่น access บอท
════════════════════════════════════════════════════════════
"""

import logging
import os
import json
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
log = logging.getLogger("dashboard.telegram_bot")

with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

DB_PATH      = Path(CFG['paths']['db'])
FLAGS_DIR    = Path(CFG['paths']['flags'])
ACCOUNT_JSON = Path(CFG['paths']['logs']) / "account.json"

TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ══════════════════════════════════════════════════════════════
# Security Guard
# ══════════════════════════════════════════════════════════════
def _authorized(update: Update) -> bool:
    """
    ตรวจว่า message มาจาก chat_id ที่อนุญาตเท่านั้น
    ถ้าไม่ใช่ → ไม่ทำอะไร + log warning
    """
    chat_id = str(update.effective_chat.id)

    if chat_id != str(CHAT_ID):
        log.warning(
            f"Unauthorized access attempt: "
            f"chat_id={chat_id} "
            f"user={update.effective_user.username}"
        )
        return False
    return True


# ══════════════════════════════════════════════════════════════
# Helper Functions
# ══════════════════════════════════════════════════════════════
def _load_account() -> dict:
    """โหลด account snapshot ล่าสุด"""
    try:
        return json.loads(ACCOUNT_JSON.read_text())
    except Exception:
        return {}


def _load_trades(days_back: int = 1) -> list:
    """โหลด closed trades จาก SQLite"""
    if not DB_PATH.exists():
        return []

    conn  = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows  = conn.execute("""
        SELECT symbol, direction, volume,
               open_price, close_price,
               profit, close_time, ticket
        FROM   trades
        WHERE  close_time IS NOT NULL
          AND  close_time >= datetime('now', ?)
        ORDER  BY close_time DESC
        LIMIT  50
    """, (f"-{days_back} days",)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_open_positions() -> list:
    """ดึง open positions จาก MT5"""
    try:
        import MetaTrader5 as mt5
        positions = mt5.positions_get() or []
        return [
            {
                'ticket' : p.ticket,
                'symbol' : p.symbol,
                'type'   : 'BUY' if p.type == 0 else 'SELL',
                'volume' : p.volume,
                'price'  : p.price_open,
                'profit' : p.profit,
                'sl'     : p.sl,
                'tp'     : p.tp,
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
        FLAGS_DIR.mkdir(exist_ok=True)
        pause_file.touch()
    else:
        if pause_file.exists():
            pause_file.unlink()


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _pnl_summary(days_back: int) -> dict:
    """คำนวณ P&L summary จาก DB"""
    if not DB_PATH.exists():
        return {'trades': 0, 'pnl': 0, 'wins': 0, 'losses': 0}

    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute("""
        SELECT
            COUNT(*)                                        AS trades,
            COALESCE(SUM(profit), 0)                        AS pnl,
            SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END)    AS wins,
            SUM(CASE WHEN profit <= 0 THEN 1 ELSE 0 END)   AS losses
        FROM trades
        WHERE  close_time IS NOT NULL
          AND  close_time >= datetime('now', ?)
    """, (f"-{days_back} days",)).fetchone()
    conn.close()

    if row is None:
        return {'trades': 0, 'pnl': 0, 'wins': 0, 'losses': 0}

    return {
        'trades' : row[0] or 0,
        'pnl'    : round(row[1] or 0, 2),
        'wins'   : row[2] or 0,
        'losses' : row[3] or 0,
    }


# ══════════════════════════════════════════════════════════════
# Command Handlers
# ══════════════════════════════════════════════════════════════

# ── /status ──────────────────────────────────────────────────
async def cmd_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    แสดงสถานะรวมของบอท
    account + open positions + pause status + today P&L
    """
    if not _authorized(update):
        return

    acc       = _load_account()
    positions = _get_open_positions()
    paused    = _is_paused()
    today     = _pnl_summary(days_back=1)

    # Status icon
    status_icon = "⏸ PAUSED" if paused else "▶️ RUNNING"
    balance     = acc.get('balance',    0)
    equity      = acc.get('equity',     0)
    profit      = acc.get('profit',     0)
    free_margin = acc.get('free_margin',0)

    # Open positions summary
    if positions:
        pos_lines = []
        for p in positions:
            icon = "↑" if p['type'] == "BUY" else "↓"
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

    await update.message.reply_text(
        msg, parse_mode="Markdown"
    )


# ── /closeall ─────────────────────────────────────────────────
async def cmd_closeall(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    ปิด position ทั้งหมดทันที
    ต้องยืนยันก่อน (2 ขั้นตอน)
    """
    if not _authorized(update):
        return

    positions = _get_open_positions()

    if not positions:
        await update.message.reply_text(
            "ℹ️ ไม่มี open position ที่จะปิด"
        )
        return

    # แสดงรายการก่อนยืนยัน
    total_profit = sum(p['profit'] for p in positions)
    lines        = [
        f"  {'↑' if p['type']=='BUY' else '↓'} "
        f"`{p['symbol']}` lot={p['volume']:.2f} "
        f"${p['profit']:+.2f}"
        for p in positions
    ]

    confirm_msg = (
        f"⚠️ *Confirm Close All*\n\n"
        f"จะปิด *{len(positions)} positions*:\n"
        f"{chr(10).join(lines)}\n\n"
        f"Floating P&L: `${total_profit:+.2f}`\n\n"
        f"ยืนยันด้วยคำสั่ง `/confirmclose`"
    )

    # บันทึก state รอยืนยัน
    context.user_data['pending_closeall'] = True
    context.user_data['pending_timeout']  = (
        datetime.now(timezone.utc) + timedelta(seconds=30)
    )

    await update.message.reply_text(
        confirm_msg, parse_mode="Markdown"
    )


async def cmd_confirmclose(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """ยืนยันการปิด position ทั้งหมด"""
    if not _authorized(update):
        return

    # ตรวจ pending state
    pending  = context.user_data.get('pending_closeall', False)
    timeout  = context.user_data.get('pending_timeout')

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

    # ดำเนินการปิด
    await update.message.reply_text("🔄 กำลังปิด positions...")

    try:
        from bot.mt5_client  import MT5Client
        from bot.risk_manager import RiskManager
        from bot.executor    import OrderExecutor

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


# ── /pause ────────────────────────────────────────────────────
async def cmd_pause(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    หยุดเทรดชั่วคราว
    บอทยังรันอยู่แต่ไม่เปิด order ใหม่
    positions ที่เปิดอยู่ยังคงอยู่
    """
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
        if positions else
        "ไม่มี open positions"
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
async def cmd_resume(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """เปิดเทรดต่อหลัง pause"""
    if not _authorized(update):
        return

    if not _is_paused():
        await update.message.reply_text(
            "ℹ️ Bot ทำงานอยู่แล้ว ไม่ได้ถูก pause"
        )
        return

    _set_pause(False)
    acc = _load_account()

    await update.message.reply_text(
        f"▶️ *Bot Resumed*\n\n"
        f"บอทกลับมาเทรดแล้ว\n"
        f"Balance: `${acc.get('balance',0):,.2f}`\n"
        f"⏰ `{_now_str()}`",
        parse_mode="Markdown",
    )

    log.info("Bot resumed via Telegram")


# ── /pnl ──────────────────────────────────────────────────────
async def cmd_pnl(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """แสดง P&L หลายช่วงเวลา"""
    if not _authorized(update):
        return

    today  = _pnl_summary(days_back=1)
    week   = _pnl_summary(days_back=7)
    month  = _pnl_summary(days_back=30)
    total  = _pnl_summary(days_back=3650)

    def _wr(d: dict) -> str:
        t = d['trades']
        if t == 0:
            return "N/A"
        return f"{d['wins']/t*100:.0f}%"

    def _icon(pnl: float) -> str:
        return "📈" if pnl >= 0 else "📉"

    msg = (
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
        f"⏰ `{_now_str()}`"
    )

    await update.message.reply_text(
        msg, parse_mode="Markdown"
    )


# ── /positions ────────────────────────────────────────────────
async def cmd_positions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """รายละเอียด open positions ทุกตัว"""
    if not _authorized(update):
        return

    positions = _get_open_positions()

    if not positions:
        await update.message.reply_text(
            "ℹ️ ไม่มี open positions"
        )
        return

    lines = ["📋 *Open Positions*\n"]
    total_profit = 0

    for p in positions:
        arrow  = "↑" if p['type'] == "BUY" else "↓"
        icon   = "🟢" if p['profit'] >= 0 else "🔴"
        total_profit += p['profit']
        lines.append(
            f"{icon} {arrow} *{p['symbol']}* "
            f"#{p['ticket']}\n"
            f"  Lot:    `{p['volume']:.2f}`\n"
            f"  Entry:  `{p['price']:.5f}`\n"
            f"  SL:     `{p['sl']:.5f}`\n"
            f"  TP:     `{p['tp']:.5f}`\n"
            f"  P&L:    `${p['profit']:+.2f}`\n"
        )

    lines.append(
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Total Floating: "
        f"`${total_profit:+.2f}`\n"
        f"⏰ `{_now_str()}`"
    )

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )


# ── /trades ───────────────────────────────────────────────────
async def cmd_trades(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    ดู trades ล่าสุด
    /trades    → 5 trades ล่าสุด
    /trades 10 → 10 trades ล่าสุด
    """
    if not _authorized(update):
        return

    # Parse จำนวน trades ที่ต้องการ
    try:
        n = int(context.args[0]) if context.args else 5
        n = min(max(n, 1), 20)   # จำกัด 1-20
    except (ValueError, IndexError):
        n = 5

    trades = _load_trades(days_back=30)[:n]

    if not trades:
        await update.message.reply_text(
            "ℹ️ ยังไม่มี trade ใน 30 วันล่าสุด"
        )
        return

    lines = [f"📋 *{n} Trades ล่าสุด*\n"]

    for t in trades:
        icon   = "💚" if (t['profit'] or 0) >= 0 else "🔴"
        arrow  = "↑" if t['direction'] == "BUY" else "↓"
        time_str = t['close_time'][:16] if t['close_time'] else "?"
        lines.append(
            f"{icon} {arrow} `{t['symbol']}` "
            f"lot={t['volume']:.2f} "
            f"*${(t['profit'] or 0):+.2f}*\n"
            f"  `{time_str}`"
        )

    total = sum(t['profit'] or 0 for t in trades)
    lines.append(
        f"\nTotal: `${total:+.2f}`\n"
        f"⏰ `{_now_str()}`"
    )

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )


# ── /balance ──────────────────────────────────────────────────
async def cmd_balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Balance, equity, margin details"""
    if not _authorized(update):
        return

    acc = _load_account()

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
        f"Floating P&L: `${acc.get('profit',0):+.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Margin Used:  `${margin:,.2f}`\n"
        f"Free Margin:  `${free_margin:,.2f}`\n"
        f"{margin_icon} Margin Level: "
        f"`{margin_level:.0f}%`\n"
        f"Leverage:     `1:{leverage}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏰ `{_now_str()}`",
        parse_mode="Markdown",
    )


# ── /risk ─────────────────────────────────────────────────────
async def cmd_risk(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Risk settings ปัจจุบัน"""
    if not _authorized(update):
        return

    r = CFG['risk']
    s = CFG['signal']

    paused_text = "⏸ PAUSED" if _is_paused() else "▶️ ACTIVE"

    await update.message.reply_text(
        f"⚙️ *Risk Settings*\n\n"
        f"Bot Status:     `{paused_text}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Risk/Trade:     "
        f"`{r['risk_per_trade']:.1%}`\n"
        f"Max Daily Loss: "
        f"`{r['max_daily_loss_pct']:.1%}`\n"
        f"Max Trades:     "
        f"`{r['max_open_trades']}`\n"
        f"Min Confidence: "
        f"`{s['min_confidence']:.0%}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"SL Points:\n"
        f"  XAUUSD: "
        f"`{r['max_spread_points'].get('XAUUSD',30)}`\n"
        f"  EURUSD: "
        f"`{r['max_spread_points'].get('EURUSD',15)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Strategy: `v{CFG.get('model',{}).get('active','?')}`\n"
        f"⏰ `{_now_str()}`",
        parse_mode="Markdown",
    )


# ── /help ─────────────────────────────────────────────────────
async def cmd_help(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """รายการคำสั่งทั้งหมด"""
    if not _authorized(update):
        return

    await update.message.reply_text(
        f"🤖 *AURUM BOT Commands*\n\n"
        f"*ดูข้อมูล:*\n"
        f"  /status    → สถานะรวมทั้งหมด\n"
        f"  /balance   → balance + margin\n"
        f"  /positions → open positions\n"
        f"  /trades N  → N trades ล่าสุด\n"
        f"  /pnl       → P&L วัน/สัปดาห์/เดือน\n"
        f"  /risk      → risk settings\n\n"
        f"*ควบคุม:*\n"
        f"  /pause     → หยุดเทรดชั่วคราว\n"
        f"  /resume    → เปิดเทรดต่อ\n"
        f"  /closeall  → ปิดทุก position\n\n"
        f"*อื่นๆ:*\n"
        f"  /help      → แสดง menu นี้\n\n"
        f"⚠️ คำสั่งทำงานเฉพาะ chat นี้เท่านั้น",
        parse_mode="Markdown",
    )


# ── Unknown command ───────────────────────────────────────────
async def cmd_unknown(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
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

    # Build application
    app = Application.builder().token(TOKEN).build()

    # Register commands
    handlers = [
        ("status",       cmd_status),
        ("closeall",     cmd_closeall),
        ("confirmclose", cmd_confirmclose),
        ("pause",        cmd_pause),
        ("resume",       cmd_resume),
        ("pnl",          cmd_pnl),
        ("positions",    cmd_positions),
        ("trades",       cmd_trades),
        ("balance",      cmd_balance),
        ("risk",         cmd_risk),
        ("help",         cmd_help),
    ]

    for cmd, handler in handlers:
        app.add_handler(CommandHandler(cmd, handler))

    # Unknown command handler
    app.add_handler(
        MessageHandler(filters.COMMAND, cmd_unknown)
    )

    # Set bot commands (แสดงใน Telegram menu)
    async def _set_commands(application):
        await application.bot.set_my_commands([
            BotCommand("status",    "สถานะรวม account + positions"),
            BotCommand("balance",   "Balance + margin details"),
            BotCommand("positions", "Open positions"),
            BotCommand("trades",    "Trades ล่าสุด"),
            BotCommand("pnl",       "P&L วัน/สัปดาห์/เดือน"),
            BotCommand("risk",      "Risk settings"),
            BotCommand("pause",     "หยุดเทรดชั่วคราว"),
            BotCommand("resume",    "เปิดเทรดต่อ"),
            BotCommand("closeall",  "ปิดทุก position"),
            BotCommand("help",      "รายการคำสั่ง"),
        ])

    app.post_init = _set_commands

    log.info("✅ Telegram bot ready — polling...")
    app.run_polling(
        allowed_updates = ["message"],
        drop_pending_updates = True,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level  = logging.INFO,
        format = "%(asctime)s [%(levelname)s] %(message)s",
    )
    run_telegram_bot()