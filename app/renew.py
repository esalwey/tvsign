"""Renouvellement : mise à jour de la source, signature, installation, et boucle de planification."""
import asyncio
import base64
import json
import shutil
import time
import urllib.parse
import urllib.request

from . import db, devices, ipa, plume, proc, settings
from .config import (ACCOUNT_CHECK_HOURS, APP_WAIT_MINUTES, APPLE_BACKOFF_MINUTES, CHECK_INTERVAL, IPA_DIR,
                     RELEASE_CHECK_MINUTES, RENEW_TIMEOUT_MINUTES, RETRY_MINUTES)

# Arrêt du service en cours : plus aucun renouvellement ne démarre, ceux en cours peuvent finir.
STOPPING = False

_RUNNING: set[int] = set()
# Étape du renouvellement en cours, par app, pour l'interface :
# {"step": checking|signing|uploading|installing|waiting_app, "since": ts, ...}
PHASE: dict[int, dict] = {}
_RERUN: set[int] = set()                # destination changée pendant un renouvellement : on relance après
_EXPIRED_LOGGED: set[int] = set()       # apps dont l'expiration est déjà au journal (une fois par épisode)
_NO_MATCH_LOGGED: set[int] = set()      # apps dont l'absence de release correspondante est déjà au journal

# État de la boucle, exposé dans /api/state pour l'interface.
HEALTH: dict = {
    "started_at": time.time(),
    "last_loop_at": None,               # dernier passage de la boucle
    "next_check_at": None,              # prochain passage prévu
    "loop_error": None,                 # dernière erreur inattendue de la boucle
    "last_account_check_at": None,
    "last_release_check_at": None,
    "notify_error": None,               # dernier échec d'envoi ntfy
    "restarts": 0,                      # relances de la boucle par le superviseur
}
TASK: asyncio.Task | None = None        # tâche de la boucle supervisée (voir start())
_BG: dict[str, asyncio.Task] = {}       # contrôles de fond : comptes, nouvelles versions


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
    """Cette release (ce contenu) a-t-elle déjà été refusée ?"""
    return tag == app.get("bad_tag") and (key is None or app.get("bad_key") in (None, key))


