"""Renouvellement : mise à jour de la source, signature, installation, et boucle de planification."""
import asyncio
import hashlib
import json
import os
import re
import shutil
import time

from . import backup, db, devices, ipa, notifier, plume, proc, selfupdate, settings, util
from .config import (ACCOUNT_CHECK_HOURS, APP_WAIT_MINUTES, APPLE_BACKOFF_MINUTES, CHECK_INTERVAL, IPA_DIR,
                     RELEASE_CHECK_MINUTES, RENEW_TIMEOUT_MINUTES, RETRY_MINUTES, UPDATE_WAIT_MINUTES)

# Arrêt du service en cours : plus aucun renouvellement ne démarre, ceux en cours peuvent finir.
STOPPING = False

_RUNNING: set[int] = set()
# Étape du renouvellement en cours, par app, pour l'interface :
# {"step": checking|signing|uploading|installing|waiting_app, "since": ts, ...}
PHASE: dict[int, dict] = {}
_RERUN: set[int] = set()                # destination changée pendant un renouvellement : on relance après
_EXPIRED_LOGGED: set[int] = set()       # apps dont l'expiration est déjà au journal (une fois par épisode)
_NO_MATCH_LOGGED: set[int] = set()      # apps dont l'absence de release correspondante est déjà au journal
_RELEASE_ERR: dict[int, str] = {}       # apps dont la recherche de version échoue : message (une ligne de journal par épisode)
_LINK_CHECKED: dict[int, float] = {}    # liens sans marqueur de version : dernière comparaison d'empreinte
_OS_REFRESHED: dict[int, float] = {}    # Apple TV dont la version de tvOS a été relue pour lever un blocage
_OUTCOME: dict[int, tuple[str, str | None]] = {}   # résultat du dernier essai par app (historique des renouvellements)
_STALL_ALERTED = False                  # blocage de la boucle déjà signalé (une alerte par épisode)

# Ce que l'utilisateur remet à zéro en agissant (destination, source, renouvellement manuel…) : plus d'attente,
# ni d'action requise, ni de délai croissant.
CLEAR_BLOCK = dict(next_attempt_at=None, needs_action=None, fail_count=0, fail_kind=None)

# Déclencheurs d'une mise à jour facultative : l'app installée reste valable, un échec ne la fait pas passer en erreur.
UPDATE_TRIGGERS = ("nouvelle version", "mise à jour", "nouvelle IPA", "nouvelle source", "retour arrière")
# Renouvellements qui re-signent toujours et appliquent la version demandée même si la mise à jour auto est coupée.
FORCE_SIGN = ("manuel", "nouvelle destination", "nouvelle IPA", "retour arrière")
FORCE_UPDATE = ("mise à jour", "nouvelle source")

# État de la boucle, exposé dans /api/state pour l'interface.
HEALTH: dict = {
    "started_at": time.time(),
    "last_loop_at": None,               # dernier passage de la boucle
    "next_check_at": None,              # prochain passage prévu
    "loop_error": None,                 # dernière erreur inattendue de la boucle
    "last_account_check_at": None,
    "last_release_check_at": None,
    "notify_error": None,               # dernier échec d'envoi à une destination de notification
    "restarts": 0,                      # relances de la boucle par le superviseur
    "release_check_error": None,        # dernier échec de la recherche de nouvelles versions (GitHub)
}
TASK: asyncio.Task | None = None        # tâche de la boucle supervisée (voir start())
_BG: dict[str, asyncio.Task] = {}       # contrôles de fond : comptes, nouvelles versions


