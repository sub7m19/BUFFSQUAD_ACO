<#
  BUFFSQUAD ACO: one-time setup on a Windows Server VPS.

  Run in PowerShell as Administrator:
    Set-ExecutionPolicy -Scope Process Bypass -Force
    irm https://raw.githubusercontent.com/sub7m19/BUFFSQUAD_ACO/main/deploy/windows/setup.ps1 -OutFile $env:TEMP\buffsquad-setup.ps1
    & $env:TEMP\buffsquad-setup.ps1 -TunnelToken "<token from Cloudflare>"

  What it does (safe to run again; it skips what's already done):
    - Installs Python 3.13 and Git if they're missing
    - Puts the code in C:\buffsquad\app and its Python packages in C:\buffsquad\venv
    - Creates C:\buffsquad\buffsquad.env (settings + a new encryption key) and
      C:\buffsquad\data (database, backups), readable only by Administrators and SYSTEM
    - Adds three scheduled tasks, all running as SYSTEM:
        BUFFSQUAD App     starts the website + bot at boot
        BUFFSQUAD Deploy  every minute: pulls new code from GitHub, tests it, restarts
        BUFFSQUAD Backup  nightly database backup at 4:00 AM
    - Installs Cloudflare's tunnel as a Windows service when -TunnelToken is given

  The website only listens on 127.0.0.1:8000. Visitors reach it through the
  Cloudflare tunnel, so no ports need to be opened in the firewall.
#>
param(
  [string]$Domain = "app.buffsquad.net",
  [string]$TunnelToken = "",
  [string]$Repo = "https://github.com/sub7m19/BUFFSQUAD_ACO.git"
)

$ErrorActionPreference = "Stop"
$Root = "C:\buffsquad"
$App = "$Root\app"
$Venv = "$Root\venv"
$EnvFile = "$Root\buffsquad.env"
$PythonExe = "C:\Program Files\Python313\python.exe"
$GitExe = "C:\Program Files\Git\cmd\git.exe"
$PythonVersion = "3.13.17"

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Done($text) { Write-Host "    $text" -ForegroundColor Green }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run PowerShell as Administrator (right-click > Run as administrator), then run this again." }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"  # makes downloads much faster in Windows PowerShell
$hasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)

# 1. Python
Step "Python $PythonVersion"
if (Test-Path $PythonExe) {
  Done "Already installed"
} else {
  $installer = "$env:TEMP\python-$PythonVersion-amd64.exe"
  Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" -OutFile $installer
  Start-Process $installer -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
  if (-not (Test-Path $PythonExe)) { throw "Python didn't install. Run $installer by hand, tick 'Install for all users', then run this script again." }
  Done "Installed"
}

# 2. Git
Step "Git"
if (Test-Path $GitExe) {
  Done "Already installed"
} else {
  if ($hasWinget) {
    winget install --id Git.Git --exact --silent --accept-source-agreements --accept-package-agreements | Out-Null
  }
  if (-not (Test-Path $GitExe)) {
    $release = Invoke-RestMethod "https://api.github.com/repos/git-for-windows/git/releases/latest" -Headers @{ "User-Agent" = "buffsquad-setup" }
    $asset = $release.assets | Where-Object { $_.name -match '^Git-[\d.]+-64-bit\.exe$' } | Select-Object -First 1
    $installer = "$env:TEMP\$($asset.name)"
    Invoke-WebRequest $asset.browser_download_url -OutFile $installer
    Start-Process $installer -ArgumentList "/VERYSILENT /NORESTART /NOCANCEL /SP-" -Wait
  }
  if (-not (Test-Path $GitExe)) { throw "Git didn't install. Install it from https://git-scm.com/download/win, then run this script again." }
  Done "Installed"
}
$env:Path = "C:\Program Files\Git\cmd;C:\Program Files\Python313;" + $env:Path
# The scheduled tasks run git as SYSTEM, which Git otherwise refuses in a folder an admin created.
if (-not (& $GitExe config --system --get-all safe.directory | Where-Object { $_ -eq "C:/buffsquad/app" })) {
  & $GitExe config --system --add safe.directory "C:/buffsquad/app"
}

# 3. Code
Step "Code in $App"
New-Item -ItemType Directory -Force -Path $Root, "$Root\data", "$Root\data\backups", "$Root\logs", "$Root\bin" | Out-Null
if (Test-Path "$App\.git") {
  & $GitExe -C $App fetch --quiet origin main
  & $GitExe -C $App reset --hard --quiet origin/main
  Done "Updated to the latest version"
} else {
  & $GitExe clone --quiet --branch main $Repo $App
  if ($LASTEXITCODE -ne 0) { throw "Couldn't download the code from $Repo" }
  Done "Downloaded"
}

# 4. Python packages
Step "Python packages"
if (-not (Test-Path "$Venv\Scripts\python.exe")) { & $PythonExe -m venv $Venv }
& "$Venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade pip
& "$Venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r "$App\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "Installing Python packages failed. Check the internet connection and run this again." }
Done "Installed"

