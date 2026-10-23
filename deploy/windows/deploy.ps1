<#
  BUFFSQUAD ACO: runs every minute as the "BUFFSQUAD Deploy" scheduled task.

  - New commit on GitHub's main branch: pull it, install packages, run the tests.
    Tests pass: restart the app and wait for /api/health. Tests fail, or the app
    doesn't come back: go back to the previous version and skip that commit.
  - No new commit: make sure the app is up (and the bot checking in), and start
    it again if it isn't.

  -Restart restarts the app now (C:\buffsquad\bin\restart.cmd runs this after
  you edit the settings file).

  Log: C:\buffsquad\logs\deploy.log
#>
param([switch]$Restart)

$ErrorActionPreference = "Continue"
$Root = "C:\buffsquad"
$App = "$Root\app"
$Py = "$Root\venv\Scripts\python.exe"
$Git = "C:\Program Files\Git\cmd\git.exe"
$LogFile = "$Root\logs\deploy.log"
$Health = "http://127.0.0.1:8000/api/health"
$SkipFile = "$Root\logs\skip-commit.txt"

function Log($text) {
  "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $text" | Add-Content -Path $LogFile -Encoding UTF8
  # Keep the log from growing forever.
  if ((Get-Item $LogFile).Length -gt 2MB) { Move-Item $LogFile "$LogFile.old" -Force }
}

function Get-Health {
  try { Invoke-RestMethod $Health -TimeoutSec 5 } catch { $null }
}

function Stop-App {
  Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.ExecutablePath -like "$Root\venv\*" -and $_.CommandLine -like "*start.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  # cmd.exe wrapper from the scheduled task
  Stop-ScheduledTask -TaskName "BUFFSQUAD App" -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
}

function Start-App {
  Start-ScheduledTask -TaskName "BUFFSQUAD App"
  for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    if ((Get-Health).ok) { return $true }
  }
  return $false
}

function Restart-App { Stop-App; Start-App }

# One run at a time (a deploy can take longer than a minute).
$lock = "$Root\deploy.lock"
if ((Test-Path $lock) -and (Get-Item $lock).LastWriteTime -gt (Get-Date).AddMinutes(-15)) { exit 0 }
Set-Content -Path $lock -Value $PID
try {
  if ($Restart) {
    Log "Restart requested"
    if (Restart-App) { Log "Restarted" } else { Log "Restarted but not answering. Check $Root\logs\app.log" }
    exit 0
  }

  & $Git -C $App fetch --quiet origin main
  if ($LASTEXITCODE -ne 0) { Log "Couldn't reach GitHub"; }
  $current = (& $Git -C $App rev-parse HEAD).Trim()
  $latest = (& $Git -C $App rev-parse origin/main).Trim()
  $skip = if (Test-Path $SkipFile) { (Get-Content $SkipFile -Raw).Trim() } else { "" }

  if ($latest -and $latest -ne $current -and $latest -ne $skip) {
    Log "Deploying $($latest.Substring(0, 7)) (was $($current.Substring(0, 7)))"
    & $Git -C $App reset --hard --quiet $latest
    & $Py -m pip install --quiet --disable-pip-version-check -r "$App\requirements.txt" *> "$Root\logs\last-pip.log"

    # Tests run with their own throwaway database, never the live one.
    Push-Location $App
    $env:BUFFSQUAD_ENV = ""
    & $Py -m unittest discover -s tests -t . *> "$Root\logs\last-tests.log"
    $testsPassed = ($LASTEXITCODE -eq 0)
    Pop-Location

    if (-not $testsPassed) {
      Log "Tests failed, staying on $($current.Substring(0, 7)). See $Root\logs\last-tests.log"
      & $Git -C $App reset --hard --quiet $current
      Set-Content -Path $SkipFile -Value $latest
    } elseif (Restart-App) {
      Log "Live: $((Get-Health).version)"
    } else {
      Log "New version didn't start, going back to $($current.Substring(0, 7)). See $Root\logs\app.log"
      & $Git -C $App reset --hard --quiet $current
      & $Py -m pip install --quiet --disable-pip-version-check -r "$App\requirements.txt" *> $null
      Set-Content -Path $SkipFile -Value $latest
      if (Restart-App) { Log "Back on $($current.Substring(0, 7))" } else { Log "Previous version didn't start either. Check $Root\logs\app.log" }
    }
    exit 0
  }

  # Watchdog: start the app if it stopped, or if the bot stopped checking in.
  $health = Get-Health
  if (-not $health.ok) {
    Log "Not answering, starting it"
    if (Restart-App) { Log "Started" } else { Log "Didn't start. Check $Root\logs\app.log" }
  } elseif ($health.bot.configured -and $health.bot.lastSeen -and -not $health.bot.error) {
    # (A sign-in error is a settings problem; restarting every minute wouldn't fix it.)
    $quietMinutes = ((Get-Date).ToUniversalTime() - [DateTime]::new(1970, 1, 1, 0, 0, 0, [DateTimeKind]::Utc).AddMilliseconds($health.bot.lastSeen)).TotalMinutes
    if ($quietMinutes -gt 10) {
      Log "Bot quiet for $([int]$quietMinutes) minutes, restarting"
      Restart-App | Out-Null
    }
  }
} finally {
  Remove-Item $lock -ErrorAction SilentlyContinue
}
