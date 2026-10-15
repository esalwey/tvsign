"""Apple TV : découverte, appairage par PIN, connexion et installation sans fil, via plumesign.

Tout passe par `plumesign pair …` (tunnel RemotePairing en espace utilisateur) : ni
pymobiledevice3, ni interface TUN, ni droits administrateur.

Les appareils sont ciblés par l'UDID de leur appairage (`-i <UDID>`) : l'identifiant annoncé en
mDNS change à chaque connexion et n'est valable qu'un instant. La TV est reconnue sur le réseau
par le contrôle de son authTag contre l'appairage stocké ; plumesign ne se rabat jamais sur un
nouvel appairage (aucun PIN n'apparaît sur une TV qui n'est pas la nôtre).
"""
import asyncio
import json
import re
import time
from pathlib import Path

from . import db, plume
from .config import DEVICE_TIMEOUT, INSTALL_TIMEOUT, PAIR_HOST, PLUMESIGN
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
        db.execute("INSERT INTO devices (name, pair_identifier, pairing_udid, udid, model, pair_host, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                   rec["name"] or "Apple TV", rec["udid"], rec["udid"], rec["udid"], rec["model"] or None,
                   CLI_HOST, time.time())
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
                      pairing_lost=0, legacy_pairing=0, last_error=None, last_ip=ip)
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
                    on_line=None, on_timeout=None) -> Result:
    """Lance `plumesign pair <command> -i <UDID> …` et traduit les codes de sortie.
    on_timeout() fournit le message d'un délai dépassé (contexte de l'appelant)."""
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
    if res.code == EXIT_PAIRING_LOST:
        msg = (f"appairage perdu avec « {device['name']} » : réappaire-la depuis tvsign "
               f"(TV réinitialisée, ou tvsign retiré dans Réglages > Télécommandes et appareils) [{why}]")
        _failed(device, msg, lost=True)
        raise PairingLost(msg)
    if res.code == EXIT_NOT_FOUND:
        msg = f"Apple TV « {device['name']} » introuvable sur le réseau (éteinte ou en veille ?) [{why}]"
        _failed(device, "introuvable sur le réseau : " + why)
        raise Unreachable(msg)
    if res.code == -1:
        msg = on_timeout() if on_timeout else f"délai dépassé ({command})"
        _failed(device, msg)
        raise Unreachable(f"Apple TV « {device['name']} » : {msg}")
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
    fields = {k: v for k, v in dict(
        udid=info.get("UniqueDeviceID"),
        model=info.get("ProductType"),
        os_version=info.get("ProductVersion"),
        last_ip=info.get("ip"),
        last_port=info.get("port"),
    ).items() if v}
    db.update("devices", device_id, **fields)
    if fields.get("os_version") and device.get("os_version") and fields["os_version"] != device["os_version"]:
        # nouvelle tvOS : une version écartée parce qu'elle exigeait une tvOS plus récente peut convenir maintenant
        db.execute("UPDATE apps SET bad_tag = NULL, bad_key = NULL WHERE device_id = ?", device_id)
    if info.get("udid_matches_pairing") is False:
        db.log(f"« {device['name']} » : UDID matériel {info.get('UniqueDeviceID')} différent de "
               f"l'UDID d'appairage {device['pairing_udid']}", "warn")
    return info


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
                        raise AppInUse(
                            f"installd ne progresse plus depuis {waited / 60:.0f} min sur « {device['name']} », "
                            f"installation abandonnée : {app_name or 'l’app'} est probablement ouverte sur l'Apple TV "
                            "(ferme-la pour terminer la mise à jour), sinon vérifie l'espace de stockage")

            def on_timeout() -> str:
                if st["waiting_since"]:
                    return (f"installation bloquée à {st['percent']} % depuis "
                            f"{(time.time() - st['waiting_since']) / 60:.0f} min (app ouverte sur la TV ?)")
                if st["percent"] is None:
                    return "envoi de l'IPA trop long (Wi-Fi lent ou Apple TV en veille ?)"
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
