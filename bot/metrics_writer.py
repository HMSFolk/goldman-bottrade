<<<<<<< HEAD
# bot/metrics_writer.py
"""
Metrics Writer — บันทึก Trade และ Account ลง SQLite
════════════════════════════════════════════════════════════
ใช้ SQLite เพราะ:
  - ไม่ต้องติดตั้ง service เพิ่ม (ต่างจาก InfluxDB)
  - ไฟล์เดียว — backup ง่าย (copy db/trades.db)
  - Streamlit อ่านได้โดยตรงผ่าน pandas
  - เร็วพอสำหรับ 100 trades/วัน

Schema:
  trades            ← ทุก order (open + closed)
  account_snapshots ← balance/equity ทุก tick
  signals           ← ML signal ทุก bar
  daily_summary     ← สรุปรายวัน
════════════════════════════════════════════════════════════
"""

import logging
import sqlite3
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

import pandas as pd

log = logging.getLogger("bot.metrics_writer")

# ✅ FIX BUG-7+9: ใช้ get_config() แทน open(config.yaml) โดยตรง
#    และย้าย path constants มาเป็น lazy เพื่อให้แน่ใจว่า config โหลดแล้ว
from config import get_config

def _get_db_path() -> Path:
    """Lazy path resolution — อ่านจาก config ทุกครั้ง"""
    return Path(get_config()['paths']['db'])

def _get_log_dir() -> Path:
    return Path(get_config()['paths']['logs'])

# Module-level shortcuts (resolve ตอน import — ปลอดภัยเพราะ config.py ทำงานแล้ว)
_CFG         = get_config()
DB_PATH      = Path(_CFG['paths']['db'])
LOG_DIR      = Path(_CFG['paths']['logs'])
ACCOUNT_JSON = LOG_DIR / "account.json"


