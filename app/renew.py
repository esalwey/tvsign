"""Renouvellement : mise à jour de la source, signature, installation, et boucle de planification."""
import asyncio
import json
import time
import urllib.request

from . import db, devices, ipa, plume, proc, settings
from .config import (ACCOUNT_CHECK_HOURS, APP_WAIT_MINUTES, CHECK_INTERVAL, IPA_DIR,
                     RELEASE_CHECK_MINUTES, RENEW_TIMEOUT_MINUTES, RETRY_MINUTES)

_RUNNING: set[int] = set()
# Étape du renouvellement en cours, par app, pour l'interface :
# {"step": checking|signing|uploading|installing|waiting_app, "since": ts, ...}
PHASE: dict[int, dict] = {}
_RERUN: set[int] = set()                # destination changée pendant un renouvellement : on relance après
_ACC_ALERTED: set[str] = set()          # comptes déjà signalés déconnectés (une alerte par épisode)

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
    return time.strftime("%d/%m %H:%M", time.localtime(ts))


# ---------- notifications ----------

def _send(message: str, priority: str, url: str | None = None) -> None:
    req = urllib.request.Request(url or settings.ntfy_url(), data=message.encode("utf-8"),
                                 headers={"Title": "tvsign", "Priority": priority})
    urllib.request.urlopen(req, timeout=10).close()


async def notify_test(url: str | None = None) -> None:
    """Notification d'essai depuis les Réglages ; lève l'erreur pour l'afficher."""
    target = url or settings.ntfy_url()
    if not target:
        raise RuntimeError("aucune adresse de notification")
    await asyncio.to_thread(_send, "tvsign : notification de test", "default", target)
    HEALTH["notify_error"] = None


async def notify(message: str, priority: str = "default") -> None:
    """Envoi ntfy hors de la boucle d'événements ; un échec est journalisé une fois."""
    if not settings.ntfy_url():
        return
    try:
        await asyncio.to_thread(_send, message, priority)
        HEALTH["notify_error"] = None
    except Exception as e:  # noqa: BLE001 - la notification est accessoire
        if HEALTH["notify_error"] is None:
            db.log(f"notification ntfy échouée : {e}", "warn")
        HEALTH["notify_error"] = str(e)


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


async def alert(app: dict, reason: str, now: float) -> None:
    lvl = _level(app, now)
    prev = app.get("notified_level")
    if prev in _LEVELS and _LEVELS.index(lvl) <= _LEVELS.index(prev):
        return  # déjà notifié à ce niveau
    db.update("apps", app["id"], notified_level=lvl)
    if lvl == "fail":
        await notify(f"{app['name']} : renouvellement échoué, {reason}")
    elif lvl == "expired":
        await notify(f"{app['name']} : profil EXPIRÉ, l'app ne se lance plus. {reason}", "urgent")
    else:
        await notify(f"{app['name']} expire dans moins de {lvl[:-1]} h et le renouvellement échoue : {reason}", "high")


# ---------- source ----------

async def refresh_source(app: dict) -> bool:
    """Télécharge une nouvelle version si la source en publie une ; True si l'IPA a changé.
    Si une IPA utilisable existe déjà, un problème de source ne bloque pas le renouvellement."""
    if app["source_type"] == "upload":
        return False
    dest = app_dir(app["id"]) / "original.ipa"
    tag, sha256 = None, None
    try:
        if app["source_type"] == "github":
            found = await asyncio.to_thread(_latest, app)
            if not found:
                if not dest.exists():
                    channel = "stable" if app.get("channel") != "beta" else "beta"
                    prefixes, _ = ipa.release_rule(app)
                    which = f", tags {' / '.join(x + '*' for x in prefixes)}" if prefixes else ""
                    raise RuntimeError(f"aucune IPA trouvée dans les releases ({channel}{which}) de {app['source']}")
                return False
            tag, url, sha256 = found["tag"], found["url"], found["sha256"]
        else:
            tag, url = app["source"], app["source"]
        if dest.exists() and tag in (app["source_tag"], app.get("bad_tag")):
            return False
        db.log(f"{app['name']} : téléchargement de {tag}"
               + (" (empreinte SHA-256 vérifiée)" if sha256 else ""), app_id=app["id"])
        # dest remplacé seulement si valide ; un bundle ID différent de l'app suivie est refusé
        expect = app.get("bundle_id") if dest.exists() else None
        info = await asyncio.to_thread(ipa.download, url, dest, sha256, expect)
    except Exception as e:  # noqa: BLE001
        if not dest.exists():
            raise
        # SourceRejected (IPA invalide, empreinte fausse, bundle ID changé) est un ValueError
        if tag and isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError):
            # IPA invalide (pas une IPA, pas tvOS) : ne pas la retélécharger à chaque essai.
            # Le reste (coupure, réponse tronquée, JSON GitHub illisible) est passager : on réessaiera.
            db.update("apps", app["id"], bad_tag=tag)
            db.log(f"{app['name']} : version {tag} inutilisable ({e}), version précédente conservée",
                   "warn", app_id=app["id"])
            await notify(f"{app['name']} : nouvelle version {tag} inutilisable ({e}), version précédente conservée")
        else:
            db.log(f"{app['name']} : source indisponible ({e}), version précédente conservée",
                   "warn", app_id=app["id"])
        return False
    db.update("apps", app["id"], source_tag=tag, version=info["version"], bundle_id=info["bundle_id"],
              bad_tag=None, source_sha256=info.get("sha256"))
    return True


