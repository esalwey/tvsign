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
from .config import IMPORT_PLUME, LOGIN_TIMEOUT, PLUME_HOME, PLUME_LOCK_TIMEOUT, PLUMESIGN
from .proc import Result, Session, register, run, spawn
from .util import replace_retry

# accounts.json est partagé : une seule opération plumesign à la fois.
LOCK = asyncio.Lock()
STORE = PLUME_HOME / "PlumeImpactor"


def env() -> dict:
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
    anisette = settings.anisette_url()
    if anisette:
        e["PLUMESIGN_ANISETTE_URL"] = anisette
    return e


@asynccontextmanager
async def _locked():
    """Verrou plumesign borné : un détenteur bloqué devient une erreur visible, pas un blocage muet."""
    try:
        await asyncio.wait_for(LOCK.acquire(), PLUME_LOCK_TIMEOUT)
    except TimeoutError:
        raise RuntimeError("plumesign occupé depuis trop longtemps (connexion Apple en attente ?)") from None
    try:
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
    Apple ; on le dit une fois dans le journal. True si l'ancienne était encore là."""
    if not (STORE / "state.plist").exists() or (STORE / ANISETTE_STATE).exists():
        return False
    db.log("Identité anisette Android abandonnée (Apple refuse ses données depuis le 12 octobre 2026) : "
           "une identité Windows sera créée au prochain contact avec Apple")
    return True


# ---------- comptes ----------

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
            "selected": email == selected,
            "ok": st.get("ok"),                 # 1 valide, 0 reconnexion nécessaire, None inconnu
            "message": st.get("message"),
            "checked_at": st.get("checked_at"),
        })
    return out


def default_account() -> str | None:
    accs = accounts()
    sel = [a["email"] for a in accs if a["selected"]]
    return sel[0] if sel else (accs[0]["email"] if accs else None)


class UnknownAccount(RuntimeError):
    """Compte absent du store plumesign (déjà retiré, ou interface périmée)."""


async def _switch(email: str) -> None:
    if _store().get("selected_account") == email:
        return  # évite une réécriture inutile de accounts.json
    if email not in (_store().get("accounts") or {}):
        raise UnknownAccount(f"compte {email} introuvable")
    res = await run(PLUMESIGN, "account", "switch", email, env=env(), timeout=30)
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


def session_expired(output: str) -> bool:
    if classify(output):
        return False        # -80045, 503, SMS refusé, -22411… : aucun rapport avec la session
    codes = set(_API_CODE.findall(output))
    if codes & _SESSION_CODES:
        return True
    return not codes and bool(_SESSION_TXT.search(output))


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


def _failure(email: str, what: str, res: Result) -> RuntimeError:
    """Erreur d'une commande plumesign du compte, classée : session expirée (bouton Reconnecter),
    refus d'Apple (attendre), passagère (anisette, réseau), ou autre."""
    kind = classify(res.output)
    if kind in ("apple_refused", "apple_rate_limited", "apple_503"):
        return AppleRefused(f"{what} : {_why(res)}", kind)
    if kind in USER_KINDS:
        return RuntimeError(f"{what} : {_why(res)}")
    if session_expired(res.output):
        _set_status(email, False, f"session expirée, reconnexion nécessaire : {REAUTH_REASON}")
        return SessionExpired(f"session Apple de {email} expirée : reconnecte-le (bouton Reconnecter) "
                              f"[{what} : {res.tail(2)}]", email)
    if kind == "anisette_refused":
        reset_anisette(f"Apple refuse les données anisette : {what}")
    errors = _errors(res.output)
    if kind or re.search(r"anisette|panicked", errors, re.I) or _TRANSIENT.search(errors) or res.code == -1:
        return Transient(f"{what} : {_why(res)} (passager, nouvel essai automatique)")
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
    try:
        async with _locked(), _as(email):
            res = await run(PLUMESIGN, "account", "devices", env=env(), timeout=90)
    except Exception as e:  # noqa: BLE001
        _set_status(email, None, f"vérification impossible : {e}")
        return None
    ok = _classify(res)
    if ok is None:
        _set_status(email, None, _why(res))
    else:
        _set_status(email, ok, "session valide" if ok else f"session expirée, reconnexion nécessaire : {REAUTH_REASON}")
    return ok