def app_dir(app_id: int):
    d = IPA_DIR / str(app_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fmt(ts: float) -> str:
    return notifier.fmt_time(ts)


# ---------- notifications ----------

async def notify(message: str, priority: str = "default", event: str = "fail", **extra) -> bool:
    """Envoi aux destinations abonnées à `event` (voir notifier.py), hors de la boucle d'événements.
    True si la notification est partie : l'appelant ne doit la considérer envoyée qu'alors."""
    sent = await notifier.dispatch(event, message, priority, **extra)
    HEALTH["notify_error"] = notifier.last_error()
    return sent


# Niveaux d'alerte croissants : on ne renotifie que si la situation s'aggrave.
_LEVELS = ["fail", "24h", "6h", "expired"]


def _level(app: dict, now: float) -> str:
    exp = app["expires_at"]
    if not exp:
        return "fail"
    left = exp - now
    if left <= 0:
        return "expired"
    if left < 6 * 3600:
        return "6h"
    if left < 24 * 3600:
        return "24h"
    return "fail"


async def alert(app: dict, reason: str, now: float, quiet_fail: bool = False) -> None:
    """quiet_fail : pas de notification au niveau « fail » (panne banale, ex. TV en veille) ;
    les niveaux 24h / 6h / expired restent notifiés."""
    lvl = _level(app, now)
    prev = app.get("notified_level")
    if prev in _LEVELS and _LEVELS.index(lvl) <= _LEVELS.index(prev):
        return  # déjà notifié à ce niveau
    if lvl == "fail" and quiet_fail:
        return
    name = app["name"]
    if lvl == "fail":
        sent = await notify(notifier.text("renew_failed", name=name, reason=reason), "default", "fail", app=name)
    elif lvl == "expired":
        sent = await notify(notifier.text("expired", name=name, reason=reason), "urgent", "expiry", app=name)
    else:
        sent = await notify(notifier.text("expiring", name=name, h=lvl[:-1], reason=reason), "high", "expiry",
                            app=name)
    if sent:
        # niveau mémorisé seulement si la notification est partie : sinon on réessaiera au prochain échec
        db.update("apps", app["id"], notified_level=lvl)


# ---------- source ----------

def _unchanged(app: dict, tag: str, key: str | None) -> bool:
    """La source publie-t-elle exactement ce qui est déjà téléchargé ? Le tag seul ne suffit pas :
    un tag fixe (nightly, latest), un lien dont le fichier est remplacé ou une IPA republiée sous le
    même tag changent de clé (empreinte, ETag…). Sans clé connue, seul le tag compte."""
    return tag == app["source_tag"] and (key is None or app.get("source_key") in (None, key))


def _rejected(app: dict, tag: str, key: str | None) -> bool:
    """Cette release (ce contenu) a-t-elle déjà été refusée ? Une clé inconnue (NULL) n'est pas un joker :
    un refus sans clé ne doit pas bloquer tous les fichiers futurs du même lien."""
    return tag == app.get("bad_tag") and app.get("bad_key") == key


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def pending_update(app: dict) -> bool:
    """Une IPA plus récente que celle installée attend-elle d'être installée ? (téléchargée, mais le dernier
    essai a échoué ou n'a pas encore eu lieu). Faux tant que l'empreinte de l'installée est inconnue."""
    return bool(app.get("installed_ipa") and app.get("source_sha256")
                and app["installed_ipa"] != app["source_sha256"])


def has_previous(app_id: int) -> bool:
    return (app_dir(app_id) / "original.prev.ipa").exists()


def _swap_files(app_id: int) -> None:
    """Échange original.ipa et original.prev.ipa. Transactionnel : si une étape échoue (antivirus qui
    verrouille un fichier sous Windows), ce qui a été déplacé est remis en place avant de relever l'erreur,
    pour ne jamais laisser l'app sans original.ipa."""
    d = app_dir(app_id)
    orig, prev, tmp = d / "original.ipa", d / "original.prev.ipa", d / "original.swap"
    tmp.unlink(missing_ok=True)
    util.replace_retry(orig, tmp)
    try:
        util.replace_retry(prev, orig)
    except OSError:
        _put_back(tmp, orig)
        raise
    try:
        util.replace_retry(tmp, prev)
    except OSError:
        # original.ipa porte maintenant l'ancienne précédente : on défait les deux déplacements
        _put_back(orig, prev)
        _put_back(tmp, orig)
        raise


def _put_back(src, dst) -> None:
    try:
        util.replace_retry(src, dst)
    except OSError:
        pass        # au pire, recover_swaps() le rétablira au prochain démarrage


def recover_swaps() -> None:
    """Un échange interrompu (arrêt brutal, verrou persistant) a pu laisser original.swap sans original.ipa :
    c'est l'IPA d'origine, on la remet en place."""
    if not IPA_DIR.exists():
        return
    for swap in IPA_DIR.glob("*/original.swap"):
        orig = swap.with_name("original.ipa")
        try:
            if not orig.exists():
                os.replace(swap, orig)
                db.log(f"IPA d'origine rétablie après un échange interrompu ({swap.parent.name})", "warn")
        except OSError:
            pass


def _device_os(app: dict) -> str | None:
    if not app.get("device_id"):
        return None
    d = db.row("SELECT os_version FROM devices WHERE id = ?", app["device_id"])
    return d["os_version"] if d else None


def _blocked_by_os(app: dict) -> str | None:
    """tvOS minimal demandé par la version refusée, si le refus vient de là (« tvos:26.1 »)."""
    block = str(app.get("update_block") or "")
    return block[5:] if block.startswith("tvos:") else None


async def _lift_os_block(app: dict) -> None:
    """Version refusée parce que la TV était trop ancienne : si la TV a été mise à jour depuis, on la
    redevient candidate. La version de tvOS enregistrée peut être périmée : on la relit (au plus
    toutes les 6 h par TV) tant qu'un blocage existe."""
    need = _blocked_by_os(app)
    if not need or not app.get("device_id"):
        return
    have = _device_os(app)
    if ipa.os_older(have, need) and time.time() - _OS_REFRESHED.get(app["device_id"], 0) > 6 * 3600:
        _OS_REFRESHED[app["device_id"]] = time.time()
        try:
            await devices.refresh_info(app["device_id"])
        except Exception:  # noqa: BLE001 - TV éteinte : on réessaiera plus tard
            return
        have = _device_os(app)
    if not ipa.os_older(have, need):
        fields = dict(bad_tag=None, bad_key=None, update_tag=None, update_notes=None, update_block=None)
        db.update("apps", app["id"], **fields)
        app.update(fields)
        db.log(f"{app['name']} : tvOS {have} sur l'Apple TV, la version refusée (tvOS {need} minimum) est de nouveau candidate",
               app_id=app["id"])


def _note_update(app: dict, tag: str | None, notes: str | None, block: str | None) -> bool:
    """Mémorise (ou efface, avec tag None) la mise à jour disponible mais non installée, pour l'interface.
    Renvoie True quand c'est une nouvelle version qui vient d'être signalée."""
    want = dict(update_tag=tag, update_notes=notes or None if tag else None, update_block=block if tag else None)
    fields = {k: v for k, v in want.items() if app.get(k) != v}
    if fields:
        db.update("apps", app["id"], **fields)
        app.update(fields)
    return bool(tag) and "update_tag" in fields


async def _announce_available(app: dict, tag: str) -> None:
    """Mises à jour automatiques coupées : une ligne au journal et une notification par nouvelle version."""
    label = _link_label(app, tag)
    db.log(f"{app['name']} : version {label} disponible (mises à jour automatiques désactivées)",
           app_id=app["id"])
    await notify(notifier.text("update_available", name=app["name"], tag=label), "default", "update",
                 app=app["name"])


def _link_label(app: dict, tag: str | None) -> str:
    return ipa.redact(tag) if app["source_type"] == "url" and tag else str(tag)


async def refresh_source(app: dict, force: bool = False) -> bool:
    """Télécharge une nouvelle version si la source en publie une ; True si l'IPA a changé.
    Si une IPA utilisable existe déjà, un problème de source ne bloque pas le renouvellement.
    force : mise à jour demandée par l'utilisateur (ignore « mise à jour auto » coupée et les refus passés)."""
    if app["source_type"] == "upload":
        return False
    dest = app_dir(app["id"]) / "original.ipa"
    prev = app_dir(app["id"]) / "original.prev.ipa"
    hold = dest.with_suffix(".hold")
    tag, key, sha256, notes = None, None, None, None
    try:
        if dest.exists():
            await _lift_os_block(app)
        if app["source_type"] == "github":
            found = await asyncio.to_thread(_latest, app)
            if not found:
                if not dest.exists():
                    channel = "stable" if app.get("channel") != "beta" else "beta"
                    prefixes, _ = ipa.release_rule(app)
                    which = f", tags {' / '.join(x + '*' for x in prefixes)}" if prefixes else ""
                    hint, beta_tag = await _beta_hint(app) if channel == "stable" else ("", None)
                    pin = f" (version épinglée : {app['pinned_tag']})" if app.get("pinned_tag") else ""
                    raise NoRelease(f"aucune IPA trouvée dans les releases ({channel}{which}) de "
                                    f"{app['source']}{pin}{hint}", beta_tag)
                if app["id"] not in _NO_MATCH_LOGGED:
                    _NO_MATCH_LOGGED.add(app["id"])
                    db.log(f"{app['name']} : aucune release ne correspond plus dans {app['source']} "
                           "(tags renommés, IPA absente ou release trop ancienne ?), version actuelle conservée",
                           "warn", app_id=app["id"])
                return False
            _NO_MATCH_LOGGED.discard(app["id"])
            tag, url, sha256, key = found["tag"], found["url"], found["sha256"], found.get("key")
            notes = found.get("notes")
        else:
            tag = url = app["source"]
            key = await asyncio.to_thread(ipa.url_key, url)
        unchanged_sha = None
        if dest.exists():
            if not force and _rejected(app, tag, key):
                return False
            if _unchanged(app, tag, key):
                if key and app.get("source_key") is None:
                    db.update("apps", app["id"], source_key=key)   # première lecture de la clé (ancienne base)
                if app["source_type"] == "url" and key is None and app.get("source_sha256") \
                        and tag not in (app.get("bad_tag"),) \
                        and (force or (app.get("auto_update", 1)
                                       and time.time() - _LINK_CHECKED.get(app["id"], 0) > 6 * 3600)):
                    # serveur sans ETag, date ni taille : seul un nouveau téléchargement dit si le fichier a changé
                    # (périodiquement si la mise à jour auto est active, sinon seulement sur demande)
                    unchanged_sha = app["source_sha256"]
                else:
                    if app["source_type"] == "github":
                        _note_update(app, None, None, None)
                    return False
            elif not force and not app.get("auto_update", 1):
                # mise à jour automatique coupée : on signale la nouvelle version sans l'installer
                if _note_update(app, tag, notes, "manual"):
                    await _announce_available(app, tag)
                return False
        if not unchanged_sha:
            db.log(f"{app['name']} : téléchargement de {_link_label(app, tag)}"
                   + (" (fichier mis à jour sous le même nom)" if tag == app["source_tag"] and dest.exists() else "")
                   + (" (empreinte SHA-256 vérifiée)" if sha256 else ""), app_id=app["id"])
        # dest remplacé seulement si valide ; un bundle ID différent de l'app suivie est refusé
        expect = app.get("bundle_id") if dest.exists() else None
        extra = {}
        have_os = _device_os(app)
        if have_os:
            extra["max_os"] = have_os             # une release qui demande un tvOS plus récent que la TV est refusée
        if unchanged_sha:
            extra["unchanged_sha"] = unchanged_sha
        # dernière IPA installée gardée à côté (retour en arrière) ; si une mise à jour jamais installée
        # attend déjà, la copie existante (la dernière installée) reste en place
        keep = dest.exists() and not pending_update(app)
        if keep:
            await asyncio.to_thread(ipa.keep_copy, dest, hold, not unchanged_sha)   # copie possible : pas sur la boucle
        info = await asyncio.to_thread(ipa.download, url, dest, sha256, expect, **extra)
        if unchanged_sha:
            _LINK_CHECKED[app["id"]] = time.time()
        if info is None:
            return False                          # même empreinte : le fichier du lien n'a pas changé
        if keep and hold.exists():
            await asyncio.to_thread(util.replace_retry, hold, prev)
            db.update("apps", app["id"], prev_tag=app.get("source_tag"), prev_key=app.get("source_key"),
                      prev_version=app.get("version"), prev_sha256=app.get("source_sha256"))
    except ipa.OsTooOld as e:
        if not dest.exists():
            raise
        # TV trop ancienne pour cette version : app installée intacte, version non retéléchargée à chaque
        # passage, et reprise dès que la TV est mise à jour (_lift_os_block)
        db.update("apps", app["id"], bad_tag=tag, bad_key=key, update_tag=tag, update_notes=notes or None,
                  update_block=f"tvos:{e.min_os}")
        params = dict(name=app["name"], tag=_link_label(app, tag), need=e.min_os, have=e.have)
        db.log(notifier.text("update_blocked_os", "fr", **params), "warn", app_id=app["id"])
        await notify(notifier.text("update_blocked_os", **params), "default", "update", app=app["name"])
        return False
    except Exception as e:  # noqa: BLE001
        if not dest.exists():
            raise
        # SourceRejected (IPA invalide, empreinte fausse, bundle ID changé) est un ValueError.
        # Un lien dont on n'a pas pu lire de clé (erreur dès url_key, serveur sans marqueur) ne mémorise pas
        # de refus : il bloquerait aussi les fichiers corrigés. Dans ce cas c'est une panne passagère.
        key_known = key is not None or app["source_type"] == "github"
        if tag and isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError) and key_known:
            # IPA invalide (pas une IPA, pas tvOS) : ne pas la retélécharger à chaque essai.
            # Le reste (coupure, réponse tronquée, JSON GitHub illisible) est passager : on réessaiera.
            db.update("apps", app["id"], bad_tag=tag, bad_key=key)
            db.log(f"{app['name']} : version {_link_label(app, tag)} inutilisable ({e}), version précédente conservée",
                   "warn", app_id=app["id"])
            await notify(notifier.text("update_refused", name=app["name"], tag=_link_label(app, tag), why=e),
                         "default", "update", app=app["name"])
        else:
            db.log(f"{app['name']} : source indisponible ({e}), version précédente conservée",
                   "warn", app_id=app["id"])
        return False
    finally:
        hold.unlink(missing_ok=True)
    db.update("apps", app["id"], source_tag=tag, source_key=key, version=info["version"],
              bundle_id=info["bundle_id"], bad_tag=None, bad_key=None, source_sha256=info.get("sha256"),
              notes=notes or None, update_tag=None, update_notes=None, update_block=None)
    return True


