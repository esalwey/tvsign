"""tvsign : signe et maintient installées des apps tvOS sur des Apple TV, avec un compte Apple gratuit."""
import asyncio
import re
import shutil
import time
import zipfile
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, backup, db, devices, diag, ipa, plume, proc, renew, settings
from .config import MAX_UPLOAD_MB, ROOT, SHUTDOWN_DRAIN_SECONDS, TMP_DIR, VERSION, WINDOWS
from .proc import SESSIONS
from .util import normalize_repo


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    _purge_half_added()
    backup.prune_old()
    auth.init()
    renew.startup()            # un renouvellement interrompu ne reste pas « en cours »
    plume.import_existing_store()
    await devices.sync_store()
    if settings.raw_get("onboarded") is None and (db.rows("SELECT id FROM apps LIMIT 1")
                                                  or db.rows("SELECT id FROM devices LIMIT 1")):
        settings.raw_set("onboarded", "1")   # installation existante : pas d'assistant
    for f in TMP_DIR.glob("*"):
        if f.is_file() and time.time() - f.stat().st_mtime > 3600:
            f.unlink(missing_ok=True)
    task = asyncio.create_task(renew.loop())
    yield
    # arrêt : une installation en cours peut finir avant que l'arrêt ne l'interrompe
    await renew.drain(SHUTDOWN_DRAIN_SECONDS)
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
UPLOAD_PATTERN = re.compile(r"^/api/apps/\d+/ipa$")      # remplacement de l'IPA d'une app importée
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
        upload = path in UPLOADS or bool(UPLOAD_PATTERN.match(path))
        limit = MAX_UPLOAD_MB * 1024 * 1024 if upload else 1024 * 1024
        if length is None and upload:
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
        response.set_cookie(value=auth.make_cookie(), **auth.cookie_args(request))
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
        "edition": "windows" if WINDOWS else "docker",
    }


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


class LoginBody(BaseModel):
    password: str


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
    code: Optional[str] = None
    password: Optional[str] = None
    language: Optional[str] = None


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
def auth_logout():
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
        # moment où la boucle lancera le renouvellement automatique (None : dès que possible)
        a["renew_at"] = exp - before * 3600 if exp else None
        a["effective_account"] = a["account_email"] or default
        a["tv_present"], a["tv_checked_at"] = tv_presence(a)
    health = renew.health(now)
    return {
        "now": now,
        "version": VERSION,
        "renew_before_hours": before,
        "notifications": health["notifications"],
        "auth_disabled": not auth.enabled(),
        "health": health,
        "apps": apps,
        "devices": db.rows("SELECT * FROM devices ORDER BY name"),
        "accounts": plume.accounts(),
        "events": db.rows("SELECT * FROM events ORDER BY id DESC LIMIT 80"),
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
    return plume.start_login(body.email.strip(), body.password).public()


@app.post("/api/accounts/{email}/check")
async def account_check(email: str):
    return {"ok": await plume.check_account(email)}


@app.post("/api/accounts/{email}/select")
async def account_select(email: str):
    try:
        await plume.select(email)
    except plume.UnknownAccount:
        raise HTTPException(404, "unknown_account") from None
    return {"ok": True}


@app.delete("/api/accounts/{email}")
async def account_delete(email: str):
    try:
        await plume.logout(email)
    except plume.UnknownAccount:
        raise HTTPException(404, "unknown_account") from None
    except RuntimeError as e:
        db.log(f"Déconnexion de {email} échouée : {e}", "error")
        raise HTTPException(502, "logout_failed") from None
    return {"ok": True}


# ---------- Apple TV ----------

@app.post("/api/devices/discover")
async def device_discover():
    try:
        return await devices.discover()
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
        return await devices.refresh_info(device_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


@app.get("/api/devices/{device_id}/apps")
async def device_apps(device_id: int):
    try:
        return await devices.installed_apps(device_or_404(device_id))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


def _is_installed(bundle_id: str, installed: set[str]) -> bool:
    """L'identifiant installé est celui de l'IPA, suffixé de l'identifiant d'équipe Apple à la signature
    (com.exemple.app.ABCDE12345)."""
    b = bundle_id.lower()
    return any(i == b or (i.startswith(b + ".") and re.fullmatch(r"[0-9a-z]{10}", i[len(b) + 1:]))
               for i in installed)


def _signed_id(app_id: int) -> str:
    """Identifiant exact de la dernière IPA signée (celui installé sur la TV), en minuscules ; vide si illisible."""
    try:
        return (ipa.app_info(renew.app_dir(app_id) / "signed.ipa")["bundle_id"] or "").lower()
    except Exception:  # noqa: BLE001 - pas de signature, ou fichier partiel
        return ""


@app.post("/api/devices/{device_id}/verify")
async def device_verify(device_id: int):
    """Les apps suivies sont-elles encore sur l'Apple TV ? (lecture seule : rien n'est signé ni installé)."""
    device = device_or_404(device_id)
    try:
        listed = await devices.installed_apps(device)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))
    installed = {str(x.get("bundle_id") or "").lower() for x in listed if isinstance(x, dict)} - {""}
    now, out = time.time(), []
    for a in db.rows("SELECT id, bundle_id FROM apps WHERE device_id = ? AND bundle_id IS NOT NULL "
                     "AND status != 'adding'", device_id):
        if a["id"] in renew._RUNNING:
            continue        # installation en cours : l'absence ne dirait rien
        present = _is_installed(a["bundle_id"], installed) or _signed_id(a["id"]) in installed
        db.update("apps", a["id"], on_tv=int(present), on_tv_checked_at=now)
        out.append({"app_id": a["id"], "present": present})
    return {"ok": True, "checked_at": now, "apps": out}


