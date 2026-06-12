# scripts/nssm_setup.ps1
# ════════════════════════════════════════════════════════════
# NSSM Service Setup — Trading Bot + Dashboard
# รันครั้งเดียวหลัง install.ps1 เสร็จ
#
# Services ที่สร้าง:
#   TradingBot       → bot/main.py       (main loop)
#   TradingDashboard → dashboard/app.py  (Streamlit)
#   TelegramBot      → dashboard/telegram_bot.py (optional)
#
# NSSM Features ที่ใช้:
#   - Auto-start เมื่อ Windows เปิด
#   - Restart อัตโนมัติถ้า crash
#   - Log stdout/stderr แยกไฟล์
#   - Log rotation ทุก 10MB
#   - Environment variables จาก .env
# ════════════════════════════════════════════════════════════

#Requires -RunAsAdministrator

param(
    [string]$InstallDir  = "C:\trading-bot",
    [string]$PythonExe   = "C:\Python311\python.exe",
    [string]$NSSMExe     = "C:\Windows\System32\nssm.exe",
    [switch]$RemoveFirst = $false,   # ลบ service เก่าก่อน
    [switch]$SkipTelegram= $false,   # ไม่สร้าง TelegramBot service
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"


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
function Write-Fail { param([string]$m) Write-Host "  ❌ $m" -ForegroundColor Red    }
function Write-Info { param([string]$m) Write-Host "     $m" -ForegroundColor Gray   }

function Test-NSSM {
    if (-not (Test-Path $NSSMExe)) {
        Write-Fail "ไม่พบ NSSM: $NSSMExe"
        Write-Info "รัน install.ps1 ก่อน"
        exit 1
    }
}

function Get-ServiceStatus {
    param([string]$Name)
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($svc) { return $svc.Status }
    return "NotFound"
}

function Remove-NSSMService {
    param([string]$Name)
    $status = Get-ServiceStatus -Name $Name
    if ($status -ne "NotFound") {
        Write-Info "Removing existing service: $Name"
        if ($status -eq "Running") {
            & $NSSMExe stop $Name | Out-Null
            Start-Sleep -Seconds 3
        }
        & $NSSMExe remove $Name confirm | Out-Null
        Start-Sleep -Seconds 2
        Write-OK "Removed: $Name"
    }
}

function Install-BotService {
    param(
        [string]$Name,
        [string]$DisplayName,
        [string]$Description,
        [string]$Script,
        [string]$Arguments  = "",
        [string]$WorkDir,
        [string]$StdoutLog,
        [string]$StderrLog,
        [int]   $RestartDelay = 10000,   # ms
        [int]   $MaxRestarts  = 5,
        [int]   $RestartWindow= 60000,   # ms
    )

    Write-Info "Creating service: $Name"

    # ── Install service ──────────────────────────────────
    if ($Arguments) {
        & $NSSMExe install $Name $PythonExe $Script $Arguments
    } else {
        & $NSSMExe install $Name $PythonExe $Script
    }

    # ── Working directory ─────────────────────────────────
    & $NSSMExe set $Name AppDirectory $WorkDir

    # ── Display info ──────────────────────────────────────
    & $NSSMExe set $Name DisplayName  $DisplayName
    & $NSSMExe set $Name Description  $Description

    # ── Startup type: Automatic ───────────────────────────
    & $NSSMExe set $Name Start SERVICE_AUTO_START

    # ── Run as LocalSystem ────────────────────────────────
    & $NSSMExe set $Name ObjectName   LocalSystem

    # ── stdout/stderr logging ─────────────────────────────
    & $NSSMExe set $Name AppStdout     $StdoutLog
    & $NSSMExe set $Name AppStderr     $StderrLog

    # Log rotation: rotate ทุก 10MB
    & $NSSMExe set $Name AppRotateFiles     1
    & $NSSMExe set $Name AppRotateBytes     10485760   # 10 MB
    & $NSSMExe set $Name AppRotateOnline    1          # rotate ขณะ running

    # ── Restart policy ────────────────────────────────────
    # Restart ถ้า exit ด้วย error code อะไรก็ตาม
    & $NSSMExe set $Name AppExit       Default Restart
    & $NSSMExe set $Name AppRestartDelay  $RestartDelay
    & $NSSMExe set $Name AppThrottle      $RestartDelay

    # ── Environment variables ─────────────────────────────
    # โหลด .env เข้า service environment
    $envFile = "$WorkDir\.env"
    if (Test-Path $envFile) {
        $envVars = @()
        Get-Content $envFile | ForEach-Object {
            if ($_ -match '^\s*([^#][^=]+)=(.+)$') {
                $key = $Matches[1].Trim()
                $val = $Matches[2].Trim()
                $envVars += "$key=$val"
            }
        }
        if ($envVars.Count -gt 0) {
            $envString = $envVars -join "`n"
            & $NSSMExe set $Name AppEnvironmentExtra $envString
            Write-Info "  Loaded $($envVars.Count) env vars from .env"
        }
    } else {
        Write-Warn "  ไม่พบ .env — service จะโหลด env จาก system"
    }

    # ── Priority ──────────────────────────────────────────
    & $NSSMExe set $Name AppPriority ABOVE_NORMAL_PRIORITY_CLASS

    Write-OK "Service configured: $Name"
}


# ══════════════════════════════════════════════════════════
# Pre-checks
# ══════════════════════════════════════════════════════════
Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Yellow
Write-Host "║   NSSM Service Setup — Trading Bot  ║" -ForegroundColor Yellow
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Yellow
Write-Host ""

Test-NSSM

# ตรวจ files ที่จำเป็น
$required = @(
    $PythonExe,
    "$InstallDir\bot\main.py",
    "$InstallDir\dashboard\app.py",
    "$InstallDir\config.yaml",
)
foreach ($f in $required) {
    if (-not (Test-Path $f)) {
        Write-Fail "ไม่พบไฟล์: $f"
        Write-Info "รัน install.ps1 ก่อน"
        exit 1
    }
}
Write-OK "Pre-checks passed"


# ══════════════════════════════════════════════════════════
# Service 1: TradingBot
# ══════════════════════════════════════════════════════════
Write-Step "Service 1/3: TradingBot (main loop)"

$BOT_NAME    = "TradingBot"
$BOT_SCRIPT  = "$InstallDir\bot\main.py"
$BOT_STDOUT  = "$InstallDir\logs\bot_stdout.log"
$BOT_STDERR  = "$InstallDir\logs\bot_stderr.log"

if ($RemoveFirst) {
    Remove-NSSMService -Name $BOT_NAME
}

$status = Get-ServiceStatus -Name $BOT_NAME
if ($status -ne "NotFound") {
    Write-Warn "Service '$BOT_NAME' มีอยู่แล้ว (status=$status)"
    Write-Info "ใช้ -RemoveFirst เพื่อสร้างใหม่"
} else {
    Install-BotService `
        -Name         $BOT_NAME `
        -DisplayName  "AURUM Trading Bot" `
        -Description  "Gold & Forex Trading Bot — Main Loop" `
        -Script       $BOT_SCRIPT `
        -WorkDir      $InstallDir `
        -StdoutLog    $BOT_STDOUT `
        -StderrLog    $BOT_STDERR `
        -RestartDelay 15000   # รอ 15 วินาทีก่อน restart
}


# ══════════════════════════════════════════════════════════
# Service 2: TradingDashboard (Streamlit)
# ══════════════════════════════════════════════════════════
Write-Step "Service 2/3: TradingDashboard (Streamlit)"

$DASH_NAME   = "TradingDashboard"
$DASH_ARGS   = "-m streamlit run dashboard\app.py " +
               "--server.port 8501 " +
               "--server.headless true " +
               "--server.address 0.0.0.0 " +
               "--server.enableCORS false " +
               "--browser.gatherUsageStats false"
$DASH_STDOUT = "$InstallDir\logs\dashboard_stdout.log"
$DASH_STDERR = "$InstallDir\logs\dashboard_stderr.log"

if ($RemoveFirst) {
    Remove-NSSMService -Name $DASH_NAME
}

$status = Get-ServiceStatus -Name $DASH_NAME
if ($status -ne "NotFound") {
    Write-Warn "Service '$DASH_NAME' มีอยู่แล้ว (status=$status)"
} else {
    Install-BotService `
        -Name         $DASH_NAME `
        -DisplayName  "AURUM Trading Dashboard" `
        -Description  "Streamlit Dashboard — Port 8501" `
        -Script       "" `
        -Arguments    $DASH_ARGS `
        -WorkDir      $InstallDir `
        -StdoutLog    $DASH_STDOUT `
        -StderrLog    $DASH_STDERR `
        -RestartDelay 10000

    # Override: Streamlit ใช้ module ไม่ใช่ script
    & $NSSMExe set $DASH_NAME Application    $PythonExe
    & $NSSMExe set $DASH_NAME AppParameters  $DASH_ARGS
    Write-OK "Streamlit arguments configured"
}


# ══════════════════════════════════════════════════════════
# Service 3: TelegramBot (optional)
# ══════════════════════════════════════════════════════════
Write-Step "Service 3/3: TelegramBot (command bot)"

$TG_NAME   = "TelegramBot"
$TG_SCRIPT = "$InstallDir\dashboard\telegram_bot.py"

if ($SkipTelegram) {
    Write-Info "Skipping TelegramBot (--SkipTelegram flag)"
} elseif (-not (Test-Path $TG_SCRIPT)) {
    Write-Warn "ไม่พบ $TG_SCRIPT — ข้าม"
} else {
    if ($RemoveFirst) {
        Remove-NSSMService -Name $TG_NAME
    }

    $status = Get-ServiceStatus -Name $TG_NAME
    if ($status -ne "NotFound") {
        Write-Warn "Service '$TG_NAME' มีอยู่แล้ว"
    } else {
        Install-BotService `
            -Name         $TG_NAME `
            -DisplayName  "AURUM Telegram Bot" `
            -Description  "Telegram Command Bot — /status /closeall" `
            -Script       $TG_SCRIPT `
            -WorkDir      $InstallDir `
            -StdoutLog    "$InstallDir\logs\telegram_stdout.log" `
            -StderrLog    "$InstallDir\logs\telegram_stderr.log" `
            -RestartDelay 30000   # Telegram มี reconnect built-in รอนานกว่า
    }
}


# ══════════════════════════════════════════════════════════
# Start Services
# ══════════════════════════════════════════════════════════
Write-Step "Starting Services"

$servicesToStart = @($BOT_NAME, $DASH_NAME)
if (-not $SkipTelegram) { $servicesToStart += $TG_NAME }

foreach ($svcName in $servicesToStart) {
    $status = Get-ServiceStatus -Name $svcName
    if ($status -eq "NotFound") {
        Write-Warn "$svcName: ไม่พบ service"
        continue
    }

    if ($status -eq "Running") {
        Write-OK "$svcName: กำลังรันอยู่แล้ว"
        continue
    }

    Write-Info "Starting $svcName..."
    try {
        & $NSSMExe start $svcName | Out-Null
        Start-Sleep -Seconds 3

        $newStatus = Get-ServiceStatus -Name $svcName
        if ($newStatus -eq "Running") {
            Write-OK "$svcName: Started ✅"
        } else {
            Write-Warn "$svcName: status=$newStatus (อาจยังไม่พร้อม)"
        }
    } catch {
        Write-Fail "$svcName: Failed to start — $_"
    }
}


# ══════════════════════════════════════════════════════════
# Verify
# ══════════════════════════════════════════════════════════
Write-Step "Service Status Verification"

Start-Sleep -Seconds 5

foreach ($svcName in $servicesToStart) {
    $status = Get-ServiceStatus -Name $svcName
    $icon   = if ($status -eq "Running") { "✅" } else { "⚠️" }
    Write-Host "  $icon $svcName : $status" -ForegroundColor (
        if ($status -eq "Running") { "Green" } else { "Yellow" }
    )
}


# ══════════════════════════════════════════════════════════
# Summary & Next Steps
# ══════════════════════════════════════════════════════════
Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║   Services Setup Complete! 🚀        ║" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  Dashboard URL: http://$(hostname):8501" -ForegroundColor Cyan
Write-Host ""
Write-Host "  คำสั่งที่ใช้บ่อย:" -ForegroundColor Yellow
Write-Host ""
Write-Host "  # ดูสถานะ" -ForegroundColor DarkGray
Write-Host "  nssm status TradingBot" -ForegroundColor White
Write-Host "  nssm status TradingDashboard" -ForegroundColor White
Write-Host ""
Write-Host "  # หยุด / เริ่ม / restart" -ForegroundColor DarkGray
Write-Host "  nssm stop    TradingBot" -ForegroundColor White
Write-Host "  nssm start   TradingBot" -ForegroundColor White
Write-Host "  nssm restart TradingBot" -ForegroundColor White
Write-Host ""
Write-Host "  # ดู log real-time" -ForegroundColor DarkGray
Write-Host "  Get-Content logs\bot_stdout.log -Wait -Tail 50" -ForegroundColor White
Write-Host "  Get-Content logs\bot_stderr.log -Wait -Tail 20" -ForegroundColor White
Write-Host ""
Write-Host "  # แก้ไข service config" -ForegroundColor DarkGray
Write-Host "  nssm edit TradingBot" -ForegroundColor White
Write-Host ""
Write-Host "  # GUI จัดการ services" -ForegroundColor DarkGray
Write-Host "  services.msc" -ForegroundColor White
Write-Host ""