async def _beta_hint(app: dict) -> tuple[str, str | None]:
    """Canal stable sans release : indique si le canal beta, lui, en a une (texte, tag)."""
    try:
        prefixes, prereleases = ipa.release_rule({**app, "channel": "beta"})
        found = await asyncio.to_thread(ipa.latest_github, app["source"], prefixes, prereleases,
                                        app.get("asset_name"))
    except Exception:  # noqa: BLE001 - simple indication
        return "", None
    return (f" ; une version beta existe ({found['tag']}) : choisis le canal Beta", found["tag"]) if found else ("", None)


class NoRelease(RuntimeError):
    """Aucune release GitHub ne correspond (canal, préfixes de tag) et aucune IPA n'est encore téléchargée."""

    def __init__(self, message: str, beta_tag: str | None = None):
        super().__init__(message)
        self.beta_tag = beta_tag


def _latest(app: dict) -> dict | None:
    prefixes, prereleases = ipa.release_rule(app)
    return ipa.latest_github(app["source"], prefixes, prereleases, app.get("asset_name"),
                             app.get("pinned_tag") or None)


def _release_error(app: dict, e: Exception) -> None:
    """La recherche de version échoue (quota GitHub, dépôt renommé…) : une ligne de journal par épisode,
    et l'état exposé à l'interface, au lieu d'un silence qui arrête les mises à jour sans trace."""
    msg = str(e) or e.__class__.__name__
    if _RELEASE_ERR.get(app["id"]) != f"{app['name']} : {msg}":
        db.log(f"{app['name']} : recherche de nouvelle version impossible ({msg})", "warn", app_id=app["id"])
    _RELEASE_ERR[app["id"]] = f"{app['name']} : {msg}"
    HEALTH["release_check_error"] = next(iter(_RELEASE_ERR.values()))


def _release_ok(app: dict) -> None:
    """La recherche de version ne pose plus problème pour cette app (réussie, app supprimée, en pause ou
    source changée) : on l'oublie, et la bannière disparaît quand plus aucune app n'échoue."""
    if _RELEASE_ERR.pop(app["id"], None) is not None:
        HEALTH["release_check_error"] = next(iter(_RELEASE_ERR.values()), None)


def _prune_release_errors(apps: list[dict]) -> None:
    """Oublie les échecs de recherche des apps qui n'ont plus à être cherchées (supprimées, en pause, plus GitHub)."""
    alive = {a["id"] for a in apps if a["enabled"] and a["source_type"] == "github"}
    for app_id in [i for i in _RELEASE_ERR if i not in alive]:
        _release_ok({"id": app_id})


async def new_release(app: dict) -> bool:
    """Vrai si la source GitHub publie une version différente de celle installée (autre tag, ou
    même tag dont l'IPA a été remplacée), pas déjà refusée, et si la mise à jour automatique est active.
    Sinon, une version disponible mais non installée est mémorisée pour l'interface. Les liens ne sont
    relus qu'au renouvellement (refresh_source)."""
    if app["source_type"] != "github":
        _release_ok(app)
        return False
    try:
        found = await asyncio.to_thread(_latest, app)
    except Exception as e:  # noqa: BLE001 - GitHub indisponible : on réessaiera, mais on le dit
        _release_error(app, e)
        return False
    _release_ok(app)
    if not found:
        return False
    await _lift_os_block(app)
    tag, key = found["tag"], found.get("key")
    differs = tag != app["source_tag"] or (bool(key) and app.get("source_key") not in (None, key))
    if not differs:
        _note_update(app, None, None, None)
        return False
    if _rejected(app, tag, key):
        if not _blocked_by_os(app) and app.get("update_block") != "rollback":
            _note_update(app, None, None, None)
        return False
    if not app.get("auto_update", 1):
        if _note_update(app, tag, found.get("notes"), "manual"):
            await _announce_available(app, tag)
        return False
    return True


# ---------- renouvellement ----------

class NeedsAction(RuntimeError):
    """Échec qu'un nouvel essai ne réparera pas : une action de l'utilisateur est nécessaire.
    kind : session (reconnecter le compte), config (Apple TV, compte ou IPA à corriger)."""

    def __init__(self, message: str, kind: str = "config"):
        super().__init__(message)
        self.kind = kind


class TvosTooOld(NeedsAction):
    """L'IPA demande un tvOS plus récent que celui de l'Apple TV : rien à signer ni envoyer. Seule une mise à
    jour de la TV (ou une version plus ancienne de l'app) y change quelque chose."""

    def __init__(self, message: str):
        super().__init__(message, "config")


# Délai avant de réessayer un échec qui attend une action de l'utilisateur : l'action (reconnexion,
# appairage, modification de l'app, renouvellement manuel) relance tout de suite, ceci n'est qu'un filet
# pour une réparation faite ailleurs. Un renouvellement qui échoue sur la limite du compte gratuit
# consomme des App ID : on laisse plus de temps.
BLOCKED_RETRY_HOURS = {"session": 6, "pairing": 6, "config": 6, "limit": 24}


# Équipe de développeur absente ou ambiguë (messages de plumesign) : se règle dans le compte Apple, pas en réessayant.
_TEAM = re.compile(r"NO_TEAM|MULTIPLE_TEAMS|TEAM_NOT_FOUND")


def failure_kind(exc: BaseException) -> str | None:
    """Action requise (session | pairing | limit | config) ou None si un nouvel essai a une chance de réussir."""
    if isinstance(exc, NeedsAction):
        return exc.kind
    if isinstance(exc, (plume.SessionExpired, plume.UnknownAccount)):
        return "session"
    if isinstance(exc, devices.PairingLost):
        return "pairing"
    if isinstance(exc, plume.LimitReached):
        return "limit"
    if _TEAM.search(str(exc)):
        return "config"
    return None


def is_transient(exc: BaseException) -> bool:
    """Panne passagère (anisette, réseau, Apple TV en veille ou app ouverte, outil de signature occupé)."""
    return isinstance(exc, (plume.Transient, plume.Busy, devices.AppInUse, devices.Unreachable))


def ipa_fault(exc: BaseException) -> bool:
    """L'échec peut-il venir de l'IPA elle-même ? Oui quand plumesign ou installd désignent le contenu
    (ContentError : bundle, Info.plist, entitlements, paquet refusé). Sinon, ni la TV, le compte, le réseau,
    Apple (qui refuse ou limite) ni une limite du compte ou un disque plein : l'ancienne version les suivrait
    à l'identique."""
    if isinstance(exc, util.ContentError):
        return True
    return not (failure_kind(exc) or is_transient(exc) or isinstance(exc, plume.AppleRefused)
                or util.is_limit_text(str(exc)))


def backoff_seconds(fails: int, transient: bool = False, left: float | None = None) -> float:
    """Délai avant le prochain essai après `fails` échecs d'affilée : double à chaque échec (5 ou 10 min,
    puis 10-20-40…) jusqu'à RETRY_MINUTES. Plus court pour une panne passagère (anisette, réseau, TV en
    veille : 15 min au plus) et quand moins de 12 h restent avant l'expiration (10 min au plus)."""
    base = (5 if transient else 10) * 60
    cap = RETRY_MINUTES * 60
    if transient:
        cap = min(cap, 15 * 60)
    if left is not None and left < 12 * 3600:
        cap = min(cap, 10 * 60)
    return min(cap, base * 2 ** min(max(fails - 1, 0), 10))


def _retry_at(app: dict, now: float, transient: bool = False, fails: int = 1, kind: str | None = None,
              slow: bool = False) -> float:
    """Prochain essai, jamais après l'expiration (une dernière tentative juste avant). `slow` : Apple a
    refusé la requête (-80045, 429) et limite les essais, chaque nouvel essai peut prolonger le blocage :
    on espace beaucoup (APPLE_BACKOFF_MINUTES), un peu moins près de l'expiration (30 min au plus)."""
    exp = app["expires_at"]
    if kind:
        delay = BLOCKED_RETRY_HOURS.get(kind, 6) * 3600
        if exp and exp - now < 12 * 3600 and kind != "limit":
            delay = min(delay, 2 * 3600)
    else:
        delay = backoff_seconds(fails, transient, exp - now if exp else None)
        if slow:
            delay = max(delay, APPLE_BACKOFF_MINUTES * 60)
            if exp and exp - now < 12 * 3600:
                delay = min(delay, 30 * 60)
    if exp and now < exp < now + delay:
        delay = max(exp - now - 60, 60)
    return now + delay


