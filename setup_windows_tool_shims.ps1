# Make git / gh / node resolve on the minimal PATH that DSH shell tools inherit.
#
# Why this exists: DSH's pwsh tool runs
#   powershell.exe -NoLogo -NoProfile -NonInteractive -Command ...
# and passes the harness process's PATH through unchanged (dsh-desktop-host only
# prepends a bin directory for its pnpm package manager, not for shell calls).
# That inherited PATH is the bare Windows baseline, so the persisted (registry)
# PATH entries for Git, GitHub CLI and nvm never reach a shell call. A
# PowerShell profile cannot help because the tool starts with -NoProfile.
#
# Of the directories on that baseline PATH, only WindowsApps is user-writable.
# So the shims live there:
#   node.exe  - hard link to the real nvm binary (node is self-contained).
#   git.cmd   - wrapper, because git.exe is only a 44 KB locator that must run
#               from its own installation directory; copying it breaks with
#               "error launching git".
#   gh.cmd    - wrapper (a 41 MB copy would also work but goes stale on upgrade).
#
# Known limits:
#   - .cmd shims satisfy PowerShell, cmd and Node (PATHEXT), but not Python
#     `subprocess(['git', ...])`, which only resolves `.exe`. The contribution
#     pipeline is unaffected: run_tick.ps1 prepends the real Git/gh directories
#     and contribute.py::command() injects `-c http.sslBackend=openssl`.
#   - PowerShell cannot pass an argument containing double quotes to a native
#     command reliably; that fails for the real executable too and is unrelated
#     to these shims.
#
# Re-run this script if a Windows or Store update clears WindowsApps.

$ErrorActionPreference = 'Stop'

$WindowsApps = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
$HardLinks = @{ 'node.exe' = 'C:\nvm4w\nodejs\node.exe' }
$Wrappers = @{
    'git.cmd' = 'C:\Program Files\Git\cmd\git.exe'
    'gh.cmd'  = 'C:\Program Files\GitHub CLI\gh.exe'
}
# Broken approach from an earlier attempt: copied locator executables.
$StaleCopies = @('git.exe', 'gh.exe')

if (-not (Test-Path $WindowsApps)) { throw "WindowsApps not found: $WindowsApps" }

foreach ($name in $StaleCopies) {
    $path = Join-Path $WindowsApps $name
    if (Test-Path $path) { Remove-Item $path -Force; Write-Host "rm   $name" }
}

foreach ($name in $HardLinks.Keys) {
    $target = $HardLinks[$name]
    $link = Join-Path $WindowsApps $name
    if (-not (Test-Path $target)) { Write-Warning "skip $name (missing $target)"; continue }
    if (Test-Path $link) { Write-Host "ok   $name"; continue }
    try {
        New-Item -ItemType HardLink -Path $link -Target $target | Out-Null
        Write-Host "link $name -> $target"
    } catch {
        Write-Warning "failed $name : $($_.Exception.Message)"
    }
}

foreach ($name in $Wrappers.Keys) {
    $target = $Wrappers[$name]
    $link = Join-Path $WindowsApps $name
    if (-not (Test-Path $target)) { Write-Warning "skip $name (missing $target)"; continue }
    if (Test-Path $link) { Write-Host "ok   $name"; continue }
    $body = "@echo off`r`n`"$target`" %*`r`n"
    [System.IO.File]::WriteAllText($link, $body, [System.Text.Encoding]::ASCII)
    Write-Host "wrap $name -> $target"
}

Write-Host ''
foreach ($tool in 'git', 'gh', 'node') {
    $found = Get-Command $tool -ErrorAction SilentlyContinue
    if ($found) { Write-Host "resolve $tool -> $($found.Source)" }
    else { Write-Warning "$tool still unresolved" }
}
