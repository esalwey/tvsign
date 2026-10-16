"""tvsign : signe et maintient installées des apps tvOS sur des Apple TV, avec un compte Apple gratuit."""
import asyncio
import json
import re
import shutil
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, backup, db, devices, diag, ipa, notifier, plume, proc, renew, selfupdate, settings
from .config import EDITION, MAX_UPLOAD_MB, ROOT, SHUTDOWN_DRAIN_SECONDS, TMP_DIR, VERSION, WINDOWS
from .proc import SESSIONS
from .util import replace_retry


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    settings.migrate()         # serveur anisette Android enregistré -> serveur par défaut
    _purge_half_added()
    backup.prune_old()
    auth.init()
    renew.startup()            # un renouvellement interrompu ne reste pas « en cours »
    plume.import_existing_store()
    plume.note_legacy_anisette()
    await devices.sync_store()
    if settings.raw_get("onboarded") is None and (db.rows("SELECT id FROM apps LIMIT 1")
                                                  or db.rows("SELECT id FROM devices LIMIT 1")):
        settings.raw_set("onboarded", "1")   # installation existante : pas d'assistant
    for f in TMP_DIR.glob("*"):
        if f == plume.STAGE_DIR:
            continue           # nettoyé par plume.sweep_stage(), au rythme de ses sous-dossiers
        # fichiers (export interrompu) et dossiers (restauration interrompue : store Apple extrait) oubliés
        try:
            if time.time() - f.stat().st_mtime > 3600:
                shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
        except OSError:
            pass
    plume.sweep_stage()        # copies de travail de plumesign laissées par un processus tué
    # renew.start() lance la boucle supervisée (relancée si une exception lui échappe) ; watchdog() prévient si elle se fige
    tasks = [renew.start(), asyncio.create_task(renew.watchdog())]
    yield
    # arrêt : une installation en cours peut finir avant que l'arrêt ne l'interrompe
    await renew.drain(SHUTDOWN_DRAIN_SECONDS)
    for task in tasks:
        task.cancel()
    for s in SESSIONS.values():
        s.cancel()


def _purge_half_added() -> None:
    """Une app « en cours d'ajout » (téléchargement, validation) n'a de sens que pendant la requête :
    après un arrêt en plein ajout, la fiche et son dossier sont retirés."""
    for a in db.rows("SELECT id FROM apps WHERE status = 'adding'"):
        _abort_add(a["id"])


class UTF8JSON(JSONResponse):
    # charset explicite : certains clients (PowerShell 5.1) lisent sinon le JSON en Latin-1
    media_type = "application/json; charset=utf-8"


app = FastAPI(title="tvsign", version=VERSION, lifespan=lifespan, default_response_class=UTF8JSON,
              docs_url=None, redoc_url=None, openapi_url=None)


