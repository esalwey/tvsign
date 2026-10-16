"""tvsign for Windows: native window (WebView2) + tray icon + in-process backend.

    tvsign.exe            open the window (or bring the running instance to front)
    tvsign.exe --tray     start in the notification area only (used by autostart)
    tvsign.exe --quit     ask the running instance to exit (used by the installer)

The backend (app.main) runs in a background thread on 127.0.0.1, port 8765 when free, otherwise a
random free port. The window signs in with a per-launch token (TVSIGN_LAUNCH_TOKEN); requests
from loopback are trusted (TVSIGN_TRUST_LOOPBACK) until the user sets a password.
Closing the window hides it; "Quit" in the tray menu stops everything (a renewal in progress gets
SHUTDOWN_DRAIN_SECONDS to finish). While a renewal runs or is about to, the PC is kept awake
(SetThreadExecutionState); an opt-in tray option also schedules a wake-up for the next renewal.
Pure decisions (when to stay awake, which toasts, log rotation...) live in winlogic.py.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from ctypes import wintypes
from pathlib import Path

import winlogic

APP_NAME = "tvsign"
MUTEX_NAME = "tvsign-desktop-instance"        # also AppMutex in the Inno Setup script
SHOW_EVENT = "tvsign-desktop-show"
QUIT_EVENT = "tvsign-desktop-quit"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"    # Evergreen bootstrapper
STARTUP_SETTINGS = "ms-settings:startupapps"
DEFAULT_PORT = 8765
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
TVSIGN_ROOT = Path(__file__).resolve().parents[2]   # source tree: tvsign/ (holds app/ and web/)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
kernel32.SetThreadExecutionState.restype = wintypes.DWORD
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
SM_SHUTTINGDOWN = 0x2000
CLOSE_REASON_WINDOWS_SHUTDOWN = 1       # System.Windows.Forms.CloseReason
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0


# ---------- paths, logging ----------

def data_dir() -> Path:
    custom = os.environ.get("TVSIGN_DATA", "").strip()
    if custom:
        return Path(custom).resolve()
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def setup_logging(folder: Path) -> Path:
    """A windowed exe has no stdout/stderr: everything goes to <data>/logs/tvsign.log."""
    logs = folder / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "tvsign.log"
    # 2 MB per file, tvsign.log.1 and .2 kept: the app runs for weeks without a restart
    stream = winlogic.RotatingLog(log)
    if FROZEN or sys.stdout is None:
        sys.stdout = sys.stderr = stream
    return log


def say(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [desktop] {msg}", flush=True)


def app_version() -> str:
    for p in (BUNDLE / "version.txt", Path(__file__).resolve().parent.parent / "version.txt"):
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "0.9.0-dev"


# ---------- single instance (named mutex + named events) ----------

def acquire_instance() -> wintypes.HANDLE | None:
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def signal_event(name: str) -> bool:
    h = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not h:
        return False
    kernel32.SetEvent(h)
    kernel32.CloseHandle(h)
    return True


def instance_running() -> bool:
    h = kernel32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
    if h:
        kernel32.CloseHandle(h)
        return True
    return False


def watch_event(name: str, callback) -> None:
    """Auto-reset event: another launch (or the installer) signals it."""
    h = kernel32.CreateEventW(None, False, False, name)

    def run():
        while True:
            if kernel32.WaitForSingleObject(h, 0xFFFFFFFF) == WAIT_OBJECT_0:
                try:
                    callback()
                except Exception as e:  # noqa: BLE001
                    say(f"event {name}: {e}")

    threading.Thread(target=run, name=f"event-{name}", daemon=True).start()


def message_box(text: str, title: str = APP_NAME) -> None:
    say(f"dialog: {text}")
    if os.environ.get("TVSIGN_NO_DIALOGS"):        # CI: never block on a dialog
        return
    user32.MessageBoxW(None, text, title, 0x10)   # MB_ICONERROR


def ask_yes_no(text: str, title: str = APP_NAME) -> bool:
    say(f"question: {text}")
    if os.environ.get("TVSIGN_NO_DIALOGS"):
        return False
    return user32.MessageBoxW(None, text, title, 0x24) == 6   # MB_YESNO | MB_ICONQUESTION -> IDYES


def keep_awake(on: bool) -> None:
    """Called from one dedicated thread only: the execution state belongs to the calling thread."""
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    if not kernel32.SetThreadExecutionState(flags):
        say("SetThreadExecutionState failed")


def system_shutting_down() -> bool:
    try:
        return bool(user32.GetSystemMetrics(SM_SHUTTINGDOWN))
    except Exception:  # noqa: BLE001
        return False


# ---------- autostart (HKCU Run) ----------

def autostart_command() -> str:
    if FROZEN:
        return f'"{sys.executable}" --tray'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" "{Path(__file__).resolve()}" --tray'


def _run_value_exists() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def _approved_blob() -> bytes | None:
    """The enabled/disabled flag Task Manager and Windows Settings set on a startup entry (no flag: enabled)."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
            blob, kind = winreg.QueryValueEx(k, APP_NAME)
            return bytes(blob) if kind == winreg.REG_BINARY else None
    except OSError:
        return None


