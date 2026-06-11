# ============================================================
# bot/setup_logging.py
# เรียกใน bot/main.py ก่อน import อื่นๆ ทั้งหมด
#
# Usage (ใน main.py บรรทัดแรกสุด):
#   from bot.setup_logging import setup_logging
#   setup_logging()
# ============================================================

import logging
import logging.config
import sys
from pathlib import Path

import yaml


def setup_logging(
    config_path: str | Path | None = None,
    log_level_override: str | None = None,
) -> None:
    """
    สร้าง log directories และโหลด logging.yaml

    Args:
        config_path: path ไปยัง logging.yaml
                     (default: project_root/logging.yaml)
        log_level_override: override level ทั้งหมด เช่น "DEBUG"
                            (มีประโยชน์ตอน dev/debug)
    """
    # ── หา project root ─────────────────────────────────────
    # bot/setup_logging.py อยู่ใน bot/ → parent = project root
    project_root = Path(__file__).parent.parent

    # ── สร้าง log directories ก่อน ─────────────────────────
    log_dirs = [
        project_root / "logs",
        project_root / "logs" / "daily",
    ]
    for d in log_dirs:
        d.mkdir(parents=True, exist_ok=True)

    # ── โหลด logging.yaml ────────────────────────────────────
    if config_path is None:
        config_path = project_root / "logging.yaml"

    config_path = Path(config_path)

    if not config_path.exists():
        # fallback: basicConfig ถ้าไม่มี logging.yaml
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[
                logging.StreamHandler(sys.stdout),
                logging.FileHandler(
                    project_root / "logs" / "bot.log",
                    encoding="utf-8"
                ),
            ],
        )
        logging.warning(
            f"[LOGGING] ไม่พบ {config_path} — ใช้ basicConfig แทน"
        )
        return

    try:
        with open(config_path, encoding="utf-8") as f:
            log_cfg = yaml.safe_load(f)

        # override level ถ้าระบุ (ใช้ตอน debug)
        if log_level_override:
            level = log_level_override.upper()
            for handler in log_cfg.get("handlers", {}).values():
                handler["level"] = level
            for logger in log_cfg.get("loggers", {}).values():
                logger["level"] = level
            if "root" in log_cfg:
                log_cfg["root"]["level"] = level

        logging.config.dictConfig(log_cfg)
        logging.getLogger("bot.main").info(
            f"[LOGGING] Setup complete — config: {config_path}"
        )

    except Exception as e:
        # ถ้า logging.yaml พัง → fallback basicConfig อย่างน้อยยังดู log ได้
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s | %(levelname)-8s | %(message)s",
        )
        logging.error(f"[LOGGING] Failed to load {config_path}: {e}")
        logging.error("[LOGGING] Falling back to basicConfig")
        raise
