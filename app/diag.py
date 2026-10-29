"""Environment diagnostics (onboarding and Settings › Diagnostics).

Read-only checks: no Apple sign-in, no pairing, no connection to an Apple TV.
Each check returns {id, status: ok|warn|error, detail, value, code}; the UI translates ids, and
the code of the case that applies (the English detail stays as a fallback and in the support bundle).
"""
import asyncio
import email.utils
import ipaddress
import json
import os
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import db, devices, plume, settings
from .config import DATA_DIR, PASSWORD_FILE_ERROR, PLUMESIGN, VERSION, WINDOWS

DEFAULT_ANISETTE = settings.DEFAULT_ANISETTE


def _check(id_: str, status: str, detail: str = "", value=None, code: str | None = None) -> dict:
    return {"id": id_, "status": status, "detail": detail, "value": value, "code": code}


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
        return _check("plumesign", "error", f"not found: {PLUMESIGN} (TVSIGN_PLUMESIGN)", code="missing")
    v = await plume.version()
    if not v:
        return _check("plumesign", "error", f"{PLUMESIGN} does not answer --version", code="no_answer")
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
    detail = f"{len(found)} announcement(s), {ours} paired with tvsign, {pairable} waiting to be paired"
    value = {"total": len(found), "ours": ours, "pairable": pairable, "bridge": False, "direct": 0}
    if found:
        return _check("mdns", "ok", detail, value, code="found")
    value["bridge"] = _bridge_network()
    value["direct"] = await _direct_answers()
    if value["direct"]:
        # multicast does not get through, but the TV's known address answers: renewals work
        return _check("mdns", "warn", f"{detail}; {value['direct']} Apple TV(s) answering directly at their address", value,
                      code="none")
    return _check("mdns", "warn", detail, value, code="none")


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


def _client_info(url: str) -> tuple[dict | None, str]:
    """GET <serveur>/v3/client_info : (réponse JSON, erreur). Un serveur anisette v3 répond le client_info
    et le user_agent qu'il faut présenter à Apple."""
    req = urllib.request.Request(url.rstrip("/") + "/v3/client_info", headers={"User-Agent": f"tvsign/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read(65536).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, str(getattr(e, "reason", e))
    info = data.get("client_info") if isinstance(data, dict) else None
    agent = data.get("user_agent") if isinstance(data, dict) else None
    # plumesign needs both: a bare HTTP answer does not prove that this is an anisette-v3 server
    if not isinstance(info, str) or not info.strip() or not isinstance(agent, str) or not agent.strip():
        return None, "réponse inattendue (ce n'est pas un serveur anisette v3 ?)"
    return data, ""


def _server_line(url: str, data: dict | None, err: str, seconds: float | None = None) -> dict:
    """Ce qu'on sait d'un serveur : joignable ou non, le client info / user agent qu'il annonce et son temps de
    réponse. Un texte étrange n'est jamais une exception ici, le diagnostic doit toujours répondre."""
    ms = None if seconds is None else round(seconds * 1000)
    info = data.get("client_info") if isinstance(data, dict) else None
    if not isinstance(info, str) or not info.strip():
        return {"url": url, "reachable": False, "client_info": None, "user_agent": None, "ms": ms,
                "error": err or "unreachable"}
    user_agent = data.get("user_agent")
    return {"url": url, "reachable": True, "client_info": info.strip(), "ms": ms,
            "user_agent": user_agent.strip() if isinstance(user_agent, str) else None, "error": ""}


async def _anisette() -> dict:
    """Serveurs anisette : chacun est interrogé (client info et user agent annoncés, temps de réponse, ou la raison
    pour laquelle il ne répond pas). N'importe quel serveur anisette v3 qui répond est utilisable, quelle que soit
    la plateforme qu'il annonce : seul Apple dit si les données conviennent, et plumesign passe au serveur suivant
    quand l'un échoue ou qu'Apple refuse ses données."""
    urls = settings.anisette_servers()

    async def probe(url: str) -> dict:
        started = time.time()
        data, err = await asyncio.to_thread(_client_info, url)
        return _server_line(url, data, err, time.time() - started)

    servers = list(await asyncio.gather(*(probe(url) for url in urls)))
    last = plume.last_server()       # le dernier serveur dont Apple a accepté les données (anisette/last-server.json)
    for s in servers:
        s["last_accepted"] = bool(last and settings.same_server(last["server"], s["url"]))
    forced = {"client_info": settings.anisette_client_info() or None,
              "user_agent": settings.anisette_user_agent() or None}
    value = {"servers": servers, "last_good": last, "override": forced if any(forced.values()) else None}

    def line(s: dict) -> str:
        ms = f" ({s['ms']} ms)" if s["reachable"] and s["ms"] is not None else ""
        return f"{s['url']} : {s['client_info'] if s['reachable'] else s['error']}{ms}"

    extra = " ; client identity forced by the settings" if value["override"] else ""
    up = [s for s in servers if s["reachable"]]
    if not up:
        return _check("anisette", "error", "no anisette server answers: " + " ; ".join(line(s) for s in servers) + extra,
                      value)
    first = servers[0]
    if first["reachable"]:
        detail = line(first)
        if servers[1:]:
            detail += " ; behind it: " + " ; ".join(line(s) for s in servers[1:])
        return _check("anisette", "ok", detail + extra, value)
    return _check("anisette", "warn",
                  f"{first['url']} does not answer ({first['error']}): {up[0]['url']} takes over ({up[0]['client_info']})"
                  + extra, value)


def _password_file() -> dict | None:
    """TVSIGN_PASSWORD_FILE défini mais inutilisable : le mot de passe prévu n'est pas appliqué."""
    if not PASSWORD_FILE_ERROR:
        return None
    return _check("password_file", "error", PASSWORD_FILE_ERROR)


def _disk() -> dict:
    free = shutil.disk_usage(DATA_DIR).free
    gb = free / 1e9
    status = "ok" if gb >= 1 else "warn" if gb >= 0.2 else "error"
    return _check("disk", status, f"{gb:.1f} GB free in {DATA_DIR}", round(gb, 2), code="free")


def _loop() -> dict:
    if not WINDOWS:
        return _check("event_loop", "ok", type(asyncio.get_running_loop()).__name__)
    name = type(asyncio.get_running_loop()).__name__
    ok = "Proactor" in name
    return _check("event_loop", "ok" if ok else "error",
                  name + ("" if ok else ": no subprocesses, start with \"python -m app\""),
                  code=None if ok else "no_subprocess")


async def run_all() -> dict:
    started = time.time()

    async def safe(id_: str, coro, pair: bool = False):
        # une sonde qui lève ne doit pas faire échouer tout le diagnostic : elle devient une erreur affichée
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            failed = _check(id_, "error", f"{type(e).__name__}: {e}")
            return (failed, None) if pair else failed

    plumesign_, mdns, (apple, apple_date), anis, (github, gh_date) = await asyncio.gather(
        safe("plumesign", _plumesign()), safe("mdns", _mdns()),
        safe("apple", _apple(), True),
        safe("anisette", _anisette()),
        safe("github", _reach("github", "https://api.github.com/"), True),
    )
    server_date = apple_date or gh_date
    if server_date is None:
        clock = _check("clock", "warn", "no server date available to compare with", code="no_date")
    else:
        skew = time.time() - server_date
        status = "ok" if abs(skew) < 120 else "error"
        clock = _check("clock", status, f"{skew:+.0f} s from Apple's clock", round(skew), code="skew")
    checks = [plumesign_, mdns, apple, anis, github, clock, _disk(), _loop()]
    pw = _password_file()
    if pw:
        checks.insert(0, pw)
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