class DevicePatch(BaseModel):
    name: str


@app.patch("/api/devices/{device_id}")
def device_patch(device_id: int, body: DevicePatch):
    device_or_404(device_id)
    db.update("devices", device_id, name=body.name.strip() or "Apple TV")
    return {"ok": True}


@app.delete("/api/devices/{device_id}")
async def device_delete(device_id: int):
    d = device_or_404(device_id)
    # l'appairage d'abord : s'il reste dans le store, sync_store recrée la TV au prochain démarrage
    try:
        await devices.forget(d)
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from None
    db.execute("DELETE FROM devices WHERE id = ?", device_id)
    return {"ok": True}


# ---------- apps ----------

@app.get("/api/presets")
def presets():
    return ipa.presets()


async def _check_source(source_type: str, source: str | None) -> str | None:
    source = (source or "").strip() or None
    if source_type == "github":
        source = normalize_repo(source)
    if source_type == "github" and not re.fullmatch(r"[\w.-]+/[\w.-]+", source or ""):
        raise HTTPException(400, "invalid_repo")
    if source_type == "url":
        if not (source or "").startswith("https://"):
            raise HTTPException(400, "https_only")
        try:
            # résolution DNS hors de la boucle d'événements : un résolveur lent ne gèle pas l'interface
            await asyncio.to_thread(ipa._check_url, source)
        except (ValueError, ConnectionError) as e:
            raise HTTPException(400, str(e)) from None
    return source


def _abort_add(app_id: int) -> None:
    db.execute("DELETE FROM apps WHERE id = ?", app_id)
    shutil.rmtree(renew.app_dir(app_id), ignore_errors=True)


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
    account = account_email or plume.default_account()
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
            await renew.refresh_source(db.row("SELECT * FROM apps WHERE id = ?", app_id))
        try:
            info = ipa.app_info(dest)
        except (zipfile.BadZipFile, ValueError):
            raise HTTPException(400, "not_an_ipa") from None
        if info["platform"] != "appletvos":
            raise HTTPException(400, f"not_tvos:{info['platform'] or 'unknown'}")
        # même app, même compte : une 2e fiche signerait deux fois et les copies s'écraseraient sur la TV
        if info["bundle_id"] and db.row(
                "SELECT id FROM apps WHERE id != ? AND bundle_id = ? AND COALESCE(account_email, '') = ?",
                app_id, info["bundle_id"], account or ""):
            raise HTTPException(409, "already_added")
    except HTTPException:
        _abort_add(app_id)
        raise
    except Exception as e:  # noqa: BLE001
        _abort_add(app_id)
        raise HTTPException(400, str(e))

    db.update("apps", app_id, name=info["name"], bundle_id=info["bundle_id"], version=info["version"], enabled=1,
              status="pending")
    db.log(f"App ajoutée : {info['name']} {info['version']} ({info['bundle_id']})", app_id=app_id)
    renew.spawn(app_id, "ajout")
    return db.row("SELECT * FROM apps WHERE id = ?", app_id)


