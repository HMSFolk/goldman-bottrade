# scripts/nssm_setup.ps1
# Requires -RunAsAdministrator

param(
    [string]$InstallDir  = "C:\Goldman_Bot",
    [string]$PythonExe   = "C:\Python313\python.exe", # ✅ แก้พาธเป็น 313
    [string]$NSSMExe     = "C:\Windows\System32\nssm.exe",
    [switch]$RemoveFirst = $false,
    [switch]$SkipTelegram= $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Install-BotService {
    param($Name, $DisplayName, $Script, $Arguments="", $WorkDir, $StdoutLog, $StderrLog)
    if ($Arguments) { & $NSSMExe install $Name $PythonExe $Script $Arguments } 
    else { & $NSSMExe install $Name $PythonExe $Script }
    & $NSSMExe set $Name AppDirectory $WorkDir
    & $NSSMExe set $Name DisplayName $DisplayName
    & $NSSMExe set $Name Start SERVICE_AUTO_START
    & $NSSMExe set $Name ObjectName LocalSystem
    & $NSSMExe set $Name AppStdout $StdoutLog
    & $NSSMExe set $Name AppStderr $StderrLog
    & $NSSMExe set $Name AppRotateFiles 1
    & $NSSMExe set $Name AppRotateBytes 10485760
    & $NSSMExe set $Name AppExit Default Restart
    Write-Host "✅ Service configured: $Name" -ForegroundColor Green
}

if ($RemoveFirst) {
    & $NSSMExe stop TradingBot | Out-Null; & $NSSMExe remove TradingBot confirm | Out-Null
    & $NSSMExe stop TradingDashboard | Out-Null; & $NSSMExe remove TradingDashboard confirm | Out-Null
}

Install-BotService -Name "TradingBot" -DisplayName "AURUM Trading Bot" -Script "$InstallDir\bot\main.py" -WorkDir $InstallDir -StdoutLog "$InstallDir\logs\bot_stdout.log" -StderrLog "$InstallDir\logs\bot_stderr.log"

$DASH_ARGS = "-m streamlit run dashboard\app.py --server.port 8501 --server.headless true"
Install-BotService -Name "TradingDashboard" -DisplayName "AURUM Dashboard" -Script "" -Arguments $DASH_ARGS -WorkDir $InstallDir -StdoutLog "$InstallDir\logs\dash_stdout.log" -StderrLog "$InstallDir\logs\dash_stderr.log"
& $NSSMExe set TradingDashboard Application $PythonExe
& $NSSMExe set TradingDashboard AppParameters $DASH_ARGS

& $NSSMExe start TradingBot
& $NSSMExe start TradingDashboard
Write-Host "🚀 Services Started!" -ForegroundColor Cyan