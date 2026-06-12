<<<<<<< HEAD
# scripts/start_dashboard.ps1
# ════════════════════════════════════════════════════════════
# Dashboard Setup — Streamlit on port 8501
# รันครั้งเดียวหลัง nssm_setup.ps1 เสร็จ
#
# ทำอะไร:
#   1. ตรวจสอบ dependencies
#   2. เปิด Firewall port 8501
#   3. สร้าง NSSM service TradingDashboard
#   4. Start service
#   5. ทดสอบ HTTP connection
#   6. แสดง URL สำหรับเข้าถึง
# ════════════════════════════════════════════════════════════

#Requires -RunAsAdministrator

param(
    [string]$InstallDir    = "C:\trading-bot",
    [string]$PythonExe     = "C:\Python311\python.exe",
    [string]$NSSMExe       = "C:\Windows\System32\nssm.exe",
    [int]   $Port          = 8501,
    [string]$BindAddress   = "0.0.0.0",   # เข้าจาก internet ได้
    [switch]$LocalOnly     = $false,       # เข้าได้แค่ localhost
    [switch]$Restart       = $false,       # restart service ถ้ามีอยู่แล้ว
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$SERVICE_NAME = "TradingDashboard"
$LOG_DIR      = "$InstallDir\logs"
$STDOUT_LOG   = "$LOG_DIR\dashboard_stdout.log"
$STDERR_LOG   = "$LOG_DIR\dashboard_stderr.log"


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

function Get-PublicIP {
    try {
        $ip = Invoke-RestMethod -Uri "https://api.ipify.org" `
              -TimeoutSec 5
        return $ip.Trim()
    } catch {
        return $null
    }
}

function Get-LocalIP {
    $ips = Get-NetIPAddress -AddressFamily IPv4 `
           -InterfaceAlias "Ethernet*","Local Area*" `
           -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -notmatch "^127\." }
    if ($ips) { return $ips[0].IPAddress }
    return "localhost"
}

function Test-PortOpen {
    param([int]$PortNum, [int]$WaitSec = 10)
    $deadline = (Get-Date).AddSeconds($WaitSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $tcp = New-Object Net.Sockets.TcpClient
            $tcp.Connect("127.0.0.1", $PortNum)
            $tcp.Close()
            return $true
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

function Get-ServiceStatus {
    param([string]$Name)
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($svc) { return $svc.Status }
    return "NotFound"
}


# ══════════════════════════════════════════════════════════
# Header
# ══════════════════════════════════════════════════════════
Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Yellow
Write-Host "║   Dashboard Setup — Port $Port      ║" -ForegroundColor Yellow
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Install Dir : $InstallDir"  -ForegroundColor Gray
Write-Host "  Python      : $PythonExe"   -ForegroundColor Gray
Write-Host "  Port        : $Port"         -ForegroundColor Gray
Write-Host "  Bind        : $BindAddress"  -ForegroundColor Gray


# ══════════════════════════════════════════════════════════
# Step 1: Pre-checks
# ══════════════════════════════════════════════════════════
Write-Step "Step 1/6: Pre-checks"

# ตรวจ Python
if (-not (Test-Path $PythonExe)) {
    Write-Fail "ไม่พบ Python: $PythonExe"
    exit 1
}
Write-OK "Python: $PythonExe"

# ตรวจ NSSM
if (-not (Test-Path $NSSMExe)) {
    Write-Fail "ไม่พบ NSSM: $NSSMExe"
    Write-Info "รัน install.ps1 ก่อน"
    exit 1
}
Write-OK "NSSM: $NSSMExe"

# ตรวจ dashboard/app.py
$appScript = "$InstallDir\dashboard\app.py"
if (-not (Test-Path $appScript)) {
    Write-Fail "ไม่พบ: $appScript"
    exit 1
}
Write-OK "Dashboard script: $appScript"

# ตรวจ Streamlit ติดตั้งแล้ว
$stCheck = & $PythonExe -c "import streamlit; print(streamlit.__version__)" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-OK "Streamlit version: $stCheck"
} else {
    Write-Warn "Streamlit ไม่พบ — กำลังติดตั้ง..."
    & $PythonExe -m pip install streamlit --quiet
    Write-OK "Streamlit installed"
}

# ตรวจว่า port ถูกใช้อยู่ไหม
$portUsed = Get-NetTCPConnection -LocalPort $Port `
            -State Listen `
            -ErrorAction SilentlyContinue
if ($portUsed) {
    $pid   = $portUsed[0].OwningProcess
    $pname = (Get-Process -Id $pid -ErrorAction SilentlyContinue).ProcessName
    Write-Warn "Port $Port ถูกใช้อยู่โดย: $pname (PID=$pid)"
    Write-Warn "จะ restart service เพื่อเข้าครอบครอง port"
    $Restart = $true
}

# สร้าง log dir
New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null
Write-OK "Log dir: $LOG_DIR"


# ══════════════════════════════════════════════════════════
# Step 2: Firewall Rules
# ══════════════════════════════════════════════════════════
Write-Step "Step 2/6: Firewall Configuration"

$ruleName = "Streamlit Dashboard Port $Port"

# ลบ rule เก่าถ้ามี
$existing = Get-NetFirewallRule -DisplayName $ruleName `
            -ErrorAction SilentlyContinue
if ($existing) {
    Remove-NetFirewallRule -DisplayName $ruleName
    Write-Info "Removed old firewall rule"
}

if ($LocalOnly) {
    # Local only — ไม่เปิด inbound จาก internet
    Write-Info "LocalOnly mode: ไม่เปิด firewall inbound"
    Write-OK "Dashboard จะเข้าได้จาก localhost เท่านั้น"
} else {
    # เปิด inbound port 8501
    New-NetFirewallRule `
        -DisplayName  $ruleName `
        -Direction    Inbound `
        -Protocol     TCP `
        -LocalPort    $Port `
        -Action       Allow `
        -Profile      Any `
        -Description  "AURUM Bot Streamlit Dashboard" | Out-Null

    Write-OK "Firewall: port $Port opened (inbound)"

    # ตรวจ Windows Defender Firewall state
    $fwProfiles = Get-NetFirewallProfile
    foreach ($profile in $fwProfiles) {
        if ($profile.Enabled -eq $false) {
            Write-Warn "Firewall profile '$($profile.Name)' disabled"
        }
    }
}


# ══════════════════════════════════════════════════════════
# Step 3: NSSM Service Configuration
# ══════════════════════════════════════════════════════════
Write-Step "Step 3/6: NSSM Service Setup"

$svcStatus = Get-ServiceStatus -Name $SERVICE_NAME

if ($svcStatus -ne "NotFound" -and -not $Restart) {
    Write-OK "Service '$SERVICE_NAME' มีอยู่แล้ว (status=$svcStatus)"
    Write-Info "ใช้ -Restart เพื่อ recreate service"
} else {
    # หยุดและลบ service เก่า
    if ($svcStatus -ne "NotFound") {
        Write-Info "Removing existing service..."
        if ($svcStatus -eq "Running") {
            & $NSSMExe stop $SERVICE_NAME | Out-Null
            Start-Sleep -Seconds 3
        }
        & $NSSMExe remove $SERVICE_NAME confirm | Out-Null
        Start-Sleep -Seconds 2
        Write-OK "Removed old service"
    }

    # กำหนด bind address
    $bindAddr = if ($LocalOnly) { "localhost" } else { $BindAddress }

    # Streamlit arguments
    $stArgs = (
        "-m streamlit run dashboard\app.py " +
        "--server.port $Port " +
        "--server.address $bindAddr " +
        "--server.headless true " +
        "--server.enableCORS false " +
        "--server.enableXsrfProtection false " +
        "--browser.gatherUsageStats false " +
        "--logger.level info"
    )

    Write-Info "Creating NSSM service: $SERVICE_NAME"

    # Install
    & $NSSMExe install $SERVICE_NAME $PythonExe $stArgs

    # Working directory
    & $NSSMExe set $SERVICE_NAME AppDirectory $InstallDir

    # Display info
    & $NSSMExe set $SERVICE_NAME DisplayName  "AURUM Trading Dashboard"
    & $NSSMExe set $SERVICE_NAME Description  "Streamlit Dashboard — Port $Port"

    # Auto-start
    & $NSSMExe set $SERVICE_NAME Start SERVICE_AUTO_START

    # Run as SYSTEM
    & $NSSMExe set $SERVICE_NAME ObjectName LocalSystem

    # Logging
    & $NSSMExe set $SERVICE_NAME AppStdout      $STDOUT_LOG
    & $NSSMExe set $SERVICE_NAME AppStderr       $STDERR_LOG
    & $NSSMExe set $SERVICE_NAME AppRotateFiles  1
    & $NSSMExe set $SERVICE_NAME AppRotateBytes  10485760   # 10MB
    & $NSSMExe set $SERVICE_NAME AppRotateOnline 1

    # Restart policy
    & $NSSMExe set $SERVICE_NAME AppExit      Default Restart
    & $NSSMExe set $SERVICE_NAME AppRestartDelay 10000   # 10s

    # Environment vars จาก .env
    $envFile = "$InstallDir\.env"
    if (Test-Path $envFile) {
        $envVars = @()
        Get-Content $envFile | ForEach-Object {
            if ($_ -match '^\s*([^#][^=]+)=(.+)$') {
                $envVars += "$($Matches[1].Trim())=$($Matches[2].Trim())"
            }
        }
        if ($envVars.Count -gt 0) {
            & $NSSMExe set $SERVICE_NAME AppEnvironmentExtra ($envVars -join "`n")
            Write-Info "  Loaded $($envVars.Count) env vars"
        }
    }

    Write-OK "Service configured: $SERVICE_NAME"
    Write-Info "  App     : $PythonExe"
    Write-Info "  Args    : $stArgs"
    Write-Info "  WorkDir : $InstallDir"
    Write-Info "  Log     : $STDOUT_LOG"
}


# ══════════════════════════════════════════════════════════
# Step 4: Start Service
# ══════════════════════════════════════════════════════════
Write-Step "Step 4/6: Starting Service"

$currentStatus = Get-ServiceStatus -Name $SERVICE_NAME

if ($currentStatus -eq "Running") {
    if ($Restart) {
        Write-Info "Restarting service..."
        & $NSSMExe restart $SERVICE_NAME | Out-Null
        Start-Sleep -Seconds 5
        Write-OK "Service restarted"
    } else {
        Write-OK "Service already running"
    }
} else {
    Write-Info "Starting $SERVICE_NAME..."
    & $NSSMExe start $SERVICE_NAME | Out-Null
    Start-Sleep -Seconds 5

    $newStatus = Get-ServiceStatus -Name $SERVICE_NAME
    if ($newStatus -eq "Running") {
        Write-OK "Service started"
    } else {
        Write-Fail "Service failed to start (status=$newStatus)"
        Write-Info "Check stderr: Get-Content '$STDERR_LOG' -Tail 20"
        exit 1
    }
}


# ══════════════════════════════════════════════════════════
# Step 5: Test Connection
# ══════════════════════════════════════════════════════════
Write-Step "Step 5/6: Testing Connection"

Write-Info "Waiting for Streamlit to start (up to 20s)..."

if (Test-PortOpen -PortNum $Port -WaitSec 20) {
    Write-OK "Port $Port is responding"

    # ทดสอบ HTTP response
    try {
        $resp = Invoke-WebRequest `
            -Uri        "http://localhost:$Port" `
            -TimeoutSec 10 `
            -UseBasicParsing
        Write-OK "HTTP response: $($resp.StatusCode)"
    } catch {
        Write-Warn "Port open แต่ HTTP ยังไม่พร้อม (อาจต้องรออีก 5-10s)"
    }
} else {
    Write-Fail "Port $Port ไม่ตอบสนองใน 20 วินาที"
    Write-Info "ดู stderr log:"
    if (Test-Path $STDERR_LOG) {
        Get-Content $STDERR_LOG -Tail 10 |
            ForEach-Object { Write-Host "  $_" -ForegroundColor DarkRed }
    }

    Write-Info ""
    Write-Info "ลองรันด้วยมือเพื่อดู error:"
    Write-Info "  cd $InstallDir"
    Write-Info "  $PythonExe -m streamlit run dashboard\app.py --server.port $Port"
    exit 1
}


# ══════════════════════════════════════════════════════════
# Step 6: Display URLs
# ══════════════════════════════════════════════════════════
Write-Step "Step 6/6: Access URLs"

$localIP  = Get-LocalIP
$publicIP = Get-PublicIP

Write-Host ""
Write-Host "  ┌─────────────────────────────────────┐" -ForegroundColor Green
Write-Host "  │   Dashboard URLs                    │" -ForegroundColor Green
Write-Host "  ├─────────────────────────────────────┤" -ForegroundColor Green
Write-Host "  │ Local:   http://localhost:$Port    │" -ForegroundColor White
if ($localIP) {
Write-Host "  │ LAN:     http://${localIP}:$Port   │" -ForegroundColor White
}
if ($publicIP -and -not $LocalOnly) {
Write-Host "  │ Public:  http://${publicIP}:$Port  │" -ForegroundColor Cyan
}
Write-Host "  └─────────────────────────────────────┘" -ForegroundColor Green
Write-Host ""


# ══════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════
Write-Host "  Service Status:" -ForegroundColor Yellow
Write-Host "  $(& $NSSMExe status $SERVICE_NAME)" -ForegroundColor White
Write-Host ""
Write-Host "  คำสั่งที่ใช้บ่อย:" -ForegroundColor Yellow
Write-Host ""
Write-Host "  # ดูสถานะ" -ForegroundColor DarkGray
Write-Host "  nssm status $SERVICE_NAME" -ForegroundColor White
Write-Host ""
Write-Host "  # ดู log" -ForegroundColor DarkGray
Write-Host "  Get-Content '$STDOUT_LOG' -Wait -Tail 30" -ForegroundColor White
Write-Host ""
Write-Host "  # Restart" -ForegroundColor DarkGray
Write-Host "  nssm restart $SERVICE_NAME" -ForegroundColor White
Write-Host ""
Write-Host "  # หยุด" -ForegroundColor DarkGray
Write-Host "  nssm stop $SERVICE_NAME" -ForegroundColor White
=======
# scripts/start_dashboard.ps1
# ════════════════════════════════════════════════════════════
# Dashboard Setup — Streamlit on port 8501
# รันครั้งเดียวหลัง nssm_setup.ps1 เสร็จ
#
# ทำอะไร:
#   1. ตรวจสอบ dependencies
#   2. เปิด Firewall port 8501
#   3. สร้าง NSSM service TradingDashboard
#   4. Start service
#   5. ทดสอบ HTTP connection
#   6. แสดง URL สำหรับเข้าถึง
# ════════════════════════════════════════════════════════════

#Requires -RunAsAdministrator

param(
    [string]$InstallDir    = "C:\trading-bot",
    [string]$PythonExe     = "C:\Python311\python.exe",
    [string]$NSSMExe       = "C:\Windows\System32\nssm.exe",
    [int]   $Port          = 8501,
    [string]$BindAddress   = "0.0.0.0",   # เข้าจาก internet ได้
    [switch]$LocalOnly     = $false,       # เข้าได้แค่ localhost
    [switch]$Restart       = $false,       # restart service ถ้ามีอยู่แล้ว
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$SERVICE_NAME = "TradingDashboard"
$LOG_DIR      = "$InstallDir\logs"
$STDOUT_LOG   = "$LOG_DIR\dashboard_stdout.log"
$STDERR_LOG   = "$LOG_DIR\dashboard_stderr.log"


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

function Get-PublicIP {
    try {
        $ip = Invoke-RestMethod -Uri "https://api.ipify.org" `
              -TimeoutSec 5
        return $ip.Trim()
    } catch {
        return $null
    }
}

function Get-LocalIP {
    $ips = Get-NetIPAddress -AddressFamily IPv4 `
           -InterfaceAlias "Ethernet*","Local Area*" `
           -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -notmatch "^127\." }
    if ($ips) { return $ips[0].IPAddress }
    return "localhost"
}

