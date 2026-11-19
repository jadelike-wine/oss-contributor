# OSS Contributor scheduler wrapper: single-instance lock + per-tick logging.
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$logDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logFile = Join-Path $logDir "tick-$stamp.log"

# Single-instance mutex (prevents overlapping ticks from double-writing SQLite).
$mutex = New-Object System.Threading.Mutex($false, 'Global\OSS-Contributor-Tick')
if (-not $mutex.WaitOne(0)) {
    "SKIP $(Get-Date -Format o) previous tick still running" | Tee-Object -FilePath $logFile -Append
    exit 0
}
try {
    $env:Path = "C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI;" + $env:Path
    $env:OSS_CONTRIBUTOR_ROOT = $Root
    # API keys come from User env; ensure present for the subprocess.
    if (-not $env:OSS_DSH_API_KEY)   { $env:OSS_DSH_API_KEY   = [Environment]::GetEnvironmentVariable('OSS_DSH_API_KEY','User') }
    if (-not $env:TOKENKING_API_KEY){ $env:TOKENKING_API_KEY= [Environment]::GetEnvironmentVariable('TOKENKING_API_KEY','User') }
    $py = "C:\Users\lyp99\AppData\Roaming\uv\python\cpython-3.14-windows-x86_64-none\python.exe"
    $hermes = "$env:LOCALAPPDATA\hermes\bin"
    if (Test-Path $hermes) { $env:Path = "$hermes;" + $env:Path }

    "=== TICK START $(Get-Date -Format o) ===" | Tee-Object -FilePath $logFile
    & $py -X utf8 (Join-Path $Root 'hermes_tick.py') *>&1 | Tee-Object -FilePath $logFile -Append
    "=== TICK END $(Get-Date -Format o) exit=$LASTEXITCODE ===" | Tee-Object -FilePath $logFile -Append

    # Heartbeat for health monitoring; surface a non-zero tick exit instead of
    # always reporting success (e.g. the daily model budget being exhausted).
    $hb = @{
        last_tick = (Get-Date).ToString('o'); log = $logFile; exit = $LASTEXITCODE
        pid = $PID; state = $(if ($LASTEXITCODE -eq 0) { 'ok' } else { 'failed' })
    } | ConvertTo-Json
    Set-Content (Join-Path $logDir 'heartbeat.json') $hb -Encoding utf8
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally { $mutex.ReleaseMutex(); $mutex.Dispose() }
