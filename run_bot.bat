@echo off
:: ===========================================================
:: AURUM BOT -- Manual Runner
:: FIX: ใช้ ASCII แทน Unicode box chars (ไม่ต้อง chcp 65001)
:: ===========================================================
title AURUM BOT -- Manual Runner
color 0A

:: -- Paths ------------------------------------------------
set "WORKDIR=%~dp0"
if "%WORKDIR:~-1%"=="\" set "WORKDIR=%WORKDIR:~0,-1%"
set "PYTHON=%WORKDIR%\.venv\Scripts\python.exe"
set "LOGDIR=%WORKDIR%\logs"
set "PYTHONPATH=%WORKDIR%"

:: -- Go to project root -------------------------------------
cd /d "%WORKDIR%"

:: -- Check Python -------------------------------------------
if not exist "%PYTHON%" (
    echo.
    echo  [ERROR] Python not found: %PYTHON%
    echo  Run install.ps1 first
    echo.
    pause
    exit /b 1
)

:: -- Check .env ---------------------------------------------
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
echo  ==================================================
echo    AURUM BOT -- Manual Runner
echo    %date%  %time:~0,8%
echo  ==================================================
echo.
echo  [ Run Services ]
echo    1. Run Bot (main loop)
echo    2. Run Dashboard (Streamlit :8501)
echo    3. Run Telegram Bot
echo  --------------------------------------------------
echo  [ Data Pipeline ]
echo    4. Collect Data only
echo    5. Build Features only
echo  --------------------------------------------------
echo  [ Model Training ]
echo    6. Train XGB + LGBM
echo    7. Train LSTM
echo    8. Backtest + Deploy Checklist
echo    9. Full Retrain Pipeline
echo  --------------------------------------------------
echo  [ Debug Tools ]
echo    A. View Bot Log (real-time)
echo    B. View Error Log
echo    C. Check MT5 Connection
echo    D. Check Model Files
echo    E. DB Stats
echo  --------------------------------------------------
echo  [ Scripts and Monitoring ]
echo    F. Scripts and Monitoring Tools
echo  --------------------------------------------------
echo    0. Exit
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
if /i "%CHOICE%"=="F" goto SCRIPTS_MENU
if "%CHOICE%"=="0" goto EXIT

echo  Invalid choice. Try again.
timeout /t 2 /nobreak >nul
goto MENU

:: ===========================================================
:: 1. Run Bot
:: ===========================================================
:RUN_BOT
cls
echo.
echo.
echo  Running Bot -- Main Loop
echo  Press Ctrl+C to stop
echo.
echo.
echo  [INFO] Starting bot/main.py ...
echo  [INFO] Log: %LOGDIR%\bot.log
echo.
"%PYTHON%" -u bot\main.py
echo.
echo  [INFO] Bot stopped.
goto DONE

:: ===========================================================
:: 2. Run Dashboard
:: ===========================================================
:RUN_DASHBOARD
cls
echo.
echo.
echo  Running Streamlit Dashboard
echo  URL: http://localhost:8501
echo  Press Ctrl+C to stop
echo.
echo.
"%PYTHON%" -m streamlit run dashboard\app.py ^
    --server.port 8501 ^
    --server.headless false ^
    --server.address localhost ^
    --browser.serverAddress localhost
goto DONE

:: ===========================================================
:: 3. Run Telegram Bot
:: ===========================================================
:RUN_TELEGRAM
cls
echo.
echo.
echo  Running Telegram Command Bot
echo  Press Ctrl+C to stop
echo.
echo.
"%PYTHON%" -u dashboard\telegram_bot.py
goto DONE

:: ===========================================================
:: 4. Collect Data
:: ===========================================================
:COLLECT_DATA
cls
echo.
echo.
echo  Collecting Market Data
echo.
echo.
echo  Options:
echo   1. Full update (MT5 + Macro + News)
echo   2. Quick update (MT5 latest only)
echo   3. MT5 only
echo   4. Macro only (DXY, VIX, US10Y)
echo.
set /p DCHOICE=  Choose (1-4): 
if "%DCHOICE%"=="1" "%PYTHON%" data\pipeline.py
if "%DCHOICE%"=="2" "%PYTHON%" data\pipeline.py --quick
if "%DCHOICE%"=="3" "%PYTHON%" data\pipeline.py --no-macro --no-news
if "%DCHOICE%"=="4" "%PYTHON%" data\pipeline.py --no-mt5 --no-news
goto DONE

:: ===========================================================
:: 5. Build Features
:: ===========================================================
:BUILD_FEATURES
cls
echo.
echo.
echo  Building ML Features
echo.
echo.
"%PYTHON%" features\pipeline.py
goto DONE