function Test-PortOpen {
    param([int]$PortNum, [int]$WaitSec = 10)
    $deadline = (Get-Date).AddSeconds($WaitSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $tcp = New-Object Net.Sockets.TcpClient
            $tcp.Connect("127.0.0.1", $PortNum)
            $tcp.Close()
            return $true
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

function Get-ServiceStatus {
    param([string]$Name)
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($svc) { return $svc.Status }
    return "NotFound"
}


# ══════════════════════════════════════════════════════════
# Header
# ══════════════════════════════════════════════════════════
Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Yellow
Write-Host "║   Dashboard Setup — Port $Port      ║" -ForegroundColor Yellow
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Install Dir : $InstallDir"  -ForegroundColor Gray
Write-Host "  Python      : $PythonExe"   -ForegroundColor Gray
Write-Host "  Port        : $Port"         -ForegroundColor Gray
Write-Host "  Bind        : $BindAddress"  -ForegroundColor Gray


# ══════════════════════════════════════════════════════════
# Step 1: Pre-checks
# ══════════════════════════════════════════════════════════
Write-Step "Step 1/6: Pre-checks"

# ตรวจ Python
if (-not (Test-Path $PythonExe)) {
    Write-Fail "ไม่พบ Python: $PythonExe"
    exit 1
}
Write-OK "Python: $PythonExe"

# ตรวจ NSSM
if (-not (Test-Path $NSSMExe)) {
    Write-Fail "ไม่พบ NSSM: $NSSMExe"
    Write-Info "รัน install.ps1 ก่อน"
    exit 1
}
Write-OK "NSSM: $NSSMExe"

# ตรวจ dashboard/app.py
$appScript = "$InstallDir\dashboard\app.py"
if (-not (Test-Path $appScript)) {
    Write-Fail "ไม่พบ: $appScript"
    exit 1
}
Write-OK "Dashboard script: $appScript"

# ตรวจ Streamlit ติดตั้งแล้ว
$stCheck = & $PythonExe -c "import streamlit; print(streamlit.__version__)" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-OK "Streamlit version: $stCheck"
} else {
    Write-Warn "Streamlit ไม่พบ — กำลังติดตั้ง..."
    & $PythonExe -m pip install streamlit --quiet
    Write-OK "Streamlit installed"
}

# ตรวจว่า port ถูกใช้อยู่ไหม
$portUsed = Get-NetTCPConnection -LocalPort $Port `
            -State Listen `
            -ErrorAction SilentlyContinue
if ($portUsed) {
    $pid   = $portUsed[0].OwningProcess
    $pname = (Get-Process -Id $pid -ErrorAction SilentlyContinue).ProcessName
    Write-Warn "Port $Port ถูกใช้อยู่โดย: $pname (PID=$pid)"
    Write-Warn "จะ restart service เพื่อเข้าครอบครอง port"
    $Restart = $true
}

# สร้าง log dir
New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null
Write-OK "Log dir: $LOG_DIR"


# ══════════════════════════════════════════════════════════
# Step 2: Firewall Rules
# ══════════════════════════════════════════════════════════
Write-Step "Step 2/6: Firewall Configuration"

$ruleName = "Streamlit Dashboard Port $Port"

# ลบ rule เก่าถ้ามี
$existing = Get-NetFirewallRule -DisplayName $ruleName `
            -ErrorAction SilentlyContinue
if ($existing) {
    Remove-NetFirewallRule -DisplayName $ruleName
    Write-Info "Removed old firewall rule"
}

if ($LocalOnly) {
    # Local only — ไม่เปิด inbound จาก internet
    Write-Info "LocalOnly mode: ไม่เปิด firewall inbound"
    Write-OK "Dashboard จะเข้าได้จาก localhost เท่านั้น"
} else {
    # เปิด inbound port 8501
    New-NetFirewallRule `
        -DisplayName  $ruleName `
        -Direction    Inbound `
        -Protocol     TCP `
        -LocalPort    $Port `
        -Action       Allow `
        -Profile      Any `
        -Description  "AURUM Bot Streamlit Dashboard" | Out-Null

    Write-OK "Firewall: port $Port opened (inbound)"

    # ตรวจ Windows Defender Firewall state
    $fwProfiles = Get-NetFirewallProfile
    foreach ($profile in $fwProfiles) {
        if ($profile.Enabled -eq $false) {
            Write-Warn "Firewall profile '$($profile.Name)' disabled"
        }
    }
}


