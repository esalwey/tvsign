"""Nouvelle version de tvsign : une recherche par jour dans les releases GitHub du projet, désactivable
(réglage « update_check » ou TVSIGN_UPDATE_CHECK=off). Aucune donnée n'est envoyée en dehors de l'en-tête
User-Agent « tvsign/<version> » ; rien n'est téléchargé ni installé : l'interface indique seulement
quoi faire selon l'édition (Windows : l'installeur ; Docker : docker compose pull)."""
import asyncio
import re
import time

from . import db, ipa, settings
from .config import EDITION, UPDATE_REPO, VERSION

CHECK_EVERY = 24 * 3600           # une recherche par jour
RETRY_AFTER = 3600                # après un échec (hors ligne, quota GitHub) : une heure

STATUS: dict = {"latest": None, "url": None, "checked_at": None, "error": None}
_last = 0.0


def parse(version) -> tuple | None:
    """« v1.2.3 » -> (1, 2, 3, 1) ; une préversion (1.2.3-rc1) compte avant la version finale : (1, 2, 3, 0)."""
    m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(-[\w.+-]*)?$", str(version or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), 0 if m.group(4) else 1


def newer(latest, current) -> bool:
    a, b = parse(latest), parse(current)
    return bool(a and b and a > b)


def enabled() -> bool:
    return settings.update_check()


async def check(force: bool = False) -> None:
    """Interroge GitHub si la dernière recherche date de plus d'un jour (ou si force)."""
    global _last
    if not enabled() or "dev" in VERSION:
        return
    now = time.time()
    if not force and now - _last < CHECK_EVERY:
        return
    _last = now
    try:
        data = await asyncio.to_thread(ipa._get_json, f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest")
        tag = str(data.get("tag_name") or "")
        if data.get("draft") or data.get("prerelease") or not parse(tag):
            raise ValueError("aucune release publique")
    except ipa.SourceUnavailable as e:
        if e.code != "gh_not_found":
            _failed(e, now)
            return
        STATUS.update(latest=None, url=None, checked_at=now, error=None)    # aucune release publiée : rien à signaler
        return
    except Exception as e:  # noqa: BLE001 - accessoire : jamais bloquant, on réessaiera plus tard
        _failed(e, now)
        return
    url = str(data.get("html_url") or "")
    # lien affiché tel quel dans l'interface : uniquement une page de GitHub
    STATUS.update(latest=tag.lstrip("v"), url=url if url.startswith("https://github.com/") else None,
                  checked_at=now, error=None)
    if newer(STATUS["latest"], VERSION) and settings.raw_get("update_seen") != STATUS["latest"]:
        settings.raw_set("update_seen", STATUS["latest"])
        db.log(f"tvsign {STATUS['latest']} est disponible (version installée : {VERSION})")


def _failed(e: Exception, now: float) -> None:
    """Accessoire : jamais bloquant ; une ligne de journal par épisode, nouvel essai dans une heure."""
    global _last
    _last = now - CHECK_EVERY + RETRY_AFTER
    if STATUS["error"] is None:
        db.log(f"recherche de mise à jour de tvsign impossible ({e})", "warn")
    STATUS["error"] = str(e)


def available() -> dict | None:
    """Mise à jour à proposer dans l'interface, ou None (à jour, recherche coupée, déjà écartée)."""
    if not enabled():
        return None
    latest = STATUS["latest"]
    if not latest or not newer(latest, VERSION) or settings.raw_get("update_dismissed") == latest:
        return None
    return {"version": latest, "url": STATUS["url"], "edition": EDITION}


def dismiss() -> None:
    """L'utilisateur écarte la bannière pour la version proposée (la suivante la fera réapparaître)."""
    if STATUS["latest"]:
        settings.raw_set("update_dismissed", STATUS["latest"])


def summary() -> dict:
    return {"enabled": enabled(), "current": VERSION, "latest": STATUS["latest"], "url": STATUS["url"],
            "checked_at": STATUS["checked_at"], "error": STATUS["error"], "edition": EDITION,
            "available": newer(STATUS["latest"], VERSION)}
