"""User settings: environment variable > value stored in the database > default.

A setting fixed by the environment is shown read-only in the UI ("set by TVSIGN_...").
The same table also holds internal values (admin password hash, onboarding flag...),
which are never exposed through /api/settings.
"""
import os
import re
import time
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

# Événements de notification qu'une destination peut recevoir (voir notifier.py).
NOTIFY_EVENTS = ("fail", "expiry", "recovered", "update", "account")


class Spec:
    def __init__(self, default, kind, envs, check=None):
        self.default, self.kind, self.envs, self.check = default, kind, envs, check


# Les erreurs de validation sont des CODES stables (traduits par l'interface, clés err_*), pas du texte :
# « url_expected », « range:12:150 », « number_expected », « language_expected », « hour_expected »…
def valid_url(value: str) -> str:
    """Adresse http(s) exploitable : hôte présent, ni espace ni caractère de contrôle, port valide."""
    value = value.strip()
    if not value or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise ValueError("url_expected")
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port  # noqa: B018 - lève ValueError si le port est invalide
    except ValueError:
        raise ValueError("url_expected") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url_expected")
    return value


def _url(value: str) -> None:
    if value:
        valid_url(value)


def split_urls(value: str) -> list[str]:
    return [u for u in re.split(r"[\s,;]+", value or "") if u]


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


def _urls(value: str) -> None:
    """Une ou plusieurs adresses http(s):// (séparées par des espaces, virgules ou retours à la ligne),
    dont aucune ne désigne un serveur anisette Android (code « anisette_android:<hôte> »)."""
    for one in split_urls(value):
        _url(one)
        if is_legacy_anisette(one):
            raise ValueError(f"anisette_android:{anisette_host(one)}")


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


_USERINFO_PASSWORD = re.compile(r"^(\w+://[^/@:\s]*:)([^/@\s]*)@")


def unmask_url(posted: str, existing: str) -> str:
    """Adresse saisie dont le mot de passe vaut « *** » (valeur masquée renvoyée par l'interface,
    peut-être avec un autre hôte ou sujet) : reprend le mot de passe de l'adresse enregistrée."""
    new = _USERINFO_PASSWORD.match(posted or "")
    old = _USERINFO_PASSWORD.match(existing or "")
    if new and old and new.group(2) == "***" and old.group(2) != "***":
        return new.group(1) + old.group(2) + "@" + posted[new.end():]
    return posted


def _hours(value: float) -> None:
    # profil gratuit = 7 jours (168 h) ; garder au moins 12 h de marge et un renouvellement possible
    if not 12 <= value <= 150:
        raise ValueError("range:12:150")


def _onoff(value: str) -> str:
    if value.lower() not in ("on", "off", "1", "0", "true", "false", "yes", "no"):
        raise ValueError("onoff_expected")
    return "off" if value.lower() in ("off", "0", "false", "no") else "on"    # l'interface ne connaît que on / off


def _language(value: str) -> None:
    if value not in ("auto", "en", "fr"):
        raise ValueError("language_expected")


def _hour(value: str) -> None:
    if value and not (value.isdigit() and 0 <= int(value) <= 23):
        raise ValueError("hour_expected")


def _cert_revoke(value: str) -> None:
    if value not in ("never", "own", "any"):
        raise ValueError("cert_revoke_expected")


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
    # iPhone / iPad : souvent hors du réseau ou en veille, on commence plus tôt (jamais moins que le réglage ci-dessus)
    "renew_before_hours_mobile": Spec(96.0, float, ["TVSIGN_RENEW_BEFORE_HOURS_MOBILE"], _hours),
    # vide = serveur par défaut de plumesign (DEFAULT_ANISETTE, repli FALLBACK_ANISETTE). Plusieurs adresses :
    # la première d'abord, la suivante prend le relais si le serveur anisette tombe (voir plume._plume).
    "anisette_url": Spec("", str, ["TVSIGN_ANISETTE_URL", "PLUMESIGN_ANISETTE_URL", "ANISETTE_URL_V3"], _urls),
    # Compte Apple plein (2 certificats de développement) : quels certificats plumesign peut révoquer.
    # own = seulement ceux de cette installation (et les morts) ; never = aucun ; any = tous (Xcode,
    # SideStore... compris : leurs apps cesseraient de se lancer).
    "cert_revoke": Spec("own", str, ["TVSIGN_CERT_REVOKE", "PLUMESIGN_REVOKE"], _cert_revoke),
    # Plage horaire (heure locale) des renouvellements NON urgents ; vide = à toute heure.
    # Ignorée quand il reste moins de 24 h, et pour un renouvellement lancé à la main.
    "renew_window_start": Spec("", str, ["TVSIGN_RENEW_WINDOW_START"], _hour),
    "renew_window_end": Spec("", str, ["TVSIGN_RENEW_WINDOW_END"], _hour),
    # recherche quotidienne d'une nouvelle version de tvsign (GitHub) ; off : aucune requête
    "update_check": Spec("on", str, ["TVSIGN_UPDATE_CHECK"], _onoff),
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
        if key == "ntfy_url" and isinstance(raw, str) and raw.strip():
            if raw.strip() != get(key) and raw.strip() == mask_url(get(key)):
                continue        # valeur masquée renvoyée telle quelle par l'interface : on garde le secret
            raw = unmask_url(raw.strip(), get(key))     # hôte ou sujet changé, mot de passe masqué conservé
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


def migrate() -> None:
    """Au démarrage : un serveur anisette Android enregistré (réglage ou variable d'environnement) ne
    marche plus, on le remplace par le serveur par défaut et on le dit dans le journal."""
    stored = raw_get("anisette_url")
    legacy = [u for u in split_urls(stored) if is_legacy_anisette(u)]
    if legacy:
        kept = [u for u in split_urls(stored) if not is_legacy_anisette(u)]
        raw_set("anisette_url", " ".join(kept) if kept else None)
        db.log(f"Serveur anisette {' '.join(legacy)} retiré, "
               + (f"{' '.join(kept)} conservé" if kept else f"remplacé par le serveur par défaut ({DEFAULT_ANISETTE})")
               + " : Apple refuse les données des serveurs anisette Android depuis le 12 octobre 2026 "
               "(erreur -80045)", "warn")
    for name in PUBLIC["anisette_url"].envs:
        value = os.environ.get(name, "").strip()
        if value and any(is_legacy_anisette(u) for u in split_urls(value)):
            db.log(f"{name}={value} pointe vers un serveur anisette Android, refusé par Apple : ignoré, "
                   f"le serveur par défaut ({DEFAULT_ANISETTE}) est utilisé. Retire cette variable.", "warn")


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


def ntfy_events() -> list[str]:
    return events_of(get("ntfy_events"))


def anisette_url() -> str:
    """Première adresse anisette choisie par l'utilisateur (vide : serveur par défaut avec son repli).
    Une adresse de serveur Android n'est jamais renvoyée (valeur invalide : défaut)."""
    urls = anisette_urls()
    return urls[0] if urls else ""


def anisette_urls() -> list[str]:
    """Adresses choisies par l'utilisateur, dans l'ordre (vide : serveur par défaut)."""
    return split_urls(get("anisette_url"))


def same_server(a: str, b: str) -> bool:
    """Même adresse de serveur, à la casse et à la barre finale près (comme plumesign)."""
    return a.strip().rstrip("/").lower() == b.strip().rstrip("/").lower()


def anisette_servers() -> list[str]:
    """Serveurs que le renouvellement essaiera, dans l'ordre. Comme plumesign : le réglage vide, ou l'adresse
    par défaut saisie telle quelle, donne le serveur par défaut et son repli ; tout autre serveur est utilisé
    tel quel, les suivants de la liste prenant le relais s'il tombe."""
    out: list[str] = []
    for url in anisette_urls() or [DEFAULT_ANISETTE]:
        for one in ([DEFAULT_ANISETTE, FALLBACK_ANISETTE] if same_server(url, DEFAULT_ANISETTE) else [url]):
            if not any(same_server(one, seen) for seen in out):
                out.append(one)
    return out


def cert_revoke() -> str:
    return get("cert_revoke")


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


def update_check() -> bool:
    return str(get("update_check")).lower() not in ("off", "0", "false", "no")


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
