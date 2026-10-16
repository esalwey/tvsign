"""tvsign for Windows: native window (WebView2) + tray icon + in-process backend.

    tvsign.exe            open the window (or bring the running instance to front)
    tvsign.exe --tray     start in the notification area only (used by autostart)
    tvsign.exe --quit     ask the running instance to exit (used by the installer)

The backend (app.main) runs in a background thread on 127.0.0.1, port 8765 when free, otherwise a
random free port. The window signs in with a per-launch token (TVSIGN_LAUNCH_TOKEN); requests
from loopback are trusted (TVSIGN_TRUST_LOOPBACK) until the user sets a password.
Closing the window hides it; "Quit" in the tray menu stops everything.
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
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

APP_NAME = "tvsign"
MUTEX_NAME = "tvsign-desktop-instance"        # also AppMutex in the Inno Setup script
SHOW_EVENT = "tvsign-desktop-show"
QUIT_EVENT = "tvsign-desktop-quit"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
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
    try:
        if log.exists() and log.stat().st_size > 2 * 1024 * 1024:
            os.replace(log, logs / "tvsign.log.1")
    except OSError:
        pass
    stream = open(log, "a", encoding="utf-8", buffering=1, errors="replace")
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
    user32.MessageBoxW(None, text, title, 0x10)   # MB_ICONERROR


# ---------- autostart (HKCU Run) ----------

def autostart_command() -> str:
    if FROZEN:
        return f'"{sys.executable}" --tray'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" "{Path(__file__).resolve()}" --tray'


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ---------- labels (follow the backend "language" setting) ----------

LABELS = {
    "fr": {
        "open": "Ouvrir",
        "renew": "Renouveler maintenant",
        "autostart": "Démarrer avec Windows",
        "quit": "Quitter",
        "tooltip": "tvsign",
        "hidden": "tvsign continue en arrière-plan pour renouveler vos apps. "
                  "Clic droit sur l'icône pour quitter.",
        "renew_started": "Renouvellement lancé : {names}.",
        "renew_none": "Aucune app à renouveler.",
        "renew_failed": "Impossible de lancer le renouvellement : {err}",
        "app_failed": "{name} : le renouvellement a échoué.",
        "app_expired": "{name} : le profil a expiré. Ouvrez tvsign.",
        "account_lost": "Compte Apple {email} : reconnexion nécessaire.",
        "start_failed": "tvsign n'a pas pu démarrer son serveur local.\n\n{err}\n\nJournal : {log}",
    },
    "en": {
        "open": "Open",
        "renew": "Renew now",
        "autostart": "Start with Windows",
        "quit": "Quit",
        "tooltip": "tvsign",
        "hidden": "tvsign keeps running in the background to renew your apps. "
                  "Right-click the icon to quit.",
        "renew_started": "Renewal started: {names}.",
        "renew_none": "No app to renew.",
        "renew_failed": "Could not start the renewal: {err}",
        "app_failed": "{name}: renewal failed.",
        "app_expired": "{name}: the profile has expired. Open tvsign.",
        "account_lost": "Apple account {email}: sign in again.",
        "start_failed": "tvsign could not start its local server.\n\n{err}\n\nLog: {log}",
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
        self.window = None
        self.icon = None
        self.server = None
        self.server_thread = None
        self.loaded = False            # window navigated to the launch URL yet
        self.ready = threading.Event()
        self.prefs_path = data / "desktop.json"
        self.prefs = self._read_prefs()
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
        logging.getLogger("uvicorn.access").addFilter(_RedactToken())

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
    def show(self) -> None:
        if self.quitting or self.window is None:
            return
        with self._lock:
            if not self.loaded:
                self.ready.wait(60)
                self.window.load_url(f"{self.base}/auth/launch?token={self.token}")
                self.loaded = True
        self.window.show()
        self.window.restore()
        try:
            self.window.on_top = True
            self.window.on_top = False
        except Exception:  # noqa: BLE001
            pass

    def on_closing(self):
        if self.quitting:
            return True
        threading.Thread(target=self._hide, daemon=True).start()
        return False                     # cancel the close: hide to the tray instead

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
            set_autostart(not autostart_enabled())
        except OSError as e:
            say(f"autostart: {e}")
        if self.icon is not None:
            self.icon.update_menu()

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

        item = pystray.MenuItem
        menu = pystray.Menu(
            item(lambda _: self.t("open"), lambda: threading.Thread(target=self.show, daemon=True).start(),
                 default=True),
            item(lambda _: self.t("renew"), lambda: threading.Thread(target=self.renew_now, daemon=True).start()),
            item(lambda _: self.t("autostart"), self.toggle_autostart, checked=lambda _: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            item(lambda _: self.t("quit"), self.quit),
        )
        self.icon = pystray.Icon(APP_NAME, image, APP_NAME, menu)
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    # ----- background watcher: language + failure notifications -----
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
                snapshot = {}
                now = state.get("now") or time.time()
                for a in state.get("apps", []):
                    status = "expired" if a.get("expired") else a.get("status")
                    exp = a.get("expires_at")
                    if (status == "error" and (a.get("fail_kind") == "transient" or a.get("needs_action") == "session")
                            and exp and exp - now > 24 * 3600):
                        # panne banale (Apple TV en veille, app ouverte, anisette, reconnexion à faire) loin de l'expiration :
                        # même règle que les notifications du serveur, pas de toast avant le niveau 24 h
                        status = "error-quiet"
                    snapshot[f"app:{a['id']}"] = (status, a["name"])
                for acc in state.get("accounts", []):
                    snapshot[f"acc:{acc['email']}"] = ("lost" if acc.get("ok") == 0 else "ok", acc["email"])
                if known is not None:
                    for key, (status, name) in snapshot.items():
                        before = known.get(key, (None, None))[0]
                        if status == before:
                            continue
                        if status == "error":
                            self.notify(self.t("app_failed", name=name))
                        elif status == "expired":
                            self.notify(self.t("app_expired", name=name))
                        elif status == "lost":
                            self.notify(self.t("account_lost", email=name))
                known = snapshot
            except Exception as e:  # noqa: BLE001
                say(f"watch: {e}")
            for _ in range(60):
                if self.quitting:
                    return
                time.sleep(1)

    # ----- main -----
    def run(self) -> int:
        import webview

        self.start_server()
        threading.Thread(target=self._boot, daemon=True).start()
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
        storage = self.data / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        webview.start(func=None, gui="edgechromium", private_mode=False, storage_path=str(storage),
                      debug=bool(os.environ.get("TVSIGN_WEBVIEW_DEBUG")))
        # window destroyed: stop everything
        self.quitting = True
        say("window closed, stopping")
        if self.server is not None:
            self.server.should_exit = True
        if self.server_thread is not None:
            self.server_thread.join(timeout=15)
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass
        return 0

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
            for _ in range(100):
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
