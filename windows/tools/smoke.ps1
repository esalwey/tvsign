# Smoke test of the built Windows installer. Touches no Apple account and no Apple TV; installs into a
# temporary folder (no autostart, no firewall rule), uses a temporary data folder and a non-default port.
#
#   powershell -ExecutionPolicy Bypass -File smoke.ps1 -Setup dist\installer\tvsign-setup-<version>.exe
#
# 1. silent install                      2. the runtime DLLs sit beside plumesign.exe
# 3. plumesign.exe --version, run from a copy of its folder with a bare PATH (only what ships beside it)
# 4. tvsign.exe --tray: GET /api/livez answers with the installed version, the UI is served
# 5. the wake-up task XML is accepted by Windows' Task Scheduler (then deleted)
# 6. tvsign.exe --quit stops the server cleanly     7. silent uninstall
param(
    [Parameter(Mandatory)][string]$Setup,
    [int]$Port = 18765
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$windows = Split-Path $here -Parent
$base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$tmp = Join-Path $base ('tvsign-smoke-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$app = Join-Path $tmp 'app'
$data = Join-Path $tmp 'data'
$version = (Get-Content (Join-Path $windows 'version.txt') -Raw).Trim()
$py = Join-Path $windows '.venv-build\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = (Get-Command python).Source }
New-Item -ItemType Directory -Force $tmp, $data | Out-Null

function Step($msg) { Write-Host "== $msg" -ForegroundColor Cyan }
function Fail($msg) { throw "SMOKE FAIL: $msg" }
function Show-Log {
    foreach ($f in @((Join-Path $data 'logs\tvsign.log'), (Join-Path $tmp 'setup.log'))) {
        if (Test-Path $f) { Write-Host "---- $f (tail)"; Get-Content $f -Tail 60 }
    }
}

try {
    Step "silent install -> $app"
    $p = Start-Process -FilePath $Setup -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', "/DIR=$app", "/LOG=$tmp\setup.log",
        '/TASKS=!autostart,!firewall,!desktopicon')
    if ($p.ExitCode -ne 0) { Fail "installer exit code $($p.ExitCode)" }
    foreach ($f in 'tvsign.exe', 'plumesign.exe', 'licenses\LICENSE') {
        if (-not (Test-Path (Join-Path $app $f))) { Fail "missing after install: $f" }
    }

    Step 'runtime DLLs beside plumesign.exe'
    & $py (Join-Path $here 'pe_deps.py') check (Join-Path $app 'plumesign.exe')
    if ($LASTEXITCODE -ne 0) { Fail 'plumesign.exe imports a runtime DLL that is not beside it' }

    Step 'plumesign.exe --version from an isolated copy with a bare PATH'
    $iso = Join-Path $tmp 'iso'
    New-Item -ItemType Directory $iso | Out-Null
    Copy-Item (Join-Path $app 'plumesign.exe') $iso
    Get-ChildItem $app -Filter '*.dll' | Copy-Item -Destination $iso
    $savedPath = $env:PATH
    $env:PATH = Join-Path $env:SystemRoot 'System32'
    try {
        $out = & (Join-Path $iso 'plumesign.exe') --version
        $code = $LASTEXITCODE
    } finally { $env:PATH = $savedPath }
    Write-Host $out
    if ($code -ne 0) { Fail "plumesign.exe --version exit code $code (0x$('{0:X}' -f $code))" }

    Step 'launch tvsign.exe --tray'
    $env:TVSIGN_DATA = $data
    $env:TVSIGN_NO_DIALOGS = '1'
    $proc = Start-Process -FilePath (Join-Path $app 'tvsign.exe') -ArgumentList '--tray', '--port', $Port -PassThru
    $live = $null
    for ($i = 0; $i -lt 90 -and -not $live; $i++) {
        Start-Sleep -Seconds 1
        if ($proc.HasExited) { Fail "tvsign.exe exited early (code $($proc.ExitCode))" }
        try { $live = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/livez" -TimeoutSec 3 } catch { $live = $null }
    }
    if (-not $live) { Fail "/api/livez did not answer on port $Port" }
    Write-Host ($live | ConvertTo-Json -Compress)
    if (-not $live.ok) { Fail '/api/livez is not ok' }
    if ($live.version -ne $version) { Fail "version $($live.version) != $version" }
    $index = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 10
    if ($index.StatusCode -ne 200 -or $index.Content -notmatch 'tvsign') { Fail 'the web interface is not served' }

    Step 'Task Scheduler accepts the wake-up task XML'
    $xml = Join-Path $tmp 'wake.xml'
    & $py -I -c "import sys,time; sys.path.insert(0, sys.argv[1]); import winlogic; open(sys.argv[2],'wb').write(winlogic.wake_task_xml(time.time()+86400*30, sys.argv[3], '--tray').encode('utf-16'))" (Join-Path $windows 'launcher') $xml (Join-Path $app 'tvsign.exe')
    if ($LASTEXITCODE -ne 0) { Fail 'could not generate the task XML' }
    schtasks /Create /TN 'tvsign wake smoke' /XML $xml /F | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail 'schtasks rejected the wake-up task' }
    $q = schtasks /Query /TN 'tvsign wake smoke' /XML | Out-String
    if ($q -notmatch 'WakeToRun>true') { Fail 'the wake-up task does not wake the PC' }
    # no execution time limit: the instance started by the task may be the one real tvsign
    if ($q -notmatch 'ExecutionTimeLimit>PT0S<') { Fail 'the wake-up task has an execution time limit' }
    schtasks /Delete /TN 'tvsign wake smoke' /F | Out-Null

    Step 'tvsign.exe --quit'
    $q = Start-Process -FilePath (Join-Path $app 'tvsign.exe') -ArgumentList '--quit' -PassThru -Wait
    for ($i = 0; $i -lt 60 -and -not $proc.HasExited; $i++) { Start-Sleep -Seconds 1 }
    if (-not $proc.HasExited) { Fail 'tvsign.exe did not exit after --quit' }
    $log = Get-Content (Join-Path $data 'logs\tvsign.log') -Raw
    if ($log -notmatch 'server stopped') { Fail 'the log does not show a clean server stop' }
    if (Get-Process plumesign -ErrorAction SilentlyContinue) { Fail 'a plumesign.exe is still running' }

    Step 'silent uninstall'
    $unins = Get-ChildItem $app -Filter 'unins*.exe' | Select-Object -First 1
    $p = Start-Process -FilePath $unins.FullName -Wait -PassThru -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
    if ($p.ExitCode -ne 0) { Fail "uninstaller exit code $($p.ExitCode)" }
    Write-Host 'OK: smoke test passed'
} catch {
    Write-Host $_ -ForegroundColor Red
    Show-Log
    Get-Process tvsign, plumesign -ErrorAction SilentlyContinue | Stop-Process -Force
    exit 1
} finally {
    Remove-Item Env:TVSIGN_DATA, Env:TVSIGN_NO_DIALOGS -ErrorAction SilentlyContinue
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
