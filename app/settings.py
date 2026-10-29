"""User settings: environment variable > value stored in the database > default.

A setting fixed by the environment is shown read-only in the UI ("set by TVSIGN_...").
The same table also holds internal values (admin password hash, onboarding flag...),
which are never exposed through /api/settings.
"""
import os
import re
import urllib.parse

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

# Serveurs anisette de 1.0.0 (Android) : si l'un d'eux est encore enregistré dans les réglages après une mise à
# jour, il est effacé une fois (migrate). Ce n'est pas une règle d'exécution : aucun serveur n'est jamais refusé
# d'après son nom, et le même nom saisi après la migration est gardé.
STALE_ANISETTE_HOSTS = frozenset({
    "ani.sidestore.app", "ani.stikstore.app", "anisette.crystall1ne.dev",
    "ani3server.fly.dev", "ani.jaydenha.uk", "omni.ethxn.xyz",
})
MIGRATED_KEY = "anisette_migrated"


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


def _server_list(value: str) -> None:
    """Adresses de serveurs séparées par des virgules, des points-virgules ou des espaces (ou none / off)."""
    if len(value) > 2000:
        raise ValueError("2000 caractères au plus")
    for token in _split(value):
        if token.lower() in ("none", "off"):
            continue
        if "://" in token and not re.match(r"^https?://[^\s/]+", token, re.I):
            raise ValueError(f"adresse http(s):// attendue : {token}")


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
    # Avancé : la liste des serveurs à la place de celle par défaut (derrière le serveur choisi, s'il y en a un),
    # séparés par des virgules ou des espaces, dans l'ordre. C'est ce que lit plumesign dans
    # PLUMESIGN_ANISETTE_FALLBACK_URL, qui reste donc une source de ce réglage : changer la liste ne demande ni
    # nouvelle version ni recompilation (le jour où l'un des serveurs par défaut disparaît).
    "anisette_servers": Spec("", str, ["TVSIGN_ANISETTE_SERVERS", "PLUMESIGN_ANISETTE_FALLBACK_URL"], _server_list),

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


def _split(value: str) -> list[str]:
    return [t for t in re.split(r"[,;\s]+", value or "") if t]


def normalize_server(url: str) -> str:
    """Adresse d'un serveur comme plumesign l'emploie : sans espaces ni barre finale, https:// sans schéma."""
    url = url.strip().rstrip("/")
    return url if "://" in url else f"https://{url}"


def same_server(a: str, b: str) -> bool:
    """Même serveur, à la casse, à la barre finale et au schéma près s'il manque (comme plumesign)."""
    return normalize_server(a).lower() == normalize_server(b).lower()


def _unique(servers: list[str]) -> list[str]:
    out: list[str] = []
    for server in servers:
        if not any(same_server(known, server) for known in out):
            out.append(server)
    return out


def resolve_servers(requested: str, fallback_override: str | None) -> list[str]:
    """Les serveurs que plumesign essaie, dans l'ordre : le même calcul que omnisette::resolve_servers.
    Rien de choisi ni de liste : les serveurs par défaut ; un serveur choisi : en premier, puis la liste s'il y
    en a une, sinon les serveurs par défaut ; une liste sans serveur choisi : la liste est toute la liste ;
    « none » / « off » : le premier seul."""
    first = normalize_server(requested) if requested.strip() else None
    override = (fallback_override or "").strip()
    listed = [normalize_server(t) for t in _split(override) if t.lower() not in ("none", "off")]
    if override.lower() in ("none", "off"):
        servers = [first or DEFAULT_ANISETTE]
    elif listed:
        servers = ([first] if first else []) + listed
    else:
        servers = [first or DEFAULT_ANISETTE, *DEFAULT_ANISETTE_SERVERS]
    return _unique(servers)


def anisette_servers_setting() -> str:
    return get("anisette_servers")


def fallback_override() -> str | None:
    """Ce que plumesign reçoit dans PLUMESIGN_ANISETTE_FALLBACK_URL : « none » quand l'utilisateur a désactivé le
    repli (le serveur choisi, ou à défaut le premier des serveurs par défaut, seul), sinon la liste des réglages
    (qui lit aussi cette variable), sinon rien."""
    listed = [t for t in _split(anisette_servers_setting()) if t.lower() not in ("none", "off")]
    if not anisette_fallback():
        # sans serveur choisi, c'est le premier de la liste qui reste seul (elle remplace celle par défaut)
        return "none" if anisette_url() or not listed else listed[0]
    return anisette_servers_setting().strip() or None


def anisette_servers() -> list[str]:
    """Serveurs que plumesign essaiera, dans l'ordre, calculés comme lui (resolve_servers) à partir des mêmes
    sources : le serveur choisi, la liste et le repli des réglages ou de l'environnement. Le dernier serveur qui
    a marché passe en tête chez plumesign, ce que cette liste ne dit pas (voir plume.last_server)."""
    return resolve_servers(anisette_url(), fallback_override())


def migrate() -> None:
    """Au démarrage, une seule fois après une mise à jour (nettoyage de données, pas une règle d'exécution) :
    - un serveur anisette de 1.0.0 (Android) encore enregistré est effacé : il passerait en tête de la liste ;
    - un serveur choisi avant la mise à jour était utilisé seul (1.0.1) : le repli sur les serveurs publics est
      désactivé pour lui, pour que le trafic anisette d'un Apple ID ne parte pas ailleurs sans que
      l'utilisateur l'ait voulu. Il le réactive dans les Réglages."""
    if raw_get(MIGRATED_KEY) is not None:
        return
    stored = raw_get("anisette_url")
    if stored and _host(stored) in STALE_ANISETTE_HOSTS:
        raw_set("anisette_url", None)
        db.log(f"Serveur anisette {stored} effacé des réglages : serveur de 1.0.0 qui n'est plus utilisé, les "
               "serveurs par défaut sont essayés à la place", "warn")
    if anisette_url() and raw_get("anisette_fallback") is None:
        raw_set("anisette_fallback", "off")
        db.log(f"Serveur anisette {anisette_url()} : utilisé seul, comme avant la mise à jour (Réglages › « Si ce "
               "serveur échoue » pour essayer aussi les serveurs par défaut)", "warn")
    raw_set(MIGRATED_KEY, "1")


def _host(url: str) -> str:
    value = (url or "").strip()
    if "://" not in value:
        value = "//" + value
    try:
        return (urllib.parse.urlsplit(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
