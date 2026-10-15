"""Renouvellement : mise à jour de la source, signature, installation, et boucle de planification."""
import asyncio
import base64
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request

from . import db, devices, ipa, plume, proc, selfupdate, settings, util
from .config import (ACCOUNT_CHECK_HOURS, APP_WAIT_MINUTES, CHECK_INTERVAL, IPA_DIR,
                     RELEASE_CHECK_MINUTES, RENEW_TIMEOUT_MINUTES, RETRY_MINUTES)

# Arrêt du service en cours : plus aucun renouvellement ne démarre, ceux en cours peuvent finir.
STOPPING = False

_RUNNING: set[int] = set()
# Étape du renouvellement en cours, par app, pour l'interface :
# {"step": checking|signing|uploading|installing|waiting_app, "since": ts, ...}
PHASE: dict[int, dict] = {}
_RERUN: set[int] = set()                # destination changée pendant un renouvellement : on relance après
_ACC_ALERTED: set[str] = set()          # comptes déjà signalés déconnectés (une alerte par épisode)
_EXPIRED_LOGGED: set[int] = set()       # apps dont l'expiration est déjà au journal (une fois par épisode)
_NO_MATCH_LOGGED: set[int] = set()      # apps dont l'absence de release correspondante est déjà au journal
_RELEASE_ERR: dict[int, str] = {}       # apps dont la recherche de version échoue : message (une ligne de journal par épisode)
_LINK_CHECKED: dict[int, float] = {}    # liens sans marqueur de version : dernière comparaison d'empreinte
_OS_REFRESHED: dict[int, float] = {}    # Apple TV dont la version de tvOS a été relue pour lever un blocage

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
    "notify_error": None,               # dernier échec d'envoi ntfy
    "release_check_error": None,        # dernier échec de la recherche de nouvelles versions (GitHub)
}


def app_dir(app_id: int):
    d = IPA_DIR / str(app_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fmt(ts: float) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(ts))


# ---------- notifications ----------

def _send(message: str, priority: str, url: str | None = None) -> None:
    url = url or settings.ntfy_url()
    headers = {"Title": "tvsign", "Priority": priority}
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None:
        # https://user:pass@hôte/sujet : urllib n'en fait pas d'authentification, on l'envoie en Basic
        cred = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
        headers["Authorization"] = "Basic " + base64.b64encode(cred.encode("utf-8")).decode()
        host = parts.hostname + (f":{parts.port}" if parts.port else "")
        url = urllib.parse.urlunsplit(parts._replace(netloc=host))
    req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers)
    urllib.request.urlopen(req, timeout=10).close()


async def notify_test(url: str | None = None) -> None:
    """Notification d'essai depuis les Réglages ; lève l'erreur pour l'afficher."""
    target = url or settings.ntfy_url()
    if not target:
        raise RuntimeError("aucune adresse de notification")
    await asyncio.to_thread(_send, "tvsign : notification de test", "default", target)
    HEALTH["notify_error"] = None


async def notify(message: str, priority: str = "default") -> bool:
    """Envoi ntfy hors de la boucle d'événements ; un échec est journalisé une fois.
    True si la notification est partie : l'appelant ne doit la considérer envoyée qu'alors."""
    if not settings.ntfy_url():
        return False
    try:
        await asyncio.to_thread(_send, message, priority)
        HEALTH["notify_error"] = None
        return True
    except Exception as e:  # noqa: BLE001 - la notification est accessoire
        if HEALTH["notify_error"] is None:
            db.log(f"notification ntfy échouée : {e}", "warn")
        HEALTH["notify_error"] = str(e)
        return False


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
    if lvl == "fail":
        sent = await notify(f"{app['name']} : renouvellement échoué, {reason}")
    elif lvl == "expired":
        sent = await notify(f"{app['name']} : profil EXPIRÉ, l'app ne se lance plus. {reason}", "urgent")
    else:
        sent = await notify(f"{app['name']} expire dans moins de {lvl[:-1]} h et le renouvellement échoue : {reason}", "high")
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
    """Échange original.ipa et original.prev.ipa."""
    d = app_dir(app_id)
    tmp = d / "original.swap"
    tmp.unlink(missing_ok=True)
    util.replace_retry(d / "original.ipa", tmp)
    util.replace_retry(d / "original.prev.ipa", d / "original.ipa")
    util.replace_retry(tmp, d / "original.prev.ipa")


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