# ══════════════════════════════════════════════════════════
# Step 3: NSSM Service Configuration
# ══════════════════════════════════════════════════════════
Write-Step "Step 3/6: NSSM Service Setup"

$svcStatus = Get-ServiceStatus -Name $SERVICE_NAME

if ($svcStatus -ne "NotFound" -and -not $Restart) {
    Write-OK "Service '$SERVICE_NAME' มีอยู่แล้ว (status=$svcStatus)"
    Write-Info "ใช้ -Restart เพื่อ recreate service"
} else {
    # หยุดและลบ service เก่า
    if ($svcStatus -ne "NotFound") {
        Write-Info "Removing existing service..."
        if ($svcStatus -eq "Running") {
            & $NSSMExe stop $SERVICE_NAME | Out-Null
            Start-Sleep -Seconds 3
        }
        & $NSSMExe remove $SERVICE_NAME confirm | Out-Null
        Start-Sleep -Seconds 2
        Write-OK "Removed old service"
    }

    # กำหนด bind address
    $bindAddr = if ($LocalOnly) { "localhost" } else { $BindAddress }

    # Streamlit arguments
    $stArgs = (
        "-m streamlit run dashboard\app.py " +
        "--server.port $Port " +
        "--server.address $bindAddr " +
        "--server.headless true " +
        "--server.enableCORS false " +
        "--server.enableXsrfProtection false " +
        "--browser.gatherUsageStats false " +
        "--logger.level info"
    )

    Write-Info "Creating NSSM service: $SERVICE_NAME"

    # Install
    & $NSSMExe install $SERVICE_NAME $PythonExe $stArgs

    # Working directory
    & $NSSMExe set $SERVICE_NAME AppDirectory $InstallDir

    # Display info
    & $NSSMExe set $SERVICE_NAME DisplayName  "AURUM Trading Dashboard"
    & $NSSMExe set $SERVICE_NAME Description  "Streamlit Dashboard — Port $Port"

    # Auto-start
    & $NSSMExe set $SERVICE_NAME Start SERVICE_AUTO_START

    # Run as SYSTEM
    & $NSSMExe set $SERVICE_NAME ObjectName LocalSystem

    # Logging
    & $NSSMExe set $SERVICE_NAME AppStdout      $STDOUT_LOG
    & $NSSMExe set $SERVICE_NAME AppStderr       $STDERR_LOG
    & $NSSMExe set $SERVICE_NAME AppRotateFiles  1
    & $NSSMExe set $SERVICE_NAME AppRotateBytes  10485760   # 10MB
    & $NSSMExe set $SERVICE_NAME AppRotateOnline 1

    # Restart policy
    & $NSSMExe set $SERVICE_NAME AppExit      Default Restart
    & $NSSMExe set $SERVICE_NAME AppRestartDelay 10000   # 10s

    # Environment vars จาก .env
    $envFile = "$InstallDir\.env"
    if (Test-Path $envFile) {
        $envVars = @()
        Get-Content $envFile | ForEach-Object {
            if ($_ -match '^\s*([^#][^=]+)=(.+)$') {
                $envVars += "$($Matches[1].Trim())=$($Matches[2].Trim())"
            }
        }
        if ($envVars.Count -gt 0) {
            & $NSSMExe set $SERVICE_NAME AppEnvironmentExtra ($envVars -join "`n")
            Write-Info "  Loaded $($envVars.Count) env vars"
        }
    }

    Write-OK "Service configured: $SERVICE_NAME"
    Write-Info "  App     : $PythonExe"
    Write-Info "  Args    : $stArgs"
    Write-Info "  WorkDir : $InstallDir"
    Write-Info "  Log     : $STDOUT_LOG"
}


