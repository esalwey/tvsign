#!/bin/sh
# tvsign container entrypoint (runs under tini).
# Started as root (default): prepares /data, hands it to PUID:PGID (default 1000:1000), then drops
# to that user for good. Started with `user:` already non-root: just checks /data is writable.
set -eu

PUID=${PUID:-1000}
PGID=${PGID:-1000}
DATA=${TVSIGN_DATA:-/data}
umask 077    # accounts, certificate keys and pairings are secrets

if [ "$(id -u)" = 0 ]; then
    case "$PUID$PGID" in
        *[!0-9]*) echo "tvsign: PUID/PGID must be numeric" >&2; exit 1 ;;
    esac
    if [ "$PUID" = 0 ]; then
        echo "tvsign: refusing to run as root (PUID=0); pick an unprivileged uid" >&2
        exit 1
    fi
    mkdir -p "$DATA" "$HOME"
    # Only walk the tree when ownership is wrong (first start, or PUID changed).
    if [ "$(stat -c %u:%g "$DATA")" != "$PUID:$PGID" ] || \
       [ -n "$(find "$DATA" \( ! -user "$PUID" -o ! -group "$PGID" \) -print -quit 2>/dev/null)" ]; then
        echo "tvsign: giving $DATA to $PUID:$PGID"
        chown -R "$PUID:$PGID" "$DATA"
    fi
    chmod 700 "$DATA"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --inh-caps=-all --bounding-set=-all \
        "$@"
fi

mkdir -p "$DATA" "$HOME" 2>/dev/null || true
if ! [ -w "$DATA" ]; then
    echo "tvsign: $DATA is not writable by uid $(id -u); fix the volume owner (chown -R $(id -u):$(id -g) <host folder>)" >&2
    exit 1
fi
exec "$@"