def _note_update(app: dict, tag: str | None, notes: str | None, block: str | None) -> None:
    """Mémorise (ou efface, avec tag None) la mise à jour disponible mais non installée, pour l'interface."""
    want = dict(update_tag=tag, update_notes=notes or None if tag else None, update_block=block if tag else None)
    fields = {k: v for k, v in want.items() if app.get(k) != v}
    if fields:
        db.update("apps", app["id"], **fields)
        app.update(fields)


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
                    hint = await _beta_hint(app) if channel == "stable" else ""
                    pin = f" (version épinglée : {app['pinned_tag']})" if app.get("pinned_tag") else ""
                    raise RuntimeError(f"aucune IPA trouvée dans les releases ({channel}{which}) de "
                                       f"{app['source']}{pin}{hint}")
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
                        and app.get("auto_update", 1) and tag not in (app.get("bad_tag"),) \
                        and (force or time.time() - _LINK_CHECKED.get(app["id"], 0) > 6 * 3600):
                    # serveur sans ETag, date ni taille : seul un nouveau téléchargement dit si le fichier a changé
                    unchanged_sha = app["source_sha256"]
                else:
                    if app["source_type"] == "github":
                        _note_update(app, None, None, None)
                    return False
            elif not force and not app.get("auto_update", 1):
                # mise à jour automatique coupée : on signale la nouvelle version sans l'installer
                _note_update(app, tag, notes, "manual")
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
            ipa.keep_copy(dest, hold, copy=not unchanged_sha)
        info = await asyncio.to_thread(ipa.download, url, dest, sha256, expect, **extra)
        if unchanged_sha:
            _LINK_CHECKED[app["id"]] = time.time()
        if info is None:
            return False                          # même empreinte : le fichier du lien n'a pas changé
        if keep and hold.exists():
            util.replace_retry(hold, prev)
            db.update("apps", app["id"], prev_tag=app.get("source_tag"), prev_key=app.get("source_key"),
                      prev_version=app.get("version"), prev_sha256=app.get("source_sha256"))
    except ipa.OsTooOld as e:
        if not dest.exists():
            raise
        # TV trop ancienne pour cette version : app installée intacte, version non retéléchargée à chaque
        # passage, et reprise dès que la TV est mise à jour (_lift_os_block)
        db.update("apps", app["id"], bad_tag=tag, bad_key=key, update_tag=tag, update_notes=notes or None,
                  update_block=f"tvos:{e.min_os}")
        msg = (f"{app['name']} : la version {_link_label(app, tag)} demande tvOS {e.min_os} ou plus, "
               f"l'Apple TV est en tvOS {e.have} : version actuelle conservée")
        db.log(msg, "warn", app_id=app["id"])
        await notify(msg)
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
            await notify(f"{app['name']} : nouvelle version {_link_label(app, tag)} inutilisable ({e}), "
                         "version précédente conservée")
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


async def _beta_hint(app: dict) -> str:
    """Canal stable sans release : indique si le canal beta, lui, en a une."""
    try:
        prefixes, prereleases = ipa.release_rule({**app, "channel": "beta"})
        found = await asyncio.to_thread(ipa.latest_github, app["source"], prefixes, prereleases,
                                        app.get("asset_name"))
    except Exception:  # noqa: BLE001 - simple indication
        return ""
    return f" ; une version beta existe ({found['tag']}) : choisis le canal Beta" if found else ""


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
    if _RELEASE_ERR.pop(app["id"], None) is not None:
        HEALTH["release_check_error"] = next(iter(_RELEASE_ERR.values()), None)


async def new_release(app: dict) -> bool:
    """Vrai si la source GitHub publie une version différente de celle installée (autre tag, ou
    même tag dont l'IPA a été remplacée), pas déjà refusée, et si la mise à jour automatique est active.
    Sinon, une version disponible mais non installée est mémorisée pour l'interface. Les liens ne sont
    relus qu'au renouvellement (refresh_source)."""
    if app["source_type"] != "github":
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
        _note_update(app, tag, found.get("notes"), "manual")
        return False
    return True


