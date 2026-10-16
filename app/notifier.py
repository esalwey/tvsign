"""Notifications : destinations (ntfy, webhook JSON, Discord, Telegram, Gotify), événements choisis
par destination, messages français / anglais.

Les destinations sont :
  - l'adresse ntfy historique (réglage « ntfy_url », variable TVSIGN_NTFY_URL), toujours présente
    sous l'identifiant « ntfy » tant qu'elle est renseignée ;
  - la liste JSON du réglage interne « notify_targets » (jamais renvoyée en clair par l'API : les
    secrets sont masqués, et la valeur masquée renvoyée telle quelle conserve le secret).

Un envoi ne suit jamais les redirections (l'en-tête Authorization n'est pas transmis à un autre
hôte) et ne met jamais un secret dans un message d'erreur ni dans le journal.
"""
import asyncio
import base64
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

from . import db, reasons, settings
from .config import VERSION
from .settings import NOTIFY_EVENTS, mask_url, ntfy_address, unmask_url, valid_url

MASK = "********"
TARGETS_KEY = "notify_targets"
MAX_TARGETS = 10
PRIORITIES = ("default", "high", "urgent")

# type -> champs de la destination ; ceux de « secret » sont masqués dans l'API
PROVIDERS: dict[str, dict] = {
    "ntfy": {"fields": ("url", "token"), "secret": ("token",)},
    "webhook": {"fields": ("url",), "secret": ("url",)},
    "discord": {"fields": ("url",), "secret": ("url",)},
    "telegram": {"fields": ("token", "chat_id"), "secret": ("token",)},
    "gotify": {"fields": ("url", "token"), "secret": ("token",)},
}

# Dernier échec d'envoi par destination (en mémoire) : {id: texte}
_ERRORS: dict[str, str] = {}


# ---------- langue et messages ----------

def lang() -> str:
    """Langue des notifications : réglage explicite, sinon celle de l'interface vue en dernier,
    sinon celle du système, sinon l'anglais."""
    chosen = settings.get("language")
    if chosen in ("fr", "en"):
        return chosen
    seen = settings.raw_get("ui_lang")
    if seen in ("fr", "en"):
        return seen
    system = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
    return "fr" if system.startswith("fr") else "en"


MESSAGES: dict[str, tuple[str, str]] = {
    "test": ("tvsign : notification de test", "tvsign: test notification"),
    "renew_failed": ("{name} : renouvellement échoué, {reason}", "{name}: renewal failed, {reason}"),
    "expired": ("{name} : profil EXPIRÉ, l'app ne se lance plus. {reason}",
                "{name}: profile EXPIRED, the app no longer launches. {reason}"),
    "expiring": ("{name} expire dans moins de {h} h et le renouvellement échoue : {reason}",
                 "{name} expires in less than {h} h and renewal is failing: {reason}"),
    "renewed": ("{name} : renouvelée, valable jusqu'au {until}", "{name}: renewed, valid until {until}"),
    "update_refused": ("{name} : nouvelle version {tag} inutilisable ({why}), version précédente conservée",
                       "{name}: new version {tag} is unusable ({why}), previous version kept"),
    "update_installed": ("{name} : version {version} installée{notes}", "{name}: version {version} installed{notes}"),
    "update_rolled_back": ("{name} : la version {tag} n'a pas pu être installée ({why}), retour à la version {prev}",
                           "{name}: version {tag} could not be installed ({why}), back to version {prev}"),
    "update_blocked_os": ("{name} : la version {tag} demande {system} {need} ou plus, {who} est en {system} {have} : "
                          "version actuelle conservée",
                          "{name}: version {tag} needs {system} {need} or later, the device runs {system} {have}: "
                          "current version kept"),
    "update_failed": ("{name} : la mise à jour n'a pas pu être installée ({why}), la version actuelle reste valable",
                      "{name}: the update could not be installed ({why}), the current version stays valid"),
    "update_available": ("{name} : version {tag} disponible (mises à jour automatiques désactivées)",
                         "{name}: version {tag} is available (automatic updates are off)"),
    "cert_revoked": ("un ancien certificat de {email} a été révoqué pour signer {name} : les autres apps "
                     "sideloadées avec ce compte (iPhone…) vont cesser de fonctionner",
                     "an old certificate of {email} was revoked to sign {name}: other apps sideloaded with "
                     "this account (iPhone…) will stop working"),
    "cert_revoke_loop": ("le certificat de {email} a été révoqué {times} fois ou plus en 7 jours : un autre outil "
                         "(AltStore, SideStore, Sideloadly…) utilise le même Apple ID et les deux se révoquent en "
                         "boucle. Utilise un Apple ID dédié à tvsign",
                         "the certificate of {email} was revoked {times} times or more in 7 days: another tool "
                         "(AltStore, SideStore, Sideloadly…) uses the same Apple ID and the two keep revoking each "
                         "other. Use an Apple ID dedicated to tvsign"),
    "cert_revoked_other": ("{name} : certificat Apple révoqué (autre outil sur le même compte), "
                           "re-signature en cours",
                           "{name}: Apple certificate revoked (another tool on the same account), re-signing"),
    "account_lost": ("Compte Apple {email} déconnecté : reconnecte-le dans tvsign. {reason}",
                     "Apple account {email} signed out: reconnect it in tvsign. {reason}"),
    # certificat importé (.p12 + profil) : pas de renouvellement tous les 7 jours, on prévient avant la fin
    "import_expiring": ("Le certificat importé « {name} » expire dans moins de {d} jour(s) (le {until}) : importe-en un "
                        "nouveau pour {apps}, sinon ces apps cesseront de se lancer",
                        "The imported certificate “{name}” expires in less than {d} day(s) (on {until}): import a new "
                        "one for {apps}, or these apps will stop launching"),
    "import_expiring_failing": ("{name} : le certificat importé expire dans moins de {d} jour(s) et l'installation "
                                "échoue : {reason}",
                                "{name}: the imported certificate expires in less than {d} day(s) and the install is "
                                "failing: {reason}"),
    "import_expired": ("Le certificat importé « {name} » a expiré : {apps} ne se lance plus. Importe un nouveau certificat "
                       "et son profil (réglages de l'app › Avancé)",
                       "The imported certificate “{name}” has expired: {apps} no longer launches. Import a new certificate "
                       "and profile (app settings › Advanced)"),
    "import_revoked": ("Le certificat importé « {name} » a été révoqué : {apps} ne peut plus être installée ni lancée. "
                       "Importe un nouveau certificat et son profil, ou reviens à l'Apple ID",
                       "The imported certificate “{name}” was revoked: {apps} can no longer be installed or launched. "
                       "Import a new certificate and profile, or go back to the Apple ID"),
    "loop_stalled": ("tvsign : la boucle de renouvellement ne répond plus, redémarre le service",
                     "tvsign: the renewal loop is not responding, restart the service"),
}


