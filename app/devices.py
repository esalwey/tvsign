"""Apple TV, iPhone, iPad : découverte, appairage, connexion et installation sans fil, via plumesign.

Tout passe par `plumesign pair …` (tunnel RemotePairing en espace utilisateur) : ni
pymobiledevice3, ni interface TUN, ni droits administrateur.

Apple TV : appairage par le code affiché à l'écran. iPhone / iPad (iOS 17 et plus) : un appairage
une seule fois, soit par USB (`plumesign pair usb`, là où usbmuxd existe), soit en important un
fichier d'appairage fait ailleurs (Docker n'a pas d'USB) ; ensuite tout se fait en Wi-Fi, comme
pour une Apple TV.

Les appareils sont ciblés par l'UDID de leur appairage (`-i <UDID>`) : l'identifiant annoncé en
mDNS change à chaque connexion et n'est valable qu'un instant. La TV est reconnue sur le réseau
par le contrôle de son authTag contre l'appairage stocké ; plumesign ne se rabat jamais sur un
nouvel appairage (aucun PIN n'apparaît sur une TV qui n'est pas la nôtre).
"""
import asyncio
import ipaddress
import json
import os
import plistlib
import re
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from . import db, families, ipa, plume
from .config import (DEVICE_TIMEOUT, INSTALL_CHECK_HOURS, INSTALL_TIMEOUT, PAIR_HOST, PLUMESIGN, TMP_DIR,
                     UNICAST_HINT, WINDOWS)
from .proc import Result, Session, register, run, spawn
from .util import ContentError, is_content_error

# Nom d'hôte de plumesign par défaut : appairages faits hors de tvsign (plumesign en CLI).
CLI_HOST = "plume"

# Codes de sortie de plumesign pair info/check/install/apps.
EXIT_PAIRING_LOST = 2
EXIT_NOT_FOUND = 3

# Erreur d'utilisation de clap (code 2, comme PAIRING_LOST) : plumesign plus ancien que tvsign.
_CLI_ERROR = re.compile(r"unrecognized subcommand|unexpected argument|invalid value .* for", re.I)


class PairingLost(RuntimeError):
    """L'Apple TV ne reconnaît plus notre appairage (ou il n'existe pas) : il faut réappairer."""


class Unreachable(RuntimeError):
    """Apple TV introuvable sur le réseau ou qui ne répond plus."""


class AppInUse(RuntimeError):
    """installd garde l'installation en attente : l'app est ouverte sur l'Apple TV."""


class OldPlumesign(RuntimeError):
    """plumesign ne connaît pas cette commande ou cette option."""


# Appelées (device_id) quand un appareil vient d'être (ré)appairé : enregistrées par renew.
ON_PAIRED: list = []


class Busy(RuntimeError):
    """Une installation tient la connexion à cet appareil (un appel de l'interface n'attend pas)."""


class NotInstalled(RuntimeError):
    """L'installation s'est terminée mais l'app n'apparaît pas dans la liste des apps de l'appareil."""


class DeviceLocked(Unreachable):
    """iPhone / iPad verrouillé depuis son démarrage : installd refuse tant qu'il n'a pas été
    déverrouillé une fois avec son code. Passager, comme un appareil absent du réseau."""


# Messages d'un appareil qui n'a pas été déverrouillé depuis son démarrage (lockdown / installd)
_LOCKED = re.compile(r"PasswordProtected|device is locked|is locked|passcode|"
                     r"not (yet )?(been )?unlocked|before (the )?first unlock|since (boot|restart)", re.I)


def label(device: dict | None) -> str:
    """« Apple TV », « iPhone », « iPad » : le mot à employer dans les messages."""
    return families.label((device or {}).get("family"))


def is_mobile(device: dict | None) -> bool:
    return families.is_mobile((device or {}).get("family"))


# Une connexion à la fois par appareil.
_LOCKS: dict[int, asyncio.Lock] = {}


def lock(device_id: int) -> asyncio.Lock:
    return _LOCKS.setdefault(device_id, asyncio.Lock())


@asynccontextmanager
async def _held(device_id: int, wait: bool = True):
    """Verrou de l'appareil. wait=False (boutons de l'interface) : échoue tout de suite si une
    installation le tient, au lieu de rester suspendu jusqu'à la fin d'une attente de 30 min."""
    lk = lock(device_id)
    if not wait and lk.locked():
        raise Busy("une installation est en cours sur cette Apple TV : réessaie quand elle est terminée")
    async with lk:
        yield


# ---------- adresse saisie à la main (mDNS bloqué) ----------

_HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def clean_host(value: str | None) -> str | None:
    """Adresse IP ou nom d'hôte saisi par l'utilisateur ; None si vide. ValueError si invalide."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.split("%")[0])
    except ValueError:
        # que des chiffres et des points (« 999.1.1.1 ») : une adresse IPv4 ratée, pas un nom d'hôte à résoudre
        if not _HOSTNAME.match(value) or re.fullmatch(r"[\d.]+", value):
            raise ValueError("adresse invalide") from None
        return value
    if ip.is_unspecified or ip.is_loopback or ip.is_multicast:
        raise ValueError("adresse invalide")
    return value


async def resolve_host(value: str | None) -> str | None:
    """IP à partir d'une adresse saisie (IP telle quelle, nom résolu par le DNS) ; None si impossible."""
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.split("%")[0]))     # sans zone (« %eth0 ») : plumesign n'accepte qu'une IP
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(value, None, type=socket.SOCK_STREAM)
    except OSError:
        return None
    ips = sorted({i[4][0] for i in infos}, key=lambda a: (":" in a, a))   # IPv4 d'abord
    return ips[0] if ips else None