# ---------- renouvellement ----------

async def renew(app_id: int, trigger: str = "manuel") -> None:
    if app_id in _RUNNING or STOPPING:
        return
    _RUNNING.add(app_id)
    try:
        while True:
            try:
                await asyncio.wait_for(_renew(app_id, trigger), RENEW_TIMEOUT_MINUTES * 60)
            except TimeoutError:
                app = db.row("SELECT * FROM apps WHERE id = ?", app_id)
                if app:
                    db.log(f"{app['name']} : renouvellement trop long, abandonné", "error", app_id=app_id)
                    await alert(app, "renouvellement trop long, abandonné", time.time())
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
    if app_id in _RUNNING or STOPPING:
        return False
    proc.spawn(renew(app_id, trigger))
    return True


def rerun(app_id: int) -> None:
    """À appeler quand la destination change : relance après le renouvellement en cours."""
    if app_id in _RUNNING:
        _RERUN.add(app_id)


# Causes qui ne se règlent que par une action de l'utilisateur (compte plein, pas d'équipe, tvOS trop ancien) :
# réessayer toutes les heures ne ferait que solliciter Apple pour rien.
_PERMANENT = re.compile(r"NO_TEAM|MULTIPLE_TEAMS|maximum (number )?of (apps|app ids)|too many (apps|app ids)|"
                        r"app id limit|limit of \d+ (apps|app ids)|demande tvOS", re.I)
# Échecs locaux, avant tout appel à Apple : sans coût pour Apple, mais inutiles tant que rien n'a changé.
_LOCAL = re.compile(r"aucune Apple TV|aucun compte Apple|déconnecté|reconnecte-le|appairage perdu|"
                    r"compte .*(introuvable|retiré)", re.I)


def error_kind(e: Exception) -> str:
    """unreachable (TV introuvable) | local (compte, appairage…) | transient (réseau, anisette, app ouverte) |
    permanent (action de l'utilisateur requise) | other."""
    if isinstance(e, devices.Unreachable):
        return "unreachable"
    if isinstance(e, (devices.PairingLost, plume.SessionExpired, plume.UnknownAccount)) or _LOCAL.search(str(e)):
        return "local"
    if isinstance(e, (plume.Transient, devices.AppInUse)):
        return "transient"
    if isinstance(e, TvosTooOld) or _PERMANENT.search(str(e)):
        return "permanent"
    return "other"


# (premier délai, plafond) en minutes par nature d'échec ; le délai double à chaque échec consécutif.
_BACKOFF = {"unreachable": (10, 60), "local": (10, 60), "transient": (10, 180), "permanent": (1440, 1440)}
# plafond quand l'échéance est dans moins de 12 h : on insiste davantage, sauf pour ce qui demande l'utilisateur
_NEAR_EXPIRY = {"unreachable": 10, "local": 10, "transient": 30, "other": 60, "permanent": 360}


def _retry_at(app: dict, now: float, transient: bool = False, kind: str | None = None,
              fails: int | None = None) -> float:
    """Prochain essai après un échec : recul exponentiel selon le nombre d'échecs consécutifs (fail_count),
    plafonné. Les échecs qui ne touchent pas Apple (TV éteinte, compte déconnecté) reviennent plus vite ;
    ceux qui exigent l'utilisateur, une fois par jour. Plus rapproché à l'approche de l'échéance (jamais après
    elle), mais sans insister après l'expiration : Apple ne doit pas être sollicité toutes les 10 min."""
    kind = kind or ("transient" if transient else "other")
    n = max(1, fails if fails is not None else (app.get("fail_count") or 0) + 1)
    first, cap = _BACKOFF.get(kind, (RETRY_MINUTES, 360))
    delay = min(first * 2 ** min(n - 1, 12), cap) * 60
    exp = app.get("expires_at")
    if exp and 0 < exp - now < 12 * 3600:
        delay = min(delay, _NEAR_EXPIRY[kind] * 60)
    elif exp and exp <= now and kind in ("unreachable", "local"):
        delay = min(delay, 30 * 60)
    if exp and now < exp < now + delay:
        delay = max(exp - now - 60, 60)
    return now + delay


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


