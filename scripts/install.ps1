# scripts/install.ps1
# ════════════════════════════════════════════════════════════
# Trading Bot — First-Time Installation Script
# รันบน Windows VPS ครั้งแรกครั้งเดียว
#
# สิ่งที่ติดตั้ง:
#   1. Chocolatey (package manager)
#   2. Python 3.11
#   3. Git
#   4. Visual C++ Redistributable (สำหรับ MT5)
#   5. MetaTrader 5
#   6. NSSM (Windows Service Manager)
#   7. Clone โปรเจกต์จาก GitHub
#   8. ติดตั้ง Python packages
#   9. สร้างโฟลเดอร์และไฟล์ที่จำเป็น
#  10. ตั้งค่า Firewall
# ════════════════════════════════════════════════════════════
#
# วิธีรัน:
#   1. เปิด PowerShell ในฐานะ Administrator
#   2. Set-ExecutionPolicy Bypass -Scope Process -Force
#   3. .\scripts\install.ps1
# ════════════════════════════════════════════════════════════

#Requires -RunAsAdministrator

param(
    [string]$GitRepo    = "https://github.com/YOUR_USERNAME/trading-bot.git",
    [string]$InstallDir = "C:\trading-bot",
    [string]$PythonVer  = "3.11.9",
    [switch]$SkipMT5    = $false,    # ข้าม MT5 ถ้าติดตั้งแล้ว
    [switch]$SkipGit    = $false,    # ข้าม git clone ถ้ามีแล้ว
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"


# ══════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════
function Write-Step {
    param([string]$Message, [string]$Color = "Cyan")
    Write-Host ""
    Write-Host "═══════════════════════════════════════" -ForegroundColor DarkGray
    Write-Host "  $Message" -ForegroundColor $Color
    Write-Host "═══════════════════════════════════════" -ForegroundColor DarkGray
}

function Write-OK   { param([string]$m) Write-Host "  ✅ $m" -ForegroundColor Green  }
function Write-Warn { param([string]$m) Write-Host "  ⚠️  $m" -ForegroundColor Yellow }
function Write-Fail { param([string]$m) Write-Host "  ❌ $m" -ForegroundColor Red    }
function Write-Info { param([string]$m) Write-Host "     $m" -ForegroundColor Gray   }

function Test-CommandExists {
    param([string]$Command)
    return [bool](Get-Command $Command -ErrorAction SilentlyContinue)
}

function Invoke-WithRetry {
    param(
        [scriptblock]$ScriptBlock,
        [int]$MaxRetries = 3,
        [int]$DelaySeconds = 5,
        [string]$Name = "operation"
    )
    for ($i = 1; $i -le $MaxRetries; $i++) {
        try {
            & $ScriptBlock
            return
        } catch {
            if ($i -eq $MaxRetries) { throw }
            Write-Warn "$Name ล้มเหลว (attempt $i/$MaxRetries) รอ ${DelaySeconds}s..."
            Start-Sleep -Seconds $DelaySeconds
        }
    }
}

function Get-FileFromWeb {
    param([string]$Url, [string]$OutFile)
    Write-Info "Downloading: $([System.IO.Path]::GetFileName($OutFile))"
    Invoke-WithRetry -Name "download" -ScriptBlock {
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    }
}


# ══════════════════════════════════════════════════════════
# Start
# ══════════════════════════════════════════════════════════
$StartTime = Get-Date

Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Yellow
Write-Host "║   AURUM BOT — Installation Script   ║" -ForegroundColor Yellow
Write-Host "║   Windows VPS Setup v1.0             ║" -ForegroundColor Yellow
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Install Dir : $InstallDir"  -ForegroundColor Gray
Write-Host "  Python Ver  : $PythonVer"   -ForegroundColor Gray
Write-Host "  Git Repo    : $GitRepo"     -ForegroundColor Gray
Write-Host ""

# ตรวจว่า Admin
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal   = New-Object Security.Principal.WindowsPrincipal($currentUser)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Fail "ต้องรันในฐานะ Administrator"
    exit 1
}

# Temp directory
$TempDir = "$env:TEMP\aurum_install"
New-Item -ItemType Directory -Path $TempDir -Force | Out-Null


# ══════════════════════════════════════════════════════════
# Step 1: Chocolatey
# ══════════════════════════════════════════════════════════
Write-Step "Step 1/10: Installing Chocolatey"