# ══════════════════════════════════════════════════════════════
# Database Initialization
# ══════════════════════════════════════════════════════════════
def init_db():
    """
    สร้าง database และ tables ถ้ายังไม่มี
    เรียกครั้งเดียวตอน startup
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    with _get_conn() as conn:
        conn.executescript("""
            -- ── trades ──────────────────────────────────────
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket       INTEGER UNIQUE,        -- MT5 ticket
                open_time    TEXT    NOT NULL,
                close_time   TEXT,                  -- NULL = ยังเปิดอยู่
                symbol       TEXT    NOT NULL,
                direction    TEXT    NOT NULL,       -- BUY / SELL
                volume       REAL    NOT NULL,
                open_price   REAL    NOT NULL,
                close_price  REAL,
                sl           REAL,
                tp           REAL,
                profit       REAL,                  -- NULL = ยังไม่ปิด
                swap         REAL    DEFAULT 0,
                commission   REAL    DEFAULT 0,
                confidence   REAL    DEFAULT 0,
                comment      TEXT    DEFAULT '',
                strategy_ver TEXT    DEFAULT 'v1',
                created_at   TEXT    DEFAULT (datetime('now'))
            );

            -- ── account snapshots ──────────────────────────
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT    NOT NULL,
                balance      REAL    NOT NULL,
                equity       REAL    NOT NULL,
                margin       REAL    DEFAULT 0,
                free_margin  REAL    DEFAULT 0,
                margin_level REAL    DEFAULT 0,
                profit       REAL    DEFAULT 0,
                open_trades  INTEGER DEFAULT 0
            );

            -- ── signals ────────────────────────────────────
            CREATE TABLE IF NOT EXISTS signals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT    NOT NULL,
                symbol       TEXT    NOT NULL,
                signal       INTEGER NOT NULL,      -- -1/0/1
                confidence   REAL    DEFAULT 0,
                proba_sell   REAL    DEFAULT 0,
                proba_hold   REAL    DEFAULT 0,
                proba_buy    REAL    DEFAULT 0,
                regime       TEXT    DEFAULT '',
                session      TEXT    DEFAULT ''
            );

            -- ── daily summary ───────────────────────────────
            CREATE TABLE IF NOT EXISTS daily_summary (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT    UNIQUE NOT NULL,
                start_balance REAL   DEFAULT 0,
                end_balance  REAL    DEFAULT 0,
                pnl          REAL    DEFAULT 0,
                trades       INTEGER DEFAULT 0,
                wins         INTEGER DEFAULT 0,
                losses       INTEGER DEFAULT 0,
                win_rate     REAL    DEFAULT 0,
                profit_factor REAL   DEFAULT 0,
                max_drawdown REAL    DEFAULT 0
            );

            -- ── Indexes ─────────────────────────────────────
            CREATE INDEX IF NOT EXISTS idx_trades_symbol
                ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_open_time
                ON trades(open_time);
            CREATE INDEX IF NOT EXISTS idx_trades_close_time
                ON trades(close_time);
            CREATE INDEX IF NOT EXISTS idx_account_ts
                ON account_snapshots(ts);
            CREATE INDEX IF NOT EXISTS idx_signals_ts
                ON signals(ts);
            CREATE INDEX IF NOT EXISTS idx_signals_symbol
                ON signals(symbol);
        """)

    log.info(f"✅ Database initialized: {DB_PATH}")


# ══════════════════════════════════════════════════════════════
# Connection Manager
# ══════════════════════════════════════════════════════════════
@contextmanager
def _get_conn():
    """
    Context manager สำหรับ SQLite connection
    auto-commit และ auto-close เสมอ
    ตั้ง timeout=10 วินาที (ป้องกัน lock จาก Streamlit)
    """
    conn = sqlite3.connect(
        DB_PATH,
        timeout    = 10,
        check_same_thread = False,
    )
    conn.row_factory = sqlite3.Row   # dict-like access
    conn.execute("PRAGMA journal_mode=WAL")   # ป้องกัน lock
    conn.execute("PRAGMA synchronous=NORMAL")

    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"DB error: {e}", exc_info=True)
        raise
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# MetricsWriter Class
# ══════════════════════════════════════════════════════════════
class MetricsWriter:
    """
    จัดการการเขียนข้อมูลทุกประเภทลง SQLite

    วิธีใช้:
        writer = MetricsWriter()
        writer.write_trade({...})
        writer.write_account({...})
        writer.write_signal("XAUUSD", 1, 0.73)
    """

    def __init__(self):
        self._snap_count    = 0
        self._snap_interval = 4   # บันทึก account ทุก 4 tick (1 ชั่วโมง)
        log.info(f"MetricsWriter → {DB_PATH}")

    # ══════════════════════════════════════════════════════════
    # Trade Recording
    # ══════════════════════════════════════════════════════════
    def write_trade(self, trade: dict):
        """
        บันทึก trade ลง DB

        trade dict ต้องมี:
            open_time, symbol, direction, volume,
            open_price, sl, tp, ticket, confidence

        ถ้ามี close_time และ close_price → update row เดิม
        ถ้าไม่มี → insert row ใหม่ (trade ยังเปิดอยู่)
        """
        ticket     = trade.get('ticket', 0)
        close_time = trade.get('close_time')

        if ticket and close_time:
            # Trade ปิดแล้ว — update row เดิม
            self._update_closed_trade(trade)
        else:
            # Trade เปิดใหม่ — insert
            self._insert_open_trade(trade)

    def _insert_open_trade(self, trade: dict):
        """Insert trade ที่เพิ่งเปิด"""
        # ✅ FIX BUG-8 CRITICAL: executor.py ส่ง key 'lot' แต่เดิมอ่าน 'volume'
        #    ทำให้ lot size ในฐานข้อมูลผิดทุก trade — fix ด้วยการรับทั้งสอง key
        volume = trade.get('volume') or trade.get('lot', 0.01)

        with _get_conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO trades
                (ticket, open_time, symbol, direction, volume,
                 open_price, sl, tp, confidence, comment,
                 strategy_ver)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade.get('ticket',     0),
                trade.get('open_time',  _now()),
                trade.get('symbol',     ''),
                trade.get('direction',  'BUY'),
                volume,                            # ✅ ใช้ volume ที่ resolve แล้ว
                trade.get('open_price', 0.0),
                trade.get('sl',         0.0),
                trade.get('tp',         0.0),
                trade.get('confidence', 0.0),
                trade.get('comment',    ''),
                trade.get('strategy_ver', 'v1'),
            ))

        log.debug(
            f"Inserted trade #{trade.get('ticket')} "
            f"{trade.get('symbol')} {trade.get('direction')}"
        )

    def _update_closed_trade(self, trade: dict):
        """Update trade ที่ปิดแล้ว — เพิ่ม close data"""
        with _get_conn() as conn:
            conn.execute("""
                UPDATE trades SET
                    close_time  = ?,
                    close_price = ?,
                    profit      = ?,
                    swap        = ?,
                    commission  = ?
                WHERE ticket = ?
            """, (
                trade.get('close_time',  _now()),
                trade.get('close_price', 0.0),
                trade.get('pnl',         0.0),
                trade.get('swap',        0.0),
                trade.get('commission',  0.0),
                trade.get('ticket',      0),
            ))

        log.debug(
            f"Updated trade #{trade.get('ticket')} "
            f"P&L={trade.get('pnl', 0):+.2f}"
        )

        # อัพเดต daily summary
        self._update_daily_summary(trade)

    # ══════════════════════════════════════════════════════════
    # Account Snapshot
    # ══════════════════════════════════════════════════════════
    def write_account(self, account: dict):
        """
        บันทึก account snapshot
        ไม่บันทึกทุก tick (throttle ด้วย _snap_interval)
        แต่บันทึก account.json ทุกครั้ง (Streamlit อ่าน)
        """
        # อัพเดต account.json ทุกครั้ง (เบา)
        try:
            ACCOUNT_JSON.write_text(
                json.dumps({
                    **account,
                    'updated_at': _now(),
                }),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning(f"account.json write error: {e}")

        # บันทึกลง DB ทุก n tick (ไม่ต้องบันทึกทุกครั้ง)
        self._snap_count += 1
        if self._snap_count % self._snap_interval != 0:
            return

        with _get_conn() as conn:
            conn.execute("""
                INSERT INTO account_snapshots
                (ts, balance, equity, margin, free_margin,
                 margin_level, profit, open_trades)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                _now(),
                account.get('balance',      0.0),
                account.get('equity',       0.0),
                account.get('margin',       0.0),
                account.get('free_margin',  0.0),
                account.get('margin_level', 0.0),
                account.get('profit',       0.0),
                account.get('open_trades',  0),
            ))

        log.debug(
            f"Account snapshot: "
            f"balance=${account.get('balance',0):,.2f} "
            f"equity=${account.get('equity',0):,.2f}"
        )

    # ══════════════════════════════════════════════════════════
    # Signal Recording
    # ══════════════════════════════════════════════════════════
    def write_signal(
        self,
        symbol:     str,
        signal:     int,
        confidence: float,
        proba_sell: float = 0.0,
        proba_hold: float = 0.0,
        proba_buy:  float = 0.0,
        regime:     str   = "",
        session:    str   = "",
    ):
        """บันทึก ML signal ทุก bar"""
        with _get_conn() as conn:
            conn.execute("""
                INSERT INTO signals
                (ts, symbol, signal, confidence,
                 proba_sell, proba_hold, proba_buy,
                 regime, session)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                _now(), symbol, signal, confidence,
                proba_sell, proba_hold, proba_buy,
                regime, session,
            ))

    # ══════════════════════════════════════════════════════════
    # Daily Summary
    # ══════════════════════════════════════════════════════════
    def _update_daily_summary(self, trade: dict):
        """อัพเดตสรุปรายวันหลังปิด trade"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with _get_conn() as conn:
            # ดึง trades วันนี้ทั้งหมด
            rows = conn.execute("""
                SELECT profit FROM trades
                WHERE date(close_time) = ?
                  AND profit IS NOT NULL
            """, (today,)).fetchall()

        if not rows:
            return

        profits       = [r['profit'] for r in rows]
        wins          = [p for p in profits if p > 0]
        losses        = [p for p in profits if p <= 0]
        total_pnl     = sum(profits)
        win_rate      = len(wins) / len(profits) if profits else 0
        profit_factor = (
            sum(wins) / abs(sum(losses))
            if losses else float('inf')
        )

        # คำนวณ max drawdown วันนี้
        cumulative = 0.0
        peak       = 0.0
        max_dd     = 0.0
        for p in profits:
            cumulative += p
            peak        = max(peak, cumulative)
            dd          = peak - cumulative
            max_dd      = max(max_dd, dd)

        with _get_conn() as conn:
            conn.execute("""
                INSERT INTO daily_summary
                    (date, pnl, trades, wins, losses,
                     win_rate, profit_factor, max_drawdown)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(date) DO UPDATE SET
                    pnl           = excluded.pnl,
                    trades        = excluded.trades,
                    wins          = excluded.wins,
                    losses        = excluded.losses,
                    win_rate      = excluded.win_rate,
                    profit_factor = excluded.profit_factor,
                    max_drawdown  = excluded.max_drawdown
            """, (
                today, total_pnl, len(profits),
                len(wins), len(losses),
                win_rate, profit_factor, max_dd,
            ))

    # ══════════════════════════════════════════════════════════
    # Query Methods (ใช้จาก dashboard)
    # ══════════════════════════════════════════════════════════
    def get_trades(
        self,
        days_back: int  = 30,
        symbol:    str  = None,
        closed_only:bool= True,
    ) -> pd.DataFrame:
        """โหลด trades เป็น DataFrame"""
        query  = """
            SELECT * FROM trades
            WHERE open_time >= datetime('now', ?)
        """
        params = [f"-{days_back} days"]

        if symbol:
            query  += " AND symbol = ?"
            params.append(symbol)

        if closed_only:
            query += " AND close_time IS NOT NULL"

        query += " ORDER BY close_time DESC"

        with _get_conn() as conn:
            df = pd.read_sql_query(query, conn, params=params)

        if not df.empty:
            for col in ['open_time','close_time']:
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col], utc=True)

        return df

    def get_equity_curve(
        self, days_back: int = 90
    ) -> pd.DataFrame:
        """Equity curve จาก account snapshots"""
        with _get_conn() as conn:
            df = pd.read_sql_query("""
                SELECT ts, balance, equity, profit, open_trades
                FROM account_snapshots
                WHERE ts >= datetime('now', ?)
                ORDER BY ts ASC
            """, conn, params=[f"-{days_back} days"])

        if not df.empty:
            df['ts'] = pd.to_datetime(df['ts'], utc=True)
            df.set_index('ts', inplace=True)

        return df

    def get_daily_summary(
        self, days_back: int = 30
    ) -> dict:
        """
        สรุป performance รวม
        ใช้ใน bot/main.py สำหรับ daily_summary notification
        """
        with _get_conn() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*)           AS trades,
                    SUM(profit)        AS total_pnl,
                    AVG(profit)        AS avg_trade,
                    SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END)
                                       AS wins,
                    MAX(profit)        AS best_trade,
                    MIN(profit)        AS worst_trade
                FROM trades
                WHERE close_time IS NOT NULL
                  AND close_time >= datetime('now', ?)
            """, (f"-{days_back} days",)).fetchone()

        if row is None or row['trades'] == 0:
            return {
                'trades': 0, 'total_pnl': 0.0,
                'win_rate': 0.0, 'avg_trade': 0.0,
            }

        return {
            'trades'     : row['trades'],
            'total_pnl'  : round(row['total_pnl'] or 0, 2),
            'wins'       : row['wins'],
            'win_rate'   : (row['wins'] / max(row['trades'], 1)),
            'avg_trade'  : round(row['avg_trade'] or 0, 2),
            'best_trade' : round(row['best_trade'] or 0, 2),
            'worst_trade': round(row['worst_trade'] or 0, 2),
        }

    def get_symbol_performance(self) -> pd.DataFrame:
        """P&L แยกตาม symbol"""
        with _get_conn() as conn:
            df = pd.read_sql_query("""
                SELECT
                    symbol,
                    COUNT(*)                              AS trades,
                    SUM(profit)                           AS total_pnl,
                    AVG(profit)                           AS avg_trade,
                    SUM(CASE WHEN profit > 0 THEN 1 END) AS wins,
                    ROUND(
                        100.0 *
                        SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END)
                        / COUNT(*), 1
                    )                                     AS win_rate_pct
                FROM trades
                WHERE profit IS NOT NULL
                GROUP BY symbol
                ORDER BY total_pnl DESC
            """, conn)
        return df

    def get_recent_signals(
        self,
        symbol:    str = None,
        limit:     int = 100,
    ) -> pd.DataFrame:
        """Signal ล่าสุดสำหรับ dashboard"""
        # ✅ FIX BUG-10: ใช้ parameterized query แทน f-string LIMIT
        query  = "SELECT * FROM signals"
        params: list = []
        if symbol:
            query  += " WHERE symbol = ?"
            params.append(symbol)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(max(1, int(limit)))   # clamp ป้องกัน negative

        with _get_conn() as conn:
            df = pd.read_sql_query(query, conn, params=params)
        return df

    # ══════════════════════════════════════════════════════════
    # Maintenance
    # ══════════════════════════════════════════════════════════
    def cleanup_old_data(self, keep_days: int = 180):
        """
        ลบข้อมูลเก่าเกิน keep_days วัน
        รันผ่าน Task Scheduler ทุกเดือน
        """
        cutoff = f"-{keep_days} days"
        with _get_conn() as conn:
            # ลบ account snapshots เก่า (เก็บแค่ 1 snapshot/ชั่วโมง)
            conn.execute("""
                DELETE FROM account_snapshots
                WHERE ts < datetime('now', ?)
            """, (cutoff,))

            # ลบ signals เก่า
            conn.execute("""
                DELETE FROM signals
                WHERE ts < datetime('now', ?)
            """, (cutoff,))

            # trades ไม่ลบ — เก็บไว้วิเคราะห์ตลอด

        log.info(f"Cleanup: ลบข้อมูลเก่ากว่า {keep_days} วัน")
        self._vacuum()

    def _vacuum(self):
        """VACUUM เพื่อคืนพื้นที่หลัง delete"""
        conn = sqlite3.connect(DB_PATH)
        conn.execute("VACUUM")
        conn.close()
        log.info("Database VACUUM complete")

    def export_csv(self, output_dir: str = "reports"):
        """Export ทุก table เป็น CSV"""
        Path(output_dir).mkdir(exist_ok=True)

        tables = [
            'trades', 'account_snapshots',
            'signals', 'daily_summary',
        ]
        for table in tables:
            with _get_conn() as conn:
                df = pd.read_sql_query(
                    f"SELECT * FROM {table}", conn
                )
            path = f"{output_dir}/{table}.csv"
            df.to_csv(path, index=False)
            log.info(f"Exported {table} → {path}")

    def get_db_stats(self) -> dict:
        """สถิติ database"""
        with _get_conn() as conn:
            stats = {}
            for table in ['trades','account_snapshots','signals']:
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                ).fetchone()
                stats[table] = row['n']

        stats['db_size_mb'] = round(
            DB_PATH.stat().st_size / 1_048_576, 2
        )
        return stats


# ══════════════════════════════════════════════════════════════
# Module-Level Functions (ใช้จาก executor โดยตรง)
# ══════════════════════════════════════════════════════════════
_writer_instance: Optional[MetricsWriter] = None

def _get_writer() -> MetricsWriter:
    """Lazy singleton"""
    global _writer_instance
    if _writer_instance is None:
        _writer_instance = MetricsWriter()
    return _writer_instance

def write_trade(trade: dict):
    """Shortcut — ใช้จาก executor.py"""
    _get_writer().write_trade(trade)

def write_account(account: dict):
    """Shortcut — ใช้จาก bot/main.py"""
    _get_writer().write_account(account)

def write_signal(
    symbol:     str,
    signal:     int,
    confidence: float,
    **kwargs,
):
    """Shortcut — ใช้จาก bot/main.py"""
    _get_writer().write_signal(symbol, signal, confidence, **kwargs)


# ── Utility ────────────────────────────────────────────────────
def _now() -> str:
    """Timestamp UTC เป็น ISO string"""
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--stats",   action="store_true")
    parser.add_argument("--export",  action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--vacuum",  action="store_true")
    args = parser.parse_args()

    init_db()
    w = MetricsWriter()

    if args.stats:
        stats = w.get_db_stats()
        print(f"\n── DB Stats ──")
        for k, v in stats.items():
            print(f"  {k:<25}: {v}")

    if args.export:
        w.export_csv()

    if args.cleanup:
        w.cleanup_old_data(keep_days=180)

    if args.vacuum:
=======
# bot/metrics_writer.py
"""
Metrics Writer — บันทึก Trade และ Account ลง SQLite
════════════════════════════════════════════════════════════
ใช้ SQLite เพราะ:
  - ไม่ต้องติดตั้ง service เพิ่ม (ต่างจาก InfluxDB)
  - ไฟล์เดียว — backup ง่าย (copy db/trades.db)
  - Streamlit อ่านได้โดยตรงผ่าน pandas
  - เร็วพอสำหรับ 100 trades/วัน