async def hint_ip(device: dict) -> str | None:
    """Adresse où interroger la TV directement : celle saisie à la main, sinon la dernière connue."""
    if not UNICAST_HINT:
        return None
    ip = (await resolve_host(device.get("host_hint"))) or device.get("last_ip")
    if not ip:
        return None
    base, _, zone = ip.partition("%")
    try:
        text = str(ipaddress.ip_address(base))
    except ValueError:
        return None
    # plumesign n'accepte qu'un index d'interface numérique (fe80::1%12), pas un nom de zone (« %eth0 »)
    return f"{text}%{zone}" if zone.isdigit() and ":" in text else text


# ---------- store d'appairage plumesign ----------

_RECORD = re.compile(r"^(?P<name>.*) \((?P<model>[^()]*)\) \[(?P<udid>[^\[\]]+)\]$")


async def stored_pairings() -> list[dict]:
    """Appairages présents dans le store plumesign (lecture locale, aucune connexion réseau)."""
    e = plume.env()
    e["RUST_LOG"] = "warn"
    res = await run(PLUMESIGN, "pair", "list", env=e, timeout=30, split=True)
    if not res.ok:
        raise RuntimeError(f"lecture des appairages : {res.tail(2)}")
    out = []
    for line in res.lines():
        m = _RECORD.match(line)
        if m:
            out.append(m.groupdict())
    return out


STORE_MISSING = "appairage absent du store plumesign"


def _store_host(udid: str) -> str:
    """Nom d'hôte sous lequel l'appairage du store a été fait. plumesign range dans le fichier d'appairage un
    identifiant tiré de ce nom (UUID v3 du nom, espace DNS) et demande de présenter le même nom ensuite : on retrouve,
    parmi les noms que tvsign a employés, celui qui redonne cet identifiant, au lieu de supposer « plume ». Sans
    correspondance (appairage d'un autre outil, fichier illisible), le nom par défaut de plumesign."""
    try:
        data = plistlib.loads((plume.STORE / "remotepairing" / f"{udid}.plist").read_bytes())
        found = str(data.get("identifier") or "").lower()
    except Exception:  # noqa: BLE001 - absent, tronqué, pas un plist : le nom par défaut
        return CLI_HOST
    for name in dict.fromkeys((PAIR_HOST, "tvsign", CLI_HOST)):
        if str(uuid.uuid3(uuid.NAMESPACE_DNS, name)).lower() == found:
            return name
    return CLI_HOST


def _reconcile(device: dict, by_udid: dict) -> None:
    """Compare l'appairage d'une TV au store plumesign : absent = perdu (à réappairer), de nouveau
    présent (sauvegarde restaurée, fichier remis) = le drapeau posé par ce contrôle est retiré."""
    present = device["pairing_udid"].lower() in by_udid
    if not present and not device["pairing_lost"]:
        db.update("devices", device["id"], pairing_lost=1, last_error=STORE_MISSING, last_error_at=time.time())
    elif present and device["pairing_lost"] and device["last_error"] == STORE_MISSING:
        db.update("devices", device["id"], pairing_lost=0, last_error=None)


async def check_store() -> None:
    """Détecte un appairage disparu du store en cours de route (lecture locale, aucun accès réseau),
    sans attendre qu'un renouvellement tombe dessus."""
    try:
        records = await stored_pairings()
    except Exception:  # noqa: BLE001 - plumesign absent : le diagnostic le dira
        return
    by_udid = {r["udid"].lower(): r for r in records}
    for d in db.rows("SELECT * FROM devices WHERE pairing_udid IS NOT NULL AND legacy_pairing = 0"):
        _reconcile(d, by_udid)


async def sync_store() -> None:
    """Au démarrage : rattache les appairages plumesign aux Apple TV connues, et signale celles
    dont l'appairage date de l'ancienne version (pymobiledevice3), à refaire une fois."""
    try:
        records = await stored_pairings()
    except Exception as e:  # noqa: BLE001 - plumesign absent : le diagnostic le dira
        db.log(f"appairages plumesign illisibles : {e}", "warn")
        return
    by_udid = {r["udid"].lower(): r for r in records}
    for d in db.rows("SELECT * FROM devices"):
        if d["pairing_udid"]:
            _reconcile(d, by_udid)
            continue
        rec = by_udid.get((d["udid"] or "").lower())
        if rec:
            # appairage plumesign déjà présent pour cette TV (en ligne de commande, ou par tvsign avant une base
            # perdue) : adopté, avec le nom d'hôte sous lequel il a été fait
            db.update("devices", d["id"], pairing_udid=rec["udid"], pair_host=_store_host(rec["udid"]),
                      legacy_pairing=0, pairing_lost=0)
            db.log(f"Apple TV « {d['name']} » : appairage plumesign existant repris")
        elif not d["legacy_pairing"]:
            db.update("devices", d["id"], legacy_pairing=1)
            db.log(f"Apple TV « {d['name']} » : appairage d'une ancienne version de tvsign, "
                   "à refaire une fois (Appareils › Appairer)", "warn")
    known = {(d["pairing_udid"] or "").lower() for d in db.rows("SELECT pairing_udid FROM devices")}
    for udid, rec in by_udid.items():
        if udid in known:
            continue
        if db.row("SELECT id FROM devices WHERE lower(pair_identifier) = ? OR lower(udid) = ?", udid, udid):
            continue
        family = families.family_of(rec["model"]) or "tvos"
        db.execute("INSERT INTO devices (name, pair_identifier, pairing_udid, udid, model, pair_host, created_at, family) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   rec["name"] or families.label(family), rec["udid"], rec["udid"], rec["udid"], rec["model"] or None,
                   _store_host(rec["udid"]), time.time(), family)
        db.log(f"Appairage existant repris : {rec['name']} ({rec['udid']})")