:: ===========================================================
:: 6. Train XGB + LGBM
:: ===========================================================
:TRAIN_XGB_LGBM
cls
echo.
echo.
echo  Training XGBoost + LightGBM
echo.
echo.
echo  Options:
echo   1. Train all symbols (default)
echo   2. Train XAUUSDm only
echo   3. Train with Hyperopt (slower but better)
echo.
set /p TCHOICE=  Choose (1-3): 
if "%TCHOICE%"=="1" (
    "%PYTHON%" models\train_xgb.py
    "%PYTHON%" models\train_lgbm.py
)
if "%TCHOICE%"=="2" (
    "%PYTHON%" models\train_xgb.py  --symbols XAUUSDm
    "%PYTHON%" models\train_lgbm.py --symbols XAUUSDm
)
if "%TCHOICE%"=="3" (
    "%PYTHON%" models\train_xgb.py  --hyperopt
    "%PYTHON%" models\train_lgbm.py --hyperopt
)
goto DONE

:: ===========================================================
:: 7. Train LSTM
:: ===========================================================
:TRAIN_LSTM
cls
echo.
echo.
echo  Training LSTM + Attention
echo  (takes 1-3 hours)
echo.
echo.
"%PYTHON%" models\train_lstm.py --symbols XAUUSDm
goto DONE

:: ===========================================================
:: 8. Backtest + Checklist
:: ===========================================================
:BACKTEST
cls
echo.
echo.
echo  Backtest + Deploy Checklist
echo.
echo.
echo  Options:
echo   1. Deploy Checklist (recommended before live)
echo   2. Full Backtest (ensemble)
echo   3. Optimize SL/TP (Optuna)
echo   4. Walk-Forward only
echo.
set /p BCHOICE=  Choose (1-4): 
if "%BCHOICE%"=="1" "%PYTHON%" models\backtest.py --checklist
if "%BCHOICE%"=="2" "%PYTHON%" models\backtest.py --method ensemble
if "%BCHOICE%"=="3" "%PYTHON%" models\backtest.py --optimize --trials 200
if "%BCHOICE%"=="4" "%PYTHON%" models\backtest.py --method xgb
goto DONE

:: ===========================================================
:: 9. Full Retrain Pipeline
:: ===========================================================
:FULL_RETRAIN
cls
echo.
echo.
echo  Full Retrain Pipeline
echo  backup -- data -- features -- train -- backtest
echo  (takes 2-4 hours)
echo.
echo.
echo  [WARN] Backup + retrain ALL models. Confirm? (Y/N)
set /p CONFIRM=  Confirm: 
if /i not "%CONFIRM%"=="Y" goto MENU
"%PYTHON%" scripts\retrain_all.py
goto DONE

:: ===========================================================
:: A. View Bot Log
:: ===========================================================
:VIEW_BOT_LOG
cls
echo.
echo.
echo  Bot Log -- Real-time (Ctrl+C to stop)
echo.
echo.
if exist "%LOGDIR%\bot.log" (
    powershell -Command "Get-Content '%LOGDIR%\bot.log' -Wait -Tail 50"
) else (
    echo  [INFO] bot.log not found yet
)
goto DONE

:: ===========================================================
:: B. View Error Log
:: ===========================================================
:VIEW_ERROR_LOG
cls
echo.
echo.
echo  Error Log -- Last 100 lines
echo.
echo.
if exist "%LOGDIR%\errors.log" (
    powershell -Command "Get-Content '%LOGDIR%\errors.log' -Tail 100"
) else (
    echo  [INFO] No errors.log found -- no errors yet!
)
goto DONE

:: ===========================================================
:: C. Check MT5 Connection
:: ===========================================================
:CHECK_MT5
cls
echo.
echo.
echo  Checking MT5 Connection
echo.
echo.
"%PYTHON%" scripts\debug_mt5.py
goto DONE

:: ===========================================================
:: D. Check Model Files
:: ===========================================================
:CHECK_MODELS
cls
echo.
echo.
echo  Model Files Status
echo.
echo.
"%PYTHON%" -c "from models.manage_models import print_model_table, verify_model; print_model_table(); print('\nVerifying XAUUSDm models...'); verify_model('XAUUSDm')"
goto DONE

:: ===========================================================
:: E. DB Stats
:: ===========================================================
:DB_STATS
cls
echo.
echo.
echo  Database Statistics
echo.
echo.
"%PYTHON%" bot\metrics_writer.py --stats
goto DONE

:: ===========================================================
:: Done -- กลับ menu
:: ===========================================================
:DONE
echo.
echo  --------------------------------------------
echo   Done. Press any key to return to menu...
echo  --------------------------------------------
pause
goto MENU