if (Test-CommandExists "choco") {
    Write-OK "Chocolatey มีอยู่แล้ว: $(choco --version)"
} else {
    Write-Info "Installing Chocolatey..."
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = `
        [System.Net.ServicePointManager]::SecurityProtocol `
        -bor 3072
    Invoke-Expression (
        (New-Object System.Net.WebClient).DownloadString(
            'https://community.chocolatey.org/install.ps1'
        )
    )
    # Reload PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable(
        "Path","Machine"
    ) + ";" + [System.Environment]::GetEnvironmentVariable(
        "Path","User"
    )
    Write-OK "Chocolatey installed"
}


# ══════════════════════════════════════════════════════════
# Step 2: Python 3.11
# ══════════════════════════════════════════════════════════
Write-Step "Step 2/10: Installing Python $PythonVer"

$PythonExe = "C:\Python311\python.exe"

if (Test-Path $PythonExe) {
    $pyVer = & $PythonExe --version 2>&1
    Write-OK "Python มีอยู่แล้ว: $pyVer"
} else {
    # ดาวน์โหลดจาก python.org โดยตรง (stable กว่า choco)
    $pyUrl  = "https://www.python.org/ftp/python/$PythonVer/python-$PythonVer-amd64.exe"
    $pyInst = "$TempDir\python-$PythonVer-amd64.exe"
    Get-FileFromWeb -Url $pyUrl -OutFile $pyInst

    Write-Info "Installing Python (silent)..."
    $process = Start-Process -FilePath $pyInst -Wait -PassThru -ArgumentList @(
        "/quiet",
        "InstallAllUsers=1",
        "PrependPath=1",
        "Include_pip=1",
        "Include_launcher=1",
        "TargetDir=C:\Python311"
    )

    if ($process.ExitCode -ne 0) {
        Write-Fail "Python installation failed (exit code: $($process.ExitCode))"
        exit 1
    }

    # Reload PATH
    $env:Path = "C:\Python311;C:\Python311\Scripts;" + $env:Path
    Write-OK "Python $PythonVer installed → C:\Python311"
}

# Upgrade pip
Write-Info "Upgrading pip..."
& $PythonExe -m pip install --upgrade pip --quiet
Write-OK "pip upgraded"


# ══════════════════════════════════════════════════════════
# Step 3: Git
# ══════════════════════════════════════════════════════════
Write-Step "Step 3/10: Installing Git"

if (Test-CommandExists "git") {
    Write-OK "Git มีอยู่แล้ว: $(git --version)"
} else {
    Write-Info "Installing Git via Chocolatey..."
    Invoke-WithRetry -Name "git install" -ScriptBlock {
        choco install git -y --no-progress
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") `
                + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    Write-OK "Git installed: $(git --version)"
}

# ตั้งค่า Git
git config --global core.autocrlf false
git config --global core.longpaths true
Write-OK "Git configured"


# ══════════════════════════════════════════════════════════
# Step 4: Visual C++ Redistributable
# ══════════════════════════════════════════════════════════
Write-Step "Step 4/10: Visual C++ Redistributable"

# MT5 ต้องการ VC++ 2015-2022
$vcRedistUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
$vcRedistExe = "$TempDir\vc_redist.x64.exe"

Write-Info "Downloading VC++ Redistributable..."
Get-FileFromWeb -Url $vcRedistUrl -OutFile $vcRedistExe

Write-Info "Installing VC++ Redistributable (silent)..."
$vc = Start-Process -FilePath $vcRedistExe -Wait -PassThru `
      -ArgumentList "/install", "/quiet", "/norestart"

if ($vc.ExitCode -in @(0, 1638, 3010)) {
    Write-OK "VC++ Redistributable installed"
} else {
    Write-Warn "VC++ exit code: $($vc.ExitCode) (อาจมีอยู่แล้ว)"
}


# ══════════════════════════════════════════════════════════
# Step 5: MetaTrader 5
# ══════════════════════════════════════════════════════════
Write-Step "Step 5/10: Installing MetaTrader 5"

$MT5Path = "C:\Program Files\MetaTrader 5\terminal64.exe"

if ($SkipMT5) {
    Write-Info "Skipping MT5 (--SkipMT5 flag)"
} elseif (Test-Path $MT5Path) {
    Write-OK "MT5 มีอยู่แล้ว: $MT5Path"
} else {
    $mt5Url = "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
    $mt5Exe = "$TempDir\mt5setup.exe"
    Get-FileFromWeb -Url $mt5Url -OutFile $mt5Exe

    Write-Info "Installing MT5..."
    $mt5 = Start-Process -FilePath $mt5Exe -Wait -PassThru `
           -ArgumentList "/auto"

    if (Test-Path $MT5Path) {
        Write-OK "MT5 installed: $MT5Path"
    } else {
        Write-Warn "MT5 installer อาจต้องการ interaction — รันด้วยมือ"
        Write-Warn "ดาวน์โหลดได้ที่: https://www.exness.com/th/mt5/"
    }
}