async def forget(device: dict) -> None:
    """Supprime l'appairage local (la TV garde l'entrée dans ses réglages ; sans effet sur elle)."""
    if device.get("pairing_udid"):
        e = plume.env()
        e["RUST_LOG"] = "warn"
        try:
            res = await run(PLUMESIGN, "pair", "forget", "-u", device["pairing_udid"], env=e, timeout=30, split=True)
        except (OSError, RuntimeError) as err:    # plumesign absent, bloqué, non exécutable
            raise RuntimeError(f"oubli de l'appairage de « {device['name']} » impossible : {err}") from None
        # « Error: No stored pairing for X » est écrit sur stderr (sortie 1) : déjà absent du store,
        # rien à oublier — sinon une TV dont l'appairage a disparu ne pourrait plus être retirée
        if not res.ok and "No stored pairing" not in f"{res.output}\n{res.err}":
            raise RuntimeError(f"oubli de l'appairage de « {device['name']} » impossible : {res.tail(2)}")


# ---------- découverte ----------

async def discover(host: str | None = None) -> dict:
    """Apple TV visibles : celles qui proposent l'appairage, et celles déjà appairées avec nous.
    Lecture seule (mDNS) : aucune connexion n'est ouverte vers les TV.
    host : adresse saisie à la main, interrogée directement quand le multicast est bloqué."""
    ip = None
    if host:
        ip = await resolve_host(clean_host(host))
        if not ip:
            raise RuntimeError(f"adresse introuvable : {host}")
    found = await plume.discover(ip=ip)
    ours = {(d["pairing_udid"] or "").lower(): d for d in db.rows("SELECT * FROM devices")}
    pairable = [{**d, "known": False} for d in found if d["kind"] == "pairable"]
    paired = []
    for d in found:
        if d["kind"] != "paired" or not d.get("udid"):
            continue   # appairée à un autre hôte (iPhone, Mac…) : pas la nôtre
        dev = ours.get(d["udid"].lower())
        paired.append({**d, "device_id": dev["id"] if dev else None})
    return {"pairable": pairable, "paired": paired}


# ---------- appairage ----------

_PAIRED = re.compile(r"^PAIRED (\S+) (\S+)$")


def start_pairing(identifier: str | None, ip: str, port: int, name: str) -> Session:
    """Appairage par PIN avec une TV en mode « App Télécommande et appareils ».
    Seule action qui fait apparaître un code sur une TV : déclenchée uniquement par l'utilisateur."""
    session = register(Session("pair", re.compile(r"^PIN_REQUIRED$")))

    host = PAIR_HOST

    async def done(s: Session) -> None:
        match = next((m for l in reversed(s.lines) if (m := _PAIRED.match(l.strip()))), None)
        if s.user_cancelled and not match:
            db.log(f"Appairage de {name} annulé")      # choix de l'utilisateur, pas une panne
            return
        if not s.exit_ok or not match:
            if s.exit_ok:
                s.state = "failed"
            db.log(f"Appairage de {name} échoué : {' | '.join(s.lines[-2:])}", "error")
            return
        udid = match.group(2)
        existing = db.row("SELECT id FROM devices WHERE lower(pairing_udid) = lower(?) OR lower(udid) = lower(?) "
                          "OR lower(pair_identifier) = lower(?) ORDER BY id LIMIT 1", udid, udid, udid)
        fields = dict(name=name, pair_identifier=udid, pairing_udid=udid, pair_host=host,
                      pairing_lost=0, legacy_pairing=0, last_error=None, last_ip=ip, family="tvos")
        if existing:
            device_id = existing["id"]
            # une ligne « fantôme » (ancien identifiant) pourrait déjà porter cet UDID en clé
            db.execute("DELETE FROM devices WHERE id != ? AND pair_identifier = ?", device_id, udid)
            db.update("devices", device_id, **fields)
        else:
            device_id = db.execute(
                "INSERT INTO devices (name, pair_identifier, pairing_udid, udid, pair_host, last_ip, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", name, udid, udid, udid, host, ip, time.time())
        s.result["device_id"] = device_id
        s.result["udid"] = udid
        db.log(f"Apple TV « {name} » appairée")
        # Complète modèle / version (la TV peut ne pas s'être encore ré-annoncée ou être passée en veille) :
        # quelques essais espacés, l'UDID matériel confirmé en dépend (voir _renew)
        for attempt, pause in enumerate((0, 3, 8)):
            if pause:
                await asyncio.sleep(pause)
            try:
                await refresh_info(device_id)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    db.log(f"Infos de {name} non récupérées pour l'instant : {e}", "warn")
        for hook in ON_PAIRED:
            try:
                hook(device_id)
            except Exception as e:  # noqa: BLE001 - accessoire
                db.log(f"reprise après appairage de {name} : {e}", "warn")

    args = ["pair", "--ip", ip, "--port", port, "--host", host]
    if identifier:
        args += ["--identifier", identifier]
    env = plume.env()
    spawn(session.start(PLUMESIGN, *args, env=env, on_exit=done))
    return session


# ---------- iPhone / iPad : appairage USB ou import d'un fichier ----------

def usb_pairing_available() -> bool:
    """usbmuxd est-il là ? Windows (Apple Mobile Device Service) et macOS l'ont ; sous Linux, le
    socket de usbmuxd, absent du conteneur Docker (pas d'USB, c'est voulu : voir l'import)."""
    if WINDOWS or sys.platform == "darwin":
        return True
    return Path("/var/run/usbmuxd").exists() or bool(os.environ.get("USBMUXD_SOCKET_ADDRESS"))


def _save_paired(name: str, udid: str, host: str, model: str | None = None, family: str | None = None,
                 os_version: str | None = None, ip: str | None = None, rename: bool = True) -> int:
    """Enregistre (ou met à jour) l'appareil dont l'appairage vient d'entrer dans le store plumesign."""
    family = family or families.family_of(model) or "ios"
    existing = db.row("SELECT id FROM devices WHERE lower(pairing_udid) = lower(?) OR lower(udid) = lower(?) "
                      "OR lower(pair_identifier) = lower(?) ORDER BY id LIMIT 1", udid, udid, udid)
    fields = dict(pair_identifier=udid, pairing_udid=udid, udid=udid, pair_host=host, pairing_lost=0,
                  legacy_pairing=0, last_error=None, family=family)
    for key, value in (("model", model), ("os_version", os_version), ("last_ip", ip)):
        if value:
            fields[key] = value
    if existing:
        if rename:
            fields["name"] = name
        # une ligne « fantôme » (ancien identifiant) pourrait déjà porter cet UDID en clé
        db.execute("DELETE FROM devices WHERE id != ? AND pair_identifier = ?", existing["id"], udid)
        db.update("devices", existing["id"], **fields)
        return existing["id"]
    return db.execute(
        "INSERT INTO devices (name, pair_identifier, pairing_udid, udid, model, os_version, last_ip, pair_host, "
        "family, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        name, udid, udid, udid, model, os_version, ip, host, family, time.time())


# UDID : commence par un caractère alphanumérique (plumesign refuse une valeur qui commence par « - »)
_UDID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{7,63}")
_MODEL_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z,._ -]{0,31}")
MAX_NAME = 64


def valid_udid(udid: str | None) -> bool:
    return bool(udid) and _UDID_RE.fullmatch(udid) is not None


def _identity_line(lines: list[str]) -> dict:
    """Ligne JSON d'identité que plumesign affiche après l'appairage. On la reconnaît à son contenu,
    pas à l'ordre des clés (serde_json les trie par ordre alphabétique)."""
    found: dict = {}
    for l in lines:
        l = l.strip()
        if l.startswith("{") and "UniqueDeviceID" in l:
            try:
                data = json.loads(l)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                found = data
    return found


async def _usb_pairing_done(s: Session) -> None:
    """Fin de `plumesign pair usb`. Appelée par la session alors qu'elle est encore « finishing » (elle ne passe à
    « done » qu'après ce travail de fin) : le succès se lit donc dans exit_ok, comme pour l'appairage d'une TV."""
    match = next((m for l in reversed(s.lines) if (m := _PAIRED.match(l.strip()))), None)
    if s.user_cancelled and not match:
        db.log("Appairage USB annulé")           # choix de l'utilisateur, pas une panne
        return
    if not s.exit_ok or not match:
        if s.exit_ok:
            s.state = "failed"        # sorti en 0 sans dire quel appareil : la session ne peut pas finir « done »
        db.log(f"Appairage USB échoué : {' | '.join(s.lines[-2:])}", "error")
        return
    found = _identity_line(s.lines)
    paired_udid = match.group(2)
    model = found.get("ProductType")
    family = found.get("DeviceFamily") or families.family_of(model) or "ios"
    name = found.get("DeviceName") or families.label(family)
    device_id = _save_paired(name, paired_udid, PAIR_HOST, model, family, found.get("ProductVersion"),
                             rename=False)
    s.result.update(device_id=device_id, udid=paired_udid, family=family)
    db.log(f"{families.label(family)} « {name} » appairé en USB")
    try:
        await refresh_info(device_id)
    except Exception as e:  # noqa: BLE001 - l'appareil n'est peut-être pas encore joignable en Wi-Fi
        s.result["info_error"] = str(e)
        db.log(f"Infos de {name} non récupérées pour l'instant : {e}", "warn")


def start_usb_pairing(udid: str | None = None) -> Session:
    """Appairage d'un iPhone / iPad branché en USB sur la machine qui fait tourner tvsign
    (`plumesign pair usb`). Une seule fois : ensuite tout se passe en Wi-Fi."""
    session = register(Session("pair", re.compile(r"^\0$")))      # aucune saisie attendue
    args = ["pair", "usb", "--host", PAIR_HOST] + (["-u", udid] if udid else [])
    spawn(session.start(PLUMESIGN, *args, env=plume.env(), on_exit=_usb_pairing_done))
    return session


async def import_pairing(data: bytes, filename: str | None, udid: str | None, name: str | None,
                         model: str | None = None, family: str | None = None) -> dict:
    """Importe un fichier d'appairage fait ailleurs (plumesign pair usb sur un autre ordinateur,
    Impactor) : plumesign vérifie qu'il est lisible puis le range dans son store.
    Lève ValueError("udid_required" | "invalid_udid" | "invalid_pairing_file") pour une entrée refusée."""
    udid = (udid or "").strip() or families.udid_from_name(filename)
    if not udid:
        raise ValueError("udid_required")
    if not valid_udid(udid):
        raise ValueError("invalid_udid")
    if not data or len(data) > 64 * 1024:
        raise ValueError("invalid_pairing_file")
    model = (model or "").strip() or None
    if model and not _MODEL_RE.fullmatch(model):
        raise ValueError("invalid_model")
    family = (family or "").strip() or None
    if family and family not in families.SUPPORTED:
        raise ValueError("invalid_family")
    family = family or families.family_of(model) or "ios"
    name = (name or "").strip() or families.label(family)
    if len(name) > MAX_NAME or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("invalid_name")
    tmp = TMP_DIR / f"pairing-{int(time.time() * 1000)}.plist"
    tmp.write_bytes(data)
    try:
        e = plume.env()
        e["RUST_LOG"] = "warn"
        args = ["pair", "import", tmp, "-u", udid, "-n", name, "--host", CLI_HOST]
        if model:
            args += ["-m", model]
        res = await run(PLUMESIGN, *args, env=e, timeout=30, split=True)
    except (OSError, RuntimeError) as err:        # plumesign absent, bloqué par l'antivirus, non exécutable
        raise RuntimeError(f"plumesign introuvable ou inutilisable : {err}") from None
    finally:
        tmp.unlink(missing_ok=True)
    if not res.ok:
        if _CLI_ERROR.search(res.err):
            raise OldPlumesign(f"plumesign trop ancien pour « pair import » : {res.tail(1)}")
        if "Not a valid pairing file" in res.output:
            raise ValueError("invalid_pairing_file")
        raise RuntimeError(f"import de l'appairage : {_error_line(res)}")
    device_id = _save_paired(name, udid, CLI_HOST, model, family)
    db.log(f"{families.label(family)} « {name} » : fichier d'appairage importé")
    out = {"device_id": device_id, "udid": udid, "family": family, "info_error": None}
    try:
        await refresh_info(device_id)     # confirme que l'appareil répond avec ce fichier
    except Exception as e:  # noqa: BLE001 - hors du Wi-Fi de la maison, par exemple
        out["info_error"] = str(e)
    return out


# ---------- commandes sans fil ----------

def _failed(device: dict, message: str, lost: bool = False) -> None:
    fields = dict(last_error=message, last_error_at=time.time())
    if lost:
        fields["pairing_lost"] = 1     # ne se retire qu'au succès ou après un nouvel appairage, pas à une autre panne
    db.update("devices", device["id"], **fields)


def _target(device: dict, hint: str | None = None) -> list[str]:
    if device.get("legacy_pairing") or not device.get("pairing_udid"):
        msg = (f"appairage perdu avec « {device['name']} » : appairage d'une ancienne version de tvsign, "
               "réappaire-la une fois depuis tvsign (Appareils › Appairer)")
        _failed(device, msg, lost=True)
        raise PairingLost(msg)
    target = ["-i", device["pairing_udid"], "-t", str(DEVICE_TIMEOUT), "--host", device.get("pair_host") or CLI_HOST]
    if hint:
        # requête mDNS directe vers la TV avant la recherche multicast (filtrée sur certains réseaux)
        target += ["--hint-ip", hint]
    return target


def _error_line(res: Result) -> str:
    for line in reversed(res.lines()):
        if line.startswith("ERROR "):
            parts = line.split(" ", 2)
            return parts[2] if len(parts) > 2 else line
    tail = res.tail(2)
    if not tail and res.code not in (0, -1) and (res.code < 0 or res.code in (137, 139)):
        # tué par le système sans rien écrire (SIGKILL : le plus souvent manque de mémoire)
        return f"plumesign a été arrêté par le système (code {res.code}), probablement par manque de mémoire"
    return tail


async def _wireless(device: dict, command: str, *extra, timeout: float = 120, ok_codes=(0,),
                    on_line=None, on_timeout=None, use_hint: bool = True, quiet: bool = False,
                    soft: bool = False) -> Result:
    """Lance `plumesign pair <command> -i <UDID> …` et traduit les codes de sortie.
    on_timeout() fournit le message d'un délai dépassé (contexte de l'appelant).
    quiet : un appareil injoignable (en veille) ne laisse pas d'erreur sur sa fiche (contrôles périodiques).
    soft : une commande annexe (mode développeur) dont l'échec ordinaire ne doit pas être affiché
    comme une panne de l'appareil."""
    hint = await hint_ip(device) if use_hint else None
    target = _target(device, hint)
    e = plume.env()
    res = await run(PLUMESIGN, "pair", command, *target, *extra, env=e, timeout=timeout, split=True,
                    on_line=on_line)
    if res.code in ok_codes:
        db.update("devices", device["id"], last_seen=time.time(), last_error=None, pairing_lost=0)
        return res
    why = _error_line(res)
    if res.code == EXIT_PAIRING_LOST and _CLI_ERROR.search(res.err):
        if hint and "hint-ip" in res.err:
            # plumesign plus ancien que tvsign : sans la requête dirigée, par multicast seul
            return await _wireless(device, command, *extra, timeout=timeout, ok_codes=ok_codes,
                                   on_line=on_line, on_timeout=on_timeout, use_hint=False, quiet=quiet, soft=soft)
        # pas une perte d'appairage : plumesign trop ancien (TVSIGN_PLUMESIGN vers un autre binaire ?)
        raise OldPlumesign(f"plumesign trop ancien pour « pair {command} » : {res.tail(1)}")
    mobile = is_mobile(device)
    if res.code == EXIT_PAIRING_LOST:
        if mobile:
            msg = (f"appairage perdu avec « {device['name']} » : refais l'appairage (USB, ou importe un nouveau "
                   f"fichier d'appairage) [{why}]")
        else:
            msg = (f"appairage perdu avec « {device['name']} » : réappaire-la depuis tvsign "
                   f"(TV réinitialisée, ou tvsign retiré dans Réglages > Télécommandes et appareils) [{why}]")
        _failed(device, msg, lost=True)
        raise PairingLost(msg)
    if res.code == EXIT_NOT_FOUND:
        if mobile:
            # un téléphone qui n'est pas là est normal : hors du Wi-Fi, en veille, en économie d'énergie
            msg = (f"{label(device)} « {device['name']} » introuvable sur le réseau (pas à la maison, Wi-Fi coupé, "
                   f"verrouillé ou en veille ?) [{why}]")
        else:
            msg = f"Apple TV « {device['name']} » introuvable sur le réseau (éteinte ou en veille ?) [{why}]"
        if not quiet:
            _failed(device, "introuvable sur le réseau : " + why)
        raise Unreachable(msg)
    if res.code == -1:
        msg = on_timeout() if on_timeout else f"délai dépassé ({command})"
        if not quiet:
            _failed(device, msg)
        raise Unreachable(f"{label(device)} « {device['name']} » : {msg}")
    if mobile and _LOCKED.search(why):
        msg = (f"{label(device)} « {device['name']} » verrouillé depuis son démarrage : déverrouille-le une fois "
               f"avec son code [{why}]")
        if not quiet:
            _failed(device, msg)
        raise DeviceLocked(msg)
    if not soft:
        _failed(device, f"{command} : {why}")
    if command == "install" and is_content_error(res.output + " " + res.err):
        raise ContentError(f"{command} : {why}")      # paquet refusé par installd : en cause, l'IPA
    raise RuntimeError(f"{command} : {why}")


async def check_reachable(device: dict, wait: bool = True) -> None:
    """Tunnel complet + RSD puis fermeture : échoue vite si l'Apple TV est injoignable,
    avant de solliciter Apple pour signer."""
    async with _held(device["id"], wait):
        res = await _wireless(device, "check", timeout=90)
    for line in res.lines():
        parts = line.split()
        if len(parts) == 5 and parts[0] == "REACHABLE":
            db.update("devices", device["id"], last_ip=parts[3], last_port=int(parts[4]))


async def refresh_info(device_id: int, wait: bool = True) -> dict:
    device = db.row("SELECT * FROM devices WHERE id = ?", device_id)
    if not device:
        raise RuntimeError("Apple TV inconnue")
    async with _held(device_id, wait):
        res = await _wireless(device, "info", "--json", timeout=90)
    line = next((l for l in res.lines() if l.startswith("{")), None)
    if not line:
        raise RuntimeError(f"réponse inattendue de plumesign : {res.tail(2)}")
    info = json.loads(line)
    fam = info.get("DeviceFamily") or families.family_of(info.get("ProductType"), info.get("DeviceClass"))
    fields = {k: v for k, v in dict(
        udid=info.get("UniqueDeviceID"),
        model=info.get("ProductType"),
        os_version=info.get("ProductVersion"),
        last_ip=info.get("ip"),
        last_port=info.get("port"),
        family=fam,
    ).items() if v}
    db.update("devices", device_id, **fields)
    major = families.version_tuple(info.get("ProductVersion"))[:1]
    if families.is_mobile(fam) and major and major[0] < 17:
        db.log(f"« {device['name']} » est en {info.get('ProductVersion')} : le renouvellement sans fil demande "
               "iOS / iPadOS 17 ou plus récent", "warn")
    if families.is_mobile(fam or device.get("family")):
        try:        # mode développeur : information en plus, jamais une raison d'échouer
            await check_devmode({**device, "family": fam or device.get("family")}, wait=wait)
        except Exception:  # noqa: BLE001
            pass
    if info.get("udid_matches_pairing") is False:
        db.log(f"« {device['name']} » : UDID matériel {info.get('UniqueDeviceID')} différent de "
               f"l'UDID d'appairage {device['pairing_udid']}", "warn")
    return info


_DEVMODE = re.compile(r"^DEVMODE (on|off)$")


async def check_devmode(device: dict, wait: bool = True) -> bool | None:
    """Mode développeur de l'iPhone / iPad (iOS 16+) : sans lui, une app signée s'installe mais ne se
    lance pas. True / False, mémorisé ; plumesign trop ancien : None, sans bloquer."""
    try:
        async with _held(device["id"], wait):
            res = await _wireless(device, "devmode", timeout=60, soft=True)
    except OldPlumesign:
        return None
    on = next((m.group(1) == "on" for l in res.lines() if (m := _DEVMODE.match(l))), None)
    if on is not None:
        db.update("devices", device["id"], dev_mode=int(on), dev_mode_at=time.time())
    return on


