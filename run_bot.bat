@echo off
:: ════════════════════════════════════════════════════════════
:: AURUM BOT — Manual Runner
:: ใช้ตอน debug / ทดสอบ / deploy ใหม่
::
:: เมนู:
::   1. Run Bot (main loop)
::   2. Run Dashboard (Streamlit)
::   3. Run Telegram Bot
::   4. Collect Data only
::   5. Build Features only
::   6. Train Models (XGB + LGBM)
::   7. Backtest + Deploy Checklist
::   8. Full Retrain Pipeline
::   9. View Logs
::   0. Exit
:: ════════════════════════════════════════════════════════════

title AURUM BOT — Manual Runner
color 0A

:: ── Paths ──────────────────────────────────────────────────
set PYTHON=%WORKDIR%\.venv\Scripts\python.exe
set WORKDIR=%cd%
set LOGDIR=%WORKDIR%\logs

:: ── Go to project root ─────────────────────────────────────
cd /d %WORKDIR%

:: ── Check Python ───────────────────────────────────────────
if not exist "%PYTHON%" (
    echo.
    echo  [ERROR] Python not found: %PYTHON%
    echo  Run install.ps1 first
    echo.
    pause
    exit /b 1
)

:: ── Check .env ─────────────────────────────────────────────
if not exist ".env" (
    echo.
    echo  [WARN] .env not found!
    echo  Copy .env.example to .env and fill in credentials
    echo.
    pause
)

:MENU
cls
echo.
echo  ╔══════════════════════════════════════════╗
echo  ║   AURUM BOT — Manual Runner              ║
echo  ║   %date% %time:~0,8%                   ║
echo  ╚══════════════════════════════════════════╝
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Run Services                            │
echo  │   1. Run Bot (main loop)                 │
echo  │   2. Run Dashboard (Streamlit :8501)     │
echo  │   3. Run Telegram Bot                    │
echo  ├──────────────────────────────────────────┤
echo  │  Data Pipeline                           │
echo  │   4. Collect Data only                   │
echo  │   5. Build Features only                 │
echo  ├──────────────────────────────────────────┤
echo  │  Model Training                          │
echo  │   6. Train XGB + LGBM                    │
echo  │   7. Train LSTM                          │
echo  │   8. Backtest + Deploy Checklist         │
echo  │   9. Full Retrain Pipeline               │
echo  ├──────────────────────────────────────────┤
echo  │  Debug Tools                             │
echo  │   A. View Bot Log (real-time)            │
echo  │   B. View Error Log                      │
echo  │   C. Check MT5 Connection                │
echo  │   D. Check Model Files                   │
echo  │   E. DB Stats                            │
echo  ├──────────────────────────────────────────┤
echo  │   0. Exit                                │
echo  └──────────────────────────────────────────┘
echo.
set /p CHOICE=  Enter choice: 

if "%CHOICE%"=="1" goto RUN_BOT
if "%CHOICE%"=="2" goto RUN_DASHBOARD
if "%CHOICE%"=="3" goto RUN_TELEGRAM
if "%CHOICE%"=="4" goto COLLECT_DATA
if "%CHOICE%"=="5" goto BUILD_FEATURES
if "%CHOICE%"=="6" goto TRAIN_XGB_LGBM
if "%CHOICE%"=="7" goto TRAIN_LSTM
if "%CHOICE%"=="8" goto BACKTEST
if "%CHOICE%"=="9" goto FULL_RETRAIN
if /i "%CHOICE%"=="A" goto VIEW_BOT_LOG
if /i "%CHOICE%"=="B" goto VIEW_ERROR_LOG
if /i "%CHOICE%"=="C" goto CHECK_MT5
if /i "%CHOICE%"=="D" goto CHECK_MODELS
if /i "%CHOICE%"=="E" goto DB_STATS
if "%CHOICE%"=="0" goto EXIT

echo  Invalid choice. Try again.
timeout /t 2 /nobreak >nul
goto MENU


:: ════════════════════════════════════════════════════════════
:: 1. Run Bot
:: ════════════════════════════════════════════════════════════
:RUN_BOT
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Running Bot — Main Loop                 │
echo  │  Press Ctrl+C to stop                    │
echo  └──────────────────────────────────────────┘
echo.
echo  [INFO] Starting bot/main.py ...
echo  [INFO] Log: %LOGDIR%\bot.log
echo.

:: รัน bot พร้อม unbuffered output (-u)
%PYTHON% -u bot\main.py

echo.
echo  [INFO] Bot stopped.
goto DONE


