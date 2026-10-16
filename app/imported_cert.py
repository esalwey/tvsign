"""Signing with an imported certificate: the secondary route, next to the free Apple ID (the default).

The user brings a signing certificate with its private key (a .p12 and its password) and a provisioning
profile (a .mobileprovision), from their own Apple Developer account or from a provider they chose. This
module

  - reads and checks both files before anything is saved (prepare): certificate and profile not expired,
    profile made for this kind of device and for this very device, certificate one of the profile's, app
    identifier of the profile covering the app;
  - keeps them sealed in the data folder, one file per certificate (AES-256-GCM, key in vault.key, owner-only
    permissions). They are not part of a backup unless the user asks for it (backup.py);
  - hands plumesign a short-lived pair of PEM files for one signature (signing_files);
  - says when the certificate or the profile runs out (the app is not renewed every 7 days with it) and
    whether the certificate was revoked: an OCSP query, or the way installd refuses the install.

Nothing here talks to Apple except that optional OCSP query, sent to *.apple.com only.
"""
import base64
import contextlib
import hashlib
import http.client
import json
import os
import plistlib
import re
import secrets
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from xml.parsers.expat import ExpatError

from . import db, ipa
from .config import CERT_DIR, DATA_DIR, TMP_DIR, VERSION
from .util import write_private

try:        # in requirements.txt; without it the rest of tvsign keeps working and the interface says so
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature, InvalidTag
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509 import ocsp
    from cryptography.x509.oid import AuthorityInformationAccessOID, ExtendedKeyUsageOID
    AVAILABLE = True
except ImportError:         # pragma: no cover - exercised only in an environment without the package
    AVAILABLE = False
    InvalidTag = InvalidSignature = ValueError

APPLE_ID, CERTIFICATE = "apple_id", "certificate"
MODES = (APPLE_ID, CERTIFICATE)

MAX_P12 = 256 * 1024
MAX_PROFILE = 512 * 1024
MAX_PASSWORD = 256
EXPIRY_ALERT_DAYS = (7, 1)          # warned this many days before the certificate or the profile runs out
SHORT_VALIDITY_DAYS = 30            # a profile that short is mentioned at import (a free-account style profile)
_MAGIC = b"TVC1"
_KEY_FILE = DATA_DIR / "vault.key"


