"""User settings: environment variable > value stored in the database > default.

A setting fixed by the environment is shown read-only in the UI ("set by TVSIGN_...").
The same table also holds internal values (admin password hash, onboarding flag...),
which are never exposed through /api/settings.
"""
import os
import re
import urllib.parse

from . import db

# Serveur anisette par défaut de plumesign : il fournit des données Windows, les seules qu'Apple accepte
# encore. Le serveur de repli prend le relais quand il ne répond pas (réglage vide seulement).
DEFAULT_ANISETTE = "https://ani.jkcoxson.com"
FALLBACK_ANISETTE = "https://ani2.stikstore.app"
# Serveurs anisette « Android » : depuis le 12 octobre 2026 Apple refuse leurs données (erreur -80045,
# puis HTTP 503). Une adresse qui pointe vers l'un d'eux est remplacée par le serveur par défaut.
LEGACY_ANISETTE_HOSTS = frozenset({
    "ani.sidestore.app", "ani.stikstore.app", "anisette.crystall1ne.dev",
    "ani3server.fly.dev", "ani.jaydenha.uk", "omni.ethxn.xyz",
})


class Spec:
    def __init__(self, default, kind, envs, check=None):
        self.default, self.kind, self.envs, self.check = default, kind, envs, check


def _url(value: str) -> None:
    if value and not re.match(r"^https?://[^\s/]+", value):
        raise ValueError("adresse http(s):// attendue")


def anisette_host(url: str) -> str:
    """Hôte (minuscules, sans port ni identifiants) d'une adresse de serveur anisette."""
    value = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(value if "://" in value else "//" + value)
        return (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_legacy_anisette(url: str) -> bool:
    """Serveur anisette Android, dont Apple refuse les données."""
    return anisette_host(url) in LEGACY_ANISETTE_HOSTS


def _anisette(value: str) -> None:
    _url(value)
    if value and is_legacy_anisette(value):
        raise ValueError(f"serveur anisette Android, refusé par Apple depuis le 12 octobre 2026 : "
                         f"laisser vide pour le serveur par défaut ({anisette_host(DEFAULT_ANISETTE)})")


def _hours(value: float) -> None:
    # profil gratuit = 7 jours (168 h) ; garder au moins 12 h de marge et un renouvellement possible
    if not 12 <= value <= 150:
        raise ValueError("entre 12 et 150 heures")


def _language(value: str) -> None:
    if value not in ("auto", "en", "fr"):
        raise ValueError("auto, en ou fr")


PUBLIC: dict[str, Spec] = {
    "language": Spec("auto", str, ["TVSIGN_LANGUAGE"], _language),
    "ntfy_url": Spec("", str, ["TVSIGN_NTFY_URL"], _url),
    "renew_before_hours": Spec(60.0, float, ["TVSIGN_RENEW_BEFORE_HOURS"], _hours),
    # vide = serveur par défaut de plumesign (DEFAULT_ANISETTE, repli FALLBACK_ANISETTE)
    "anisette_url": Spec("", str, ["TVSIGN_ANISETTE_URL", "PLUMESIGN_ANISETTE_URL", "ANISETTE_URL_V3"], _anisette),
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


def migrate() -> None:
    """Au démarrage : un serveur anisette Android enregistré (réglage ou variable d'environnement) ne
    marche plus, on le remplace par le serveur par défaut et on le dit dans le journal."""
    stored = raw_get("anisette_url")
    if stored and is_legacy_anisette(stored):
        raw_set("anisette_url", None)
        db.log(f"Serveur anisette {stored} remplacé par le serveur par défaut ({DEFAULT_ANISETTE}) : "
               "Apple refuse les données des serveurs anisette Android depuis le 12 octobre 2026 "
               "(erreur -80045)", "warn")
    for name in PUBLIC["anisette_url"].envs:
        value = os.environ.get(name, "").strip()
        if value and is_legacy_anisette(value):
            db.log(f"{name}={value} pointe vers un serveur anisette Android, refusé par Apple : ignoré, "
                   f"le serveur par défaut ({DEFAULT_ANISETTE}) est utilisé. Retire cette variable.", "warn")


def renew_before_hours() -> float:
    return float(get("renew_before_hours"))


def ntfy_url() -> str:
    return get("ntfy_url")


def anisette_url() -> str:
    """Serveur anisette choisi par l'utilisateur ; vide = serveur par défaut avec son serveur de repli.
    Une adresse de serveur Android n'est jamais renvoyée (valeur invalide : défaut)."""
    return get("anisette_url")


def same_server(a: str, b: str) -> bool:
    """Même adresse de serveur, à la casse et à la barre finale près (comme plumesign)."""
    return a.strip().rstrip("/").lower() == b.strip().rstrip("/").lower()


def anisette_servers() -> list[str]:
    """Serveurs que plumesign essaiera, dans l'ordre. Comme lui : le réglage vide, ou l'adresse par défaut
    saisie telle quelle, donne le serveur par défaut et son repli ; tout autre serveur est utilisé seul."""
    custom = anisette_url()
    if custom and not same_server(custom, DEFAULT_ANISETTE):
        return [custom]
    return [DEFAULT_ANISETTE, FALLBACK_ANISETTE]
