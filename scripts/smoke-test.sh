#!/usr/bin/env bash
# Smoke test of a built tvsign image. Touches nothing outside the container: no Apple sign-in,
# no pairing, no install. Usage: scripts/smoke-test.sh [image] (default tvsign:test)
set -euo pipefail

IMAGE=${1:-tvsign:test}
NAME=tvsign-smoke-$$
PORT=${SMOKE_PORT:-18765}
DATA=$(mktemp -d)
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf "$DATA" 2>/dev/null || sudo rm -rf "$DATA" 2>/dev/null || true
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; docker logs "$NAME" 2>&1 | tail -n 50 >&2 || true; exit 1; }

echo "== plumesign binary"
docker run --rm --entrypoint plumesign "$IMAGE" --version
docker run --rm --entrypoint plumesign "$IMAGE" pair --help >/dev/null

echo "== python modules"
docker run --rm -e TVSIGN_DATA=/tmp/d --entrypoint python "$IMAGE" -c \
    "import app.main, app.auth, app.devices, app.diag, app.backup; print('import ok')"

echo "== server"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8765" -v "$DATA:/data" -e TZ=Europe/Paris "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
    curl -fsS "http://127.0.0.1:$PORT/api/livez" >/dev/null 2>&1 && break
    sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/api/livez" || fail "/api/livez not answering"
echo

status=$(curl -fsS "http://127.0.0.1:$PORT/api/auth/status")
echo "$status"
grep -q '"setup_required":true' <<<"$status" || fail "fresh instance should require setup"
grep -q '"setup_code_required":true' <<<"$status" || fail "setup code should be required from outside"

code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/state")
[ "$code" = 401 ] || fail "/api/state without auth returned $code, expected 401"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
    -d '{"code":"x","password":"y"}' "http://127.0.0.1:$PORT/api/setup")
[ "$code" = 403 ] || fail "POST without X-Tvsign returned $code, expected 403 (csrf)"

echo "== setup with the code from /data/setup-code.txt"
setup_code=$(docker exec "$NAME" cat /data/setup-code.txt | tr -d '[:space:]')
[ -n "$setup_code" ] || fail "no setup code written"
jar=$(mktemp)
code=$(curl -s -o /dev/null -w '%{http_code}' -c "$jar" -X POST -H 'X-Tvsign: 1' \
    -H "Origin: http://127.0.0.1:$PORT" -H 'Content-Type: application/json' \
    -d "{\"code\":\"$setup_code\",\"password\":\"smoke-test-password-123\",\"language\":\"en\"}" \
    "http://127.0.0.1:$PORT/api/setup")
[ "$code" = 200 ] || fail "setup returned $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -b "$jar" "http://127.0.0.1:$PORT/api/state")
rm -f "$jar"
[ "$code" = 200 ] || fail "/api/state after setup returned $code"

echo "== runs unprivileged, data is private"
owner=$(docker exec "$NAME" stat -c '%u:%g %a' /data/tvsign.db /data)
echo "$owner"
grep -q '^0:' <<<"$owner" && fail "files in /data are owned by root"
pid_uid=$(docker exec "$NAME" sh -c 'for p in /proc/[0-9]*; do grep -q "python" $p/cmdline 2>/dev/null && awk "/^Uid:/{print \$2}" $p/status; done' | sort -u)
echo "python uid: $pid_uid"
[ -n "$pid_uid" ] && ! grep -qx 0 <<<"$pid_uid" || fail "python runs as root"

echo "== healthcheck"
docker exec "$NAME" python /usr/local/lib/tvsign/healthcheck.py || fail "healthcheck script failed"

echo "OK: smoke test passed for $IMAGE"
