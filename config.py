import yaml
from functools import lru_cache
from pathlib import Path

@lru_cache(maxsize=1)
def get_config() -> dict:
    """โหลด config.yaml ครั้งเดียว cache ไว้ใช้ทั้งโปรเจกต์"""
    path = Path(__file__).parent / "config.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)

def reload_config() -> dict:
    """reload ใหม่ (ใช้หลัง hot-reload)"""
    get_config.cache_clear()
    return get_config()

# shortcut สำหรับ access แบบสะดวก
cfg = get_config()