class CertError(ValueError):
    """A file was refused at import, or a certificate cannot be used for this app. `code` is the stable API
    code ("cert_password_wrong", "cert_profile_expired:2026-10-01"…): the interface translates err_<code>."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CertProblem(RuntimeError):
    """An app set to an imported certificate cannot be signed or installed because of that certificate. The
    message (French, shown on the card) says what to do; retrying changes nothing (renew.failure_kind)."""
    kind = "config"

    def __init__(self, message: str, code: str = "other"):
        super().__init__(message)
        self.code = code


def is_cert(app: dict | None) -> bool:
    """Is this app set to sign with an imported certificate? (Anything else is the Apple ID route.)"""
    return (app or {}).get("sign_mode") == CERTIFICATE


def _need_package() -> None:
    if not AVAILABLE:
        raise CertError("cert_unavailable")


def _day(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "?"


def _ts(value: datetime) -> float:
    """plistlib and the X.509 readers give UTC; a naive date is read as UTC, never as local time."""
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).timestamp()


def norm_udid(value: str | None) -> str:
    """UDID compared without case or dashes (00008110-001A2B3C4D5E6F78 == 00008110001a2b3c4d5e6f78)."""
    return re.sub(r"[^0-9a-f]", "", str(value or "").lower())


# ---------- the provisioning profile ----------

@dataclass(frozen=True)
class Profile:
    name: str
    uuid: str
    team_id: str
    team_name: str
    app_id: str                   # application-identifier entitlement, team prefix included
    pattern: str                  # the same without the prefix: "*", "com.example.*" or "com.example.app"
    platforms: tuple[str, ...]    # as the profile writes them: ("tvOS",), ("iOS",)…
    expires_at: float
    udids: frozenset              # normalized (norm_udid)
    has_device_list: bool
    all_devices: bool             # ProvisionsAllDevices (enterprise)
    cert_ders: tuple              # DeveloperCertificates

    @property
    def wildcard(self) -> bool:
        return self.pattern.endswith("*")


_XML_PLIST = re.compile(rb"<\?xml.*?</plist>", re.S)
_PLATFORM_NAMES = {"ios": "ios", "tvos": "tvos", "xros": "xros", "visionos": "xros", "osx": "macos", "macos": "macos",
                   "watchos": "watchos"}
_FAMILY_PLATFORM = {"tvos": "tvOS", "ios": "iOS", "ipados": "iOS", "visionos": "xrOS"}


def platform_needed(family: str | None) -> str:
    """The profile platform a device of this family needs: tvOS for an Apple TV, iOS for an iPhone or iPad."""
    return _FAMILY_PLATFORM.get(family or "tvos", "tvOS")


def _same_platform(a: str, b: str) -> bool:
    return _PLATFORM_NAMES.get(a.strip().lower(), a.strip().lower()) == _PLATFORM_NAMES.get(b.strip().lower(), b.strip().lower())


def _strings(value) -> list[str]:
    value = [value] if isinstance(value, str) else value
    return [v for v in value if isinstance(v, str)] if isinstance(value, (list, tuple)) else []


def parse_profile(data: bytes) -> Profile:
    """Reads a .mobileprovision (a signed container around an XML property list, or the bare list). The
    signature of the container is not checked here: installd checks it, and only Apple can make a valid one."""
    if not data or len(data) > MAX_PROFILE:
        raise CertError("cert_profile_invalid")
    found = _XML_PLIST.search(data)
    try:
        plist = plistlib.loads(found.group(0) if found else data)
    except (plistlib.InvalidFileException, ExpatError, ValueError, OverflowError, TypeError):
        raise CertError("cert_profile_invalid") from None
    ent = plist.get("Entitlements") if isinstance(plist, dict) else None
    app_id = ent.get("application-identifier") if isinstance(ent, dict) else None
    expires = plist.get("ExpirationDate") if isinstance(plist, dict) else None
    if not isinstance(app_id, str) or not app_id.strip() or not isinstance(expires, datetime):
        raise CertError("cert_profile_invalid")
    team = (_strings(plist.get("TeamIdentifier")) or _strings(plist.get("ApplicationIdentifierPrefix")) or [""])[0]
    team = team or str(ent.get("com.apple.developer.team-identifier") or "")
    app_id = app_id.strip()
    pattern = app_id[len(team) + 1:] if team and app_id.startswith(team + ".") else re.sub(r"^[A-Z0-9]{10}\.", "", app_id)
    platforms = tuple(_strings(plist.get("Platform"))) or ("iOS",)     # profiles older than tvOS name no platform
    devices = plist.get("ProvisionedDevices")
    certs = plist.get("DeveloperCertificates")
    return Profile(
        name=str(plist.get("Name") or plist.get("AppIDName") or "")[:120],
        uuid=str(plist.get("UUID") or ""),
        team_id=team,
        team_name=str(plist.get("TeamName") or "")[:120],
        app_id=app_id,
        pattern=pattern,
        platforms=platforms,
        expires_at=_ts(expires),
        udids=frozenset(norm_udid(u) for u in _strings(devices)) - {""},
        has_device_list=isinstance(devices, list),
        all_devices=plist.get("ProvisionsAllDevices") is True,
        cert_ders=tuple(bytes(c) for c in certs if isinstance(c, (bytes, bytearray))) if isinstance(certs, list) else (),
    )


def pattern_matches(pattern: str, bundle_id: str | None) -> bool:
    """Does the profile's app identifier (without the team) cover this bundle identifier? `*` covers any app,
    `com.example.*` every identifier under that prefix, anything else exactly itself."""
    if not pattern or not bundle_id:
        return False
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return bundle_id.startswith(pattern[:-1])
    return pattern == bundle_id


# ---------- the certificate ----------

@dataclass(frozen=True)
class CertInfo:
    subject: str
    serial: str          # hex, upper case, no leading zeros: the form apps.cert_serial uses
    sha1: str
    not_before: float
    not_after: float
    der: bytes
    key_kind: str = "rsa"


@dataclass
class Material:
    key_pem: bytes
    cert_pem: bytes
    info: CertInfo
    chain: list = field(default_factory=list)       # the other certificates of the .p12 (DER), the issuer among them


def _looks_like_pfx(data: bytes) -> bool:
    """PFX ::= SEQUENCE { version INTEGER (3), authSafe, macData OPTIONAL }: tells a .p12 from any other file,
    so that a failure to open it can be put down to the password."""
    try:
        tag, start, end, _ = ipa._ber(data, 0)
        first = ipa._kids(data, start, end)[0]
        return tag == 0x30 and first[0] == 0x02 and data[first[1]:first[2]] == b"\x03"
    except (IndexError, ValueError, RecursionError):
        return False


def read_p12(data: bytes, password: str) -> Material:
    """Opens a .p12 and returns the PEM pair plumesign expects (PKCS#8 key, certificate). The password is
    not kept. Raises CertError: cert_p12_invalid, cert_password_wrong, cert_p12_no_key, cert_p12_no_cert,
    cert_key_unsupported, cert_key_mismatch."""
    _need_package()
    if not data or len(data) > MAX_P12 or not _looks_like_pfx(data):
        raise CertError("cert_p12_invalid")
    if len(password or "") > MAX_PASSWORD:
        raise CertError("cert_password_wrong")
    # an empty password is stored as "no password" or as an empty one, depending on the tool that wrote the file
    tries = [password.encode("utf-8")] if password else [None, b""]
    loaded = None
    for secret in tries:
        try:
            loaded = pkcs12.load_key_and_certificates(data, secret)
            break
        except ValueError:
            continue
    if loaded is None:
        raise CertError("cert_password_wrong")
    key, cert, extra = loaded
    if key is None:
        raise CertError("cert_p12_no_key")
    if cert is None:
        raise CertError("cert_p12_no_cert")
    if isinstance(key, rsa.RSAPrivateKey):
        kind = "rsa"
    elif isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name == "secp256r1":
        kind = "ec"
    else:
        raise CertError("cert_key_unsupported")
    if key.public_key().public_numbers() != cert.public_key().public_numbers():
        raise CertError("cert_key_mismatch")
    try:
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        subject = cn[0].value if cn else cert.subject.rfc4514_string()
    except (ValueError, IndexError):
        subject = ""
    der = cert.public_bytes(serialization.Encoding.DER)
    info = CertInfo(subject=str(subject)[:120], serial=format(cert.serial_number, "X"),
                    sha1=hashlib.sha1(der).hexdigest().upper(),     # an identifier, not a security measure
                    not_before=_ts(cert.not_valid_before_utc), not_after=_ts(cert.not_valid_after_utc),
                    der=der, key_kind=kind)
    return Material(
        key_pem=key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()),
        cert_pem=cert.public_bytes(serialization.Encoding.PEM), info=info,
        chain=[c.public_bytes(serialization.Encoding.DER) for c in extra or []])


# ---------- checks before saving ----------

def device_ids(device: dict | None) -> set[str]:
    """Identifiers a profile may know the device by: its hardware UDID, and the UDID of its pairing."""
    device = device or {}
    return {norm_udid(v) for v in (device.get("udid"), device.get("pairing_udid"))} - {""}


def check(profile: Profile, cert: CertInfo, device: dict, app: dict | None = None, now: float | None = None) -> list[str]:
    """Is this certificate with this profile fit to sign for this device (and this app, when given)? Raises
    CertError with the first problem; otherwise returns the warnings worth showing: "trust" (an enterprise
    profile on an iPhone or iPad must be trusted once in Settings), "short" (the profile lasts under 30 days)."""
    now = now or time.time()
    if cert.not_after <= now:
        raise CertError(f"cert_expired:{_day(cert.not_after)}")
    if cert.not_before > now + 86400:
        raise CertError(f"cert_not_yet_valid:{_day(cert.not_before)}")
    if profile.expires_at <= now:
        raise CertError(f"cert_profile_expired:{_day(profile.expires_at)}")
    if cert.der not in profile.cert_ders:
        raise CertError("cert_not_in_profile")
    needed = platform_needed(device.get("family"))
    if not any(_same_platform(p, needed) for p in profile.platforms):
        raise CertError(f"cert_platform:{'/'.join(profile.platforms)}:{needed}")
    if not profile.all_devices:
        if not profile.has_device_list:
            raise CertError("cert_profile_not_for_devices")     # an App Store profile: it never installs on a device
        ids = device_ids(device)
        if not ids:
            raise CertError("cert_device_unknown")              # the device was never read: nothing to compare
        if not ids & profile.udids:
            raise CertError("cert_device_not_in_profile")
    if app is not None:
        why = app_problem(profile.pattern, app)
        if why:
            raise CertError(why)
    warnings = []
    if profile.all_devices and device.get("family") in ("ios", "ipados"):
        warnings.append("trust")
    if profile.expires_at - now < SHORT_VALIDITY_DAYS * 86400 or cert.not_after - now < SHORT_VALIDITY_DAYS * 86400:
        warnings.append("short")
    return warnings


def app_problem(pattern: str, app: dict) -> str | None:
    """Code of the reason a profile with this app identifier cannot sign this app, or None."""
    bundle = app.get("bundle_id")
    if not bundle:
        return None                       # the app has no known identifier yet: checked again at its first signature
    if not pattern_matches(pattern, bundle):
        return f"cert_bundle_mismatch:{pattern}:{bundle}"
    extensions = [w for w in (app.get("warnings") or "").split(",") if w.startswith("appex:")]
    if extensions and not pattern.endswith("*"):
        # the same profile is embedded in every extension, whose identifier is not the app's
        return f"cert_needs_wildcard:{pattern}"
    return None


@dataclass
class Prepared:
    """Files that passed every check, ready to be stored."""
    name: str
    info: CertInfo
    profile: Profile
    profile_bytes: bytes
    key_pem: bytes
    cert_pem: bytes
    chain: list
    warnings: list
    expires_at: float


def prepare(device: dict, p12: bytes, password: str, profile_data: bytes, app: dict | None = None,
            now: float | None = None) -> Prepared:
    """Reads and checks the two files. Nothing is stored; raises CertError. Slow (the password is stretched):
    call it from a thread."""
    material = read_p12(p12, password)
    profile = parse_profile(profile_data)
    warnings = check(profile, material.info, device, app, now)
    label = profile.name or material.info.subject or "certificat"
    return Prepared(
        name=(material.info.subject or label)[:80], info=material.info, profile=profile, profile_bytes=profile_data,
        key_pem=material.key_pem, cert_pem=material.cert_pem, chain=material.chain, warnings=warnings,
        expires_at=min(material.info.not_after, profile.expires_at))


# ---------- sealed storage ----------

_KEY: bytes | None = None
_KEY_LOCK = threading.Lock()


def _vault_key() -> bytes:
    """32 random bytes in <data>/vault.key, owner-only, created on first use. A key of its own (not the cookie
    secret): signing everyone out must not lose the certificates."""
    global _KEY
    with _KEY_LOCK:
        if _KEY is None:
            try:
                key = bytes.fromhex(_KEY_FILE.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                key = b""
            if len(key) != 32:
                key = secrets.token_bytes(32)
                write_private(_KEY_FILE, key.hex())
            _KEY = key
        return _KEY


def _path(ident_id: int) -> Path:
    return CERT_DIR / f"{int(ident_id)}.bin"


def _aad(ident_id: int) -> bytes:
    return b"tvsign-cert:%d" % int(ident_id)        # a sealed file copied under another number does not open


def seal(ident_id: int, payload: dict) -> bytes:
    _need_package()
    nonce = secrets.token_bytes(12)
    return _MAGIC + nonce + AESGCM(_vault_key()).encrypt(nonce, json.dumps(payload).encode("utf-8"), _aad(ident_id))


def unseal(ident_id: int, blob: bytes) -> dict:
    _need_package()
    if blob[:4] != _MAGIC:
        raise ValueError("not a sealed certificate")
    return json.loads(AESGCM(_vault_key()).decrypt(blob[4:16], blob[16:], _aad(ident_id)))


def _payload(prep: Prepared) -> dict:
    return {"v": 1, "cert_pem": prep.cert_pem.decode("ascii"), "key_pem": prep.key_pem.decode("ascii"),
            "chain": [base64.b64encode(c).decode("ascii") for c in prep.chain],
            "profile": base64.b64encode(prep.profile_bytes).decode("ascii")}


def write_material(ident_id: int, payload: dict) -> None:
    write_private(_path(ident_id), seal(ident_id, payload))


def read_material(ident_id: int) -> dict | None:
    """The clear payload of a stored certificate; None when its file is not here (a backup restored without
    the certificates); CertProblem when the file cannot be opened (vault.key lost)."""
    try:
        blob = _path(ident_id).read_bytes()
    except OSError:
        return None
    if not AVAILABLE:
        raise CertProblem("le module « cryptography » manque sur ce serveur : une app signée avec un certificat importé "
                          "ne peut pas être signée (réinstalle tvsign, ou reviens à l'Apple ID dans les réglages de l'app)",
                          "unavailable")
    try:
        return unseal(ident_id, blob)
    except (InvalidTag, ValueError):
        raise CertProblem("certificat importé illisible (clé vault.key changée ou fichier abîmé) : importe-le de "
                          "nouveau (réglages de l'app › Avancé)", "missing") from None


def has_material(ident_id: int) -> bool:
    return _path(ident_id).is_file()


@contextlib.contextmanager
def signing_files(ident_id: int):
    """Certificate, key and profile as plain files for ONE plumesign run: a folder of ours, owner-only, removed
    as soon as the run ends (and at startup, if tvsign was killed in between)."""
    payload = read_material(ident_id)
    if payload is None:
        raise CertProblem("certificat importé introuvable sur ce serveur (sauvegarde restaurée sans les certificats ?) "
                          ": importe-le de nouveau (réglages de l'app › Avancé)", "missing")
    folder = Path(tempfile.mkdtemp(prefix="cert-", dir=TMP_DIR))
    try:
        with contextlib.suppress(OSError):
            os.chmod(folder, 0o700)
        files = SimpleNamespace(cert=folder / "cert.pem", key=folder / "key.pem", profile=folder / "profile.mobileprovision")
        write_private(files.cert, payload["cert_pem"])
        write_private(files.key, payload["key_pem"])
        write_private(files.profile, base64.b64decode(payload["profile"]))
        yield files
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def sweep_temp() -> None:
    """At startup: folders of a signature that never finished (the process was killed) hold a private key."""
    for folder in TMP_DIR.glob("cert-*"):
        shutil.rmtree(folder, ignore_errors=True)


def settle() -> int:
    """At startup and at the end of a restore. Files `<id>.json` are certificates that came out of a backup made
    with them (clear text): they are sealed with this instance's key and removed. Sealed files with no
    certificate row (a device removed, a restore) are deleted. Returns how many files were sealed or removed."""
    known = {r["id"] for r in db.rows("SELECT id FROM imported_certs")}
    changed = 0
    for path in sorted(CERT_DIR.glob("*.json")):
        try:
            if path.stem.isdigit() and int(path.stem) in known:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and {"cert_pem", "key_pem", "profile"} <= payload.keys():
                    write_material(int(path.stem), payload)
        except (OSError, ValueError, CertError):
            pass                    # unreadable: the certificate shows as missing and is imported again
        finally:
            path.unlink(missing_ok=True)
        changed += 1
    for path in CERT_DIR.glob("*.bin"):
        if not path.stem.isdigit() or int(path.stem) not in known:
            path.unlink(missing_ok=True)
            changed += 1
    for path in CERT_DIR.glob("*.tmp"):          # a write that was interrupted
        path.unlink(missing_ok=True)
        changed += 1
    return changed


# ---------- database ----------

_COLUMNS = ("device_id", "name", "subject", "team_id", "team_name", "cert_serial", "cert_sha1", "cert_not_after",
            "profile_uuid", "profile_name", "profile_app_id", "profile_platforms", "profile_expires", "all_devices",
            "expires_at", "revoked_at", "revoked_reason", "checked_at", "check_state", "notified_level",
            "created_at", "updated_at")


def row(ident_id: int | None) -> dict | None:
    return db.row("SELECT * FROM imported_certs WHERE id = ?", ident_id) if ident_id else None


def rows(device_id: int | None = None) -> list[dict]:
    if device_id is None:
        return db.rows("SELECT * FROM imported_certs ORDER BY name, id")
    return db.rows("SELECT * FROM imported_certs WHERE device_id = ? ORDER BY name, id", device_id)


def commit(prep: Prepared, device_id: int, replace_id: int | None = None, now: float | None = None) -> dict:
    """Stores a prepared certificate: a new one, or the files of `replace_id` (its apps keep pointing at it)."""
    now = now or time.time()
    fields = dict(device_id=device_id, name=prep.name, subject=prep.info.subject, team_id=prep.profile.team_id,
                  team_name=prep.profile.team_name, cert_serial=prep.info.serial, cert_sha1=prep.info.sha1,
                  cert_not_after=prep.info.not_after, profile_uuid=prep.profile.uuid, profile_name=prep.profile.name,
                  profile_app_id=prep.profile.pattern, profile_platforms=",".join(prep.profile.platforms),
                  profile_expires=prep.profile.expires_at, all_devices=int(prep.profile.all_devices),
                  expires_at=prep.expires_at, revoked_at=None, revoked_reason=None, checked_at=None,
                  check_state=None, notified_level=None, updated_at=now)
    if replace_id:
        write_material(replace_id, _payload(prep))      # the sealed file first: a failure leaves the old one whole
        db.update("imported_certs", replace_id, **fields)
        return row(replace_id)
    fields["created_at"] = now
    ident_id = db.execute(f"INSERT INTO imported_certs ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                          *fields.values())
    try:
        write_material(ident_id, _payload(prep))
    except Exception:
        db.execute("DELETE FROM imported_certs WHERE id = ?", ident_id)
        raise
    return row(ident_id)


def names_using(ident_id: int) -> list[str]:
    return [a["name"] for a in db.rows("SELECT name FROM apps WHERE cert_id = ? AND sign_mode = ? ORDER BY name",
                                       ident_id, CERTIFICATE)]


def remove(ident_id: int) -> None:
    """Deletes a certificate and its sealed file. Raises CertError("cert_in_use:<apps>") while an app uses it."""
    using = names_using(ident_id)
    if using:
        raise CertError("cert_in_use:" + ", ".join(n.replace(":", " ") for n in using))
    db.execute("DELETE FROM imported_certs WHERE id = ?", ident_id)
    _path(ident_id).unlink(missing_ok=True)


def forget_device(device_id: int) -> None:
    """A device is removed: its certificates go with it, and the apps that used them lose the link."""
    for r in rows(device_id):
        db.execute("UPDATE apps SET cert_id = NULL WHERE cert_id = ?", r["id"])
        db.execute("DELETE FROM imported_certs WHERE id = ?", r["id"])
        _path(r["id"]).unlink(missing_ok=True)


# ---------- state, expiry, selection ----------

def expiry_level(left: float) -> str | None:
    """"expired", "1d" (under a day left), "7d" (under a week) or None: the thresholds of the warnings."""
    if left <= 0:
        return "expired"
    for days in sorted(EXPIRY_ALERT_DAYS):
        if left < days * 86400:
            return f"{days}d"
    return None


LEVEL_RANK = {"7d": 1, "1d": 2, "expired": 3, "revoked": 4}


def state_of(ident: dict, now: float | None = None) -> str:
    """"revoked", "expired", "expiring" (under 7 days), "missing" (no file here) or "ok"."""
    now = now or time.time()
    if ident.get("revoked_at"):
        return "revoked"
    left = (ident.get("expires_at") or 0) - now
    if left <= 0:
        return "expired"
    if not has_material(ident["id"]):
        return "missing"
    return "expiring" if expiry_level(left) else "ok"


def view(ident: dict, now: float | None = None) -> dict:
    """What the interface may see of a certificate (it never holds a secret): the row plus its state."""
    now = now or time.time()
    return {**ident, "state": state_of(ident, now), "has_material": has_material(ident["id"]),
            "platforms": [p for p in (ident.get("profile_platforms") or "").split(",") if p],
            "wildcard": (ident.get("profile_app_id") or "").endswith("*"), "apps": names_using(ident["id"])}


def problem_for(ident: dict | None, device: dict | None, app: dict | None, now: float | None = None) -> tuple[str, str] | None:
    """Why this certificate cannot sign this app: (stable API code, French message), or None when it can.
    Used by the API when the user picks a certificate and by the renewal before every signature."""
    now = now or time.time()
    if not ident:
        return ("cert_none", "aucun certificat importé choisi pour cette app : importes-en un (réglages de l'app › Avancé) "
                             "ou reviens à l'Apple ID")
    name = ident.get("name") or "certificat"
    if ident.get("revoked_at"):
        return ("cert_revoked", f"certificat importé révoqué « {name} » : Apple ne le reconnaît plus, l'app ne peut plus "
                                "être installée ni lancée avec lui. Importe un nouveau certificat et son profil (réglages "
                                "de l'app › Avancé) ou reviens à l'Apple ID")
    if ident.get("cert_not_after") and ident["cert_not_after"] <= now:
        return (f"cert_expired:{_day(ident['cert_not_after'])}",
                f"certificat importé expiré le {_day(ident['cert_not_after'])} « {name} » : importe un nouveau certificat "
                "et son profil (réglages de l'app › Avancé)")
    if ident.get("profile_expires") and ident["profile_expires"] <= now:
        return (f"cert_profile_expired:{_day(ident['profile_expires'])}",
                f"profil importé expiré le {_day(ident['profile_expires'])} « {name} » : importe un nouveau profil "
                "(réglages de l'app › Avancé)")
    if not has_material(ident["id"]):
        return ("cert_missing", f"certificat importé introuvable sur ce serveur « {name} » (sauvegarde restaurée sans les "
                                "certificats ?) : importe-le de nouveau (réglages de l'app › Avancé)")
    if device and ident.get("device_id") != device.get("id"):
        return ("cert_wrong_device", f"le certificat importé « {name} » est fait pour un autre appareil : importe-le pour "
                                     f"« {device.get('name') or 'cet appareil'} »")
    if device:
        needed = platform_needed(device.get("family"))
        platforms = [p for p in (ident.get("profile_platforms") or "").split(",") if p]
        if platforms and not any(_same_platform(p, needed) for p in platforms):
            return (f"cert_platform:{'/'.join(platforms)}:{needed}",
                    f"le profil importé « {name} » est fait pour {'/'.join(platforms)}, cet appareil demande {needed}")
    if app is not None:
        why = app_problem(ident.get("profile_app_id") or "", app)
        if why:
            code = why.split(":", 1)[0]
            if code == "cert_needs_wildcard":
                return (why, f"l'app contient des extensions et le profil importé « {name} » ne couvre qu'un seul "
                             f"identifiant ({ident.get('profile_app_id')}) : utilise un profil à joker")
            return (why, f"le profil importé « {name} » ne couvre pas l'identifiant {app.get('bundle_id')} (profil : "
                         f"{ident.get('profile_app_id')}) : utilise un profil à joker ou fait pour cette app")
    return None


def require(app: dict, device: dict | None, now: float | None = None) -> dict:
    """The certificate an app set to the certificate route must sign with, or CertProblem."""
    ident = row(app.get("cert_id"))
    problem = problem_for(ident, device, app, now)
    if problem:
        raise CertProblem(problem[1], problem[0].split(":", 1)[0])
    return ident


def pick(device: dict, app: dict | None = None, now: float | None = None) -> dict | None:
    """The certificate to use when the user did not choose one: among those imported for this device that can
    sign this app, the one that stays valid the longest."""
    fit = [i for i in rows(device["id"]) if problem_for(i, device, app, now) is None]
    return max(fit, key=lambda i: (i.get("expires_at") or 0, i["id"]), default=None)


def signature_expiry(ident: dict, signed_ipa) -> float:
    """When an app signed with this certificate stops launching: the earlier of the profile embedded in the
    signed IPA and the certificate."""
    try:
        embedded = ipa.profile_expiry(signed_ipa)
    except Exception:  # noqa: BLE001 - partial or unreadable file: the stored dates are what is known
        embedded = None
    return min(t for t in (embedded, ident.get("cert_not_after"), ident.get("profile_expires"), ident.get("expires_at")) if t)


# ---------- how installd refuses an install ----------

_REVOKED = re.compile(r"identity used to sign the executable is no longer valid|0xe8008018|certificate .{0,20}revoked|"
                      r"CSSMERR_TP_CERT_REVOKED|CERT_REVOKED|revoked certificate", re.I)
_PROFILE_EXPIRED = re.compile(r"provisioning profile .{0,40}expired|profile has expired|ExpiredProvisioningProfile|"
                              r"0xe800801[ab]", re.I)
_NO_PROFILE = re.compile(r"valid provisioning profile for this executable was not found|0xe8008015|"
                         r"does not include the device|not included in the provisioning profile", re.I)
_ENTITLEMENTS = re.compile(r"signed with invalid entitlements|0xe8008016|entitlements? .{0,40}(not allowed|not permitted)", re.I)
_IDENTIFIER = re.compile(r"MismatchedApplicationIdentifierEntitlement|application-identifier entitlement .{0,40}"
                         r"(did not|does not|doesn't) match|0xe8008020", re.I)


def classify_install_failure(text: str) -> str | None:
    """What installd's refusal says about the certificate: "revoked", "profile_expired", "no_profile" (no valid
    profile for this executable on this device), "entitlements", "identifier", or None for anything else."""
    text = text or ""
    for kind, rx in (("revoked", _REVOKED), ("profile_expired", _PROFILE_EXPIRED), ("identifier", _IDENTIFIER),
                     ("entitlements", _ENTITLEMENTS), ("no_profile", _NO_PROFILE)):
        if rx.search(text):
            return kind
    return None


def explain_install_failure(text: str, ident: dict) -> CertProblem | None:
    """The CertProblem to show for an install refused because of the certificate, or None when the refusal is
    about something else (full disk, app open…). The caller records a revocation (record_revoked)."""
    kind = classify_install_failure(text)
    name = ident.get("name") or "certificat"
    if kind == "revoked":
        return CertProblem(problem_for({**ident, "revoked_at": time.time()}, None, None)[1], "revoked")
    if kind == "profile_expired":
        return CertProblem(f"profil importé expiré « {name} » : importe un nouveau profil (réglages de l'app › Avancé)",
                           "profile_expired")
    if kind == "no_profile":
        return CertProblem(f"profil importé refusé par l'appareil « {name} » : aucun profil valide pour cette app (appareil "
                           "absent du profil, profil expiré ou certificat qui n'y figure pas). Importe un profil à jour",
                           "no_profile")
    if kind == "entitlements":
        return CertProblem(f"droits de l'app refusés par le profil importé « {name} » : l'app demande des capacités que le "
                           "profil n'accorde pas. Utilise un profil qui les accorde", "entitlements")
    if kind == "identifier":
        return CertProblem(f"identifiant de l'app refusé par le profil importé « {name} » : le profil ne couvre pas cet "
                           "identifiant. Utilise un profil à joker ou fait pour cette app", "identifier")
    return None


def record_revoked(ident_id: int, reason: str, now: float | None = None) -> bool:
    """Marks a certificate revoked (OCSP said so, or installd refused its identity). True when it was not
    already marked, so the caller warns once."""
    current = row(ident_id)
    if not current or current.get("revoked_at"):
        return False
    db.update("imported_certs", ident_id, revoked_at=now or time.time(), revoked_reason=reason[:200],
              check_state="revoked", checked_at=now or time.time())
    return True


# ---------- revocation: OCSP ----------

OCSP_TIMEOUT = 8.0


@dataclass
class Revocation:
    state: str                       # good | revoked | unknown
    detail: str = ""                 # revoked: the reason OCSP gave; unknown: why nothing could be concluded
    at: float | None = None          # revoked: since when


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def apple_host(url: str) -> bool:
    """OCSP and issuer addresses come from the certificate: only Apple's are ever contacted."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return parts.scheme in ("http", "https") and (host == "apple.com" or host.endswith(".apple.com"))


