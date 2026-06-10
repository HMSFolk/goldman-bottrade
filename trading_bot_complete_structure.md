# Trading Bot — Complete File Structure & Startup Guide
> Windows VPS + MT5 Exness (No Docker) — ทุกไฟล์ครบถ้วน

---

## โครงสร้างโฟลเดอร์ทั้งหมด

```
C:\trading-bot\
│
├── .env                          ← credentials (ห้าม git)
├── .gitignore
├── config.yaml                   ← ตั้งค่าทั้งหมด (แก้ที่นี่ที่เดียว)
├── logging.yaml                  ← ตั้งค่า log format
├── requirements.txt
├── README.md
│
├── data\
│   ├── pipeline.py               ← รวม collect_mt5 + collect_macro + collect_news
│   ├── collect_mt5.py            ← ดึงราคาจาก MT5
│   ├── collect_macro.py          ← DXY, US10Y, VIX ผ่าน yfinance
│   ├── collect_news.py           ← GDELT news sentiment
│   ├── raw\                      ← ข้อมูลดิบ .parquet (git ignore)
│   │   ├── XAUUSD_M15.parquet
│   │   ├── XAUUSD_H1.parquet
│   │   ├── XAUUSD_H4.parquet
│   │   ├── EURUSD_M15.parquet
│   │   ├── GBPUSD_M15.parquet
│   │   ├── macro_DXY.parquet
│   │   ├── macro_US10Y.parquet
│   │   ├── macro_VIX.parquet
│   │   └── news_sentiment.parquet
│   └── processed\                ← หลัง feature engineering (git ignore)
│       ├── XAUUSD_M15_features.parquet
│       ├── EURUSD_M15_features.parquet
│       └── GBPUSD_M15_features.parquet
│
├── features\
│   ├── pipeline.py               ← จุดรวม: เรียก trend+momentum+volatility+price_action+mtf
│   ├── trend.py                  ← EMA, MACD, ADX, Ichimoku
│   ├── momentum.py               ← RSI, Stochastic, Williams %R, CCI
│   ├── volatility.py             ← ATR, Bollinger Bands, Keltner, Squeeze
│   ├── price_action.py           ← candle pattern, pivot, support/resistance
│   └── mtf_label.py              ← multi-timeframe join + สร้าง label สำหรับ train
│
├── models\
│   ├── rule_based.py             ← EMA cross + RSI (baseline, ไม่ต้อง train)
│   ├── train_xgb.py              ← train XGBoost + walk-forward validation
│   ├── train_lgbm.py             ← train LightGBM
│   ├── train_lstm.py             ← train LSTM + Attention
│   ├── ensemble.py               ← รวม XGB + LGBM + LSTM โหวตสัญญาณ
│   ├── backtest.py               ← vectorbt backtest + walk-forward + Optuna
│   ├── strategies\
│   │   ├── __init__.py
│   │   ├── strategy_v1.py        ← กลยุทธ์ production ปัจจุบัน (ห้ามแก้)
│   │   └── strategy_v2.py        ← กลยุทธ์ทดสอบ (uncommit เมื่อพร้อม)
│   └── saved\                    ← โมเดลที่ train แล้ว (git ignore — ไฟล์ใหญ่)
│       ├── xgb_XAUUSD.pkl
│       ├── xgb_EURUSD.pkl
│       ├── xgb_GBPUSD.pkl
│       ├── lgbm_XAUUSD.pkl
│       └── lstm_XAUUSD.pth
│
├── bot\
│   ├── main.py                   ← loop หลัก: ดึงราคา → predict → send order
│   ├── mt5_client.py             ← connect MT5, get_ohlcv, ensure_connected
│   ├── risk_manager.py           ← คำนวณ lot, check spread/session/daily loss
│   ├── executor.py               ← send_order, close_all, ตรวจทุก condition
│   ├── metrics_writer.py         ← บันทึก trade/account ลง SQLite (ไม่ใช่ InfluxDB)
│   └── notifier.py               ← Telegram alert + Telegram command bot
│
├── dashboard\
│   ├── app.py                    ← Streamlit dashboard อ่านจาก SQLite
│   └── telegram_bot.py           ← /status /closeall /pause commands
│
├── scripts\
│   ├── install.ps1               ← ติดตั้ง Python + Git + NSSM + MT5 ครั้งแรก
│   ├── nssm_setup.ps1            ← ลงทะเบียน TradingBot + TradingDashboard เป็น Windows Service
│   ├── setup_scheduler.ps1       ← ตั้ง Task Scheduler: retrain + update data
│   ├── start_dashboard.ps1       ← เปิด port 8501, สร้าง service dashboard
│   ├── run_bot.bat               ← รัน bot ด้วยมือ (ใช้ตอน debug)
│   └── retrain_all.py            ← Python script: update data → build features → train ทุก symbol
│
├── db\
│   └── trades.db                 ← SQLite database (git ignore)
│
├── logs\                         ← git ignore ทั้งโฟลเดอร์
│   ├── bot.log                   ← log หลักของ bot
│   ├── bot_stdout.log            ← stdout จาก NSSM service
│   ├── bot_stderr.log            ← stderr จาก NSSM service
│   ├── trades.csv                ← backup trades ในรูป CSV
│   └── account.json              ← account snapshot ล่าสุด (Streamlit อ่าน)
│
├── reports\                      ← ผล backtest, equity curve images
│   └── walk_forward_results.csv
│
└── flags\                        ← control flags (git ignore)
    └── paused                    ← สร้างไฟล์นี้เพื่อหยุด bot ชั่วคราว
```