def _latest(app: dict) -> dict | None:
    prefixes, prereleases = ipa.release_rule(app)
    return ipa.latest_github(app["source"], prefixes, prereleases, app.get("asset_name"))


async def new_release(app: dict) -> bool:
    """Vrai si la source GitHub publie un tag différent de celui installé (et pas déjà refusé)."""
    if app["source_type"] != "github":
        return False
    try:
        found = await asyncio.to_thread(_latest, app)
    except Exception:  # noqa: BLE001 - GitHub indisponible : on réessaiera
        return False
    return bool(found) and found["tag"] not in (app["source_tag"], app.get("bad_tag"))


# ---------- renouvellement ----------

async def renew(app_id: int, trigger: str = "manuel") -> None:
    if app_id in _RUNNING:
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
    if app_id in _RUNNING:
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


async def _renew(app_id: int, trigger: str) -> None:
    app = db.row("SELECT * FROM apps WHERE id = ?", app_id)
    if not app:
        return  # app supprimée entre-temps
    started = time.time()
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
        if app.get("deferred_reason"):
            db.update("apps", app_id, deferred_reason=None, deferred_since=None)

        updated = await refresh_source(app)
        if trigger == "nouvelle version" and not updated and app["status"] == "ok":
            # téléchargement raté ou version refusée : inutile de re-signer l'ancienne version
            db.update("apps", app_id, status="ok", last_error=None, deferred_reason=None, deferred_since=None)
            return
        original = app_dir(app_id) / "original.ipa"
        signed = app_dir(app_id) / "signed.ipa"
        if not original.exists():
            raise RuntimeError("IPA d'origine absente : réimporte l'app")

        # 2. Signature (réutilise une signature récente jamais installée, sauf demande manuelle).
        force = trigger in ("manuel", "nouvelle destination")
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

        fields = dict(status="ok", signed_at=time.time(), expires_at=expires, next_attempt_at=None,
                      last_error=None, notified_level=None, cert_serial=_serials(signed),
                      deferred_reason=None, deferred_since=None)
        cur = db.row("SELECT account_email FROM apps WHERE id = ?", app_id)
        if cur and not cur["account_email"]:
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
        db.update("apps", app_id, status="error", last_error="renouvellement interrompu",
                  next_attempt_at=_retry_at(app, time.time()))
        raise
    except Exception as e:  # noqa: BLE001 - l'erreur est remontée dans l'interface
        now = time.time()
        transient = isinstance(e, (plume.Transient, devices.AppInUse))
        db.update("apps", app_id, status="error", last_error=str(e),
                  next_attempt_at=_retry_at(app, now, transient))
        db.log(f"{app['name']} : échec, {e}", "error", app_id=app_id)
        await alert(db.row("SELECT * FROM apps WHERE id = ?", app_id) or app, str(e), now)


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
            db.update("apps", app["id"], expires_at=0, next_attempt_at=None)
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
    for app in db.rows("SELECT * FROM apps WHERE enabled = 1 AND expires_at > 0 AND expires_at < ?", now):
        if app["notified_level"] != "expired":
            db.log(f"{app['name']} : profil expiré le {_fmt(app['expires_at'])}", "error", app_id=app["id"])
            await alert(app, app["last_error"] or "renouvellement pas encore réussi", now)


def startup() -> None:
    """Au démarrage : rien ne peut être « en cours », et on corrige les dates déjà enregistrées."""
    db.execute("UPDATE apps SET status = 'pending' WHERE status = 'running'")
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
                now = time.time()
                if due(app, now):
                    await _guarded(f"renouvellement de {app['name']}", renew(app["id"], "auto"))
                elif check_releases and idle(app, now) and await new_release(app):
                    await _guarded(f"renouvellement de {app['name']}", renew(app["id"], "nouvelle version"))
            await _check_expired(time.time())
        except Exception as e:  # noqa: BLE001 - la boucle ne doit jamais s'arrêter
            HEALTH["loop_error"] = str(e)
            db.log(f"boucle de renouvellement : {e}", "error")
        HEALTH["next_check_at"] = time.time() + CHECK_INTERVAL
        await asyncio.sleep(CHECK_INTERVAL)
