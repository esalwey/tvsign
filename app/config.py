"""Static service parameters. Everything can be overridden through environment variables.

User-facing settings (notifications, renewal window, anisette server, language) live in
app/settings.py: environment > database > default.
"""
import os
import shutil
import sys
from pathlib import Path

VERSION = os.environ.get("TVSIGN_VERSION", "1.0.0-dev")
WINDOWS = os.name == "nt"
FROZEN = bool(getattr(sys, "frozen", False))

# Code and web assets (PyInstaller unpacks data files under sys._MEIPASS).
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
# Folder of the installed application (where a bundled plumesign sits).
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else ROOT


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _default_data_dir() -> Path:
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "tvsign"
    return ROOT / "data"


DATA_DIR = Path(_env("TVSIGN_DATA") or _default_data_dir()).resolve()
IPA_DIR = DATA_DIR / "ipas"
PLUME_HOME = DATA_DIR / "plume"          # holds PlumeImpactor/ (accounts, keys, anisette, pairings)
DB_PATH = DATA_DIR / "tvsign.db"
TMP_DIR = DATA_DIR / "tmp"


def _default_plumesign() -> Path:
    exe = "plumesign.exe" if WINDOWS else "plumesign"
    candidates = [APP_DIR / exe, APP_DIR / "bin" / exe]
    if not FROZEN:
        candidates += [ROOT / exe, ROOT / "bin" / exe]
    for c in candidates:
        if c.is_file():
            return c
    found = shutil.which("plumesign")
    if found:
        return Path(found)
    if not WINDOWS and Path("/usr/local/bin/plumesign").exists():
        return Path("/usr/local/bin/plumesign")
    return Path(exe)   # not found: reported by /api/diagnostics


PLUMESIGN = Path(_env("TVSIGN_PLUMESIGN") or _default_plumesign())

# Web server. Windows (desktop app) listens on loopback only by default.
HOST = _env("TVSIGN_HOST", "127.0.0.1" if WINDOWS else "0.0.0.0")
PORT = int(_env("TVSIGN_PORT", "8765"))

# ---------- security ----------
# "none" disables the admin password (only behind an authenticating reverse proxy / VPN).
AUTH_MODE = _env("TVSIGN_AUTH", "password").lower()
# Admin password preset by the environment (or a Docker secret file): skips the setup step.
PASSWORD_ENV = _env("TVSIGN_PASSWORD")
_pw_file = _env("TVSIGN_PASSWORD_FILE")
if _pw_file and not PASSWORD_ENV:
    try:
        PASSWORD_ENV = Path(_pw_file).read_text(encoding="utf-8").strip()
    except OSError:
        PASSWORD_ENV = ""
# Desktop edition: requests from 127.0.0.1 (with a loopback Host header) need no setup code,
# and no password unless one was chosen.
TRUST_LOOPBACK = _flag("TVSIGN_TRUST_LOOPBACK")
# Per-launch token handed to the desktop window (GET /auth/launch?token=...).
LAUNCH_TOKEN = _env("TVSIGN_LAUNCH_TOKEN")
# Optional Host allow-list (comma separated), e.g. "tvsign.lan,192.168.1.10:8765".
ALLOWED_HOSTS = [h.strip().lower() for h in _env("TVSIGN_ALLOWED_HOSTS").split(",") if h.strip()]
MAX_UPLOAD_MB = int(_env("TVSIGN_MAX_UPLOAD_MB", "2048"))
# Allow "link" sources that resolve to private / loopback addresses (SSRF guard off).
ALLOW_PRIVATE_URLS = _flag("TVSIGN_ALLOW_PRIVATE_URLS")
SESSION_DAYS = int(_env("TVSIGN_SESSION_DAYS", "30"))

# ---------- sources ----------
GITHUB_TOKEN = _env("TVSIGN_GITHUB_TOKEN")
PRESETS_FILE = Path(_env("TVSIGN_PRESETS_FILE") or (ROOT / "app" / "presets.json"))
# Copy an existing PlumeImpactor folder (Apple session, pairings) on first launch.
IMPORT_PLUME = _flag("TVSIGN_IMPORT_PLUME", False)   # opt-in: an existing session could share an Apple ID with another tvsign

# ---------- renewal loop ----------
# Loop period, in seconds.
CHECK_INTERVAL = int(_env("TVSIGN_CHECK_INTERVAL", "600"))
# Delay before retrying a failed app (Apple TV asleep, ...), in minutes.
RETRY_MINUTES = int(_env("TVSIGN_RETRY_MINUTES", "45"))
# Apple session check, in hours.
ACCOUNT_CHECK_HOURS = float(_env("TVSIGN_ACCOUNT_CHECK_HOURS", "6"))
# GitHub release check between renewals, in minutes (0 = never).
RELEASE_CHECK_MINUTES = float(_env("TVSIGN_RELEASE_CHECK_MINUTES", "30"))
# Max duration of an Apple sign-in (2FA entry included), in seconds.
LOGIN_TIMEOUT = int(_env("TVSIGN_LOGIN_TIMEOUT", "600"))
# Max wait for the plumesign lock before giving up ("plumesign busy"), in seconds.
PLUME_LOCK_TIMEOUT = int(_env("TVSIGN_PLUME_LOCK_TIMEOUT", "900"))
# App open on the Apple TV: installd holds the installation until the app is closed (a process
# alone does not mean the app is on screen: tvOS keeps closed apps in memory). Max wait, in minutes.
APP_WAIT_MINUTES = float(_env("TVSIGN_APP_WAIT_MINUTES", "30"))
# Max duration of the installation itself (upload + installd), waiting excluded, in seconds.
INSTALL_TIMEOUT = int(_env("TVSIGN_INSTALL_TIMEOUT", "900"))
# Max duration of a full renewal, in minutes (always leaves room for the app wait).
RENEW_TIMEOUT_MINUTES = max(float(_env("TVSIGN_RENEW_TIMEOUT_MINUTES", "60")), APP_WAIT_MINUTES + 25)
# On shutdown, how long a renewal in progress may finish before being interrupted, in seconds
# (keep the container's stop_grace_period above it).
SHUTDOWN_DRAIN_SECONDS = int(_env("TVSIGN_SHUTDOWN_DRAIN_SECONDS", "240"))
# How long plumesign looks for an Apple TV on the network, in seconds.
DEVICE_TIMEOUT = int(_env("TVSIGN_DEVICE_TIMEOUT", "10"))
# Name presented to the Apple TV when pairing (kept per device afterwards).
PAIR_HOST = _env("TVSIGN_PAIR_HOST", "tvsign")

# ---------- update check ----------
# GitHub repository whose latest release is compared with VERSION (setting "update_check" turns it off).
UPDATE_REPO = _env("TVSIGN_UPDATE_REPO", "esalwey/tvsign")

for d in (DATA_DIR, IPA_DIR, PLUME_HOME, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)