async def installed_apps(device: dict, bundle_id: str | None = None, wait: bool = True,
                         quiet: bool = False) -> list[dict]:
    extra = ["--json"] + (["-b", bundle_id] if bundle_id else [])
    async with _held(device["id"], wait):
        res = await _wireless(device, "apps", *extra, timeout=90, quiet=quiet)
    out = []
    for line in res.lines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


# ---------- l'app est-elle bien sur la TV ? ----------

def _has_bundle(apps: list[dict], bundle_id: str) -> bool:
    return any((a.get("bundle_id") or "").lower() == bundle_id.lower() for a in apps)


def _listed(apps: list[dict]) -> list[dict]:
    """Les entrées exploitables d'une liste d'apps. Une liste sans aucune n'est pas crédible (journal parasite,
    TV à peine réveillée) : elle ne prouve l'absence de rien."""
    return [x for x in apps if isinstance(x, dict) and x.get("bundle_id")]


def signed_bundle(app_dir: Path, app: dict) -> str | None:
    """Bundle ID réellement installé : celui de la dernière signature (il change avec l'équipe Apple),
    sinon celui noté à la dernière vérification."""
    signed = app_dir / "signed.ipa"
    try:
        if signed.exists():
            return ipa.app_info(signed)["bundle_id"] or app.get("installed_bundle")
    except Exception:  # noqa: BLE001 - fichier partiel ou corrompu
        pass
    return app.get("installed_bundle")


def is_on_tv(app: dict, apps: list[dict], app_dir: Path) -> bool:
    """L'app figure dans la liste des apps de la TV : sous l'identifiant de sa dernière signature, ou, faute de
    signature, sous celui de l'IPA (éventuellement suffixé de l'identifiant d'équipe Apple :
    com.exemple.app.ABCDE12345)."""
    signed = signed_bundle(app_dir, app)
    if signed and _has_bundle(apps, signed):
        return True
    own = (app.get("bundle_id") or "").lower()
    if not own:
        return False
    return any((x.get("bundle_id") or "").lower() == own
               or (((x.get("bundle_id") or "").lower().startswith(own + "."))
                   and re.fullmatch(r"[0-9a-z]{10}", (x.get("bundle_id") or "").lower()[len(own) + 1:]) is not None)
               for x in apps)


def record_presence(app: dict, apps: list[dict], app_dir: Path, now: float) -> bool:
    """Contrôle à la demande (rien n'est signé ni installé) : note sur la fiche si l'app est sur la TV. Vue
    présente, c'est la même constatation que celle d'après une installation (verified_at)."""
    present = is_on_tv(app, apps, app_dir)
    fields = dict(on_tv=int(present), on_tv_checked_at=now)
    if present:
        fields["verified_at"] = now
        bundle = signed_bundle(app_dir, app)
        if bundle:
            fields["installed_bundle"] = bundle
    db.update("apps", app["id"], **fields)
    return present


async def confirm_installed(device: dict, bundle_id: str | None) -> float | None:
    """Vérifie dans la liste des apps de la TV que l'app y est vraiment après l'installation.
    Renvoie l'heure de la vérification, None si elle n'a pas pu se faire (TV qui s'endort, plumesign
    ancien : on ne présume rien), NotInstalled si la TV répond sans l'app (confirmé deux fois). Une liste vide
    aux deux essais n'est pas une réponse crédible (comme pour /verify) : installation non vérifiée, sans erreur."""
    if not bundle_id:
        return None
    credible = False
    for attempt in range(2):
        try:
            apps = await installed_apps(device)
        except Exception as e:  # noqa: BLE001
            db.log(f"« {device['name']} » : liste des apps illisible, installation non vérifiée ({e})", "warn")
            return None
        credible = credible or bool(_listed(apps))
        if _has_bundle(apps, bundle_id):
            return time.time()
        if attempt == 0:
            await asyncio.sleep(3)     # installd peut mettre un instant à publier l'app
    if not credible:
        db.log(f"« {device['name']} » : la liste de ses apps est vide, installation non vérifiée", "warn")
        return None
    raise NotInstalled(f"l'installation s'est terminée mais « {bundle_id} » n'apparaît pas dans les apps de "
                       f"« {device['name']} » : elle sera réinstallée")


_AUDIT_NEXT: dict[int, float] = {}      # appareil -> prochain contrôle périodique