Schema:
  trades            ← ทุก order (open + closed)
  account_snapshots ← balance/equity ทุก tick
  signals           ← ML signal ทุก bar
  daily_summary     ← สรุปรายวัน
════════════════════════════════════════════════════════════
"""

import logging
import sqlite3
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

import pandas as pd

log = logging.getLogger("bot.metrics_writer")

# ✅ FIX BUG-7+9: ใช้ get_config() แทน open(config.yaml) โดยตรง
#    และย้าย path constants มาเป็น lazy เพื่อให้แน่ใจว่า config โหลดแล้ว
from config import get_config

def _get_db_path() -> Path:
    """Lazy path resolution — อ่านจาก config ทุกครั้ง"""
    return Path(get_config()['paths']['db'])

def _get_log_dir() -> Path:
    return Path(get_config()['paths']['logs'])

# Module-level shortcuts (resolve ตอน import — ปลอดภัยเพราะ config.py ทำงานแล้ว)
_CFG         = get_config()
DB_PATH      = Path(_CFG['paths']['db'])
LOG_DIR      = Path(_CFG['paths']['logs'])
ACCOUNT_JSON = LOG_DIR / "account.json"


# ══════════════════════════════════════════════════════════════
# Database Initialization
# ══════════════════════════════════════════════════════════════
def init_db():
    """
    สร้าง database และ tables ถ้ายังไม่มี
    เรียกครั้งเดียวตอน startup
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    with _get_conn() as conn:
        conn.executescript("""
            -- ── trades ──────────────────────────────────────
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket       INTEGER UNIQUE,        -- MT5 ticket
                open_time    TEXT    NOT NULL,
                close_time   TEXT,                  -- NULL = ยังเปิดอยู่
                symbol       TEXT    NOT NULL,
                direction    TEXT    NOT NULL,       -- BUY / SELL
                volume       REAL    NOT NULL,
                open_price   REAL    NOT NULL,
                close_price  REAL,
                sl           REAL,
                tp           REAL,
                profit       REAL,                  -- NULL = ยังไม่ปิด
                swap         REAL    DEFAULT 0,
                commission   REAL    DEFAULT 0,
                confidence   REAL    DEFAULT 0,
                comment      TEXT    DEFAULT '',
                strategy_ver TEXT    DEFAULT 'v1',
                created_at   TEXT    DEFAULT (datetime('now'))
            );

            -- ── account snapshots ──────────────────────────
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT    NOT NULL,
                balance      REAL    NOT NULL,
                equity       REAL    NOT NULL,
                margin       REAL    DEFAULT 0,
                free_margin  REAL    DEFAULT 0,
                margin_level REAL    DEFAULT 0,
                profit       REAL    DEFAULT 0,
                open_trades  INTEGER DEFAULT 0
            );

            -- ── signals ────────────────────────────────────
            CREATE TABLE IF NOT EXISTS signals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT    NOT NULL,
                symbol       TEXT    NOT NULL,
                signal       INTEGER NOT NULL,      -- -1/0/1
                confidence   REAL    DEFAULT 0,
                proba_sell   REAL    DEFAULT 0,
                proba_hold   REAL    DEFAULT 0,
                proba_buy    REAL    DEFAULT 0,
                regime       TEXT    DEFAULT '',
                session      TEXT    DEFAULT ''
            );

            -- ── daily summary ───────────────────────────────
            CREATE TABLE IF NOT EXISTS daily_summary (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT    UNIQUE NOT NULL,
                start_balance REAL   DEFAULT 0,
                end_balance  REAL    DEFAULT 0,
                pnl          REAL    DEFAULT 0,
                trades       INTEGER DEFAULT 0,
                wins         INTEGER DEFAULT 0,
                losses       INTEGER DEFAULT 0,
                win_rate     REAL    DEFAULT 0,
                profit_factor REAL   DEFAULT 0,
                max_drawdown REAL    DEFAULT 0
            );

            -- ── Indexes ─────────────────────────────────────
            CREATE INDEX IF NOT EXISTS idx_trades_symbol
                ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_open_time
                ON trades(open_time);
            CREATE INDEX IF NOT EXISTS idx_trades_close_time
                ON trades(close_time);
            CREATE INDEX IF NOT EXISTS idx_account_ts
                ON account_snapshots(ts);
            CREATE INDEX IF NOT EXISTS idx_signals_ts
                ON signals(ts);
            CREATE INDEX IF NOT EXISTS idx_signals_symbol
                ON signals(symbol);
        """)

    log.info(f"✅ Database initialized: {DB_PATH}")


