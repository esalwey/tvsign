<#
.SYNOPSIS
  Copies the Impactor workspace needed to build plumesign into vendor\Impactor.

.DESCRIPTION
  Only committed files are exported (git archive): no target\, no .git, no local edits.
  The whole Cargo workspace is kept (every member manifest must exist for `cargo build --locked`),
  minus packaging assets, CI files and screenshots. The source commit is written to
  vendor\Impactor\VENDORED.txt. Commit the result; vendor\Impactor\target stays ignored.

  Local patches: every vendor\patches\*.patch is applied on top of the export (in name order)
  and listed in VENDORED.txt. They carry the tvsign changes not yet in the fork (unicast
  discovery: --hint-ip / --ip). Once a patch is merged into the fork branch, delete it.

.EXAMPLE
  scripts\vendor-impactor.ps1
  scripts\vendor-impactor.ps1 -ImpactorDir D:\src\Impactor -Ref tvsign
#>
param(
    [string]$ImpactorDir = $(if ($env:IMPACTOR_DIR) { $env:IMPACTOR_DIR } else { Join-Path $PSScriptRoot '..\..\Impactor' }),
    [string]$Ref = 'HEAD'
)
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$src = (Resolve-Path $ImpactorDir).Path
$dest = Join-Path $root 'vendor\Impactor'
$paths = @('Cargo.toml', 'Cargo.lock', 'LICENSE', 'README.md', 'rustfmt.toml', 'apps', 'crates', '3rdparty', 'locales')

if (-not (Test-Path (Join-Path $src '.git'))) { throw "$src is not a git checkout of Impactor" }
$commit = (git -C $src rev-parse --verify "$Ref^{commit}").Trim()
if ($LASTEXITCODE -ne 0) { throw "unknown ref $Ref in $src" }
$branch = (git -C $src rev-parse --abbrev-ref HEAD).Trim()
if ($Ref -eq 'HEAD' -and (git -C $src status --porcelain --untracked-files=no)) {
    Write-Warning "$src has uncommitted changes; they are NOT vendored (commit them first)"
}

# Clear the previous copy but keep a local target\ cache if there is one.
New-Item -ItemType Directory -Force $dest | Out-Null
Get-ChildItem -Force $dest | Where-Object { $_.Name -ne 'target' } | Remove-Item -Recurse -Force

$tar = Join-Path ([IO.Path]::GetTempPath()) "impactor-$([guid]::NewGuid()).tar"
try {
    git -C $src -c core.autocrlf=false -c core.eol=lf archive --format=tar -o $tar $commit -- @paths
    if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
    # tar.exe (bsdtar) ships with Windows 10 1803+
    & "$env:SystemRoot\System32\tar.exe" -x -f $tar -C $dest
    if ($LASTEXITCODE -ne 0) { throw 'tar extraction failed' }
} finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $tar
}

# Local tvsign patches (paths inside the patches are relative to the repository root).
$applied = @()
foreach ($p in (Get-ChildItem -Path (Join-Path $root 'vendor\patches') -Filter '*.patch' -ErrorAction SilentlyContinue | Sort-Object Name)) {
    git -C $root apply --ignore-whitespace $p.FullName
    if ($LASTEXITCODE -ne 0) { throw "patch $($p.Name) does not apply to $commit; rebase it" }
    $applied += $p.Name
}
$patchLine = if ($applied) { $applied -join ' ' } else { 'none' }

$stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
@"
Vendored from the Impactor fork (plumesign with wireless tvOS pairing/install).
commit: $commit
branch: $branch
date:   $stamp
paths:  $($paths -join ' ')
local patches: $patchLine (vendor/patches/, applied by the script after the export)
Regenerate with scripts/vendor-impactor.sh (or .ps1). Do not edit files here by hand:
change the fork (branch tvsign) or add/update a patch in vendor/patches/, then re-vendor.
"@ -replace "`r`n", "`n" | Set-Content -NoNewline -Encoding ascii (Join-Path $dest 'VENDORED.txt')

Write-Host "vendored Impactor $commit ($branch) into vendor\Impactor"