---

## ไฟล์ที่สับสน — อธิบายให้ชัด

### metrics_writer.py (มีแค่ไฟล์เดียว อยู่ใน bot\)
เวอร์ชัน Docker เดิมเขียนลง InfluxDB — ตอนนี้เปลี่ยนเป็น SQLite แทน:

```python
# bot\metrics_writer.py
import sqlite3, json, os
from datetime import datetime, timezone

DB_PATH = "db/trades.db"

def init_db():
    os.makedirs("db", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            open_time TEXT, close_time TEXT,
            symbol TEXT, type TEXT,
            volume REAL, open_price REAL, close_price REAL,
            profit REAL, sl REAL, tp REAL,
            confidence REAL, comment TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS account_snapshots (
            ts TEXT, balance REAL, equity REAL,
            profit REAL, free_margin REAL, open_trades INTEGER
        )
    """)
    con.commit(); con.close()

def write_trade(trade: dict):
    con = sqlite3.connect(DB_PATH)
    con.execute("""INSERT INTO trades
        (open_time,close_time,symbol,type,volume,open_price,
         close_price,profit,sl,tp,confidence,comment)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade.get("open_time"), trade.get("close_time"),
         trade["symbol"], trade["direction"], trade["lot"],
         trade["open_price"], trade["close_price"], trade["pnl"],
         trade.get("sl",0), trade.get("tp",0),
         trade.get("confidence",0), trade.get("comment","")))
    con.commit(); con.close()

def write_account(account: dict):
    con = sqlite3.connect(DB_PATH)
    con.execute("""INSERT INTO account_snapshots
        (ts,balance,equity,profit,free_margin,open_trades)
        VALUES (?,?,?,?,?,?)""",
        (datetime.now(timezone.utc).isoformat(),
         account["balance"], account["equity"], account["profit"],
         account["free_margin"], account.get("open_trades",0)))
    con.commit(); con.close()
    # บันทึก account.json ด้วย (Streamlit อ่าน)
    with open("logs/account.json","w") as f:
        json.dump(account, f)
```

### user_data\ — มาจากไหน?
มาจากคำสั่ง `freqtrade create-userdir` ใน Phase 4 (backtest ด้วย freqtrade)
ถ้าไม่ได้ใช้ freqtrade ลบทิ้งได้เลย — ใช้ vectorbt backtest แทน

### notifier.py (แยกออกจาก executor.py)
```python
# bot\notifier.py
import requests, os

def notify(msg: str):
    """ส่ง Telegram — ใช้จาก executor และ bot หลัก"""
    token   = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                timeout=5
            )
        except Exception as e:
            pass  # ไม่ให้ Telegram ขัด bot หลัก
```

---

## .env (สร้างบน VPS ด้วยมือ — ห้าม commit)

```env
# MT5 Exness
MT5_LOGIN=12345678
MT5_PASSWORD=your_password_here
MT5_SERVER=Exness-MT5Real8

# Telegram
TELEGRAM_TOKEN=1234567890:ABCdef...
TELEGRAM_CHAT_ID=987654321
```

---

## requirements.txt

```txt
MetaTrader5>=5.0.45
pandas>=2.0
numpy>=1.24
scikit-learn>=1.3
xgboost>=2.0
lightgbm>=4.0
torch>=2.0
ta>=0.10
joblib>=1.3
schedule>=1.2
python-dotenv>=1.0
requests>=2.31
pyarrow>=14.0
streamlit>=1.30
plotly>=5.18
sqlalchemy>=2.0
optuna>=3.4
vectorbt>=0.26
yfinance>=0.2
vaderSentiment>=3.3
python-telegram-bot>=20.0
```

---

## config.yaml (ตั้งค่าที่นี่ที่เดียว)

```yaml
trading:
  symbols:         ["XAUUSD", "EURUSD", "GBPUSD"]
  timeframe:       "M15"
  risk_per_trade:  0.01
  max_daily_loss:  0.05
  max_open_trades: 3
  max_spread_pts:  30

signal:
  min_confidence:  0.62
  sl_points:       150
  tp_ratio:        2.0

model:
  active:               "ensemble"   # xgb | lgbm | lstm | ensemble
  retrain_every_days:   30

paths:
  mt5_terminal: "C:\\Program Files\\MetaTrader 5\\terminal64.exe"
  db:           "db/trades.db"
  logs:         "logs"
  models:       "models/saved"
```

---

## .gitignore

```gitignore
# credentials
.env

# data (ใหญ่เกินไป)
data/raw/
data/processed/

# trained models (ใหญ่เกินไป)
models/saved/

# database & logs
db/
logs/
flags/
reports/

# Python
__pycache__/
*.pyc
*.pyo
.venv/
*.egg-info/

# freqtrade (ถ้าไม่ใช้)
user_data/
```