class AppPatch(BaseModel):
    enabled: Optional[bool] = None
    device_id: Optional[int] = None
    account_email: Optional[str] = None
    source: Optional[str] = None           # dépôt GitHub (owner/repo) ou URL https
    source_filter: Optional[str] = None    # préfixe de tag (github)
    channel: Optional[str] = None          # stable | beta (github)


def app_or_404(app_id: int) -> dict:
    a = db.row("SELECT * FROM apps WHERE id = ?", app_id)
    if not a:
        raise HTTPException(404, "unknown_app")
    return a


@app.patch("/api/apps/{app_id}")
async def app_patch(app_id: int, body: AppPatch):
    a = app_or_404(app_id)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("enabled") is None:
        fields.pop("enabled", None)          # null n'a pas de sens : ignoré
    else:
        fields["enabled"] = int(fields["enabled"])
    if "account_email" in fields:
        fields["account_email"] = fields["account_email"] or plume.default_account()
    if "source" in fields:
        fields["source"] = await _check_source(a["source_type"], fields["source"])
        if a["preset"] and fields["source"] != a["source"]:
            fields["preset"] = None            # source personnalisée : plus le préréglage
            fields["asset_name"] = None
    if "channel" in fields:
        fields["channel"] = "beta" if fields["channel"] == "beta" else "stable"
    if "device_id" in fields and fields["device_id"] is not None:
        if not db.row("SELECT id FROM devices WHERE id = ?", fields["device_id"]):
            raise HTTPException(400, "unknown_device")
    source_changed = any(k in fields for k in ("source", "source_filter", "channel"))
    if source_changed:
        fields["source_tag"] = None        # nouvelle source : la prochaine release sera installée
        fields["source_key"] = None
        fields["bad_tag"] = None
        fields["bad_key"] = None
        fields["bad_reason"] = None
    dest_changed = "device_id" in fields or "account_email" in fields
    if dest_changed:
        # autre appareil/compte : la signature déjà prête ne convient plus
        (renew.app_dir(app_id) / "signed.ipa").unlink(missing_ok=True)
        fields["next_attempt_at"] = None
        fields["on_tv"] = fields["on_tv_checked_at"] = None     # la vérification visait l'ancienne TV
    db.update("apps", app_id, **fields)
    if dest_changed:
        renew.rerun(app_id)    # un renouvellement en cours vise l'ancienne destination
    if source_changed:
        renew.spawn(app_id, "nouvelle source")
    return db.row("SELECT * FROM apps WHERE id = ?", app_id)


@app.post("/api/apps/{app_id}/renew")
async def app_renew(app_id: int):
    app_or_404(app_id)
    db.update("apps", app_id, next_attempt_at=None)
    return {"ok": True, "started": renew.spawn(app_id, "manuel")}


async def _renew_all(ids: list[int]) -> None:
    """Un à un : plumesign signe sous verrou, lancer tout d'un coup ne ferait qu'empiler des attentes."""
    for app_id in ids:
        if renew.STOPPING:
            break
        await renew.renew(app_id, "manuel")


@app.post("/api/apps/renew-all")
async def app_renew_all():
    """Renouvelle maintenant toutes les apps actives qui ont une Apple TV, l'une après l'autre."""
    ids = [a["id"] for a in db.rows("SELECT id FROM apps WHERE enabled = 1 AND device_id IS NOT NULL "
                                    "AND status != 'adding' ORDER BY expires_at IS NULL, expires_at, id")
           if a["id"] not in renew._RUNNING]
    for app_id in ids:
        db.update("apps", app_id, next_attempt_at=None)
    if ids:
        proc.spawn(_renew_all(ids))
    return {"ok": True, "started": len(ids)}


@app.post("/api/apps/{app_id}/retry-version")
async def app_retry_version(app_id: int):
    """Réessaie la version refusée (téléchargement raté jugé invalide, empreinte…) sans toucher à la source."""
    a = app_or_404(app_id)
    db.update("apps", app_id, bad_tag=None, bad_key=None, bad_reason=None, next_attempt_at=None)
    return {"ok": True, "started": renew.spawn(app_id, "nouvelle version" if a["status"] == "ok" else "manuel")}