# Les erreurs passent sinon par les gestionnaires par défaut (JSONResponse sans charset).
@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException):
    return UTF8JSON({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    return UTF8JSON({"detail": jsonable_encoder(exc.errors())}, status_code=422)


# ---------- sécurité : Host, CSRF, authentification, taille des envois, en-têtes ----------

PUBLIC = {"/api/livez", "/api/auth/status", "/api/auth/login", "/api/setup", "/api/health", "/auth/launch"}
UPLOADS = {"/api/apps", "/api/backup/import"}
UPLOAD_ROUTES = re.compile(r"/api/apps/\d+/ipa")      # remplacement de l'IPA d'une app importée
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'self'"),
}


def _deny(code: int, detail: str) -> UTF8JSON:
    return UTF8JSON({"detail": detail}, status_code=code)


@app.middleware("http")
async def security(request: Request, call_next):
    path = request.url.path
    if path == "/api/livez":
        response = await call_next(request)          # supervision : jamais filtrée
    elif not auth.host_allowed(request):
        response = _deny(421, "host_not_allowed")
    elif backup.RESTORING and path.startswith("/api/") and path != "/api/backup/import":
        response = _deny(503, "restoring")           # base et dossiers en cours de remplacement
    else:
        response = await _guarded(request, path, call_next)
    for k, v in HEADERS.items():
        response.headers.setdefault(k, v)
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


async def _guarded(request: Request, path: str, call_next):
    mutating = request.method not in SAFE_METHODS
    if mutating and (path.startswith("/api/") or path.startswith("/auth/")):
        # un formulaire d'un autre site ne peut ni poser cet en-tête ni usurper Origin
        if request.headers.get("x-tvsign") != "1" or not auth.same_origin(request):
            return _deny(403, "csrf")
        length = request.headers.get("content-length")
        upload = path in UPLOADS or bool(UPLOAD_ROUTES.fullmatch(path))
        limit = MAX_UPLOAD_MB * 1024 * 1024 if upload else 1024 * 1024
        # Sans Content-Length, une requête en morceaux (Transfer-Encoding: chunked) échappait à la limite :
        # un corps de plusieurs Go sur une route publique (connexion) aurait été lu entièrement en mémoire.
        if length is None and (upload or request.headers.get("transfer-encoding")):
            return _deny(411, "content_length_required")
        if length is not None and (not length.isdigit() or int(length) > limit):
            return _deny(413, f"too_large:{limit // (1024 * 1024)}")
    if path.startswith("/api/") and path not in PUBLIC:
        if auth.setup_required() and not auth.trusted(request):
            return _deny(401, "setup_required")
        if not auth.authenticated(request):
            return _deny(401, "auth_required")
    response = await call_next(request)
    # cookie glissant : renouvelé au-delà d'un jour d'ancienneté
    data = auth.read_cookie(request.cookies.get(auth.COOKIE))
    if data and not path.startswith("/api/auth/logout") and time.time() - data.get("iat", 0) > 86400:
        response.set_cookie(value=auth.make_cookie(data.get("sid")), **auth.cookie_args(request))
    return response


WEB = ROOT / "web"


@app.api_route("/", methods=["GET", "HEAD"])
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


class RevalidatedStatic(StaticFiles):
    """Interface toujours revalidée (ETag) : après une mise à jour, le navigateur ne garde pas
    l'ancien app.js en cache."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatedStatic(directory=WEB), name="static")


# ---------- authentification ----------

@app.get("/api/livez")
def livez():
    """Vivacité du processus (HEALTHCHECK Docker) : toujours 200 tant que le serveur répond."""
    return {"ok": True, "version": VERSION}


@app.get("/api/auth/status")
def auth_status(request: Request):
    trusted = auth.trusted(request)
    return {
        "version": VERSION,
        "auth": "none" if not auth.enabled() else "password",
        "has_password": auth.has_password(),
        "password_from_env": bool(auth.PASSWORD_ENV),
        "setup_required": auth.setup_required(),
        "setup_code_required": auth.setup_required() and not trusted,
        "trusted": trusted,
        "authenticated": auth.authenticated(request),
        "onboarded": settings.raw_get("onboarded") == "1",
        "language": settings.get("language"),
        # texte adapté à la machine : « windows » (application de bureau) ou « docker » (serveur, NAS)
        "edition": EDITION,
    }


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)      # borné : scrypt ne hache jamais des mégaoctets


_LOGIN_LOCK = asyncio.Lock()


@app.post("/api/auth/login")
async def auth_login(request: Request, body: LoginBody):
    if not auth.enabled() or not auth.has_password():
        raise HTTPException(400, "no_password")
    ip = _client_ip(request)
    # un essai à la fois : sinon une rafale passe toute le contrôle avant que le 1er échec soit compté
    async with _LOGIN_LOCK:
        wait = auth.throttle_wait(ip)
        if wait > 0:
            raise HTTPException(429, f"too_many_attempts:{int(wait) + 1}")
        stored = auth.stored_hash()
        ok = await asyncio.to_thread(auth.verify_password, body.password, stored)
        if not ok:
            auth.record_failure(ip)
            print(f"[warn] échec de connexion à l'interface depuis {ip}", flush=True)
            raise HTTPException(401, "bad_password")
    auth.clear_failures(ip)
    response = UTF8JSON({"ok": True})
    response.set_cookie(value=auth.make_cookie(), **auth.cookie_args(request))
    return response


class SetupBody(BaseModel):
    code: Optional[str] = Field(None, max_length=64)
    password: Optional[str] = Field(None, max_length=1024)
    language: Optional[str] = Field(None, max_length=16)


@app.post("/api/setup")
async def setup(request: Request, body: SetupBody):
    if not auth.setup_required():
        raise HTTPException(409, "already_set_up")
    trusted = auth.trusted(request)
    ip = _client_ip(request)
    if not trusted:
        wait = auth.throttle_wait(ip)
        if wait > 0:
            raise HTTPException(429, f"too_many_attempts:{int(wait) + 1}")
        if not auth.check_setup_code(body.code):
            auth.record_failure(ip)
            raise HTTPException(401, "bad_setup_code")
    password = (body.password or "").strip() and body.password
    if not password and not trusted:
        raise HTTPException(400, "password_required")
    if body.language:
        try:
            settings.update({"language": body.language})
        except ValueError:
            pass
    if password:
        try:
            await asyncio.to_thread(auth.set_password, password)
        except ValueError:
            raise HTTPException(400, f"password_too_short:{auth.MIN_PASSWORD}") from None
    auth.finish_setup()
    auth.clear_failures(ip)
    db.log("Interface sécurisée" + (" par mot de passe" if password else " (accès local, sans mot de passe)"))
    response = UTF8JSON({"ok": True})
    response.set_cookie(value=auth.make_cookie(), **auth.cookie_args(request))
    return response


@app.get("/auth/launch")
def auth_launch(request: Request, token: str = ""):
    """Fenêtre de l'édition bureau : jeton par lancement -> cookie de session -> page d'accueil."""
    if not auth.is_local(request) or not auth.check_launch_token(token):
        raise HTTPException(403, "bad_launch_token")
    if auth.setup_required():
        auth.finish_setup()      # le lanceur est l'utilisateur local : pas de code de setup
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(value=auth.make_cookie(), **auth.cookie_args(request))
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    # ce navigateur seulement : le cookie est révoqué côté serveur (un exemplaire copié ne vaut plus rien) ;
    # « se déconnecter partout » reste le moyen de couper aussi les autres appareils
    data = auth.read_cookie(request.cookies.get(auth.COOKIE))
    if data and data.get("sid"):
        auth.revoke(data["sid"])
    response = UTF8JSON({"ok": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.post("/api/auth/logout-all")
def auth_logout_all():
    auth.rotate_sessions()
    response = UTF8JSON({"ok": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


class PasswordBody(BaseModel):
    current: Optional[str] = None
    new: Optional[str] = None       # vide : retirer le mot de passe (accès local de confiance seulement)


@app.post("/api/auth/password")
async def auth_password(request: Request, body: PasswordBody):
    if auth.PASSWORD_ENV:
        raise HTTPException(409, "password_from_env")
    if auth.has_password() and not auth.trusted(request):
        ok = await asyncio.to_thread(auth.verify_password, body.current or "", auth.stored_hash())
        if not ok:
            auth.record_failure(_client_ip(request))
            raise HTTPException(401, "bad_password")
    if not body.new:
        if not auth.trusted(request):
            raise HTTPException(400, "password_required")
        auth.set_password(None)
    else:
        try:
            await asyncio.to_thread(auth.set_password, body.new)
        except ValueError:
            raise HTTPException(400, f"password_too_short:{auth.MIN_PASSWORD}") from None
    auth.finish_setup()
    db.log("Mot de passe de l'interface " + ("modifié" if body.new else "retiré"))
    response = UTF8JSON({"ok": True})
    response.set_cookie(value=auth.make_cookie(), **auth.cookie_args(request))   # sessions renouvelées
    return response


@app.post("/api/onboarding/complete")
def onboarding_complete():
    settings.raw_set("onboarded", "1")
    return {"ok": True}


# ---------- état global ----------

def tv_presence(a: dict) -> tuple[bool | None, float | None]:
    """Résultat de la dernière vérification « l'app est-elle sur l'Apple TV ? ». Sans objet (None) si
    elle n'a jamais eu lieu ou si l'app a été réinstallée depuis : le résultat serait périmé."""
    at = a.get("on_tv_checked_at")
    if a.get("on_tv") is None or not at:
        return None, None
    if a.get("signed_at") and at < a["signed_at"]:
        return None, None
    return bool(a["on_tv"]), at


@app.api_route("/api/state", methods=["GET", "HEAD"])
def state():
    now = time.time()
    before = settings.renew_before_hours()
    # 'adding' : fiche créée pendant le téléchargement/la validation de l'IPA, pas encore une app
    apps = db.rows("SELECT a.*, d.name AS device_name FROM apps a LEFT JOIN devices d ON d.id = a.device_id "
                   "WHERE a.status != 'adding' ORDER BY a.name")
    default = plume.default_account()
    for a in apps:
        a["running"] = a["id"] in renew._RUNNING
        # étape en cours (signing, uploading, installing, waiting_app…) : voir renew.PHASE
        a["phase"] = renew.PHASE.get(a["id"]) if a["running"] else None
        exp = a["expires_at"]
        a["expired"] = bool(exp) and exp <= now
        # moment où la boucle lancera le renouvellement automatique, plage horaire comprise ;
        # window_wait : il attend l'ouverture de la plage (window_at)
        a.update(renew.window_view(a, now, before))
        a["effective_account"] = a["account_email"] or default
        a["can_rollback"] = renew.has_previous(a["id"])             # IPA précédente gardée : « version précédente »
        a["update_pending"] = renew.pending_update(a)               # IPA téléchargée, pas encore installée
        a["untracked_link"] = a["source_type"] == "url" and bool(a["source_tag"]) and not a["source_key"]
        a["tv_present"], a["tv_checked_at"] = tv_presence(a)
        # niveau d'alerte (fail | 24h | 6h | expired | None), même règle que ntfy : voir renew.alert_state
        a["alert"] = renew.alert_state(a, now)
    health = renew.health(now)
    devices_ = db.rows("SELECT * FROM devices ORDER BY name")
    for d in devices_:
        d["busy"] = devices.lock(d["id"]).locked()     # une installation tient la connexion à cette TV
    accounts_ = plume.accounts()
    slots = renew.account_slots(apps, devices_, accounts_, default)
    for acc in accounts_:
        acc["slots"] = slots.get(acc["email"], [])
    notices = []
    try:
        revoked = json.loads(settings.raw_get("revocation_notice") or "null")
    except ValueError:
        revoked = None
    if isinstance(revoked, dict):
        notices.append({"id": "revocation", **revoked})
    return {
        "now": now,
        "version": VERSION,
        "windows": WINDOWS,
        "renew_before_hours": before,
        "renew_window": settings.renew_window(),
        "max_active_apps": renew.MAX_ACTIVE_APPS,
        "notifications": health["notifications"],
        "auth_disabled": not auth.enabled(),
        "health": health,
        "update": selfupdate.available(),         # nouvelle version de tvsign à proposer (ou None)
        "apps": apps,
        "devices": devices_,
        "accounts": accounts_,
        "events": db.rows("SELECT * FROM events ORDER BY id DESC LIMIT 80"),
        "notices": notices,
    }


@app.api_route("/api/health", methods=["GET", "HEAD"])
def api_health(request: Request):
    """Pour une supervision externe : 503 si la boucle est figée ou si une app va expirer en échec.
    Sans authentification, seul le nombre de problèmes est donné (pas d'e-mails ni de noms d'apps)."""
    now = time.time()
    h = renew.health(now)
    problems = []
    if h["loop_stalled"]:
        problems.append("boucle de renouvellement figée")
    for a in db.rows("SELECT * FROM apps WHERE enabled = 1"):
        exp = a["expires_at"]
        if exp and exp <= now:
            problems.append(f"{a['name']} : profil expiré")
        elif a["status"] == "error" and exp and exp - now < 24 * 3600:
            problems.append(f"{a['name']} : expire dans moins de 24 h, renouvellement en échec")
    for acc in plume.accounts():
        if acc["ok"] == 0:
            problems.append(f"compte {acc['email']} : reconnexion nécessaire")
    code = 503 if problems else 200
    if not auth.authenticated(request):
        return UTF8JSON({"ok": not problems, "count": len(problems)}, status_code=code)
    return UTF8JSON({"ok": not problems, "count": len(problems), "problems": problems, **h}, status_code=code)


# ---------- sessions interactives ----------

def session_or_404(sid: str):
    s = SESSIONS.get(sid)
    if not s:
        raise HTTPException(404, "session_expired")
    return s


@app.get("/api/sessions/{sid}")
def session_state(sid: str):
    return session_or_404(sid).public()


class Answer(BaseModel):
    value: str


@app.post("/api/sessions/{sid}/answer")
async def session_answer(sid: str, body: Answer):
    s = session_or_404(sid)
    if s.state != "waiting":
        raise HTTPException(409, "no_input_expected")
    await s.send(body.value)
    return s.public()


@app.post("/api/sessions/{sid}/cancel")
async def session_cancel(sid: str):
    """Abandon d'une connexion/appairage (dialogue fermé) : libère plumesign tout de suite."""
    s = session_or_404(sid)
    s.cancel()
    return s.public()


# ---------- comptes Apple ----------

class Login(BaseModel):
    email: str
    password: str


# async : start_login crée des tâches asyncio, impossible depuis le pool de threads d'une route « def »
@app.post("/api/accounts/login")
async def account_login(body: Login):
    email = body.email.strip()
    if not email or not body.password:
        raise HTTPException(400, "credentials_required")
    return plume.start_login(email, body.password).public()


def account_or_404(email: str) -> str:
    """Écriture du store pour ce compte (la casse de l'adresse ne compte pas), ou 404."""
    known = plume.find_account(email)
    if not known:
        raise HTTPException(404, "unknown_account")
    return known


@app.post("/api/accounts/{email}/check")
async def account_check(email: str):
    return {"ok": await plume.check_account(account_or_404(email))}


@app.post("/api/accounts/{email}/select")
async def account_select(email: str):
    email = account_or_404(email)
    try:
        await plume.select(email)
    except plume.UnknownAccount:
        raise HTTPException(404, "unknown_account") from None
    except plume.Busy:
        raise HTTPException(503, "plumesign_busy") from None      # occupé (connexion Apple en attente ?)
    return {"ok": True}


@app.delete("/api/accounts/{email}")
async def account_delete(email: str):
    email = account_or_404(email)
    try:
        await plume.logout(email)
    except plume.UnknownAccount:
        raise HTTPException(404, "unknown_account") from None
    except plume.Busy:
        raise HTTPException(503, "plumesign_busy") from None
    except RuntimeError as e:
        db.log(f"Déconnexion de {email} échouée : {e}", "error")
        raise HTTPException(502, "logout_failed") from None
    return {"ok": True}


class TeamChoice(BaseModel):
    team_id: str


@app.get("/api/accounts/{email}/teams")
async def account_teams(email: str):
    try:
        return {"teams": await plume.teams(email), "env_team": plume.env_team()}
    except plume.UnknownAccount:
        raise HTTPException(404, "unknown_account") from None
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from None


@app.post("/api/accounts/{email}/team")
async def account_team(email: str, body: TeamChoice):
    try:
        await plume.set_team(email, body.team_id.strip().upper())
    except plume.UnknownAccount:
        raise HTTPException(404, "unknown_account") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from None
    return {"ok": True}


# ---------- Apple TV ----------

class Discover(BaseModel):
    host: Optional[str] = None      # adresse IP ou nom d'hôte de la TV, quand le multicast est bloqué


@app.post("/api/devices/discover")
async def device_discover(body: Optional[Discover] = None):
    host = None
    if body and (body.host or "").strip():
        try:
            host = devices.clean_host(body.host)
        except ValueError:
            raise HTTPException(400, "invalid_host") from None
    try:
        return await devices.discover(host)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


class Pair(BaseModel):
    identifier: Optional[str] = None
    ip: str
    port: int
    name: str


@app.post("/api/devices/pair")
async def device_pair(body: Pair):
    if not re.fullmatch(r"[0-9A-Fa-f:.%\w-]+", body.ip) or not 0 < body.port < 65536:
        raise HTTPException(400, "invalid_address")
    return devices.start_pairing(body.identifier, body.ip, body.port, body.name.strip() or "Apple TV").public()


def device_or_404(device_id: int) -> dict:
    d = db.row("SELECT * FROM devices WHERE id = ?", device_id)
    if not d:
        raise HTTPException(404, "unknown_device")
    return d


@app.post("/api/devices/{device_id}/refresh")
async def device_refresh(device_id: int):
    device_or_404(device_id)
    try:
        return await devices.refresh_info(device_id, wait=False)
    except devices.Busy:
        raise HTTPException(409, "device_busy") from None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


@app.get("/api/devices/{device_id}/apps")
async def device_apps(device_id: int):
    device = device_or_404(device_id)      # hors du try : un 404 ne devient pas un 502
    try:
        return await devices.installed_apps(device, wait=False)
    except devices.Busy:
        raise HTTPException(409, "device_busy") from None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


@app.post("/api/devices/{device_id}/verify")
async def device_verify(device_id: int):
    """Les apps suivies sont-elles encore sur l'Apple TV ? (lecture seule : rien n'est signé ni installé)."""
    device = device_or_404(device_id)
    try:
        listed = await devices.installed_apps(device, wait=False)
    except devices.Busy:
        raise HTTPException(409, "device_busy") from None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))
    listed = [x for x in listed if isinstance(x, dict) and x.get("bundle_id")]
    if not listed:
        # une liste vide n'est pas crédible (journal parasite, TV à peine réveillée) : on n'écrit rien
        raise HTTPException(502, "tv_listing_empty")
    now, out = time.time(), []
    for a in db.rows("SELECT * FROM apps WHERE device_id = ? AND bundle_id IS NOT NULL AND status != 'adding'",
                     device_id):
        if a["id"] in renew._RUNNING:
            continue        # installation en cours : l'absence ne dirait rien
        out.append({"app_id": a["id"], "present": devices.record_presence(a, listed, renew.app_dir(a["id"]), now)})
    return {"ok": True, "checked_at": now, "apps": out}


