"""Admin authentication, first-run setup code, session cookie, CSRF and Host checks.

- One admin password (scrypt, stdlib), stored in the settings table, or preset by
  TVSIGN_PASSWORD / TVSIGN_PASSWORD_FILE. TVSIGN_AUTH=none disables it (reverse proxy / VPN).
- First run: until a password exists, the API only answers the setup routes. Setup needs a
  one-time code printed in the logs and written to <data>/setup-code.txt, so a LAN neighbour
  cannot claim the instance first.
- Desktop edition (TVSIGN_TRUST_LOOPBACK=1): a request from 127.0.0.1/::1 *with a loopback Host
  header* needs no setup code, and no password while none is set. The desktop window also gets
  a per-launch token (TVSIGN_LAUNCH_TOKEN, GET /auth/launch?token=...) that signs it in.
- Session: HMAC-signed cookie, HttpOnly, SameSite=Strict, Secure over HTTPS, 30 days sliding.
- Mutating requests need the X-Tvsign header and a same-origin Origin/Referer.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time

from . import db, settings
from .config import (ALLOWED_HOSTS, AUTH_MODE, DATA_DIR, LAUNCH_TOKEN, PASSWORD_ENV, SESSION_DAYS,
                     TRUST_LOOPBACK)
from .util import write_private

COOKIE = "tvsign_session"
SECRET_FILE = DATA_DIR / "secret.key"
SETUP_FILE = DATA_DIR / "setup-code.txt"
MIN_PASSWORD = 8

_SECRET: bytes | None = None
SETUP_CODE: str | None = None


def enabled() -> bool:
    return AUTH_MODE != "none"


# ---------- secret / password ----------

def secret() -> bytes:
    global _SECRET
    if _SECRET is None:
        try:
            _SECRET = bytes.fromhex(SECRET_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            _SECRET = b""
        if len(_SECRET) < 32:
            _SECRET = secrets.token_bytes(32)
            write_private(SECRET_FILE, _SECRET.hex())
    return _SECRET


_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        want = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt),
                             n=int(n), r=int(r), p=int(p), dklen=len(want), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError):
        return False


_ENV_HASH: str | None = None


def stored_hash() -> str | None:
    global _ENV_HASH
    if PASSWORD_ENV:
        if _ENV_HASH is None:
            _ENV_HASH = hash_password(PASSWORD_ENV)
        return _ENV_HASH
    return settings.raw_get("admin_password")


def has_password() -> bool:
    return bool(stored_hash())


def set_password(password: str | None) -> None:
    if PASSWORD_ENV:
        raise ValueError("mot de passe défini par TVSIGN_PASSWORD")
    if password is None:
        settings.raw_set("admin_password", None)
    else:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"au moins {MIN_PASSWORD} caractères")
        settings.raw_set("admin_password", hash_password(password))
    rotate_sessions()


def setup_done() -> bool:
    return has_password() or settings.raw_get("setup_done") == "1"


def setup_required() -> bool:
    return enabled() and not setup_done()


# ---------- setup code ----------

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # sans 0/O, 1/I


def init() -> None:
    """At startup: make sure the cookie key exists, print a setup code if setup is pending."""
    global SETUP_CODE
    secret()
    load_revoked()
    if PASSWORD_ENV and len(PASSWORD_ENV) < MIN_PASSWORD:
        print(f"[warn] le mot de passe prévu par TVSIGN_PASSWORD / TVSIGN_PASSWORD_FILE fait moins de "
              f"{MIN_PASSWORD} caractères : à renforcer", flush=True)
    if not enabled():
        print("[warn] TVSIGN_AUTH=none : interface sans mot de passe, à n'utiliser que derrière un "
              "proxy authentifiant ou un VPN", flush=True)
        return
    if setup_done():
        SETUP_FILE.unlink(missing_ok=True)
        return
    SETUP_CODE = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    write_private(SETUP_FILE, SETUP_CODE + "\n")
    print(f"[info] tvsign setup code: {SETUP_CODE}  (also in {SETUP_FILE})", flush=True)


def check_setup_code(code: str | None) -> bool:
    if not SETUP_CODE or not code:
        return False
    # en octets : compare_digest lève TypeError sur une chaîne non ASCII (tiret typographique collé…)
    return hmac.compare_digest(code.strip().upper().replace("-", "").replace(" ", "").encode("utf-8"),
                               SETUP_CODE.encode("utf-8"))


def finish_setup() -> None:
    global SETUP_CODE
    settings.raw_set("setup_done", "1")
    SETUP_CODE = None
    SETUP_FILE.unlink(missing_ok=True)


# ---------- session cookie ----------

def _version() -> str:
    return settings.raw_get("session_version") or "1"


def rotate_sessions() -> None:
    """« Se déconnecter partout » : invalide tous les cookies émis."""
    settings.raw_set("session_version", secrets.token_hex(4))


def _sign(payload: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(secret(), payload, hashlib.sha256).digest()).decode().rstrip("=")


# Sessions déconnectées individuellement (« Se déconnecter ») : leur cookie, même copié ailleurs, ne vaut plus rien.
_REVOKED: dict[str, float] = {}


def load_revoked() -> None:
    now = time.time()
    db.execute("DELETE FROM revoked_sessions WHERE until < ?", now)
    _REVOKED.clear()
    _REVOKED.update({r["sid"]: r["until"] for r in db.rows("SELECT sid, until FROM revoked_sessions")})


def revoke(sid: str) -> None:
    """Révoque une session. Un cookie expire de toute façon SESSION_DAYS après son émission."""
    now = time.time()
    until = now + SESSION_DAYS * 86400
    _REVOKED[sid] = until
    db.execute("INSERT INTO revoked_sessions (sid, until) VALUES (?, ?) "
               "ON CONFLICT(sid) DO UPDATE SET until = excluded.until", sid, until)
    for old in [s for s, u in _REVOKED.items() if u < now]:
        _REVOKED.pop(old, None)
    db.execute("DELETE FROM revoked_sessions WHERE until < ?", now)


def make_cookie(sid: str | None = None) -> str:
    """Cookie de session signé. sid : identifiant conservé d'un renouvellement à l'autre (sinon nouveau)."""
    now = int(time.time())
    body = {"v": _version(), "iat": now, "sid": sid or secrets.token_hex(8)}
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload.encode())}"


def read_cookie(value: str | None) -> dict | None:
    if not value or "." not in value:
        return None
    payload, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig.encode("utf-8"), _sign(payload.encode()).encode("utf-8")):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("v") != _version() or time.time() - data.get("iat", 0) > SESSION_DAYS * 86400:
        return None
    if not data.get("sid"):
        # cookie émis avant les sessions individuelles : un identifiant dérivé de sa signature permet de
        # le révoquer à la déconnexion (il reste valable tel quel jusqu'à son renouvellement)
        data["sid"] = "legacy-" + sig[:16]
    if data["sid"] in _REVOKED:
        return None
    return data


def cookie_args(request) -> dict:
    return dict(key=COOKIE, httponly=True, samesite="strict", max_age=SESSION_DAYS * 86400, path="/",
                secure=request.url.scheme == "https")


# ---------- request classification ----------

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def host_name(request) -> str:
    host = (request.headers.get("host") or "").lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _is_loopback_ip(ip: str | None) -> bool:
    try:
        return ipaddress.ip_address((ip or "").split("%")[0]).is_loopback
    except ValueError:
        return False


def is_local(request) -> bool:
    """Client loopback ET en-tête Host loopback (une page web qui rebinde un nom DNS vers
    127.0.0.1 garde son propre nom dans Host)."""
    client = request.client.host if request.client else None
    return _is_loopback_ip(client) and host_name(request) in _LOOPBACK_HOSTS


def trusted(request) -> bool:
    """Édition bureau : requête locale exemptée de code de setup (et de mot de passe s'il n'y en a pas)."""
    return TRUST_LOOPBACK and is_local(request)


def check_launch_token(token: str | None) -> bool:
    return bool(LAUNCH_TOKEN) and bool(token) and hmac.compare_digest(token.encode("utf-8"), LAUNCH_TOKEN.encode("utf-8"))


def authenticated(request) -> bool:
    if not enabled():
        return True
    if trusted(request) and not has_password():
        return True
    if is_local(request):
        bearer = request.headers.get("authorization", "")
        if bearer.lower().startswith("bearer ") and check_launch_token(bearer[7:].strip()):
            return True
    return read_cookie(request.cookies.get(COOKIE)) is not None


def host_allowed(request) -> bool:
    """Liste blanche TVSIGN_ALLOWED_HOSTS si définie. Sans mot de passe (TVSIGN_AUTH=none, ou
    bureau sans mot de passe), on n'accepte que des noms locaux : protège du DNS rebinding."""
    full = (request.headers.get("host") or "").lower()
    name = host_name(request)
    if ALLOWED_HOSTS:
        return full in ALLOWED_HOSTS or name in ALLOWED_HOSTS or name in _LOOPBACK_HOSTS
    if enabled() and (has_password() or not setup_done()):
        # mot de passe, ou setup en attente (code de setup exigé hors accès local de confiance) :
        # un nom DNS quelconque (proxy, ts.net) doit pouvoir terminer le setup
        return True
    return _local_name(name)


