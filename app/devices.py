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
import json
import os
import re
import sys
import time
from pathlib import Path

from . import db, families, plume
from .config import DEVICE_TIMEOUT, INSTALL_TIMEOUT, PAIR_HOST, PLUMESIGN, TMP_DIR, WINDOWS
from .proc import Result, Session, register, run, spawn

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
            if d["pairing_udid"].lower() not in by_udid and not d["pairing_lost"]:
                db.update("devices", d["id"], pairing_lost=1,
                          last_error="appairage absent du store plumesign", last_error_at=time.time())
            continue
        rec = by_udid.get((d["udid"] or "").lower())
        if rec:
            # appairage plumesign déjà présent pour cette TV (fait en ligne de commande) : adopté
            db.update("devices", d["id"], pairing_udid=rec["udid"], pair_host=CLI_HOST,
                      legacy_pairing=0, pairing_lost=0)
            db.log(f"Apple TV « {d['name']} » : appairage plumesign existant repris")
        elif not d["legacy_pairing"]:
            db.update("devices", d["id"], legacy_pairing=1)
            db.log(f"Apple TV « {d['name']} » : appairage d'une ancienne version de tvsign, "
                   "à refaire une fois (Apple TV › Appairer)", "warn")
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
                   CLI_HOST, time.time(), family)
        db.log(f"Appairage existant repris : {rec['name']} ({rec['udid']})")


async def forget(device: dict) -> None:
    """Supprime l'appairage local (la TV garde l'entrée dans ses réglages ; sans effet sur elle)."""
    if device.get("pairing_udid"):
        e = plume.env()
        e["RUST_LOG"] = "warn"
        res = await run(PLUMESIGN, "pair", "forget", "-u", device["pairing_udid"], env=e, timeout=30, split=True)
        if not res.ok and "No stored pairing" not in res.output:    # déjà absent du store : rien à oublier
            raise RuntimeError(f"oubli de l'appairage de « {device['name']} » impossible : {res.tail(2)}")


# ---------- découverte ----------

async def discover() -> dict:
    """Apple TV visibles : celles qui proposent l'appairage, et celles déjà appairées avec nous.
    Lecture seule (mDNS) : aucune connexion n'est ouverte vers les TV."""
    found = await plume.discover()
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

    async def done(s: Session) -> None:
        match = next((m for l in reversed(s.lines) if (m := _PAIRED.match(l.strip()))), None)
        if s.state != "done" or not match:
            if s.state == "done":
                s.state = "failed"
            db.log(f"Appairage de {name} échoué : {' | '.join(s.lines[-2:])}", "error")
            return
        udid = match.group(2)
        existing = db.row("SELECT id FROM devices WHERE lower(pairing_udid) = lower(?) OR lower(udid) = lower(?) "
                          "OR lower(pair_identifier) = lower(?) ORDER BY id LIMIT 1", udid, udid, udid)
        fields = dict(name=name, pair_identifier=udid, pairing_udid=udid, pair_host=PAIR_HOST,
                      pairing_lost=0, legacy_pairing=0, last_error=None, last_ip=ip, family="tvos")
        if existing:
            device_id = existing["id"]
            # une ligne « fantôme » (ancien identifiant) pourrait déjà porter cet UDID en clé
            db.execute("DELETE FROM devices WHERE id != ? AND pair_identifier = ?", device_id, udid)
            db.update("devices", device_id, **fields)
        else:
            device_id = db.execute(
                "INSERT INTO devices (name, pair_identifier, pairing_udid, udid, pair_host, last_ip, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", name, udid, udid, udid, PAIR_HOST, ip, time.time())
        s.result["device_id"] = device_id
        s.result["udid"] = udid
        db.log(f"Apple TV « {name} » appairée")
        # Complète modèle / version (peut échouer si la TV se met en veille).
        try:
            await refresh_info(device_id)
        except Exception as e:  # noqa: BLE001
            db.log(f"Infos de {name} non récupérées pour l'instant : {e}", "warn")

    args = ["pair", "--ip", ip, "--port", port, "--host", PAIR_HOST]
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
    match = next((m for l in reversed(s.lines) if (m := _PAIRED.match(l.strip()))), None)
    if s.state != "done" or not match:
        if s.state == "done":
            s.state = "failed"
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


def _target(device: dict) -> list[str]:
    if device.get("legacy_pairing") or not device.get("pairing_udid"):
        msg = (f"appairage perdu avec « {device['name']} » : appairage d'une ancienne version de tvsign, "
               "réappaire-la une fois depuis tvsign (Apple TV › Appairer)")
        _failed(device, msg, lost=True)
        raise PairingLost(msg)
    return ["-i", device["pairing_udid"], "-t", str(DEVICE_TIMEOUT), "--host", device.get("pair_host") or CLI_HOST]