class DevicePatch(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None      # adresse saisie à la main ; vide : la retirer


@app.patch("/api/devices/{device_id}")
def device_patch(device_id: int, body: DevicePatch):
    device_or_404(device_id)
    fields = {}
    if body.name is not None:
        fields["name"] = body.name.strip() or "Apple TV"
    if body.host is not None:
        try:
            fields["host_hint"] = devices.clean_host(body.host)
        except ValueError:
            raise HTTPException(400, "invalid_host") from None
    db.update("devices", device_id, **fields)
    return {"ok": True}


class Relocate(BaseModel):
    host: Optional[str] = None      # nouvelle adresse ; absente : on retente avec celle déjà enregistrée


@app.post("/api/devices/{device_id}/relocate")
async def device_relocate(device_id: int, body: Optional[Relocate] = None):
    """Retrouve la TV à l'adresse donnée (multicast bloqué, adresse changée) : l'adresse est enregistrée,
    puis la TV est contactée directement. L'adresse reste enregistrée même si la TV dort."""
    device_or_404(device_id)
    if body and body.host is not None:
        try:
            db.update("devices", device_id, host_hint=devices.clean_host(body.host))
        except ValueError:
            raise HTTPException(400, "invalid_host") from None
    try:
        await devices.refresh_info(device_id, wait=False)
    except devices.Busy:
        raise HTTPException(409, "device_busy") from None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))
    d = device_or_404(device_id)
    return {"ok": True, "ip": d["last_ip"], "port": d["last_port"]}