def _fetch(url: str, body: bytes | None = None) -> bytes:
    if not apple_host(url):
        raise OSError("address not allowed")
    headers = {"User-Agent": f"tvsign/{VERSION}"}
    if body is not None:
        headers["Content-Type"] = "application/ocsp-request"
    with _OPENER.open(urllib.request.Request(url, data=body, headers=headers), timeout=OCSP_TIMEOUT) as r:
        return r.read(256 * 1024)


def _issuer(cert, candidates):
    for c in candidates:
        try:
            cert.verify_directly_issued_by(c)
            return c
        except (ValueError, TypeError, InvalidSignature):
            continue
    return None


def _responder_ok(resp, issuer) -> bool:
    """Is the OCSP answer signed by the issuer, or by a responder certificate the issuer signed for that use?"""
    signers = [issuer]
    for c in resp.certificates:
        try:
            c.verify_directly_issued_by(issuer)
            if ExtendedKeyUsageOID.OCSP_SIGNING in c.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
                signers.append(c)
        except (ValueError, TypeError, InvalidSignature, x509.ExtensionNotFound):
            continue
    for signer in signers:
        if resp.responder_name is not None and signer.subject != resp.responder_name:
            continue
        key = signer.public_key()
        if resp.responder_key_hash is not None:
            bits = (key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1)
                    if isinstance(key, rsa.RSAPublicKey)
                    else key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
            if hashlib.sha1(bits).digest() != resp.responder_key_hash:
                continue
        try:
            if isinstance(key, rsa.RSAPublicKey):
                key.verify(resp.signature, resp.tbs_response_bytes, padding.PKCS1v15(), resp.signature_hash_algorithm)
            elif isinstance(key, ec.EllipticCurvePublicKey):
                key.verify(resp.signature, resp.tbs_response_bytes, ec.ECDSA(resp.signature_hash_algorithm))
            else:
                continue
            return True
        except (InvalidSignature, ValueError, TypeError):
            continue
    return False


