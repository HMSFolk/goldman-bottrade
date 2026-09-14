# Forex/Gold Trading Bot (ML-based)

โปรเจกต์ส่วนตัวที่พัฒนาระบบเทรดอัตโนมัติสำหรับ 3 คู่เงิน + ทองคำ (XAU) โดยใช้ Python ทั้งระบบ ตั้งแต่ดึงข้อมูลราคา, สร้างฟีเจอร์, เทรนโมเดล ML, ไปจนถึงส่งคำสั่งเทรดจริงผ่าน MetaTrader 5 (MT5) พร้อมแดชบอร์ดสำหรับมอนิเตอร์การทำงาน

> **สถานะ:** ใช้งานได้จริง (รันบน demo/สังเกตพฤติกรรม) แต่ยังไม่พร้อมสำหรับใช้เงินจริง — เป็นโปรเจกต์ที่ทำระหว่างฝึกงาน/ฝึกฝนตัวเอง ยังต้องพัฒนาเรื่อง risk management และการทดสอบเพิ่มเติมก่อนนำไปใช้จริง

## ภาพรวมระบบ (Pipeline)

```
data (ดึงราคาจาก MT5/ข่าว) 
   → features (คำนวณอินดิเคเตอร์ + label) 
   → models (เทรนโมเดล + backtest) 
   → bot (โหลดโมเดล วิเคราะห์ตลาด ตัดสินใจ ส่งคำสั่งไป MT5) 
   → dashboard (มอนิเตอร์สถานะ / สั่งเปิด-ปิดระบบ)
```

## โครงสร้างโปรเจกต์

| โฟลเดอร์ | หน้าที่ |
|---|---|
| `bot/` | "สมอง" ของระบบ — โหลดข้อมูล+โมเดล วิเคราะห์สถานะตลาด ตัดสินใจ แล้วส่งคำสั่งไป MT5 ผ่าน `mt5_client.py`, คุมความเสี่ยงด้วย `risk_manager.py`, แจ้งเตือนผ่าน `notifier.py` |
| `dashboard/` | เว็บแดชบอร์ด (Streamlit) แสดงสถานะว่าบอทกำลังทำงานอยู่หรือเปล่า ดูผลการเทรด และสั่งหยุด/เปิดระบบได้ |
| `data/` | ดึงข้อมูลราคาแท่งเทียน (M15) จาก MT5 (`collect_mt5.py`), ดึงข่าว/ข้อมูลเศรษฐกิจ (`collect_macro.py`, `collect_news.py`) แล้วเก็บดิบไว้ใน `raw/` |
| `features/` | อ่านข้อมูลดิบมากรอง/คำนวณเป็นฟีเจอร์ (trend, momentum, volatility, price action, order flow, multi-timeframe + label) แล้วเก็บผลไว้ใน `data/processed/` |
| `models/` | เทรนโมเดล ML แยกตามคู่เงิน/ทอง (XGBoost, LightGBM, LSTM) รวมถึง ensemble และ backtest engine ผลลัพธ์โมเดลเก็บใน `models/saved/` |
| `reports/` | รายงานผลการเทรน/backtest |
| `scripts/` | สคริปต์ช่วยงานทั่วไป (รัน pipeline, เทรน, ดึงข้อมูล ฯลฯ) |
| `windows_conf/` | ไฟล์ตั้งค่าเฉพาะฝั่ง Windows (เช่น `run_bot.bat` สำหรับรันบอทบนเครื่อง) |
| `config*.yaml` | ตั้งค่าคู่เงิน, timeframe, ความเสี่ยง ฯลฯ |

## เทคโนโลยีที่ใช้

- **ภาษา/พื้นฐาน:** Python, YAML config
- **จัดการข้อมูล:** pandas, numpy, pyarrow (parquet)
- **Technical Analysis:** ta (RSI, MACD, Bollinger Bands, EMA)
- **Machine Learning:** scikit-learn, XGBoost, LightGBM
- **Deep Learning:** PyTorch (LSTM) — รันแบบ CPU-only
- **Hyperparameter Tuning:** Optuna
- **Backtesting:** vectorbt
- **เชื่อมต่อตลาด:** MetaTrader5 (ดึงราคาเรียลไทม์ + ส่งคำสั่งซื้อขาย)
- **แจ้งเตือน:** Telegram Bot
- **ฐานข้อมูล:** SQLAlchemy (เก็บประวัติออเดอร์/log)
- **แดชบอร์ด/กราฟ:** Streamlit, Plotly, Matplotlib
- **วิเคราะห์ข่าว:** VADER Sentiment (ประเมินข่าวเป็นบวก/ลบ)

## สิ่งที่ทำเอง

- ออกแบบโครงสร้าง pipeline และสั่งงานให้ AI ช่วยเขียนโค้ดในแต่ละส่วน
- เก็บข้อมูล เตรียม/ตรวจสอบข้อมูล (data collection & inspection)
- Debug และแก้ปัญหาบางส่วนของระบบ (เช่น ปัญหาเวอร์ชัน scikit-learn ระหว่างเครื่องเทรนกับเครื่องรันจริงไม่ตรงกัน)
- ทดสอบการทำงานของบอทแบบ end-to-end

## ข้อจำกัด / สิ่งที่ยังต้องพัฒนาต่อ

- ยังไม่ผ่านการทดสอบ risk management ที่รัดกุมพอสำหรับเงินจริง
- ยังไม่มี unit test ครอบคลุม
- เอกสารประกอบ (docstring/README ภายในแต่ละโมดูล) ยังน้อย

---
*โปรเจกต์นี้ทำขึ้นเพื่อฝึกฝนและเรียนรู้เรื่อง ML + การเทรดอัตโนมัติ ไม่ใช่คำแนะนำการลงทุน*