@app.delete("/api/devices/{device_id}")
async def device_delete(device_id: int, force: bool = False):
    d = device_or_404(device_id)
    if devices.lock(device_id).locked() or any(
            r["id"] in renew._RUNNING for r in db.rows("SELECT id FROM apps WHERE device_id = ?", device_id)):
        # un renouvellement installe sur cette TV : l'appairage oublié, il se terminerait sans cible
        raise HTTPException(409, "device_busy")
    # l'appairage d'abord : s'il reste dans le store, sync_store recrée la TV au prochain démarrage
    try:
        await devices.forget(d)
    except RuntimeError as e:
        if not force:
            raise HTTPException(502, str(e)) from None
        db.log(f"Apple TV « {d['name']} » retirée de la liste, appairage non effacé : {e}", "warn")
    db.execute("DELETE FROM devices WHERE id = ?", device_id)
    return {"ok": True}


# ---------- apps ----------

@app.get("/api/presets")
def presets():
    return ipa.presets()


async def _check_source(source_type: str, source: str | None) -> str | None:
    source = (source or "").strip() or None
    if source_type == "github":
        source = ipa.normalize_repo(source)     # accepte aussi une adresse github.com collée telle quelle
        if not source:
            raise HTTPException(400, "invalid_repo")
    if source_type == "url":
        if not (source or "").startswith("https://"):
            raise HTTPException(400, "https_only")
        try:
            # résolution DNS hors de la boucle d'événements : un résolveur lent ne gèle pas l'interface
            await asyncio.to_thread(ipa._check_url, source)
        except ipa.UrlRejected as e:
            raise HTTPException(400, e.code) from None
        except (ValueError, ConnectionError) as e:
            raise HTTPException(400, getattr(e, "code", None) or str(e)) from None
    return source


def _check_min_os(device_id: int | None, info: dict) -> None:
    """MinimumOSVersion de l'IPA contre le tvOS de l'Apple TV choisie (version inconnue : on laisse passer)."""
    dev = db.row("SELECT os_version FROM devices WHERE id = ?", device_id) if device_id else None
    if dev and ipa.os_older(dev["os_version"], info.get("min_os")):
        raise HTTPException(400, f"tvos_too_old:{info['min_os']}")


def _abort_add(app_id: int) -> None:
    db.execute("DELETE FROM apps WHERE id = ?", app_id)
    shutil.rmtree(renew.app_dir(app_id), ignore_errors=True)


def _already_added(device_id: int | None) -> HTTPException:
    """409 qui nomme la TV concernée (« already_added:Salon »)."""
    tv = db.row("SELECT name FROM devices WHERE id = ?", device_id) if device_id else None
    name = (tv["name"] if tv else "").replace(":", " ").strip()
    return HTTPException(409, f"already_added:{name}" if name else "already_added")


