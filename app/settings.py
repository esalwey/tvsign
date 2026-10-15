"""User settings: environment variable > value stored in the database > default.

A setting fixed by the environment is shown read-only in the UI ("set by TVSIGN_...").
The same table also holds internal values (admin password hash, onboarding flag...),
which are never exposed through /api/settings.
"""
import os
import re

from . import db


class Spec:
    def __init__(self, default, kind, envs, check=None):
        self.default, self.kind, self.envs, self.check = default, kind, envs, check


_URL = re.compile(r"^https?://[^\s/]+(/\S*)?$")


def _url(value: str) -> None:
    # tout le texte est contrôlé, pas seulement le début : « http://a b » est refusée
    if value and not _URL.match(value):
        raise ValueError("adresse http(s):// attendue")


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
    # iPhone / iPad : souvent hors du réseau ou en veille, on commence plus tôt (jamais moins que le réglage ci-dessus)
    "renew_before_hours_mobile": Spec(96.0, float, ["TVSIGN_RENEW_BEFORE_HOURS_MOBILE"], _hours),
    # vide = serveur par défaut de plumesign (https://ani.sidestore.app)
    "anisette_url": Spec("", str, ["TVSIGN_ANISETTE_URL", "PLUMESIGN_ANISETTE_URL", "ANISETTE_URL_V3"], _url),
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
        except ValueError:
            if name not in _WARNED:
                _WARNED.add(name)
                print(f"[warn] {name}={value!r} ignorée : valeur invalide", flush=True)
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


def renew_before_hours_for(device: dict | None) -> float:
    """Délai de renouvellement d'un appareil : le sien s'il en a un, sinon celui de sa famille
    (iPhone / iPad : au moins `renew_before_hours_mobile`), sinon le réglage général."""
    base = renew_before_hours()
    if not device:
        return base
    own = device.get("renew_before_hours")
    if own:
        return float(own)
    if device.get("family") in ("ios", "ipados", "visionos"):
        return max(base, float(get("renew_before_hours_mobile")))
    return base


def ntfy_url() -> str:
    return get("ntfy_url")


def anisette_url() -> str:
    return get("anisette_url")