def autostart_enabled() -> bool:
    return _run_value_exists() and winlogic.startup_approved(_approved_blob())


def autostart_blocked() -> bool:
    """The entry exists but the user switched it off in Windows: nothing starts at the next sign-in."""
    return _run_value_exists() and not winlogic.startup_approved(_approved_blob())


def set_autostart(enable: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
            # a "disabled" flag left by Windows Settings survives the removal of the Run value
            try:
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as a:
                    winreg.SetValueEx(a, APP_NAME, 0, winreg.REG_BINARY, b"\x02" + b"\x00" * 11)
            except OSError:
                pass
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ---------- labels (follow the backend "language" setting) ----------

LABELS = {
    "fr": {
        "open": "Ouvrir",
        "browser": "Ouvrir dans le navigateur",
        "renew": "Renouveler maintenant",
        "autostart": "Démarrer avec Windows",
        "autostart_blocked": "Démarrage désactivé dans Windows (ouvrir les réglages)",
        "wake": "Réveiller le PC pour les renouvellements",
        "update": "Nouvelle version {version} : télécharger",
        "quit": "Quitter",
        "tooltip": "tvsign",
        "hidden": "tvsign continue en arrière-plan pour renouveler vos apps. "
                  "Clic droit sur l'icône pour quitter.",
        "renew_started": "Renouvellement lancé : {names}.",
        "renew_none": "Aucune app à renouveler.",
        "renew_failed": "Impossible de lancer le renouvellement : {err}",
        "app_failed": "{name} : le renouvellement a échoué.",
        "app_expiring": "{name} : le profil expire bientôt et le renouvellement échoue. Ouvrez tvsign.",
        "app_expired": "{name} : le profil a expiré. Ouvrez tvsign.",
        "app_cert_expiring": "{name} : le certificat importé expire bientôt. Importez-en un nouveau dans tvsign.",
        "app_recovered": "{name} : renouvelée.",
        "account_lost": "Compte Apple {email} : reconnexion nécessaire.",
        "update_toast": "tvsign {version} est disponible. Clic droit sur l'icône pour la télécharger.",
        "quitting_wait": "tvsign termine le renouvellement en cours avant de s'arrêter…",
        "wake_failed": "Impossible de programmer le réveil du PC : {err}",
        "start_failed": "tvsign n'a pas pu démarrer son serveur local.\n\n{err}\n\nJournal : {log}",
        "webview_missing": "tvsign a besoin de Microsoft Edge WebView2 pour afficher sa fenêtre, mais il est "
                           "introuvable ou ne démarre pas.\n\n{err}\n\nOuvrir la page de téléchargement ?\n\n"
                           "Dans tous les cas, tvsign reste actif près de l'horloge : clic droit sur l'icône, "
                           "puis « Ouvrir dans le navigateur ».",
        "webview_missing_toast": "WebView2 est introuvable. Clic droit sur l'icône, puis « Ouvrir dans le navigateur ».",
    },
    "en": {
        "open": "Open",
        "browser": "Open in browser",
        "renew": "Renew now",
        "autostart": "Start with Windows",
        "autostart_blocked": "Startup disabled in Windows (open settings)",
        "wake": "Wake the PC for renewals",
        "update": "New version {version}: download",
        "quit": "Quit",
        "tooltip": "tvsign",
        "hidden": "tvsign keeps running in the background to renew your apps. "
                  "Right-click the icon to quit.",
        "renew_started": "Renewal started: {names}.",
        "renew_none": "No app to renew.",
        "renew_failed": "Could not start the renewal: {err}",
        "app_failed": "{name}: renewal failed.",
        "app_expiring": "{name}: the profile expires soon and renewal is failing. Open tvsign.",
        "app_expired": "{name}: the profile has expired. Open tvsign.",
        "app_cert_expiring": "{name}: the imported certificate expires soon. Import a new one in tvsign.",
        "app_recovered": "{name}: renewed.",
        "account_lost": "Apple account {email}: sign in again.",
        "update_toast": "tvsign {version} is available. Right-click the icon to download it.",
        "quitting_wait": "tvsign is finishing the renewal in progress before it stops…",
        "wake_failed": "Could not schedule the PC wake-up: {err}",
        "start_failed": "tvsign could not start its local server.\n\n{err}\n\nLog: {log}",
        "webview_missing": "tvsign needs Microsoft Edge WebView2 to show its window, but it is missing or "
                           "does not start.\n\n{err}\n\nOpen the download page?\n\n"
                           "Either way tvsign keeps running next to the clock: right-click the icon, then "
                           "\"Open in browser\".",
        "webview_missing_toast": "WebView2 is missing. Right-click the icon, then \"Open in browser\".",
    },
}


def windows_language() -> str:
    try:
        return "fr" if (kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x0C else "en"
    except Exception:  # noqa: BLE001
        return "en"


class _RedactToken(logging.Filter):
    """Keeps the per-launch token out of the access log (GET /auth/launch?token=...)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, tuple):
            record.args = tuple(re.sub(r"token=[^&\s]+", "token=***", a) if isinstance(a, str) else a
                                for a in record.args)
        return True


class _QuietPolling(logging.Filter):
    """The launcher polls the API every minute (and an open window every few seconds): successful polls
    stay out of the access log, which would otherwise fill up over weeks."""

    def filter(self, record: logging.LogRecord) -> bool:
        a = record.args
        if isinstance(a, tuple) and len(a) >= 5 and isinstance(a[2], str) and isinstance(a[4], int):
            return not (a[4] < 400 and winlogic.quiet_access_path(a[2]))
        return True


# ---------- Task Scheduler (opt-in wake-up) ----------

def wake_command() -> tuple[str, str]:
    """What the scheduled task runs: tvsign in the tray (no effect when it already runs)."""
    if FROZEN:
        return sys.executable, "--tray"
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return str(pyw if pyw.exists() else sys.executable), f'"{Path(__file__).resolve()}" --tray'


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, timeout=30,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def create_wake_task(when: float, folder: Path) -> None:
    command, arguments = wake_command()
    xml = winlogic.wake_task_xml(when, command, arguments)
    path = folder / "wake-task.xml"
    path.write_bytes(xml.encode("utf-16"))          # BOM + UTF-16, as the XML declaration says
    r = _schtasks("/Create", "/TN", winlogic.WAKE_TASK, "/XML", str(path), "/F")
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout or f"schtasks exit {r.returncode}").strip())


def delete_wake_task() -> None:
    _schtasks("/Delete", "/TN", winlogic.WAKE_TASK, "/F")


# ---------- the desktop application ----------

class Desktop:
    def __init__(self, port: int, token: str, data: Path, log: Path, start_hidden: bool):
        self.port = port
        self.token = token
        self.data = data
        self.log = log
        self.base = f"http://127.0.0.1:{port}"
        self.start_hidden = start_hidden
        self.lang = windows_language()
        self.quitting = False
        self.session_ending = False    # Windows is shutting down / signing out: let the window close
        self.fallback = False          # no WebView2: tray only, the UI opens in the default browser
        self.window = None
        self.icon = None
        self.server = None
        self.server_thread = None
        self.loaded = False            # window navigated to the launch URL yet
        self.ready = threading.Event()
        self.stop = threading.Event()        # set by quit(): the main thread waits on it when there is no window
        self.finished = threading.Event()    # set when everything is stopped (ends the keep-awake thread)
        self.prefs_path = data / "desktop.json"
        self.prefs = self._read_prefs()
        self.apps: list[dict] = []           # apps of the last /api/state (keep-awake and wake-up decisions)
        self.update: dict | None = None      # the "update" part of /api/state when a newer version exists
        self._wake_retry_at = 0.0
        self._lock = threading.Lock()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ----- small helpers -----
    def t(self, key: str, **kw) -> str:
        return LABELS.get(self.lang, LABELS["en"])[key].format(**kw)

    def _read_prefs(self) -> dict:
        try:
            return json.loads(self.prefs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_prefs(self) -> None:
        try:
            self.prefs_path.write_text(json.dumps(self.prefs), encoding="utf-8")
        except OSError:
            pass

    def api(self, method: str, path: str, timeout: float = 15):
        req = urllib.request.Request(self.base + path, method=method, headers={
            "Authorization": f"Bearer {self.token}", "X-Tvsign": "1", "Accept": "application/json"})
        with self._opener.open(req, timeout=timeout) as r:
            body = r.read()
        return json.loads(body.decode("utf-8")) if body else None

    def notify(self, text: str) -> None:
        say(f"notify: {text}")
        try:
            if self.icon is not None:
                self.icon.notify(text, APP_NAME)
        except Exception as e:  # noqa: BLE001
            say(f"notify failed: {e}")

    # ----- backend -----
    def start_server(self) -> None:
        import uvicorn
        from app.main import app as asgi   # imported after the TVSIGN_* environment is set

        config = uvicorn.Config(
            asgi, host="127.0.0.1", port=self.port, reload=False, workers=1, loop="asyncio",
            proxy_headers=False, server_header=False, log_level=os.environ.get("TVSIGN_LOG_LEVEL", "info"),
        )
        self.server = uvicorn.Server(config)
        access = logging.getLogger("uvicorn.access")
        access.addFilter(_RedactToken())
        access.addFilter(_QuietPolling())

        def run():
            try:
                # Proactor loop (Windows default): needed for asyncio subprocesses (plumesign)
                asyncio.run(self.server.serve())
            except BaseException as e:  # noqa: BLE001
                say(f"server stopped with error: {e!r}")
            finally:
                say("server stopped")

        self.server_thread = threading.Thread(target=run, name="uvicorn", daemon=True)
        self.server_thread.start()

    def wait_ready(self, timeout: float = 60) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.server_thread and not self.server_thread.is_alive():
                return False
            try:
                with self._opener.open(self.base + "/api/livez", timeout=2) as r:
                    if r.status == 200:
                        self.ready.set()
                        return True
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.25)
        return False

    # ----- window -----
    def launch_url(self) -> str:
        return f"{self.base}/auth/launch?token={self.token}"

    def open_browser(self) -> None:
        """The UI in the default browser (loopback only): works without WebView2."""
        self.ready.wait(60)
        webbrowser.open(self.launch_url())

    def show(self) -> None:
        if self.quitting:
            return
        if self.window is None:
            if self.fallback:
                self.open_browser()
            return
        with self._lock:
            if not self.loaded:
                self.ready.wait(60)
                self.window.load_url(self.launch_url())
                self.loaded = True
        self.window.show()
        self.window.restore()
        try:
            self.window.on_top = True
            self.window.on_top = False
        except Exception:  # noqa: BLE001
            pass

    def on_closing(self):
        if self.quitting or self.session_ending or system_shutting_down():
            return True
        threading.Thread(target=self._hide, daemon=True).start()
        return False                     # cancel the close: hide to the tray instead

    def hook_form(self) -> None:
        """Runs before the form is shown. pywebview's closing callback knows no close reason, so a Windows
        shutdown or sign-out would be cancelled like a click on the X, and Windows would report that tvsign
        prevents the shutdown. The WinForms event carries the reason: accept the close for a shutdown."""
        try:
            def form_closing(sender, args):
                if int(args.CloseReason) == CLOSE_REASON_WINDOWS_SHUTDOWN:
                    self.session_ending = True
                    args.Cancel = False
                    say("Windows is ending the session: closing")

            self.window.native.FormClosing += form_closing
        except Exception as e:  # noqa: BLE001 - the SM_SHUTTINGDOWN check in on_closing remains
            say(f"close-reason hook unavailable: {e}")

    def _hide(self) -> None:
        self.window.hide()
        if not self.prefs.get("hint_shown"):
            self.prefs["hint_shown"] = True
            self._write_prefs()
            self.notify(self.t("hidden"))

    # ----- tray actions -----
    def renew_now(self) -> None:
        try:
            state = self.api("GET", "/api/state")
            names = []
            for a in state.get("apps", []):
                if a.get("enabled") and a.get("device_id"):
                    self.api("POST", f"/api/apps/{a['id']}/renew")
                    names.append(a["name"])
            self.notify(self.t("renew_started", names=", ".join(names)) if names else self.t("renew_none"))
        except Exception as e:  # noqa: BLE001
            self.notify(self.t("renew_failed", err=e))

    def toggle_autostart(self) -> None:
        try:
            if autostart_blocked():
                os.startfile(STARTUP_SETTINGS)       # Windows owns that switch: send the user there
                return
            set_autostart(not autostart_enabled())
        except OSError as e:
            say(f"autostart: {e}")
        if self.icon is not None:
            self.icon.update_menu()

    def toggle_wake(self) -> None:
        self.prefs["wake_pc"] = not self.prefs.get("wake_pc")
        self._write_prefs()
        if self.prefs["wake_pc"]:
            self._sync_wake()
        else:
            self._clear_wake()
        if self.icon is not None:
            self.icon.update_menu()

    def open_update(self) -> None:
        url = (self.update or {}).get("url")
        if url and url.startswith("https://"):
            webbrowser.open(url)

    def quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        say("quit requested")
        try:
            if self.window is not None:
                self.window.destroy()    # webview.start() returns on the main thread
        except Exception as e:  # noqa: BLE001
            say(f"destroy: {e}")
        self.stop.set()

    # ----- keep awake / wake-up -----
    def awake_loop(self) -> None:
        """One thread owns the execution state (it is per thread). The PC stays awake while a renewal runs
        or is about to start, and may sleep again afterwards. A sleep the user asks for (menu, lid, power
        button) still wins: Windows does not let an application block those."""
        on = None
        while not self.finished.is_set():
            need = winlogic.keep_awake_needed(self.apps, time.time())
            if need != on:
                say("keeping the PC awake" if need else "the PC may sleep again")
                on = need
            keep_awake(need)
            self.finished.wait(15)
        keep_awake(False)

    def _clear_wake(self) -> None:
        if self.prefs.pop("wake_at", None):
            self._write_prefs()
        try:
            delete_wake_task()
        except Exception as e:  # noqa: BLE001
            say(f"wake task: {e}")

    def _sync_wake(self) -> None:
        """Keep one scheduled task, set a few minutes before the next renewal, in line with the apps."""
        if not self.prefs.get("wake_pc"):
            return
        now = time.time()
        want = winlogic.next_wake_at(self.apps, now)
        have = self.prefs.get("wake_at")
        if want is None:
            if have:
                self._clear_wake()
            return
        if (have and have > now and abs(want - have) < 300) or now < self._wake_retry_at:
            return
        try:
            create_wake_task(want, self.data)
            self.prefs["wake_at"] = want
            self._write_prefs()
            say("wake-up scheduled for " + time.strftime("%Y-%m-%d %H:%M", time.localtime(want)))
        except Exception as e:  # noqa: BLE001
            self._wake_retry_at = now + 600
            say(f"wake task failed: {e}")
            self.notify(self.t("wake_failed", err=e))

    # ----- tray icon -----
    def start_tray(self) -> None:
        import pystray
        from PIL import Image

        icon_path = BUNDLE / "tray.png"
        if not icon_path.exists():
            icon_path = Path(__file__).resolve().parent.parent / "assets" / "tray.png"
        try:
            image = Image.open(icon_path).convert("RGBA")
        except Exception:  # noqa: BLE001
            image = Image.new("RGBA", (32, 32), (30, 30, 34, 255))

        def autostart_label(_):
            return self.t("autostart_blocked" if autostart_blocked() else "autostart")

        def later(fn):
            return lambda: threading.Thread(target=fn, daemon=True).start()

        item = pystray.MenuItem
        menu = pystray.Menu(
            item(lambda _: self.t("open"), later(self.show), default=True),
            item(lambda _: self.t("browser"), later(self.open_browser)),
            item(lambda _: self.t("renew"), later(self.renew_now)),
            item(lambda _: self.t("update", version=str((self.update or {}).get("version", "")).lstrip("v")),
                 self.open_update, visible=lambda _: bool(self.update)),
            pystray.Menu.SEPARATOR,
            item(autostart_label, self.toggle_autostart, checked=lambda _: autostart_enabled()),
            item(lambda _: self.t("wake"), self.toggle_wake, checked=lambda _: bool(self.prefs.get("wake_pc"))),
            pystray.Menu.SEPARATOR,
            item(lambda _: self.t("quit"), self.quit),
        )
        self.icon = pystray.Icon(APP_NAME, image, APP_NAME, menu)
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    # ----- background watcher: language, notifications, wake-up, update check -----
    def watch(self) -> None:
        known: dict | None = None
        while not self.quitting:
            try:
                s = self.api("GET", "/api/settings")
                lang = (s.get("settings", {}).get("language", {}) or {}).get("value") or "auto"
                new_lang = windows_language() if lang == "auto" else ("fr" if lang == "fr" else "en")
                if new_lang != self.lang:
                    self.lang = new_lang
                    if self.icon is not None:
                        self.icon.update_menu()
                state = self.api("GET", "/api/state")
                self.apps = state.get("apps", [])
                snapshot = winlogic.snapshot(state)
                # the alert level comes from the backend (same rule as ntfy): an Apple TV that is merely
                # asleep does not raise a toast, and successive retries do not repeat it
                for label, name in winlogic.toasts(known, snapshot):
                    self.notify(self.t(label, name=name, email=name))
                known = snapshot
                self._apply_update(state.get("update"))
                self._sync_wake()
            except Exception as e:  # noqa: BLE001
                say(f"watch: {e}")
            for _ in range(60):
                if self.quitting:
                    return
                time.sleep(1)

    def _apply_update(self, info: dict | None) -> None:
        """New tvsign version, as announced by the backend in /api/state (one check a day, opt-out in Settings):
        one toast per version, then a menu item."""
        was = bool(self.update)
        self.update = info if info and info.get("version") else None
        if self.update and self.prefs.get("update_notified") != self.update["version"]:
            self.prefs["update_notified"] = self.update["version"]
            self._write_prefs()
            self.notify(self.t("update_toast", version=str(self.update["version"]).lstrip("v")))
        if self.icon is not None and was != bool(self.update):
            self.icon.update_menu()

    # ----- main -----
    def run(self) -> int:
        import webview

        self.start_server()
        threading.Thread(target=self._boot, daemon=True).start()
        threading.Thread(target=self.awake_loop, name="awake", daemon=True).start()
        self.start_tray()
        watch_event(SHOW_EVENT, lambda: threading.Thread(target=self.show, daemon=True).start())
        watch_event(QUIT_EVENT, self.quit)

        webview.settings["ALLOW_DOWNLOADS"] = True          # backup export
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        self.window = webview.create_window(
            APP_NAME, html="<html><body style='background:#1c1c1e'></body></html>",
            width=1100, height=760, min_size=(820, 560), hidden=True, background_color="#1C1C1E",
            text_select=True,
        )
        self.window.events.closing += self.on_closing
        self.window.events.before_show += self.hook_form
        storage = self.data / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        try:
            webview.start(func=None, gui="edgechromium", private_mode=False, storage_path=str(storage),
                          debug=bool(os.environ.get("TVSIGN_WEBVIEW_DEBUG")))
        except Exception as e:  # noqa: BLE001 - typically: the WebView2 runtime is missing
            if self.quitting:
                raise
            self.no_webview(e)
        # window destroyed (or Quit chosen without a window): stop everything
        self.quitting = True
        say("window closed, stopping")
        if self.server is not None:
            self.server.should_exit = True
        if self.server_thread is not None:
            # a renewal in progress gets the backend's drain time (SHUTDOWN_DRAIN_SECONDS), not a few seconds
            threading.Thread(target=self._announce_drain, daemon=True).start()
            self.server_thread.join(timeout=winlogic.drain_wait_seconds())
        self.finished.set()
        if self.prefs.get("wake_at"):
            self._clear_wake()                # a tvsign that was quit must not be restarted by its own wake-up
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass
        return 0

    def _announce_drain(self) -> None:
        time.sleep(3)
        if self.server_thread is not None and self.server_thread.is_alive() and not self.session_ending:
            self.notify(self.t("quitting_wait"))

    def no_webview(self, err: Exception) -> None:
        """The window cannot be created: stay alive in the tray, UI in the default browser, until Quit."""
        say(f"webview unavailable: {err!r}")
        self.window = None
        self.fallback = True
        if self.start_hidden:
            self.notify(self.t("webview_missing_toast"))
        else:
            if ask_yes_no(self.t("webview_missing", err=err)):
                webbrowser.open(WEBVIEW2_URL)
            threading.Thread(target=self.open_browser, daemon=True).start()
        self.stop.wait()

    def _boot(self) -> None:
        if not self.wait_ready():
            message_box(self.t("start_failed", err="no answer on " + self.base, log=self.log))
            os._exit(1)
        say(f"backend ready on {self.base}")
        if not self.start_hidden:
            self.show()
        threading.Thread(target=self.watch, name="watch", daemon=True).start()


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


def main() -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME, add_help=False)
    parser.add_argument("--tray", action="store_true")
    parser.add_argument("--quit", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args, _ = parser.parse_known_args()

    if args.quit:
        if signal_event(QUIT_EVENT):
            # a renewal in progress may take the drain time to finish
            for _ in range(int((winlogic.drain_wait_seconds() + 15) / 0.2)):
                if not instance_running():
                    break
                time.sleep(0.2)
        return 0

    mutex = acquire_instance()
    if mutex is None:
        if not args.tray:
            signal_event(SHOW_EVENT)
        return 0

    data = data_dir()
    data.mkdir(parents=True, exist_ok=True)
    log = setup_logging(data)
    token = secrets.token_urlsafe(32)
    port = free_port(args.port or int(os.environ.get("TVSIGN_PORT") or DEFAULT_PORT))

    # The backend reads its configuration from the environment at import time.
    os.environ.update({
        "TVSIGN_HOST": "127.0.0.1",
        "TVSIGN_PORT": str(port),
        "TVSIGN_TRUST_LOOPBACK": "1",
        "TVSIGN_LAUNCH_TOKEN": token,
        "TVSIGN_DATA": str(data),
    })
    os.environ.setdefault("TVSIGN_VERSION", app_version())
    if not FROZEN and str(TVSIGN_ROOT) not in sys.path:
        sys.path.insert(0, str(TVSIGN_ROOT))
    say(f"tvsign {os.environ['TVSIGN_VERSION']} starting (port {port}, data {data}, tray={args.tray})")

    try:
        return Desktop(port, token, data, log, start_hidden=args.tray).run()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        message_box(LABELS[windows_language()]["start_failed"].format(err=e, log=log))
        return 1


if __name__ == "__main__":
    sys.exit(main())