# ติดตั้ง MT5 Python package
Write-Info "Installing MetaTrader5 Python package..."
& $PythonExe -m pip install MetaTrader5 --quiet
Write-OK "MetaTrader5 package installed"


# ══════════════════════════════════════════════════════════
# Step 6: NSSM
# ══════════════════════════════════════════════════════════
Write-Step "Step 6/10: Installing NSSM"

$NSSMPath = "C:\Windows\System32\nssm.exe"

if (Test-Path $NSSMPath) {
    Write-OK "NSSM มีอยู่แล้ว"
} else {
    $nssmUrl = "https://nssm.cc/release/nssm-2.24.zip"
    $nssmZip = "$TempDir\nssm.zip"
    $nssmDir = "$TempDir\nssm"

    Get-FileFromWeb -Url $nssmUrl -OutFile $nssmZip

    Write-Info "Extracting NSSM..."
    Expand-Archive -Path $nssmZip -DestinationPath $nssmDir -Force
    Copy-Item "$nssmDir\nssm-2.24\win64\nssm.exe" $NSSMPath -Force
    Write-OK "NSSM installed → $NSSMPath"
}


# ══════════════════════════════════════════════════════════
# Step 7: Clone Project
# ══════════════════════════════════════════════════════════
Write-Step "Step 7/10: Cloning Project"

if ($SkipGit) {
    Write-Info "Skipping git clone (--SkipGit flag)"
} elseif (Test-Path "$InstallDir\.git") {
    Write-OK "Project มีอยู่แล้ว — กำลัง pull..."
    Push-Location $InstallDir
    git pull origin main
    Pop-Location
} else {
    Write-Info "Cloning from: $GitRepo"
    Invoke-WithRetry -Name "git clone" -ScriptBlock {
        git clone $GitRepo $InstallDir
    }
    Write-OK "Project cloned → $InstallDir"
}


# ══════════════════════════════════════════════════════════
# Step 8: Python Packages
# ══════════════════════════════════════════════════════════
Write-Step "Step 8/10: Installing Python Packages"

Push-Location $InstallDir

$ReqFile = "$InstallDir\requirements.txt"
if (-not (Test-Path $ReqFile)) {
    Write-Fail "ไม่พบ requirements.txt ใน $InstallDir"
    exit 1
}

Write-Info "Installing packages (อาจใช้เวลา 5-10 นาที)..."

