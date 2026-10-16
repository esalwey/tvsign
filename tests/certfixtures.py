"""Fixtures for the imported-certificate tests: throw-away certificates, a .p12, provisioning profiles, OCSP
answers and a minimal unsigned tvOS IPA holding a real (empty) arm64 Mach-O, so that plumesign can really sign it.

The functions that generate keys need the cryptography package (tests skip without it); profile_bytes() and the
static legacy .p12 below do not.
"""
import datetime
import io
import plistlib
import struct
import zipfile

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509 import ocsp
    from cryptography.x509.oid import AuthorityInformationAccessOID, ExtendedKeyUsageOID, NameOID
    HAVE_CRYPTO = True
except ImportError:                 # pragma: no cover
    HAVE_CRYPTO = False

TEAM = "ABCDE12345"
UDID = "00008110-001A2B3C4D5E6F78"
OCSP_URL = "http://ocsp.apple.com/ocsp03-wwdr01"
ISSUER_URL = "http://developer.apple.com/certificationauthority/AppleWWDRCAG3.cer"


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ---------- certificates ----------

def _name(cn: str, ou: str | None = None) -> "x509.Name":
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    if ou:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, ou))
    return x509.Name(attrs + [x509.NameAttribute(NameOID.COUNTRY_NAME, "US")])


