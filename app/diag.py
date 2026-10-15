"""Environment diagnostics (onboarding and Settings › Diagnostics).

Read-only checks: no Apple sign-in, no pairing, no connection to an Apple TV.
Each check returns {id, status: ok|warn|error, detail, value}; the UI translates ids.
"""
import asyncio
import email.utils
import os
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request

from . import plume, settings
from .config import DATA_DIR, PLUMESIGN, VERSION, WINDOWS

DEFAULT_ANISETTE = "https://ani.sidestore.app"


def _check(id_: str, status: str, detail: str = "", value=None) -> dict:
    return {"id": id_, "status": status, "detail": detail, "value": value}


def _head(url: str) -> tuple[int | None, float | None, str]:
    """(code HTTP, date serveur, erreur). Toute réponse HTTP prouve que l'hôte est joignable."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": f"tvsign/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            code, headers = r.status, r.headers
    except urllib.error.HTTPError as e:
        code, headers = e.code, e.headers
    except Exception as e:  # noqa: BLE001
        return None, None, str(getattr(e, "reason", e))
    date = headers.get("Date") if headers else None
    try:
        ts = email.utils.parsedate_to_datetime(date).timestamp() if date else None
    except (ValueError, TypeError, OverflowError):
        ts = None       # Date illisible (portail captif, proxy, serveur maison) : la sonde reste valable
    return code, ts, ""


async def _plumesign() -> dict:
    if not PLUMESIGN.exists() and not shutil.which(str(PLUMESIGN)):
        return _check("plumesign", "error", f"not found: {PLUMESIGN} (TVSIGN_PLUMESIGN)")
    v = await plume.version()
    if not v:
        return _check("plumesign", "error", f"{PLUMESIGN} does not answer --version")
    return _check("plumesign", "ok", str(PLUMESIGN), v)


async def _mdns() -> dict:
    try:
        found = await plume.discover(4)
    except Exception as e:  # noqa: BLE001
        return _check("mdns", "error", str(e), 0)
    ours = sum(1 for d in found if d.get("udid"))
    pairable = sum(1 for d in found if d["kind"] == "pairable")
    detail = f"{len(found)} announcement(s), {ours} paired with tvsign, {pairable} waiting to be paired"
    return _check("mdns", "ok" if found else "warn", detail, {"total": len(found), "ours": ours, "pairable": pairable})


def _tcp(host: str, port: int = 443) -> str:
    try:
        socket.create_connection((host, port), timeout=8).close()
        return ""
    except OSError as e:
        return str(e)


async def _apple() -> tuple[dict, float | None]:
    """Portail développeur en HTTPS (certificat public, date serveur) + connexion TCP à GSA
    (gsa.apple.com est signé par une autorité Apple privée, que plumesign embarque)."""
    res, ts = await _reach("apple", "https://developerservices2.apple.com/")
    err = await asyncio.to_thread(_tcp, "gsa.apple.com")
    if err and res["status"] == "ok":
        res = _check("apple", "error", f"gsa.apple.com:443 : {err}")
    elif not err and res["status"] == "ok":
        res["detail"] += ", gsa.apple.com:443"
    return res, ts


async def _reach(id_: str, url: str) -> tuple[dict, float | None]:
    code, ts, err = await asyncio.to_thread(_head, url)
    if code is None:
        return _check(id_, "error", f"{url} : {err}"), None
    return _check(id_, "ok", f"{url} (HTTP {code})", code), ts


def _disk() -> dict:
    free = shutil.disk_usage(DATA_DIR).free
    gb = free / 1e9
    status = "ok" if gb >= 1 else "warn" if gb >= 0.2 else "error"
    return _check("disk", status, f"{gb:.1f} GB free in {DATA_DIR}", round(gb, 2))


def _loop() -> dict:
    if not WINDOWS:
        return _check("event_loop", "ok", type(asyncio.get_running_loop()).__name__)
    name = type(asyncio.get_running_loop()).__name__
    ok = "Proactor" in name
    return _check("event_loop", "ok" if ok else "error",
                  name + ("" if ok else ": no subprocesses, start with \"python -m app\""))


async def run_all() -> dict:
    anisette = settings.anisette_url() or DEFAULT_ANISETTE
    started = time.time()

    async def safe(id_: str, coro, pair: bool = False):
        # une sonde qui lève ne doit pas faire échouer tout le diagnostic : elle devient une erreur affichée
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            failed = _check(id_, "error", f"{type(e).__name__}: {e}")
            return (failed, None) if pair else failed

    plumesign_, mdns, (apple, apple_date), (anis, _), (github, gh_date) = await asyncio.gather(
        safe("plumesign", _plumesign()), safe("mdns", _mdns()),
        safe("apple", _apple(), True),
        safe("anisette", _reach("anisette", anisette), True),
        safe("github", _reach("github", "https://api.github.com/"), True),
    )
    server_date = apple_date or gh_date
    if server_date is None:
        clock = _check("clock", "warn", "no server date available to compare with")
    else:
        skew = time.time() - server_date
        status = "ok" if abs(skew) < 120 else "error"
        clock = _check("clock", status, f"{skew:+.0f} s from Apple's clock", round(skew))
    checks = [plumesign_, mdns, apple, anis, github, clock, _disk(), _loop()]
    return {
        "checks": checks,
        "ok": all(c["status"] != "error" for c in checks),
        "version": VERSION,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "data_dir": str(DATA_DIR),
        "pid": os.getpid(),
        "duration": round(time.time() - started, 1),
    }
