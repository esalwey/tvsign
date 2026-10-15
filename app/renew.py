"""Renouvellement : mise à jour de la source, signature, installation, et boucle de planification."""
import asyncio
import base64
import json
import time
import urllib.parse
import urllib.request

from . import db, devices, ipa, plume, proc, settings
from .config import (ACCOUNT_CHECK_HOURS, APP_WAIT_MINUTES, CHECK_INTERVAL, IPA_DIR, PRESENCE_MINUTES,
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
_ABSENT: set[int] = set()               # apps dont le dernier échec vient d'un appareil absent du réseau
_DEVMODE_LOGGED: set[int] = set()       # appareils dont le mode développeur désactivé est déjà au journal
PRESENCE_MIN_GAP = 10 * 60              # secondes entre deux essais déclenchés par la réapparition d'un appareil
REVOKE_WINDOW = 7 * 86400
REVOKE_LOOP = 3                         # révocations en 7 jours : un autre outil partage le compte

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


def _device_of(app: dict) -> dict | None:
    return db.row("SELECT * FROM devices WHERE id = ?", app["device_id"]) if app.get("device_id") else None


def before_hours(app: dict, device: dict | None = None) -> float:
    """Heures avant l'expiration où le renouvellement automatique commence, pour l'appareil de l'app."""
    return settings.renew_before_hours_for(device or _device_of(app))


def _download_args(device: dict | None) -> dict:
    """Arguments de plateforme pour ipa.download. Une Apple TV (ou aucun appareil) : aucun, le
    contrôle d'origine (IPA tvOS) s'applique tel quel."""
    if not device or device.get("family", "tvos") == "tvos":
        return {}
    return dict(family=device["family"], os_version=device.get("os_version"), model=device.get("model"))


def _older_than_installed(app: dict, found: dict) -> bool:
    """La release proposée est-elle plus ancienne que celle installée ? (publication retardée de la
    plus récente, asset effacé puis renvoyé : ne jamais revenir en arrière)."""
    have, new = app.get("source_published_at"), found.get("published_at")
    return bool(have and new and new < have)


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
    tag, key, sha256, published = None, None, None, None
    try:
        if app["source_type"] == "github":
            found = await asyncio.to_thread(_latest, app)
            if found and dest.exists() and _older_than_installed(app, found):
                return False        # la release la plus récente n'a pas (encore) d'IPA : on garde la version actuelle
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
            published = found.get("published_at")
            # plusieurs IPA dans la release (iOS + tvOS) : la suivante est essayée si la première ne convient pas
            candidates = found.get("candidates") or [{"url": url, "sha256": sha256, "key": key}]
        else:
            tag = url = app["source"]
            key = await asyncio.to_thread(ipa.url_key, url)
            candidates = [{"url": url, "sha256": None, "key": key}]
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
        extra = _download_args(_device_of(app))
        for i, cand in enumerate(candidates):
            try:
                info = await asyncio.to_thread(ipa.download, cand["url"], dest, cand.get("sha256"), expect, **extra)
                key = cand.get("key", key)
                break
            except ipa.PlatformRejected:
                if i == len(candidates) - 1:
                    raise      # aucune IPA de la release ne convient à cet appareil
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
              bundle_id=info["bundle_id"], bad_tag=None, bad_key=None, source_sha256=info.get("sha256"),
              source_published_at=published, **ipa_columns(info))
    return True


def app_ids_used(app: dict) -> int:
    """App IDs que l'app consomme sur le compte : un, plus un par extension (widget, partage…).
    Estimation tirée de l'IPA ; un compte gratuit en crée 10 par période de 7 jours."""
    n = 1
    for w in (app.get("warnings") or "").split(","):
        if w.startswith("appex:") and w[6:].isdigit():
            n += int(w[6:])
    return n


