"""Environment diagnostics (onboarding and Settings › Diagnostics).

Read-only checks: no Apple sign-in, no pairing, no connection to an Apple TV.
Each check returns {id, status: ok|warn|error, detail, value}; the UI translates ids.
"""
import asyncio
import email.utils
import json
import os
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request

from . import plume, settings
from .config import DATA_DIR, PLUMESIGN, VERSION, WINDOWS

DEFAULT_ANISETTE = settings.DEFAULT_ANISETTE


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


async def _mdns() -> dict:
    try:
        found = await plume.discover(4)
    except Exception as e:  # noqa: BLE001
        return _check("mdns", "error", str(e), 0)
    ours = sum(1 for d in found if d.get("udid"))
    pairable = sum(1 for d in found if d["kind"] == "pairable")
    detail = f"{len(found)} annonce(s), {ours} appairée(s) avec tvsign, {pairable} en attente d'appairage"
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
    if not isinstance(info, str) or not info.strip():
        return None, "réponse inattendue (ce n'est pas un serveur anisette v3 ?)"
    return data, ""


def _windows(data: dict | None) -> bool:
    """Le serveur annonce des données Windows. Un client_info qui n'est pas du texte (réponse JSON étrange)
    ne l'est pas : jamais d'exception ici, le diagnostic doit toujours répondre."""
    info = data.get("client_info") if isinstance(data, dict) else None
    return isinstance(info, str) and "windows" in info.lower()


async def _anisette() -> dict:
    """Serveur anisette : joignable, et il fournit des données Windows (les seules qu'Apple accepte).
    Avec le serveur par défaut, le serveur de repli prend le relais quand il ne répond pas."""
    found = []
    for url in settings.anisette_servers():
        data, err = await asyncio.to_thread(_client_info, url)
        found.append((url, data, err))
        if _windows(data):
            break
    url, data, err = found[-1]
    if _windows(data):
        if len(found) > 1:
            first_url, first_data, first_err = found[0]
            why = first_err or f"répond « {first_data['client_info']} »"
            return _check("anisette", "warn", f"{first_url} inutilisable ({why}) : repli sur {url}", url)
        return _check("anisette", "ok", f"{url} : {data['client_info']}", url)
    if data:
        # plumesign refuse un tel serveur sans rien envoyer à Apple : les renouvellements échoueront
        return _check("anisette", "error",
                      f"{url} répond « {data['client_info']} » : Apple n'accepte que des données Windows, "
                      "plumesign n'utilisera pas ce serveur (laisser le réglage vide pour le serveur par défaut)", url)
    return _check("anisette", "error", " ; ".join(f"{u} : {e or 'données non Windows'}" for u, _, e in found), url)


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
    started = time.time()
    results = await asyncio.gather(
        _plumesign(), _mdns(),
        _apple(),
        _anisette(),
        _reach("github", "https://api.github.com/"),
    )
    plumesign_, mdns, (apple, apple_date), anis, (github, gh_date) = results
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
