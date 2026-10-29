"""User settings: environment variable > value stored in the database > default.

A setting fixed by the environment is shown read-only in the UI ("set by TVSIGN_...").
The same table also holds internal values (admin password hash, onboarding flag...),
which are never exposed through /api/settings.
"""
import os
import re

from . import db

# Serveurs anisette essayés par plumesign, dans cet ordre (c'est aussi la liste de omnisette, voir
# vendor/Impactor/PATCHES.md) : le premier qui répond et dont Apple accepte les données sert, et le dernier
# qui a marché passe en tête la fois suivante. Aucune plateforme n'est imposée : la liste change au gré de
# l'écosystème (Android jusqu'au 12 octobre 2026, Windows jusqu'au 22, macOS depuis), les données d'un serveur
# partent avec le client info et le user agent qu'il annonce, et c'est la réponse d'Apple qui décide.
DEFAULT_ANISETTE_SERVERS = (
    "https://mac-ani.jkcoxson.com",
    "https://ani.iloader.app",
    "https://new.ani.sidestore.io",
    "https://ani3.stikstore.app",
    "https://ani.jkcoxson.com",
)
DEFAULT_ANISETTE = DEFAULT_ANISETTE_SERVERS[0]


class Spec:
    def __init__(self, default, kind, envs, check=None):
        self.default, self.kind, self.envs, self.check = default, kind, envs, check


def _url(value: str) -> None:
    if value and not re.match(r"^https?://[^\s/]+", value):
        raise ValueError("adresse http(s):// attendue")


def _hours(value: float) -> None:
    # profil gratuit = 7 jours (168 h) ; garder au moins 12 h de marge et un renouvellement possible
    if not 12 <= value <= 150:
        raise ValueError("entre 12 et 150 heures")


def _language(value: str) -> None:
    if value not in ("auto", "en", "fr"):
        raise ValueError("auto, en ou fr")


def _fallback(value: str) -> None:
    if value not in ("on", "off"):
        raise ValueError("on ou off")


def _header(value: str) -> None:
    """Une valeur d'en-tête HTTP : ASCII imprimable et espaces (plumesign ignorerait le reste)."""
    if value and not all(c == " " or (c.isascii() and c.isprintable()) for c in value):
        raise ValueError("caractères ASCII imprimables seulement")
    if len(value) > 300:
        raise ValueError("300 caractères au plus")


PUBLIC: dict[str, Spec] = {
    "language": Spec("auto", str, ["TVSIGN_LANGUAGE"], _language),
    "ntfy_url": Spec("", str, ["TVSIGN_NTFY_URL"], _url),
    "renew_before_hours": Spec(60.0, float, ["TVSIGN_RENEW_BEFORE_HOURS"], _hours),
    # vide = les serveurs par défaut de plumesign (DEFAULT_ANISETTE_SERVERS) ; une adresse est essayée en
    # premier, les serveurs par défaut derrière elle (sauf si anisette_fallback = off)
    "anisette_url": Spec("", str, ["TVSIGN_ANISETTE_URL", "PLUMESIGN_ANISETTE_URL", "ANISETTE_URL_V3"], _url),
    # off : le serveur choisi est utilisé seul, sans repli sur les serveurs par défaut
    "anisette_fallback": Spec("on", str, ["TVSIGN_ANISETTE_FALLBACK"], _fallback),
    # Issue de secours, vide par défaut : le client info et le user agent envoyés à Apple à la place de ceux
    # que le serveur annonce (ils vont par paire : changer les deux)
    "anisette_client_info": Spec("", str, ["TVSIGN_ANISETTE_CLIENT_INFO", "PLUMESIGN_ANISETTE_CLIENT_INFO"], _header),
    "anisette_user_agent": Spec("", str, ["TVSIGN_ANISETTE_USER_AGENT", "PLUMESIGN_ANISETTE_USER_AGENT"], _header),
}


_WARNED: set[str] = set()


