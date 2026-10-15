"""Renouvellement : mise à jour de la source, signature, installation, et boucle de planification."""
import asyncio
import json
import shutil
import time

from . import backup, db, devices, ipa, notifier, plume, proc, settings
from .config import (ACCOUNT_CHECK_HOURS, APP_WAIT_MINUTES, CHECK_INTERVAL, IPA_DIR,
                     RELEASE_CHECK_MINUTES, RENEW_TIMEOUT_MINUTES, RETRY_MINUTES, UPDATE_WAIT_MINUTES)
from .util import ContentError, replace_retry

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
_UPDATE_FAILS: dict[int, int] = {}      # échecs de signature/installation d'une mise à jour pas encore installée
_OUTCOME: dict[int, tuple[str, str | None]] = {}   # résultat du dernier essai par app (historique des renouvellements)
_STALL_ALERTED = False                  # blocage de la boucle déjà signalé (une alerte par épisode)

# Après N échecs de contenu (pas de réseau) d'une version téléchargée, on revient à la précédente.
ROLLBACK_AFTER = 2

# État de la boucle, exposé dans /api/state pour l'interface.
HEALTH: dict = {
    "started_at": time.time(),
    "last_loop_at": None,               # dernier passage de la boucle
    "next_check_at": None,              # prochain passage prévu
    "loop_error": None,                 # dernière erreur inattendue de la boucle
    "last_account_check_at": None,
    "last_release_check_at": None,
    "notify_error": None,               # dernier échec d'envoi ntfy
}


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
    """Cette release (ce contenu) a-t-elle déjà été refusée ?"""
    return tag == app.get("bad_tag") and (key is None or app.get("bad_key") in (None, key))


class NoRelease(RuntimeError):
    """Aucune release GitHub ne correspond (canal, préfixes de tag) et aucune IPA n'est encore téléchargée."""

    def __init__(self, message: str, beta_tag: str | None = None):
        super().__init__(message)
        self.beta_tag = beta_tag


def _keep_previous(dest, app: dict):
    """Garde une copie de l'IPA actuelle avant qu'une nouvelle la remplace (une copie, pas un lien
    physique : aucun écrivain ne peut alors altérer les deux). Renvoie le chemin provisoire, ou None
    si rien à garder."""
    if not dest.exists():
        return None
    keep = dest.with_name("previous.new")
    keep.unlink(missing_ok=True)
    shutil.copy2(dest, keep)
    return keep


def _device_os(app: dict) -> str | None:
    if not app.get("device_id"):
        return None
    d = db.row("SELECT os_version FROM devices WHERE id = ?", app["device_id"])
    return (d or {}).get("os_version") or None