def make_ca(cn: str = "Test Developer Relations CA"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = _name(cn)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now_utc() - datetime.timedelta(days=30))
            .not_valid_after(now_utc() + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def make_cert(cn: str = f"Apple Development: Test ({TEAM})", days: int = 300, begin_days: int = -1, ca=None,
              aia: bool = True, ec_key: bool = False, serial: int | None = None, key=None, ocsp_url: str = OCSP_URL,
              issuer_url: str = ISSUER_URL):
    """(key, certificate). Signed by `ca` ((key, cert)) when given, else self-signed. With a CA the certificate
    carries the OCSP and issuer addresses the way Apple's do (`aia=False`: none)."""
    if key is None:
        key = (ec.generate_private_key(ec.SECP256R1()) if ec_key
               else rsa.generate_private_key(public_exponent=65537, key_size=2048))
    subject = _name(cn, TEAM)
    builder = (x509.CertificateBuilder().subject_name(subject)
               .issuer_name(ca[1].subject if ca else subject).public_key(key.public_key())
               .serial_number(serial or x509.random_serial_number())
               .not_valid_before(now_utc() + datetime.timedelta(days=begin_days))
               .not_valid_after(now_utc() + datetime.timedelta(days=days)))
    if ca and aia:
        builder = builder.add_extension(x509.AuthorityInformationAccess([
            x509.AccessDescription(AuthorityInformationAccessOID.OCSP, x509.UniformResourceIdentifier(ocsp_url)),
            x509.AccessDescription(AuthorityInformationAccessOID.CA_ISSUERS, x509.UniformResourceIdentifier(issuer_url)),
        ]), critical=False)
    cert = builder.sign(ca[0] if ca else key, hashes.SHA256())
    return key, cert


def der(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def make_p12(key, cert, password: str | None = "secret", chain=()) -> bytes:
    enc = (serialization.BestAvailableEncryption(password.encode()) if password else serialization.NoEncryption())
    return pkcs12.serialize_key_and_certificates(b"tvsign test", key, cert, list(chain) or None, enc)


# ---------- provisioning profiles ----------

def profile_bytes(certs, app_id: str = f"{TEAM}.*", udids=(UDID,), platform=("tvOS",), days: float = 300,
                  all_devices: bool = False, device_list: bool = True, name: str = "Test profile",
                  team: str = TEAM, created_days: float = -30) -> bytes:
    """A .mobileprovision: a stand-in for the CMS container (plumesign and tvsign only read the XML plist in it)
    around the plist. `certs` are DER bytes."""
    data = {
        "AppIDName": name, "ApplicationIdentifierPrefix": [team], "Name": name, "UUID": "11111111-2222-3333-4444-555555555555",
        "CreationDate": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(days=created_days),
        "ExpirationDate": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(days=days),
        "Platform": list(platform), "TeamIdentifier": [team], "TeamName": "Test Team", "TimeToLive": 365, "Version": 1,
        "DeveloperCertificates": [bytes(c) for c in certs],
        "Entitlements": {"application-identifier": app_id, "com.apple.developer.team-identifier": team,
                         "get-task-allow": True, "keychain-access-groups": [f"{team}.*"]},
    }
    if device_list:
        data["ProvisionedDevices"] = list(udids)
    if all_devices:
        data["ProvisionsAllDevices"] = True
    return b"\x30\x82\x01\x00 not a real CMS header \x00" + plistlib.dumps(data) + b"\x00\x00trailer"


# ---------- an unsigned tvOS IPA whose executable plumesign can sign ----------

def macho(platform: int = 3) -> bytes:
    """Minimal arm64 executable: __PAGEZERO, __TEXT (one section), __LINKEDIT, symtab, uuid, build version, main."""
    def seg(name, vmaddr, vmsize, fileoff, filesize, maxprot, initprot, sects=b"", nsects=0):
        return struct.pack("<II16sQQQQiiII", 0x19, 72 + len(sects), name.encode().ljust(16, b"\0"),
                           vmaddr, vmsize, fileoff, filesize, maxprot, initprot, nsects, 0) + sects

    text_off = 0x3F00
    sect = struct.pack("<16s16sQQIIIIIIII", b"__text", b"__TEXT", 0x100000000 + text_off, 8, text_off, 2, 0, 0,
                       0x80000400, 0, 0, 0)
    cmds = [
        seg("__PAGEZERO", 0, 0x100000000, 0, 0, 0, 0),
        seg("__TEXT", 0x100000000, 0x4000, 0, 0x4000, 5, 5, sect, 1),
        seg("__LINKEDIT", 0x100004000, 0x4000, 0x4000, 0x100, 1, 1),
        struct.pack("<IIIIII", 0x2, 24, 0x4000, 0, 0x4000, 8),                       # LC_SYMTAB
        struct.pack("<II", 0xB, 80) + b"\0" * 72,                                      # LC_DYSYMTAB
        struct.pack("<II16s", 0x1B, 24, bytes(range(16))),                             # LC_UUID
        struct.pack("<IIIIII", 0x32, 24, platform, 0x000F0000, 0x000F0000, 0),         # LC_BUILD_VERSION
        struct.pack("<IIQQ", 0x80000028, 24, text_off, 0),                             # LC_MAIN
    ]
    dylib = b"/usr/lib/libSystem.B.dylib\0"
    dylib += b"\0" * (-(24 + len(dylib)) % 8)
    cmds.append(struct.pack("<IIIIII", 0xC, 24 + len(dylib), 24, 2, 0x010000, 0x010000) + dylib)   # LC_LOAD_DYLIB
    body = b"".join(cmds)
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, len(cmds), len(body), 0x200085, 0)
    blob = (header + body).ljust(text_off, b"\0") + struct.pack("<II", 0xD65F03C0, 0xD65F03C0)    # two `ret`
    return blob.ljust(0x4000, b"\0") + b"\0" * 0x100


def signable_ipa(bundle: str = "com.example.fixture", platform_name: str = "appletvos", extension: str | None = None) -> bytes:
    info = {"CFBundleName": "Fixture", "CFBundleExecutable": "Fixture", "CFBundleIdentifier": bundle,
            "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1", "DTPlatformName": platform_name,
            "MinimumOSVersion": "15.0", "UIDeviceFamily": [3]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Payload/Fixture.app/Info.plist", plistlib.dumps(info))
        exe = zipfile.ZipInfo("Payload/Fixture.app/Fixture")
        exe.external_attr = 0o755 << 16
        z.writestr(exe, macho())
        if extension:
            z.writestr("Payload/Fixture.app/PlugIns/Widget.appex/Info.plist", plistlib.dumps(
                {"CFBundleName": "Widget", "CFBundleExecutable": "Widget", "CFBundleIdentifier": extension,
                 "CFBundlePackageType": "XPC!", "NSExtension": {"NSExtensionPointIdentifier": "com.apple.widget-extension"}}))
            ext = zipfile.ZipInfo("Payload/Fixture.app/PlugIns/Widget.appex/Widget")
            ext.external_attr = 0o755 << 16
            z.writestr(ext, macho())
    return buf.getvalue()


def embedded_entitlements(ipa_path, executable: str | None = None) -> dict | None:
    """Entitlements written into the code signature of the main executable of a signed IPA (or of `executable`,
    a path inside the IPA such as an extension's)."""
    z = zipfile.ZipFile(ipa_path)
    if executable:
        exe = z.read(executable)
    else:
        plist_name = next(n for n in z.namelist() if n.endswith(".app/Info.plist") and n.count("/") == 2)
        info = plistlib.loads(z.read(plist_name))
        exe = z.read(plist_name.rsplit("/", 1)[0] + "/" + info["CFBundleExecutable"])
    off = 32
    for _ in range(struct.unpack_from("<I", exe, 16)[0]):
        cmd, size = struct.unpack_from("<II", exe, off)
        if cmd == 0x1D:                                  # LC_CODE_SIGNATURE
            dataoff, datasize = struct.unpack_from("<II", exe, off + 8)
            blob = exe[dataoff:dataoff + datasize]
            for i in range(struct.unpack_from(">I", blob, 8)[0]):
                kind, at = struct.unpack_from(">II", blob, 12 + 8 * i)
                if kind == 5:                            # CSSLOT_ENTITLEMENTS
                    length = struct.unpack_from(">II", blob, at)[1]
                    return plistlib.loads(blob[at + 8:at + length])
        off += size
    return None


# ---------- OCSP ----------

def ocsp_answer(cert, issuer_key, issuer_cert, status: str = "good", this_update_days: float = 0, next_update_days: float = 1,
                responder=None, wrong_serial: bool = False, revoked_days_ago: float = 3, serial=None) -> bytes:
    """A DER OCSP answer about `cert`, signed by the issuer (or by `responder` = (key, certificate) when given).
    status: good | revoked | unknown."""
    now = now_utc()
    builder = ocsp.OCSPResponseBuilder()
    target = cert
    if status == "good":
        kind, reason, when = ocsp.OCSPCertStatus.GOOD, None, None
    elif status == "revoked":
        kind, reason, when = ocsp.OCSPCertStatus.REVOKED, x509.ReasonFlags.key_compromise, now - datetime.timedelta(days=revoked_days_ago)
    else:
        kind, reason, when = ocsp.OCSPCertStatus.UNKNOWN, None, None
    builder = builder.add_response(
        cert=target, issuer=issuer_cert, algorithm=hashes.SHA1(), cert_status=kind,
        this_update=now + datetime.timedelta(days=this_update_days),
        next_update=now + datetime.timedelta(days=next_update_days) if next_update_days is not None else None,
        revocation_time=when, revocation_reason=reason)
    signer_key, signer_cert = responder if responder else (issuer_key, issuer_cert)
    builder = builder.responder_id(ocsp.OCSPResponderEncoding.HASH, signer_cert)
    if responder:
        builder = builder.certificates([responder[1]])
    return builder.sign(signer_key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


def ocsp_responder_cert(ca_key, ca_cert, cn: str = "Test OCSP Responder"):
    """A certificate the CA signed to answer OCSP queries in its name."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now_utc() - datetime.timedelta(days=1))
            .not_valid_after(now_utc() + datetime.timedelta(days=30))
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.OCSP_SIGNING]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert
