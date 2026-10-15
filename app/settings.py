"""User settings: environment variable > value stored in the database > default.

A setting fixed by the environment is shown read-only in the UI ("set by TVSIGN_...").
The same table also holds internal values (admin password hash, onboarding flag...),
which are never exposed through /api/settings.
"""
import os
import re
import time

from . import db


class Spec:
    def __init__(self, default, kind, envs, check=None):
        self.default, self.kind, self.envs, self.check = default, kind, envs, check


# Les erreurs de validation sont des CODES stables (traduits par l'interface, clés err_*), pas du texte :
# « url_expected », « range:12:150 », « number_expected », « language_expected », « hour_expected »…
def _url(value: str) -> None:
    if value and not re.match(r"^https?://[^\s/]+", value):
        raise ValueError("url_expected")


def _urls(value: str) -> None:
    """Une ou plusieurs adresses http(s):// (séparées par des espaces, virgules ou retours à la ligne)."""
    for one in split_urls(value):
        _url(one)


def split_urls(value: str) -> list[str]:
    return [u for u in re.split(r"[\s,;]+", value or "") if u]


def _hours(value: float) -> None:
    # profil gratuit = 7 jours (168 h) ; garder au moins 12 h de marge et un renouvellement possible
    if not 12 <= value <= 150:
        raise ValueError("range:12:150")


def _language(value: str) -> None:
    if value not in ("auto", "en", "fr"):
        raise ValueError("language_expected")


def _hour(value: str) -> None:
    if value and not (value.isdigit() and 0 <= int(value) <= 23):
        raise ValueError("hour_expected")


PUBLIC: dict[str, Spec] = {
    "language": Spec("auto", str, ["TVSIGN_LANGUAGE"], _language),
    "ntfy_url": Spec("", str, ["TVSIGN_NTFY_URL"], _url),
    "renew_before_hours": Spec(60.0, float, ["TVSIGN_RENEW_BEFORE_HOURS"], _hours),
    # vide = serveur par défaut de plumesign (https://ani.sidestore.app). Plusieurs adresses : la première
    # d'abord, la suivante prend le relais si le serveur anisette tombe (voir plume._plume).
    "anisette_url": Spec("", str, ["TVSIGN_ANISETTE_URL", "PLUMESIGN_ANISETTE_URL", "ANISETTE_URL_V3"], _urls),
    # Plage horaire (heure locale) des renouvellements NON urgents ; vide = à toute heure.
    # Ignorée quand il reste moins de 24 h, et pour un renouvellement lancé à la main.
    "renew_window_start": Spec("", str, ["TVSIGN_RENEW_WINDOW_START"], _hour),
    "renew_window_end": Spec("", str, ["TVSIGN_RENEW_WINDOW_END"], _hour),
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
            raise ValueError("number_expected") from None
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
            errors[key] = "unknown_setting"
            continue
        if _env(spec):
            errors[key] = f"set_by_env:{_env(spec)[0]}"
            continue
        if raw is None or raw == "":
            todo[key] = None
            continue
        try:
            todo[key] = _convert(spec, raw)
        except ValueError as e:
            errors[key] = str(e)
    if not errors and ("renew_window_start" in values or "renew_window_end" in values):
        # une seule borne de la plage horaire serait ignorée sans le dire (renouvellements à toute heure)
        pair = {}
        for key in ("renew_window_start", "renew_window_end"):
            if key in todo:
                pair[key] = todo[key] or ""
            else:
                pair[key] = str(get(key) or "")
        if bool(pair["renew_window_start"]) != bool(pair["renew_window_end"]):
            errors["renew_window_end" if "renew_window_end" in values else "renew_window_start"] = "window_incomplete"
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
    """Première adresse anisette (vide : serveur par défaut de plumesign)."""
    urls = anisette_urls()
    return urls[0] if urls else ""


def anisette_urls() -> list[str]:
    return split_urls(get("anisette_url"))


def renew_window() -> tuple[int, int] | None:
    """(début, fin) en heures locales, ou None si aucune plage n'est définie."""
    start, end = get("renew_window_start"), get("renew_window_end")
    if start == "" or end == "":
        return None
    return int(start), int(end)


def in_renew_window(now: float, window: tuple[int, int] | None = None, tz_hour: int | None = None) -> bool:
    """Vrai si l'heure locale est dans la plage (qui peut passer minuit, ex. 23 -> 6). Début = fin :
    toute la journée."""
    window = window if window is not None else renew_window()
    if not window:
        return True
    start, end = window
    hour = tz_hour if tz_hour is not None else time.localtime(now).tm_hour
    if start == end:
        return True
    return start <= hour < end if start < end else hour >= start or hour < end


def next_window_start(ts: float, window: tuple[int, int] | None = None) -> float:
    """Premier instant >= ts où un renouvellement non urgent est permis : ts lui-même dans la plage, sinon
    le prochain début de plage (heure locale pleine)."""
    window = window if window is not None else renew_window()
    if in_renew_window(ts, window):
        return ts
    lt = time.localtime(ts)
    t = ts - lt.tm_min * 60 - lt.tm_sec        # début de l'heure locale en cours
    for _ in range(50):                        # au plus un jour et demi (changements d'heure compris)
        t += 3600
        if in_renew_window(t, window):
            return t
    return ts