def _duplicate(app_id: int, bundle_id: str | None, account: str | None, device_id: int | None) -> bool:
    """Même app, même compte, même Apple TV : une 2e fiche signerait deux fois et les copies
    s'écraseraient sur la TV. Sur une autre TV, rien ne se heurte : chaque fiche a sa signature, son
    installation et son état."""
    return bool(bundle_id) and bool(db.row(
        "SELECT id FROM apps WHERE id != ? AND bundle_id = ? AND COALESCE(account_email, '') = ? "
        "AND COALESCE(device_id, 0) = ?", app_id, bundle_id, account or "", device_id or 0))


@app.post("/api/apps")
async def app_create(
    source_type: str = Form(...),               # upload | github | url | preset
    source: Optional[str] = Form(None),
    source_filter: Optional[str] = Form(None),
    channel: Optional[str] = Form(None),        # stable | beta (github)
    preset: Optional[str] = Form(None),
    device_id: Optional[int] = Form(None),
    account_email: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
):
    asset_name = None
    if source_type == "preset":
        p = ipa.preset(preset or "")
        if not p:
            raise HTTPException(400, "unknown_preset")
        # préfixes de tag : ceux du canal, relus dans le préréglage à chaque recherche (ipa.release_rule)
        source_type, source, source_filter = "github", p["repo"], None
        asset_name = p.get("asset")
        channel = channel if channel in p["channels"] else p["default_channel"]
    else:
        preset = None
    if source_type not in ("upload", "github", "url"):
        raise HTTPException(400, "unknown_source")
    if source_type == "upload" and not file:
        raise HTTPException(400, "missing_file")
    if source_type != "upload":
        source = await _check_source(source_type, source)
    channel = "beta" if channel == "beta" else "stable"
    if device_id is not None and not db.row("SELECT id FROM devices WHERE id = ?", device_id):
        raise HTTPException(400, "unknown_device")

    # compte figé dès la création : en changer créerait une 2e copie (autre équipe = autre bundle ID)
    if account_email and account_email.strip():
        account = plume.find_account(account_email)
        if not account:
            raise HTTPException(400, "unknown_account")
    else:
        account = plume.default_account()
    if source_type != "upload" and db.row(
            "SELECT id FROM apps WHERE source_type = ? AND source = ? AND COALESCE(asset_name, '') = ? "
            "AND COALESCE(source_filter, '') = ? AND COALESCE(account_email, '') = ? "
            "AND COALESCE(device_id, 0) = ?", source_type, source, asset_name or "",
            (source_filter or "").strip(), account or "", device_id or 0):
        raise _already_added(device_id)      # avant tout téléchargement
    # enabled = 0 et status = 'adding' tant que l'IPA n'est pas validée : ni la boucle de renouvellement ni
    # l'interface (/api/state) ne voient l'app à moitié ajoutée
    app_id = db.execute(
        "INSERT INTO apps (name, source_type, source, source_filter, channel, preset, asset_name, device_id, "
        "account_email, created_at, enabled, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'adding')",
        file.filename if file else source, source_type, source,
        (source_filter or "").strip() or None, channel, preset, asset_name, device_id, account, time.time())
    try:
        dest = renew.app_dir(app_id) / "original.ipa"
        if file:
            limit, got = MAX_UPLOAD_MB * 1024 * 1024, 0
            with open(dest, "wb") as f:
                while chunk := await file.read(1 << 20):
                    got += len(chunk)
                    if got > limit:
                        raise HTTPException(413, f"too_large:{MAX_UPLOAD_MB}")
                    f.write(chunk)
        else:
            try:
                await renew.refresh_source(db.row("SELECT * FROM apps WHERE id = ?", app_id))
            except ipa.SourceUnavailable as e:
                raise HTTPException(400, e.code) from None
            except ipa.OsTooOld as e:
                raise HTTPException(400, f"tvos_too_old:{e.min_os}") from None
        try:
            info = ipa.app_info(dest)
        except (zipfile.BadZipFile, ValueError):
            raise HTTPException(400, "not_an_ipa") from None
        if info["platform"] != "appletvos":
            raise HTTPException(400, f"not_tvos:{info['platform'] or 'unknown'}")
        _check_min_os(device_id, info)
        # même app, même compte, même Apple TV : une 2e fiche signerait deux fois et les copies s'écraseraient
        if _duplicate(app_id, info["bundle_id"], account, device_id):
            raise _already_added(device_id)
        sha = await asyncio.to_thread(renew._sha256_file, dest)   # identité du fichier : version installée / en attente
        db.update("apps", app_id, name=info["name"], bundle_id=info["bundle_id"], version=info["version"],
                  enabled=1, status="pending", source_sha256=sha)
    except HTTPException:
        _abort_add(app_id)
        raise
    except BaseException as e:  # noqa: BLE001 - y compris l'annulation (arrêt, client parti) : pas de ligne fantôme
        _abort_add(app_id)
        if isinstance(e, renew.NoRelease):
            raise HTTPException(400, f"no_release_beta:{e.beta_tag}" if e.beta_tag else "no_release") from None
        if isinstance(e, Exception):
            # SourceRejected porte un code stable que l'interface traduit ; sinon le texte brut (français)
            raise HTTPException(400, getattr(e, "code", None) or str(e)) from None
        raise
    db.log(f"App ajoutée : {info['name']} {info['version']} ({info['bundle_id']})", app_id=app_id)
    renew.spawn(app_id, "ajout")
    created = db.row("SELECT * FROM apps WHERE id = ?", app_id)
    # au-delà de 3 apps actives pour ce compte sur cette Apple TV, la limite du compte gratuit sera atteinte
    created["warnings"] = renew.add_warnings(created)
    return created


class AppPatch(BaseModel):
    enabled: Optional[bool] = None
    device_id: Optional[int] = None
    account_email: Optional[str] = None
    source: Optional[str] = None           # dépôt GitHub (owner/repo) ou URL https
    source_filter: Optional[str] = None    # préfixe de tag (github)
    channel: Optional[str] = None          # stable | beta (github)
    auto_update: Optional[bool] = None     # false : renouveler seulement, ne pas installer les nouvelles versions
    pinned_tag: Optional[str] = None       # github : seule cette version est suivie ("" : suivre la dernière)


def app_or_404(app_id: int) -> dict:
    a = db.row("SELECT * FROM apps WHERE id = ?", app_id)
    if not a:
        raise HTTPException(404, "unknown_app")
    return a