def check_revocation(cert_der: bytes, chain=(), now: float | None = None, fetch=None) -> Revocation:
    """Asks the certificate's OCSP responder whether it was revoked. Concludes "revoked" only on a signed answer
    that says so; anything else (no address, responder unreachable, unsigned or stale answer, unknown
    certificate) is "unknown", which never alarms. `fetch(url, body=None)` is replaceable for tests."""
    if not AVAILABLE:
        return Revocation("unknown", "cryptography package missing")
    fetch = fetch or _fetch
    now = now or time.time()
    try:
        cert = x509.load_der_x509_certificate(cert_der)
        try:
            access = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
        except x509.ExtensionNotFound:
            return Revocation("unknown", "no OCSP address in the certificate")
        responders = [d.access_location.value for d in access
                      if d.access_method == AuthorityInformationAccessOID.OCSP and apple_host(d.access_location.value)]
        if not responders:
            return Revocation("unknown", "no OCSP address on apple.com")
        issuer = _issuer(cert, [x509.load_der_x509_certificate(c) for c in chain])
        if issuer is None:
            for d in access:
                if d.access_method != AuthorityInformationAccessOID.CA_ISSUERS or not apple_host(d.access_location.value):
                    continue
                data = fetch(d.access_location.value)
                try:
                    candidate = x509.load_der_x509_certificate(data)
                except ValueError:
                    candidate = x509.load_pem_x509_certificate(data)
                issuer = _issuer(cert, [candidate])
                if issuer:
                    break
        if issuer is None:
            return Revocation("unknown", "issuer certificate not found")
        request = ocsp.OCSPRequestBuilder().add_certificate(cert, issuer, hashes.SHA1()).build()
        resp = ocsp.load_der_ocsp_response(fetch(responders[0], request.public_bytes(serialization.Encoding.DER)))
    except (OSError, ValueError, TypeError, http.client.HTTPException) as e:
        return Revocation("unknown", str(e)[:120] or type(e).__name__)
    if resp.response_status != ocsp.OCSPResponseStatus.SUCCESSFUL:
        return Revocation("unknown", f"OCSP answer: {resp.response_status.name}")
    if not _responder_ok(resp, issuer):
        return Revocation("unknown", "OCSP answer not signed by the issuer")
    if (resp.serial_number != cert.serial_number or resp.issuer_key_hash != request.issuer_key_hash
            or resp.issuer_name_hash != request.issuer_name_hash):
        return Revocation("unknown", "OCSP answer about another certificate")
    if resp.this_update_utc.timestamp() > now + 600 or (resp.next_update_utc and resp.next_update_utc.timestamp() < now - 600):
        return Revocation("unknown", "OCSP answer out of date")
    if resp.certificate_status == ocsp.OCSPCertStatus.REVOKED:
        reason = resp.revocation_reason.name if resp.revocation_reason else "unspecified"
        return Revocation("revoked", reason, resp.revocation_time_utc.timestamp() if resp.revocation_time_utc else None)
    if resp.certificate_status == ocsp.OCSPCertStatus.GOOD:
        return Revocation("good")
    return Revocation("unknown", "certificate unknown to the responder")