def _serials(signed) -> str | None:
    """Certificat de la signature : celui qui a réellement signé (lu dans l'exécutable) ; à défaut, ceux du profil."""
    try:
        return ipa.signer_serial(signed) or ",".join(sorted(ipa.profile_cert_serials(signed))) or None
    except Exception:  # noqa: BLE001
        return None


def _phase(app_id: int, step: str, **info) -> None:
    prev = PHASE.get(app_id)
    since = prev["since"] if prev and prev["step"] == step else time.time()
    PHASE[app_id] = {"step": step, "since": since, **info}


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


class TvosTooOld(RuntimeError):
    """L'IPA demande un tvOS plus récent que celui de l'Apple TV : rien à signer ni envoyer."""


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
    le problème ne vient pas de la nouvelle version : tout est remis en place et l'erreur d'origine remonte."""
    cur = db.row("SELECT * FROM apps WHERE id = ?", app_id)
    if not (cur and error_kind(first) == "other" and pending_update(cur) and has_previous(app_id)):
        raise first
    name = cur["name"]
    new_label = cur["source_tag"] or cur["version"] or "la nouvelle version"
    db.log(f"{name} : la version {new_label} n'a pas pu être installée ({first}), nouvel essai avec la version "
           f"précédente {cur['prev_tag'] or cur['prev_version'] or ''}".rstrip(), "warn", app_id=app_id)
    signed.unlink(missing_ok=True)
    _swap_files(app_id)
    try:
        expires = await deliver()
    except BaseException as e:
        _swap_files(app_id)
        signed.unlink(missing_ok=True)
        if isinstance(e, Exception):
            raise first from None
        raise
    (app_dir(app_id) / "original.prev.ipa").unlink(missing_ok=True)       # la version écartée
    db.update("apps", app_id, source_tag=cur["prev_tag"], source_key=cur["prev_key"], version=cur["prev_version"],
              source_sha256=cur["prev_sha256"], bad_tag=cur["source_tag"], bad_key=cur["source_key"],
              update_tag=None, update_notes=None, update_block=None, notes=None,
              prev_tag=None, prev_key=None, prev_version=None, prev_sha256=None)
    msg = (f"{name} : la version {new_label} n'a pas pu être installée ({first}) ; la version précédente "
           "est réinstallée et la nouvelle ignorée")
    db.log(msg, "warn", app_id=app_id)
    await notify(msg)
    return expires


async def _renew(app_id: int, trigger: str) -> None:
    app = db.row("SELECT * FROM apps WHERE id = ?", app_id)
    if not app:
        return  # app supprimée entre-temps
    if trigger == "auto" and not app["enabled"]:
        return  # mise en pause pendant que la boucle traitait une autre app
    started = time.time()
    was_ok = app["status"] == "ok"
    db.update("apps", app_id, status="running", last_error=None, last_attempt_at=started,
              last_trigger=trigger)
    try:
        device = db.row("SELECT * FROM devices WHERE id = ?", app["device_id"]) if app["device_id"] else None
        if not device:
            raise RuntimeError("aucune Apple TV associée")
        email = app["account_email"] or plume.default_account()
        if not email:
            raise RuntimeError("aucun compte Apple connecté")
        accounts = {a["email"]: a for a in plume.accounts()}
        if email not in accounts:
            raise RuntimeError(f"compte Apple {email} déconnecté : reconnecte-le "
                               "(en choisir un autre installerait une 2e copie de l'app)")
        if accounts[email]["ok"] == 0:
            # session déjà connue comme expirée : inutile de solliciter Apple
            raise plume.SessionExpired(f"session Apple de {email} expirée : reconnecte-le (bouton Reconnecter)")

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
        if trigger in ("nouvelle version", "mise à jour") and not updated and was_ok and not pending_update(cur):
            # téléchargement raté, version refusée ou rien de nouveau : inutile de re-signer l'ancienne version
            db.update("apps", app_id, status="ok", last_error=None)
            if trigger == "mise à jour" and not cur.get("update_tag"):
                db.log(f"{app['name']} : déjà à jour", app_id=app_id)
            return
        original = app_dir(app_id) / "original.ipa"
        signed = app_dir(app_id) / "signed.ipa"
        if not original.exists():
            raise RuntimeError("IPA d'origine absente : réimporte l'app")
        if not cur.get("source_sha256"):
            cur["source_sha256"] = await asyncio.to_thread(_sha256_file, original)   # ancienne base ou import
            db.update("apps", app_id, source_sha256=cur["source_sha256"])
        await _preflight_os(device, original)

        force = trigger in FORCE_SIGN

        async def deliver() -> float:
            # 2. Signature (réutilise une signature récente jamais installée, sauf demande manuelle).
            expires = None if force else _reusable_signature(app, original, signed, time.time())
            if expires:
                db.log(f"{app['name']} : signature du {_fmt(signed.stat().st_mtime)} réutilisée", app_id=app_id)
            else:
                _phase(app_id, "signing")
                await plume.ensure_device(email, device["udid"], device["name"])
                db.log(f"{app['name']} : signature avec {email}", app_id=app_id)
                if await plume.sign(email, original, signed):
                    msg = (f"un ancien certificat de {email} a été révoqué pour signer {app['name']} : "
                           "les autres apps sideloadées avec ce compte (iPhone…) vont cesser de fonctionner")
                    db.log(msg, "warn", app_id=app_id)
                    await notify(msg)
                    # les autres apps tvsign du compte, signées avec le certificat révoqué, sont à refaire
                    # tout de suite plutôt qu'au prochain contrôle périodique
                    await _guarded("contrôle des certificats", _check_certificates(email))
                expires = ipa.profile_expiry(signed) or time.time() + 7 * 86400

            # 3. Installation. Si l'app est ouverte sur la TV, installd attend qu'elle soit fermée :
            # l'interface le signale, et on abandonne au bout de APP_WAIT_MINUTES.
            db.log(f"{app['name']} : installation sur « {device['name']} »", app_id=app_id)
            wait_cap = APP_WAIT_MINUTES * 60
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
        installed = db.row("SELECT source_sha256 FROM apps WHERE id = ?", app_id) or cur
        fields = dict(status="ok", signed_at=now, expires_at=expires, next_attempt_at=None,
                      last_error=None, notified_level=None, cert_serial=_serials(signed), fail_count=0,
                      update_error=None, installed_ipa=installed["source_sha256"])
        if expires - now <= settings.renew_before_hours() * 3600 + CHECK_INTERVAL:
            # profil déjà dans la fenêtre de renouvellement (réglage au maximum, profil plus court que prévu) :
            # sans ce délai, la boucle re-signerait et réinstallerait à chaque passage
            fields["next_attempt_at"] = now + 6 * 3600
            db.log(f"{app['name']} : le profil obtenu expire le {_fmt(expires)}, dans la fenêtre de renouvellement "
                   f"({settings.renew_before_hours():g} h) : prochain essai dans 6 h", "warn", app_id=app_id)
        cur_account = db.row("SELECT account_email FROM apps WHERE id = ?", app_id)
        if cur_account and not cur_account["account_email"]:
            # fige le compte : un autre compte = autre équipe = autre bundle ID (2e copie).
            # Pas d'écrasement d'un compte choisi pendant l'opération.
            fields["account_email"] = email
        db.update("apps", app_id, **fields)
        db.log(f"{app['name']} : renouvelée ({trigger}), valable jusqu'au {_fmt(expires)}, "
               f"prochain renouvellement auto le {_fmt(expires - settings.renew_before_hours() * 3600)}",
               app_id=app_id)
        if app["status"] == "error" or app.get("notified_level"):
            await notify(f"{app['name']} : renouvelée, valable jusqu'au {_fmt(expires)}")
    except asyncio.CancelledError:
        # délai dépassé ou arrêt du service : ne pas rester « en cours »
        if STOPPING:
            # arrêt normal (redémarrage, mise à jour) : pas une erreur, on reprendra au démarrage
            db.update("apps", app_id, status="pending", last_error=None, next_attempt_at=None)
        else:
            db.update("apps", app_id, status="error", last_error="renouvellement trop long, abandonné",
                      next_attempt_at=_retry_at(app, time.time()), fail_count=(app.get("fail_count") or 0) + 1)
        raise
    except Exception as e:  # noqa: BLE001 - l'erreur est remontée dans l'interface
        now = time.time()
        kind = error_kind(e)
        fails = (app.get("fail_count") or 0) + 1
        retry = _retry_at(app, now, kind=kind, fails=fails)
        exp = app["expires_at"]
        if trigger in UPDATE_TRIGGERS and was_ok and exp and exp - now > settings.renew_before_hours() * 3600:
            # Mise à jour facultative impossible alors que l'app installée est valable pour longtemps :
            # elle reste « ok » (pas de rouge, pas d'alerte). La mise à jour est retentée plus tard, avec recul ;
            # le message est montré sur la carte.
            db.update("apps", app_id, status="ok", last_error=None, update_error=str(e)[:500],
                      next_attempt_at=retry, fail_count=fails)
            waiting = kind in ("unreachable", "transient", "local")
            db.log(f"{app['name']} : mise à jour {'en attente' if waiting else 'impossible'}, {e}",
                   "warn", app_id=app_id)
            if not waiting and not app.get("update_error"):
                await notify(f"{app['name']} : la mise à jour n'a pas pu être installée ({e}), "
                             "la version actuelle reste valable")
            return
        db.update("apps", app_id, status="error", last_error=str(e), next_attempt_at=retry, fail_count=fails)
        db.log(f"{app['name']} : échec, {e}", "error", app_id=app_id)
        # une TV en veille loin de l'expiration est banale : pas de notification avant le niveau 24h
        await alert(db.row("SELECT * FROM apps WHERE id = ?", app_id) or app, str(e), now,
                    quiet_fail=kind == "unreachable")


def due(app: dict, now: float) -> bool:
    if not app["enabled"] or app["id"] in _RUNNING:
        return False
    if app["next_attempt_at"] and app["next_attempt_at"] > now:
        return False
    if not app["expires_at"] or app["status"] in ("error", "pending"):
        # échec (même loin de l'expiration : nouvelle version, TV en veille…) ou essai interrompu
        # par un redémarrage : on réessaie, au rythme de next_attempt_at
        return True
    return app["expires_at"] - now < settings.renew_before_hours() * 3600


def idle(app: dict, now: float) -> bool:
    """Ni en cours, ni en attente d'un nouvel essai après échec."""
    return bool(app["enabled"]) and app["id"] not in _RUNNING and not (
        app["next_attempt_at"] and app["next_attempt_at"] > now)


