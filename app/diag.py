"""Environment diagnostics (onboarding and Settings › Diagnostics).

Read-only checks: no Apple sign-in, no pairing, no connection to an Apple TV.
Each check returns {id, status: ok|warn|error, detail, value}; the UI translates ids.
"""
import asyncio
import email.utils
import ipaddress
import os
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import db, devices, plume, settings
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
    ts = email.utils.parsedate_to_datetime(date).timestamp() if date else None
    return code, ts, ""


async def _plumesign() -> dict:
    if not PLUMESIGN.exists() and not shutil.which(str(PLUMESIGN)):
        return _check("plumesign", "error", f"introuvable : {PLUMESIGN} (TVSIGN_PLUMESIGN)")
    v = await plume.version()
    if not v:
        return _check("plumesign", "error", f"{PLUMESIGN} ne répond pas à --version")
    return _check("plumesign", "ok", str(PLUMESIGN), v)


def _bridge_network() -> bool:
    """Conteneur Docker sur le réseau « bridge » (seule une interface eth0 en 172.16/12) : le multicast
    de la maison n'y arrive pas, il faut network_mode: host."""
    if WINDOWS or not Path("/.dockerenv").exists():
        return False
    try:
        names = [n.name for n in Path("/sys/class/net").iterdir() if n.name != "lo"]
        addrs = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        return False
    private = ipaddress.ip_network("172.16.0.0/12")
    return len(names) == 1 and bool(addrs) and all(ipaddress.ip_address(a) in private for a in addrs)


async def _direct_answers() -> int:
    """Apple TV déjà connues qui répondent à une requête mDNS envoyée directement à leur adresse :
    le multicast est filtré mais tvsign les joint quand même."""
    async def one(ip: str) -> int:
        try:
            return len(await plume.discover(2, ip=ip))
        except Exception:  # noqa: BLE001 - plumesign ancien, adresse injoignable…
            return 0
    ips = set()
    for d in db.rows("SELECT host_hint, last_ip FROM devices"):
        ip = await devices.resolve_host(d["host_hint"]) or d["last_ip"]
        if ip:
            ips.add(ip)
    return sum(await asyncio.gather(*(one(ip) for ip in ips))) if ips else 0


async def _mdns() -> dict:
    try:
        found = await plume.discover(4)
    except Exception as e:  # noqa: BLE001
        return _check("mdns", "error", str(e), 0)
    ours = sum(1 for d in found if d.get("udid"))
    pairable = sum(1 for d in found if d["kind"] == "pairable")
    detail = f"{len(found)} annonce(s), {ours} appairée(s) avec tvsign, {pairable} en attente d'appairage"
    value = {"total": len(found), "ours": ours, "pairable": pairable, "bridge": False, "direct": 0}
    if found:
        return _check("mdns", "ok", detail, value)
    value["bridge"] = _bridge_network()
    value["direct"] = await _direct_answers()
    if value["direct"]:
        # le multicast ne passe pas, mais l'adresse connue de la TV répond : les renouvellements fonctionnent
        return _check("mdns", "warn", f"{detail} ; {value['direct']} Apple TV répond(ent) en direct à son adresse", value)
    return _check("mdns", "warn", detail, value)


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
    return _check("disk", status, f"{gb:.1f} Go libres dans {DATA_DIR}", round(gb, 2))


def _loop() -> dict:
    if not WINDOWS:
        return _check("event_loop", "ok", type(asyncio.get_running_loop()).__name__)
    name = type(asyncio.get_running_loop()).__name__
    ok = "Proactor" in name
    return _check("event_loop", "ok" if ok else "error",
                  name + ("" if ok else " : sous-processus impossibles, lancer avec « python -m app »"))


async def run_all() -> dict:
    anisette = settings.anisette_url() or DEFAULT_ANISETTE
    started = time.time()
    results = await asyncio.gather(
        _plumesign(), _mdns(),
        _apple(),
        _reach("anisette", anisette),
        _reach("github", "https://api.github.com/"),
    )
    plumesign_, mdns, (apple, apple_date), (anis, _), (github, gh_date) = results
    server_date = apple_date or gh_date
    if server_date is None:
        clock = _check("clock", "warn", "aucune date serveur disponible pour comparer")
    else:
        skew = time.time() - server_date
        status = "ok" if abs(skew) < 120 else "error"
        clock = _check("clock", status, f"écart {skew:+.0f} s avec l'heure d'Apple", round(skew))
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