async def refresh_source(app: dict, force: bool = False) -> bool:
    """Télécharge une nouvelle version si la source en publie une ; True si l'IPA a changé.
    Si une IPA utilisable existe déjà, un problème de source ne bloque pas le renouvellement.
    Une app dont les mises à jour automatiques sont coupées ne change de version que sur demande
    (force) ; la version précédente reste alors proposée comme « disponible »."""
    if app["source_type"] == "upload":
        return False
    dest = app_dir(app["id"]) / "original.ipa"
    tag, key, sha256, notes, keep = None, None, None, None, None
    try:
        if app["source_type"] == "github":
            found = await asyncio.to_thread(_latest, app)
            if not found:
                if not dest.exists():
                    channel = "stable" if app.get("channel") != "beta" else "beta"
                    prefixes, _ = ipa.release_rule(app)
                    which = f", tags {' / '.join(x + '*' for x in prefixes)}" if prefixes else ""
                    hint, beta_tag = await _beta_hint(app) if channel == "stable" else ("", None)
                    raise NoRelease(f"aucune IPA trouvée dans les releases ({channel}{which}) de "
                                    f"{app['source']}{hint}", beta_tag)
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
        if dest.exists():
            if _rejected(app, tag, key) and not force:
                return False         # « Installer maintenant » (force) retente une version écartée
            if _unchanged(app, tag, key):
                if key and app.get("source_key") is None:
                    db.update("apps", app["id"], source_key=key)   # première lecture de la clé (ancienne base)
                return False
            if not force and not app.get("auto_update", 1):
                if app["source_type"] == "github" and app.get("avail_tag") != tag:
                    db.update("apps", app["id"], avail_tag=tag, avail_notes=notes)
                return False
        db.log(f"{app['name']} : téléchargement de {tag}"
               + (" (fichier mis à jour sous le même nom)" if tag == app["source_tag"] and dest.exists() else "")
               + (" (empreinte SHA-256 vérifiée)" if sha256 else ""), app_id=app["id"])
        # dest remplacé seulement si valide ; un bundle ID différent de l'app suivie est refusé,
        # ainsi qu'une version qui exige une tvOS plus récente que celle de l'Apple TV
        expect = app.get("bundle_id") if dest.exists() else None
        extra = {"device_os": _device_os(app)} if _device_os(app) else {}
        keep = await asyncio.to_thread(_keep_previous, dest, app)
        info = await asyncio.to_thread(ipa.download, url, dest, sha256, expect, **extra)
    except Exception as e:  # noqa: BLE001
        if keep:
            keep.unlink(missing_ok=True)
        if not dest.exists():
            raise
        # SourceRejected (IPA invalide, empreinte fausse, bundle ID changé, tvOS trop ancienne)
        # est un ValueError
        if tag and isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError):
            # IPA invalide (pas une IPA, pas tvOS) : ne pas la retélécharger à chaque essai.
            # Le reste (coupure, réponse tronquée, JSON GitHub illisible) est passager : on réessaiera.
            db.update("apps", app["id"], bad_tag=tag, bad_key=key)
            db.log(f"{app['name']} : version {tag} inutilisable ({e}), version précédente conservée",
                   "warn", app_id=app["id"])
            await notify(notifier.text("update_refused", name=app["name"], tag=tag, why=e), "default", "update",
                         app=app["name"])
        else:
            db.log(f"{app['name']} : source indisponible ({e}), version précédente conservée",
                   "warn", app_id=app["id"])
        return False
    fields = dict(source_tag=tag, source_key=key, version=info["version"], bundle_id=info["bundle_id"],
                  bad_tag=None, bad_key=None, source_sha256=info.get("sha256"), release_notes=notes,
                  avail_tag=None, avail_notes=None)
    if keep:
        if app.get("update_pending") and (dest.parent / "previous.ipa").exists():
            keep.unlink(missing_ok=True)      # la version précédente reste la dernière installée avec succès
        else:
            replace_retry(keep, dest.parent / "previous.ipa")
            fields.update(prev_tag=app["source_tag"], prev_key=app.get("source_key"), prev_version=app["version"])
        fields["update_pending"] = 1
    db.update("apps", app["id"], **fields)
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


def _latest(app: dict) -> dict | None:
    prefixes, prereleases = ipa.release_rule(app)
    return ipa.latest_github(app["source"], prefixes, prereleases, app.get("asset_name"))


def _newer(app: dict, found: dict | None) -> bool:
    """La release trouvée diffère-t-elle de celle installée (autre tag, ou même tag dont l'IPA a été
    remplacée) et n'a-t-elle pas déjà été refusée ?"""
    if not found:
        return False
    tag, key = found["tag"], found.get("key")
    if _rejected(app, tag, key):
        return False
    if tag != app["source_tag"]:
        return True
    return bool(key) and app.get("source_key") not in (None, key)


async def new_release(app: dict) -> bool:
    """Vrai si la source GitHub publie une version différente de celle installée et pas déjà refusée.
    Les liens ne sont relus qu'au renouvellement (refresh_source)."""
    if app["source_type"] != "github":
        return False
    try:
        found = await asyncio.to_thread(_latest, app)
    except Exception:  # noqa: BLE001 - GitHub indisponible : on réessaiera
        return False
    return _newer(app, found)


async def check_available(app: dict) -> None:
    """Apps sans mise à jour automatique : mémorise la version disponible (pastille et notes dans
    l'interface) et prévient une fois par nouvelle version."""
    if app["source_type"] != "github":
        return
    try:
        found = await asyncio.to_thread(_latest, app)
    except Exception:  # noqa: BLE001
        return
    if _newer(app, found):
        if app.get("avail_tag") != found["tag"]:
            db.update("apps", app["id"], avail_tag=found["tag"], avail_notes=found.get("notes"))
            db.log(f"{app['name']} : version {found['tag']} disponible (mises à jour automatiques désactivées)",
                   app_id=app["id"])
            await notify(notifier.text("update_available", name=app["name"], tag=found["tag"]), "default",
                         "update", app=app["name"])
    elif app.get("avail_tag"):
        db.update("apps", app["id"], avail_tag=None, avail_notes=None)


# ---------- renouvellement ----------

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