# ══════════════════════════════════════════════════════════════
# Connection Manager
# ══════════════════════════════════════════════════════════════
@contextmanager
def _get_conn():
    """
    Context manager สำหรับ SQLite connection
    auto-commit และ auto-close เสมอ
    ตั้ง timeout=10 วินาที (ป้องกัน lock จาก Streamlit)
    """
    conn = sqlite3.connect(
        DB_PATH,
        timeout    = 10,
        check_same_thread = False,
    )
    conn.row_factory = sqlite3.Row   # dict-like access
    conn.execute("PRAGMA journal_mode=WAL")   # ป้องกัน lock
    conn.execute("PRAGMA synchronous=NORMAL")

    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"DB error: {e}", exc_info=True)
        raise
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# MetricsWriter Class
# ══════════════════════════════════════════════════════════════
class MetricsWriter:
    """
    จัดการการเขียนข้อมูลทุกประเภทลง SQLite

    วิธีใช้:
        writer = MetricsWriter()
        writer.write_trade({...})
        writer.write_account({...})
        writer.write_signal("XAUUSD", 1, 0.73)
    """

    def __init__(self):
        self._snap_count    = 0
        self._snap_interval = 4   # บันทึก account ทุก 4 tick (1 ชั่วโมง)
        log.info(f"MetricsWriter → {DB_PATH}")

    # ══════════════════════════════════════════════════════════
    # Trade Recording
    # ══════════════════════════════════════════════════════════
    def write_trade(self, trade: dict):
        """
        บันทึก trade ลง DB

        trade dict ต้องมี:
            open_time, symbol, direction, volume,
            open_price, sl, tp, ticket, confidence

        ถ้ามี close_time และ close_price → update row เดิม
        ถ้าไม่มี → insert row ใหม่ (trade ยังเปิดอยู่)
        """
        ticket     = trade.get('ticket', 0)
        close_time = trade.get('close_time')

        if ticket and close_time:
            # Trade ปิดแล้ว — update row เดิม
            self._update_closed_trade(trade)
        else:
            # Trade เปิดใหม่ — insert
            self._insert_open_trade(trade)

    def _insert_open_trade(self, trade: dict):
        """Insert trade ที่เพิ่งเปิด"""
        # ✅ FIX BUG-8 CRITICAL: executor.py ส่ง key 'lot' แต่เดิมอ่าน 'volume'
        #    ทำให้ lot size ในฐานข้อมูลผิดทุก trade — fix ด้วยการรับทั้งสอง key
        volume = trade.get('volume') or trade.get('lot', 0.01)

        with _get_conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO trades
                (ticket, open_time, symbol, direction, volume,
                 open_price, sl, tp, confidence, comment,
                 strategy_ver)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade.get('ticket',     0),
                trade.get('open_time',  _now()),
                trade.get('symbol',     ''),
                trade.get('direction',  'BUY'),
                volume,                            # ✅ ใช้ volume ที่ resolve แล้ว
                trade.get('open_price', 0.0),
                trade.get('sl',         0.0),
                trade.get('tp',         0.0),
                trade.get('confidence', 0.0),
                trade.get('comment',    ''),
                trade.get('strategy_ver', 'v1'),
            ))

        log.debug(
            f"Inserted trade #{trade.get('ticket')} "
            f"{trade.get('symbol')} {trade.get('direction')}"
        )

    def _update_closed_trade(self, trade: dict):
        """Update trade ที่ปิดแล้ว — เพิ่ม close data"""
        with _get_conn() as conn:
            conn.execute("""
                UPDATE trades SET
                    close_time  = ?,
                    close_price = ?,
                    profit      = ?,
                    swap        = ?,
                    commission  = ?
                WHERE ticket = ?
            """, (
                trade.get('close_time',  _now()),
                trade.get('close_price', 0.0),
                trade.get('pnl',         0.0),
                trade.get('swap',        0.0),
                trade.get('commission',  0.0),
                trade.get('ticket',      0),
            ))

        log.debug(
            f"Updated trade #{trade.get('ticket')} "
            f"P&L={trade.get('pnl', 0):+.2f}"
        )

        # อัพเดต daily summary
        self._update_daily_summary(trade)

    # ══════════════════════════════════════════════════════════
    # Account Snapshot
    # ══════════════════════════════════════════════════════════
    def write_account(self, account: dict):
        """
        บันทึก account snapshot
        ไม่บันทึกทุก tick (throttle ด้วย _snap_interval)
        แต่บันทึก account.json ทุกครั้ง (Streamlit อ่าน)
        """
        # อัพเดต account.json ทุกครั้ง (เบา)
        try:
            ACCOUNT_JSON.write_text(
                json.dumps({
                    **account,
                    'updated_at': _now(),
                }),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning(f"account.json write error: {e}")

        # บันทึกลง DB ทุก n tick (ไม่ต้องบันทึกทุกครั้ง)
        self._snap_count += 1
        if self._snap_count % self._snap_interval != 0:
            return

        with _get_conn() as conn:
            conn.execute("""
                INSERT INTO account_snapshots
                (ts, balance, equity, margin, free_margin,
                 margin_level, profit, open_trades)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                _now(),
                account.get('balance',      0.0),
                account.get('equity',       0.0),
                account.get('margin',       0.0),
                account.get('free_margin',  0.0),
                account.get('margin_level', 0.0),
                account.get('profit',       0.0),
                account.get('open_trades',  0),
            ))

        log.debug(
            f"Account snapshot: "
            f"balance=${account.get('balance',0):,.2f} "
            f"equity=${account.get('equity',0):,.2f}"
        )

    # ══════════════════════════════════════════════════════════
    # Signal Recording
    # ══════════════════════════════════════════════════════════
    def write_signal(
        self,
        symbol:     str,
        signal:     int,
        confidence: float,
        proba_sell: float = 0.0,
        proba_hold: float = 0.0,
        proba_buy:  float = 0.0,
        regime:     str   = "",
        session:    str   = "",
    ):
        """บันทึก ML signal ทุก bar"""
        with _get_conn() as conn:
            conn.execute("""
                INSERT INTO signals
                (ts, symbol, signal, confidence,
                 proba_sell, proba_hold, proba_buy,
                 regime, session)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                _now(), symbol, signal, confidence,
                proba_sell, proba_hold, proba_buy,
                regime, session,
            ))

    # ══════════════════════════════════════════════════════════
    # Daily Summary
    # ══════════════════════════════════════════════════════════
    def _update_daily_summary(self, trade: dict):
        """อัพเดตสรุปรายวันหลังปิด trade"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with _get_conn() as conn:
            # ดึง trades วันนี้ทั้งหมด
            rows = conn.execute("""
                SELECT profit FROM trades
                WHERE date(close_time) = ?
                  AND profit IS NOT NULL
            """, (today,)).fetchall()

        if not rows:
            return

        profits       = [r['profit'] for r in rows]
        wins          = [p for p in profits if p > 0]
        losses        = [p for p in profits if p <= 0]
        total_pnl     = sum(profits)
        win_rate      = len(wins) / len(profits) if profits else 0
        profit_factor = (
            sum(wins) / abs(sum(losses))
            if losses else float('inf')
        )

        # คำนวณ max drawdown วันนี้
        cumulative = 0.0
        peak       = 0.0
        max_dd     = 0.0
        for p in profits:
            cumulative += p
            peak        = max(peak, cumulative)
            dd          = peak - cumulative
            max_dd      = max(max_dd, dd)

        with _get_conn() as conn:
            conn.execute("""
                INSERT INTO daily_summary
                    (date, pnl, trades, wins, losses,
                     win_rate, profit_factor, max_drawdown)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(date) DO UPDATE SET
                    pnl           = excluded.pnl,
                    trades        = excluded.trades,
                    wins          = excluded.wins,
                    losses        = excluded.losses,
                    win_rate      = excluded.win_rate,
                    profit_factor = excluded.profit_factor,
                    max_drawdown  = excluded.max_drawdown
            """, (
                today, total_pnl, len(profits),
                len(wins), len(losses),
                win_rate, profit_factor, max_dd,
            ))

    # ══════════════════════════════════════════════════════════
    # Query Methods (ใช้จาก dashboard)
    # ══════════════════════════════════════════════════════════
    def get_trades(
        self,
        days_back: int  = 30,
        symbol:    str  = None,
        closed_only:bool= True,
    ) -> pd.DataFrame:
        """โหลด trades เป็น DataFrame"""
        query  = """
            SELECT * FROM trades
            WHERE open_time >= datetime('now', ?)
        """
        params = [f"-{days_back} days"]

        if symbol:
            query  += " AND symbol = ?"
            params.append(symbol)

        if closed_only:
            query += " AND close_time IS NOT NULL"

        query += " ORDER BY close_time DESC"

        with _get_conn() as conn:
            df = pd.read_sql_query(query, conn, params=params)

        if not df.empty:
            for col in ['open_time','close_time']:
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col], utc=True)

        return df

    def get_equity_curve(
        self, days_back: int = 90
    ) -> pd.DataFrame:
        """Equity curve จาก account snapshots"""
        with _get_conn() as conn:
            df = pd.read_sql_query("""
                SELECT ts, balance, equity, profit, open_trades
                FROM account_snapshots
                WHERE ts >= datetime('now', ?)
                ORDER BY ts ASC
            """, conn, params=[f"-{days_back} days"])

        if not df.empty:
            df['ts'] = pd.to_datetime(df['ts'], utc=True)
            df.set_index('ts', inplace=True)

        return df

    def get_daily_summary(
        self, days_back: int = 30
    ) -> dict:
        """
        สรุป performance รวม
        ใช้ใน bot/main.py สำหรับ daily_summary notification
        """
        with _get_conn() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*)           AS trades,
                    SUM(profit)        AS total_pnl,
                    AVG(profit)        AS avg_trade,
                    SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END)
                                       AS wins,
                    MAX(profit)        AS best_trade,
                    MIN(profit)        AS worst_trade
                FROM trades
                WHERE close_time IS NOT NULL
                  AND close_time >= datetime('now', ?)
            """, (f"-{days_back} days",)).fetchone()

        if row is None or row['trades'] == 0:
            return {
                'trades': 0, 'total_pnl': 0.0,
                'win_rate': 0.0, 'avg_trade': 0.0,
            }

        return {
            'trades'     : row['trades'],
            'total_pnl'  : round(row['total_pnl'] or 0, 2),
            'wins'       : row['wins'],
            'win_rate'   : (row['wins'] / max(row['trades'], 1)),
            'avg_trade'  : round(row['avg_trade'] or 0, 2),
            'best_trade' : round(row['best_trade'] or 0, 2),
            'worst_trade': round(row['worst_trade'] or 0, 2),
        }

    def get_symbol_performance(self) -> pd.DataFrame:
        """P&L แยกตาม symbol"""
        with _get_conn() as conn:
            df = pd.read_sql_query("""
                SELECT
                    symbol,
                    COUNT(*)                              AS trades,
                    SUM(profit)                           AS total_pnl,
                    AVG(profit)                           AS avg_trade,
                    SUM(CASE WHEN profit > 0 THEN 1 END) AS wins,
                    ROUND(
                        100.0 *
                        SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END)
                        / COUNT(*), 1
                    )                                     AS win_rate_pct
                FROM trades
                WHERE profit IS NOT NULL
                GROUP BY symbol
                ORDER BY total_pnl DESC
            """, conn)
        return df

    def get_recent_signals(
        self,
        symbol:    str = None,
        limit:     int = 100,
    ) -> pd.DataFrame:
        """Signal ล่าสุดสำหรับ dashboard"""
        # ✅ FIX BUG-10: ใช้ parameterized query แทน f-string LIMIT
        query  = "SELECT * FROM signals"
        params: list = []
        if symbol:
            query  += " WHERE symbol = ?"
            params.append(symbol)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(max(1, int(limit)))   # clamp ป้องกัน negative

        with _get_conn() as conn:
            df = pd.read_sql_query(query, conn, params=params)
        return df

    # ══════════════════════════════════════════════════════════
    # Maintenance
    # ══════════════════════════════════════════════════════════
    def cleanup_old_data(self, keep_days: int = 180):
        """
        ลบข้อมูลเก่าเกิน keep_days วัน
        รันผ่าน Task Scheduler ทุกเดือน
        """
        cutoff = f"-{keep_days} days"
        with _get_conn() as conn:
            # ลบ account snapshots เก่า (เก็บแค่ 1 snapshot/ชั่วโมง)
            conn.execute("""
                DELETE FROM account_snapshots
                WHERE ts < datetime('now', ?)
            """, (cutoff,))

            # ลบ signals เก่า
            conn.execute("""
                DELETE FROM signals
                WHERE ts < datetime('now', ?)
            """, (cutoff,))

            # trades ไม่ลบ — เก็บไว้วิเคราะห์ตลอด

        log.info(f"Cleanup: ลบข้อมูลเก่ากว่า {keep_days} วัน")
        self._vacuum()

    def _vacuum(self):
        """VACUUM เพื่อคืนพื้นที่หลัง delete"""
        conn = sqlite3.connect(DB_PATH)
        conn.execute("VACUUM")
        conn.close()
        log.info("Database VACUUM complete")

    def export_csv(self, output_dir: str = "reports"):
        """Export ทุก table เป็น CSV"""
        Path(output_dir).mkdir(exist_ok=True)

        tables = [
            'trades', 'account_snapshots',
            'signals', 'daily_summary',
        ]
        for table in tables:
            with _get_conn() as conn:
                df = pd.read_sql_query(
                    f"SELECT * FROM {table}", conn
                )
            path = f"{output_dir}/{table}.csv"
            df.to_csv(path, index=False)
            log.info(f"Exported {table} → {path}")

    def get_db_stats(self) -> dict:
        """สถิติ database"""
        with _get_conn() as conn:
            stats = {}
            for table in ['trades','account_snapshots','signals']:
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                ).fetchone()
                stats[table] = row['n']

        stats['db_size_mb'] = round(
            DB_PATH.stat().st_size / 1_048_576, 2
        )
        return stats