:: ════════════════════════════════════════════════════════════
:: 2. Run Dashboard
:: ════════════════════════════════════════════════════════════
:RUN_DASHBOARD
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Running Streamlit Dashboard             │
echo  │  URL: http://localhost:8501              │
echo  │  Press Ctrl+C to stop                    │
echo  └──────────────────────────────────────────┘
echo.
%PYTHON% -m streamlit run dashboard\app.py ^
    --server.port 8501 ^
    --server.headless false ^
    --server.address localhost ^
    --browser.serverAddress localhost

goto DONE


:: ════════════════════════════════════════════════════════════
:: 3. Run Telegram Bot
:: ════════════════════════════════════════════════════════════
:RUN_TELEGRAM
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Running Telegram Command Bot            │
echo  │  Press Ctrl+C to stop                    │
echo  └──────────────────────────────────────────┘
echo.
%PYTHON% -u dashboard\telegram_bot.py
goto DONE


:: ════════════════════════════════════════════════════════════
:: 4. Collect Data
:: ════════════════════════════════════════════════════════════
:COLLECT_DATA
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Collecting Market Data                  │
echo  └──────────────────────────────────────────┘
echo.
echo  Options:
echo   1. Full update (MT5 + Macro + News)
echo   2. Quick update (MT5 latest only)
echo   3. MT5 only
echo   4. Macro only (DXY, VIX, US10Y)
echo.
set /p DCHOICE=  Choose (1-4): 

if "%DCHOICE%"=="1" %PYTHON% data\pipeline.py
if "%DCHOICE%"=="2" %PYTHON% data\pipeline.py --quick
if "%DCHOICE%"=="3" %PYTHON% data\pipeline.py --no-macro --no-news
if "%DCHOICE%"=="4" %PYTHON% data\pipeline.py --no-mt5 --no-news
goto DONE


:: ════════════════════════════════════════════════════════════
:: 5. Build Features
:: ════════════════════════════════════════════════════════════
:BUILD_FEATURES
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Building ML Features                    │
echo  └──────────────────────────────────────────┘
echo.
%PYTHON% features\pipeline.py
goto DONE


:: ════════════════════════════════════════════════════════════
:: 6. Train XGB + LGBM
:: ════════════════════════════════════════════════════════════
:TRAIN_XGB_LGBM
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Training XGBoost + LightGBM             │
echo  └──────────────────────────────────────────┘
echo.
echo  Options:
echo   1. Train all symbols (default)
echo   2. Train XAUUSD only
echo   3. Train with Hyperopt (ช้ากว่า แต่ดีกว่า)
echo.
set /p TCHOICE=  Choose (1-3): 

if "%TCHOICE%"=="1" (
    %PYTHON% models\train_xgb.py
    %PYTHON% models\train_lgbm.py
)
if "%TCHOICE%"=="2" (
    %PYTHON% models\train_xgb.py  --symbols XAUUSD
    %PYTHON% models\train_lgbm.py --symbols XAUUSD
)
if "%TCHOICE%"=="3" (
    %PYTHON% models\train_xgb.py  --hyperopt
    %PYTHON% models\train_lgbm.py --hyperopt
)
goto DONE


:: ════════════════════════════════════════════════════════════
:: 7. Train LSTM
:: ════════════════════════════════════════════════════════════
:TRAIN_LSTM
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Training LSTM + Attention               │
echo  │  (ใช้เวลานาน — 1-3 ชั่วโมง)            │
echo  └──────────────────────────────────────────┘
echo.
%PYTHON% models\train_lstm.py --symbols XAUUSD
goto DONE


:: ════════════════════════════════════════════════════════════
:: 8. Backtest + Checklist
:: ════════════════════════════════════════════════════════════
:BACKTEST
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Backtest + Deploy Checklist             │
echo  └──────────────────────────────────────────┘
echo.
echo  Options:
echo   1. Deploy Checklist (แนะนำก่อน live)
echo   2. Full Backtest (ensemble)
echo   3. Optimize SL/TP (Optuna)
echo   4. Walk-Forward only
echo.
set /p BCHOICE=  Choose (1-4): 

if "%BCHOICE%"=="1" %PYTHON% models\backtest.py --checklist
if "%BCHOICE%"=="2" %PYTHON% models\backtest.py --method ensemble
if "%BCHOICE%"=="3" %PYTHON% models\backtest.py --optimize --trials 200
if "%BCHOICE%"=="4" %PYTHON% models\backtest.py --method xgb
goto DONE


