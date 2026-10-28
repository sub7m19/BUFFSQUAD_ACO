<#
  BUFFSQUAD ACO: runs every minute as the "BUFFSQUAD Deploy" scheduled task.

  - New commit on GitHub's main branch: test it first in a separate folder
    (C:\buffsquad\stage), so the live site never shows an untested version.
    Tests pass: switch the live folder to it, restart, and wait for /api/health.
    Tests fail, or the app doesn't come back: stay on (or go back to) the previous
    version and skip that commit until a newer one is pushed.
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
$Stage = "$Root\stage"
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

# After a deploy: load the public splash page through Cloudflare, the way members
# see it, and check it links this version's files, and that Cloudflare doesn't
# tell browsers to keep them (Browser Cache TTL should be "Respect Existing Headers").
function Test-Public($version) {
  $line = Get-Content "$Root\buffsquad.env" -ErrorAction SilentlyContinue | Where-Object { $_ -match '^\s*PUBLIC_URL\s*=' } | Select-Object -First 1
  $url = if ($line) { ($line -split '=', 2)[1].Trim().Trim('"', "'").TrimEnd('/') } else { "" }
  if (-not $url -or $url -like "http://127.0.0.1*" -or -not $version) { return }
  try {
    $page = Invoke-WebRequest "$url/index.html" -UseBasicParsing -TimeoutSec 15 -Headers @{ "Cache-Control" = "no-cache" }
    if ($page.Content -notmatch [regex]::Escape("styles.css?v=$version")) {
      Log "Warning: $url still shows an older version. Check Cloudflare: Caching > Cache Rules shouldn't cache HTML, and Caching Level should be Standard"
      return
    }
    $css = Invoke-WebRequest "$url/styles.css?v=$version" -UseBasicParsing -TimeoutSec 15 -Method Head
    if ($css.Headers["Cache-Control"] -notmatch "no-cache") {
      Log "Warning: Cloudflare sends '$($css.Headers["Cache-Control"])' for site files. Set Caching > Configuration > Browser Cache TTL to 'Respect Existing Headers'"
      return
    }
    Log "Checked $url`: members get $version"
  } catch {
    Log "Couldn't check $url after deploying: $($_.Exception.Message)"
  }
}

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
    Log "Testing $($latest.Substring(0, 7)) (live: $($current.Substring(0, 7)))"

    # A second checkout of the same repository, used only for testing.
    if (-not (Test-Path "$Stage\.git")) {
      if (-not (& $Git config --system --get-all safe.directory | Where-Object { $_ -eq "C:/buffsquad/stage" })) {
        & $Git config --system --add safe.directory "C:/buffsquad/stage"
      }
      & $Git -C $App worktree prune
      & $Git -C $App worktree add --detach --quiet $Stage $latest
    }
    & $Git -C $Stage checkout --detach --quiet --force $latest
    & $Git -C $Stage clean -fdq
    & $Py -m pip install --quiet --disable-pip-version-check -r "$Stage\requirements.txt" *> "$Root\logs\last-pip.log"

    # Tests run with their own throwaway database, never the live one.
    Push-Location $Stage
    $env:BUFFSQUAD_ENV = ""
    & $Py -m unittest discover -s tests -t . *> "$Root\logs\last-tests.log"
    $testsPassed = ($LASTEXITCODE -eq 0)
    Pop-Location

    if (-not $testsPassed) {
      Log "Tests failed, live site stays on $($current.Substring(0, 7)). See $Root\logs\last-tests.log"
      Set-Content -Path $SkipFile -Value $latest
      exit 0
    }

    Log "Tests passed, deploying $($latest.Substring(0, 7))"
    & $Git -C $App reset --hard --quiet $latest
    if (Restart-App) {
      $version = (Get-Health).version
      Log "Live: $version"
      Test-Public $version
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