---

## วิธีเริ่มรัน — ทำตามลำดับนี้

### รอบแรก (ทำครั้งเดียว บน VPS)

```
ขั้นที่ 1: RDP เข้า Windows VPS
ขั้นที่ 2: เปิด PowerShell (Admin)
ขั้นที่ 3: รัน scripts\install.ps1
ขั้นที่ 4: Login MT5 กับ Exness ด้วยมือ
ขั้นที่ 5: สร้างไฟล์ .env (ใส่ login + password)
ขั้นที่ 6: รัน scripts\nssm_setup.ps1
ขั้นที่ 7: รัน scripts\setup_scheduler.ps1
ขั้นที่ 8: รัน scripts\start_dashboard.ps1
```

### รอบแรก — เตรียมข้อมูลและโมเดล

```powershell
cd C:\trading-bot

# 1. ดึงข้อมูลย้อนหลัง
python data\pipeline.py

# 2. สร้าง features
python features\pipeline.py

# 3. Train โมเดล (ใช้เวลา 10-30 นาที)
python models\train_xgb.py
python models\train_lgbm.py

# 4. Backtest ตรวจสอบก่อน live
python models\backtest.py

# 5. เริ่ม bot service
nssm start TradingBot
nssm start TradingDashboard
```

### ตรวจสอบว่ารันได้

```powershell
# ดู service status
nssm status TradingBot
nssm status TradingDashboard

# ดู log แบบ real-time
Get-Content logs\bot.log -Wait -Tail 50
```

### ดู dashboard
เปิดเบราว์เซอร์: `http://VPS_IP:8501`

---

## ทุกวันหลังจากนั้น — ไม่ต้องทำอะไร

```
MT5          → รันอัตโนมัติกับ Windows
TradingBot   → รันอัตโนมัติ (NSSM service)
Dashboard    → รันอัตโนมัติ (NSSM service)
Update data  → Task Scheduler ทำทุกวันตี 1
Retrain      → Task Scheduler ทำทุกอาทิตย์ตี 2
```

### เมื่ออยากแก้โค้ด (จาก Windows local)

```powershell
# บน Windows local
git add .
git commit -m "ปรับ feature"
git push origin main

# RDP เข้า VPS แล้วรัน
cd C:\trading-bot
git pull
nssm restart TradingBot
```

---

## สรุปไฟล์ทั้งหมด (25 ไฟล์ + โฟลเดอร์)

| ไฟล์ | โฟลเดอร์ | แก้บ่อยแค่ไหน |
|------|----------|----------------|
| .env | root | ครั้งแรกครั้งเดียว |
| config.yaml | root | เมื่อปรับ risk/parameter |
| requirements.txt | root | เมื่อเพิ่ม library |
| .gitignore | root | ไม่ค่อยแก้ |
| pipeline.py | data\ | เมื่อเพิ่ม symbol/source |
| collect_mt5.py | data\ | ไม่ค่อยแก้ |
| collect_macro.py | data\ | เมื่อเพิ่ม macro data |
| collect_news.py | data\ | ไม่ค่อยแก้ |
| pipeline.py | features\ | เมื่อเปิด/ปิด feature group |
| trend.py | features\ | เมื่อเพิ่ม indicator |
| momentum.py | features\ | เมื่อเพิ่ม indicator |
| volatility.py | features\ | เมื่อเพิ่ม indicator |
| price_action.py | features\ | เมื่อเพิ่ม pattern |
| mtf_label.py | features\ | เมื่อเปลี่ยน label logic |
| train_xgb.py | models\ | เมื่อปรับ hyperparameter |
| train_lgbm.py | models\ | เมื่อปรับ hyperparameter |
| train_lstm.py | models\ | เมื่อปรับ architecture |
| ensemble.py | models\ | เมื่อปรับ weight โมเดล |
| backtest.py | models\ | ทดสอบก่อน deploy |
| strategy_v1.py | models\strategies\ | ห้ามแก้ (production) |
| strategy_v2.py | models\strategies\ | กลยุทธ์ใหม่ที่ทดสอบ |
| main.py | bot\ | logic หลัก bot loop |
| mt5_client.py | bot\ | ไม่ค่อยแก้ |
| risk_manager.py | bot\ | เมื่อปรับ risk rule |
| executor.py | bot\ | เมื่อปรับ order logic |
| metrics_writer.py | bot\ | ไม่ค่อยแก้ |
| notifier.py | bot\ | เมื่อเพิ่มประเภท alert |
| app.py | dashboard\ | เมื่อเพิ่ม chart/metric |
| telegram_bot.py | dashboard\ | เมื่อเพิ่ม command |
| install.ps1 | scripts\ | ครั้งแรกครั้งเดียว |
| nssm_setup.ps1 | scripts\ | ครั้งแรกครั้งเดียว |
| setup_scheduler.ps1 | scripts\ | ครั้งแรกครั้งเดียว |
| retrain_all.py | scripts\ | ไม่ค่อยแก้ |
