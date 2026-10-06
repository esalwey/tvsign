"""Docker HEALTHCHECK: GET /api/livez (200 while the server answers; never filtered by auth).

/api/health is deliberately not used: it reports 503 while an app is failing, which would make
autoheal-style tools restart the container in a loop.
"""
import os
import sys
import urllib.request

host = os.environ.get("TVSIGN_HOST", "").strip() or "0.0.0.0"
if host in ("0.0.0.0", "::"):
    host = "127.0.0.1"
if ":" in host:
    host = f"[{host}]"
port = os.environ.get("TVSIGN_PORT", "").strip() or "8765"
try:
    with urllib.request.urlopen(f"http://{host}:{port}/api/livez", timeout=4) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception as exc:  # noqa: BLE001
    print(f"unhealthy: {exc}", file=sys.stderr)
    sys.exit(1)
