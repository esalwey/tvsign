"""Nouvelle version de tvsign disponible ? (GitHub releases, les deux éditions.)

Une seule requête GET anonyme vers api.github.com (aucune donnée sur l'installation, seulement
l'en-tête User-Agent « tvsign/<version> » déjà envoyé pour les releases des apps). Le résultat est
gardé 6 h en mémoire et en base, pour qu'un redémarrage ne refasse pas l'appel. Désactivable :
réglage « update_check » ou TVSIGN_UPDATE_CHECK=off. Un échec réseau ne gêne jamais le reste.
"""
import asyncio
import json
import re
import time

from . import ipa, settings
from .config import UPDATE_REPO, VERSION, WINDOWS

CACHE_SECONDS = 6 * 3600
RETRY_SECONDS = 600            # après un échec : pas avant 10 min (quota GitHub : 60 requêtes/h)
FORCE_MIN_SECONDS = 60         # « vérifier maintenant » ne peut pas marteler GitHub
_KEY = "update_cache"

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")

_STATE: dict = {}              # dernier résultat : {latest, url, checked_at, error, ok_at}
_LOCK = asyncio.Lock()


def parse_version(text: str | None):
    """(majeur, mineur, correctif, pré-version) ou None si ce n'est pas une version semver
    (« edge-abc1234 » des images de test : jamais comparée)."""
    m = _SEMVER.match((text or "").strip())
    if not m:
        return None
    pre = m.group(4)
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), pre


def _pre_key(pre: str | None):
    # semver : une version finale passe avant ses pré-versions ; identifiants numériques < texte
    if pre is None:
        return (1,)
    parts = [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")]
    return (0, parts)


def is_newer(latest: str | None, current: str | None) -> bool:
    """True si `latest` est strictement plus récente que `current`. Une version illisible
    (build « edge », tag exotique) ne déclenche jamais d'annonce."""
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False
    return (a[:3], _pre_key(a[3])) > (b[:3], _pre_key(b[3]))


def enabled() -> bool:
    return settings.update_check_enabled()


def _fetch() -> dict:
    """Dernière release publiée (hors pré-versions et brouillons, ce que renvoie /releases/latest)."""
    rel = ipa._get_json(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest")
    url = rel.get("html_url") or ""
    if not url.startswith("https://github.com/"):
        url = f"https://github.com/{UPDATE_REPO}/releases/latest"      # le lien affiché ne sort jamais de GitHub
    return {"latest": str(rel.get("tag_name") or "")[:64] or None, "url": url}


def _load() -> None:
    if _STATE:
        return
    try:
        _STATE.update(json.loads(settings.raw_get(_KEY) or "{}"))
    except (ValueError, TypeError):
        pass


def _save() -> None:
    try:
        settings.raw_set(_KEY, json.dumps({k: _STATE.get(k) for k in ("latest", "url", "checked_at", "ok_at", "error")}))
    except Exception:  # noqa: BLE001 - le cache est accessoire
        pass


def _public(now: float) -> dict:
    latest = _STATE.get("latest")
    on = enabled()
    return {
        "enabled": on,
        "current": VERSION,
        "latest": latest if on else None,
        "newer": bool(on and latest and is_newer(latest, VERSION)),
        "url": _STATE.get("url") if on else None,
        "checked_at": _STATE.get("ok_at") if on else None,
        "error": _STATE.get("error") if on else None,
        "edition": "windows" if WINDOWS else "docker",
        "repo": UPDATE_REPO,
    }


async def check(force: bool = False) -> dict:
    """État de la vérification, en interrogeant GitHub seulement si le cache est périmé."""
    now = time.time()
    async with _LOCK:
        _load()
        if enabled():
            tried = _STATE.get("checked_at") or 0
            wait = FORCE_MIN_SECONDS if force else (CACHE_SECONDS if not _STATE.get("error") else RETRY_SECONDS)
            if now - tried >= wait:
                _STATE["checked_at"] = now
                try:
                    found = await asyncio.to_thread(_fetch)
                    _STATE.update(found, ok_at=now, error=None)
                except Exception as e:  # noqa: BLE001 - hors ligne, quota GitHub, dépôt privé…
                    _STATE["error"] = str(getattr(e, "reason", e))[:200]
                _save()
        return _public(now)
