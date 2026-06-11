# ============================================================
# bot/log_formatter.py
# ⚠️  ไฟล์นี้ REQUIRED โดย logging.yaml (json_formatter handler)
#     ถ้าไม่มีไฟล์นี้ bot จะ CRASH ตอน setup logging
# ============================================================

import json
import logging
import traceback
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """
    Format log record เป็น JSON 1 บรรทัดต่อ 1 record
    ใช้กับ file_trades handler ใน logging.yaml
    เพื่อให้ parse และวิเคราะห์ง่ายด้วย Python/pandas

    ตัวอย่าง output:
    {"time": "2025-05-25T14:30:15Z", "level": "INFO", "logger": "bot.executor",
     "func": "send_order", "msg": "BUY XAUUSD", "symbol": "XAUUSD", ...}
    """

    # fields ที่ดึงจาก LogRecord เสมอ
    BASE_FIELDS = (
        "name", "levelname", "funcName", "lineno",
        "pathname", "process", "thread",
    )

    def format(self, record: logging.LogRecord) -> str:
        # timestamp ISO-8601 UTC
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        log_entry: dict = {
            "time":   dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
            "level":  record.levelname,
            "logger": record.name,
            "func":   record.funcName,
            "line":   record.lineno,
            "msg":    record.getMessage(),
        }

        # exception info (ถ้ามี)
        if record.exc_info:
            log_entry["exc"] = self.formatException(record.exc_info)

        # stack info (ถ้ามี)
        if record.stack_info:
            log_entry["stack"] = self.formatStack(record.stack_info)

        # extra fields — ถ้า caller ส่ง extra={...} มาด้วย
        # เช่น logger.info("BUY", extra={"symbol": "XAUUSD", "lot": 0.01})
        standard_keys = {
            "msg", "args", "created", "exc_info", "exc_text",
            "filename", "funcName", "id", "levelname", "levelno",
            "lineno", "message", "module", "msecs", "name",
            "pathname", "process", "processName", "relativeCreated",
            "stack_info", "taskName", "thread", "threadName",
        }
        for key, val in record.__dict__.items():
            if key not in standard_keys:
                try:
                    # ทดสอบว่า serialize ได้ก่อนใส่
                    json.dumps(val)
                    log_entry[key] = val
                except (TypeError, ValueError):
                    log_entry[key] = str(val)

        return json.dumps(log_entry, ensure_ascii=False)


class TradeJSONFormatter(JSONFormatter):
    """
    JSONFormatter เฉพาะสำหรับ trade events
    บังคับให้มี field: symbol, action, lot, price, sl, tp

    Usage:
        logger.info("ORDER_OPEN", extra={
            "symbol": "XAUUSD",
            "action": "BUY",
            "lot":    0.01,
            "price":  2341.50,
            "sl":     2326.50,
            "tp":     2371.50,
            "magic":  20250101,
            "ticket": 12345678,
        })
    """
    TRADE_FIELDS = ("symbol", "action", "lot", "price", "sl", "tp", "ticket", "magic")

    def format(self, record: logging.LogRecord) -> str:
        base = json.loads(super().format(record))

        # เพิ่ม trade fields ถ้าไม่มีให้ใส่ null
        for field in self.TRADE_FIELDS:
            if field not in base:
                base[field] = None

        return json.dumps(base, ensure_ascii=False)