# ══════════════════════════════════════════════════════════════
# Module-Level Functions (ใช้จาก executor โดยตรง)
# ══════════════════════════════════════════════════════════════
_writer_instance: Optional[MetricsWriter] = None

def _get_writer() -> MetricsWriter:
    """Lazy singleton"""
    global _writer_instance
    if _writer_instance is None:
        _writer_instance = MetricsWriter()
    return _writer_instance

def write_trade(trade: dict):
    """Shortcut — ใช้จาก executor.py"""
    _get_writer().write_trade(trade)

def write_account(account: dict):
    """Shortcut — ใช้จาก bot/main.py"""
    _get_writer().write_account(account)

def write_signal(
    symbol:     str,
    signal:     int,
    confidence: float,
    **kwargs,
):
    """Shortcut — ใช้จาก bot/main.py"""
    _get_writer().write_signal(symbol, signal, confidence, **kwargs)


# ── Utility ────────────────────────────────────────────────────
def _now() -> str:
    """Timestamp UTC เป็น ISO string"""
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--stats",   action="store_true")
    parser.add_argument("--export",  action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--vacuum",  action="store_true")
    args = parser.parse_args()

    init_db()
    w = MetricsWriter()

    if args.stats:
        stats = w.get_db_stats()
        print(f"\n── DB Stats ──")
        for k, v in stats.items():
            print(f"  {k:<25}: {v}")

    if args.export:
        w.export_csv()

    if args.cleanup:
        w.cleanup_old_data(keep_days=180)

    if args.vacuum:
>>>>>>> 98ac82b18ee8d71be376450f278ba77dde0c1c3e
        w._vacuum()