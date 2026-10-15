#!/usr/bin/env sh
# Copies the Impactor workspace needed to build plumesign into vendor/Impactor.
#
# Usage: scripts/vendor-impactor.sh [IMPACTOR_DIR] [REF]
#   IMPACTOR_DIR  Impactor git checkout (default: $IMPACTOR_DIR, then ../Impactor next to this repo)
#   REF           commit, branch or tag to export (default: HEAD of that checkout)
#
# Only committed files are exported (git archive): no target/, no .git, no local edits.
# The whole Cargo workspace is kept (every member manifest must exist for `cargo build --locked`),
# minus packaging assets, CI files and screenshots. The source commit is written to
# vendor/Impactor/VENDORED.txt. Commit the result; vendor/Impactor/target stays ignored.
#
# Local patches: every vendor/patches/*.patch is applied on top of the export (in name order)
# and listed in VENDORED.txt. They carry the tvsign changes not yet in the fork (unicast
# discovery: --hint-ip / --ip). Once a patch is merged into the fork branch, delete it.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
SRC=${1:-${IMPACTOR_DIR:-$ROOT/../Impactor}}
REF=${2:-HEAD}
DEST=$ROOT/vendor/Impactor
PATHS="Cargo.toml Cargo.lock LICENSE README.md rustfmt.toml apps crates 3rdparty locales"

[ -d "$SRC/.git" ] || [ -f "$SRC/.git" ] || { echo "error: $SRC is not a git checkout of Impactor" >&2; exit 1; }
COMMIT=$(git -C "$SRC" rev-parse --verify "$REF^{commit}")
BRANCH=$(git -C "$SRC" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
if [ "$REF" = HEAD ] && [ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]; then
    echo "warning: $SRC has uncommitted changes; they are NOT vendored (commit them first)" >&2
fi

# Clear the previous copy but keep a local target/ cache if there is one.
mkdir -p "$DEST"
find "$DEST" -mindepth 1 -maxdepth 1 ! -name target -exec rm -rf {} +

# shellcheck disable=SC2086
git -C "$SRC" -c core.autocrlf=false -c core.eol=lf archive --format=tar "$COMMIT" -- $PATHS | tar -x -C "$DEST"

# Local tvsign patches (paths inside the patches are relative to the repository root).
PATCHES=""
for p in "$ROOT"/vendor/patches/*.patch; do
    [ -f "$p" ] || continue
    git -C "$ROOT" apply --ignore-whitespace "$p" || { echo "error: patch $p does not apply to $COMMIT; rebase it" >&2; exit 1; }
    PATCHES="$PATCHES $(basename "$p")"
done

cat > "$DEST/VENDORED.txt" <<EOF
Vendored from the Impactor fork (plumesign with wireless tvOS pairing/install).
commit: $COMMIT
branch: $BRANCH
date:   $(date -u +%Y-%m-%dT%H:%M:%SZ)
paths:  $PATHS
local patches:${PATCHES:- none} (vendor/patches/, applied by the script after the export)
Regenerate with scripts/vendor-impactor.sh (or .ps1). Do not edit files here by hand:
change the fork (branch tvsign) or add/update a patch in vendor/patches/, then re-vendor.
EOF

echo "vendored Impactor $COMMIT ($BRANCH) into vendor/Impactor"
