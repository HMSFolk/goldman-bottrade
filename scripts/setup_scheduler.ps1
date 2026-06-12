# scripts/setup_scheduler.ps1
# ════════════════════════════════════════════════════════════
# Windows Task Scheduler Setup
# สร้าง scheduled tasks ทั้งหมดสำหรับ trading bot
#
# Tasks ที่สร้าง:
#   BotUpdateData    → อัพเดตข้อมูลทุกคืน ตี 1 UTC
#   BotBuildFeatures → สร้าง features ทุกคืน ตี 1:30 UTC
#   BotRetrain       → retrain โมเดลทุกอาทิตย์ อาทิตย์ ตี 2 UTC
#   BotBacktest      → backtest ทุก 2 สัปดาห์ ตี 4 UTC
#   BotDBCleanup     → ลบข้อมูลเก่าทุกเดือน วันที่ 1 ตี 3 UTC
#   BotHealthCheck   → ตรวจสอบ bot ทุกชั่วโมง
# ════════════════════════════════════════════════════════════

#Requires -RunAsAdministrator

param(
    [string]$InstallDir = "C:\Goldman-Bot",
    [string]$PythonExe  = "C:\Python313\python.exe",   # ✅ แก้เป็น 313
    [string]$LogDir     = "C:\Goldman-Bot\logs",
    [switch]$RemoveAll  = $false,   # ลบ tasks เก่าก่อนสร้างใหม่
    [switch]$ListOnly   = $false,   # แค่แสดง tasks ไม่สร้าง
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TASK_FOLDER = "\TradingBot\"   # Task Scheduler folder


# ══════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════
function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "─────────────────────────────────────" -ForegroundColor DarkGray
    Write-Host "  $Message" -ForegroundColor Cyan
    Write-Host "─────────────────────────────────────" -ForegroundColor DarkGray
}
function Write-OK   { param([string]$m) Write-Host "  ✅ $m" -ForegroundColor Green  }
function Write-Warn { param([string]$m) Write-Host "  ⚠️  $m" -ForegroundColor Yellow }
function Write-Info { param([string]$m) Write-Host "     $m" -ForegroundColor Gray   }

