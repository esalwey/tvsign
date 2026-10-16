"""Static service parameters. Everything can be overridden through environment variables.

User-facing settings (notifications, renewal window, anisette server, language) live in
app/settings.py: environment > database > default.
"""
import os
import re
import secrets
import shutil
import sys
from pathlib import Path

VERSION = os.environ.get("TVSIGN_VERSION", "1.0.1-dev")
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


def _num(name: str, default, minimum=None, maximum=None, kind=int):
    """Variable numérique : une valeur illisible ou hors limites ne doit ni empêcher le démarrage
    ni emballer la boucle (intervalle 0). Retombe sur le défaut (illisible) ou borne (hors limites),
    avec un avertissement."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return kind(default)
    try:
        value = kind(float(raw)) if kind is int else float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(raw)
    except (ValueError, OverflowError):
        print(f"[warn] {name}={raw!r} ignorée (nombre attendu), valeur par défaut : {default}", flush=True)
        return kind(default)
    if minimum is not None and value < minimum:
        print(f"[warn] {name}={raw} trop petite, ramenée à {minimum}", flush=True)
        value = kind(minimum)
    if maximum is not None and value > maximum:
        print(f"[warn] {name}={raw} trop grande, ramenée à {maximum}", flush=True)
        value = kind(maximum)
    return value


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
PORT = _num("TVSIGN_PORT", 8765, 1, 65535)

# ---------- security ----------
# "none" disables the admin password (only behind an authenticating reverse proxy / VPN).
AUTH_MODE = _env("TVSIGN_AUTH", "password").lower()
# Admin password preset by the environment (or a Docker secret file): skips the setup step.
PASSWORD_ENV = _env("TVSIGN_PASSWORD")
_pw_file = _env("TVSIGN_PASSWORD_FILE")
# Problème de lecture du fichier de mot de passe (None : rien à signaler), repris par le diagnostic.
PASSWORD_FILE_ERROR: str | None = None
if _pw_file and not PASSWORD_ENV:
    try:
        PASSWORD_ENV = Path(_pw_file).read_text(encoding="utf-8").strip()
        if not PASSWORD_ENV:
            PASSWORD_FILE_ERROR = f"{_pw_file} est vide"
    except OSError as e:
        PASSWORD_ENV = ""
        PASSWORD_FILE_ERROR = f"{_pw_file} illisible ({e.strerror or e.__class__.__name__}) : lisible par PUID/PGID ?"
    if PASSWORD_FILE_ERROR:
        # sans ce message, tvsign démarre en configuration initiale comme si aucun mot de passe n'était prévu
        print(f"[error] TVSIGN_PASSWORD_FILE : {PASSWORD_FILE_ERROR}. Le mot de passe prévu N'EST PAS appliqué.",
              flush=True)
# Desktop edition: requests from 127.0.0.1 (with a loopback Host header) need no setup code,
# and no password unless one was chosen.
TRUST_LOOPBACK = _flag("TVSIGN_TRUST_LOOPBACK")
# Per-launch token handed to the desktop window (GET /auth/launch?token=...).
LAUNCH_TOKEN = _env("TVSIGN_LAUNCH_TOKEN")
# Optional Host allow-list (comma separated), e.g. "tvsign.lan,192.168.1.10:8765".
ALLOWED_HOSTS = [h.strip().lower() for h in _env("TVSIGN_ALLOWED_HOSTS").split(",") if h.strip()]
MAX_UPLOAD_MB = _num("TVSIGN_MAX_UPLOAD_MB", 2048, 1)
# Allow "link" sources that resolve to private / loopback addresses (SSRF guard off).
ALLOW_PRIVATE_URLS = _flag("TVSIGN_ALLOW_PRIVATE_URLS")
SESSION_DAYS = _num("TVSIGN_SESSION_DAYS", 30, 1)

# ---------- sources ----------
GITHUB_TOKEN = _env("TVSIGN_GITHUB_TOKEN")
# Release check of tvsign itself (once a day, opt-out in Settings / TVSIGN_UPDATE_CHECK=off).
UPDATE_REPO = _env("TVSIGN_UPDATE_REPO", "esalwey/tvsign")
EDITION = "windows" if WINDOWS else "docker"
PRESETS_FILE = Path(_env("TVSIGN_PRESETS_FILE") or (ROOT / "app" / "presets.json"))
# Copy an existing PlumeImpactor folder (Apple session, pairings) on first launch.
IMPORT_PLUME = _flag("TVSIGN_IMPORT_PLUME", False)   # opt-in: an existing session could share an Apple ID with another tvsign

# ---------- renewal loop ----------
# Loop period, in seconds.
CHECK_INTERVAL = _num("TVSIGN_CHECK_INTERVAL", 600, 30)
# Delay before retrying a failed app (Apple TV asleep, ...), in minutes.
RETRY_MINUTES = _num("TVSIGN_RETRY_MINUTES", 45, 5)
# Pause before trying again after Apple refused a request outright (error -80045, HTTP 503/429): it
# limits the attempts, and every new one can lengthen the block. In minutes.
APPLE_BACKOFF_MINUTES = _num("TVSIGN_APPLE_BACKOFF_MINUTES", 180, 5)
# Apple session check, in hours.
ACCOUNT_CHECK_HOURS = _num("TVSIGN_ACCOUNT_CHECK_HOURS", 6, 1, kind=float)
# GitHub release check between renewals, in minutes (0 = never).
RELEASE_CHECK_MINUTES = _num("TVSIGN_RELEASE_CHECK_MINUTES", 30, 0, kind=float)
# Max duration of an Apple sign-in (2FA entry included), in seconds.
LOGIN_TIMEOUT = _num("TVSIGN_LOGIN_TIMEOUT", 600, 30)
# Max wait for the plumesign lock before giving up ("plumesign busy"), in seconds.
PLUME_LOCK_TIMEOUT = _num("TVSIGN_PLUME_LOCK_TIMEOUT", 900, 30)
# App open on the Apple TV: installd holds the installation until the app is closed (a process
# alone does not mean the app is on screen: tvOS keeps closed apps in memory). Max wait, in minutes.
APP_WAIT_MINUTES = _num("TVSIGN_APP_WAIT_MINUTES", 30, 1, kind=float)
# Max duration of the installation itself (upload + installd), waiting excluded, in seconds.
INSTALL_TIMEOUT = _num("TVSIGN_INSTALL_TIMEOUT", 900, 60)
# Max duration of a full renewal, in minutes (always leaves room for the app wait).
RENEW_TIMEOUT_MINUTES = max(_num("TVSIGN_RENEW_TIMEOUT_MINUTES", 60, 1, kind=float), APP_WAIT_MINUTES + 25)
# On shutdown, how long a renewal in progress may finish before being interrupted, in seconds
# (keep the container's stop_grace_period above it).
SHUTDOWN_DRAIN_SECONDS = _num("TVSIGN_SHUTDOWN_DRAIN_SECONDS", 240, 0)
# How long plumesign looks for an Apple TV on the network, in seconds.
DEVICE_TIMEOUT = _num("TVSIGN_DEVICE_TIMEOUT", 10, 1)

for d in (DATA_DIR, IPA_DIR, PLUME_HOME, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _pair_host() -> str:
    """Name presented to the Apple TV when pairing (kept per device afterwards).

    The TV derives the pairing identifier from it: two installs sharing a name would overwrite each
    other's pairing. Default: tvsign-<8 hex> drawn once and kept in the data folder, so each install
    shows up under its own name in the TV's Remotes and Devices list. TVSIGN_PAIR_HOST forces a name.
    """
    explicit = _env("TVSIGN_PAIR_HOST")
    if explicit:
        return explicit
    path = DATA_DIR / "pair_host"
    try:
        stored = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"tvsign-[0-9a-f]{8}", stored):
            return stored
    except OSError:
        pass
    name = "tvsign-" + secrets.token_hex(4)
    try:
        path.write_text(name + "\n", encoding="utf-8")
    except OSError:
        return "tvsign"        # dossier en lecture seule : ancien nom fixe plutôt qu'un nom qui change à chaque lancement
    return name


PAIR_HOST = _pair_host()