def record_check(ident_id: int, result: Revocation, now: float | None = None) -> None:
    """Remembers the outcome of a check. An "unknown" answer keeps the last conclusion and only moves the date
    forward (the next check is a full period later, not on every pass)."""
    now = now or time.time()
    if result.state == "unknown":
        db.update("imported_certs", ident_id, checked_at=now)
    elif result.state == "good":
        db.update("imported_certs", ident_id, checked_at=now, check_state="good")
    else:
        record_revoked(ident_id, f"OCSP : {result.detail}", result.at or now)
        db.update("imported_certs", ident_id, checked_at=now)


def material_cert(ident_id: int) -> tuple[bytes, list] | None:
    """(certificate DER, chain DER) of a stored certificate, for the revocation check."""
    payload = read_material(ident_id)
    if payload is None:
        return None
    _need_package()
    pem = payload["cert_pem"].encode("ascii")
    der = x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER)
    return der, [base64.b64decode(c) for c in payload.get("chain", [])]


# ---------- backups (opt-in) ----------

def export_payloads() -> dict[int, dict]:
    """{id: clear payload} of every stored certificate, for a backup the user asked to include them in."""
    out = {}
    for r in rows():
        try:
            payload = read_material(r["id"])
        except CertProblem:
            continue
        if payload is not None:
            out[r["id"]] = payload
    return out