function Remove-TaskIfExists {
    param([string]$Name)
    $existing = Get-ScheduledTask -TaskName $Name `
                -TaskPath $TASK_FOLDER `
                -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask `
            -TaskName $Name `
            -TaskPath $TASK_FOLDER `
            -Confirm:$false
        Write-Info "Removed existing task: $Name"
    }
}

function New-BotTask {
    param(
        [string]  $Name,
        [string]  $Description,
        [string]  $Script,
        [string]  $Arguments   = "",
        [object]  $Trigger,
        [string]  $LogFile,
        [int]     $TimeoutMins = 120,
        [bool]    $RunOnBattery= $false
    )

    # Action: python script >> logfile 2>&1
    $cmd     = $PythonExe
    $fullArg = if ($Arguments) {
        "$Script $Arguments >> `"$LogFile`" 2>&1"
    } else {
        "$Script >> `"$LogFile`" 2>&1"
    }

    # ใช้ cmd /c เพื่อ redirect ได้
    $action  = New-ScheduledTaskAction `
        -Execute  "cmd.exe" `
        -Argument "/c `"$cmd $fullArg`"" `
        -WorkingDirectory $InstallDir

    # Settings
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit        (New-TimeSpan -Minutes $TimeoutMins) `
        -MultipleInstances         IgnoreNew `
        -DisallowStartIfOnBatteries (-not $RunOnBattery) `
        -StopIfGoingOnBatteries    $false `
        -RunOnlyIfNetworkAvailable $false `
        -StartWhenAvailable        $true `
        -WakeToRun                 $false

    # Principal: ให้รันในฐานะ SYSTEM (ไม่ต้อง login)
    $principal = New-ScheduledTaskPrincipal `
        -UserId    "NT AUTHORITY\SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel  Highest

    # Register
    Register-ScheduledTask `
        -TaskName   $Name `
        -TaskPath   $TASK_FOLDER `
        -Action     $action `
        -Trigger    $Trigger `
        -Settings   $settings `
        -Principal  $principal `
        -Description $Description `
        -Force | Out-Null

    Write-OK "Created: $Name"
    Write-Info "  Schedule: $($Trigger.GetType().Name)"
    Write-Info "  Script:   $Script"
    Write-Info "  Log:      $LogFile"
}


# ══════════════════════════════════════════════════════════
# Pre-checks
# ══════════════════════════════════════════════════════════
Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Yellow
Write-Host "║   Task Scheduler Setup               ║" -ForegroundColor Yellow
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Yellow

if (-not (Test-Path $PythonExe)) {
    Write-Host "❌ ไม่พบ Python: $PythonExe" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $InstallDir)) {
    Write-Host "❌ ไม่พบ project: $InstallDir" -ForegroundColor Red
    exit 1
}

# สร้าง Task folder
$scheduler = New-Object -ComObject "Schedule.Service"
$scheduler.Connect()
$rootFolder = $scheduler.GetFolder("\")
try {
    $rootFolder.GetFolder($TASK_FOLDER) | Out-Null
} catch {
    $rootFolder.CreateFolder($TASK_FOLDER.Trim("\")) | Out-Null
    Write-OK "Created Task folder: $TASK_FOLDER"
}

# สร้าง log dir
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

# ListOnly mode
if ($ListOnly) {
    Write-Host ""
    Write-Host "Tasks ที่จะสร้าง:" -ForegroundColor Yellow
    @(
        "BotUpdateData     → ทุกคืน ตี 1:00 UTC   — อัพเดตข้อมูล"
        "BotBuildFeatures  → ทุกคืน ตี 1:30 UTC   — build features"
        "BotRetrain        → อาทิตย์ ตี 2:00 UTC  — retrain โมเดล"
        "BotBacktest       → 2 สัปดาห์ ตี 4:00 UTC — backtest"
        "BotDBCleanup      → วันที่ 1 ตี 3:00 UTC  — ลบข้อมูลเก่า"
        "BotHealthCheck    → ทุกชั่วโมง             — health check"
    ) | ForEach-Object { Write-Host "  • $_" -ForegroundColor Gray }
    exit 0
}

# Remove all existing tasks
if ($RemoveAll) {
    Write-Step "Removing existing tasks"
    @("BotUpdateData","BotBuildFeatures","BotRetrain",
      "BotBacktest","BotDBCleanup","BotHealthCheck") | ForEach-Object {
        Remove-TaskIfExists -Name $_
    }
}


# ══════════════════════════════════════════════════════════
# Task 1: Update Data — ทุกคืน ตี 1:00 UTC
# ══════════════════════════════════════════════════════════
Write-Step "Task 1/6: BotUpdateData"

Remove-TaskIfExists -Name "BotUpdateData"

$trigger1 = New-ScheduledTaskTrigger `
    -Daily `
    -At "01:00AM"   # UTC 01:00 = ตลาดปิดแล้ว

New-BotTask `
    -Name        "BotUpdateData" `
    -Description "อัพเดตข้อมูลราคา + macro + news ทุกคืน" `
    -Script      "data\pipeline.py" `
    -Trigger     $trigger1 `
    -LogFile     "$LogDir\scheduler_update_data.log" `
    -TimeoutMins 60


# ══════════════════════════════════════════════════════════
# Task 2: Build Features — ทุกคืน ตี 1:30 UTC
# ══════════════════════════════════════════════════════════
Write-Step "Task 2/6: BotBuildFeatures"

Remove-TaskIfExists -Name "BotBuildFeatures"

# รันหลัง UpdateData เสร็จ (30 นาที buffer)
$trigger2 = New-ScheduledTaskTrigger `
    -Daily `
    -At "01:30AM"

New-BotTask `
    -Name        "BotBuildFeatures" `
    -Description "สร้าง ML features จากข้อมูลใหม่" `
    -Script      "features\pipeline.py" `
    -Trigger     $trigger2 `
    -LogFile     "$LogDir\scheduler_features.log" `
    -TimeoutMins 45


# ══════════════════════════════════════════════════════════
# Task 3: Retrain Models — ทุกอาทิตย์ ตี 2:00 UTC
# ══════════════════════════════════════════════════════════
Write-Step "Task 3/6: BotRetrain"

Remove-TaskIfExists -Name "BotRetrain"

$trigger3 = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Sunday `
    -At "02:00AM"

New-BotTask `
    -Name        "BotRetrain" `
    -Description "Retrain XGB + LGBM + LSTM ทุกอาทิตย์" `
    -Script      "scripts\retrain_all.py" `
    -Trigger     $trigger3 `
    -LogFile     "$LogDir\scheduler_retrain.log" `
    -TimeoutMins 240   # 4 ชั่วโมง (LSTM ใช้เวลานาน)


# ══════════════════════════════════════════════════════════
# Task 4: Backtest — ทุก 2 สัปดาห์ ตี 4:00 UTC
# ══════════════════════════════════════════════════════════
Write-Step "Task 4/6: BotBacktest"

Remove-TaskIfExists -Name "BotBacktest"

# Task Scheduler ไม่มี biweekly trigger ต้องใช้ custom
# วิธีแก้: ใช้ Weekly แต่ตรวจสัปดาห์คี่/คู่ใน script
$trigger4 = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 2 `
    -DaysOfWeek Sunday `
    -At "04:00AM"

New-BotTask `
    -Name        "BotBacktest" `
    -Description "Backtest + deploy checklist ทุก 2 สัปดาห์" `
    -Script      "models\backtest.py" `
    -Arguments   "--checklist" `
    -Trigger     $trigger4 `
    -LogFile     "$LogDir\scheduler_backtest.log" `
    -TimeoutMins 120


# ══════════════════════════════════════════════════════════
# Task 5: DB Cleanup — วันที่ 1 ของทุกเดือน ตี 3:00 UTC
# ══════════════════════════════════════════════════════════
Write-Step "Task 5/6: BotDBCleanup"

Remove-TaskIfExists -Name "BotDBCleanup"

$trigger5 = New-ScheduledTaskTrigger `
    -Monthly `
    -DaysOfMonth 1 `
    -At "03:00AM"

New-BotTask `
    -Name        "BotDBCleanup" `
    -Description "ลบข้อมูลเก่า + VACUUM SQLite ทุกเดือน" `
    -Script      "bot\metrics_writer.py" `
    -Arguments   "--cleanup --vacuum" `
    -Trigger     $trigger5 `
    -LogFile     "$LogDir\scheduler_cleanup.log" `
    -TimeoutMins 30


# ══════════════════════════════════════════════════════════
# Task 6: Health Check — ทุกชั่วโมง
# ══════════════════════════════════════════════════════════
Write-Step "Task 6/6: BotHealthCheck"

Remove-TaskIfExists -Name "BotHealthCheck"

$trigger6 = New-ScheduledTaskTrigger `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -Once `
    -At (Get-Date)

# สร้าง health check script
$healthScript = @"
# scripts/health_check.py
import MetaTrader5 as mt5
import subprocess, sys, json
from pathlib import Path
from datetime import datetime, timezone
from bot.notifier import notify

def check_services():
    services = ['TradingBot', 'TradingDashboard']
    restarted = []
    for svc in services:
        result = subprocess.run(
            ['sc', 'query', svc],
            capture_output=True, text=True
        )
        if 'RUNNING' not in result.stdout:
            print(f"[WARN] {svc} not running — restarting...")
            subprocess.run(['nssm', 'restart', svc])
            restarted.append(svc)
    return restarted

def check_mt5():
    if not mt5.initialize():
        return False
    info = mt5.terminal_info()
    mt5.shutdown()
    return info is not None

def check_last_tick():
    # ตรวจว่า bot ทำ tick ล่าสุดไม่นานเกิน 30 นาที
    acc_json = Path('logs/account.json')
    if not acc_json.exists():
        return False, 'no account.json'
    data     = json.loads(acc_json.read_text())
    updated  = data.get('updated_at','')
    if not updated:
        return False, 'no updated_at'
    from datetime import datetime, timezone
    last = datetime.fromisoformat(updated)
    age  = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return age < 30, f'last tick {age:.0f}min ago'

if __name__ == '__main__':
    issues   = []
    restarted= check_services()
    if restarted:
        issues.append(f'Restarted: {restarted}')
    if not check_mt5():
        issues.append('MT5 not responding')
    tick_ok, tick_msg = check_last_tick()
    if not tick_ok:
        issues.append(f'Stale tick: {tick_msg}')
    if issues:
        notify(f'⚠️ Health Check Issues:\n' + '\n'.join(issues))
        print(f'Issues found: {issues}')
    else:
        print(f'Health OK: {datetime.now(timezone.utc).isoformat()}')
"@

$healthScriptPath = "$InstallDir\scripts\health_check.py"
$healthScript | Out-File -FilePath $healthScriptPath -Encoding UTF8

New-BotTask `
    -Name        "BotHealthCheck" `
    -Description "ตรวจสอบ services + MT5 + last tick ทุกชั่วโมง" `
    -Script      "scripts\health_check.py" `
    -Trigger     $trigger6 `
    -LogFile     "$LogDir\scheduler_health.log" `
    -TimeoutMins 10


# ══════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════
Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║   Task Scheduler Setup Complete!     ║" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""

# แสดง tasks ทั้งหมด
$tasks = Get-ScheduledTask -TaskPath $TASK_FOLDER -ErrorAction SilentlyContinue
if ($tasks) {
    Write-Host "  Tasks สร้างแล้ว:" -ForegroundColor Yellow
    foreach ($t in $tasks) {
        $info   = Get-ScheduledTaskInfo -TaskName $t.TaskName `
                  -TaskPath $TASK_FOLDER -ErrorAction SilentlyContinue
        $next   = if ($info.NextRunTime) {
                      $info.NextRunTime.ToString("yyyy-MM-dd HH:mm")
                  } else { "N/A" }
        $state  = $t.State
        $icon   = if ($state -eq "Ready") { "✅" } else { "⚠️" }
        Write-Host ("  $icon {0,-20} Next: {1}" -f $t.TaskName, $next) `
                   -ForegroundColor (if ($state -eq "Ready") { "White" } else { "Yellow" })
    }
}

Write-Host ""
Write-Host "  คำสั่งที่ใช้บ่อย:" -ForegroundColor Yellow
Write-Host ""
Write-Host "  # ดู tasks ทั้งหมด" -ForegroundColor DarkGray
Write-Host "  Get-ScheduledTask -TaskPath '\TradingBot\'" -ForegroundColor White
Write-Host ""
Write-Host "  # รัน task ทันที (ทดสอบ)" -ForegroundColor DarkGray
Write-Host "  Start-ScheduledTask -TaskName 'BotUpdateData' -TaskPath '\TradingBot\'" -ForegroundColor White
Write-Host ""
Write-Host "  # ดู log ล่าสุด" -ForegroundColor DarkGray
Write-Host "  Get-Content logs\scheduler_retrain.log -Tail 30" -ForegroundColor White
Write-Host ""
Write-Host "  # เปิด Task Scheduler GUI" -ForegroundColor DarkGray
Write-Host "  taskschd.msc" -ForegroundColor White
Write-Host ""