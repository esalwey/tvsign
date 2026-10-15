"""Pure logic of the Windows launcher: no Windows dependency, tested by tests/test_windows_launcher.py.

- sleep: when to keep the PC awake, when to wake it (scheduled task);
- tray toasts: same rules as ntfy (no alert for an Apple TV that is merely asleep);
- autostart: reading Windows' StartupApproved flag;
- log: a size-bounded file (a windowed exe has no stdout).
"""
from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

# ---------- sleep ----------

AWAKE_LEAD = 30 * 60        # keep the PC awake in the half hour before a planned renewal...
AWAKE_GRACE = 15 * 60       # ...and for a quarter of an hour after it (the backend loop runs every 10 min)
WAKE_MARGIN = 5 * 60        # the scheduled task wakes the PC 5 min before the due time
NEAR_EXPIRY = 24 * 3600     # a failing app only justifies staying awake when its expiry is close
WAKE_TASK = "tvsign wake"


def attempt_at(app: dict, now: float) -> float | None:
    """When the backend will next try to renew this app, or None when there is no point holding the PC
    awake for it (paused, no Apple TV, already expired, or failing far from its expiry)."""
    if not app.get("enabled") or not app.get("device_id"):
        return None
    exp = app.get("expires_at")
    nxt = app.get("next_attempt_at") or 0
    if exp and exp <= now:
        return None
    if not exp or app.get("status") in ("error", "pending"):
        # first install, or a failure: the backend retries at next_attempt_at (every 45 min)
        if exp and exp - now > NEAR_EXPIRY:
            return None
        return max(nxt, now)
    renew_at = app.get("renew_at")
    if renew_at is None:
        renew_at = exp - 60 * 3600
    return max(renew_at, nxt)


def keep_awake_needed(apps: list[dict], now: float) -> bool:
    """A renewal is running or about to start: Windows must not put the PC to sleep."""
    for a in apps:
        if a.get("running"):
            return True
        t = attempt_at(a, now)
        if t is not None and now - AWAKE_GRACE <= t <= now + AWAKE_LEAD:
            return True
    return False


def next_wake_at(apps: list[dict], now: float) -> float | None:
    """When to wake the PC for the next renewal (WAKE_MARGIN before it), or None. Due times already inside
    the keep-awake window do not count: the PC is awake then."""
    best = None
    for a in apps:
        t = attempt_at(a, now)
        if t is None or a.get("running"):
            continue
        when = t - WAKE_MARGIN
        if when <= now + 60:
            continue
        best = when if best is None else min(best, when)
    return best


def wake_task_xml(when: float, command: str, arguments: str = "", description: str = "") -> str:
    """One-shot scheduled task that wakes the PC (WakeToRun) and starts tvsign in the tray (no effect when it
    already runs). Local time, current user, no administrator rights."""
    start = datetime.fromtimestamp(when).strftime("%Y-%m-%dT%H:%M:%S")
    args = f"\n      <Arguments>{escape(arguments)}</Arguments>" if arguments else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description or "tvsign: wake the PC for the next renewal")}</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>{args}
    </Exec>
  </Actions>
</Task>
"""


# ---------- tray toasts ----------

_RANK = {"fail": 0, "24h": 1, "6h": 2, "expired": 3}
_TOAST = {"fail": "app_failed", "24h": "app_expiring", "6h": "app_expiring", "expired": "app_expired"}


def snapshot(state: dict) -> dict:
    """What /api/state says about the apps (the backend's alert level) and the Apple accounts."""
    snap = {}
    for a in state.get("apps", []):
        snap[f"app:{a['id']}"] = (a.get("alert"), a["name"], a.get("status") == "ok")
    for acc in state.get("accounts", []):
        snap[f"acc:{acc['email']}"] = ("lost" if acc.get("ok") == 0 else None, acc["email"], True)
    return snap


def toasts(prev: dict | None, cur: dict) -> list[tuple[str, str]]:
    """Toasts to show between two snapshots: [(label key, name)]. One per worsening (failed, expires soon,
    expired), one when an app that was in alert is renewed again. The level comes from the backend
    (renew.alert_state): a switched-off Apple TV raises nothing, and successive retries do not repeat it."""
    if prev is None:
        return []
    out = []
    for key, (level, name, ok) in cur.items():
        before = prev.get(key, (None, None, True))[0]
        if level == before:
            continue
        if key.startswith("acc:"):
            if level == "lost":
                out.append(("account_lost", name))
        elif level is None:
            if before is not None and ok:
                out.append(("app_recovered", name))
        elif _RANK.get(level, 0) > _RANK.get(before, -1):
            out.append((_TOAST.get(level, "app_failed"), name))
    return out


# ---------- autostart ----------

def startup_approved(blob: bytes | None) -> bool:
    r"""Our value under HKCU\...\Explorer\StartupApproved\Run: an even first byte (02, 06) means enabled,
    an odd one (03, 07) means disabled in Task Manager or Settings. No value: enabled."""
    if not blob:
        return True
    return blob[0] % 2 == 0


# ---------- log ----------

class RotatingLog:
    """Text stream replacing stdout/stderr, size-bounded: past max_bytes, tvsign.log becomes .1 (then .2).
    The log of a launcher that runs for weeks no longer grows without limit."""

    encoding = "utf-8"

    def __init__(self, path: Path, max_bytes: int = 2 * 1024 * 1024, backups: int = 2):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        self._file = None
        self._size = 0
        self._open()

    def _open(self) -> None:
        try:
            self._size = self.path.stat().st_size
        except OSError:
            self._size = 0
        self._file = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)

    def _rotate(self) -> None:
        try:
            self._file.close()
            for i in range(self.backups, 1, -1):
                src = self.path.with_name(f"{self.path.name}.{i - 1}")
                if src.exists():
                    os.replace(src, self.path.with_name(f"{self.path.name}.{i}"))
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        except OSError:
            pass          # file locked: keep writing to the same one
        self._open()

    def write(self, text: str) -> int:
        with self._lock:
            try:
                if self._size >= self.max_bytes:
                    self._rotate()
                self._file.write(text)
                self._size += len(text.encode("utf-8", "replace"))
            except (OSError, ValueError):
                pass
        return len(text)

    def flush(self) -> None:
        with self._lock:
            try:
                self._file.flush()
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        raise OSError("no file descriptor")

    def close(self) -> None:
        with self._lock:
            try:
                self._file.close()
            except OSError:
                pass


def drain_wait_seconds(environ=None) -> float:
    """How long the launcher waits for a renewal to finish when quitting: the backend's drain time
    (TVSIGN_SHUTDOWN_DRAIN_SECONDS, 240 s) plus a margin for the clean stop."""
    env = os.environ if environ is None else environ
    try:
        drain = float((env.get("TVSIGN_SHUTDOWN_DRAIN_SECONDS") or "240").strip())
    except ValueError:
        drain = 240.0
    return max(drain, 0.0) + 30.0


def quiet_access_path(path: str) -> bool:
    """Polling requests of the launcher and of the web UI: kept out of the access log."""
    return path.split("?", 1)[0] in {"/api/state", "/api/settings", "/api/livez", "/api/update", "/api/auth/status"}