def start_login(email: str, password: str) -> Session:
    """Connexion Apple. La session passe en « waiting » quand Apple demande le code 2FA."""
    session = register(Session("login", re.compile(r"Enter (2FA code|the code sent)", re.I)))
    session.result["email"] = email

    async def done(s: Session) -> None:
        if s.state == "done":
            _set_status(email, True, "connecté")
            _backup_store()
            db.execute("DELETE FROM account_devices WHERE email = ?", email)
            db.log(f"Compte Apple {email} connecté")
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
            res = await run(PLUMESIGN, "account", "logout", env=env(), timeout=30)
    except UnknownAccount:
        db.execute("DELETE FROM account_status WHERE email = ?", email)   # déjà retiré du store
        raise
    if not res.ok:
        raise RuntimeError(f"plumesign account logout : {res.tail(2)}")
    db.execute("DELETE FROM account_status WHERE email = ?", email)


async def select(email: str) -> None:
    async with _locked():
        await _switch(email)


# ---------- signature ----------

_DUPLICATE = re.compile(r"already exists|existe déjà|déjà enregistr", re.I)


async def ensure_device(email: str, udid: str, name: str) -> None:
    # Pas de cache : sur un compte gratuit l'enregistrement expire au bout de 7 jours
    # (erreur 8220 « no devices » ensuite), et le réenregistrer le prolonge.
    async with _locked(), _as(email):
        res = await run(PLUMESIGN, "account", "register-device", "-u", udid, "-n", name, env=env(), timeout=90)
        listed = None
        if not res.ok and not _DUPLICATE.search(res.output):
            # Réponse ambiguë : on vérifie dans la liste des appareils plutôt que de deviner.
            listed = await run(PLUMESIGN, "account", "devices", env=env(), timeout=90)
    if not res.ok and not _DUPLICATE.search(res.output):
        if not (listed and listed.ok and udid.lower() in listed.output.lower()):
            raise _failure(email, f"enregistrement de l'appareil sur {email}", res)
    _set_status(email, True, "session valide")
    db.execute("INSERT OR IGNORE INTO account_devices (email, udid) VALUES (?, ?)", email, udid)


async def sign(email: str, ipa: Path, out: Path) -> bool:
    """Signe ipa vers out (remplacé seulement en cas de succès).
    Renvoie True si plumesign a dû révoquer un ancien certificat du compte."""
    tmp = out.with_name(out.stem + ".tmp.ipa")
    tmp.unlink(missing_ok=True)
    try:
        async with _locked(), _as(email):
            e = env()
            e["RUST_LOG"] = "warn"
            res = await run(PLUMESIGN, "sign", "-p", ipa, "--apple-id", "-o", tmp, env=e, timeout=600)
        if not res.ok or not tmp.exists():
            raise _failure(email, "signature", res)
        replace_retry(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    _set_status(email, True, "session valide")
    return "Revoked certificate" in res.output


async def issued_cert_serials(email: str) -> set[str] | None:
    """Numéros de série (hex, sans zéros de tête) des certificats encore valides du compte.
    None en cas d'échec : une erreur réseau ne doit jamais forcer un renouvellement."""
    try:
        async with _locked(), _as(email):
            e = env()
            e["RUST_LOG"] = "info"   # la liste est écrite dans le journal info (format Debug Rust)
            res = await run(PLUMESIGN, "account", "certificates", env=e, timeout=90)
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


async def discover(seconds: int = 6) -> list[dict]:
    """Annonces mDNS des Apple TV. Lecture seule : n'ouvre aucune connexion vers les TV."""
    e = env()
    e["RUST_LOG"] = "warn"
    res = await run(PLUMESIGN, "pair", "discover", "-t", seconds, "--json", env=e,
                    timeout=seconds + 30, split=True)
    if not res.ok:
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