async def audit_installed(now: float | None = None) -> None:
    """Contrôle périodique : une app que la TV n'a plus (désinstallée à la main, TV réinitialisée,
    installd qui l'a retirée) ne doit pas rester « valide » dans tvsign. Elle repasse en échec et
    est réinstallée tout de suite. Une TV injoignable est simplement revue plus tard."""
    if INSTALL_CHECK_HOURS <= 0:
        return
    from . import renew      # import tardif : renew importe devices
    now = now or time.time()
    for device in db.rows("SELECT * FROM devices WHERE pairing_lost = 0 AND legacy_pairing = 0"):
        if _AUDIT_NEXT.get(device["id"], 0) > now or lock(device["id"]).locked():
            continue
        mine = [a for a in db.rows("SELECT * FROM apps WHERE device_id = ? AND enabled = 1 AND status = 'ok' "
                                   "AND signed_at IS NOT NULL", device["id"])
                if a["id"] not in renew._RUNNING and now - a["signed_at"] > 600]
        if not mine:
            continue
        try:
            apps = await installed_apps(device, wait=False, quiet=True)
        except Exception:  # noqa: BLE001 - TV en veille, occupée… : on réessaie dans une heure, sans erreur sur la fiche
            _AUDIT_NEXT[device["id"]] = now + 3600
            continue
        if not _listed(apps):
            # liste vide (TV à peine réveillée, journal parasite) : pas une preuve que les apps ont disparu, et en
            # réinstaller tout de suite consommerait des App ID pour rien
            _AUDIT_NEXT[device["id"]] = now + 3600
            continue
        _AUDIT_NEXT[device["id"]] = now + INSTALL_CHECK_HOURS * 3600
        for a in mine:
            fresh = db.row("SELECT * FROM apps WHERE id = ?", a["id"])
            if not fresh or fresh["status"] != "ok" or fresh["id"] in renew._RUNNING:
                continue
            bundle = signed_bundle(renew.app_dir(a["id"]), fresh)
            if not bundle:
                continue
            if _has_bundle(apps, bundle):
                db.update("apps", a["id"], verified_at=time.time(), installed_bundle=bundle,
                          on_tv=1, on_tv_checked_at=time.time())
                continue
            db.update("apps", a["id"], status="error", verified_at=None, next_attempt_at=None,
                      last_error=f"l'app n'est plus installée sur « {device['name']} » "
                                 "(désinstallée depuis la TV ?) : réinstallation en cours")
            db.log(f"{a['name']} : absente de « {device['name']} », réinstallation", "warn", app_id=a["id"])
            renew.spawn(a["id"], "app absente")


async def install(device: dict, ipas: list[Path], on_event=None, wait_cap: float | None = None,
                  app_name: str | None = None) -> None:
    """Installe une ou plusieurs IPA signées.

    on_event(étape, **infos) suit l'avancement : uploading, installing (percent), waiting
    (percent, since) quand installd ne progresse plus (app ouverte sur la TV : il attend qu'elle
    soit fermée). Au-delà de wait_cap secondes d'attente : AppInUse, l'installation est abandonnée."""
    def event(step: str, **info) -> None:
        if on_event:
            on_event(step, **info)

    async with lock(device["id"]):
        for package in ipas:
            st = {"percent": None, "waiting_since": None}

            def line(text: str) -> None:
                parts = text.split()
                if not parts:
                    return
                if parts[0] == "UPLOADING":
                    event("uploading")
                elif parts[0] == "UPLOADED":
                    st["percent"] = 0
                    event("installing", percent=0)
                elif parts[0] == "PROGRESS" and len(parts) > 1 and parts[1].isdigit():
                    st["percent"], st["waiting_since"] = int(parts[1]), None
                    event("installing", percent=st["percent"])
                elif parts[0] == "WAITING" and len(parts) > 2 and parts[2].isdigit():
                    if st["waiting_since"] is None:
                        st["waiting_since"] = time.time() - int(parts[2])
                    event("waiting", percent=st["percent"], since=st["waiting_since"])
                    waited = max(time.time() - st["waiting_since"], int(parts[2]))
                    if wait_cap and waited >= wait_cap:
                        if is_mobile(device):
                            # iOS remplace une app ouverte : l'attente vient d'ailleurs (appareil verrouillé, stockage…)
                            raise AppInUse(
                                f"l'installation n'avance plus depuis {waited / 60:.0f} min sur « {device['name']} », "
                                "abandonnée : vérifie que l'appareil est déverrouillé, branché, "
                                "et son espace de stockage")
                        raise AppInUse(
                            f"installd ne progresse plus depuis {waited / 60:.0f} min sur « {device['name']} », "
                            f"installation abandonnée : {app_name or 'l’app'} est probablement ouverte sur l'Apple TV "
                            "(ferme-la pour terminer la mise à jour), sinon vérifie l'espace de stockage")

            def on_timeout() -> str:
                if st["waiting_since"]:
                    return (f"installation bloquée à {st['percent']} % depuis "
                            f"{(time.time() - st['waiting_since']) / 60:.0f} min "
                            + ("(appareil verrouillé ou occupé ?)" if is_mobile(device) else "(app ouverte sur la TV ?)"))
                if st["percent"] is None:
                    return ("envoi de l'IPA trop long (Wi-Fi lent ou appareil en veille ?)" if is_mobile(device)
                            else "envoi de l'IPA trop long (Wi-Fi lent ou Apple TV en veille ?)")
                return f"installation trop longue, arrêtée à {st['percent']} %"

            budget = INSTALL_TIMEOUT + (wait_cap or 0)
            try:
                res = await _wireless(device, "install", package, "--stall", "60", timeout=budget,
                                      on_line=line, on_timeout=on_timeout)
            except OldPlumesign:
                # plumesign sans --stall : installation sans détection de l'attente
                res = await _wireless(device, "install", package, timeout=budget, on_line=line,
                                      on_timeout=on_timeout)
            if "INSTALLED" not in res.lines():
                kind = ContentError if is_content_error(res.output + " " + res.err) else RuntimeError
                raise kind(f"installation de {package.name} : {res.tail()}")