def _next_fails(app: dict, nature: str) -> int:
    """Nombre d'échecs d'affilée de même nature, celui qui vient d'avoir lieu compris. Une autre nature
    d'échec (TV en veille puis erreur Apple…) repart de 1 : elle ne doit pas hériter du délai de l'autre.
    Sans nature enregistrée (base d'avant la colonne), le compteur continue."""
    same = app.get("fail_kind") in (None, nature)
    return int(app.get("fail_count") or 0) + 1 if same else 1


def failure_fields(app: dict, now: float, exc: BaseException, transient: bool, optional: bool = False) -> dict:
    """Champs à enregistrer après un échec : compteur d'échecs d'affilée (et leur nature), action requise,
    prochain essai. `optional` : échec d'une mise à jour facultative, qui compte à part (préfixe « update: »)
    pour ne pas faire monter le délai du renouvellement obligatoire, ni l'inverse."""
    kind = failure_kind(exc)
    slow = isinstance(exc, plume.AppleRefused) and exc.slow
    nature = ("update:" if optional else "") + (kind or ("slow" if slow else "transient" if transient else "other"))
    fails = _next_fails(app, nature)
    return dict(fail_count=fails, fail_kind=nature, needs_action=kind,
                next_attempt_at=_retry_at(app, now, transient, fails, kind, slow))


def _reusable_signature(app: dict, original, signed, now: float) -> float | None:
    """Expiration d'une IPA signée récemment mais jamais installée (TV en veille au dernier essai),
    pour ne pas re-signer chez Apple à chaque nouvel essai. None si inutilisable."""
    try:
        if not signed.exists() or signed.stat().st_mtime < original.stat().st_mtime:
            return None
        exp = ipa.profile_expiry(signed)
    except Exception:  # noqa: BLE001 - fichier partiel ou corrompu
        return None
    if not exp or exp - now < (settings.renew_before_hours() + 24) * 3600:
        return None
    if app["expires_at"] and abs(exp - app["expires_at"]) < 120:
        return None  # c'est la signature déjà installée : on veut un nouveau profil
    return exp


def _serials(signed, email: str | None = None, exact: bool = False) -> str | None:
    """Certificat de la signature : celui qui a réellement signé (lu dans l'exécutable, à défaut le certificat du
    profil qui correspond à la clé privée du compte `email`) ; sinon, sauf `exact`, tous ceux du profil."""
    try:
        one = ipa.signing_serial(signed, plume.signing_key(email) if email else None)
        if one or exact:
            return one
        return ",".join(sorted(ipa.profile_cert_serials(signed))) or None
    except Exception:  # noqa: BLE001
        return None


def _phase(app_id: int, step: str, **info) -> None:
    prev = PHASE.get(app_id)
    since = prev["since"] if prev and prev["step"] == step else time.time()
    PHASE[app_id] = {"step": step, "since": since, **info}


def forget(app_id: int) -> None:
    """App supprimée : oublie son état en mémoire (l'identifiant SQLite peut être réattribué)."""
    for s in (_EXPIRED_LOGGED, _NO_MATCH_LOGGED, _RERUN):
        s.discard(app_id)
    PHASE.pop(app_id, None)
    _LINK_CHECKED.pop(app_id, None)
    _release_ok({"id": app_id})            # sinon la bannière « recherche impossible » resterait jusqu'au redémarrage


def _record(app_id: int, trigger: str, started: float) -> None:
    """Historique des renouvellements (inclus dans le dossier de support) ; ne doit jamais gêner un essai."""
    outcome, error = _OUTCOME.pop(app_id, ("skipped", None))
    try:
        db.execute("INSERT INTO renewals (app_id, started, finished, cause, outcome, error) VALUES (?, ?, ?, ?, ?, ?)",
                   app_id, started, time.time(), trigger, outcome, (error or "")[:500] or None)
        db.execute("DELETE FROM renewals WHERE id NOT IN (SELECT id FROM renewals ORDER BY id DESC LIMIT 500)")
    except Exception:  # noqa: BLE001
        pass


async def renew(app_id: int, trigger: str = "manuel") -> None:
    if app_id in _RUNNING or STOPPING or backup.RESTORING:
        return
    _RUNNING.add(app_id)
    try:
        while True:
            started = time.time()
            try:
                await asyncio.wait_for(_renew(app_id, trigger), RENEW_TIMEOUT_MINUTES * 60)
            except TimeoutError:
                _OUTCOME[app_id] = ("error", "renouvellement trop long, abandonné")
                app = db.row("SELECT * FROM apps WHERE id = ?", app_id)
                if app:
                    db.log(f"{app['name']} : renouvellement trop long, abandonné", "error", app_id=app_id)
                    await alert(app, "renouvellement trop long, abandonné", time.time())
            except Exception as e:  # noqa: BLE001 - une tâche de fond ne doit jamais mourir en silence
                _OUTCOME[app_id] = ("error", str(e))
                HEALTH["loop_error"] = f"renouvellement : {e}"
                _log_quietly(f"renouvellement de l'app {app_id} : {e}", app_id=app_id)
            finally:
                _record(app_id, trigger, started)
            if app_id not in _RERUN:
                break
            # Apple TV ou compte changés pendant l'opération : l'essai terminé visait l'ancienne destination
            _RERUN.discard(app_id)
            trigger = "nouvelle destination"
    finally:
        _RUNNING.discard(app_id)
        _RERUN.discard(app_id)
        PHASE.pop(app_id, None)


def spawn(app_id: int, trigger: str = "manuel") -> bool:
    """Lance un renouvellement en tâche de fond (référence conservée). False si déjà en cours."""
    if app_id in _RUNNING or STOPPING or backup.RESTORING:
        return False
    proc.spawn(renew(app_id, trigger))
    return True


def rerun(app_id: int) -> None:
    """À appeler quand la destination change : relance après le renouvellement en cours."""
    if app_id in _RUNNING:
        _RERUN.add(app_id)


def clear_block(app_id: int) -> None:
    """L'utilisateur a agi (destination, source, renouvellement manuel) : on repart de zéro."""
    db.update("apps", app_id, **CLEAR_BLOCK)


async def drain(seconds: float) -> None:
    """À l'arrêt du service : laisse finir les renouvellements en cours (jusqu'à `seconds`)
    avant que l'arrêt ne les interrompe. Aucun nouveau renouvellement ne démarre (STOPPING)."""
    global STOPPING
    STOPPING = True
    end = time.monotonic() + seconds
    if _RUNNING:
        db.log("Arrêt demandé : fin des renouvellements en cours avant de s'arrêter")
    while _RUNNING and time.monotonic() < end:
        await asyncio.sleep(1)


# Une session déjà jugée expirée est revérifiée auprès d'Apple au bout de ce délai plutôt que crue sur parole.
SESSION_RECHECK_SECONDS = 600


async def _usable_account(app: dict) -> str:
    """Compte qui signera l'app, après vérification de sa session. Lève NeedsAction / SessionExpired."""
    email = app["account_email"] or plume.default_account()
    if not email:
        raise NeedsAction("aucun compte Apple connecté", "session")
    known = plume.find_account(email)
    if not known:
        raise NeedsAction(f"compte Apple {email} déconnecté : reconnecte-le "
                          "(en choisir un autre installerait une 2e copie de l'app)", "session")
    email = known
    acc = next((a for a in plume.accounts() if a["email"] == email), None)
    if acc is None:      # déconnecté entre-temps (DELETE /api/accounts)
        raise NeedsAction(f"compte Apple {email} déconnecté : reconnecte-le "
                          "(en choisir un autre installerait une 2e copie de l'app)", "session")
    if acc["ok"] == 0:
        # Session jugée expirée. Récemment, inutile de solliciter Apple ; sinon on revérifie une fois : un
        # faux « expirée » (réponse passagère mal classée) ne doit pas bloquer les renouvellements
        # jusqu'au prochain contrôle périodique.
        if time.time() - (acc["checked_at"] or 0) < SESSION_RECHECK_SECONDS:
            raise plume.SessionExpired(f"session Apple de {email} expirée : reconnecte-le (bouton Reconnecter)", email)
        valid = await plume.check_account(email)
        if valid is False:
            raise plume.SessionExpired(f"session Apple de {email} expirée : reconnecte-le (bouton Reconnecter)", email)
        if valid is None:
            raise plume.Transient(f"session Apple de {email} non vérifiable pour l'instant (réseau ou anisette)")
    return email


def _as_limit(exc: Exception) -> Exception:
    """Limite du compte gratuit signalée par l'Apple TV (installd) ou Apple dans un message brut."""
    if not isinstance(exc, plume.LimitReached) and plume.is_limit(str(exc)):
        return plume.LimitReached(plume.limit_message("installation", str(exc)[-160:]))
    return exc