Invoke-WithRetry -Name "pip install" -ScriptBlock {
    & $PythonExe -m pip install `
        -r $ReqFile `
        --no-warn-script-location `
        --quiet
}

Write-OK "Python packages installed"

# ตรวจ packages สำคัญ
$criticalPkgs = @(
    "MetaTrader5",
    "pandas",
    "xgboost",
    "lightgbm",
    "streamlit",
    "python-telegram-bot",
    "vectorbt",
    "schedule",
)

Write-Info "Verifying critical packages..."
foreach ($pkg in $criticalPkgs) {
    $result = & $PythonExe -c "import $($pkg.Replace('-','_').Replace('.','_').ToLower()); print('ok')" 2>&1
    if ($result -eq "ok") {
        Write-OK "  $pkg"
    } else {
        Write-Warn "  $pkg — ติดตั้งไม่สำเร็จ (จะติดตั้งใหม่)"
        & $PythonExe -m pip install $pkg --quiet
    }
}

Pop-Location


# ══════════════════════════════════════════════════════════
# Step 9: Create Folders & Files
# ══════════════════════════════════════════════════════════
Write-Step "Step 9/10: Creating Folders & Files"

$folders = @(
    "$InstallDir\data\raw",
    "$InstallDir\data\processed",
    "$InstallDir\models\saved",
    "$InstallDir\models\strategies",
    "$InstallDir\models\backup",
    "$InstallDir\logs\daily",
    "$InstallDir\db",
    "$InstallDir\reports",
    "$InstallDir\flags",
)

foreach ($folder in $folders) {
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    # สร้าง .gitkeep
    $keepFile = "$folder\.gitkeep"
    if (-not (Test-Path $keepFile)) {
        New-Item -ItemType File -Path $keepFile -Force | Out-Null
    }
}
Write-OK "Folders created"

# สร้าง .env template ถ้ายังไม่มี
$envPath = "$InstallDir\.env"
if (-not (Test-Path $envPath)) {
    @"
# MT5 Exness Credentials
MT5_LOGIN=your_login_number
MT5_PASSWORD=your_password_here
MT5_SERVER=Exness-MT5Real8
MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe

# Telegram Bot
TELEGRAM_TOKEN=your_bot_token_from_botfather
TELEGRAM_CHAT_ID=your_chat_id
"@ | Out-File -FilePath $envPath -Encoding UTF8

    Write-OK ".env template created → $envPath"
    Write-Warn "แก้ .env ด้วย credentials จริงก่อนรัน bot!"
} else {
    Write-OK ".env มีอยู่แล้ว"
}


# ══════════════════════════════════════════════════════════
# Step 10: Firewall Rules
# ══════════════════════════════════════════════════════════
Write-Step "Step 10/10: Configuring Firewall"

# Streamlit Dashboard (port 8501)
$rule1 = Get-NetFirewallRule -DisplayName "Streamlit Dashboard" `
         -ErrorAction SilentlyContinue
if ($rule1) {
    Write-OK "Firewall rule ของ Streamlit มีอยู่แล้ว"
} else {
    New-NetFirewallRule `
        -DisplayName  "Streamlit Dashboard" `
        -Direction    Inbound `
        -Protocol     TCP `
        -LocalPort    8501 `
        -Action       Allow `
        -Profile      Any | Out-Null
    Write-OK "Firewall: port 8501 (Streamlit) opened"
}

# MT5 (ใช้ outbound — ไม่ต้องเปิด inbound)
Write-OK "MT5 ใช้ outbound connection — ไม่ต้องเปิด firewall"

# Timezone — ตั้งเป็น UTC
Write-Info "Setting timezone to UTC..."
Set-TimeZone -Id "UTC"
Write-OK "Timezone set to UTC"


# ══════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════
$Duration = [math]::Round(
    ((Get-Date) - $StartTime).TotalMinutes, 1
)

Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║   Installation Complete! 🚀          ║" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  Duration: ${Duration} minutes" -ForegroundColor Gray
Write-Host ""
Write-Host "  ขั้นตอนต่อไป:" -ForegroundColor Yellow
Write-Host ""
Write-Host "  1. แก้ .env ใส่ credentials จริง" -ForegroundColor White
Write-Host "     notepad $InstallDir\.env" -ForegroundColor Cyan
Write-Host ""
Write-Host "  2. Login MT5 กับ Exness" -ForegroundColor White
Write-Host "     เปิด MT5 → File → Open Account → Exness" -ForegroundColor Cyan
Write-Host ""
Write-Host "  3. ดึงข้อมูลและ train โมเดล" -ForegroundColor White
Write-Host "     cd $InstallDir" -ForegroundColor Cyan
Write-Host "     python data\pipeline.py" -ForegroundColor Cyan
Write-Host "     python features\pipeline.py" -ForegroundColor Cyan
Write-Host "     python models\train_xgb.py" -ForegroundColor Cyan
Write-Host "     python models\backtest.py --checklist" -ForegroundColor Cyan
Write-Host ""
Write-Host "  4. ติดตั้ง Windows Services" -ForegroundColor White
Write-Host "     .\scripts\nssm_setup.ps1" -ForegroundColor Cyan
Write-Host ""
Write-Host "  5. ตรวจสอบทุกอย่าง" -ForegroundColor White
Write-Host "     nssm status TradingBot" -ForegroundColor Cyan
Write-Host "     Get-Content logs\bot.log -Tail 20" -ForegroundColor Cyan
Write-Host ""

# Cleanup temp
Remove-Item -Path $TempDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "  Temp files cleaned up" -ForegroundColor DarkGray
Write-Host ""