:: ===========================================================
:: Exit
:: ===========================================================
:: ===========================================================
:: F. Scripts and Monitoring -- Sub-menu
:: ===========================================================
:SCRIPTS_MENU
cls
echo.
echo  ==================================================
echo    Scripts and Monitoring Tools
echo  ==================================================
echo.
echo  [ Checks and Validation ]
echo    1. Test Config  (check config.yaml + .env + MT5)
echo    2. Check Symbols  (verify MT5 symbols)
echo    3. Daily Checklist  (daily checklist)
echo    4. Final Checklist  (check before go live)
echo    5. Walk-Forward Test  (OOS validation)
echo    6. Optimize SL/TP  (tune SL/TP with Optuna)
echo  --------------------------------------------------
echo  [ Monitoring ]
echo    7. Paper Trading Monitor  (paper trade summary)
echo    8. Paper Trading Weekly   (weekly summary)
echo    9. Live Monitor  (dashboard real-time)
echo  --------------------------------------------------
echo  [ Analysis ]
echo    L. Analyze Logs  (analyze trade history)
echo  --------------------------------------------------
echo    0. Back to Main Menu
echo.
set /p SCHOICE=  Enter choice: 

if "%SCHOICE%"=="1" goto SC_TEST_CONFIG
if "%SCHOICE%"=="2" goto SC_CHECK_SYMBOLS
if "%SCHOICE%"=="3" goto SC_DAILY
if "%SCHOICE%"=="4" goto SC_FINAL
if "%SCHOICE%"=="5" goto SC_WALKFORWARD
if "%SCHOICE%"=="6" goto SC_OPTIMIZE
if "%SCHOICE%"=="7" goto SC_PAPER_DAILY
if "%SCHOICE%"=="8" goto SC_PAPER_WEEKLY
if "%SCHOICE%"=="9" goto SC_LIVE_MONITOR
if /i "%SCHOICE%"=="L" goto SC_ANALYZE_LOGS
if "%SCHOICE%"=="0" goto MENU

echo  Invalid choice.
timeout /t 2 /nobreak >nul
goto SCRIPTS_MENU

:SC_TEST_CONFIG
cls
echo  [Test Config] Checking config.yaml + .env + MT5
echo.
"%PYTHON%" test_config.py
goto SCRIPTS_DONE

:SC_CHECK_SYMBOLS
cls
echo  [Check Symbols] Verifying MT5 symbols
echo.
"%PYTHON%" scripts\cheack_symbols.py
goto SCRIPTS_DONE

:SC_DAILY
cls
echo  [Daily Checklist]
echo   1. Morning check
echo   2. Evening review
echo   3. Weekly review
echo.
set /p DCHOICE=  Choose (1-3): 
if "%DCHOICE%"=="1" "%PYTHON%" scripts\daily_checklist.py morning
if "%DCHOICE%"=="2" "%PYTHON%" scripts\daily_checklist.py evening
if "%DCHOICE%"=="3" "%PYTHON%" scripts\daily_checklist.py week
goto SCRIPTS_DONE

:SC_FINAL
cls
echo  [Final Checklist] Pre-live verification
echo.
"%PYTHON%" scripts\final_checklist.py
goto SCRIPTS_DONE

:SC_WALKFORWARD
cls
echo  [Walk-Forward Test] OOS validation
echo.
"%PYTHON%" walk_forward.py
goto SCRIPTS_DONE

:SC_OPTIMIZE
cls
echo  [Optimize] Tuning SL/TP with Optuna
echo.
"%PYTHON%" optimize.py
goto SCRIPTS_DONE

:SC_PAPER_DAILY
cls
echo  [Paper Trading] Daily report
echo.
"%PYTHON%" scripts\paper_trading_monitor.py --daily 1
goto SCRIPTS_DONE

:SC_PAPER_WEEKLY
cls
echo  [Paper Trading] Weekly report + Go-Live criteria
echo.
"%PYTHON%" scripts\paper_trading_monitor.py --weekly
goto SCRIPTS_DONE

:SC_LIVE_MONITOR
cls
echo  [Live Monitor] Real-time dashboard (Ctrl+C to stop)
echo.
"%PYTHON%" scripts\live_monitor.py
goto SCRIPTS_DONE

:SC_ANALYZE_LOGS
cls
echo  [Analyze Logs] Analyzing trade history
echo.
"%PYTHON%" scripts\analyze_logs.py
goto SCRIPTS_DONE

:SCRIPTS_DONE
echo.
echo  --------------------------------------------------
echo   Done. Press any key to return to Scripts menu...
echo  --------------------------------------------------
pause
goto SCRIPTS_MENU

:: ===========================================================
:EXIT
cls
echo.
echo  Goodbye! 
echo.
exit /b 0