async def _preflight_os(device: dict, original) -> None:
    """MinimumOSVersion de l'IPA contre le tvOS de l'Apple TV, avant tout appel à Apple. La version
    enregistrée peut être périmée (TV mise à jour depuis) : elle est relue avant de refuser."""
    try:
        need = (await asyncio.to_thread(ipa.app_info, original)).get("min_os")
    except Exception:  # noqa: BLE001 - IPA illisible : la signature le dira
        return
    if not need or not ipa.os_older(device.get("os_version"), need):
        return
    try:
        await devices.refresh_info(device["id"])
        device = db.row("SELECT * FROM devices WHERE id = ?", device["id"]) or device
    except Exception:  # noqa: BLE001
        pass
    if ipa.os_older(device.get("os_version"), need):
        raise TvosTooOld(f"cette app demande tvOS {need} ou plus, « {device['name']} » est en tvOS "
                         f"{device.get('os_version')} : mets l'Apple TV à jour ou choisis une version plus ancienne")


async def _fall_back(app_id: int, first: Exception, deliver, signed) -> float:
    """La mise à jour téléchargée n'a pas pu être signée ou installée : on essaie la version précédente.
    Si elle s'installe, la nouvelle est écartée (refusée jusqu'à une autre release) ; si elle échoue aussi,
    le problème ne vient pas de la nouvelle version : tout est remis en place et l'erreur d'origine remonte.
    Seule une erreur qui peut venir de l'IPA elle-même compte (pas la TV, le compte, le réseau ni Apple)."""
    cur = db.row("SELECT * FROM apps WHERE id = ?", app_id)
    if not (cur and ipa_fault(first) and pending_update(cur) and has_previous(app_id)):
        raise first
    name = cur["name"]
    new_label = cur["source_tag"] or cur["version"] or "la nouvelle version"
    db.log(f"{name} : la version {new_label} n'a pas pu être installée ({first}), nouvel essai avec la version "
           f"précédente {cur['prev_tag'] or cur['prev_version'] or ''}".rstrip(), "warn", app_id=app_id)
    signed.unlink(missing_ok=True)
    try:
        _swap_files(app_id)
    except OSError as se:
        db.log(f"{name} : version précédente inaccessible ({se}), l'erreur d'origine est conservée", "warn", app_id=app_id)
        raise first from None
    try:
        expires = await deliver()
    except BaseException as e:
        try:
            _swap_files(app_id)
        except OSError as se:
            db.log(f"{name} : impossible de remettre la nouvelle version en place ({se})", "error", app_id=app_id)
        signed.unlink(missing_ok=True)
        if isinstance(e, Exception):
            raise first from None
        raise
    (app_dir(app_id) / "original.prev.ipa").unlink(missing_ok=True)       # la version écartée
    db.update("apps", app_id, source_tag=cur["prev_tag"], source_key=cur["prev_key"], version=cur["prev_version"],
              source_sha256=cur["prev_sha256"], bad_tag=cur["source_tag"], bad_key=cur["source_key"],
              update_tag=None, update_notes=None, update_block=None, notes=None,
              prev_tag=None, prev_key=None, prev_version=None, prev_sha256=None)
    prev_label = cur["prev_version"] or cur["prev_tag"] or "?"
    db.log(f"{name} : la version {new_label} n'a pas pu être installée ({first}) ; la version précédente "
           "est réinstallée et la nouvelle ignorée", "warn", app_id=app_id)
    await notify(notifier.text("update_rolled_back", name=name, tag=new_label, why=first, prev=prev_label),
                 "default", "update", app=name)
    return expires