def _local_name(name: str) -> bool:
    if name in _LOOPBACK_HOSTS:
        return True
    try:
        ipaddress.ip_address(name.strip("[]"))
        return True
    except ValueError:
        pass
    return "." not in name or name.endswith((".local", ".lan", ".home.arpa", ".internal", ".localdomain", ".home"))


def same_origin(request) -> bool:
    """Origin (ou à défaut Referer) doit désigner le même hôte que Host. Absents : client non
    navigateur (curl, supervision), accepté — l'en-tête X-Tvsign reste exigé."""
    host = (request.headers.get("host") or "").lower()
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            if value == "null":
                return False
            rest = value.split("://", 1)[-1]
            return rest.split("/", 1)[0].lower() == host
    return True


# ---------- anti-bruteforce ----------

_FAILS: dict[str, list[float]] = {}


def throttle_wait(ip: str) -> float:
    """Secondes à attendre avant un nouvel essai : 5 échecs / 5 min, puis délai exponentiel."""
    now = time.time()
    fails = [t for t in _FAILS.get(ip, []) if now - t < 300]
    _FAILS[ip] = fails
    if len(fails) < 5:
        return 0.0
    delay = min(300.0, 2 ** (len(fails) - 4))
    return max(0.0, fails[-1] + delay - now)


def record_failure(ip: str) -> None:
    _FAILS.setdefault(ip, []).append(time.time())


def clear_failures(ip: str) -> None:
    _FAILS.pop(ip, None)