def _env(spec: Spec) -> tuple[str, str] | None:
    """Première variable d'environnement définie ET valide (une valeur invalide est ignorée)."""
    for name in spec.envs:
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        try:
            _convert(spec, value)
        except ValueError as e:
            if name not in _WARNED:
                _WARNED.add(name)
                print(f"[warn] {name}={value!r} ignorée : {e}", flush=True)
            continue
        return name, value
    return None


def _convert(spec: Spec, raw):
    if spec.kind is float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError("nombre attendu") from None
    else:
        value = str(raw if raw is not None else "").strip()
    if spec.check:
        spec.check(value)
    return value


# ---------- internal key/value ----------

def raw_get(key: str) -> str | None:
    r = db.row("SELECT value FROM settings WHERE key = ?", key)
    return r["value"] if r else None


def raw_set(key: str, value: str | None) -> None:
    if value is None:
        db.execute("DELETE FROM settings WHERE key = ?", key)
    else:
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, str(value))


# ---------- public settings ----------

def get(key: str):
    spec = PUBLIC[key]
    env = _env(spec)
    if env:
        return _convert(spec, env[1])
    stored = raw_get(key)
    if stored is not None:
        try:
            return _convert(spec, stored)
        except ValueError:
            pass
    return spec.default


def describe() -> dict:
    out = {}
    for key, spec in PUBLIC.items():
        env = _env(spec)
        stored = raw_get(key)
        out[key] = {
            "value": get(key),
            "default": spec.default,
            "source": "env" if env else "db" if stored is not None else "default",
            "locked": bool(env),
            "env": env[0] if env else spec.envs[0],
        }
    return out


def update(values: dict) -> dict:
    """Validates everything first, then writes. None resets to the default."""
    errors, todo = {}, {}
    for key, raw in values.items():
        spec = PUBLIC.get(key)
        if not spec:
            errors[key] = "réglage inconnu"
            continue
        if _env(spec):
            errors[key] = f"défini par {_env(spec)[0]}"
            continue
        if raw is None or raw == "":
            todo[key] = None
            continue
        try:
            todo[key] = _convert(spec, raw)
        except ValueError as e:
            errors[key] = str(e)
    if errors:
        raise ValueError(errors)
    for key, value in todo.items():
        raw_set(key, None if value is None else str(value))
    return describe()


def renew_before_hours() -> float:
    return float(get("renew_before_hours"))


def ntfy_url() -> str:
    return get("ntfy_url")


def anisette_url() -> str:
    """Serveur anisette choisi par l'utilisateur, essayé en premier ; vide = les serveurs par défaut.
    Aucun serveur n'est refusé d'après son nom : c'est sa réponse, puis celle d'Apple, qui décident."""
    return get("anisette_url")


def anisette_fallback() -> bool:
    """False si l'utilisateur a désactivé le repli sur les serveurs par défaut (serveur choisi seul)."""
    return get("anisette_fallback") != "off"


def anisette_client_info() -> str:
    return get("anisette_client_info")


def anisette_user_agent() -> str:
    return get("anisette_user_agent")


def same_server(a: str, b: str) -> bool:
    """Même adresse de serveur, à la casse et à la barre finale près (comme plumesign)."""
    return a.strip().rstrip("/").lower() == b.strip().rstrip("/").lower()


def anisette_servers() -> list[str]:
    """Serveurs que plumesign essaiera, dans l'ordre, comme lui : le serveur choisi par l'utilisateur
    d'abord, puis les serveurs par défaut (sans lui une seconde fois) ; le repli désactivé, le serveur
    choisi est seul (sans serveur choisi : le premier des serveurs par défaut). Le dernier serveur qui a
    marché passe en tête chez plumesign, ce que cette liste ne dit pas (voir plume.last_server)."""
    custom = anisette_url()
    first = custom or DEFAULT_ANISETTE
    servers = [first]
    if anisette_fallback():
        servers += [s for s in DEFAULT_ANISETTE_SERVERS if not same_server(s, first)]
    return servers
