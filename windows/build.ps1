# Builds the tvsign Windows desktop app and its per-user installer.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1 [-SkipInstaller] [-Clean]
#
# 1. venv (.venv-build) with Python 3.13 if available, else 3.14
# 2. pip install ../requirements.txt + requirements-windows.txt (pywebview, pystray, pillow, pyinstaller)
# 3. icons (tools/make_icon.py), PyInstaller onedir -> dist\tvsign\
# 4. Inno Setup (installed with winget if missing) -> dist\installer\tvsign-setup-<version>.exe
#
# plumesign.exe is taken from ..\..\Impactor\target\release\plumesign.exe (override: $env:TVSIGN_PLUMESIGN_EXE).
param(
    [switch]$SkipInstaller,
    [switch]$Clean
)
# Native tools report failures through exit codes (Check); 'Stop' would turn their stderr
# warnings into terminating errors under Windows PowerShell 5.1.
$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot
$root = Split-Path $here -Parent
$venv = Join-Path $here '.venv-build'
$version = (Get-Content (Join-Path $here 'version.txt') -Raw).Trim()

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

# ---------- plumesign ----------
$plumesign = $env:TVSIGN_PLUMESIGN_EXE
if (-not $plumesign) { $plumesign = Join-Path (Split-Path $root -Parent) 'Impactor\target\release\plumesign.exe' }
if (-not (Test-Path $plumesign)) { throw "plumesign.exe not found: $plumesign (build Impactor or set TVSIGN_PLUMESIGN_EXE)" }
$env:TVSIGN_PLUMESIGN_EXE = (Resolve-Path $plumesign).Path

# ---------- Python venv ----------
if ($Clean -and (Test-Path $venv)) { Step 'Removing build venv'; Remove-Item -Recurse -Force $venv }
$py = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $py)) {
    $chosen = $null
    foreach ($v in '3.13', '3.14') {
        & py "-$v" -c "import sys" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $chosen = $v; break }
    }
    if (-not $chosen) { throw 'Python 3.13 or 3.14 is required (py launcher: py -3.13).' }
    Step "Creating venv with Python $chosen"
    & py "-$chosen" -m venv $venv; Check 'venv'
}
& $py -c "import sys; print('Python', sys.version)"
Step 'Installing dependencies'
& $py -m pip install --disable-pip-version-check -q --upgrade pip; Check 'pip upgrade'
& $py -m pip install --disable-pip-version-check -q -r (Join-Path $root 'requirements.txt') -r (Join-Path $here 'requirements-windows.txt'); Check 'pip install'
& $py -m pip freeze --disable-pip-version-check | Set-Content -Encoding utf8 (Join-Path $here 'requirements-lock.txt')

# ---------- icons ----------
Step 'Generating icons'
& $py (Join-Path $here 'tools\make_icon.py'); Check 'icons'

# ---------- PyInstaller ----------
Step 'PyInstaller (onedir)'
$dist = Join-Path $here 'dist'
$work = Join-Path $here 'build'
if (Test-Path (Join-Path $dist 'tvsign')) { Remove-Item -Recurse -Force (Join-Path $dist 'tvsign') }
Push-Location $here
try {
    & $py -m PyInstaller --noconfirm --clean --log-level WARN --distpath $dist --workpath $work (Join-Path $here 'tvsign.spec')
    Check 'PyInstaller'
} finally { Pop-Location }
$exe = Join-Path $dist 'tvsign\tvsign.exe'
if (-not (Test-Path $exe)) { throw "build output missing: $exe" }
$size = (Get-ChildItem (Join-Path $dist 'tvsign') -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("dist\tvsign: {0:N1} MB" -f ($size / 1MB))

if ($SkipInstaller) { return }

# ---------- Inno Setup ----------
function Find-Iscc {
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
                     "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                     "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}
$iscc = Find-Iscc
if (-not $iscc) {
    Step 'Installing Inno Setup (winget, per user)'
    winget install --id JRSoftware.InnoSetup -e --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $iscc = Find-Iscc
}
if (-not $iscc) {
    Write-Warning 'Inno Setup not found: installer\tvsign.iss is ready, compile it with ISCC.exe /DAppVersion=<version>.'
    return
}
Step "Inno Setup ($iscc)"
& $iscc /Q "/DAppVersion=$version" (Join-Path $here 'installer\tvsign.iss'); Check 'ISCC'
$setup = Join-Path $dist "installer\tvsign-setup-$version.exe"
Write-Host ("{0}: {1:N1} MB" -f $setup, ((Get-Item $setup).Length / 1MB))