def text(key: str, language: str | None = None, **params) -> str:
    fr, en = MESSAGES[key]
    return (fr if (language or lang()) == "fr" else en).format(**params)


def reason(why, language: str | None = None) -> str:
    """Cause d'un échec (message d'erreur du serveur, écrit en français) dans la langue de la notification : en
    anglais, la cause reconnue est dite en anglais (mêmes causes que l'interface, voir reasons.py) ; une cause
    inconnue reste telle quelle plutôt que d'être devinée."""
    why = str(why)
    if (language or lang()) == "fr":
        return why
    return reasons.english(why) or why


def fmt_time(ts: float) -> str:
    """Date courte et sans ambiguïté : « 22/10 14:05 (UTC+2) », avec le fuseau du serveur (souvent UTC
    dans Docker) ; les minutes du décalage n'apparaissent que si elles existent (« UTC+5:30 »)."""
    dt = datetime.fromtimestamp(ts).astimezone()
    off = dt.strftime("%z") or "+0000"
    hours, minutes = int(off[1:3]), off[3:5]
    zone = f"UTC{off[0]}{hours}" + (f":{minutes}" if minutes != "00" else "")
    if off in ("+0000", "-0000"):
        zone = "UTC"
    return f"{dt:%d/%m %H:%M} ({zone})"


# ---------- destinations ----------

def _stored() -> list[dict]:
    try:
        data = json.loads(settings.raw_get(TARGETS_KEY) or "[]")
    except json.JSONDecodeError:
        return []
    return [t for t in data if isinstance(t, dict) and t.get("type") in PROVIDERS and t.get("id")] \
        if isinstance(data, list) else []


def _save(items: list[dict]) -> None:
    settings.raw_set(TARGETS_KEY, json.dumps(items) if items else None)


def legacy_target() -> dict | None:
    url = settings.ntfy_url()
    if not url:
        return None
    return {"id": "ntfy", "type": "ntfy", "name": "", "url": url, "token": "",
            "events": settings.ntfy_events(), "builtin": True}


def targets() -> list[dict]:
    """Toutes les destinations, secrets en clair (usage interne)."""
    first = legacy_target()
    return ([first] if first else []) + _stored()


def configured() -> bool:
    return any(t["events"] for t in targets())