# ══════════════════════════════════════════════════════════
# Step 4: Start Service
# ══════════════════════════════════════════════════════════
Write-Step "Step 4/6: Starting Service"

$currentStatus = Get-ServiceStatus -Name $SERVICE_NAME

if ($currentStatus -eq "Running") {
    if ($Restart) {
        Write-Info "Restarting service..."
        & $NSSMExe restart $SERVICE_NAME | Out-Null
        Start-Sleep -Seconds 5
        Write-OK "Service restarted"
    } else {
        Write-OK "Service already running"
    }
} else {
    Write-Info "Starting $SERVICE_NAME..."
    & $NSSMExe start $SERVICE_NAME | Out-Null
    Start-Sleep -Seconds 5

    $newStatus = Get-ServiceStatus -Name $SERVICE_NAME
    if ($newStatus -eq "Running") {
        Write-OK "Service started"
    } else {
        Write-Fail "Service failed to start (status=$newStatus)"
        Write-Info "Check stderr: Get-Content '$STDERR_LOG' -Tail 20"
        exit 1
    }
}


# ══════════════════════════════════════════════════════════
# Step 5: Test Connection
# ══════════════════════════════════════════════════════════
Write-Step "Step 5/6: Testing Connection"

Write-Info "Waiting for Streamlit to start (up to 20s)..."