@app.patch("/api/apps/{app_id}")
async def app_patch(app_id: int, body: AppPatch):
    a = app_or_404(app_id)
    fields = body.model_dump(exclude_unset=True)
    pin_in_request = "pinned_tag" in fields
    if fields.get("enabled") is None:
        fields.pop("enabled", None)          # null n'a pas de sens : ignoré
    else:
        fields["enabled"] = int(fields["enabled"])
        if not fields["enabled"]:
            renew._release_ok(a)             # en pause : sa recherche de version n'a plus lieu d'être signalée
    if "account_email" in fields:
        wanted = (fields["account_email"] or "").strip()
        if wanted:
            fields["account_email"] = plume.find_account(wanted)
            if not fields["account_email"]:
                raise HTTPException(400, "unknown_account")
        else:
            fields["account_email"] = plume.default_account()
    if "source" in fields:
        fields["source"] = await _check_source(a["source_type"], fields["source"])
        if a["preset"] and fields["source"] != a["source"]:
            fields["preset"] = None            # source personnalisée : plus le préréglage
            fields["asset_name"] = None
    if "channel" in fields:
        fields["channel"] = "beta" if fields["channel"] == "beta" else "stable"
    if fields.get("auto_update") is None:
        fields.pop("auto_update", None)
    else:
        fields["auto_update"] = int(fields["auto_update"])
    pin_changed = False
    if "pinned_tag" in fields:
        pin = (fields["pinned_tag"] or "").strip()
        if len(pin) > 200 or re.search(r"[\x00-\x1f\x7f]", pin):
            raise HTTPException(400, "invalid_tag")
        if pin and a["source_type"] != "github":
            raise HTTPException(400, "pin_github_only")
        fields["pinned_tag"] = pin or None
        pin_changed = fields["pinned_tag"] != a["pinned_tag"]
        if not pin_changed:
            del fields["pinned_tag"]
    if "device_id" in fields and fields["device_id"] is not None:
        if not db.row("SELECT id FROM devices WHERE id = ?", fields["device_id"]):
            raise HTTPException(400, "unknown_device")
    source_changed = any(k in fields for k in ("source", "source_filter", "channel"))
    if source_changed:
        fields["source_tag"] = None        # nouvelle source : la prochaine release sera installée
        fields["source_key"] = None
        fields["bad_tag"] = None
        fields["bad_key"] = None
        fields.update(update_tag=None, update_notes=None, update_block=None, update_error=None, bad_reason=None)
        renew._release_ok(a)               # l'ancienne source n'est plus cherchée : son échec ne compte plus
        if "source" in fields and fields["source"] != a["source"]:
            # autre dépôt ou autre lien : la version épinglée (tag de l'ancien dépôt) et la version précédente
            # gardée (IPA de l'ancienne source) n'ont plus de sens
            if not pin_in_request:
                fields["pinned_tag"] = None
            fields.update(prev_tag=None, prev_key=None, prev_version=None, prev_sha256=None)
            try:
                (renew.app_dir(app_id) / "original.prev.ipa").unlink(missing_ok=True)
            except OSError:
                pass
    dest_changed = "device_id" in fields or "account_email" in fields
    if dest_changed and _duplicate(app_id, a["bundle_id"], fields.get("account_email", a["account_email"]),
                                   fields.get("device_id", a["device_id"])):
        raise _already_added(fields.get("device_id", a["device_id"]))     # la TV visée a déjà cette app
    if dest_changed:
        # autre appareil/compte : la signature déjà prête ne convient plus
        (renew.app_dir(app_id) / "signed.ipa").unlink(missing_ok=True)
        fields["on_tv"] = fields["on_tv_checked_at"] = None     # la vérification visait l'ancienne TV
    if dest_changed or source_changed or fields.get("enabled") == 1:
        # l'utilisateur a agi : on repart de zéro (plus d'attente, ni d'action requise, ni de délai croissant)
        fields.update(renew.CLEAR_BLOCK)
    if pin_changed:
        fields.update(update_tag=None, update_notes=None, update_block=None, update_error=None,
                      bad_tag=None, bad_key=None, bad_reason=None)
    db.update("apps", app_id, **fields)
    if dest_changed:
        renew.rerun(app_id)    # un renouvellement en cours vise l'ancienne destination
    if source_changed:
        renew.spawn(app_id, "nouvelle source")
    elif pin_changed:
        # version épinglée (ou libérée) : appliquée tout de suite, la mise à jour auto coupée n'y change rien
        renew.spawn(app_id, "mise à jour" if fields.get("pinned_tag") else "nouvelle version")
    return db.row("SELECT * FROM apps WHERE id = ?", app_id)


class AppCopy(BaseModel):
    device_id: int


@app.post("/api/apps/{app_id}/copy")
async def app_copy(app_id: int, body: AppCopy):
    """La même app sur une autre Apple TV (même source, même compte) : une fiche par TV, chacune avec
    son état, sa signature et son échéance. L'IPA déjà téléchargée est reprise, sans nouveau téléchargement."""
    a = app_or_404(app_id)
    if not a["enabled"] or not a["bundle_id"]:
        # fiche en cours d'ajout : l'IPA n'est pas encore validée, elle pourrait être partielle
        raise HTTPException(409, "app_not_ready")
    if not db.row("SELECT id FROM devices WHERE id = ?", body.device_id):
        raise HTTPException(400, "unknown_device")
    account = a["account_email"] or plume.default_account()
    if _duplicate(app_id, a["bundle_id"], account, body.device_id) or body.device_id == a["device_id"]:
        raise _already_added(body.device_id)
    src = renew.app_dir(app_id) / "original.ipa"
    if not src.exists():
        raise HTTPException(400, "ipa_missing")
    # enabled = 0 tant que l'IPA n'est pas copiée : la boucle ne voit pas la fiche à moitié créée
    new_id = db.execute(
        "INSERT INTO apps (name, bundle_id, version, source_type, source, source_filter, source_tag, source_key, "
        "source_sha256, channel, preset, asset_name, auto_update, pinned_tag, account_email, device_id, "
        "created_at, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        a["name"], a["bundle_id"], a["version"], a["source_type"], a["source"], a["source_filter"],
        a["source_tag"], a["source_key"], a["source_sha256"], a["channel"], a["preset"], a["asset_name"],
        a["auto_update"], a["pinned_tag"], account, body.device_id, time.time())
    try:
        await asyncio.to_thread(shutil.copy2, src, renew.app_dir(new_id) / "original.ipa")
    except OSError as e:
        _abort_add(new_id)
        db.log(f"{a['name']} : copie de l'IPA impossible : {e}", "error", app_id=app_id)   # chemins du serveur : dans le journal seulement
        raise HTTPException(500, "copy_failed") from None
    db.update("apps", new_id, enabled=1)
    db.log(f"App ajoutée : {a['name']} {a['version']} ({a['bundle_id']}), copie pour une autre Apple TV", app_id=new_id)
    renew.spawn(new_id, "ajout")
    return db.row("SELECT * FROM apps WHERE id = ?", new_id)


@app.post("/api/apps/{app_id}/renew")
async def app_renew(app_id: int):
    app_or_404(app_id)
    renew.clear_block(app_id)
    return {"ok": True, "started": renew.spawn(app_id, "manuel")}


