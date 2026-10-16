"""Enveloppe de plumesign : comptes Apple, enregistrement d'appareils, signature, découverte."""
import asyncio
import json
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from . import db, settings
from .config import IMPORT_PLUME, LOGIN_TIMEOUT, PLUME_HOME, PLUME_LOCK_TIMEOUT, PLUMESIGN, TMP_DIR
from .proc import Result, Session, register, run, spawn
from .util import ContentError, is_content_error, replace_retry

# accounts.json est partagé : une seule opération plumesign à la fois.
LOCK = asyncio.Lock()
STORE = PLUME_HOME / "PlumeImpactor"


def env(anisette: str | None = None) -> dict:
    e = os.environ.copy()
    # plumesign range ses données dans %APPDATA% (Windows) ou $XDG_CONFIG_HOME (Linux)
    e["APPDATA" if os.name == "nt" else "XDG_CONFIG_HOME"] = str(PLUME_HOME)
    e.setdefault("RUST_LOG", "info")
    e.pop("PLUMESIGN_PASSWORD", None)        # jamais hérité : passé seulement à « account login »
    for secret in ("TVSIGN_PASSWORD", "TVSIGN_LAUNCH_TOKEN", "TVSIGN_GITHUB_TOKEN"):
        e.pop(secret, None)                  # secrets de tvsign : inutiles à plumesign
    # Le serveur anisette vient des réglages seulement : jamais hérité tel quel de l'environnement (il
    # pourrait désigner un serveur Android). Vide = serveur par défaut de plumesign, avec son repli.
    for name in ("PLUMESIGN_ANISETTE_URL", "ANISETTE_URL_V3"):
        e.pop(name, None)
    if anisette is None:
        urls = _anisette_urls()
        anisette = urls[0] if urls else ""
    if anisette:
        e["PLUMESIGN_ANISETTE_URL"] = anisette
    # Compte plein : jamais de révocation de certificats étrangers sans l'accord de l'utilisateur.
    e["PLUMESIGN_REVOKE"] = settings.cert_revoke()
    # Les copies de travail de plumesign (plume_stage_*) vont dans notre dossier de données, pas dans
    # /tmp du conteneur ni %TEMP% : elles sont balayées par sweep_stage().
    e.update(_tmp_env())
    return e


STAGE_DIR = TMP_DIR / "plumesign"


def _tmp_env() -> dict:
    try:
        STAGE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {}
    path = str(STAGE_DIR)
    return {"TMP": path, "TEMP": path} if os.name == "nt" else {"TMPDIR": path}


def sweep_stage(max_age: float = 3600) -> int:
    """Supprime les copies de travail plume_stage_* laissées par un plumesign tué (délai, arrêt)."""
    removed = 0
    try:
        entries = list(STAGE_DIR.glob("plume_stage_*"))
    except OSError:
        return 0
    for d in entries:
        try:
            if d.is_dir() and time.time() - d.stat().st_mtime > max_age:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


# ---------- serveurs anisette : le dernier qui a répondu d'abord, les autres en secours ----------

_ANISETTE_OK: str | None = None
_ANISETTE_FAIL = re.compile(r"anisette", re.I)
# Refus d'Apple ou de l'utilisateur que changer de serveur anisette ne résout pas (et qui compteraient
# comme un essai de plus chez Apple) : pas de bascule.
_NO_FAILOVER = frozenset({"apple_refused", "apple_rate_limited", "sms_denied", "never_signed_in"})


def _anisette_urls() -> list[str]:
    """Adresses configurées, la dernière qui a fonctionné en tête. Vide : serveur par défaut de plumesign."""
    urls = settings.anisette_urls()
    if _ANISETTE_OK in urls:
        urls = [_ANISETTE_OK] + [u for u in urls if u != _ANISETTE_OK]
    return urls


async def _plume(*args, extra_env: dict | None = None, **kw) -> Result:
    """`plumesign …` avec bascule sur le serveur anisette suivant quand celui-ci est en panne
    (une seule passe sur la liste). Sans liste de plusieurs adresses : un simple run()."""
    global _ANISETTE_OK
    urls = _anisette_urls()
    res = None
    for i, url in enumerate(urls or [None]):
        e = env(url)
        if extra_env:
            e.update(extra_env)
        res = await run(PLUMESIGN, *args, env=e, **kw)
        if res.ok:
            if url and len(urls) > 1:
                _ANISETTE_OK = url
            return res
        if i + 1 >= len(urls):
            return res
        errors = _errors(res.output + res.err)
        if not _ANISETTE_FAIL.search(errors) or classify(errors) in _NO_FAILOVER:
            return res
        db.log(f"serveur anisette {url} en échec, essai avec {urls[i + 1]}", "warn")
    return res


class Busy(RuntimeError):
    """plumesign est tenu par une autre opération depuis trop longtemps (connexion Apple en attente ?)."""


@asynccontextmanager
async def _locked(state=None):
    """Verrou plumesign borné : un détenteur bloqué devient une erreur visible, pas un blocage muet.
    state("waiting") est appelé si le verrou est pris par une autre opération (connexion Apple, autre
    signature), state("acquired") une fois obtenu : l'interface n'affiche « signature » que pour de bon."""
    if state and LOCK.locked():
        state("waiting")
    try:
        await asyncio.wait_for(LOCK.acquire(), PLUME_LOCK_TIMEOUT)
    except TimeoutError:
        raise Busy("plumesign occupé depuis trop longtemps (connexion Apple en attente ?)") from None
    try:
        if state:
            state("acquired")
        yield
    finally:
        LOCK.release()


def import_existing_store() -> None:
    """Au premier lancement, reprend la session et les appairages plumesign déjà présents sur la
    machine (Plume Impactor, plumesign en ligne de commande). Copie locale seulement.
    Opt-in : TVSIGN_IMPORT_PLUME=1 (désactivé par défaut)."""
    if not IMPORT_PLUME:
        return
    if os.name == "nt":
        legacy = Path(os.environ.get("APPDATA", "")) / "PlumeImpactor"
    else:
        legacy = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "PlumeImpactor"
    if not legacy.is_dir() or legacy.resolve() == STORE.resolve():
        return
    if not (STORE / "accounts.json").exists() and (legacy / "accounts.json").exists():
        shutil.copytree(legacy, STORE, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("remotepairing", "*.bak"))
        db.log(f"Session Apple importée depuis {legacy}")
    pairings = STORE / "remotepairing"
    if (legacy / "remotepairing").is_dir() and not (pairings.is_dir() and any(pairings.iterdir())):
        shutil.copytree(legacy / "remotepairing", pairings, dirs_exist_ok=True)
        db.log(f"Appairages Apple TV importés depuis {legacy}")


def note_legacy_anisette() -> bool:
    """Au démarrage : une identité anisette de l'ancienne version (state.plist, Android) est inutile,
    Apple refuse ses données. plumesign la supprime et en crée une Windows au prochain contact avec
    Apple ; on le dit une fois dans le journal (et non à chaque démarrage tant que ce contact n'a pas eu lieu).
    True si l'ancienne était encore là."""
    if not (STORE / "state.plist").exists() or (STORE / ANISETTE_STATE).exists():
        return False
    if settings.raw_get(_LEGACY_NOTED) is None:
        db.log("Identité anisette Android abandonnée (Apple refuse ses données depuis le 12 octobre 2026) : "
               "une identité Windows sera créée au prochain contact avec Apple")
        settings.raw_set(_LEGACY_NOTED, "1")
    return True


_LEGACY_NOTED = "anisette_legacy_noted"     # l'ancienne identité Android a déjà été signalée au journal


# ---------- comptes ----------

# Appelées (e-mail) quand une session Apple redevient valide : reconnexion réussie ou vérification
# qui réussit après un échec. Enregistrées par renew (qui importe plume, pas l'inverse).
ON_VALID: list = []


def _valid(email: str) -> None:
    for hook in ON_VALID:
        try:
            hook(email)
        except Exception as e:  # noqa: BLE001 - accessoire
            db.log(f"reprise après reconnexion de {email} : {e}", "warn")


_LAST: dict = {}


def _store() -> dict:
    """Contenu de accounts.json, tolérant aux lectures pendant une réécriture par plumesign."""
    global _LAST
    path = STORE / "accounts.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # plumesign tronque puis réécrit le fichier : on garde la dernière version lue,
        # sinon la copie de secours faite après la dernière connexion réussie
        if _LAST:
            return _LAST
        try:
            return json.loads((STORE / "accounts.json.bak").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
    if isinstance(data, dict):
        _LAST = data
        return data
    return _LAST


def _backup_store() -> None:
    try:
        shutil.copyfile(STORE / "accounts.json", STORE / "accounts.json.bak")
    except OSError:
        pass


def accounts() -> list[dict]:
    data = _store()
    selected = data.get("selected_account")
    status = {r["email"]: r for r in db.rows("SELECT * FROM account_status")}
    out = []
    for email, acc in (data.get("accounts") or {}).items():
        st = status.get(email, {})
        out.append({
            "email": email,
            "name": acc.get("first_name"),
            "team_id": acc.get("team_id"),
            "cert_expires": settings.raw_get(_CERT_KEY + email),   # AAAA-MM-JJ, vu au dernier passage de plumesign
            "selected": email == selected,
            "ok": st.get("ok"),                 # 1 valide, 0 reconnexion nécessaire, None inconnu
            "message": st.get("message"),
            "checked_at": st.get("checked_at"),
        })
    return out


_CERT_KEY = "cert_expires:"
# Ligne de journal de plumesign : seul un certificat étranger révoqué casse d'autres apps sideloadées.
FOREIGN_REVOKED = "Revoked certificate (FOREIGN)"
_CERT_LINE = re.compile(r"^CERT\s+([0-9A-Fa-f]+)\s+(\d{4}-\d{2}-\d{2})\s*$", re.M)


def cert_expiry(output: str) -> str | None:
    """Fin de validité du certificat de signature, d'après la ligne « CERT <série> <date> » de plumesign."""
    found = _CERT_LINE.findall(output or "")
    return found[-1][1] if found else None


def _forget_cert_expiry(email: str) -> None:
    """Oublie la date de fin du certificat : elle appartient à l'ancienne session ou équipe."""
    try:
        settings.raw_set(_CERT_KEY + email, None)
    except Exception:  # noqa: BLE001 - l'affichage d'une date ne doit jamais bloquer
        pass


def pick_default(accs: list[dict]) -> str | None:
    """Compte choisi par défaut, sinon le premier par ordre alphabétique : jamais l'ordre du fichier
    (plumesign l'écrit dans un ordre qui change d'un lancement à l'autre)."""
    sel = [a["email"] for a in accs if a["selected"]]
    if sel:
        return sel[0]
    return min((a["email"] for a in accs), key=lambda e: (e.casefold(), e), default=None)


def default_account() -> str | None:
    return pick_default(accounts())


def find_account(email: str | None) -> str | None:
    """Compte du store qui correspond à cette adresse, sans tenir compte de la casse (Apple ne la
    distingue pas) ; None s'il n'y en a pas. Renvoie l'écriture du store, celle qui sert de clé."""
    wanted = (email or "").strip().casefold()
    if not wanted:
        return None
    found = sorted((a["email"] for a in accounts() if a["email"].casefold() == wanted),
                   key=lambda e: (e != (email or "").strip(), e))
    return found[0] if found else None


class UnknownAccount(RuntimeError):
    """Compte absent du store plumesign (déjà retiré, ou interface périmée)."""


async def _switch(email: str) -> None:
    if _store().get("selected_account") == email:
        return  # évite une réécriture inutile de accounts.json
    if email not in (_store().get("accounts") or {}):
        raise UnknownAccount(f"compte {email} introuvable")
    res = await _plume("account", "switch", email, timeout=30)
    if not res.ok:
        raise RuntimeError(f"compte {email} introuvable : {res.tail()}")


@asynccontextmanager
async def _as(email: str):
    """Exécute sous le compte donné puis rétablit le compte par défaut choisi par l'utilisateur
    (« account switch » est persistant). À utiliser sous LOCK."""
    prev = _store().get("selected_account")
    await _switch(email)
    try:
        yield
    finally:
        if prev and prev != email and prev in (_store().get("accounts") or {}):
            try:
                await _switch(prev)
            except Exception as e:  # noqa: BLE001 - non bloquant
                db.log(f"compte par défaut {prev} non rétabli : {e}", "warn")


# Comptes dont la déconnexion a déjà été signalée (une alerte par épisode, quel que soit le chemin qui
# la découvre : contrôle périodique ou renouvellement). Un compte redevenu valide (ou reconnecté) sort
# de l'ensemble.
ALERTED: set[str] = set()


def _set_status(email: str, ok: bool | None, message: str) -> None:
    """ok=None : état indéterminé (réseau, anisette) ; on garde le dernier état connu."""
    if ok:
        ALERTED.discard(email)
    if ok is None:
        db.execute("INSERT INTO account_status (email, ok, message, checked_at) VALUES (?, NULL, ?, ?) "
                   "ON CONFLICT(email) DO UPDATE SET message = excluded.message, "
                   "checked_at = excluded.checked_at", email, message, time.time())
        return
    db.execute("INSERT INTO account_status (email, ok, message, checked_at) VALUES (?, ?, ?, ?) "
               "ON CONFLICT(email) DO UPDATE SET ok = excluded.ok, message = excluded.message, "
               "checked_at = excluded.checked_at", email, int(ok), message, time.time())


# Classement des erreurs plumesign : code Apple structuré d'abord, phrases précises ensuite.
_API_CODE = re.compile(r"Developer API error (-?\d+)")
# 1100 = session portail expirée ; -20101/-22406/-22410 = identifiants/jeton refusés par GSA
_SESSION_CODES = {"1100", "-20101", "-22406", "-22410"}
_SESSION_TXT = re.compile(r"session (has )?expired|please (log|sign) in|not authenticated|"
                          r"HTTP Some\(401\)|Authentication SRP error|Authentication extra step|"
                          r"No account (selected|currently)", re.I)
_TRANSIENT = re.compile(r"Anisette error|Reqwest error|Request to developer session failed|"
                        r"délai dépassé|panicked|timed out|dns error|connection (refused|reset)", re.I)
# Le portail répond 401 : jeton refusé, quel que soit le code Apple qui l'accompagne.
_HTTP_401 = re.compile(r"Developer API error -?\d+ \(HTTP Some\(401\)\)")
# Panne réseau / anisette : ne prouve rien sur la session, même si le texte mentionne un 401 ou « SRP ».
_NETWORK_TXT = re.compile(r"Anisette error|Reqwest error|dns error|connection (refused|reset|closed)|timed out|"
                          r"délai dépassé|panicked", re.I)

# Lignes de journal de plumesign (« [2026-10-15T19:40:36Z WARN  omnisette::remote_anisette_v3] … ») : elles
# racontent ce qui s'est passé (repli sur un autre serveur anisette…), pas pourquoi la commande échoue.
_LOG_LINE = re.compile(r"^\[\d{4}-\d\d-\d\dT[\d:.]+Z\s+(INFO|WARN|DEBUG|TRACE)\b")


def _errors(output: str) -> str:
    """La sortie de plumesign sans ses lignes de journal : les erreurs, les paniques, les invites."""
    return "\n".join(line for line in output.splitlines() if not _LOG_LINE.match(line))


# Refus d'Apple qui ne disent rien de la session et qu'il ne sert à rien de répéter :
# - -80045 « This action cannot be completed at this time » : refus de l'ouverture de session (SRP init),
#   côté compte ou adresse IP, même avec des données anisette Windows valides ;
# - HTTP 429 : trop de requêtes, Apple demande de ralentir ;
# - HTTP 503 (page HTML) : données anisette ou identité d'appareil refusées (serveur Android, identité
#   périmée), ou charge momentanée du serveur d'Apple. Les services développeur (« Apple developer services »)
#   répondent comme GrandSlam.
_APPLE_REFUSED = re.compile(r"-80045\b|cannot be completed at this time", re.I)
_APPLE_429 = re.compile(r"(GrandSlam|GSA|developer services) (answered|returned) HTTP 429|"
                        r"Apple answered HTTP 429 to the anisette|Too Many Requests", re.I)
_APPLE_503 = re.compile(r"(GrandSlam|GSA|developer services) (answered|returned) HTTP 50[234]|"
                        r"Apple answered HTTP 50[234] to the anisette|"
                        r"50[234] (Service Temporarily Unavailable|Bad Gateway|Gateway Time-?out)", re.I)
# Envoi ou contrôle d'un code par SMS refusé (HTTP 403 « Access to this Apple Developer Service was denied »)
_SMS_DENIED = re.compile(r"SMS 2FA refused|Access to this Apple Developer Service was denied", re.I)
# -22411 : Apple ID jamais utilisé sur un appareil Apple
_NEVER_SIGNED_IN = re.compile(r"-22411\b")
# Serveur anisette qui ne fournit pas de données Windows : plumesign ne l'utilise pas (Apple les refuse)
_ANISETTE_UNSUPPORTED = re.compile(r"only accepts Windows anisette", re.I)
# -22421 (hsc 434), -22409, -80009 : données anisette désynchronisées ou refusées ;
# -45061 : identité anisette non provisionnée (plumesign en refait une toute seule)
_ANISETTE_REFUSED = re.compile(r"-22421\b|-22409\b|-80009\b|MID is invalid", re.I)
_REPROVISION = re.compile(r"-45061\b|Anisette not provisioned", re.I)

# Ce qu'il faut faire d'une erreur : « apple_refused » (-80045) et « apple_rate_limited » (429) attendent
# longtemps, Apple limite les essais ; « apple_503 » et « anisette_* » sont réessayées à l'allure habituelle ;
# « sms_denied », « never_signed_in » et « anisette_unsupported » demandent une action de l'utilisateur
# (aucun essai automatique n'y change rien).
USER_KINDS = frozenset({"sms_denied", "never_signed_in", "anisette_unsupported"})
SLOW_KINDS = frozenset({"apple_refused", "apple_rate_limited"})


def classify(output: str) -> str | None:
    """Nature d'un refus d'Apple ou d'un problème d'anisette reconnu dans la sortie de plumesign, ou None.
    Jamais « session expirée » : ces erreurs ne disent rien de l'état de la session.
    Les lignes de journal (repli sur un autre serveur, nouvel essai…) ne comptent pas : seule l'erreur finale."""
    output = _errors(output)
    for kind, rx in (("apple_refused", _APPLE_REFUSED), ("apple_rate_limited", _APPLE_429),
                     ("apple_503", _APPLE_503), ("sms_denied", _SMS_DENIED),
                     ("never_signed_in", _NEVER_SIGNED_IN), ("anisette_unsupported", _ANISETTE_UNSUPPORTED),
                     ("anisette_refused", _ANISETTE_REFUSED), ("anisette_reprovision", _REPROVISION)):
        if rx.search(output):
            return kind
    return None


# Message du compte « à reconnecter » : ce qu'Apple exige et pourquoi
REAUTH_REASON = ("Apple n'accepte plus la session enregistrée : reconnecte le compte (mot de passe, puis code "
                 "à deux facteurs affiché sur un appareil Apple de confiance)")
# le même, dans une notification en anglais (le texte français reste celui de l'état du compte et du journal)
REAUTH_REASON_EN = ("Apple no longer accepts the stored session: sign the account in again (password, then the "
                    "two-factor code shown on a trusted Apple device)")


def session_expired(output: str) -> bool:
    if classify(output):
        return False        # -80045, 503, SMS refusé, -22411… : aucun rapport avec la session
    codes = set(_API_CODE.findall(output))
    if codes & _SESSION_CODES or _HTTP_401.search(output):
        return True
    return not codes and bool(_SESSION_TXT.search(output)) and not _NETWORK_TXT.search(_errors(output))


def _classify(res: Result) -> bool | None:
    """True = session valide, False = reconnexion nécessaire, None = indéterminé."""
    if res.ok:
        return True
    if session_expired(res.output):
        return False
    return None


class SessionExpired(RuntimeError):
    """Apple a fermé la session : reconnexion du compte nécessaire."""

    def __init__(self, message: str, email: str | None = None):
        super().__init__(message)
        self.email = email


class Transient(RuntimeError):
    """Anisette, réseau ou Apple momentanément indisponibles : nouvel essai rapproché."""


class AppleRefused(RuntimeError):
    """Apple refuse la requête (-80045, HTTP 503/429). Avec -80045 (kind « apple_refused ») et 429
    (« apple_rate_limited ») Apple limite les essais : pas de nouvel essai rapproché, ça prolongerait le
    blocage (voir renew._retry_at)."""

    def __init__(self, message: str, kind: str = "apple_refused"):
        super().__init__(message)
        self.kind = kind

    @property
    def slow(self) -> bool:
        return self.kind in SLOW_KINDS


ANISETTE_STATE = "anisette-state-windows.plist"      # identité anisette de plumesign (voir PATCHES.md)
_RESET_AT = 0.0


def reset_anisette(reason: str) -> bool:
    """Oublie l'identité anisette : plumesign en crée et en provisionne une nouvelle à la commande suivante.
    Pour des données qu'Apple refuse alors que le serveur répond (-22409, -80009, -22421 après resynchronisation).
    Au plus une fois par heure : chaque identité provisionnée est une requête de plus chez Apple."""
    global _RESET_AT
    if time.time() - _RESET_AT < 3600:
        return False
    try:
        (STORE / ANISETTE_STATE).unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        db.log(f"identité anisette non réinitialisée : {e}", "warn")
        return False
    _RESET_AT = time.time()
    db.log(f"Identité anisette réinitialisée ({reason}) : une nouvelle sera créée au prochain essai", "warn")
    return True


class LimitReached(RuntimeError):
    """Limite du compte gratuit atteinte (3 apps actives par appareil, 10 App ID par 7 jours) :
    réessayer tout de suite ne sert à rien et consomme des App ID."""


# Messages d'Apple (portail) et de l'Apple TV (installd) quand la limite du compte gratuit est atteinte.
_LIMIT = re.compile(
    r"maximum number of (installed )?(apps|app ?ids?)|too many (installed )?(apps|app ?ids?)|app ?id limit|"
    r"limit of \d+ (apps|app ?ids)|reached the maximum number|cannot create .{0,20}app ?id", re.I)


def is_limit(text: str) -> bool:
    return bool(_LIMIT.search(text or ""))


def _failure(email: str, what: str, res: Result, content: bool = False) -> RuntimeError:
    """Erreur d'une commande plumesign du compte, classée : session expirée (bouton Reconnecter),
    refus d'Apple (attendre), passagère (anisette, réseau), ou autre. content : l'IPA signée est en cause
    (ContentError) quand la sortie le dit ; une limite du compte Apple reste une erreur ordinaire."""
    kind = classify(res.output)
    if kind in ("apple_refused", "apple_rate_limited", "apple_503"):
        return AppleRefused(f"{what} : {_why(res)}", kind)
    if kind in USER_KINDS:
        err = RuntimeError(f"{what} : {_why(res)}")
        err.user_kind = kind          # lu par renew.failure_kind : action requise, pas de nouvel essai automatique
        return err
    if session_expired(res.output):
        _set_status(email, False, f"session expirée, reconnexion nécessaire : {REAUTH_REASON}")
        return SessionExpired(f"session Apple de {email} expirée : reconnecte-le (bouton Reconnecter) "
                              f"[{what} : {res.tail(2)}]", email)
    if is_limit(res.output):
        return LimitReached(limit_message(what, res.tail(2)))
    if kind == "anisette_refused":
        reset_anisette(f"Apple refuse les données anisette : {what}")
    errors = _errors(res.output)
    if kind or re.search(r"anisette|panicked", errors, re.I) or _TRANSIENT.search(errors) or res.code == -1:
        return Transient(f"{what} : {_why(res)} (passager, nouvel essai automatique)")
    if content and is_content_error(res.output):
        return ContentError(f"{what} : {res.tail()}")
    return RuntimeError(f"{what} : {res.tail()}")


def explain(kind: str | None, detail: str = "") -> str | None:
    """Phrase en français pour un refus reconnu par classify() (None : rien de particulier à dire).
    detail : la dernière ligne de plumesign, gardée pour le diagnostic."""
    tail = f" | {detail}" if detail else ""
    if kind == "apple_refused":
        return ("Apple refuse temporairement la connexion (erreur -80045, côté compte ou réseau) : n'insiste pas, "
                "Apple limite les essais et chaque nouvel essai peut prolonger le blocage ; attends plusieurs "
                f"heures{tail}")
    if kind == "apple_rate_limited":
        return ("Apple limite le nombre de requêtes (HTTP 429) : n'insiste pas, tvsign attend plusieurs heures "
                f"avant de réessayer{tail}")
    if kind == "apple_503":
        return ("Apple répond HTTP 503 : données anisette refusées (serveur anisette Android ?) ou serveur d'Apple "
                f"surchargé ; nouvel essai automatique plus tard{tail}")
    if kind == "sms_denied":
        return f"Apple refuse les codes par SMS : utilise le code d'un appareil Apple de confiance{tail}"
    if kind == "never_signed_in":
        return f"Apple refuse cet Apple ID (-22411) : il n'a jamais été connecté sur un appareil Apple{tail}"
    if kind == "anisette_unsupported":
        return ("le serveur anisette choisi ne fournit pas de données Windows, les seules qu'Apple accepte depuis le "
                "12 octobre 2026 : laisse le réglage vide (serveur par défaut) ou choisis un serveur qui en fournit"
                f"{tail}")
    if kind == "anisette_refused":
        return f"Apple refuse les données anisette (-22421/-22409) : identité anisette à réinitialiser{tail}"
    if kind == "anisette_reprovision":
        return f"identité anisette invalide (-45061), refaite automatiquement{tail}"
    return None


def limit_message(what: str, detail: str = "") -> str:
    return (f"limite du compte gratuit atteinte ({what}) : 3 apps actives par Apple TV et 10 App ID par 7 jours, "
            "les apps installées avec d'autres outils comptent aussi. Retire une app ou attends"
            + (f" [{detail}]" if detail else ""))


def _why(res: Result) -> str:
    known = explain(classify(res.output), res.tail(1))
    if known:
        return known
    errors = _errors(res.output)
    if re.search(r"anisette", errors, re.I):
        return f"serveur anisette indisponible : {res.tail(2)}"
    if re.search(r"panicked", errors, re.I):
        return f"plumesign a planté (réponse inattendue d'Apple ?) : {res.tail(2)}"
    if _TRANSIENT.search(errors):
        return f"réseau ou Apple injoignable : {res.tail(2)}"
    return f"vérification impossible : {res.tail(2)}"


async def check_account(email: str) -> bool | None:
    """Vérifie que la session Apple est encore valide (appel au portail développeur).
    Renvoie None si on ne peut pas conclure (anisette, réseau) : pas de reconnexion à réclamer."""
    known = find_account(email)
    if not known:
        return None          # compte inconnu : rien à vérifier, et pas de ligne d'état pour un compte absent
    email = known
    before = next((a["ok"] for a in accounts() if a["email"] == email), None)
    try:
        async with _locked(), _as(email):
            res = await _plume("account", "devices", timeout=90)
    except Exception as e:  # noqa: BLE001
        _set_status(email, None, f"vérification impossible : {e}")
        return None
    ok = _classify(res)
    if ok is None:
        _set_status(email, None, _why(res))
    else:
        _set_status(email, ok, "session valide" if ok else f"session expirée, reconnexion nécessaire : {REAUTH_REASON}")
        if ok and before != 1:
            _valid(email)       # session de nouveau valide : les apps en attente peuvent repartir tout de suite
    return ok


def start_login(email: str, password: str) -> Session:
    """Connexion Apple. La session passe en « waiting » quand Apple demande le code 2FA."""
    # Apple ne distingue pas la casse : si ce compte est déjà connu (autre écriture), on garde celle du store
    email = find_account(email) or email.strip()
    session = register(Session("login", re.compile(r"Enter (2FA code|the code sent)", re.I)))
    session.result["email"] = email

    async def done(s: Session) -> None:
        if s.exit_ok:
            _set_status(email, True, "connecté")
            _backup_store()
            db.execute("DELETE FROM account_devices WHERE email = ?", email)
            db.log(f"Compte Apple {email} connecté")
            _valid(email)
        elif s.user_cancelled:
            db.log(f"Connexion Apple {email} annulée")
        else:
            output = "\n".join(s.lines)
            kind = classify(output)
            errors = [l for l in _errors(output).splitlines() if l.strip()]      # sans les lignes de journal
            last = next((l for l in reversed(errors) if l.startswith("Error")), errors[-1] if errors else "")
            db.log(f"Connexion Apple {email} échouée : "
                   f"{explain(kind, last) or ' | '.join(errors[-2:] or s.lines[-2:])}", "error")
            if kind == "anisette_refused":
                reset_anisette("connexion refusée pour des données anisette invalides")

    async def go() -> None:
        try:
            async with _locked():
                if session.state == "failed":
                    return  # annulée avant d'avoir démarré
                prev = _store().get("selected_account")   # une connexion ne change pas le compte par défaut
                # mot de passe par l'environnement du seul processus enfant : jamais dans argv (ps)
                e = env()
                e["RUST_LOG"] = "info"     # l'invite du code 2FA n'est écrite qu'à ce niveau
                e["PLUMESIGN_PASSWORD"] = password
                await session.start(PLUMESIGN, "account", "login", "-u", email, env=e, on_exit=done)
                del e
                # Délai borné : un code 2FA jamais saisi ne doit pas bloquer les renouvellements.
                deadline = time.monotonic() + LOGIN_TIMEOUT
                while session.state in ("running", "waiting"):
                    if time.monotonic() > deadline:
                        session.lines.append("délai dépassé, connexion abandonnée")
                        session.cancel()
                        break
                    await asyncio.sleep(0.5)
                # ne rendre le verrou qu'une fois plumesign terminé (accounts.json)
                if session.alive:
                    session.cancel()
                if session._proc:
                    try:
                        await asyncio.wait_for(session._proc.wait(), 10)
                    except TimeoutError:
                        pass
                if prev and prev != email and prev in (_store().get("accounts") or {}):
                    try:
                        await _switch(prev)
                    except Exception as e:  # noqa: BLE001 - non bloquant
                        db.log(f"compte par défaut {prev} non rétabli : {e}", "warn")
        except Exception as e:  # noqa: BLE001
            session.lines.append(str(e))
            session.state = "failed"
            db.log(f"Connexion Apple {email} échouée : {e}", "error")
        finally:
            session.cancel()

    spawn(go())
    return session


async def logout(email: str) -> None:
    try:
        async with _locked(), _as(email):
            res = await _plume("account", "logout", timeout=30)
    except UnknownAccount:
        db.execute("DELETE FROM account_status WHERE email = ?", email)   # déjà retiré du store
        _forget_cert_expiry(email)
        raise
    if not res.ok:
        raise RuntimeError(f"plumesign account logout : {res.tail(2)}")
    db.execute("DELETE FROM account_status WHERE email = ?", email)
    _forget_cert_expiry(email)
    # plumesign ne désigne plus de compte par défaut : on en choisit un de façon stable (ordre alphabétique)
    # plutôt que de laisser l'ordre du fichier décider d'un redémarrage à l'autre
    remaining = accounts()
    if remaining and not any(a["selected"] for a in remaining):
        try:
            async with _locked():
                await _switch(pick_default(remaining))
        except Exception as e:  # noqa: BLE001 - non bloquant
            db.log(f"compte par défaut non rétabli : {e}", "warn")


async def select(email: str) -> None:
    async with _locked():
        await _switch(email)


# ---------- équipes de développeur ----------

_TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")


def parse_teams(output: str) -> list[dict]:
    """Lignes JSON de « account teams --json » -> [{team_id, name, type, status, selected}]."""
    teams = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and _TEAM_ID.match(str(item.get("team_id", ""))):
            teams.append({"team_id": item["team_id"], "name": item.get("name") or item["team_id"],
                          "type": item.get("type"), "status": item.get("status"),
                          "selected": bool(item.get("selected"))})
    return teams


def env_team() -> str | None:
    """Équipe imposée par PLUMESIGN_TEAM_ID (environnement du conteneur) : elle l'emporte sur le choix mémorisé."""
    return os.environ.get("PLUMESIGN_TEAM_ID", "").strip() or None


async def teams(email: str) -> list[dict]:
    """Équipes de développeur du compte (appel Apple)."""
    async with _locked(), _as(email):
        # stdout et stderr ensemble : _failure classe la session expirée / l'anisette d'après le journal
        # (stderr) ; parse_teams ignore les lignes qui ne sont pas du JSON.
        res = await _plume("account", "teams", "--json", extra_env={"RUST_LOG": "warn"}, timeout=90)
    if not res.ok:
        raise _failure(email, "liste des équipes", res)
    return parse_teams(res.output)


async def set_team(email: str, team_id: str) -> None:
    """Équipe utilisée pour signer : mémorisée par plumesign dans accounts.json."""
    if not _TEAM_ID.match(team_id or ""):
        raise ValueError("identifiant d'équipe invalide")
    async with _locked(), _as(email):
        res = await _plume("account", "set-team", team_id, timeout=90)
    if not res.ok:
        raise _failure(email, "choix de l'équipe", res)
    _forget_cert_expiry(email)   # le certificat dépend de l'équipe : la date affichée n'est plus la bonne
    db.log(f"Équipe {team_id} choisie pour {email} : les prochaines signatures l'utilisent")


# ---------- signature ----------

_DUPLICATE = re.compile(r"already exists|existe déjà|déjà enregistr", re.I)


async def ensure_device(email: str, udid: str, name: str, on_lock=None, platform: str | None = None) -> None:
    """Enregistre l'appareil chez Apple. platform (ios, tvos) : seulement quand il faut le préciser
    (iPhone / iPad) ; sans lui, l'appel est celui d'origine."""
    plat = ["--platform", platform] if platform else []
    # Pas de cache : sur un compte gratuit l'enregistrement expire au bout de 7 jours
    # (erreur 8220 « no devices » ensuite), et le réenregistrer le prolonge.
    # on_lock("waiting"|"acquired") : voir _locked().
    async with _locked(on_lock), _as(email):
        res = await _plume("account", "register-device", "-u", udid, "-n", name, *plat, timeout=90)
        listed = None
        if not res.ok and not _DUPLICATE.search(res.output):
            # Réponse ambiguë : on vérifie dans la liste des appareils plutôt que de deviner.
            listed = await _plume("account", "devices", *plat, timeout=90)
    if not res.ok and not _DUPLICATE.search(res.output):
        if not (listed and listed.ok and udid.lower() in listed.output.lower()):
            raise _failure(email, f"enregistrement de l'appareil sur {email}", res)
    _set_status(email, True, "session valide")
    db.execute("INSERT OR IGNORE INTO account_devices (email, udid) VALUES (?, ?)", email, udid)


async def sign(email: str, ipa: Path, out: Path, on_lock=None) -> bool:
    """Signe ipa vers out (remplacé seulement en cas de succès).
    Renvoie True si plumesign a dû révoquer un certificat d'une autre application ou installation
    (Xcode, SideStore, un autre tvsign) : les certificats morts ou ceux de cette installation ne comptent pas."""
    tmp = out.with_name(out.stem + ".tmp.ipa")
    tmp.unlink(missing_ok=True)
    try:
        async with _locked(on_lock), _as(email):
            res = await _plume("sign", "-p", ipa, "--apple-id", "-o", tmp, extra_env={"RUST_LOG": "warn"},
                               timeout=600)
        if not res.ok or not tmp.exists():
            sweep_stage()
            raise _failure(email, "signature", res, content=True)
        replace_retry(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    _set_status(email, True, "session valide")
    expiry = cert_expiry(res.output)
    if expiry:
        settings.raw_set(_CERT_KEY + email, expiry)
    return FOREIGN_REVOKED in res.output


async def sign_with_certificate(files, ipa: Path, out: Path, on_lock=None) -> None:
    """Signe ipa vers out avec un certificat importé (voir imported_cert.signing_files : `files` porte les
    chemins cert, key et profile). Aucun compte Apple, aucune session, aucun serveur anisette : plumesign signe
    hors ligne, avec le profil donné. Même verrou que les autres opérations plumesign (une à la fois)."""
    tmp = out.with_name(out.stem + ".tmp.ipa")
    tmp.unlink(missing_ok=True)
    try:
        async with _locked(on_lock):
            e = env()
            e["RUST_LOG"] = "warn"
            # --pem lit tous les arguments qui suivent : une autre option doit venir après
            res = await run(PLUMESIGN, "sign", "-p", ipa, "--pem", files.cert, files.key, "--provision", files.profile,
                            "-o", tmp, env=e, timeout=600)
        if not res.ok or not tmp.exists():
            sweep_stage()
            what = "signature avec le certificat importé"
            if res.code == -1:
                raise Transient(f"{what} : délai dépassé (passager, nouvel essai automatique)")
            if is_content_error(res.output):
                raise ContentError(f"{what} : {res.tail()}")
            raise RuntimeError(f"{what} : {res.tail()}")
        replace_retry(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


def signing_key(email: str) -> str | None:
    """Clé privée PEM avec laquelle plumesign signe pour ce compte (keys/<équipe>/key.pem du store),
    ou None si on ne la trouve pas (équipe inconnue et plusieurs clés possibles)."""
    team = next((a["team_id"] for a in accounts() if a["email"] == email), None)
    keys = STORE / "keys"
    paths = [keys / team / "key.pem"] if team else sorted(keys.glob("*/key.pem"))
    if len(paths) != 1:
        return None
    try:
        return paths[0].read_text(encoding="utf-8")
    except OSError:
        return None


async def issued_cert_serials(email: str) -> set[str] | None:
    """Numéros de série (hex, sans zéros de tête) des certificats encore valides du compte.
    None en cas d'échec : une erreur réseau ne doit jamais forcer un renouvellement."""
    try:
        async with _locked(), _as(email):
            # la liste est écrite dans le journal info (format Debug Rust)
            res = await _plume("account", "certificates", extra_env={"RUST_LOG": "info"}, timeout=90)
    except Exception:  # noqa: BLE001
        return None
    if not res.ok:
        return None
    found = re.findall(r'serial_number:\s*"([0-9A-Fa-f]+)",\s*status:\s*"([^"]*)"', res.output)
    if not found and ("serial_number" in res.output or "[]" not in res.output):
        return None  # format inattendu ou illisible : on ne conclut rien (seule une liste vide explicite compte)
    if len(found) < res.output.count("serial_number"):
        return None  # certaines entrées n'ont pas été lues : un certificat valide pourrait manquer
    return {serial.upper().lstrip("0") for serial, status in found if status.lower() == "issued"}


# ---------- découverte réseau ----------

async def version() -> str | None:
    """« plumesign 2.6.1 », ou None si le binaire est absent / inutilisable."""
    try:
        res = await run(PLUMESIGN, "--version", env=env(), timeout=15, split=True)
    except (OSError, RuntimeError):
        return None
    return res.output.strip() or None if res.ok else None


async def discover(seconds: int = 6, ip: str | None = None) -> list[dict]:
    """Annonces mDNS des Apple TV. Lecture seule : n'ouvre aucune connexion vers les TV.
    Avec `ip` (multicast bloqué), la requête mDNS est envoyée directement à cette adresse."""
    e = env()
    e["RUST_LOG"] = "warn"
    extra = ["--ip", ip] if ip else []
    res = await run(PLUMESIGN, "pair", "discover", "-t", seconds, "--json", *extra, env=e,
                    timeout=seconds + 30, split=True)
    if not res.ok:
        if ip and re.search(r"unexpected argument|unrecognized", res.err, re.I):
            raise RuntimeError("plumesign trop ancien pour chercher à une adresse : mets-le à jour avec tvsign")
        raise RuntimeError(f"recherche des Apple TV : {res.tail(2)}")
    found = []
    for line in res.output.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                found.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return found
