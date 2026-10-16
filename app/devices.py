"""Apple TV : découverte, appairage par PIN, connexion et installation sans fil, via plumesign.

Tout passe par `plumesign pair …` (tunnel RemotePairing en espace utilisateur) : ni
pymobiledevice3, ni interface TUN, ni droits administrateur.

Les appareils sont ciblés par l'UDID de leur appairage (`-i <UDID>`) : l'identifiant annoncé en
mDNS change à chaque connexion et n'est valable qu'un instant. La TV est reconnue sur le réseau
par le contrôle de son authTag contre l'appairage stocké ; plumesign ne se rabat jamais sur un
nouvel appairage (aucun PIN n'apparaît sur une TV qui n'est pas la nôtre).
"""
import asyncio
import ipaddress
import json
import re
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path

from . import db, ipa, plume
from .config import (DEVICE_TIMEOUT, INSTALL_CHECK_HOURS, INSTALL_TIMEOUT, PAIR_HOST, PLUMESIGN,
                     UNICAST_HINT)
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


# Appelées (device_id) quand une Apple TV vient d'être (ré)appairée : enregistrées par renew.
ON_PAIRED: list = []
class Busy(RuntimeError):
    """Une installation tient la connexion à cette Apple TV (un appel de l'interface n'attend pas)."""


class NotInstalled(RuntimeError):
    """L'installation s'est terminée mais l'app n'apparaît pas dans la liste des apps de la TV."""


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
        if not _HOSTNAME.match(value):
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
                      pairing_lost=0, legacy_pairing=0, last_error=None, last_ip=ip)
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


# ---------- commandes sans fil ----------

def _failed(device: dict, message: str, lost: bool = False) -> None:
    fields = dict(last_error=message, last_error_at=time.time())
    if lost:
        fields["pairing_lost"] = 1     # ne se retire qu'au succès ou après un nouvel appairage, pas à une autre panne
    db.update("devices", device["id"], **fields)


def _target(device: dict, hint: str | None = None) -> list[str]:
    if device.get("legacy_pairing") or not device.get("pairing_udid"):
        msg = (f"appairage perdu avec « {device['name']} » : appairage d'une ancienne version de tvsign, "
               "réappaire-la une fois depuis tvsign (Apple TV › Appairer)")
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
                    on_line=None, on_timeout=None, use_hint: bool = True, quiet: bool = False) -> Result:
    """Lance `plumesign pair <command> -i <UDID> …` et traduit les codes de sortie.
    on_timeout() fournit le message d'un délai dépassé (contexte de l'appelant).
    quiet : une TV injoignable (en veille) ne laisse pas d'erreur sur sa fiche (contrôles périodiques)."""
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
                                   on_line=on_line, on_timeout=on_timeout, use_hint=False, quiet=quiet)
        # pas une perte d'appairage : plumesign trop ancien (TVSIGN_PLUMESIGN vers un autre binaire ?)
        raise OldPlumesign(f"plumesign trop ancien pour « pair {command} » : {res.tail(1)}")
    if res.code == EXIT_PAIRING_LOST:
        msg = (f"appairage perdu avec « {device['name']} » : réappaire-la depuis tvsign "
               f"(TV réinitialisée, ou tvsign retiré dans Réglages > Télécommandes et appareils) [{why}]")
        _failed(device, msg, lost=True)
        raise PairingLost(msg)
    if res.code == EXIT_NOT_FOUND:
        msg = f"Apple TV « {device['name']} » introuvable sur le réseau (éteinte ou en veille ?) [{why}]"
        if not quiet:
            _failed(device, "introuvable sur le réseau : " + why)
        raise Unreachable(msg)
    if res.code == -1:
        msg = on_timeout() if on_timeout else f"délai dépassé ({command})"
        if not quiet:
            _failed(device, msg)
        raise Unreachable(f"Apple TV « {device['name']} » : {msg}")
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
    fields = {k: v for k, v in dict(
        udid=info.get("UniqueDeviceID"),
        model=info.get("ProductType"),
        os_version=info.get("ProductVersion"),
        last_ip=info.get("ip"),
        last_port=info.get("port"),
    ).items() if v}
    db.update("devices", device_id, **fields)
    if info.get("udid_matches_pairing") is False:
        db.log(f"« {device['name']} » : UDID matériel {info.get('UniqueDeviceID')} différent de "
               f"l'UDID d'appairage {device['pairing_udid']}", "warn")
    return info


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
    ancien : on ne présume rien), NotInstalled si la TV répond sans l'app (confirmé deux fois)."""
    if not bundle_id:
        return None
    for attempt in range(2):
        try:
            apps = await installed_apps(device)
        except Exception as e:  # noqa: BLE001
            db.log(f"« {device['name']} » : liste des apps illisible, installation non vérifiée ({e})", "warn")
            return None
        if _has_bundle(apps, bundle_id):
            return time.time()
        if attempt == 0:
            await asyncio.sleep(3)     # installd peut mettre un instant à publier l'app
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
                res = await _wireless(device, "install", package, "--stall", "60", timeout=budget,
                                      on_line=line, on_timeout=on_timeout)
            except OldPlumesign:
                # plumesign sans --stall : installation sans détection de l'attente
                res = await _wireless(device, "install", package, timeout=budget, on_line=line,
                                      on_timeout=on_timeout)
            if "INSTALLED" not in res.lines():
                kind = ContentError if is_content_error(res.output + " " + res.err) else RuntimeError
                raise kind(f"installation de {package.name} : {res.tail()}")