# ---------- vérifications périodiques ----------

async def _check_certificates(email: str) -> None:
    """Certificat révoqué par un autre outil (Xcode, SideStore, AltStore…) sur le même compte :
    l'app ne se lance plus alors que son profil semble valide. On force la re-signature."""
    issued = await plume.issued_cert_serials(email)
    if issued is None:
        return
    default = plume.default_account()
    for app in db.rows("SELECT * FROM apps WHERE enabled = 1 AND status = 'ok'"):
        if (app["account_email"] or default) != email or app["id"] in _RUNNING:
            continue
        signed = app_dir(app["id"]) / "signed.ipa"
        serials = app["cert_serial"] or (_serials(signed) if signed.exists() else None)
        if not serials:
            continue
        if not {s.lstrip("0") for s in serials.split(",")} & issued:
            signed.unlink(missing_ok=True)   # ne pas réutiliser une signature révoquée
            # L'app ne se lance plus : elle compte comme expirée (une seconde de retard), ce qui la
            # rend due tout de suite et garde les alertes 6h / expirée, /api/health et l'interface
            # actifs si la re-signature échoue. (Un 0 effaçait toute échéance.)
            db.update("apps", app["id"], expires_at=time.time() - 1, next_attempt_at=None,
                      notified_level=None)
            db.log(f"{app['name']} : certificat révoqué, nouvelle signature", "error", app_id=app["id"])
            await notify(f"{app['name']} : certificat Apple révoqué (autre outil sur le même compte), "
                         "re-signature en cours", "high")


