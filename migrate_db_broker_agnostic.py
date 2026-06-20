# migrate_db_broker_agnostic.py
"""
รันครั้งเดียวเพื่ออัพเดต trades.db ให้รองรับ broker-agnostic
เพิ่มคอลัมน์ symbol_canonical ในตาราง trades + signals
เก็บทั้งคู่: symbol (broker-native, ของเดิม) + symbol_canonical (ใหม่)

วิธีรัน:
    python migrate_db_broker_agnostic.py
"""
import sqlite3
from pathlib import Path
from config import get_config, unresolve_symbol

CFG = get_config()
DB_PATH = Path(CFG['paths']['db'])


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    for table in ['trades', 'signals']:
        # เช็คว่ามีคอลัมน์ symbol_canonical อยู่แล้วไหม
        cur.execute(f"PRAGMA table_info({table})")
        cols = [c[1] for c in cur.fetchall()]

        if 'symbol_canonical' in cols:
            print(f"[{table}] symbol_canonical มีอยู่แล้ว — ข้าม")
            continue

        print(f"[{table}] เพิ่มคอลัมน์ symbol_canonical...")
        cur.execute(f"ALTER TABLE {table} ADD COLUMN symbol_canonical TEXT")

        # แปลงข้อมูลเดิมทั้งหมด: broker symbol → canonical
        cur.execute(f"SELECT id, symbol FROM {table}")
        rows = cur.fetchall()

        for row_id, broker_symbol in rows:
            canonical = unresolve_symbol(broker_symbol)
            cur.execute(
                f"UPDATE {table} SET symbol_canonical = ? WHERE id = ?",
                (canonical, row_id)
            )

        print(f"[{table}] อัพเดต {len(rows)} แถวเสร็จแล้ว")

    conn.commit()
    conn.close()
    print("✅ Migration complete")


if __name__ == "__main__":
    migrate()