def _retry_at(app: dict, now: float, transient: bool = False) -> float:
    """Prochain essai : plus rapproché quand l'expiration approche (ou pour une panne passagère :
    anisette, réseau), jamais après elle."""
    delay = RETRY_MINUTES * 60
    if transient:
        delay = min(delay, 15 * 60)
    exp = app["expires_at"]
    if exp and exp - now < 12 * 3600:
        delay = min(delay, 10 * 60)
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
    try:
        return ",".join(sorted(ipa.profile_cert_serials(signed))) or None
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


async def _renew(app_id: int, trigger: str) -> None:
    app = db.row("SELECT * FROM apps WHERE id = ?", app_id)
    if not app:
        return  # app supprimée entre-temps
    if trigger == "auto" and not app["enabled"]:
        return  # mise en pause pendant que la boucle traitait une autre app
    started = time.time()
    db.update("apps", app_id, status="running", last_error=None, error_kind=None, last_attempt_at=started,
              last_trigger=trigger)
    stage = None            # étape de contenu atteinte : signing, installing (voir le retour arrière)
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
        if not device["udid"] or not device.get("os_version"):
            # première connexion, ou tvOS de la TV pas encore connue (contrôle de compatibilité de l'IPA)
            await devices.refresh_info(device["id"])
            device = db.row("SELECT * FROM devices WHERE id = ?", device["id"])
        else:
            await devices.check_reachable(device)

        updated = await refresh_source(app, force=trigger in ("mise à jour", "nouvelle source"))
        fresh = db.row("SELECT * FROM apps WHERE id = ?", app_id) or app
        pending = bool(fresh.get("update_pending"))     # version téléchargée, jamais installée avec succès
        if trigger == "nouvelle version" and not updated and not pending and app["status"] == "ok":
            # téléchargement raté ou version refusée : inutile de re-signer l'ancienne version
            db.update("apps", app_id, status="ok", last_error=None)
            _OUTCOME[app_id] = ("skipped", None)
            return
        original = app_dir(app_id) / "original.ipa"
        signed = app_dir(app_id) / "signed.ipa"
        if not original.exists():
            raise RuntimeError("IPA d'origine absente : réimporte l'app")

        # 2. Signature (réutilise une signature récente jamais installée, sauf demande manuelle).
        force = trigger in ("manuel", "nouvelle destination", "retour arrière")
        expires = None if force else _reusable_signature(app, original, signed, time.time())
        if expires:
            db.log(f"{app['name']} : signature du {_fmt(signed.stat().st_mtime)} réutilisée", app_id=app_id)
        else:
            stage = "signing"
            _phase(app_id, "signing")
            await plume.ensure_device(email, device["udid"], device["name"])
            db.log(f"{app['name']} : signature avec {email}", app_id=app_id)
            if await plume.sign(email, original, signed):
                msg = notifier.text("cert_revoked", "fr", email=email, name=app["name"])
                db.log(msg, "warn", app_id=app_id)
                # bandeau durable dans l'interface (pas seulement une notification, qui peut ne pas être configurée)
                settings.raw_set("revocation_notice", json.dumps({"email": email, "app": app["name"], "at": time.time()}))
                await notify(notifier.text("cert_revoked", email=email, name=app["name"]), "default", "account",
                             app=app["name"])
                # les autres apps tvsign du compte, signées avec le certificat révoqué, sont à refaire
                # tout de suite plutôt qu'au prochain contrôle périodique
                await _guarded("contrôle des certificats", _check_certificates(email))
            expires = ipa.profile_expiry(signed) or time.time() + 7 * 86400

        # 3. Installation. Si l'app est ouverte sur la TV, installd attend qu'elle soit fermée :
        # l'interface le signale, et on abandonne au bout de APP_WAIT_MINUTES. Une mise à jour facultative
        # (l'app installée reste valable plus de 24 h) n'attend que UPDATE_WAIT_MINUTES.
        stage = "installing"
        db.log(f"{app['name']} : installation sur « {device['name']} »", app_id=app_id)
        optional = (trigger in ("nouvelle version", "mise à jour") and app["status"] == "ok"
                    and bool(app["expires_at"]) and app["expires_at"] - time.time() > 24 * 3600)
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

        fields = dict(status="ok", signed_at=time.time(), expires_at=expires, next_attempt_at=None,
                      last_error=None, error_kind=None, notified_level=None, update_pending=0,
                      cert_serial=_serials(signed))
        cur = db.row("SELECT account_email FROM apps WHERE id = ?", app_id)
        if cur and not cur["account_email"]:
            # fige le compte : un autre compte = autre équipe = autre bundle ID (2e copie).
            # Pas d'écrasement d'un compte choisi pendant l'opération.
            fields["account_email"] = email
        db.update("apps", app_id, **fields)
        _OUTCOME[app_id] = ("ok", None)
        _UPDATE_FAILS.pop(app_id, None)
        db.log(f"{app['name']} : renouvelée ({trigger}), valable jusqu'au {_fmt(expires)}, "
               f"prochain renouvellement auto le {_fmt(expires - settings.renew_before_hours() * 3600)}",
               app_id=app_id)
        if pending:
            notes = (fresh.get("release_notes") or "").strip()
            db.log(f"{app['name']} : version {fresh['version']} installée", app_id=app_id)
            await notify(notifier.text("update_installed", name=app["name"], version=fresh["version"],
                                       notes=("\n\n" + notes[:600]) if notes else ""),
                         "default", "update", app=app["name"], version=fresh["version"])
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
            db.update("apps", app_id, status="error", last_error="renouvellement trop long, abandonné",
                      next_attempt_at=_retry_at(app, time.time()))
        raise
    except Exception as e:  # noqa: BLE001 - l'erreur est remontée dans l'interface
        now = time.time()
        row = db.row("SELECT * FROM apps WHERE id = ?", app_id) or app
        unreachable = isinstance(e, devices.Unreachable)
        transient = isinstance(e, (plume.Transient, devices.AppInUse)) or unreachable
        session = isinstance(e, plume.SessionExpired)
        far = bool(row["expires_at"]) and row["expires_at"] - now > 24 * 3600
        if trigger == "nouvelle version" and app["status"] == "ok" and far and (transient or session):
            # TV en veille, app ouverte, anisette ou session : l'app installée reste valable, la mise à jour
            # attend. Ni carte rouge ni notification d'échec avant que l'expiration approche. Une mise à jour
            # demandée à la main (« mise à jour ») ne passe pas ici : l'échec doit se voir.
            db.update("apps", app_id, status="ok", last_error=None, error_kind=None,
                      next_attempt_at=_retry_at(app, now, True))
            db.log(f"{app['name']} : mise à jour en attente, {e}", "warn", app_id=app_id)
            _OUTCOME[app_id] = ("skipped", str(e))
            return
        _OUTCOME[app_id] = ("error", str(e))
        if (row.get("update_pending") and stage and isinstance(e, ContentError) and not transient and not session):
            # une version téléchargée dont l'IPA ne se signe ou ne s'installe pas : après quelques essais, on
            # revient à la précédente plutôt que de rejouer le même échec jusqu'à l'expiration. Les pannes
            # sans rapport avec l'IPA (limite d'App ID, TV pleine ou absente…) ne comptent jamais.
            _UPDATE_FAILS[app_id] = _UPDATE_FAILS.get(app_id, 0) + 1
            if _UPDATE_FAILS[app_id] >= ROLLBACK_AFTER and await _auto_rollback(app_id, str(e)):
                return
        kind = "transient" if transient else "session" if session else "other"
        db.update("apps", app_id, status="error", last_error=str(e), error_kind=kind,
                  next_attempt_at=_retry_at(app, now, transient))
        db.log(f"{app['name']} : échec, {e}", "error", app_id=app_id)
        # une panne banale (TV en veille, app ouverte, anisette) loin de l'expiration ne prévient pas :
        # les niveaux 24h / 6h / expiré, eux, notifient toujours
        await alert(db.row("SELECT * FROM apps WHERE id = ?", app_id) or app, str(e), now, quiet_fail=transient)