@app.post("/api/apps/{app_id}/accept-version")
async def app_accept_version(app_id: int):
    """Accepte une version dont l'identifiant d'app a changé : l'ancien identifiant n'est plus exigé."""
    app_or_404(app_id)
    db.update("apps", app_id, bundle_id=None, bad_tag=None, bad_key=None, bad_reason=None, next_attempt_at=None)
    db.log("Changement d'identifiant d'app accepté", app_id=app_id)
    return {"ok": True, "started": renew.spawn(app_id, "nouvelle version")}


@app.post("/api/apps/{app_id}/ipa")
async def app_replace_ipa(app_id: int, file: UploadFile = File(...)):
    """Remplace l'IPA d'une app importée par une nouvelle build (même identifiant), sans la supprimer."""
    a = app_or_404(app_id)
    if a["source_type"] != "upload":
        raise HTTPException(400, "not_uploaded_app")
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
        except (zipfile.BadZipFile, ValueError):
            raise HTTPException(400, "not_an_ipa") from None
        if info["platform"] != "appletvos":
            raise HTTPException(400, f"not_tvos:{info['platform'] or 'unknown'}")
        if a["bundle_id"] and info["bundle_id"] != a["bundle_id"]:
            raise HTTPException(409, "bundle_changed")
        folder = renew.app_dir(app_id)
        shutil.move(str(tmp), str(folder / "original.ipa"))
        (folder / "signed.ipa").unlink(missing_ok=True)        # l'ancienne signature ne convient plus
        db.update("apps", app_id, version=info["version"], name=info["name"], next_attempt_at=None)
    finally:
        tmp.unlink(missing_ok=True)
    db.log(f"App remplacée : {info['name']} {info['version']}", app_id=app_id)
    return {"ok": True, "started": renew.spawn(app_id, "manuel"), "version": info["version"]}


@app.delete("/api/apps/{app_id}")
def app_delete(app_id: int):
    if app_id in renew._RUNNING:
        # un renouvellement en cours referait le dossier, signerait et installerait une app qu'on ne suit plus
        raise HTTPException(409, "app_busy")
    a = db.row("SELECT name FROM apps WHERE id = ?", app_id)
    db.execute("DELETE FROM apps WHERE id = ?", app_id)
    db.execute("DELETE FROM events WHERE app_id = ?", app_id)   # sinon l'identifiant réutilisé hérite de ce journal
    shutil.rmtree(renew.app_dir(app_id), ignore_errors=True)
    renew.PHASE.pop(app_id, None)
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
    }


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
    url = (body.url or "").strip() or None
    if url and not re.match(r"^https?://", url):
        raise HTTPException(400, "invalid_url")
    if not (url or settings.ntfy_url()):
        raise HTTPException(400, "no_ntfy_url")
    try:
        await renew.notify_test(url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))
    return {"ok": True}


@app.get("/api/diagnostics")
async def diagnostics():
    return await diag.run_all()


# ---------- sauvegarde ----------

@app.get("/api/backup/export")
async def backup_export(uploads: bool = False):
    async with plume._locked():      # accounts.json pas en cours de réécriture
        path = await asyncio.to_thread(backup.export, uploads)
    return FileResponse(path, filename=path.name, media_type="application/zip",
                        background=BackgroundTask(path.unlink, missing_ok=True))


@app.post("/api/backup/import")
async def backup_import(file: UploadFile = File(...)):
    if renew._RUNNING or proc.busy():
        raise HTTPException(409, "restore_busy")
    tmp = TMP_DIR / f"import-{int(time.time())}.zip"
    try:
        limit, got = MAX_UPLOAD_MB * 1024 * 1024, 0
        with open(tmp, "wb") as f:
            while chunk := await file.read(1 << 20):
                got += len(chunk)
                if got > limit:
                    raise HTTPException(413, f"too_large:{MAX_UPLOAD_MB}")
                f.write(chunk)
        async with plume._locked():
            try:
                counts = await asyncio.to_thread(backup.restore, tmp)
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
        renew.startup()
        await devices.sync_store()
        return {"ok": True, **counts}
    finally:
        tmp.unlink(missing_ok=True)