async def _renew(app_id: int, trigger: str) -> None:
    app = db.row("SELECT * FROM apps WHERE id = ?", app_id)
    if not app:
        return  # app supprimée entre-temps
    if trigger == "auto" and not app["enabled"]:
        return  # mise en pause avant que la tâche ne démarre
    started = time.time()
    # nouvel essai d'un échec déjà connu : le détail d'un essai (signature, installation) reste hors du
    # journal, seule l'issue y figure (sinon ~3 lignes par essai noient la cause d'origine)
    retry = int(app.get("fail_count") or 0) > 0
    was_ok = app["status"] == "ok"
    db.update("apps", app_id, status="running", last_error=None, last_attempt_at=started,
              last_trigger=trigger)
    try:
        device = db.row("SELECT * FROM devices WHERE id = ?", app["device_id"]) if app["device_id"] else None
        if not device:
            raise NeedsAction("aucune Apple TV associée", "config")
        email = await _usable_account(app)

        # 1. Apple TV joignable ? On échoue vite, avant tout appel à Apple.
        # Pas de report sur la seule présence d'un processus : tvOS garde en mémoire les apps
        # quittées, une app « avec processus » n'est donc pas forcément à l'écran. Seul installd
        # sait si l'app est réellement utilisée : il met alors l'installation en attente (étape 3).
        _phase(app_id, "checking")
        if not device["udid"]:
            await devices.refresh_info(device["id"])
            device = db.row("SELECT * FROM devices WHERE id = ?", device["id"])
        else:
            await devices.check_reachable(device)

        updated = await refresh_source(app, force=trigger in FORCE_UPDATE)
        cur = db.row("SELECT * FROM apps WHERE id = ?", app_id) or app
        was_pending = pending_update(cur)             # version téléchargée, jamais installée avec succès
        if trigger in ("nouvelle version", "mise à jour") and not updated and was_ok and not was_pending:
            # téléchargement raté, version refusée ou rien de nouveau : inutile de re-signer l'ancienne version
            db.update("apps", app_id, status="ok", last_error=None)
            if trigger == "mise à jour" and not cur.get("update_tag"):
                db.log(f"{app['name']} : déjà à jour", app_id=app_id)
            return
        original = app_dir(app_id) / "original.ipa"
        signed = app_dir(app_id) / "signed.ipa"
        if not original.exists():
            raise NeedsAction("IPA d'origine absente : réimporte l'app", "config")
        if not cur.get("source_sha256"):
            cur["source_sha256"] = await asyncio.to_thread(_sha256_file, original)   # ancienne base ou import
            db.update("apps", app_id, source_sha256=cur["source_sha256"])
        await _preflight_os(device, original)

        force = trigger in FORCE_SIGN

        async def deliver() -> float:
            # 2. Signature (réutilise une signature récente jamais installée, sauf demande manuelle).
            expires = None if force else _reusable_signature(app, original, signed, time.time())
            if expires:
                db.log(f"{app['name']} : signature du {_fmt(signed.stat().st_mtime)} réutilisée", app_id=app_id,
                       journal=not retry)
            else:
                # « signature » seulement quand plumesign est à nous : une connexion Apple en attente du code
                # 2FA tient le verrou jusqu'à 10 min, la file d'attente s'affiche comme telle
                def lock_state(state: str) -> None:
                    _phase(app_id, "queued" if state == "waiting" else "signing")

                await plume.ensure_device(email, device["udid"], device["name"], on_lock=lock_state)
                db.log(f"{app['name']} : signature avec {email}", app_id=app_id, journal=not retry)
                if await plume.sign(email, original, signed, on_lock=lock_state):
                    db.log(notifier.text("cert_revoked", "fr", email=email, name=app["name"]), "warn", app_id=app_id)
                    # bandeau durable dans l'interface (une notification peut ne pas être configurée)
                    settings.raw_set("revocation_notice", json.dumps({"email": email, "app": app["name"],
                                                                      "at": time.time()}))
                    await notify(notifier.text("cert_revoked", email=email, name=app["name"]), "default", "account",
                                 app=app["name"])
                    # les autres apps tvsign du compte, signées avec le certificat révoqué, sont à refaire
                    # tout de suite plutôt qu'au prochain contrôle périodique
                    await _guarded("contrôle des certificats", _check_certificates(email))
                expires = ipa.profile_expiry(signed) or time.time() + 7 * 86400

            # 3. Installation. Si l'app est ouverte sur la TV, installd attend qu'elle soit fermée :
            # l'interface le signale, et on abandonne au bout de APP_WAIT_MINUTES. Une mise à jour facultative
            # n'attend que UPDATE_WAIT_MINUTES : une TV en service ne retient pas la boucle une demi-heure.
            db.log(f"{app['name']} : installation sur « {device['name']} »", app_id=app_id, journal=not retry)
            optional = _optional_update(app, trigger, was_ok, time.time())
            wait_cap = (UPDATE_WAIT_MINUTES if optional else APP_WAIT_MINUTES) * 60
            logged = set()

            def on_event(step: str, percent=None, since=None) -> None:
                if step == "waiting":
                    _phase(app_id, "waiting_app", percent=percent, waiting_since=since,
                           deadline=(since or time.time()) + wait_cap)
                    if "waiting" not in logged:
                        logged.add("waiting")
                        db.log(f"{app['name']} : installation en attente, l'app est sans doute ouverte sur "
                               f"« {device['name']} » ; elle se terminera dès sa fermeture", "warn", app_id=app_id)
                else:
                    _phase(app_id, step, percent=percent)

            _phase(app_id, "uploading")
            await devices.install(device, [signed], on_event=on_event, wait_cap=wait_cap, app_name=app["name"])
            return expires

        try:
            expires = await deliver()
        except Exception as first:  # noqa: BLE001
            expires = await _fall_back(app_id, first, deliver, signed)

        now = time.time()
        # ce qui tourne maintenant sur la TV (« version » est la dernière téléchargée ; après un repli sur la
        # version précédente, la ligne a été remise à jour)
        done = db.row("SELECT account_email, version, source_tag, source_sha256 FROM apps WHERE id = ?", app_id) or cur
        fields = dict(status="ok", signed_at=now, expires_at=expires, next_attempt_at=None, last_error=None,
                      notified_level=None, fail_count=0, fail_kind=None, needs_action=None, update_error=None,
                      cert_serial=_serials(signed, email), installed_ipa=done["source_sha256"],
                      installed_version=done.get("version"), installed_tag=done.get("source_tag"))
        if expires - now <= settings.renew_before_hours() * 3600 + CHECK_INTERVAL:
            # profil déjà dans la fenêtre de renouvellement (réglage au maximum, profil plus court que prévu) :
            # sans ce délai, la boucle re-signerait et réinstallerait à chaque passage
            fields["next_attempt_at"] = now + 6 * 3600
            db.log(f"{app['name']} : le profil obtenu expire le {_fmt(expires)}, dans la fenêtre de renouvellement "
                   f"({settings.renew_before_hours():g} h) : prochain essai dans 6 h", "warn", app_id=app_id)
        if done and not done.get("account_email"):
            # fige le compte : un autre compte = autre équipe = autre bundle ID (2e copie).
            # Pas d'écrasement d'un compte choisi pendant l'opération.
            fields["account_email"] = email
        db.update("apps", app_id, **fields)
        _OUTCOME[app_id] = ("ok", None)
        db.log(f"{app['name']} : renouvelée ({trigger}), valable jusqu'au {_fmt(expires)}, "
               f"prochain renouvellement auto le {_fmt(expires - settings.renew_before_hours() * 3600)}",
               app_id=app_id)
        if was_pending and done.get("source_sha256") == cur.get("source_sha256"):
            notes = (cur.get("notes") or "").strip()
            db.log(f"{app['name']} : version {done.get('version')} installée", app_id=app_id)
            await notify(notifier.text("update_installed", name=app["name"], version=done.get("version"),
                                       notes=("\n\n" + notes[:600]) if notes else ""),
                         "default", "update", app=app["name"], version=done.get("version"))
        if app.get("notified_level"):
            # seulement si une alerte d'échec est partie : sinon « renouvelée » n'annonce rien de nouveau
            await notify(notifier.text("renewed", name=app["name"], until=_fmt(expires)), "default", "recovered",
                         app=app["name"])
    except asyncio.CancelledError:
        # délai dépassé ou arrêt du service : ne pas rester « en cours »
        if STOPPING:
            # arrêt normal (redémarrage, mise à jour) : pas une erreur, on reprendra au démarrage
            db.update("apps", app_id, status="pending", last_error=None, next_attempt_at=None)
        else:
            now = time.time()
            _OUTCOME[app_id] = ("error", "renouvellement trop long, abandonné")
            db.update("apps", app_id, status="error", last_error="renouvellement trop long, abandonné",
                      **failure_fields(app, now, TimeoutError(), False))
        raise
    except Exception as e:  # noqa: BLE001 - l'erreur est remontée dans l'interface
        e = _as_limit(e)
        now = time.time()
        transient = is_transient(e)
        reconnect = isinstance(e, plume.SessionExpired)
        exp = app["expires_at"]
        optional = _optional_update(app, trigger, was_ok, now)
        # les essais d'une mise à jour facultative ont leur propre recul : ils ne font pas monter celui du
        # renouvellement obligatoire (ni l'inverse)
        fields = failure_fields(app, now, e, transient, optional)
        if optional:
            # Mise à jour facultative impossible alors que l'app installée est valable pour longtemps :
            # elle reste « ok » (pas de rouge, pas d'alerte ; TV en veille : l'app valable attend simplement la
            # TV). La mise à jour est retentée plus tard, avec recul, sans jamais repousser le renouvellement
            # obligatoire (pas plus tard que l'ouverture de sa fenêtre) ; le message est montré sur la carte.
            fields["next_attempt_at"] = min(fields["next_attempt_at"],
                                            max(now + 60, exp - settings.renew_before_hours() * 3600))
            db.update("apps", app_id, status="ok", last_error=None, update_error=str(e)[:500], **fields)
            _OUTCOME[app_id] = ("skipped", str(e))
            waiting = transient or fields["needs_action"] in ("session", "pairing")
            db.log(f"{app['name']} : mise à jour {'en attente' if waiting else 'impossible'}, {e}",
                   "warn", app_id=app_id)
            if reconnect:
                await account_down(e.email or app.get("account_email") or plume.default_account(),
                                   plume.REAUTH_REASON)
            elif not waiting and not app.get("update_error"):
                await notify(notifier.text("update_failed", name=app["name"], why=e), "default", "update",
                             app=app["name"])
            return
        _OUTCOME[app_id] = ("error", str(e))
        db.update("apps", app_id, status="error", last_error=str(e), **fields)
        db.log(f"{app['name']} : échec, {e}", "error", app_id=app_id)
        if reconnect:
            # une seule alerte pour le compte, pas une par app : les niveaux 24h / 6h / expiré restent notifiés
            await account_down(e.email or app.get("account_email") or plume.default_account(), plume.REAUTH_REASON)
        # une panne banale (TV en veille, app ouverte, anisette, reconnexion à faire) loin de l'expiration ne
        # prévient pas : les niveaux 24h / 6h / expiré, eux, notifient toujours
        await alert(db.row("SELECT * FROM apps WHERE id = ?", app_id) or app, str(e), now,
                    quiet_fail=transient or reconnect)


def _optional_update(app: dict, trigger: str, was_ok: bool, now: float) -> bool:
    """Mise à jour facultative : l'app installée reste valable bien au-delà de la fenêtre de renouvellement."""
    exp = app["expires_at"]
    return bool(trigger in UPDATE_TRIGGERS and was_ok and exp and exp - now > settings.renew_before_hours() * 3600)


def urgent(app: dict, now: float) -> bool:
    """Moins de 24 h avant l'expiration, ou jamais installée : la plage horaire ne s'applique plus."""
    exp = app["expires_at"]
    return not exp or exp - now < 24 * 3600


def due(app: dict, now: float) -> bool:
    if not app["enabled"] or app["id"] in _RUNNING:
        return False
    if app["next_attempt_at"] and app["next_attempt_at"] > now:
        return False
    if not urgent(app, now) and not settings.in_renew_window(now):
        return False        # renouvellement non urgent : on attend la plage horaire choisie
    if not app["expires_at"] or app["status"] in ("error", "pending"):
        # échec (même loin de l'expiration : nouvelle version, TV en veille…) ou essai interrompu
        # par un redémarrage : on réessaie, au rythme de next_attempt_at
        return True
    return app["expires_at"] - now < settings.renew_before_hours() * 3600


def window_view(app: dict, now: float, before_hours: float) -> dict:
    """Ce que la plage horaire change pour l'interface : `renew_at` réel (jamais avant le prochain début de
    plage pour un renouvellement non urgent), `window_wait` (le renouvellement ou le nouvel essai attend la
    plage) et `window_at` (quand elle s'ouvre). Sans plage, ou pour une app urgente, rien ne change."""
    exp = app["expires_at"]
    base = exp - before_hours * 3600 if exp else None
    out = {"renew_at": base, "window_wait": False, "window_at": None}
    if not exp or not app["enabled"] or urgent(app, now) or settings.renew_window() is None:
        return out
    limit = exp - 24 * 3600                 # au-delà, la plage ne s'applique plus
    start = settings.next_window_start(max(base, now))
    if start > max(base, now) and base < limit:
        out["renew_at"] = min(start, limit)
    if app["id"] not in _RUNNING and not settings.in_renew_window(now) and (
            base <= now or app["status"] in ("error", "pending")):
        t0 = max(now, base, app["next_attempt_at"] or 0)
        out["window_wait"] = True
        out["window_at"] = min(settings.next_window_start(t0), max(limit, t0))
        out["renew_at"] = max(out["renew_at"], out["window_at"]) if base <= now else out["renew_at"]
    return out


def idle(app: dict, now: float) -> bool:
    """Ni en cours, ni en attente d'un nouvel essai après échec."""
    return bool(app["enabled"]) and app["id"] not in _RUNNING and not (
        app["next_attempt_at"] and app["next_attempt_at"] > now)


def by_urgency(apps: list[dict], now: float) -> list[dict]:
    """Les plus proches de l'expiration d'abord (expirées en tête, jamais installées comme expirées)."""
    return sorted(apps, key=lambda a: (a["expires_at"] or now, a["id"]))


