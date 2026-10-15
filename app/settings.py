"""User settings: environment variable > value stored in the database > default.

A setting fixed by the environment is shown read-only in the UI ("set by TVSIGN_...").
The same table also holds internal values (admin password hash, onboarding flag...),
which are never exposed through /api/settings.
"""
import os
import re
import urllib.parse

from . import db

# Événements de notification qu'une destination peut recevoir (voir notifier.py).
NOTIFY_EVENTS = ("fail", "expiry", "recovered", "update", "account")


class Spec:
    def __init__(self, default, kind, envs, check=None):
        self.default, self.kind, self.envs, self.check = default, kind, envs, check


# Les validateurs lèvent ValueError(code) : l'API renvoie le code tel quel et l'interface le traduit
# (clés err_<code> de web/i18n.js).

def valid_url(value: str) -> str:
    """Adresse http(s) exploitable : hôte présent, ni espace ni caractère de contrôle, port valide."""
    value = value.strip()
    if not value or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise ValueError("url_invalid")
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port  # noqa: B018 - lève ValueError si le port est invalide
    except ValueError:
        raise ValueError("url_invalid") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url_invalid")
    return value


def _url(value: str):
    if value:
        valid_url(value)


def ntfy_address(value: str) -> str:
    """Adresse ntfy ; un simple nom de sujet (« mon-sujet ») devient https://ntfy.sh/mon-sujet."""
    value = value.strip()
    if value and "://" not in value and re.fullmatch(r"[\w.-]+", value):
        value = "https://ntfy.sh/" + value
    if value:
        valid_url(value)
    return value


def _ntfy(value: str):
    return ntfy_address(value)


def mask_url(url: str) -> str:
    """Cache le mot de passe d'une adresse https://utilisateur:mot-de-passe@hôte/sujet."""
    return re.sub(r"^(\w+://[^/@:\s]*:)[^/@\s]*@", r"\1***@", url or "")


def _hours(value: float) -> None:
    # profil gratuit = 7 jours (168 h) ; garder au moins 12 h de marge et un renouvellement possible
    if not 12 <= value <= 150:
        raise ValueError("hours_range")


def _language(value: str) -> None:
    if value not in ("auto", "en", "fr"):
        raise ValueError("language_invalid")


def _events(value: str):
    """Événements reçus par la destination ntfy : liste séparée par des virgules ; vide = tous,
    « none » = aucun."""
    items = [x.strip() for x in value.split(",") if x.strip()]
    if any(x not in NOTIFY_EVENTS + ("none",) for x in items):
        raise ValueError("events_invalid")
    return ",".join(dict.fromkeys(items))


def events_of(value: str) -> list[str]:
    items = [x.strip() for x in (value or "").split(",") if x.strip() in NOTIFY_EVENTS]
    if (value or "").strip() == "none":
        return []
    return items or list(NOTIFY_EVENTS)


PUBLIC: dict[str, Spec] = {
    "language": Spec("auto", str, ["TVSIGN_LANGUAGE"], _language),
    "ntfy_url": Spec("", str, ["TVSIGN_NTFY_URL"], _ntfy),
    "ntfy_events": Spec("", str, ["TVSIGN_NTFY_EVENTS"], _events),
    "renew_before_hours": Spec(60.0, float, ["TVSIGN_RENEW_BEFORE_HOURS"], _hours),
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
        # float() accepte « 1_00 », « nan », « inf » : on exige un nombre décimal ordinaire
        if not re.fullmatch(r"\d+([.,]\d+)?", str(raw).strip()):
            raise ValueError("number_expected")
        value = float(str(raw).strip().replace(",", "."))
    else:
        value = str(raw if raw is not None else "").strip()
    if spec.check:
        fixed = spec.check(value)     # un validateur peut renvoyer la valeur normalisée
        if fixed is not None:
            value = fixed
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
            "value": mask_url(get(key)) if key == "ntfy_url" else get(key),
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
            errors[key] = "unknown_setting"
            continue
        if _env(spec):
            errors[key] = f"set_by_env:{_env(spec)[0]}"
            continue
        if key == "ntfy_url" and isinstance(raw, str) and raw.strip() and raw.strip() != get(key)                 and raw.strip() == mask_url(get(key)):
            continue        # valeur masquée renvoyée telle quelle par l'interface : on garde le secret
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


def ntfy_events() -> list[str]:
    return events_of(get("ntfy_events"))


def anisette_url() -> str:
    return get("anisette_url")