# 5. Settings file and private folders
Step "Settings file $EnvFile"
if (Test-Path $EnvFile) {
  Done "Already exists, left as it is"
} else {
  $key = & "$Venv\Scripts\python.exe" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  @"
# BUFFSQUAD ACO settings. Restart after editing: C:\buffsquad\bin\restart.cmd
HOST=127.0.0.1
PORT=8000
PUBLIC_URL=https://$Domain
DB_PATH=$Root\data\buffsquad.db
BACKUP_DIR=$Root\data\backups
LOG_DIR=$Root\logs

# Encrypts saved cards, passwords and inbox keys. BACK THIS UP somewhere safe
# (a password manager). Without it the saved secrets can't be read.
BUFFSQUAD_SECRET_KEY=$key

# Discord app: https://discord.com/developers/applications
DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=
DISCORD_BOT_TOKEN=
DISCORD_GUILD_ID=
ADMIN_DISCORD_IDS=
OWNER_DISCORD_IDS=
CHECKOUT_CHANNEL_IDS=

DEV_LOGIN=0
"@ | Set-Content -Path $EnvFile -Encoding ASCII
  Done "Created with a new encryption key"
}
# Only Administrators and SYSTEM can read the settings and the database.
icacls $EnvFile /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" | Out-Null
icacls "$Root\data" /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
Done "Locked to Administrators and SYSTEM"

# 6. Start scripts and scheduled tasks
Step "Scheduled tasks"
@"
@echo off
set BUFFSQUAD_ENV=$EnvFile
cd /d $App
"$Venv\Scripts\python.exe" start.py
"@ | Set-Content "$Root\bin\run-app.cmd" -Encoding ASCII
@"
@echo off
set BUFFSQUAD_ENV=$EnvFile
cd /d $App
"$Venv\Scripts\python.exe" backup.py >> "$Root\logs\backup.log" 2>&1
"@ | Set-Content "$Root\bin\run-backup.cmd" -Encoding ASCII
@"
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "$App\deploy\windows\deploy.ps1" -Restart
"@ | Set-Content "$Root\bin\restart.cmd" -Encoding ASCII

$system = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$forever = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$short = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName "BUFFSQUAD App" -Force -Principal $system -Settings $forever `
  -Trigger (New-ScheduledTaskTrigger -AtStartup) `
  -Action (New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Root\bin\run-app.cmd`"") `
  -Description "BUFFSQUAD website and Discord bot" | Out-Null
Register-ScheduledTask -TaskName "BUFFSQUAD Deploy" -Force -Principal $system -Settings $short `
  -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)) `
  -Action (New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$App\deploy\windows\deploy.ps1`"") `
  -Description "Every minute: deploy new code from GitHub, and restart the app if it stopped" | Out-Null
Register-ScheduledTask -TaskName "BUFFSQUAD Backup" -Force -Principal $system -Settings $short `
  -Trigger (New-ScheduledTaskTrigger -Daily -At "4:00 AM") `
  -Action (New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Root\bin\run-backup.cmd`"") `
  -Description "Nightly BUFFSQUAD database backup (keeps 14)" | Out-Null
Done "BUFFSQUAD App, BUFFSQUAD Deploy, BUFFSQUAD Backup"

# 7. Cloudflare tunnel
Step "Cloudflare tunnel"
if (Get-Service cloudflared -ErrorAction SilentlyContinue) {
  Done "Already installed as a Windows service"
} elseif ($TunnelToken) {
  $msi = "$env:TEMP\cloudflared-windows-amd64.msi"
  Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.msi" -OutFile $msi
  Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /quiet /norestart" -Wait
  $cf = @("C:\Program Files (x86)\cloudflared\cloudflared.exe", "C:\Program Files\cloudflared\cloudflared.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $cf) { throw "cloudflared didn't install. Download it from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" }
  & $cf service install $TunnelToken
  Done "Installed and connected"
} else {
  Write-Host "    Skipped: run this script again with -TunnelToken `"<token>`" once you've created the tunnel in Cloudflare." -ForegroundColor Yellow
}

# 8. Start
Step "Starting BUFFSQUAD"
Start-ScheduledTask -TaskName "BUFFSQUAD App"
$up = $false
for ($i = 0; $i -lt 30 -and -not $up; $i++) {
  Start-Sleep -Seconds 2
  try { $up = (Invoke-RestMethod "http://127.0.0.1:8000/api/health" -TimeoutSec 3).ok } catch { }
}
if ($up) { Done "Running on this server at http://127.0.0.1:8000" } else { Write-Host "    Didn't answer yet. Check $Root\logs\app.log" -ForegroundColor Yellow }

Write-Host ""
Write-Host "Setup finished." -ForegroundColor Green
Write-Host "Next: fill in the Discord values in $EnvFile (notepad $EnvFile), then run $Root\bin\restart.cmd"
Write-Host "Back up the BUFFSQUAD_SECRET_KEY line from that file to a password manager."