# ---------- limites du compte gratuit ----------

# Un compte gratuit : 3 apps actives par appareil (et 10 App ID par 7 jours, dont un par extension comme
# TopShelf). Apple ne le dit qu'au moment de signer ou d'installer : on compte à l'avance les apps suivies
# par tvsign, sans pouvoir voir celles installées avec d'autres outils (elles comptent aussi).
MAX_ACTIVE_APPS = 3


def account_slots(apps: list[dict], devices_: list[dict], accounts: list[dict], default: str | None) -> dict:
    """{e-mail: [{device_id, device_name, apps, max}]} : apps actives suivies par compte et par Apple TV."""
    names = {d["id"]: d["name"] for d in devices_}
    out = {}
    for acc in accounts:
        per: dict[int, int] = {}
        for a in apps:
            if a["enabled"] and a["device_id"] and _same_account(a["account_email"] or default, acc["email"]):
                per[a["device_id"]] = per.get(a["device_id"], 0) + 1
        out[acc["email"]] = [{"device_id": d, "device_name": names.get(d), "apps": n, "max": MAX_ACTIVE_APPS}
                             for d, n in sorted(per.items())]
    return out


def add_warnings(app: dict) -> list[str]:
    """Avertissements à l'ajout d'une app : « slots_full » si ce compte a déjà 3 apps actives sur cette TV."""
    if not app.get("device_id"):
        return []
    default = plume.default_account()
    same = [a for a in db.rows("SELECT * FROM apps WHERE enabled = 1 AND device_id = ?", app["device_id"])
            if _same_account(a["account_email"] or default, app["account_email"] or default)]
    return ["slots_full"] if len(same) > MAX_ACTIVE_APPS else []


# ---------- reprise après une action de l'utilisateur ----------

def _same_account(a: str | None, b: str | None) -> bool:
    return (a or "").casefold() == (b or "").casefold()


def _resume(apps: list[dict], trigger: str) -> int:
    """Relance tout de suite (par ordre d'urgence) ces apps ; sans boucle d'événements, seul l'état est remis à zéro."""
    now = time.time()
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False
    started = 0
    for app in by_urgency(apps, now):
        db.update("apps", app["id"], **CLEAR_BLOCK)
        if running and spawn(app["id"], trigger):
            started += 1
    return started


def _account_valid(email: str) -> None:
    """Session Apple de nouveau valide (reconnexion, ou vérification réussie après un échec) : les apps de ce
    compte qui attendaient ne patientent pas jusqu'au prochain essai, et l'alerte « déconnecté » est réarmée."""
    plume.ALERTED.discard(email)
    default = plume.default_account()
    waiting = [a for a in db.rows("SELECT * FROM apps WHERE enabled = 1 AND status = 'error'")
               if _same_account(a["account_email"] or default, email)
               and a["needs_action"] in (None, "session")]
    if waiting:
        n = _resume(waiting, "reconnexion")
        if n:
            db.log(f"Compte Apple {email} valide : reprise de {n} renouvellement(s) en attente")


def _device_paired(device_id: int) -> None:
    """Apple TV (ré)appairée : les apps qui attendaient l'appairage repartent."""
    waiting = db.rows("SELECT * FROM apps WHERE enabled = 1 AND status = 'error' AND device_id = ? "
                      "AND needs_action = 'pairing'", device_id)
    if waiting:
        _resume(waiting, "appairage")


plume.ON_VALID.append(_account_valid)
devices.ON_PAIRED.append(_device_paired)


# ---------- vérifications périodiques ----------

async def _check_certificates(email: str) -> None:
    """Certificat révoqué par un autre outil (Xcode, SideStore, AltStore…) sur le même compte :
    l'app ne se lance plus alors que son profil semble valide. On force la re-signature.
    Seul le certificat qui a signé l'app compte : le profil liste aussi ceux des autres outils de
    l'équipe, qui restent valides quand le nôtre est révoqué."""
    issued = await plume.issued_cert_serials(email)
    if issued is None:
        return
    default = plume.default_account()
    for app in db.rows("SELECT * FROM apps WHERE enabled = 1 AND status = 'ok'"):
        if not _same_account(app["account_email"] or default, email) or app["id"] in _RUNNING:
            continue
        signed = app_dir(app["id"]) / "signed.ipa"
        serials = app["cert_serial"] or (_serials(signed, email) if signed.exists() else None)
        if serials and "," in serials and signed.exists():
            # ancienne valeur (tous les certificats du profil) : on retrouve celui qui a signé
            one = _serials(signed, email, exact=True)
            if one and "," not in one:
                serials = one
                db.update("apps", app["id"], cert_serial=one)
        if not serials:
            continue
        if not {s.strip().upper().lstrip("0") for s in serials.split(",")} & issued:
            signed.unlink(missing_ok=True)   # ne pas réutiliser une signature révoquée
            # L'app ne se lance plus : elle compte comme expirée (une seconde de retard), ce qui la
            # rend due tout de suite et garde les alertes 6h / expirée, /api/health et l'interface
            # actifs si la re-signature échoue. (Un 0 effaçait toute échéance.)
            db.update("apps", app["id"], expires_at=time.time() - 1, next_attempt_at=None,
                      notified_level=None)
            db.log(f"{app['name']} : certificat révoqué, nouvelle signature", "error", app_id=app["id"])
            await notify(notifier.text("cert_revoked_other", name=app["name"]), "high", "account", app=app["name"])


async def account_down(email: str | None, reason: str) -> None:
    """Compte à reconnecter : une alerte (journal + notification) par épisode, que la déconnexion soit
    découverte par le contrôle périodique ou par un renouvellement. L'état « Déconnecté » et sa raison
    sont posés par plume (account_status)."""
    if not email or email in plume.ALERTED:
        return
    plume.ALERTED.add(email)
    db.log(f"Compte Apple {email} déconnecté : reconnexion nécessaire ({reason})", "error")
    await notify(notifier.text("account_lost", email=email, reason=reason), "high", "account")


async def _check_accounts() -> None:
    for acc in plume.accounts():
        email = acc["email"]
        was_down = email in plume.ALERTED           # _account_valid() ou _set_status() le réarme pendant check_account()
        ok = await plume.check_account(email)       # ne lève pas : None si indéterminé
        if ok is False:
            await account_down(email, plume.REAUTH_REASON)
        elif ok:
            if was_down:
                db.log(f"Compte Apple {email} de nouveau valide")
            await _check_certificates(email)


async def _check_expired(now: float, skip: set | frozenset = frozenset()) -> None:
    """Signale une expiration même si aucun essai n'est en cours. `skip` : apps dont le renouvellement
    démarre à l'instant (leur échec alertera lui-même, une réussite ne doit rien signaler)."""
    expired = db.rows("SELECT * FROM apps WHERE enabled = 1 AND expires_at > 0 AND expires_at < ?", now)
    expired = [a for a in expired if a["id"] not in skip]
    _EXPIRED_LOGGED.intersection_update(a["id"] for a in expired)   # renouvelée depuis : épisode terminé
    for app in expired:
        if app["notified_level"] != "expired":
            if app["id"] not in _EXPIRED_LOGGED:
                _EXPIRED_LOGGED.add(app["id"])
                db.log(f"{app['name']} : profil expiré le {_fmt(app['expires_at'])}", "error", app_id=app["id"])
            await alert(app, app["last_error"] or "renouvellement pas encore réussi", now)


def startup() -> None:
    """Au démarrage : rien ne peut être « en cours », et on corrige les dates déjà enregistrées."""
    db.execute("UPDATE apps SET status = 'pending' WHERE status = 'running'")
    recover_swaps()
    # ancien report « app ouverte » : supprimé, ne plus l'afficher
    db.execute("UPDATE apps SET deferred_reason = NULL, deferred_since = NULL WHERE deferred_reason IS NOT NULL")
    # ajout interrompu (redémarrage pendant un téléchargement) : ligne restée en pause, sans identifiant d'app
    for ghost in db.rows("SELECT id FROM apps WHERE enabled = 0 AND bundle_id IS NULL AND status = 'pending' "
                         "AND created_at < ?", time.time() - 120):
        shutil.rmtree(IPA_DIR / str(ghost["id"]), ignore_errors=True)
        db.execute("DELETE FROM apps WHERE id = ?", ghost["id"])
        db.execute("DELETE FROM events WHERE app_id = ?", ghost["id"])
        forget(ghost["id"])
        db.log("Ajout d'app interrompu : ligne incomplète supprimée", "warn")
    # bases d'avant le suivi de la version installée : ce qui est installé est ce qui est affiché
    db.execute("UPDATE apps SET installed_version = version, installed_tag = source_tag "
               "WHERE installed_version IS NULL AND signed_at IS NOT NULL AND status = 'ok'")
    # idem pour l'IPA : celle qui est présente est celle qui a été installée
    for app in db.rows("SELECT id, source_sha256 FROM apps WHERE installed_ipa IS NULL AND signed_at IS NOT NULL"):
        original = app_dir(app["id"]) / "original.ipa"
        try:
            digest = app["source_sha256"] or (original.exists() and _sha256_file(original)) or None
        except OSError:
            digest = None
        if digest:
            db.update("apps", app["id"], installed_ipa=digest, source_sha256=digest)
    for app in db.rows("SELECT * FROM apps WHERE status = 'ok' AND signed_at IS NOT NULL"):
        signed = app_dir(app["id"]) / "signed.ipa"
        try:
            # seulement si signed.ipa est bien la signature installée (pas un essai ultérieur)
            if not signed.exists() or abs(signed.stat().st_mtime - app["signed_at"]) > 3600:
                continue
            exp = ipa.profile_expiry(signed)
        except Exception:  # noqa: BLE001
            continue
        fields = {}
        if exp and app["expires_at"] and abs(exp - app["expires_at"]) > 60:
            fields["expires_at"] = exp       # anciennes dates lues en heure locale au lieu d'UTC
        # le certificat signataire, plus précis que la liste du profil ; la liste ne sert qu'à défaut de valeur
        serial = _serials(signed, app["account_email"] or plume.default_account(), exact=True)
        if not serial and not app["cert_serial"]:
            serial = _serials(signed)
        if serial and serial != app["cert_serial"]:
            fields["cert_serial"] = serial
        db.update("apps", app["id"], **fields)