def restore_previous(app_id: int, pin: bool = False) -> dict | None:
    """Revient à la version précédente conservée (previous.ipa) ; renvoie l'ancienne fiche, ou None
    s'il n'y a rien à restaurer. pin : mises à jour automatiques coupées, la version écartée reste
    proposée comme « disponible » ; sinon elle est marquée refusée."""
    app = db.row("SELECT * FROM apps WHERE id = ?", app_id)
    folder = app_dir(app_id)
    if not app or not app.get("prev_tag") or not (folder / "previous.ipa").exists():
        return None
    replace_retry(folder / "previous.ipa", folder / "original.ipa")
    (folder / "signed.ipa").unlink(missing_ok=True)       # signée à partir de la version écartée
    fields = dict(source_tag=app["prev_tag"], source_key=app["prev_key"], version=app["prev_version"],
                  update_pending=0, prev_tag=None, prev_key=None, prev_version=None, release_notes=None,
                  next_attempt_at=None, bad_tag=None if pin else app["source_tag"],
                  bad_key=None if pin else app["source_key"],
                  # la version écartée reste proposée (« Installer maintenant » la retente même refusée)
                  avail_tag=app["source_tag"], avail_notes=app.get("release_notes"))
    if pin:
        fields.update(auto_update=0)
    db.update("apps", app_id, **fields)
    _UPDATE_FAILS.pop(app_id, None)
    return app