async def _check_accounts() -> None:
    for acc in plume.accounts():
        email = acc["email"]
        ok = await plume.check_account(email)       # ne lève pas : None si indéterminé
        if ok is False:
            if email not in _ACC_ALERTED:
                _ACC_ALERTED.add(email)
                db.log(f"Compte Apple {email} déconnecté : reconnexion nécessaire", "error")
                await notify(f"Compte Apple {email} déconnecté : reconnecte-le dans tvsign", "high")
        elif ok:
            if email in _ACC_ALERTED:
                _ACC_ALERTED.discard(email)
                db.log(f"Compte Apple {email} de nouveau valide")
            await _check_certificates(email)


async def _check_expired(now: float) -> None:
    """Signale une expiration même si aucun essai n'est en cours."""
    expired = db.rows("SELECT * FROM apps WHERE enabled = 1 AND expires_at > 0 AND expires_at < ?", now)
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
    # ancien report « app ouverte » : supprimé, ne plus l'afficher
    db.execute("UPDATE apps SET deferred_reason = NULL, deferred_since = NULL WHERE deferred_reason IS NOT NULL")
    # Base d'avant le suivi de la version installée : l'IPA présente est celle qui a été installée
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
        serial = _serials(signed)
        if serial and serial != app["cert_serial"]:
            fields["cert_serial"] = serial       # le certificat signataire, plus précis que la liste du profil
        db.update("apps", app["id"], **fields)


