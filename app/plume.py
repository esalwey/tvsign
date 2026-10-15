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
    anisette = settings.anisette_url()
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


def _set_status(email: str, ok: bool | None, message: str) -> None:
    """ok=None : état indéterminé (réseau, anisette) ; on garde le dernier état connu."""
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


def session_expired(output: str) -> bool:
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


class Transient(RuntimeError):
    """Anisette, réseau ou Apple momentanément indisponibles : nouvel essai rapproché."""


def _failure(email: str, what: str, res: Result) -> RuntimeError:
    """Erreur d'une commande plumesign du compte, classée : session expirée (bouton Reconnecter),
    passagère (anisette, réseau), ou autre."""
    if session_expired(res.output):
        _set_status(email, False, "session expirée, reconnexion nécessaire")
        return SessionExpired(f"session Apple de {email} expirée : reconnecte-le (bouton Reconnecter) "
                              f"[{what} : {res.tail(2)}]")
    if re.search(r"anisette|panicked", res.output, re.I) or _TRANSIENT.search(res.output) or res.code == -1:
        return Transient(f"{what} : {_why(res)} (passager, nouvel essai automatique)")
    return RuntimeError(f"{what} : {res.tail()}")


def _why(res: Result) -> str:
    if re.search(r"anisette", res.output, re.I):
        return f"serveur anisette indisponible : {res.tail(2)}"
    if re.search(r"panicked", res.output, re.I):
        return f"plumesign a planté (réponse inattendue d'Apple ?) : {res.tail(2)}"
    if _TRANSIENT.search(res.output):
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
        _set_status(email, ok, "session valide" if ok else f"session expirée, reconnexion nécessaire : {res.tail(2)}")
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
            db.log(f"Connexion Apple {email} échouée : {' | '.join(s.lines[-2:])}", "error")

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
        _forget_cert_expiry(email)
        raise
    if not res.ok:
        raise RuntimeError(f"plumesign account logout : {res.tail(2)}")
    db.execute("DELETE FROM account_status WHERE email = ?", email)
    _forget_cert_expiry(email)


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
        e = env()
        e["RUST_LOG"] = "warn"
        # stdout et stderr ensemble : _failure classe la session expirée / l'anisette d'après le journal
        # (stderr) ; parse_teams ignore les lignes qui ne sont pas du JSON.
        res = await run(PLUMESIGN, "account", "teams", "--json", env=e, timeout=90)
    if not res.ok:
        raise _failure(email, "liste des équipes", res)
    return parse_teams(res.output)


async def set_team(email: str, team_id: str) -> None:
    """Équipe utilisée pour signer : mémorisée par plumesign dans accounts.json."""
    if not _TEAM_ID.match(team_id or ""):
        raise ValueError("identifiant d'équipe invalide")
    async with _locked(), _as(email):
        res = await run(PLUMESIGN, "account", "set-team", team_id, env=env(), timeout=90)
    if not res.ok:
        raise _failure(email, "choix de l'équipe", res)
    _forget_cert_expiry(email)   # le certificat dépend de l'équipe : la date affichée n'est plus la bonne
    db.log(f"Équipe {team_id} choisie pour {email} : les prochaines signatures l'utilisent")


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
    Renvoie True si plumesign a dû révoquer un certificat d'une autre application ou installation
    (Xcode, SideStore, un autre tvsign) : les certificats morts ou ceux de cette installation ne comptent pas."""
    tmp = out.with_name(out.stem + ".tmp.ipa")
    tmp.unlink(missing_ok=True)
    try:
        async with _locked(), _as(email):
            e = env()
            e["RUST_LOG"] = "warn"
            res = await run(PLUMESIGN, "sign", "-p", ipa, "--apple-id", "-o", tmp, env=e, timeout=600)
        if not res.ok or not tmp.exists():
            sweep_stage()
            raise _failure(email, "signature", res)
        replace_retry(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    _set_status(email, True, "session valide")
    expiry = cert_expiry(res.output)
    if expiry:
        settings.raw_set(_CERT_KEY + email, expiry)
    return FOREIGN_REVOKED in res.output


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