async def _auto_rollback(app_id: int, why: str) -> bool:
    old = restore_previous(app_id)
    if not old:
        return False
    msg = f"version {old['source_tag']} annulée ({why}), retour à la version {old['prev_version'] or old['prev_tag']}"
    db.update("apps", app_id, status="error", last_error=msg, error_kind="other")
    db.log(f"{old['name']} : {msg}", "warn", app_id=app_id)
    await notify(notifier.text("update_rolled_back", name=old["name"], tag=old["source_tag"], why=why,
                               prev=old["prev_version"] or old["prev_tag"]), "default", "update", app=old["name"])
    return True


def rollback(app_id: int) -> bool:
    """Retour arrière demandé par l'utilisateur : restaure la version précédente, coupe les mises à
    jour automatiques de l'app et relance une signature. False si rien à restaurer."""
    if not restore_previous(app_id, pin=True):
        return False
    db.log("Retour à la version précédente (mises à jour automatiques désactivées)", app_id=app_id)
    spawn(app_id, "retour arrière")
    return True


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
            await notify(notifier.text("cert_revoked_other", name=app["name"]), "high", "account", app=app["name"])


async def _check_accounts() -> None:
    for acc in plume.accounts():
        email = acc["email"]
        ok = await plume.check_account(email)       # ne lève pas : None si indéterminé
        if ok is False:
            if email not in _ACC_ALERTED:
                _ACC_ALERTED.add(email)
                db.log(f"Compte Apple {email} déconnecté : reconnexion nécessaire", "error")
                await notify(notifier.text("account_lost", email=email), "high", "account")
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
        if not app["cert_serial"]:
            fields["cert_serial"] = _serials(signed)
        db.update("apps", app["id"], **fields)


def health(now: float | None = None) -> dict:
    now = now or time.time()
    last = HEALTH["last_loop_at"]
    stalled = bool(last) and now - last > 3 * CHECK_INTERVAL + RENEW_TIMEOUT_MINUTES * 60
    return {**HEALTH, "notify_error": notifier.last_error(), "notifications": notifier.configured(),
            "loop_stalled": stalled,
            "check_interval": CHECK_INTERVAL, "retry_minutes": RETRY_MINUTES,
            "release_check_minutes": RELEASE_CHECK_MINUTES, "account_check_hours": ACCOUNT_CHECK_HOURS}


async def _guarded(what: str, coro) -> None:
    try:
        await coro
    except Exception as e:  # noqa: BLE001 - un échec isolé ne doit pas bloquer le reste
        HEALTH["loop_error"] = f"{what} : {e}"
        _log_quietly(f"{what} : {e}")


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
                elif check_releases and idle(app, now):
                    if not app.get("auto_update", 1):
                        await _guarded(f"version disponible de {app['name']}", check_available(app))
                    elif app.get("update_pending") or await new_release(app):
                        # (update_pending : version déjà téléchargée, installation différée par une panne banale)
                        await _guarded(f"renouvellement de {app['name']}", renew(app["id"], "nouvelle version"))
            await _check_expired(time.time())
        except Exception as e:  # noqa: BLE001 - la boucle ne doit jamais s'arrêter
            HEALTH["loop_error"] = str(e)
            _log_quietly(f"boucle de renouvellement : {e}", "error")
        HEALTH["next_check_at"] = time.time() + CHECK_INTERVAL
        await asyncio.sleep(CHECK_INTERVAL)


def _log_quietly(message: str, level: str = "error") -> None:
    """Journal sans jamais lever : appelé depuis un gestionnaire d'erreur, souvent parce que la base
    est verrouillée ou pleine, et une exception de plus tuerait la boucle de renouvellement."""
    try:
        db.log(message, level)
    except Exception as e:  # noqa: BLE001
        print(f"[{level}] {message} (journal indisponible : {e})", flush=True)


async def supervise() -> None:
    """Fait tourner loop() sans jamais la laisser mourir en silence : une exception qui lui
    échapperait est journalisée et la boucle redémarre après une courte pause."""
    while True:
        try:
            await loop()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            HEALTH["loop_error"] = str(e)
            _log_quietly(f"boucle de renouvellement arrêtée, redémarrage : {e}")
            await asyncio.sleep(30)


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