def ipa_columns(info: dict) -> dict:
    """Colonnes de l'app tirées de l'IPA (plateforme, familles d'appareils, limites du compte gratuit)."""
    return dict(platform=info.get("platform"),
                ui_family=",".join(str(n) for n in info.get("ui_family") or []) or None,
                warnings=",".join(info.get("warnings") or []) or None)


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
    device = _device_of(app)
    # famille de l'appareil : choisit l'IPA iOS ou tvOS quand une release publie les deux
    kw = {"family": device["family"]} if device and device.get("family") not in (None, "tvos") else {}
    return ipa.latest_github(app["source"], prefixes, prereleases, app.get("asset_name"), **kw)


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
    if _rejected(app, tag, key) or _older_than_installed(app, found):
        return False
    if tag != app["source_tag"]:
        return True
    return bool(key) and app.get("source_key") not in (None, key)


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
    if not exp or exp - now < (before_hours(app) + 24) * 3600:
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
        if devices.is_mobile(device):
            await _devmode_banner(device)

        updated = await refresh_source(app)
        if trigger == "nouvelle version" and not updated and app["status"] == "ok":
            # téléchargement raté ou version refusée : inutile de re-signer l'ancienne version
            db.update("apps", app_id, status="ok", last_error=None)
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
            await plume.ensure_device(email, device["udid"], device["name"],
                                      "ios" if devices.is_mobile(device) else None)
            db.log(f"{app['name']} : signature avec {email}", app_id=app_id)
            if await plume.sign(email, original, signed):
                msg = (f"un ancien certificat de {email} a été révoqué pour signer {app['name']} : "
                       "les autres apps sideloadées avec ce compte (iPhone…) vont cesser de fonctionner")
                if note_revocation(email) >= REVOKE_LOOP:
                    msg = (f"le certificat de {email} a été révoqué {REVOKE_LOOP} fois ou plus en 7 jours : un autre "
                           "outil (AltStore, SideStore, Sideloadly…) utilise le même Apple ID et les deux se "
                           "révoquent en boucle. Utilise un Apple ID dédié à tvsign")
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
                    if devices.is_mobile(device):
                        # iOS remplace une app ouverte : si l'installation attend, c'est autre chose
                        db.log(f"{app['name']} : installation en attente sur « {device['name']} » "
                               "(appareil verrouillé, occupé ou presque plein ?)", "warn", app_id=app_id)
                    else:
                        db.log(f"{app['name']} : installation en attente, l'app est sans doute ouverte sur "
                               f"« {device['name']} » ; elle se terminera dès sa fermeture", "warn", app_id=app_id)
            else:
                _phase(app_id, step, percent=percent)

        _phase(app_id, "uploading")
        await devices.install(device, [signed], on_event=on_event, wait_cap=wait_cap, app_name=app["name"])

        fields = dict(status="ok", signed_at=time.time(), expires_at=expires, next_attempt_at=None,
                      last_error=None, notified_level=None, cert_serial=_serials(signed))
        cur = db.row("SELECT account_email FROM apps WHERE id = ?", app_id)
        if cur and not cur["account_email"]:
            # fige le compte : un autre compte = autre équipe = autre bundle ID (2e copie).
            # Pas d'écrasement d'un compte choisi pendant l'opération.
            fields["account_email"] = email
        db.update("apps", app_id, **fields)
        _ABSENT.discard(app_id)
        db.log(f"{app['name']} : renouvelée ({trigger}), valable jusqu'au {_fmt(expires)}, "
               f"prochain renouvellement auto le {_fmt(expires - before_hours(app, device) * 3600)}",
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
                      next_attempt_at=_retry_at(app, time.time()))
        raise
    except Exception as e:  # noqa: BLE001 - l'erreur est remontée dans l'interface
        now = time.time()
        unreachable = isinstance(e, devices.Unreachable)
        (_ABSENT.add if unreachable else _ABSENT.discard)(app_id)
        transient = isinstance(e, (plume.Transient, devices.AppInUse)) or unreachable
        if unreachable and trigger == "nouvelle version" and app["status"] == "ok":
            # TV en veille : l'app installée reste valable, la mise à jour attend simplement la TV
            db.update("apps", app_id, status="ok", last_error=None, next_attempt_at=_retry_at(app, now, True))
            db.log(f"{app['name']} : mise à jour en attente, {e}", "warn", app_id=app_id)
            return
        db.update("apps", app_id, status="error", last_error=str(e),
                  next_attempt_at=_retry_at(app, now, transient))
        db.log(f"{app['name']} : échec, {e}", "error", app_id=app_id)
        # une TV en veille loin de l'expiration est banale : pas de notification avant le niveau 24h
        await alert(db.row("SELECT * FROM apps WHERE id = ?", app_id) or app, str(e), now, quiet_fail=unreachable)


def due(app: dict, now: float, ignore_retry: bool = False) -> bool:
    """L'app doit-elle être renouvelée ? ignore_retry : sans tenir compte du délai d'attente après un
    échec (appareil réapparu sur le réseau)."""
    if not app["enabled"] or app["id"] in _RUNNING:
        return False
    if app["next_attempt_at"] and app["next_attempt_at"] > now and not ignore_retry:
        return False
    if not app["expires_at"] or app["status"] in ("error", "pending"):
        # échec (même loin de l'expiration : nouvelle version, TV en veille…) ou essai interrompu
        # par un redémarrage : on réessaie, au rythme de next_attempt_at
        return True
    return app["expires_at"] - now < before_hours(app) * 3600