:: ════════════════════════════════════════════════════════════
:: 9. Full Retrain Pipeline
:: ════════════════════════════════════════════════════════════
:FULL_RETRAIN
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Full Retrain Pipeline                   │
echo  │  backup → data → features → train → bt  │
echo  │  (ใช้เวลา 2-4 ชั่วโมง)                  │
echo  └──────────────────────────────────────────┘
echo.
echo  [WARN] จะ backup + retrain ทุกโมเดล ยืนยัน? (Y/N)
set /p CONFIRM=  Confirm: 

if /i not "%CONFIRM%"=="Y" goto MENU

%PYTHON% scripts\retrain_all.py
goto DONE


:: ════════════════════════════════════════════════════════════
:: A. View Bot Log
:: ════════════════════════════════════════════════════════════
:VIEW_BOT_LOG
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Bot Log — Real-time (Ctrl+C to stop)    │
echo  └──────────────────────────────────────────┘
echo.
if exist "%LOGDIR%\bot.log" (
    powershell -Command "Get-Content '%LOGDIR%\bot.log' -Wait -Tail 50"
) else (
    echo  [INFO] bot.log not found yet
    echo  Bot has not run yet or logs are in bot_stdout.log
    echo.
    if exist "%LOGDIR%\bot_stdout.log" (
        echo  Showing bot_stdout.log instead...
        powershell -Command "Get-Content '%LOGDIR%\bot_stdout.log' -Wait -Tail 50"
    )
)
goto DONE


:: ════════════════════════════════════════════════════════════
:: B. View Error Log
:: ════════════════════════════════════════════════════════════
:VIEW_ERROR_LOG
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Error Log — Last 100 lines              │
echo  └──────────────────────────────────────────┘
echo.
if exist "%LOGDIR%\errors.log" (
    powershell -Command "Get-Content '%LOGDIR%\errors.log' -Tail 100"
) else (
    echo  [INFO] No errors.log found — no errors yet!
)
goto DONE


:: ════════════════════════════════════════════════════════════
:: C. Check MT5 Connection
:: ════════════════════════════════════════════════════════════
:CHECK_MT5
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Checking MT5 Connection                 │
echo  └──────────────────────────────────────────┘
echo.
%PYTHON% -c "
import MetaTrader5 as mt5
from dotenv import load_dotenv
import os

load_dotenv()
print('Connecting to MT5...')

if not mt5.initialize():
    print(f'[FAIL] MT5 initialize: {mt5.last_error()}')
    exit(1)

ok = mt5.login(
    int(os.getenv('MT5_LOGIN','0')),
    password=os.getenv('MT5_PASSWORD',''),
    server=os.getenv('MT5_SERVER',''),
)
if not ok:
    print(f'[FAIL] Login: {mt5.last_error()}')
    exit(1)

acc = mt5.account_info()
print(f'[OK] Connected!')
print(f'     Name    : {acc.name}')
print(f'     Login   : {acc.login}')
print(f'     Server  : {acc.server}')
print(f'     Balance : \${acc.balance:,.2f}')
print(f'     Leverage: 1:{acc.leverage}')

# ตรวจ symbols
symbols = ['XAUUSD','EURUSD','GBPUSD']
for s in symbols:
    tick = mt5.symbol_info_tick(s)
    if tick:
        print(f'     {s}: bid={tick.bid:.5f} ask={tick.ask:.5f}')
    else:
        print(f'     {s}: [NOT FOUND]')

mt5.shutdown()
"
goto DONE


:: ════════════════════════════════════════════════════════════
:: D. Check Model Files
:: ════════════════════════════════════════════════════════════
:CHECK_MODELS
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Model Files Status                      │
echo  └──────────────────────────────────────────┘
echo.
%PYTHON% -c "
from models.manage_models import print_model_table, verify_model
print_model_table()
print()
print('Verifying XAUUSD models...')
verify_model('XAUUSD')
"
goto DONE


:: ════════════════════════════════════════════════════════════
:: E. DB Stats
:: ════════════════════════════════════════════════════════════
:DB_STATS
cls
echo.
echo  ┌──────────────────────────────────────────┐
echo  │  Database Statistics                     │
echo  └──────────────────────────────────────────┘
echo.
%PYTHON% bot\metrics_writer.py --stats
goto DONE


:: ════════════════════════════════════════════════════════════
:: Done — กลับ menu
:: ════════════════════════════════════════════════════════════
:DONE
echo.
echo  ────────────────────────────────────────────
echo   Done. Press any key to return to menu...
echo  ────────────────────────────────────────────
pause >nul
goto MENU


:: ════════════════════════════════════════════════════════════
:: Exit
:: ════════════════════════════════════════════════════════════
:EXIT
cls
echo.
echo  Goodbye! Bot is running as Windows Service.
echo  Use 'nssm status TradingBot' to check.
echo.
exit /b 0