def _error_line(res: Result) -> str:
    for line in reversed(res.lines()):
        if line.startswith("ERROR "):
            parts = line.split(" ", 2)
            return parts[2] if len(parts) > 2 else line
    return res.tail(2)


async def _wireless(device: dict, command: str, *extra, timeout: float = 120, ok_codes=(0,),
                    on_line=None, on_timeout=None, soft: bool = False) -> Result:
    """Lance `plumesign pair <command> -i <UDID> …` et traduit les codes de sortie.
    on_timeout() fournit le message d'un délai dépassé (contexte de l'appelant).
    soft : une commande annexe (mode développeur) dont l'échec ordinaire ne doit pas être affiché
    comme une panne de l'appareil."""
    target = _target(device)
    e = plume.env()
    res = await run(PLUMESIGN, "pair", command, *target, *extra, env=e, timeout=timeout, split=True,
                    on_line=on_line)
    if res.code in ok_codes:
        db.update("devices", device["id"], last_seen=time.time(), last_error=None, pairing_lost=0)
        return res
    why = _error_line(res)
    if res.code == EXIT_PAIRING_LOST and _CLI_ERROR.search(res.err):
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
        _failed(device, "introuvable sur le réseau : " + why)
        raise Unreachable(msg)
    if res.code == -1:
        msg = on_timeout() if on_timeout else f"délai dépassé ({command})"
        _failed(device, msg)
        raise Unreachable(f"{label(device)} « {device['name']} » : {msg}")
    if mobile and _LOCKED.search(why):
        msg = (f"{label(device)} « {device['name']} » verrouillé depuis son démarrage : déverrouille-le une fois "
               f"avec son code [{why}]")
        _failed(device, msg)
        raise DeviceLocked(msg)
    if not soft:
        _failed(device, f"{command} : {why}")
    raise RuntimeError(f"{command} : {why}")


async def check_reachable(device: dict) -> None:
    """Tunnel complet + RSD puis fermeture : échoue vite si l'Apple TV est injoignable,
    avant de solliciter Apple pour signer."""
    async with lock(device["id"]):
        res = await _wireless(device, "check", timeout=90)
    for line in res.lines():
        parts = line.split()
        if len(parts) == 5 and parts[0] == "REACHABLE":
            db.update("devices", device["id"], last_ip=parts[3], last_port=int(parts[4]))


async def refresh_info(device_id: int) -> dict:
    device = db.row("SELECT * FROM devices WHERE id = ?", device_id)
    if not device:
        raise RuntimeError("Apple TV inconnue")
    async with lock(device_id):
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
            await check_devmode({**device, "family": fam or device.get("family")})
        except Exception:  # noqa: BLE001
            pass
    if info.get("udid_matches_pairing") is False:
        db.log(f"« {device['name']} » : UDID matériel {info.get('UniqueDeviceID')} différent de "
               f"l'UDID d'appairage {device['pairing_udid']}", "warn")
    return info


_DEVMODE = re.compile(r"^DEVMODE (on|off)$")


async def check_devmode(device: dict) -> bool | None:
    """Mode développeur de l'iPhone / iPad (iOS 16+) : sans lui, une app signée s'installe mais ne se
    lance pas. True / False, mémorisé ; plumesign trop ancien : None, sans bloquer."""
    try:
        async with lock(device["id"]):
            res = await _wireless(device, "devmode", timeout=60, soft=True)
    except OldPlumesign:
        return None
    on = next((m.group(1) == "on" for l in res.lines() if (m := _DEVMODE.match(l))), None)
    if on is not None:
        db.update("devices", device["id"], dev_mode=int(on), dev_mode_at=time.time())
    return on


async def installed_apps(device: dict, bundle_id: str | None = None) -> list[dict]:
    extra = ["--json"] + (["-b", bundle_id] if bundle_id else [])
    async with lock(device["id"]):
        res = await _wireless(device, "apps", *extra, timeout=90)
    out = []
    for line in res.lines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


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
        for ipa in ipas:
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
                res = await _wireless(device, "install", ipa, "--stall", "60", timeout=budget,
                                      on_line=line, on_timeout=on_timeout)
            except OldPlumesign:
                # plumesign sans --stall : installation sans détection de l'attente
                res = await _wireless(device, "install", ipa, timeout=budget, on_line=line,
                                      on_timeout=on_timeout)
            if "INSTALLED" not in res.lines():
                raise RuntimeError(f"installation de {ipa.name} : {res.tail()}")