async def _devmode_banner(device: dict) -> None:
    """Vérifie le mode développeur avant un renouvellement. Information seulement : l'app déjà
    installée doit rester renouvelée, l'interface affiche la bannière."""
    try:
        on = await devices.check_devmode(device)
    except Exception as e:  # noqa: BLE001 - un échec ici ne doit pas empêcher le renouvellement
        db.log(f"« {device['name']} » : mode développeur non vérifié ({e})", "warn")
        return
    if on is False:
        if device["id"] not in _DEVMODE_LOGGED:
            _DEVMODE_LOGGED.add(device["id"])
            db.log(f"« {device['name']} » : mode développeur désactivé, les apps signées ne s'y lanceront pas "
                   "(Réglages › Confidentialité et sécurité › Mode développeur)", "warn")
    else:
        _DEVMODE_LOGGED.discard(device["id"])


def note_revocation(email: str, now: float | None = None) -> int:
    """Note une révocation de certificat du compte ; renvoie le nombre de révocations des 7 derniers
    jours. Trois ou plus : un autre outil de sideload partage l'Apple ID et les deux se révoquent
    en boucle."""
    now = now or time.time()
    key = f"revoked:{email}"
    try:
        times = [t for t in json.loads(settings.raw_get(key) or "[]") if now - t < REVOKE_WINDOW]
    except (ValueError, TypeError):
        times = []
    times.append(now)
    settings.raw_set(key, json.dumps(times))
    return len(times)


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


async def presence_check() -> None:
    """iPhone / iPad : sont-ils de retour sur le réseau ? Un téléphone qui rentre à la maison est
    renouvelé tout de suite, sans attendre le prochain passage ni la fin du délai après échec.
    Rien ne se passe (pas même une recherche mDNS) tant qu'aucune app de téléphone n'est due."""
    now = time.time()
    todo = []
    for app in db.rows("SELECT a.* FROM apps a JOIN devices d ON d.id = a.device_id "
                       "WHERE a.enabled = 1 AND d.family IN ('ios', 'ipados')"):
        if not due(app, now, ignore_retry=True):
            continue
        waiting = app["next_attempt_at"] and app["next_attempt_at"] > now
        if waiting and app["id"] not in _ABSENT:
            continue        # échec sans rapport avec la présence : le délai habituel s'applique
        todo.append(app)
    if not todo:
        return
    found = await plume.discover(4)
    seen = {d["udid"].lower(): d for d in found if d.get("kind") == "paired" and d.get("udid")}
    for device_id in dict.fromkeys(a["device_id"] for a in todo):
        device = db.row("SELECT * FROM devices WHERE id = ?", device_id)
        entry = seen.get(((device or {}).get("pairing_udid") or "").lower())
        if not entry:
            continue
        db.update("devices", device_id, last_seen=time.time(), last_ip=entry.get("ip"),
                  last_port=entry.get("port"))
        for app in [a for a in todo if a["device_id"] == device_id]:
            app = db.row("SELECT * FROM apps WHERE id = ?", app["id"])
            now = HEALTH["last_loop_at"] = time.time()
            if not app or app["id"] in _RUNNING or STOPPING or not due(app, now, ignore_retry=True):
                continue
            if app["last_attempt_at"] and now - app["last_attempt_at"] < PRESENCE_MIN_GAP:
                continue
            db.log(f"« {device['name']} » est de retour sur le réseau : renouvellement de {app['name']}",
                   app_id=app["id"])
            await renew(app["id"], "présence")


async def _guarded(what: str, coro) -> None:
    try:
        await coro
    except Exception as e:  # noqa: BLE001 - un échec isolé ne doit pas bloquer le reste
        HEALTH["loop_error"] = f"{what} : {e}"
        db.log(f"{what} : {e}", "error")


async def _sleep_watching() -> None:
    """Attente entre deux passages ; si l'on suit des iPhone / iPad, regarde de temps en temps s'ils
    sont revenus sur le réseau (PRESENCE_MINUTES, 0 : jamais)."""
    end = time.monotonic() + CHECK_INTERVAL
    step = PRESENCE_MINUTES * 60
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return
        if step <= 0 or left <= step:
            await asyncio.sleep(left)
            return
        await asyncio.sleep(step)
        if not STOPPING:
            await _guarded("détection des appareils", presence_check())


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
            await _check_expired(time.time())
        except Exception as e:  # noqa: BLE001 - la boucle ne doit jamais s'arrêter
            HEALTH["loop_error"] = str(e)
            db.log(f"boucle de renouvellement : {e}", "error")
        HEALTH["next_check_at"] = time.time() + CHECK_INTERVAL
        await _sleep_watching()