async def refresh_source(app: dict) -> bool:
    """Télécharge une nouvelle version si la source en publie une ; True si l'IPA a changé.
    Si une IPA utilisable existe déjà, un problème de source ne bloque pas le renouvellement."""
    if app["source_type"] == "upload":
        return False
    dest = app_dir(app["id"]) / "original.ipa"
    tag, key, sha256 = None, None, None
    try:
        if app["source_type"] == "github":
            found = await asyncio.to_thread(_latest, app)
            if not found:
                if not dest.exists():
                    channel = "stable" if app.get("channel") != "beta" else "beta"
                    prefixes, _ = ipa.release_rule(app)
                    which = f", tags {' / '.join(x + '*' for x in prefixes)}" if prefixes else ""
                    hint = await _beta_hint(app) if channel == "stable" else ""
                    raise RuntimeError(f"aucune IPA trouvée dans les releases ({channel}{which}) de "
                                       f"{app['source']}{hint}")
                if app["id"] not in _NO_MATCH_LOGGED:
                    _NO_MATCH_LOGGED.add(app["id"])
                    db.log(f"{app['name']} : aucune release ne correspond plus dans {app['source']} "
                           "(tags renommés, IPA absente ou release trop ancienne ?), version actuelle conservée",
                           "warn", app_id=app["id"])
                return False
            _NO_MATCH_LOGGED.discard(app["id"])
            tag, url, sha256, key = found["tag"], found["url"], found["sha256"], found.get("key")
        else:
            tag = url = app["source"]
            key = await asyncio.to_thread(ipa.url_key, url)
        if dest.exists():
            if _rejected(app, tag, key):
                return False
            if _unchanged(app, tag, key):
                if key and app.get("source_key") is None:
                    db.update("apps", app["id"], source_key=key)   # première lecture de la clé (ancienne base)
                return False
        db.log(f"{app['name']} : téléchargement de {tag}"
               + (" (fichier mis à jour sous le même nom)" if tag == app["source_tag"] and dest.exists() else "")
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
            db.update("apps", app["id"], bad_tag=tag, bad_key=key)
            db.log(f"{app['name']} : version {tag} inutilisable ({e}), version précédente conservée",
                   "warn", app_id=app["id"])
            await notify(f"{app['name']} : nouvelle version {tag} inutilisable ({e}), version précédente conservée")
        else:
            db.log(f"{app['name']} : source indisponible ({e}), version précédente conservée",
                   "warn", app_id=app["id"])
        return False
    db.update("apps", app["id"], source_tag=tag, source_key=key, version=info["version"],
              bundle_id=info["bundle_id"], bad_tag=None, bad_key=None, source_sha256=info.get("sha256"))
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
    return ipa.latest_github(app["source"], prefixes, prereleases, app.get("asset_name"))


async def new_release(app: dict) -> bool:
    """Vrai si la source GitHub publie une version différente de celle installée (autre tag, ou
    même tag dont l'IPA a été remplacée) et pas déjà refusée. Les liens ne sont relus qu'au
    renouvellement (refresh_source)."""
    if app["source_type"] != "github":
        return False
    try:
        found = await asyncio.to_thread(_latest, app)
    except Exception:  # noqa: BLE001 - GitHub indisponible : on réessaiera
        return False
    if not found:
        return False
    tag, key = found["tag"], found.get("key")
    if _rejected(app, tag, key):
        return False
    if tag != app["source_tag"]:
        return True
    return bool(key) and app.get("source_key") not in (None, key)


# ---------- renouvellement ----------

class NeedsAction(RuntimeError):
    """Échec qu'un nouvel essai ne réparera pas : une action de l'utilisateur est nécessaire.
    kind : session (reconnecter le compte), config (Apple TV, compte ou IPA à corriger)."""

    def __init__(self, message: str, kind: str = "config"):
        super().__init__(message)
        self.kind = kind


# Délai avant de réessayer un échec qui attend une action de l'utilisateur : l'action (reconnexion,
# appairage, modification de l'app, renouvellement manuel) relance tout de suite, ceci n'est qu'un filet
# pour une réparation faite ailleurs. Un renouvellement qui échoue sur la limite du compte gratuit
# consomme des App ID : on laisse plus de temps.
BLOCKED_RETRY_HOURS = {"session": 6, "pairing": 6, "config": 6, "limit": 24}


def failure_kind(exc: BaseException) -> str | None:
    """Action requise (session | pairing | limit | config) ou None si un nouvel essai a une chance de réussir."""
    if isinstance(exc, NeedsAction):
        return exc.kind
    if isinstance(exc, plume.SessionExpired):
        return "session"
    if isinstance(exc, devices.PairingLost):
        return "pairing"
    if isinstance(exc, plume.LimitReached):
        return "limit"
    return None


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


def failure_fields(app: dict, now: float, exc: BaseException, transient: bool) -> dict:
    """Champs à enregistrer après un échec : compteur d'échecs d'affilée, action requise, prochain essai."""
    fails = int(app.get("fail_count") or 0) + 1
    kind = failure_kind(exc)
    slow = isinstance(exc, plume.AppleRefused) and exc.slow
    return dict(fail_count=fails, needs_action=kind,
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


def _serials(signed) -> str | None:
    """Tous les certificats du profil (repli quand on ne sait pas lequel a signé)."""
    try:
        return ",".join(sorted(ipa.profile_cert_serials(signed))) or None
    except Exception:  # noqa: BLE001
        return None


def _signing_serial(signed, email: str | None) -> str | None:
    """Certificat qui a réellement signé l'app (clé privée de tvsign), sinon à défaut ceux du profil."""
    pem = plume.signing_key(email) if email else None
    return (ipa.signing_serial(signed, pem) if pem else None) or _serials(signed)


def _phase(app_id: int, step: str, **info) -> None:
    prev = PHASE.get(app_id)
    since = prev["since"] if prev and prev["step"] == step else time.time()
    PHASE[app_id] = {"step": step, "since": since, **info}


def forget(app_id: int) -> None:
    """App supprimée : oublie son état en mémoire (l'identifiant SQLite peut être réattribué)."""
    for s in (_EXPIRED_LOGGED, _NO_MATCH_LOGGED, _RERUN):
        s.discard(app_id)
    PHASE.pop(app_id, None)


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
            except Exception as e:  # noqa: BLE001 - une tâche de fond ne doit jamais mourir en silence
                HEALTH["loop_error"] = f"renouvellement : {e}"
                db.log(f"renouvellement de l'app {app_id} : {e}", "error", app_id=app_id)
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


def clear_block(app_id: int) -> None:
    """L'utilisateur a agi (destination, source, renouvellement manuel) : on repart de zéro."""
    db.update("apps", app_id, next_attempt_at=None, needs_action=None, fail_count=0)


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

        updated = await refresh_source(app)
        if trigger == "nouvelle version" and not updated and app["status"] == "ok":
            # téléchargement raté ou version refusée : inutile de re-signer l'ancienne version
            db.update("apps", app_id, status="ok", last_error=None)
            return
        original = app_dir(app_id) / "original.ipa"
        signed = app_dir(app_id) / "signed.ipa"
        if not original.exists():
            raise NeedsAction("IPA d'origine absente : réimporte l'app", "config")

        # 2. Signature (réutilise une signature récente jamais installée, sauf demande manuelle).
        force = trigger in ("manuel", "nouvelle destination")
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
        db.log(f"{app['name']} : installation sur « {device['name']} »", app_id=app_id, journal=not retry)
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

        cur = db.row("SELECT account_email, version, source_tag FROM apps WHERE id = ?", app_id) or {}
        fields = dict(status="ok", signed_at=time.time(), expires_at=expires, next_attempt_at=None,
                      last_error=None, notified_level=None, fail_count=0, needs_action=None,
                      cert_serial=_signing_serial(signed, email),
                      # ce qui tourne maintenant sur la TV (« version » est la dernière téléchargée)
                      installed_version=cur.get("version"), installed_tag=cur.get("source_tag"))
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
        if STOPPING:
            # arrêt normal (redémarrage, mise à jour) : pas une erreur, on reprendra au démarrage
            db.update("apps", app_id, status="pending", last_error=None, next_attempt_at=None)
        else:
            now = time.time()
            db.update("apps", app_id, status="error", last_error="renouvellement trop long, abandonné",
                      **failure_fields(app, now, TimeoutError(), False))
        raise
    except Exception as e:  # noqa: BLE001 - l'erreur est remontée dans l'interface
        e = _as_limit(e)
        now = time.time()
        unreachable = isinstance(e, devices.Unreachable)
        transient = isinstance(e, (plume.Transient, plume.Busy, devices.AppInUse)) or unreachable
        reconnect = isinstance(e, plume.SessionExpired)
        if unreachable and trigger == "nouvelle version" and app["status"] == "ok":
            # TV en veille : l'app installée reste valable, la mise à jour attend simplement la TV
            db.update("apps", app_id, status="ok", last_error=None, **failure_fields(app, now, e, True))
            db.log(f"{app['name']} : mise à jour en attente, {e}", "warn", app_id=app_id)
            return
        fields = failure_fields(app, now, e, transient)
        db.update("apps", app_id, status="error", last_error=str(e), **fields)
        db.log(f"{app['name']} : échec, {e}", "error", app_id=app_id)
        if reconnect:
            # une seule alerte pour le compte, pas une par app : les niveaux 24h / 6h / expiré restent notifiés
            await account_down(e.email or app.get("account_email") or plume.default_account(), plume.REAUTH_REASON)
        # une TV en veille loin de l'expiration est banale : pas de notification avant le niveau 24h
        await alert(db.row("SELECT * FROM apps WHERE id = ?", app_id) or app, str(e), now,
                    quiet_fail=unreachable or reconnect)


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
        db.update("apps", app["id"], next_attempt_at=None, needs_action=None, fail_count=0)
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
        serials = app["cert_serial"] or (_serials(signed) if signed.exists() else None)
        if serials and "," in serials and signed.exists():
            # ancienne valeur (tous les certificats du profil) : on retrouve celui qui a signé
            one = _signing_serial(signed, email)
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
            await notify(f"{app['name']} : certificat Apple révoqué (autre outil sur le même compte), "
                         "re-signature en cours", "high")


async def account_down(email: str | None, reason: str) -> None:
    """Compte à reconnecter : une alerte (journal + notification) par épisode, que la déconnexion soit
    découverte par le contrôle périodique ou par un renouvellement. L'état « Déconnecté » et sa raison
    sont posés par plume (account_status)."""
    if not email or email in plume.ALERTED:
        return
    plume.ALERTED.add(email)
    db.log(f"Compte Apple {email} déconnecté : reconnexion nécessaire ({reason})", "error")
    await notify(f"Compte Apple {email} déconnecté : reconnecte-le dans tvsign. {reason}", "high")


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
    return {**HEALTH, "notifications": bool(settings.ntfy_url()), "loop_stalled": stalled, "loop_alive": alive,
            "renewals_running": len(_RUNNING), "background": background,
            "check_interval": CHECK_INTERVAL, "retry_minutes": RETRY_MINUTES,
            "release_check_minutes": RELEASE_CHECK_MINUTES, "account_check_hours": ACCOUNT_CHECK_HOURS}


async def _guarded(what: str, coro) -> None:
    try:
        await coro
    except Exception as e:  # noqa: BLE001 - un échec isolé ne doit pas bloquer le reste
        HEALTH["loop_error"] = f"{what} : {e}"
        db.log(f"{what} : {e}", "error")


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
    for app in by_urgency([a for a in db.rows("SELECT * FROM apps") if due(a, now)], now):
        if STOPPING:
            break
        if spawn(app["id"], "auto"):
            launched.add(app["id"])
    # Les tâches lancées ci-dessus n'ont pas encore démarré (_RUNNING est vide pour elles) : sans cette
    # exclusion, une app expirée dont la re-signature démarre recevrait une alerte « EXPIRÉ » à tort.
    await _guarded("alertes d'expiration", _check_expired(time.time(), launched | set(_RUNNING)))
    return last_account_check, last_release_check


async def loop() -> None:
    last_account_check = last_release_check = 0.0
    while True:
        now = time.time()
        HEALTH["last_loop_at"] = now
        HEALTH["next_check_at"] = now + CHECK_INTERVAL
        HEALTH["loop_error"] = None
        try:
            last_account_check, last_release_check = await tick(now, last_account_check, last_release_check)
        except Exception as e:  # noqa: BLE001 - la boucle ne doit jamais s'arrêter
            HEALTH["loop_error"] = str(e)
            db.log(f"boucle de renouvellement : {e}", "error")
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
            db.log(f"boucle de renouvellement arrêtée, relancée : {e}", "error")
            await asyncio.sleep(SUPERVISE_DELAY)


def start() -> asyncio.Task:
    """Démarre la boucle supervisée ; health() surveille cette tâche."""
    global TASK
    TASK = asyncio.create_task(supervise())
    return TASK