if (Test-PortOpen -PortNum $Port -WaitSec 20) {
    Write-OK "Port $Port is responding"

    # ทดสอบ HTTP response
    try {
        $resp = Invoke-WebRequest `
            -Uri        "http://localhost:$Port" `
            -TimeoutSec 10 `
            -UseBasicParsing
        Write-OK "HTTP response: $($resp.StatusCode)"
    } catch {
        Write-Warn "Port open แต่ HTTP ยังไม่พร้อม (อาจต้องรออีก 5-10s)"
    }
} else {
    Write-Fail "Port $Port ไม่ตอบสนองใน 20 วินาที"
    Write-Info "ดู stderr log:"
    if (Test-Path $STDERR_LOG) {
        Get-Content $STDERR_LOG -Tail 10 |
            ForEach-Object { Write-Host "  $_" -ForegroundColor DarkRed }
    }

    Write-Info ""
    Write-Info "ลองรันด้วยมือเพื่อดู error:"
    Write-Info "  cd $InstallDir"
    Write-Info "  $PythonExe -m streamlit run dashboard\app.py --server.port $Port"
    exit 1
}


# ══════════════════════════════════════════════════════════
# Step 6: Display URLs
# ══════════════════════════════════════════════════════════
Write-Step "Step 6/6: Access URLs"

$localIP  = Get-LocalIP
$publicIP = Get-PublicIP

Write-Host ""
Write-Host "  ┌─────────────────────────────────────┐" -ForegroundColor Green
Write-Host "  │   Dashboard URLs                    │" -ForegroundColor Green
Write-Host "  ├─────────────────────────────────────┤" -ForegroundColor Green
Write-Host "  │ Local:   http://localhost:$Port    │" -ForegroundColor White
if ($localIP) {
Write-Host "  │ LAN:     http://${localIP}:$Port   │" -ForegroundColor White
}
if ($publicIP -and -not $LocalOnly) {
Write-Host "  │ Public:  http://${publicIP}:$Port  │" -ForegroundColor Cyan
}
Write-Host "  └─────────────────────────────────────┘" -ForegroundColor Green
Write-Host ""


# ══════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════
Write-Host "  Service Status:" -ForegroundColor Yellow
Write-Host "  $(& $NSSMExe status $SERVICE_NAME)" -ForegroundColor White
Write-Host ""
Write-Host "  คำสั่งที่ใช้บ่อย:" -ForegroundColor Yellow
Write-Host ""
Write-Host "  # ดูสถานะ" -ForegroundColor DarkGray
Write-Host "  nssm status $SERVICE_NAME" -ForegroundColor White
Write-Host ""
Write-Host "  # ดู log" -ForegroundColor DarkGray
Write-Host "  Get-Content '$STDOUT_LOG' -Wait -Tail 30" -ForegroundColor White
Write-Host ""
Write-Host "  # Restart" -ForegroundColor DarkGray
Write-Host "  nssm restart $SERVICE_NAME" -ForegroundColor White
Write-Host ""
Write-Host "  # หยุด" -ForegroundColor DarkGray
Write-Host "  nssm stop $SERVICE_NAME" -ForegroundColor White
>>>>>>> 98ac82b18ee8d71be376450f278ba77dde0c1c3e
Write-Host ""