def _events(raw, default):
    if raw is None:
        return list(default)
    if not isinstance(raw, (list, tuple)) or any(e not in NOTIFY_EVENTS for e in raw):
        raise ValueError("events_invalid")
    return [e for e in NOTIFY_EVENTS if e in raw]


def clean(raw: dict, existing: dict | None = None) -> dict:
    """Valide une destination saisie ; ValueError(code) sinon. Un secret égal à MASK reprend celui
    de la destination existante."""
    kind = raw.get("type")
    if not isinstance(kind, str) or kind not in PROVIDERS:        # une liste ou un objet JSON n'est pas hachable
        raise ValueError("notify_type_invalid")
    same = existing if existing and existing.get("type") == kind else None
    out = {"id": (existing or {}).get("id") or uuid.uuid4().hex[:8], "type": kind,
           "name": str(raw.get("name") or "").strip()[:40],
           "events": _events(raw.get("events"), (existing or {}).get("events") or NOTIFY_EVENTS)}
    spec = PROVIDERS[kind]
    for field in spec["fields"]:
        value = str(raw.get(field) or "").strip()
        if value == MASK:
            value = (same or {}).get(field, "")
        elif kind == "ntfy" and field == "url" and same:
            # mot de passe masqué (« *** ») renvoyé par l'interface : on reprend celui de l'adresse
            # enregistrée, même si l'hôte ou le sujet ont changé entre-temps
            value = unmask_url(value, same.get("url", ""))
        out[field] = value
    if kind == "ntfy":
        out["url"] = ntfy_address(out["url"])
        if not out["url"]:
            raise ValueError("url_expected")
        if re.search(r"[\s\x00-\x1f]", out["token"]):
            raise ValueError("token_invalid")
    elif kind == "webhook":
        out["url"] = valid_url(out["url"])
    elif kind == "discord":
        parts = urllib.parse.urlsplit(valid_url(out["url"]))
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not re.fullmatch(r"([\w-]+\.)?discord(app)?\.com", host) \
                or not parts.path.startswith("/api/webhooks/"):
            raise ValueError("discord_url_invalid")
    elif kind == "telegram":
        if not re.fullmatch(r"\d{3,}:[\w-]{20,}", out["token"]):
            raise ValueError("telegram_token_invalid")
        if not re.fullmatch(r"-?\d{3,}|@\w{4,}", out["chat_id"]):
            raise ValueError("telegram_chat_invalid")
    elif kind == "gotify":
        out["url"] = valid_url(out["url"])
        if not re.fullmatch(r"[\w.-]{4,}", out["token"]):
            raise ValueError("gotify_token_invalid")
    return out


def public(t: dict) -> dict:
    """Destination sans secret, pour l'API. Les champs secrets non vides valent MASK."""
    spec = PROVIDERS[t["type"]]
    out = {"id": t["id"], "type": t["type"], "name": t.get("name", ""), "events": t["events"],
           "builtin": bool(t.get("builtin")), "last_error": _ERRORS.get(t["id"])}
    for field in spec["fields"]:
        value = t.get(field, "")
        if field in spec["secret"]:
            out[field] = MASK if value else ""
        elif field == "url" and t["type"] == "ntfy":
            out[field] = mask_url(value)
        else:
            out[field] = value
    out["summary"] = summary(t)
    return out


def summary(t: dict) -> str:
    """Libellé court sans secret : l'hôte, ou l'identifiant de conversation."""
    if t["type"] == "telegram":
        return t.get("chat_id", "")
    try:
        return urllib.parse.urlsplit(t.get("url", "")).hostname or ""
    except ValueError:
        return ""


def listing() -> dict:
    items = [public(t) for t in targets()]
    for i in items:
        if i["id"] == "ntfy":
            i["locked"] = bool(settings.describe()["ntfy_url"]["locked"])
    return {"targets": items, "events": list(NOTIFY_EVENTS), "types": list(PROVIDERS),
            "max": MAX_TARGETS, "language": lang()}


def get_target(target_id: str) -> dict | None:
    return next((t for t in targets() if t["id"] == target_id), None)


def _check_builtin(raw: dict) -> None:
    """La destination ntfy venue de l'adresse (réglage ntfy_url) n'a ni nom ni jeton : les accepter
    puis les oublier ferait croire à une configuration qui n'existe pas."""
    if str(raw.get("name") or "").strip() or str(raw.get("token") or "").strip():
        raise ValueError("builtin_no_token")


def save_target(raw: dict, target_id: str | None = None) -> dict:
    """Crée (target_id None) ou remplace une destination ; renvoie sa forme publique."""
    if target_id == "ntfy":
        _check_builtin(raw)
        old = legacy_target() or {"id": "ntfy", "type": "ntfy", "events": list(NOTIFY_EVENTS)}
        new = clean({**raw, "type": "ntfy", "token": ""}, old)
        events = new["events"]
        wanted = {"ntfy_url": new["url"],
                  "ntfy_events": "" if len(events) == len(NOTIFY_EVENTS) else (",".join(events) or "none")}
        shown = settings.describe()
        for key in list(wanted):
            if shown[key]["locked"]:
                # fixé par l'environnement : l'adresse ne peut pas changer d'ici, mais les
                # événements (s'ils ne sont pas fixés eux aussi) restent modifiables
                current = legacy_target()
                if current and (new["url"] != current["url"] if key == "ntfy_url" else events != current["events"]):
                    raise ValueError(f"set_by_env:{shown[key]['env']}")
                del wanted[key]
        try:
            settings.update(wanted)
        except ValueError as e:
            problems = e.args[0] if e.args and isinstance(e.args[0], dict) else {}
            raise ValueError(next(iter(problems.values()), "url_expected")) from None
        return public(legacy_target() or {**new, "builtin": True})
    items = _stored()
    existing = next((t for t in items if t["id"] == target_id), None) if target_id else None
    if target_id and not existing:
        raise LookupError(target_id)
    new = clean(raw, existing)
    if existing:
        items = [new if t["id"] == target_id else t for t in items]
    else:
        if len(items) + (1 if legacy_target() else 0) >= MAX_TARGETS:
            raise ValueError("too_many_targets")
        items.append(new)
    _save(items)
    return public(new)


def delete_target(target_id: str) -> None:
    if target_id == "ntfy":
        settings.update({"ntfy_url": None, "ntfy_events": None})
        return
    items = _stored()
    if not any(t["id"] == target_id for t in items):
        raise LookupError(target_id)
    _save([t for t in items if t["id"] != target_id])
    _ERRORS.pop(target_id, None)


# ---------- construction et envoi ----------

def _netloc(parts: urllib.parse.SplitResult) -> str:
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"          # littéral IPv6 : les crochets sont nécessaires dans l'adresse
    return host + (f":{parts.port}" if parts.port else "")


def build(t: dict, event: str, message: str, priority: str = "default", extra: dict | None = None):
    """(adresse, corps, en-têtes) de la requête POST pour cette destination."""
    kind = t["type"]
    extra = extra or {}
    if priority not in PRIORITIES:
        priority = "default"
    if kind == "ntfy":
        headers = {"Title": "tvsign", "Priority": priority}
        parts = urllib.parse.urlsplit(t["url"])
        url = t["url"]
        if t.get("token"):
            headers["Authorization"] = "Bearer " + t["token"]
        if parts.username is not None:
            if "Authorization" not in headers:
                # https://user:pass@hôte/sujet : urllib n'en fait pas d'authentification, on l'envoie en Basic
                cred = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
                headers["Authorization"] = "Basic " + base64.b64encode(cred.encode("utf-8")).decode()
            url = urllib.parse.urlunsplit(parts._replace(netloc=_netloc(parts)))
        return url, message.encode("utf-8"), headers
    json_headers = {"Content-Type": "application/json", "User-Agent": f"tvsign/{VERSION}"}
    if kind == "webhook":
        body = {"title": "tvsign", "message": message, "event": event, "priority": priority,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"), "version": VERSION, **extra}
        return t["url"], json.dumps(body).encode("utf-8"), json_headers
    if kind == "discord":
        body = {"username": "tvsign", "content": message[:1900], "allowed_mentions": {"parse": []}}
        return t["url"], json.dumps(body).encode("utf-8"), json_headers
    if kind == "telegram":
        body = {"chat_id": t["chat_id"], "text": message[:4000], "disable_web_page_preview": True}
        return (f"https://api.telegram.org/bot{t['token']}/sendMessage",
                json.dumps(body).encode("utf-8"), json_headers)
    if kind == "gotify":
        body = {"title": "tvsign", "message": message, "priority": {"default": 5, "high": 8, "urgent": 10}[priority]}
        return (t["url"].rstrip("/") + "/message", json.dumps(body).encode("utf-8"),
                {**json_headers, "X-Gotify-Key": t["token"]})
    raise ValueError("notify_type_invalid")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None             # une redirection est une erreur : jamais d'identifiants vers un autre hôte


_OPENER = urllib.request.build_opener(_NoRedirect)


def _secrets(t: dict) -> list[str]:
    values = [t.get(f, "") for f in PROVIDERS[t["type"]]["secret"]]
    try:
        parts = urllib.parse.urlsplit(t.get("url", ""))
        values += [parts.password or "", urllib.parse.unquote(parts.password or "")]
        if t["type"] in ("webhook", "discord"):
            values += [p for p in parts.path.split("/") if len(p) > 12]
    except ValueError:
        pass
    return [v for v in values if v and len(v) >= 4]


def error_code(e: Exception) -> str:
    """Cause d'un échec d'envoi sous forme de code stable (l'interface dit quoi vérifier) :
    http_401, http_403, http_404, http_429, http_other, redirect, timeout, refused, dns, tls, other."""
    if isinstance(e, urllib.error.HTTPError):
        if 300 <= e.code < 400:
            return "redirect"
        return f"http_{e.code}" if e.code in (401, 403, 404, 429) else "http_other"
    cause = e.reason if isinstance(e, urllib.error.URLError) else e
    if isinstance(cause, (TimeoutError, socket.timeout)) or "timed out" in str(cause).lower():
        return "timeout"
    if isinstance(cause, ssl.SSLError) or "CERTIFICATE_VERIFY" in str(cause) or "SSL" in str(cause):
        return "tls"
    if isinstance(cause, ConnectionRefusedError):
        return "refused"
    if isinstance(cause, socket.gaierror):
        return "dns"
    return "other"


def describe_error(e: Exception, t: dict | None = None) -> str:
    """Texte d'erreur court, sans secret, sans adresse complète."""
    if isinstance(e, urllib.error.HTTPError):
        msg = f"HTTP {e.code}" + (" (redirection refusée)" if 300 <= e.code < 400 else "")
    elif isinstance(e, urllib.error.URLError):
        msg = str(e.reason)
    else:
        msg = str(e) or type(e).__name__
    for secret in _secrets(t) if t else []:
        msg = msg.replace(secret, "***")
    return re.sub(r"\s+", " ", msg)[:200]


def send(t: dict, event: str, message: str, priority: str = "default", extra: dict | None = None) -> None:
    """Envoi synchrone (appelé dans un thread) ; lève l'erreur."""
    url, data, headers = build(t, event, message, priority, extra)
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    _OPENER.open(req, timeout=10).close()


async def dispatch(event: str, message: str, priority: str = "default", **extra) -> bool:
    """Envoie aux destinations abonnées à l'événement. True si au moins une a reçu le message ;
    un échec est mémorisé et journalisé une fois, sans empêcher les autres destinations."""
    sent = False
    for t in targets():
        if event not in t["events"]:
            continue
        label = t.get("name") or t["type"]
        try:
            await asyncio.to_thread(send, t, event, message, priority, extra)
            _ERRORS.pop(t["id"], None)
            sent = True
        except Exception as e:  # noqa: BLE001 - la notification est accessoire
            err = describe_error(e, t)
            if t["id"] not in _ERRORS:
                try:
                    db.log(f"notification {label} échouée : {err}", "warn")
                except Exception:  # noqa: BLE001 - base indisponible : ne pas masquer l'erreur d'origine
                    pass
            _ERRORS[t["id"]] = err
    return sent


def last_error() -> str | None:
    for t in targets():
        if t["id"] in _ERRORS:
            return f"{t.get('name') or t['type']} : {_ERRORS[t['id']]}"
    return None


async def test(draft: dict) -> None:
    """Message d'essai vers une destination saisie (pas forcément enregistrée) ; lève ErrorText."""
    existing = get_target(str(draft.get("id"))) if draft.get("id") else None
    if existing and existing.get("builtin"):
        _check_builtin(draft)
    t = clean(draft, existing)
    try:
        await asyncio.to_thread(send, t, "test", text("test"), "default", {"app": None})
    except Exception as e:  # noqa: BLE001
        err = describe_error(e, t)
        if existing:
            _ERRORS[existing["id"]] = err
        raise NotifyFailed(err, error_code(e)) from None
    if existing:
        _ERRORS.pop(existing["id"], None)


class NotifyFailed(RuntimeError):
    """Échec d'envoi, message déjà nettoyé de tout secret. `code` : cause (voir error_code)."""

    def __init__(self, message: str, code: str = "other"):
        super().__init__(message)
        self.code = code

    @property
    def api_detail(self) -> str:
        """Détail renvoyé par l'API : « notify_<cause>:<texte> » (clés err_notify_* de l'interface),
        ou « notify_failed:<texte> » quand la cause est inconnue."""
        return f"notify_{self.code}:{self}" if self.code != "other" else f"notify_failed:{self}"