@app.post("/api/apps/{app_id}/update")
async def app_update(app_id: int):
    """« Installer la mise à jour maintenant » : télécharge la version disponible (ou la version épinglée) et
    l'installe, même si la mise à jour automatique est coupée ou si la version avait été écartée."""
    a = app_or_404(app_id)
    if a["source_type"] == "upload":
        raise HTTPException(409, "not_followed")
    renew.clear_block(app_id)
    return {"ok": True, "started": renew.spawn(app_id, "mise à jour")}


@app.post("/api/apps/{app_id}/rollback")
async def app_rollback(app_id: int):
    """Réinstalle la version précédente (dernière IPA installée avant la mise à jour). La version quittée
    est ignorée jusqu'à une autre release, ou jusqu'à « Installer la mise à jour »."""
    a = app_or_404(app_id)
    if app_id in renew._RUNNING:
        raise HTTPException(409, "app_busy")
    if not renew.has_previous(app_id):
        raise HTTPException(409, "no_previous")
    try:
        renew._swap_files(app_id)
    except OSError:
        raise HTTPException(409, "files_locked") from None      # fichier verrouillé (antivirus…) : rien n'a changé
    d = renew.app_dir(app_id)
    (d / "original.prev.ipa").unlink(missing_ok=True)            # la version quittée : retéléchargeable depuis la source
    (d / "signed.ipa").unlink(missing_ok=True)
    followed = a["source_type"] != "upload"
    db.update("apps", app_id, source_tag=a["prev_tag"], source_key=a["prev_key"], version=a["prev_version"],
              source_sha256=a["prev_sha256"], notes=None,
              bad_tag=a["source_tag"] if followed else None, bad_key=a["source_key"] if followed else None,
              update_tag=a["source_tag"] if followed else None, update_notes=a["notes"] if followed else None,
              update_block="rollback" if followed else None, update_error=None,
              prev_tag=None, prev_key=None, prev_version=None, prev_sha256=None, **renew.CLEAR_BLOCK)
    db.log(f"{a['name']} : retour à la version précédente {a['prev_version'] or a['prev_tag'] or ''}".rstrip(),
           app_id=app_id)
    return {"ok": True, "started": renew.spawn(app_id, "retour arrière")}


_RENEW_ALL_PENDING: set[int] = set()      # apps encore en file d'un « Tout renouveler » (une restauration doit attendre)


async def _renew_all(ids: list[int]) -> None:
    """Un à un : plumesign signe sous verrou, lancer tout d'un coup ne ferait qu'empiler des attentes."""
    try:
        for app_id in ids:
            if renew.STOPPING:
                break
            try:
                if db.row("SELECT id FROM apps WHERE id = ?", app_id):      # supprimée entre-temps : on passe
                    await renew.renew(app_id, "manuel")
            finally:
                _RENEW_ALL_PENDING.discard(app_id)
    finally:
        _RENEW_ALL_PENDING.difference_update(ids)


@app.post("/api/apps/renew-all")
async def app_renew_all():
    """Renouvelle maintenant toutes les apps actives qui ont une Apple TV, l'une après l'autre."""
    ids = [a["id"] for a in db.rows("SELECT id FROM apps WHERE enabled = 1 AND device_id IS NOT NULL "
                                    "AND status != 'adding' ORDER BY expires_at IS NULL, expires_at, id")
           if a["id"] not in renew._RUNNING]
    for app_id in ids:
        renew.clear_block(app_id)
    if ids:
        _RENEW_ALL_PENDING.update(ids)
        proc.spawn(_renew_all(ids))
    return {"ok": True, "started": len(ids)}


@app.post("/api/apps/{app_id}/retry-version")
async def app_retry_version(app_id: int):
    """Réessaie la version refusée (téléchargement raté jugé invalide, empreinte…) sans toucher à la source."""
    a = app_or_404(app_id)
    db.update("apps", app_id, bad_tag=None, bad_key=None, bad_reason=None, **renew.CLEAR_BLOCK)
    return {"ok": True, "started": renew.spawn(app_id, "nouvelle version" if a["status"] == "ok" else "manuel")}


@app.post("/api/apps/{app_id}/accept-version")
async def app_accept_version(app_id: int):
    """Accepte une version dont l'identifiant d'app a changé : l'ancien identifiant n'est plus exigé."""
    a = app_or_404(app_id)
    if a["source_type"] == "upload" or not (a["bad_tag"] or a["bad_reason"]):
        # rien à accepter : effacer l'identifiant d'une app saine (ou importée, jamais retéléchargée) ferait perdre la protection
        raise HTTPException(409, "nothing_to_accept")
    db.update("apps", app_id, bundle_id=None, bad_tag=None, bad_key=None, bad_reason=None, **renew.CLEAR_BLOCK)
    db.log("Changement d'identifiant d'app accepté", app_id=app_id)
    return {"ok": True, "started": renew.spawn(app_id, "nouvelle version")}


@app.post("/api/apps/{app_id}/ipa")
async def app_replace_ipa(app_id: int, file: UploadFile = File(...)):
    """Remplace l'IPA d'une app importée (nouvelle version, ou IPA absente après une restauration) en gardant
    son historique, son Apple TV et son compte. Mêmes contrôles qu'à l'ajout, et même app (bundle ID)."""
    a = app_or_404(app_id)
    if a["source_type"] != "upload":
        raise HTTPException(409, "not_an_upload")
    if app_id in renew._RUNNING:
        raise HTTPException(409, "app_busy")
    tmp = TMP_DIR / f"replace-{app_id}-{int(time.time())}.ipa"
    try:
        limit, got = MAX_UPLOAD_MB * 1024 * 1024, 0
        with open(tmp, "wb") as f:
            while chunk := await file.read(1 << 20):
                got += len(chunk)
                if got > limit:
                    raise HTTPException(413, f"too_large:{MAX_UPLOAD_MB}")
                f.write(chunk)
        try:
            info = ipa.app_info(tmp)
        except Exception:  # noqa: BLE001 - zip illisible, Info.plist mal formé (ExpatError), clé absente…
            raise HTTPException(400, "not_an_ipa") from None
        if info["platform"] != "appletvos":
            raise HTTPException(400, f"not_tvos:{info['platform'] or 'unknown'}")
        if a["bundle_id"] and info["bundle_id"] != a["bundle_id"]:
            raise HTTPException(400, f"bundle_mismatch:{a['bundle_id']}")
        _check_min_os(a["device_id"], info)
        sha = await asyncio.to_thread(renew._sha256_file, tmp)
        if app_id in renew._RUNNING:                # un renouvellement a démarré pendant l'envoi
            raise HTTPException(409, "app_busy")
        d = renew.app_dir(app_id)
        dest, prev, hold = d / "original.ipa", d / "original.prev.ipa", d / "original.hold"
        keep = dest.exists() and not renew.pending_update(a) and a["source_sha256"] != sha

        def put_in_place() -> None:
            # copie éventuelle d'une grosse IPA et remplacements : hors de la boucle d'événements
            try:
                if keep:
                    ipa.keep_copy(dest, hold)
                replace_retry(tmp, dest)
                if keep:
                    replace_retry(hold, prev)
            finally:
                hold.unlink(missing_ok=True)

        try:
            await asyncio.to_thread(put_in_place)
        except OSError:
            raise HTTPException(409, "files_locked") from None
        (d / "signed.ipa").unlink(missing_ok=True)
        fields = dict(name=info["name"], version=info["version"], bundle_id=info["bundle_id"], source_sha256=sha,
                      bad_tag=None, bad_key=None, update_error=None, **renew.CLEAR_BLOCK)
        if keep:
            fields.update(prev_tag=a["source_tag"], prev_key=a["source_key"], prev_version=a["version"],
                          prev_sha256=a["source_sha256"])
        if a["status"] == "error":
            fields.update(status="pending", last_error=None)       # l'erreur « IPA absente » n'a plus lieu d'être
        db.update("apps", app_id, **fields)
    finally:
        tmp.unlink(missing_ok=True)
    db.log(f"IPA remplacée : {info['name']} {info['version']}", app_id=app_id)
    if a["enabled"]:
        renew.spawn(app_id, "nouvelle IPA")
    return db.row("SELECT * FROM apps WHERE id = ?", app_id)