# ---------- boucle de planification ----------

LOOP_SLACK_SECONDS = 120
SUPERVISE_DELAY = 5          # secondes avant de relancer une boucle tombée


def health(now: float | None = None) -> dict:
    """État de la boucle, sans complaisance : arrêtée si sa tâche est morte ou n'a pas avancé depuis
    plusieurs passages (une passe ne dure que le temps de lancer les renouvellements : ils tournent à part)."""
    now = now or time.time()
    last = HEALTH["last_loop_at"]
    alive = TASK is None or not TASK.done()
    stalled = not alive or now - (last or HEALTH["started_at"]) > 3 * CHECK_INTERVAL + LOOP_SLACK_SECONDS
    background = {name: not t.done() for name, t in _BG.items()}
    return {**HEALTH, "notify_error": notifier.last_error(), "notifications": notifier.configured(),
            "loop_stalled": stalled, "loop_alive": alive,
            "renewals_running": len(_RUNNING), "background": background,
            "check_interval": CHECK_INTERVAL, "retry_minutes": RETRY_MINUTES,
            "release_check_minutes": RELEASE_CHECK_MINUTES, "account_check_hours": ACCOUNT_CHECK_HOURS}


def _log_quietly(message: str, level: str = "error", app_id: int | None = None) -> None:
    """Journal sans jamais lever : appelé depuis un gestionnaire d'erreur, souvent parce que la base
    est verrouillée ou pleine, et une exception de plus tuerait la boucle de renouvellement."""
    try:
        db.log(message, level, app_id=app_id)
    except Exception as e:  # noqa: BLE001
        print(f"[{level}] {message} (journal indisponible : {e})", flush=True)


async def _guarded(what: str, coro) -> None:
    try:
        await coro
    except Exception as e:  # noqa: BLE001 - un échec isolé ne doit pas bloquer le reste
        HEALTH["loop_error"] = f"{what} : {e}"
        _log_quietly(f"{what} : {e}")


def _background(name: str, factory) -> bool:
    """Lance factory() en tâche de fond, une seule fois à la fois : les vérifications longues (contrôle
    des comptes, recherche de nouvelles versions) ne retardent jamais la boucle ni les renouvellements."""
    task = _BG.get(name)
    if task and not task.done():
        return False
    _BG[name] = proc.spawn(factory())
    return True


async def _account_pass() -> None:
    await _guarded("vérification des comptes", _check_accounts())
    HEALTH["last_account_check_at"] = time.time()


async def _release_pass() -> None:
    await _guarded("recherche de nouvelles versions", _scan_releases())
    HEALTH["last_release_check_at"] = time.time()


async def _selfupdate_pass() -> None:
    await _guarded("recherche de mise à jour de tvsign", selfupdate.check())


async def _scan_releases() -> None:
    now = time.time()
    candidates = by_urgency([a for a in db.rows("SELECT * FROM apps WHERE source_type = 'github'")
                             if idle(a, now)], now)
    for app in candidates:
        if STOPPING:
            break
        fresh = db.row("SELECT * FROM apps WHERE id = ?", app["id"])     # pause, renouvellement manuel…
        t = time.time()
        if not fresh or not idle(fresh, t):
            continue
        if not urgent(fresh, t) and not settings.in_renew_window(t):
            continue        # mise à jour non urgente : attend la plage horaire
        if await new_release(fresh):
            spawn(fresh["id"], "nouvelle version")


async def tick(now: float, last_account_check: float, last_release_check: float) -> tuple[float, float]:
    """Une passe : lance (sans les attendre) les contrôles et les renouvellements dus, plus urgents d'abord."""
    proc.sweep()   # sessions de connexion/appairage abandonnées
    if now - last_account_check > ACCOUNT_CHECK_HOURS * 3600:
        if _background("accounts", _account_pass):
            last_account_check = now
    if RELEASE_CHECK_MINUTES > 0 and now - last_release_check > RELEASE_CHECK_MINUTES * 60:
        if _background("releases", _release_pass):
            last_release_check = now
    launched = set()
    apps = db.rows("SELECT * FROM apps")
    _prune_release_errors(apps)
    for app in by_urgency([a for a in apps if due(a, now)], now):
        if STOPPING:
            break
        if spawn(app["id"], "auto"):
            launched.add(app["id"])
    # mise à jour téléchargée mais pas encore installée (TV en veille, échec) : nouvel essai, à la plage horaire
    for app in by_urgency([a for a in apps if a["id"] not in launched and a["status"] == "ok" and idle(a, now)
                           and a.get("auto_update", 1) and pending_update(a)], now):
        if STOPPING:
            break
        if (urgent(app, now) or settings.in_renew_window(now)) and spawn(app["id"], "nouvelle version"):
            launched.add(app["id"])
    _background("selfupdate", _selfupdate_pass)         # une recherche par jour au plus (réseau : hors de la boucle)
    # Les tâches lancées ci-dessus n'ont pas encore démarré (_RUNNING est vide pour elles) : sans cette
    # exclusion, une app expirée dont la re-signature démarre recevrait une alerte « EXPIRÉ » à tort.
    await _guarded("alertes d'expiration", _check_expired(time.time(), launched | set(_RUNNING)))
    return last_account_check, last_release_check


async def loop() -> None:
    last_account_check = last_release_check = 0.0
    while True:
        if backup.RESTORING:        # restauration en cours : la base et les dossiers sont en train d'être remplacés
            await asyncio.sleep(5)
            continue
        now = time.time()
        HEALTH["last_loop_at"] = now
        HEALTH["next_check_at"] = now + CHECK_INTERVAL
        HEALTH["loop_error"] = None
        try:
            last_account_check, last_release_check = await tick(now, last_account_check, last_release_check)
        except Exception as e:  # noqa: BLE001 - la boucle ne doit jamais s'arrêter
            HEALTH["loop_error"] = str(e)
            _log_quietly(f"boucle de renouvellement : {e}")
        HEALTH["next_check_at"] = time.time() + CHECK_INTERVAL
        await asyncio.sleep(CHECK_INTERVAL)


async def supervise() -> None:
    """Relance la boucle si elle s'arrête sur une erreur imprévue (elle n'est censée s'arrêter qu'à l'arrêt du
    service) : sans cela, plus aucun renouvellement n'aurait lieu et rien ne le dirait."""
    while not STOPPING:
        try:
            await loop()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            HEALTH["restarts"] += 1
            HEALTH["loop_error"] = f"boucle relancée : {e}"
            _log_quietly(f"boucle de renouvellement arrêtée, relancée : {e}")
            await asyncio.sleep(SUPERVISE_DELAY)


async def watchdog(period: float = 300) -> None:
    """Prévient (une fois par épisode) quand la boucle ne donne plus signe de vie : /api/livez reste
    vert tant que le serveur répond, et personne n'ouvre forcément l'interface."""
    global _STALL_ALERTED
    while True:
        await asyncio.sleep(period)
        try:
            if health()["loop_stalled"]:
                if not _STALL_ALERTED:
                    _STALL_ALERTED = True
                    _log_quietly("la boucle de renouvellement ne répond plus")
                    await notify(notifier.text("loop_stalled"), "urgent", "fail")
            else:
                _STALL_ALERTED = False
        except Exception as e:  # noqa: BLE001
            print(f"[warn] surveillance de la boucle : {e}", flush=True)


def start() -> asyncio.Task:
    """Démarre la boucle supervisée ; health() surveille cette tâche."""
    global TASK
    TASK = asyncio.create_task(supervise())
    return TASK