def health(now: float | None = None) -> dict:
    now = now or time.time()
    last = HEALTH["last_loop_at"]
    stalled = bool(last) and now - last > 3 * CHECK_INTERVAL + RENEW_TIMEOUT_MINUTES * 60
    return {**HEALTH, "notifications": bool(settings.ntfy_url()), "loop_stalled": stalled,
            "check_interval": CHECK_INTERVAL, "retry_minutes": RETRY_MINUTES,
            "release_check_minutes": RELEASE_CHECK_MINUTES, "account_check_hours": ACCOUNT_CHECK_HOURS}


async def _guarded(what: str, coro) -> None:
    try:
        await coro
    except Exception as e:  # noqa: BLE001 - un échec isolé ne doit pas bloquer le reste
        HEALTH["loop_error"] = f"{what} : {e}"
        db.log(f"{what} : {e}", "error")


async def loop() -> None:
    last_account_check = last_release_check = 0.0
    while True:
        now = time.time()
        HEALTH["last_loop_at"] = now
        HEALTH["next_check_at"] = now + CHECK_INTERVAL
        HEALTH["loop_error"] = None
        try:
            proc.sweep()   # sessions de connexion/appairage abandonnées
            if now - last_account_check > ACCOUNT_CHECK_HOURS * 3600:
                last_account_check = HEALTH["last_account_check_at"] = now
                await _guarded("vérification des comptes", _check_accounts())
            check_releases = RELEASE_CHECK_MINUTES > 0 and now - last_release_check > RELEASE_CHECK_MINUTES * 60
            if check_releases:
                last_release_check = HEALTH["last_release_check_at"] = now
            for app in db.rows("SELECT * FROM apps"):
                if STOPPING:
                    break
                # une app précédente a pu durer longtemps : relire la ligne (pause, renouvellement manuel…)
                app = db.row("SELECT * FROM apps WHERE id = ?", app["id"])
                if not app:
                    continue
                now = HEALTH["last_loop_at"] = time.time()   # la boucle vit tant qu'elle avance d'app en app
                if due(app, now):
                    await _guarded(f"renouvellement de {app['name']}", renew(app["id"], "auto"))
                elif check_releases and idle(app, now) and await new_release(app):
                    await _guarded(f"renouvellement de {app['name']}", renew(app["id"], "nouvelle version"))
                elif app.get("auto_update", 1) and idle(app, now) and pending_update(app) and app["status"] == "ok":
                    # mise à jour téléchargée mais pas encore installée (TV en veille, échec) : nouvel essai
                    await _guarded(f"renouvellement de {app['name']}", renew(app["id"], "nouvelle version"))
            await _check_expired(time.time())
            await _guarded("recherche de mise à jour de tvsign", selfupdate.check())
        except Exception as e:  # noqa: BLE001 - la boucle ne doit jamais s'arrêter
            HEALTH["loop_error"] = str(e)
            db.log(f"boucle de renouvellement : {e}", "error")
        HEALTH["next_check_at"] = time.time() + CHECK_INTERVAL
        await asyncio.sleep(CHECK_INTERVAL)