@app.delete("/api/apps/{app_id}")
def app_delete(app_id: int):
    if app_id in renew._RUNNING:
        # un renouvellement en cours referait le dossier, signerait et installerait une app qu'on ne suit plus
        raise HTTPException(409, "app_busy")
    a = db.row("SELECT name FROM apps WHERE id = ?", app_id)
    db.execute("DELETE FROM apps WHERE id = ?", app_id)
    db.execute("DELETE FROM events WHERE app_id = ?", app_id)   # sinon l'identifiant réutilisé hérite de ce journal
    shutil.rmtree(renew.app_dir(app_id), ignore_errors=True)
    renew.forget(app_id)       # l'identifiant peut être réattribué : rien de l'ancienne app ne doit survivre
    if a:
        db.log(f"App supprimée : {a['name']}")
    return {"ok": True}


# ---------- réglages, diagnostic ----------

@app.get("/api/settings")
def settings_get(request: Request):
    return {
        "settings": settings.describe(),
        "auth": {"enabled": auth.enabled(), "has_password": auth.has_password(),
                 "password_from_env": bool(auth.PASSWORD_ENV), "trusted": auth.trusted(request)},
        "version": VERSION,
        "update": selfupdate.summary(),
    }


@app.post("/api/update/check")
async def update_check():
    """Recherche immédiate d'une nouvelle version de tvsign (bouton des Réglages)."""
    await selfupdate.check(force=True)
    return selfupdate.summary()


@app.post("/api/update/dismiss")
def update_dismiss():
    selfupdate.dismiss()
    return {"ok": True}


@app.patch("/api/settings")
def settings_patch(body: dict):
    try:
        return {"settings": settings.update(body)}
    except ValueError as e:
        raise HTTPException(400, e.args[0] if e.args and isinstance(e.args[0], dict) else str(e)) from None


class NotifyTest(BaseModel):
    url: Optional[str] = None


@app.post("/api/settings/test-notification")
async def settings_test_notification(body: NotifyTest):
    """Ancien point d'entrée (adresse ntfy seule) ; l'interface utilise /api/notifications/test."""
    url = (body.url or "").strip() or settings.ntfy_url()
    try:
        await notifier.test({"type": "ntfy", "url": url})
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except notifier.NotifyFailed as e:
        raise HTTPException(502, e.api_detail) from None
    return {"ok": True}


@app.get("/api/diagnostics")
async def diagnostics():
    return await diag.run_all()


# ---------- sauvegarde ----------

class _SelfDeletingFile(FileResponse):
    """Réponse fichier dont le fichier disparaît à la fin de l'envoi, réussi, interrompu ou annulé."""

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)


@app.get("/api/backup/export")
async def backup_export(uploads: bool = False):
    if proc.busy():
        # connexion Apple (code 2FA en attente) ou appairage en cours : plumesign est tenu, l'export
        # resterait suspendu jusqu'à 15 min sans rien dire
        raise HTTPException(409, "busy")
    try:
        async with plume._locked():      # accounts.json pas en cours de réécriture
            path = await asyncio.to_thread(backup.export, uploads)
    except plume.Busy:
        raise HTTPException(503, "plumesign_busy") from None
    # l'archive contient les secrets : supprimée même si le téléchargement est interrompu
    return _SelfDeletingFile(path, filename=path.name, media_type="application/zip")


@app.post("/api/backup/import")
async def backup_import(file: UploadFile = File(...)):
    if renew._RUNNING or _RENEW_ALL_PENDING or proc.busy() or backup.RESTORING:
        raise HTTPException(409, "busy")
    tmp = TMP_DIR / f"import-{int(time.time())}.zip"
    try:
        limit, got = MAX_UPLOAD_MB * 1024 * 1024, 0
        with open(tmp, "wb") as f:
            while chunk := await file.read(1 << 20):
                got += len(chunk)
                if got > limit:
                    raise HTTPException(413, f"too_large:{MAX_UPLOAD_MB}")
                f.write(chunk)
        # l'envoi a pu durer : un renouvellement a pu démarrer entre-temps. Aucun await entre cette
        # vérification et le drapeau : la boucle et l'API se mettent en retrait avant tout remplacement.
        if renew._RUNNING or _RENEW_ALL_PENDING or proc.busy() or backup.RESTORING:
            raise HTTPException(409, "busy")
        backup.RESTORING = True
        try:
            async with plume._locked():
                try:
                    counts = await asyncio.to_thread(backup.restore, tmp)
                except backup.BackupError as e:
                    raise HTTPException(400 if e.code != "restore_failed" else 500, e.code) from None
                except ValueError as e:
                    raise HTTPException(400, str(e)) from None
            auth.load_revoked()
            _purge_half_added()      # une sauvegarde faite pendant un ajout contient une fiche « en cours d'ajout »
            renew.startup()
            await devices.sync_store()
        except plume.Busy:
            raise HTTPException(503, "plumesign_busy") from None
        finally:
            backup.RESTORING = False
        return {"ok": True, **counts}
    finally:
        tmp.unlink(missing_ok=True)


# ---------- routes ajoutées : notifications, journal, support (voir routes_extra.py) ----------
from . import routes_extra  # noqa: E402

app.include_router(routes_extra.router)
