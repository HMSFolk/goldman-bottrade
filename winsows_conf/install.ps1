# scripts/install.ps1
# Requires -RunAsAdministrator

param(
    [string]$GitRepo    = "https://github.com/HMSFolk/goldman-bottrade.git",
    [string]$InstallDir = "C:\Goldman-Bot",
    [string]$PythonVer  = "3.13.2",  # ✅ อัปเดตเป็น Python 3.13
    [switch]$SkipMT5    = $false,
    [switch]$SkipGit    = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message, [string]$Color = "Cyan")
    Write-Host "`n═══════════════════════════════════════" -ForegroundColor DarkGray
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
    param([scriptblock]$ScriptBlock, [int]$MaxRetries = 3, [int]$DelaySeconds = 5, [string]$Name = "operation")
    for ($i = 1; $i -le $MaxRetries; $i++) {
        try { & $ScriptBlock; return } 
        catch {
            if ($i -eq $MaxRetries) { throw }
            Write-Warn "$Name ล้มเหลว รอ ${DelaySeconds}s..."
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

$StartTime = Get-Date
$TempDir = "$env:TEMP\aurum_install"
New-Item -ItemType Directory -Path $TempDir -Force | Out-Null

Write-Step "Step 1/10: Installing Chocolatey"
if (Test-CommandExists "choco") { Write-OK "Chocolatey มีอยู่แล้ว" } 
else {
    Write-Info "Installing Chocolatey..."
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    Write-OK "Chocolatey installed"
}

Write-Step "Step 2/10: Installing Python $PythonVer"
$PythonExe = "C:\Python313\python.exe" # ✅ แก้พาธเป็น 313
if (Test-Path $PythonExe) { Write-OK "Python มีอยู่แล้ว" } 
else {
    $pyUrl  = "https://www.python.org/ftp/python/$PythonVer/python-$PythonVer-amd64.exe"
    $pyInst = "$TempDir\python-$PythonVer-amd64.exe"
    Get-FileFromWeb -Url $pyUrl -OutFile $pyInst
    Write-Info "Installing Python (silent)..."
    $process = Start-Process -FilePath $pyInst -Wait -PassThru -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_pip=1", "TargetDir=C:\Python313"
    if ($process.ExitCode -ne 0) { Write-Fail "Python failed"; exit 1 }
    $env:Path = "C:\Python313;C:\Python313\Scripts;" + $env:Path
    Write-OK "Python installed"
}
& $PythonExe -m pip install --upgrade pip --quiet

Write-Step "Step 3/10: Installing Git"
if (Test-CommandExists "git") { Write-OK "Git มีอยู่แล้ว" } 
else {
    Invoke-WithRetry -Name "git" -ScriptBlock { choco install git -y --no-progress }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}
git config --global core.autocrlf false
git config --global core.longpaths true

Write-Step "Step 4/10: VC++ Redistributable"
$vcRedistExe = "$TempDir\vc_redist.x64.exe"
Get-FileFromWeb -Url "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $vcRedistExe
$vc = Start-Process -FilePath $vcRedistExe -Wait -PassThru -ArgumentList "/install", "/quiet", "/norestart"

Write-Step "Step 5/10: MetaTrader 5"
$MT5Path = "C:\Program Files\MetaTrader 5\terminal64.exe"
if (-not $SkipMT5 -and -not (Test-Path $MT5Path)) {
    $mt5Exe = "$TempDir\mt5setup.exe"
    Get-FileFromWeb -Url "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -OutFile $mt5Exe
    Start-Process -FilePath $mt5Exe -Wait -ArgumentList "/auto"
}
& $PythonExe -m pip install MetaTrader5 --quiet

Write-Step "Step 6/10: Installing NSSM"
$NSSMPath = "C:\Windows\System32\nssm.exe"
if (-not (Test-Path $NSSMPath)) {
    $nssmZip = "$TempDir\nssm.zip"
    Get-FileFromWeb -Url "https://nssm.cc/release/nssm-2.24.zip" -OutFile $nssmZip
    Expand-Archive -Path $nssmZip -DestinationPath "$TempDir\nssm" -Force
    Copy-Item "$TempDir\nssm\nssm-2.24\win64\nssm.exe" $NSSMPath -Force
}

Write-Step "Step 7/10: Project Files"
if (-not $SkipGit -and -not (Test-Path "$InstallDir\.git")) {
    git clone $GitRepo $InstallDir
}

Write-Step "Step 8/10: Python Packages"
Push-Location $InstallDir
Invoke-WithRetry -Name "pip" -ScriptBlock { & $PythonExe -m pip install -r requirements.txt --quiet }
Pop-Location

Write-Step "Step 9/10: Folders & Env"
$folders = @("$InstallDir\data\raw", "$InstallDir\data\processed", "$InstallDir\models\saved", "$InstallDir\logs\daily", "$InstallDir\db", "$InstallDir\reports")
foreach ($f in $folders) { New-Item -ItemType Directory -Path $f -Force | Out-Null }
if (-not (Test-Path "$InstallDir\.env")) {
    @"
# MT5 Credentials
MT5_LOGIN=0
MT5_PASSWORD=
MT5_SERVER=
MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe

# Telegram Bot
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=

# Environment
BOT_ENV=production
"@ | Out-File "$InstallDir\.env" -Encoding UTF8
}

Write-Step "Step 10/10: Firewall & Timezone"
New-NetFirewallRule -DisplayName "Streamlit Dashboard" -Direction Inbound -Protocol TCP -LocalPort 8501 -Action Allow -ErrorAction SilentlyContinue | Out-Null
Set-TimeZone -Id "UTC"

Remove-Item -Path $TempDir -Recurse -Force -ErrorAction SilentlyContinue
Write-OK "Installation Complete! ($( [math]::Round(((Get-Date)-$StartTime).TotalMinutes,1) ) min)"