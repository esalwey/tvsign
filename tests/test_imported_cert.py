"""Signature par un certificat importé (.p12 + profil de provisionnement), la voie secondaire à côté de l'Apple ID gratuit :
lecture et contrôles des fichiers avant enregistrement, stockage scellé, choix du certificat par app, calendrier (pas de
renouvellement tous les 7 jours), alertes à 7 jours et à 1 jour, révocation (OCSP et refus de l'installation), sauvegardes.

    python -m unittest discover -s tests

Les contrôles du profil et du choix n'ont pas besoin du paquet cryptography ; ceux qui lisent un .p12, scellent un fichier
ou interrogent un serveur OCSP l'exigent (ignorés sans lui). Aucun plumesign ni Apple TV : les appels externes sont remplacés,
sauf pour la signature de bout en bout, qui ne tourne que si le binaire plumesign de vendor/Impactor est construit.
"""
import asyncio
import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

if "app.config" not in sys.modules:       # même environnement isolé que test_logic.py
    DATA = tempfile.mkdtemp(prefix="tvsign-test-")
    os.environ.update(TVSIGN_DATA=DATA, TVSIGN_AUTH="none", TVSIGN_PLUMESIGN=str(Path(DATA) / "absent"))
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import certdata  # noqa: E402
import certfixtures as fx  # noqa: E402
from app import backup, config, db, devices, imported_cert as ic, ipa, main, plume, renew, support  # noqa: E402
from test_renew_engine import Base, make_ipa, make_signed  # noqa: E402

HEADERS = {"X-Tvsign": "1"}
NOW = time.time()
DAY = 86400.0
needs_crypto = unittest.skipUnless(fx.HAVE_CRYPTO and ic.AVAILABLE, "paquet cryptography absent")
POSIX = os.name != "nt"


def device_row(**over) -> dict:
    row = dict(id=1, name="Salon", family="tvos", udid=fx.UDID, pairing_udid=None)
    row.update(over)
    return row


def cert_info(der=certdata.CERT1, **over) -> ic.CertInfo:
    values = dict(subject="Apple Development: Test", serial="A1B2C", sha1="00" * 20, not_before=NOW - DAY, not_after=NOW + 200 * DAY,
                  der=der)
    values.update(over)
    return ic.CertInfo(**values)


def profile(*certs, **kw) -> ic.Profile:
    return ic.parse_profile(fx.profile_bytes(certs or [certdata.CERT1], **kw))


def code_of(call, *args, **kw) -> str:
    """Le code d'erreur de l'API que lève call(), ou « ok »."""
    try:
        call(*args, **kw)
    except ic.CertError as e:
        return e.code
    return "ok"


# ---------- le profil de provisionnement ----------

class ProfileParsing(unittest.TestCase):
    def test_fields_of_a_development_profile(self):
        p = profile(certdata.CERT1, app_id="ABCDE12345.com.example.app", udids=[fx.UDID, "AAAA-bbbb"])
        self.assertEqual((p.name, p.team_id, p.team_name), ("Test profile", "ABCDE12345", "Test Team"))
        self.assertEqual((p.app_id, p.pattern, p.wildcard), ("ABCDE12345.com.example.app", "com.example.app", False))
        self.assertEqual(p.platforms, ("tvOS",))
        self.assertEqual(p.udids, frozenset({ic.norm_udid(fx.UDID), "aaaabbbb"}))
        self.assertEqual(p.cert_ders, (certdata.CERT1,))
        self.assertTrue(p.has_device_list and not p.all_devices)
        self.assertAlmostEqual(p.expires_at, NOW + 300 * DAY, delta=60)

    def test_wildcards(self):
        self.assertEqual(profile(app_id="ABCDE12345.*").pattern, "*")
        self.assertTrue(profile(app_id="ABCDE12345.*").wildcard)
        prefix = profile(app_id="ABCDE12345.com.example.*")
        self.assertEqual((prefix.pattern, prefix.wildcard), ("com.example.*", True))

    def test_enterprise_and_store_profiles(self):
        enterprise = profile(device_list=False, all_devices=True)
        self.assertTrue(enterprise.all_devices and not enterprise.has_device_list)
        store = profile(device_list=False)
        self.assertFalse(store.all_devices or store.has_device_list)

    def test_a_profile_without_platform_is_an_ios_one(self):
        data = fx.profile_bytes([certdata.CERT1])
        import plistlib
        raw = plistlib.loads(data[data.index(b"<?xml"):data.rindex(b"</plist>") + 8])
        del raw["Platform"]
        self.assertEqual(ic.parse_profile(plistlib.dumps(raw)).platforms, ("iOS",))      # a bare property list is read too

    def test_binary_property_list_is_read(self):
        import plistlib
        data = fx.profile_bytes([certdata.CERT1])
        raw = plistlib.loads(data[data.index(b"<?xml"):data.rindex(b"</plist>") + 8])
        self.assertEqual(ic.parse_profile(plistlib.dumps(raw, fmt=plistlib.FMT_BINARY)).team_id, "ABCDE12345")

    def test_files_that_are_not_profiles(self):
        import plistlib
        for junk in (b"", b"\x00" * 200, b"hello", b"<?xml version='1.0'?><plist><dict/></plist>",
                     plistlib.dumps({"Entitlements": {"application-identifier": "A.b"}}),          # no expiry date
                     plistlib.dumps({"ExpirationDate": datetime.datetime(2099, 1, 1)}),               # no entitlements
                     plistlib.dumps([1, 2]), b"x" * (ic.MAX_PROFILE + 1)):
            self.assertEqual(code_of(ic.parse_profile, junk), "cert_profile_invalid", junk[:30])

    def test_udids_are_compared_without_case_or_dashes(self):
        self.assertEqual(ic.norm_udid("00008110-001A2B3C4D5E6F78"), ic.norm_udid("00008110001a2b3c4d5e6f78"))
        self.assertEqual(ic.norm_udid(None), "")

    def test_the_platform_a_device_needs(self):
        self.assertEqual([ic.platform_needed(f) for f in ("tvos", "ios", "ipados", None)], ["tvOS", "iOS", "iOS", "tvOS"])


class BundlePatterns(unittest.TestCase):
    def test_what_a_profile_covers(self):
        table = [("*", "com.any.thing", True), ("com.example.*", "com.example.app", True), ("com.example.*", "com.example.", True),
                 ("com.example.*", "com.example", False), ("com.example.*", "com.examples.app", False),
                 ("com.example.app", "com.example.app", True), ("com.example.app", "com.example.app2", False),
                 ("com.example.app", "COM.EXAMPLE.APP", False), ("", "com.example.app", False), ("*", None, False)]
        for pattern, bundle, covered in table:
            self.assertIs(ic.pattern_matches(pattern, bundle), covered, (pattern, bundle))

    def test_app_problem_explains_why(self):
        app = {"bundle_id": "com.other.app", "warnings": ""}
        self.assertEqual(ic.app_problem("com.example.*", app), "cert_bundle_mismatch:com.example.*:com.other.app")
        self.assertIsNone(ic.app_problem("*", app))
        self.assertIsNone(ic.app_problem("com.example.app", {"bundle_id": None}))     # identifier not known yet

    def test_extensions_need_a_wildcard(self):
        app = {"bundle_id": "com.example.app", "warnings": "push,appex:2"}
        self.assertEqual(ic.app_problem("com.example.app", app), "cert_needs_wildcard:com.example.app")
        self.assertIsNone(ic.app_problem("com.example.*", app))
        self.assertIsNone(ic.app_problem("com.example.app", {"bundle_id": "com.example.app", "warnings": "push"}))


# ---------- les contrôles avant d'enregistrer ----------

class Checks(unittest.TestCase):
    def check(self, prof=None, cert=None, device=None, app=None):
        return ic.check(prof or profile(), cert or cert_info(), device or device_row(), app, NOW)

    def test_a_good_pair_passes(self):
        self.assertEqual(self.check(), [])

    def test_expired_and_not_yet_valid_certificate(self):
        self.assertEqual(code_of(self.check, cert=cert_info(not_after=NOW - 5 * DAY)), "cert_expired:" + ic._day(NOW - 5 * DAY))
        self.assertTrue(code_of(self.check, cert=cert_info(not_before=NOW + 5 * DAY, not_after=NOW + 300 * DAY))
                        .startswith("cert_not_yet_valid:"))

    def test_expired_profile(self):
        self.assertTrue(code_of(self.check, prof=profile(days=-2)).startswith("cert_profile_expired:"))

    def test_the_certificate_must_be_one_of_the_profile(self):
        self.assertEqual(code_of(self.check, prof=profile(certdata.CERT2)), "cert_not_in_profile")
        self.assertEqual(code_of(self.check, prof=profile(certdata.CERT1, certdata.CERT2)), "ok")

    def test_the_platform_must_fit_the_device(self):
        self.assertEqual(code_of(self.check, prof=profile(platform=("iOS",))), "cert_platform:iOS:tvOS")
        self.assertEqual(code_of(self.check, prof=profile(platform=("tvOS",)), device=device_row(family="ipados")),
                         "cert_platform:tvOS:iOS")
        for family in ("ios", "ipados"):
            self.assertEqual(code_of(self.check, prof=profile(platform=("iOS",)), device=device_row(family=family)), "ok")
        self.assertEqual(code_of(self.check, prof=profile(platform=("iOS", "tvOS"))), "ok")
        self.assertEqual(code_of(self.check, prof=profile(platform=("tvos",))), "ok")         # the case does not matter

    def test_the_device_must_be_in_the_profile(self):
        self.assertEqual(code_of(self.check, prof=profile(udids=["00008110-FFFFFFFFFFFFFFFF"])), "cert_device_not_in_profile")
        # same UDID written without its dash, in lower case
        self.assertEqual(code_of(self.check, device=device_row(udid="00008110001a2b3c4d5e6f78")), "ok")
        # the UDID of the pairing counts too (it is what some Apple TVs are known by before their first read)
        self.assertEqual(code_of(self.check, device=device_row(udid=None, pairing_udid=fx.UDID)), "ok")
        self.assertEqual(code_of(self.check, device=device_row(udid=None)), "cert_device_unknown")

    def test_enterprise_profile_fits_any_device(self):
        enterprise = profile(device_list=False, all_devices=True)
        self.assertEqual(self.check(prof=enterprise, device=device_row(udid="whatever-1")), [])
        phone_profile = profile(device_list=False, all_devices=True, platform=("iOS",))
        self.assertEqual(self.check(prof=phone_profile, device=device_row(family="ios")), ["trust"])

    def test_a_profile_without_device_list_is_not_an_install_profile(self):
        self.assertEqual(code_of(self.check, prof=profile(device_list=False)), "cert_profile_not_for_devices")

    def test_the_app_identifier_must_be_covered(self):
        app = {"bundle_id": "com.example.app", "warnings": ""}
        self.assertEqual(code_of(self.check, app=app), "ok")                                     # wildcard
        exact = profile(app_id="ABCDE12345.com.example.other")
        self.assertEqual(code_of(self.check, prof=exact, app=app), "cert_bundle_mismatch:com.example.other:com.example.app")
        self.assertEqual(code_of(self.check, prof=exact), "ok")                                  # no app given: nothing to compare yet
        self.assertEqual(code_of(self.check, prof=profile(app_id="ABCDE12345.com.example.*"), app=app), "ok")

    def test_warnings(self):
        self.assertEqual(self.check(prof=profile(days=20)), ["short"])
        self.assertEqual(self.check(cert=cert_info(not_after=NOW + 10 * DAY)), ["short"])


# ---------- les fichiers : .p12 ----------

@needs_crypto
class P12(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key, cls.cert = fx.make_cert()

    def test_reads_the_pair_plumesign_expects(self):
        m = ic.read_p12(fx.make_p12(self.key, self.cert, "pw"), "pw")
        self.assertTrue(m.key_pem.startswith(b"-----BEGIN PRIVATE KEY-----"))          # PKCS#8, a tag plumesign reads
        self.assertTrue(m.cert_pem.startswith(b"-----BEGIN CERTIFICATE-----"))
        self.assertEqual(m.info.der, fx.der(self.cert))
        self.assertEqual(m.info.subject, f"Apple Development: Test ({fx.TEAM})")
        self.assertEqual(m.info.serial, ipa.cert_serial(fx.der(self.cert)))               # the form apps.cert_serial uses
        self.assertAlmostEqual(m.info.not_after, NOW + 300 * DAY, delta=60)
        self.assertEqual(m.info.key_kind, "rsa")

    def test_wrong_and_missing_passwords(self):
        data = fx.make_p12(self.key, self.cert, "pw")
        self.assertEqual(code_of(ic.read_p12, data, "nope"), "cert_password_wrong")
        self.assertEqual(code_of(ic.read_p12, data, ""), "cert_password_wrong")
        self.assertEqual(code_of(ic.read_p12, data, "x" * 1000), "cert_password_wrong")
        self.assertEqual(code_of(ic.read_p12, data, "pw"), "ok")

    def test_a_p12_without_password(self):
        data = fx.make_p12(self.key, self.cert, None)
        self.assertEqual(code_of(ic.read_p12, data, ""), "ok")

    def test_non_ascii_password(self):
        data = fx.make_p12(self.key, self.cert, "mot de passe é€")
        self.assertEqual(code_of(ic.read_p12, data, "mot de passe é€"), "ok")

    def test_legacy_encryption_of_paid_services_and_old_exports(self):
        # RC2-40 for the certificate and 3DES for the key: what OpenSSL 1.x and the macOS Keychain write
        m = ic.read_p12(certdata.LEGACY_P12, certdata.LEGACY_P12_PASSWORD)
        self.assertEqual(m.info.subject, "Legacy Test Signer")
        self.assertEqual(code_of(ic.read_p12, certdata.LEGACY_P12, "autre"), "cert_password_wrong")

    def test_files_that_are_not_p12(self):
        for junk in (b"", b"hello", self.cert.public_bytes(fx.serialization.Encoding.PEM), os.urandom(500),
                     b"\x30\x03\x02\x01\x01", b"x" * (ic.MAX_P12 + 1)):
            self.assertEqual(code_of(ic.read_p12, junk, "pw"), "cert_p12_invalid", junk[:20])

    def test_no_key_or_no_certificate_inside(self):
        no_key = fx.pkcs12.serialize_key_and_certificates(b"x", None, self.cert, None, fx.serialization.NoEncryption())
        self.assertEqual(code_of(ic.read_p12, no_key, ""), "cert_p12_no_key")
        no_cert = fx.pkcs12.serialize_key_and_certificates(b"x", self.key, None, None, fx.serialization.NoEncryption())
        self.assertEqual(code_of(ic.read_p12, no_cert, ""), "cert_p12_no_cert")

    def test_key_types(self):
        key, cert = fx.make_cert(ec_key=True)
        m = ic.read_p12(fx.make_p12(key, cert, "pw"), "pw")
        self.assertEqual(m.info.key_kind, "ec")
        self.assertIn(b"BEGIN PRIVATE KEY", m.key_pem)
        key384 = fx.ec.generate_private_key(fx.ec.SECP384R1())
        _, cert384 = fx.make_cert(key=key384)
        self.assertEqual(code_of(ic.read_p12, fx.make_p12(key384, cert384, "pw"), "pw"), "cert_key_unsupported")

    def test_the_chain_is_kept_for_the_revocation_check(self):
        ca = fx.make_ca()
        key, cert = fx.make_cert(ca=ca)
        m = ic.read_p12(fx.make_p12(key, cert, "pw", chain=[ca[1]]), "pw")
        self.assertEqual(m.chain, [fx.der(ca[1])])
        self.assertEqual(m.info.der, fx.der(cert))                  # the signing certificate, not the CA


@needs_crypto
class Prepare(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key, cls.cert = fx.make_cert()
        cls.p12 = fx.make_p12(cls.key, cls.cert, "pw")
        cls.app = {"bundle_id": "com.example.app", "warnings": ""}

    def profile(self, **kw):
        return fx.profile_bytes([fx.der(self.cert)], **kw)

    def test_reads_and_checks_both_files(self):
        prep = ic.prepare(device_row(), self.p12, "pw", self.profile(), self.app)
        self.assertEqual(prep.name, f"Apple Development: Test ({fx.TEAM})")
        self.assertEqual(prep.warnings, [])
        self.assertAlmostEqual(prep.expires_at, NOW + 300 * DAY, delta=60)       # the earlier of the certificate and the profile

    def test_expiry_is_the_earlier_of_certificate_and_profile(self):
        self.assertAlmostEqual(ic.prepare(device_row(), self.p12, "pw", self.profile(days=100)).expires_at, NOW + 100 * DAY, delta=60)
        _, short = fx.make_cert(days=40, key=self.key)
        p12 = fx.make_p12(self.key, short, "pw")
        prep = ic.prepare(device_row(), p12, "pw", fx.profile_bytes([fx.der(short)], days=200))
        self.assertAlmostEqual(prep.expires_at, NOW + 40 * DAY, delta=60)

    def test_each_problem_has_its_code(self):
        d = device_row()
        self.assertEqual(code_of(ic.prepare, d, self.p12, "bad", self.profile()), "cert_password_wrong")
        self.assertEqual(code_of(ic.prepare, d, self.p12, "pw", b"not a profile"), "cert_profile_invalid")
        self.assertTrue(code_of(ic.prepare, d, self.p12, "pw", self.profile(days=-1)).startswith("cert_profile_expired:"))
        self.assertEqual(code_of(ic.prepare, d, self.p12, "pw", self.profile(platform=("iOS",))), "cert_platform:iOS:tvOS")
        self.assertEqual(code_of(ic.prepare, d, self.p12, "pw", self.profile(udids=["0000-1"])), "cert_device_not_in_profile")
        self.assertEqual(code_of(ic.prepare, d, self.p12, "pw", fx.profile_bytes([certdata.CERT2])), "cert_not_in_profile")
        self.assertEqual(code_of(ic.prepare, d, self.p12, "pw", self.profile(app_id=f"{fx.TEAM}.com.other"), self.app),
                         "cert_bundle_mismatch:com.other:com.example.app")

    def test_the_password_error_comes_first(self):
        self.assertEqual(code_of(ic.prepare, device_row(), self.p12, "bad", b"junk"), "cert_password_wrong")


# ---------- ce que dit l'appareil quand il refuse une installation ----------

class InstallFailures(unittest.TestCase):
    CASES = [
        ("Failed to verify code signature of /var/installd/x.app : 0xe8008018 (The identity used to sign the executable "
         "is no longer valid.)", "revoked"),
        ("ApplicationVerificationFailed: The identity used to sign the executable is no longer valid", "revoked"),
        ("CSSMERR_TP_CERT_REVOKED", "revoked"),
        ("install : the certificate has been revoked", "revoked"),
        ("0xe8008015 (A valid provisioning profile for this executable was not found.)", "no_profile"),
        ("The provisioning profile has expired", "profile_expired"),
        ("0xe8008016 (The executable was signed with invalid entitlements.)", "entitlements"),
        ("MismatchedApplicationIdentifierEntitlement", "identifier"),
        ("The application-identifier entitlement did not match that specified in the provisioning profile", "identifier"),
        ("installd ne progresse plus depuis 30 min", None),
        ("This device has reached the maximum number of installed apps using a free developer profile", None),
        ("", None),
    ]

    def test_classification(self):
        for text, kind in self.CASES:
            self.assertEqual(ic.classify_install_failure(text), kind, text)

    def test_the_problem_to_show(self):
        ident = {"name": "Mon certificat", "id": 3}
        revoked = ic.explain_install_failure(self.CASES[0][0], ident)
        self.assertIsInstance(revoked, ic.CertProblem)
        self.assertEqual((revoked.code, revoked.kind), ("revoked", "config"))
        self.assertIn("révoqué", str(revoked))
        self.assertIsNone(ic.explain_install_failure("disque plein", ident))
        for text, kind in self.CASES:
            if kind:
                self.assertIn("importé", str(ic.explain_install_failure(text, ident)), kind)

    def test_expiry_levels(self):
        self.assertEqual([ic.expiry_level(s) for s in (8 * DAY, 7 * DAY, 7 * DAY - 1, DAY, DAY - 1, 1, 0, -5)],
                         [None, None, "7d", "7d", "1d", "1d", "expired", "expired"])


# ---------- aides pour les tests qui touchent la base ----------

CRYPTO = fx.HAVE_CRYPTO and ic.AVAILABLE
_PAIR: dict = {}


def shared_pair():
    """Une clé et un certificat RSA créés une fois pour toute la suite (générer une clé est lent)."""
    if not _PAIR:
        _PAIR["key"], _PAIR["cert"] = fx.make_cert()
    return _PAIR["key"], _PAIR["cert"]


def add_device(**over) -> dict:
    cols = dict(name="Salon", pair_identifier=f"pid-{time.time_ns()}", udid=fx.UDID, pairing_udid=fx.UDID, os_version="18.0",
                family="tvos", created_at=0)
    cols.update(over)
    new_id = db.execute(f"INSERT INTO devices ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", *cols.values())
    return db.row("SELECT * FROM devices WHERE id = ?", new_id)


def store_identity(device: dict, days: float = 300, **profile_kw) -> dict:
    """Importe pour de bon (p12 et profil réels, fichier scellé dans le dossier de données de test). Exige cryptography."""
    key, cert = shared_pair()
    udids = profile_kw.pop("udids", (device.get("udid") or fx.UDID,))
    prep = ic.prepare(device, fx.make_p12(key, cert, "pw"), "pw", fx.profile_bytes([fx.der(cert)], days=days, udids=udids,
                                                                                   **profile_kw))
    return ic.commit(prep, device["id"])


def fake_identity(device: dict, days: float = 300, app_id: str | None = None, udids=None, platform=None, **over) -> dict:
    """Une fiche de certificat avec un fichier scellé factice : pour les tests du choix, du calendrier et des alertes, qui
    n'ouvrent pas le fichier (aucun paquet cryptography requis). Mêmes paramètres que store_identity()."""
    now = time.time()
    fields = dict(device_id=device["id"], name="Apple Development: Test (ABCDE12345)", subject="Apple Development: Test",
                  team_id=fx.TEAM, team_name="Test Team", cert_serial="A1B2C", cert_sha1="00" * 20, cert_not_after=now + days * DAY,
                  profile_uuid="u", profile_name="Test profile", profile_app_id=app_id.split(".", 1)[1] if app_id else "*",
                  profile_platforms=",".join(platform or (ic.platform_needed(device.get("family")),)),
                  profile_expires=now + days * DAY, all_devices=0, expires_at=now + days * DAY, created_at=now, updated_at=now)
    fields.update(over)
    ident_id = db.execute(f"INSERT INTO imported_certs ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})", *fields.values())
    (config.CERT_DIR / f"{ident_id}.bin").write_bytes(b"TVC1" + b"\0" * 40)      # there, but not a real sealed file
    return ic.row(ident_id)


def make_identity(device: dict, days: float = 300, **kw) -> dict:
    """Un vrai certificat importé si cryptography est là, sinon une fiche factice : les tests du choix et du calendrier
    tournent dans les deux cas."""
    return store_identity(device, days, **kw) if CRYPTO else fake_identity(device, days, **kw)


@contextlib.contextmanager
def fake_signing_files(ident_id):
    folder = Path(tempfile.mkdtemp(prefix="cert-", dir=config.TMP_DIR))
    try:
        files = SimpleNamespace(cert=folder / "cert.pem", key=folder / "key.pem", profile=folder / "profile.mobileprovision")
        files.cert.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        files.key.write_text("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n")
        files.profile.write_bytes(b"ProvisionedDevices")
        yield files
    finally:
        shutil.rmtree(folder, ignore_errors=True)


class CertBase(Base):
    """État propre : aucun certificat, aucun accès réseau, notifications relevées au lieu d'être envoyées."""

    def setUp(self):
        super().setUp()
        db.execute("DELETE FROM imported_certs")
        for path in config.CERT_DIR.glob("*"):
            path.unlink(missing_ok=True)
        renew._IDENT_LOGGED.clear()
        self.sent = []
        self.deliver = True

        async def notify(message, priority="default", event="fail", **extra):
            self.sent.append((message, priority, event, extra))
            return self.deliver

        for patch in (mock.patch.object(ic, "_fetch", side_effect=OSError("hors ligne")), mock.patch.object(renew, "notify", notify)):
            patch.start()
            self.addCleanup(patch.stop)

    def app_of(self, app_id):
        return db.row("SELECT * FROM apps WHERE id = ?", app_id)


# ---------- le stockage scellé ----------

@needs_crypto
class Vault(CertBase):
    def test_a_sealed_blob_hides_its_content_and_opens_again(self):
        blob = ic.seal(7, {"secret": "valeur-secrete"})
        self.assertNotIn(b"valeur-secrete", blob)
        self.assertEqual(ic.unseal(7, blob), {"secret": "valeur-secrete"})

    def test_a_sealed_file_is_bound_to_its_number_and_to_its_content(self):
        blob = ic.seal(7, {"a": 1})
        for wrong in (lambda: ic.unseal(8, blob),                                       # copied under another number
                      lambda: ic.unseal(7, blob[:-1] + bytes([blob[-1] ^ 1])),          # one bit changed
                      lambda: ic.unseal(7, b"XXXX" + blob[4:]),                         # not ours
                      lambda: ic.unseal(7, b"")):
            with self.assertRaises((ic.InvalidTag, ValueError)):
                wrong()

    def test_each_seal_uses_a_new_nonce(self):
        self.assertNotEqual(ic.seal(1, {"a": 1}), ic.seal(1, {"a": 1}))

    def test_the_key_lives_in_a_private_file_of_its_own(self):
        ic.seal(1, {})
        key_file = config.DATA_DIR / "vault.key"
        self.assertEqual(len(bytes.fromhex(key_file.read_text().strip())), 32)
        if POSIX:
            self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(key_file, config.DATA_DIR / "secret.key")                 # signing everyone out must not lose the certificates

    def test_a_lost_key_is_reported_not_guessed(self):
        ident = store_identity(add_device())
        with mock.patch.object(ic, "_KEY", os.urandom(32)):
            with self.assertRaises(ic.CertProblem) as cm:
                ic.read_material(ident["id"])
        self.assertEqual(cm.exception.code, "missing")
        self.assertIn("importe-le de nouveau", str(cm.exception))

    def test_stored_file_holds_no_plain_secret_and_is_owner_only(self):
        ident = store_identity(add_device())
        path = config.CERT_DIR / f"{ident['id']}.bin"
        raw = path.read_bytes()
        payload = ic.read_material(ident["id"])
        for needle in (b"PRIVATE KEY", b"BEGIN CERTIFICATE", payload["key_pem"].splitlines()[1].encode(), b"Test profile",
                       b"ProvisionedDevices"):
            self.assertNotIn(needle, raw)
        if POSIX:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(raw.startswith(b"TVC1"))

    def test_binary_content_survives_the_write(self):
        # 0x0A bytes must stay one byte on Windows too (the file is opened in binary mode)
        from app.util import write_private
        target = config.DATA_DIR / "binary-check.bin"
        data = bytes(range(256)) * 4
        write_private(target, data)
        self.assertEqual(target.read_bytes(), data)
        target.unlink()

    def test_signing_files_exist_only_during_the_signature(self):
        ident = store_identity(add_device())
        before = set(config.TMP_DIR.glob("cert-*"))
        with ic.signing_files(ident["id"]) as files:
            self.assertTrue(files.cert.read_text().startswith("-----BEGIN CERTIFICATE-----"))
            self.assertTrue(files.key.read_text().startswith("-----BEGIN PRIVATE KEY-----"))
            self.assertIn(b"ProvisionedDevices", files.profile.read_bytes())
            folder = files.key.parent
            self.assertTrue(folder.is_dir())
            if POSIX:
                self.assertEqual(files.key.stat().st_mode & 0o777, 0o600)
        self.assertFalse(folder.exists())
        self.assertEqual(set(config.TMP_DIR.glob("cert-*")), before)

    def test_signing_files_are_removed_when_the_signature_fails(self):
        ident = store_identity(add_device())
        with self.assertRaises(RuntimeError):
            with ic.signing_files(ident["id"]) as files:
                folder = files.key.parent
                raise RuntimeError("plumesign a échoué")
        self.assertFalse(folder.exists())

    def test_missing_file_is_a_problem_to_fix(self):
        with self.assertRaises(ic.CertProblem) as cm:
            with ic.signing_files(424242):
                pass
        self.assertEqual(cm.exception.code, "missing")

    def test_an_interrupted_write_is_cleaned_up_at_startup(self):
        (config.CERT_DIR / "5.bin.tmp").write_bytes(b"half a file")
        ic.settle()
        self.assertFalse((config.CERT_DIR / "5.bin.tmp").exists())

    def test_startup_removes_what_a_killed_signature_left(self):
        leftover = config.TMP_DIR / "cert-killed"
        leftover.mkdir(parents=True, exist_ok=True)
        (leftover / "key.pem").write_text("clé")
        ic.sweep_temp()
        self.assertFalse(leftover.exists())

    def test_settle_seals_backup_files_and_drops_orphans(self):
        ident = store_identity(add_device())
        payload = ic.read_material(ident["id"])
        (config.CERT_DIR / f"{ident['id']}.bin").unlink()
        (config.CERT_DIR / f"{ident['id']}.json").write_text(json.dumps(payload))        # what a backup carries
        (config.CERT_DIR / "999.json").write_text(json.dumps(payload))                    # no such certificate
        (config.CERT_DIR / "998.bin").write_bytes(b"orphan")
        (config.CERT_DIR / "bad.json").write_text("{")
        ic.settle()
        self.assertEqual(sorted(p.name for p in config.CERT_DIR.iterdir()), [f"{ident['id']}.bin"])
        self.assertEqual(ic.read_material(ident["id"]), payload)


# ---------- les fiches, le choix du certificat, les états ----------

class Store(CertBase):
    def setUp(self):
        super().setUp()
        self.tv = add_device()
        self.app = {"bundle_id": "com.example.tv", "warnings": ""}

    def test_state(self):
        ident = make_identity(self.tv)
        self.assertEqual(ic.state_of(ident, NOW), "ok")
        self.assertEqual(ic.state_of({**ident, "expires_at": NOW + 3 * DAY}, NOW), "expiring")
        self.assertEqual(ic.state_of({**ident, "expires_at": NOW - 1}, NOW), "expired")
        self.assertEqual(ic.state_of({**ident, "revoked_at": NOW}, NOW), "revoked")
        (config.CERT_DIR / f"{ident['id']}.bin").unlink()
        self.assertEqual(ic.state_of(ident, NOW), "missing")
        view = ic.view(ident, NOW)
        self.assertEqual((view["state"], view["has_material"], view["platforms"], view["wildcard"]), ("missing", False, ["tvOS"], True))

    def test_a_certificate_in_use_cannot_be_removed(self):
        ident = make_identity(self.tv)
        app_id = self.add_app("Nuvio", device_id=self.tv["id"], sign_mode="certificate", cert_id=ident["id"])
        self.assertEqual(ic.names_using(ident["id"]), ["Nuvio"])
        self.assertEqual(code_of(ic.remove, ident["id"]), "cert_in_use:Nuvio")
        self.assertTrue(ic.has_material(ident["id"]))
        db.update("apps", app_id, sign_mode="apple_id")            # back to the Apple ID: nothing uses it any more
        ic.remove(ident["id"])
        self.assertIsNone(ic.row(ident["id"]))
        self.assertFalse(ic.has_material(ident["id"]))

    def test_removing_a_device_removes_its_certificates(self):
        ident = make_identity(self.tv)
        app_id = self.add_app("Nuvio", device_id=self.tv["id"], sign_mode="certificate", cert_id=ident["id"])
        ic.forget_device(self.tv["id"])
        self.assertEqual(ic.rows(), [])
        self.assertFalse((config.CERT_DIR / f"{ident['id']}.bin").exists())
        self.assertIsNone(self.app_of(app_id)["cert_id"])

    def test_why_a_certificate_cannot_sign_an_app(self):
        ident = make_identity(self.tv)
        tv, app = self.tv, self.app
        self.assertIsNone(ic.problem_for(ident, tv, app, NOW))
        cases = [
            ("none chosen", None, tv, app, "cert_none"),
            ("revoked", {**ident, "revoked_at": NOW - 5}, tv, app, "cert_revoked"),
            ("certificate expired", {**ident, "cert_not_after": NOW - DAY}, tv, app, "cert_expired:" + ic._day(NOW - DAY)),
            ("profile expired", {**ident, "profile_expires": NOW - DAY}, tv, app, "cert_profile_expired:" + ic._day(NOW - DAY)),
            ("file gone", {**ident, "id": 9999}, tv, app, "cert_missing"),
            ("another device", ident, {**tv, "id": tv["id"] + 1}, app, "cert_wrong_device"),
            ("another platform", ident, {**tv, "family": "ios"}, app, "cert_platform:tvOS:iOS"),
            ("identifier not covered", {**ident, "profile_app_id": "com.example.other"}, tv, app,
             "cert_bundle_mismatch:com.example.other:com.example.tv"),
            ("extensions", {**ident, "profile_app_id": "com.example.tv"}, tv, {**app, "warnings": "appex:1"},
             "cert_needs_wildcard:com.example.tv"),
        ]
        for label, i, d, a, want in cases:
            got = ic.problem_for(i, d, a, NOW)
            self.assertEqual(got and got[0], want, label)
            self.assertTrue(got[1], label)              # French sentence shown on the card
        self.assertIsNone(ic.problem_for({**ident, "profile_app_id": "com.example.*"}, tv, app, NOW))

    def test_require_raises_a_problem_for_the_card(self):
        ident = make_identity(self.tv)
        app = {"bundle_id": "com.example.tv", "warnings": "", "cert_id": ident["id"]}
        self.assertEqual(ic.require(app, self.tv)["id"], ident["id"])
        db.update("imported_certs", ident["id"], revoked_at=NOW)
        with self.assertRaises(ic.CertProblem) as cm:
            ic.require(app, self.tv)
        self.assertEqual(cm.exception.code, "cert_revoked")
        with self.assertRaises(ic.CertProblem):
            ic.require({**app, "cert_id": None}, self.tv)

    def test_pick_takes_the_usable_one_that_lasts_longest(self):
        short = make_identity(self.tv, days=100)
        long = make_identity(self.tv, days=250)
        other_app = make_identity(self.tv, days=300, app_id=f"{fx.TEAM}.com.example.other")        # does not cover the app
        elsewhere = add_device(name="Chambre", udid="AAAA-0000", pairing_udid="AAAA-0000")
        make_identity(elsewhere, days=300, udids=("AAAA-0000",))                                    # another device
        self.assertEqual(ic.pick(self.tv, self.app, NOW)["id"], long["id"])
        db.update("imported_certs", long["id"], revoked_at=NOW)
        self.assertEqual(ic.pick(self.tv, self.app, NOW)["id"], short["id"])
        db.update("imported_certs", short["id"], expires_at=NOW + 50 * DAY, cert_not_after=NOW - 1)
        self.assertIsNone(ic.pick(self.tv, self.app, NOW))
        self.assertEqual(ic.pick(self.tv, {"bundle_id": "com.example.other", "warnings": ""}, NOW)["id"], other_app["id"])

    def test_record_revoked_tells_once(self):
        ident = make_identity(self.tv)
        self.assertTrue(ic.record_revoked(ident["id"], "OCSP : key_compromise"))
        self.assertFalse(ic.record_revoked(ident["id"], "again"))
        row = ic.row(ident["id"])
        self.assertEqual((row["revoked_reason"], row["check_state"]), ("OCSP : key_compromise", "revoked"))
        self.assertFalse(ic.record_revoked(424242, "nobody"))

    def test_record_check_keeps_the_last_conclusion_on_an_unknown_answer(self):
        ident = make_identity(self.tv)
        ic.record_check(ident["id"], ic.Revocation("good"), 1000.0)
        self.assertEqual((ic.row(ident["id"])["check_state"], ic.row(ident["id"])["checked_at"]), ("good", 1000.0))
        ic.record_check(ident["id"], ic.Revocation("unknown", "hors ligne"), 2000.0)
        self.assertEqual((ic.row(ident["id"])["check_state"], ic.row(ident["id"])["checked_at"]), ("good", 2000.0))
        ic.record_check(ident["id"], ic.Revocation("revoked", "key_compromise", 1500.0), 3000.0)
        row = ic.row(ident["id"])
        self.assertEqual((row["check_state"], row["revoked_at"], row["checked_at"]), ("revoked", 1500.0, 3000.0))

    def test_expiry_of_a_signed_app_is_the_earlier_of_profile_and_certificate(self):
        ident = make_identity(self.tv, days=300)
        signed = config.TMP_DIR / "expiry-check.ipa"
        try:
            signed.write_bytes(make_signed(NOW + 100 * DAY))
            self.assertAlmostEqual(ic.signature_expiry(ident, signed), NOW + 100 * DAY, delta=5)
            signed.write_bytes(make_signed(NOW + 900 * DAY))
            self.assertAlmostEqual(ic.signature_expiry(ident, signed), NOW + 300 * DAY, delta=60)
            signed.write_bytes(b"not an ipa")
            self.assertAlmostEqual(ic.signature_expiry(ident, signed), NOW + 300 * DAY, delta=60)
        finally:
            signed.unlink(missing_ok=True)


@needs_crypto
class StoreImport(CertBase):
    def setUp(self):
        super().setUp()
        self.tv = add_device()

    def test_import_stores_the_facts_and_no_secret_in_the_row(self):
        ident = store_identity(self.tv)
        self.assertEqual((ident["device_id"], ident["team_id"], ident["profile_app_id"], ident["profile_platforms"]),
                         (self.tv["id"], fx.TEAM, "*", "tvOS"))
        self.assertEqual(ident["cert_serial"], ipa.cert_serial(fx.der(shared_pair()[1])))
        self.assertAlmostEqual(ident["expires_at"], NOW + 300 * DAY, delta=60)
        self.assertNotIn("PRIVATE KEY", json.dumps(ident))
        self.assertTrue(ic.has_material(ident["id"]))
        self.assertEqual(ic.state_of(ident, NOW), "ok")

    def test_replacing_keeps_the_number_and_clears_a_revocation(self):
        ident = store_identity(self.tv)
        ic.record_revoked(ident["id"], "test")
        db.update("imported_certs", ident["id"], notified_level="revoked")
        key, cert = shared_pair()
        prep = ic.prepare(self.tv, fx.make_p12(key, cert, "pw"), "pw", fx.profile_bytes([fx.der(cert)], days=100))
        again = ic.commit(prep, self.tv["id"], replace_id=ident["id"])
        self.assertEqual(again["id"], ident["id"])
        self.assertEqual((again["revoked_at"], again["notified_level"], again["check_state"]), (None, None, None))
        self.assertAlmostEqual(again["expires_at"], NOW + 100 * DAY, delta=60)
        self.assertEqual(len(ic.rows()), 1)

    def test_a_failed_write_leaves_no_row_behind(self):
        key, cert = shared_pair()
        prep = ic.prepare(self.tv, fx.make_p12(key, cert, "pw"), "pw", fx.profile_bytes([fx.der(cert)]))
        with mock.patch.object(ic, "write_material", side_effect=OSError("disque plein")):
            with self.assertRaises(OSError):
                ic.commit(prep, self.tv["id"])
        self.assertEqual(ic.rows(), [])


# ---------- la révocation : OCSP ----------

@needs_crypto
class Ocsp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ca = fx.make_ca()
        cls.key, cls.cert = fx.make_cert(ca=cls.ca)
        cls.der = fx.der(cls.cert)
        cls.chain = [fx.der(cls.ca[1])]

    def answer(self, status="good", **kw):
        return fx.ocsp_answer(self.cert, self.ca[0], self.ca[1], status, **kw)

    @staticmethod
    def fetcher(answer=None, issuer=None, fail=None):
        calls = []

        def fetch(url, body=None):
            calls.append((url, body))
            if fail:
                raise fail
            return issuer if body is None else answer
        fetch.calls = calls
        return fetch

    def test_a_signed_good_answer(self):
        fetch = self.fetcher(self.answer())
        r = ic.check_revocation(self.der, self.chain, NOW, fetch)
        self.assertEqual((r.state, r.detail), ("good", ""))
        url, body = fetch.calls[0]
        self.assertEqual(url, fx.OCSP_URL)
        request = fx.ocsp.load_der_ocsp_request(body)                  # a real OCSP request about this certificate
        self.assertEqual(request.serial_number, self.cert.serial_number)

    def test_a_revoked_certificate(self):
        r = ic.check_revocation(self.der, self.chain, NOW, self.fetcher(self.answer("revoked", revoked_days_ago=3)))
        self.assertEqual((r.state, r.detail), ("revoked", "key_compromise"))
        self.assertAlmostEqual(r.at, NOW - 3 * DAY, delta=120)

    def test_the_issuer_is_downloaded_when_the_p12_has_no_chain(self):
        fetch = self.fetcher(self.answer(), issuer=fx.der(self.ca[1]))
        r = ic.check_revocation(self.der, [], NOW, fetch)
        self.assertEqual(r.state, "good")
        self.assertEqual([c[0] for c in fetch.calls], [fx.ISSUER_URL, fx.OCSP_URL])

    def test_a_responder_certificate_signed_by_the_issuer_is_accepted(self):
        responder = fx.ocsp_responder_cert(*self.ca)
        r = ic.check_revocation(self.der, self.chain, NOW, self.fetcher(self.answer("revoked", responder=responder)))
        self.assertEqual(r.state, "revoked")

    def test_nothing_is_concluded_from_a_doubtful_answer(self):
        other_ca = fx.make_ca("Another CA")
        stranger = fx.ocsp_responder_cert(*other_ca)
        _, other_cert = fx.make_cert(ca=self.ca)
        cases = {
            "signed by someone else": fx.ocsp_answer(self.cert, other_ca[0], other_ca[1], "revoked"),
            "responder of another CA": self.answer("revoked", responder=stranger),
            "about another certificate": fx.ocsp_answer(other_cert, self.ca[0], self.ca[1], "revoked"),
            "stale": self.answer("revoked", this_update_days=-3, next_update_days=-1),
            "from the future": self.answer("revoked", this_update_days=2, next_update_days=3),
            "unknown to the responder": self.answer("unknown"),
            "refused": fx.ocsp.OCSPResponseBuilder.build_unsuccessful(fx.ocsp.OCSPResponseStatus.UNAUTHORIZED)
                .public_bytes(fx.serialization.Encoding.DER),
            "garbage": b"<html>not found</html>",
        }
        for label, data in cases.items():
            r = ic.check_revocation(self.der, self.chain, NOW, self.fetcher(data))
            self.assertEqual(r.state, "unknown", label)
            self.assertTrue(r.detail, label)

    def test_an_unreachable_responder_is_not_an_alarm(self):
        for error in (OSError("hors ligne"), TimeoutError(), ValueError("réponse")):
            r = ic.check_revocation(self.der, self.chain, NOW, self.fetcher(fail=error))
            self.assertEqual(r.state, "unknown", repr(error))

    def test_no_responder_address(self):
        _, plain = fx.make_cert()
        fetch = self.fetcher(self.answer())
        self.assertEqual(ic.check_revocation(fx.der(plain), self.chain, NOW, fetch).state, "unknown")     # self-signed: no AIA
        self.assertEqual(fetch.calls, [])

    def test_only_apple_addresses_are_ever_contacted(self):
        _, evil = fx.make_cert(ca=self.ca, ocsp_url="http://127.0.0.1:9/ocsp", issuer_url="http://127.0.0.1:9/ca.cer")
        fetch = self.fetcher(self.answer())
        r = ic.check_revocation(fx.der(evil), self.chain, NOW, fetch)
        self.assertEqual((r.state, fetch.calls), ("unknown", []))
        with self.assertRaises(OSError):
            ic._fetch("http://127.0.0.1:9/x")

    def test_without_the_package_nothing_is_concluded(self):
        with mock.patch.object(ic, "AVAILABLE", False):
            self.assertEqual(ic.check_revocation(self.der, self.chain, NOW).state, "unknown")


class WithoutThePackage(CertBase):
    """A server where cryptography is missing keeps working; the certificates simply cannot be used."""

    def test_a_stored_certificate_cannot_be_opened_and_says_why(self):
        tv = add_device()
        ident = make_identity(tv)
        with mock.patch.object(ic, "AVAILABLE", False):
            with self.assertRaises(ic.CertProblem) as cm:
                ic.read_material(ident["id"])
            self.assertEqual(cm.exception.code, "unavailable")
            self.assertIn("cryptography", str(cm.exception))
            self.assertEqual(code_of(ic.read_p12, b"x", "pw"), "cert_unavailable")
            with self.assertRaises(ic.CertError):
                ic.seal(1, {})
        self.assertIsNone(ic.read_material(424242))                              # no file: still just "none"

    def test_the_rest_of_the_checks_do_not_need_it(self):
        with mock.patch.object(ic, "AVAILABLE", False):
            self.assertEqual(ic.parse_profile(fx.profile_bytes([certdata.CERT1])).team_id, fx.TEAM)
            self.assertEqual(ic.check(profile(), cert_info(), device_row(), None, NOW), [])
            self.assertEqual(ic.classify_install_failure("0xe8008018"), "revoked")


class Migration(unittest.TestCase):
    def test_every_column_is_added_once(self):
        added = [(t, c) for t, c, _ in db.MIGRATIONS]
        self.assertEqual(len(added), len(set(added)))
        self.assertIn(("apps", "sign_mode"), added)
        self.assertIn(("apps", "cert_id"), added)

    def test_a_database_from_before_the_feature_gets_the_columns_and_the_table(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA.split("-- Certificats importés")[0])           # the tables as they were
        for table, column, kind in db.MIGRATIONS:
            if column not in ("sign_mode", "cert_id"):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        conn.execute("INSERT INTO apps (name, source_type, created_at) VALUES ('Ancienne', 'github', 0)")
        self.assertEqual([r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE name = 'imported_certs'")], [])
        db.migrate(conn)
        db.migrate(conn)                                                            # and again: nothing more to do
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(apps)")}
        self.assertLessEqual({"sign_mode", "cert_id"}, columns)
        row = conn.execute("SELECT sign_mode, cert_id FROM apps").fetchone()
        self.assertEqual((row["sign_mode"], row["cert_id"]), ("apple_id", None))     # an existing app keeps the Apple ID route
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(imported_certs)")}
        self.assertLessEqual({"id", "device_id", "name", "cert_serial", "expires_at", "revoked_at", "notified_level"}, cols)
        self.assertFalse(any(c in cols for c in ("key_pem", "password", "p12")))    # nothing secret in the table
        conn.close()


class AppleAddresses(unittest.TestCase):
    def test_which_addresses_may_be_contacted(self):
        for url, ok in (("http://ocsp.apple.com/ocsp03-wwdr01", True), ("https://apple.com/x", True), ("http://ocsp.apple.com:80/x", True),
                        ("http://ocsp.apple.com.evil.example/x", False), ("http://notapple.com/", False), ("ftp://ocsp.apple.com/", False),
                        ("http://127.0.0.1/", False), ("file:///etc/passwd", False), ("", False)):
            self.assertIs(ic.apple_host(url), ok, url)


# ---------- un renouvellement avec un certificat importé, de bout en bout (appels externes remplacés) ----------

class CertRenew(CertBase):
    def setUp(self):
        super().setUp()
        self.tv = add_device()
        self.ident = make_identity(self.tv, days=300)
        self.app_id = self.add_app("Nuvio", device_id=self.tv["id"], status="pending", sign_mode="certificate",
                                   cert_id=self.ident["id"], version="2.0", source_tag="v2")
        (renew.app_dir(self.app_id) / "original.ipa").write_bytes(make_ipa())
        self.signed_with, self.installed, self.reached = [], [], 0
        self.install_error = None
        self.profile_expiry = NOW + 300 * DAY

        async def sign_with_certificate(files, src, out, on_lock=None):
            if on_lock:
                on_lock("acquired")
            self.signed_with.append({"cert": Path(files.cert).read_text(), "key": Path(files.key).read_text(),
                                     "profile": Path(files.profile).read_bytes()})
            out.write_bytes(make_signed(self.profile_expiry, certs=(certdata.CERT1,)))

        async def install(device, ipas, on_event=None, wait_cap=None, app_name=None):
            if self.install_error:
                raise self.install_error
            self.installed.append([p.name for p in ipas])

        async def check_reachable(device):
            self.reached += 1

        async def confirm_installed(device, bundle_id):
            return time.time()

        async def refresh(app, force=False):
            return False

        async def apple_route(*a, **k):
            raise AssertionError("la voie Apple ID a été utilisée pour une app signée avec un certificat importé")

        patches = [mock.patch.object(plume, "sign_with_certificate", sign_with_certificate),
                   mock.patch.object(devices, "install", install), mock.patch.object(devices, "check_reachable", check_reachable),
                   mock.patch.object(devices, "confirm_installed", confirm_installed), mock.patch.object(renew, "refresh_source", refresh),
                   mock.patch.object(plume, "sign", apple_route), mock.patch.object(plume, "ensure_device", apple_route),
                   mock.patch.object(renew, "_usable_account", apple_route)]
        if not CRYPTO:
            patches.append(mock.patch.object(ic, "signing_files", fake_signing_files))     # the stored file is only a stand-in
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_renew(self, trigger="manuel", app_id=None):
        asyncio.run(renew.renew(app_id or self.app_id, trigger))
        return self.app_of(app_id or self.app_id)

    # --- la signature et l'installation ---

    def test_signs_and_installs_without_any_apple_account(self):
        a = self.run_renew()                                      # no account is connected in this test at all
        self.assertEqual((a["status"], a["last_error"], a["fail_count"], a["needs_action"]), ("ok", None, 0, None))
        self.assertAlmostEqual(a["expires_at"], NOW + 300 * DAY, delta=60)
        self.assertEqual(self.installed, [["signed.ipa"]])
        self.assertEqual((a["sign_mode"], a["cert_id"], a["account_email"]), ("certificate", self.ident["id"], None))
        self.assertTrue(a["cert_serial"])
        self.assertEqual(a["installed_version"], "2.0")
        messages = [e["message"] for e in db.rows("SELECT message FROM events WHERE app_id = ?", self.app_id)]
        self.assertTrue(any("signature avec le certificat importé" in m for m in messages), messages)
        self.assertTrue(any("installée avec le certificat importé" in m and "pas de renouvellement automatique" in m
                            for m in messages), messages)

    def test_plumesign_gets_the_certificate_key_and_profile_of_the_card(self):
        self.run_renew()
        self.assertEqual(len(self.signed_with), 1)
        got = self.signed_with[0]
        self.assertTrue(got["cert"].startswith("-----BEGIN CERTIFICATE-----"))
        self.assertTrue(got["key"].startswith("-----BEGIN PRIVATE KEY-----"))
        self.assertIn(b"ProvisionedDevices", got["profile"])
        self.assertEqual(list(config.TMP_DIR.glob("cert-*")), [])               # nothing left behind

    def test_the_app_expires_with_the_certificate_when_it_ends_first(self):
        db.update("imported_certs", self.ident["id"], cert_not_after=NOW + 40 * DAY, expires_at=NOW + 40 * DAY)
        a = self.run_renew()
        self.assertAlmostEqual(a["expires_at"], NOW + 40 * DAY, delta=5)

    def test_a_new_version_is_signed_and_installed_again(self):
        self.run_renew()
        db.update("apps", self.app_id, source_sha256="new", installed_ipa="old")
        self.assertTrue(renew.pending_update(self.app_of(self.app_id)))
        self.run_renew("nouvelle version")
        self.assertEqual(len(self.signed_with), 2)
        self.assertEqual(len(self.installed), 2)

    def test_a_trigger_for_a_new_certificate_always_signs_again(self):
        self.assertIn("nouveau certificat", renew.FORCE_SIGN)
        self.run_renew()
        self.run_renew("nouveau certificat")
        self.assertEqual(len(self.signed_with), 2)

    # --- pas de renouvellement tous les 7 jours ---

    def test_not_due_when_the_app_is_valid_however_close_the_end(self):
        a = self.run_renew()
        for left in (200 * DAY, 20 * DAY, 3 * DAY, 5 * 3600):
            row = {**a, "expires_at": NOW + left}
            self.assertFalse(renew.due(row, NOW), left)
            self.assertFalse(renew.due({**row, "id": 99}, NOW, ignore_retry=True), left)
        apple = {**a, "sign_mode": "apple_id", "expires_at": NOW + 5 * 3600}
        self.assertTrue(renew.due(apple, NOW))                         # the same app on the Apple ID route is due

    def test_due_when_it_failed_or_was_never_installed(self):
        a = self.run_renew()
        self.assertTrue(renew.due({**a, "status": "error", "next_attempt_at": None}, NOW))
        self.assertTrue(renew.due({**a, "status": "pending", "expires_at": None, "next_attempt_at": None}, NOW))
        self.assertFalse(renew.due({**a, "status": "error", "next_attempt_at": NOW + 600}, NOW))

    def test_no_renewal_time_is_planned_for_the_interface(self):
        a = self.run_renew()
        view = renew.window_view(a, NOW, 60)
        self.assertEqual(view, {"renew_at": None, "window_wait": False, "window_at": None})

    def test_the_loop_leaves_a_valid_certificate_app_alone(self):
        a = self.run_renew()
        db.update("apps", self.app_id, expires_at=NOW + 5 * 3600)         # a few hours left: an Apple ID app would be renewed
        spawned = []
        with mock.patch.object(renew, "spawn", lambda app_id, trigger="manuel": spawned.append((app_id, trigger)) or True), \
                mock.patch.object(renew, "_background", lambda *a, **k: True), \
                mock.patch.object(devices, "check_store", mock.AsyncMock()):
            asyncio.run(renew.tick(time.time(), time.time(), time.time()))
        self.assertEqual(spawned, [])
        self.assertEqual(a["status"], "ok")

    def test_alert_levels_follow_the_certificate_not_the_seven_day_profile(self):
        a = {**self.run_renew(), "status": "ok"}
        table = [(30 * DAY, None), (8 * DAY, None), (6 * DAY, "7d"), (2 * DAY, "7d"), (20 * 3600, "1d"), (60, "1d"), (-5, "expired")]
        for left, level in table:
            self.assertEqual(renew.alert_state({**a, "expires_at": NOW + left}, NOW), level, left)
        apple = {**a, "sign_mode": "apple_id", "expires_at": NOW + 6 * DAY}
        self.assertIsNone(renew.alert_state(apple, NOW))                     # the Apple ID levels are untouched
        self.assertEqual(renew.alert_state({**apple, "expires_at": NOW + 20 * 3600, "status": "error"}, NOW), "24h")

    def test_failure_alert_levels(self):
        a = {**self.run_renew(), "status": "error"}
        self.assertEqual(renew._level({**a, "expires_at": NOW + 3 * DAY}, NOW), "7d")
        self.assertEqual(renew._level({**a, "expires_at": NOW + 5 * 3600}, NOW), "1d")
        self.assertEqual(renew._level({**a, "expires_at": NOW + 20 * DAY}, NOW), "fail")
        self.assertEqual(renew._level({**a, "sign_mode": "apple_id", "expires_at": NOW + 5 * 3600}, NOW), "6h")
        order = renew._LEVELS
        self.assertLess(order.index("fail"), order.index("7d"))
        self.assertLess(order.index("7d"), order.index("1d"))
        self.assertLess(order.index("1d"), order.index("expired"))
        self.assertLess(order.index("fail"), order.index("24h"))
        self.assertLess(order.index("24h"), order.index("6h"))

    # --- ce qui arrête une app signée avec un certificat ---

    def blocked(self, change=None, expect=""):
        change = change or {}
        before = {k: ic.row(self.ident["id"])[k] for k in change}
        db.update("imported_certs", self.ident["id"], **change)
        try:
            a = self.run_renew()
        finally:
            db.update("imported_certs", self.ident["id"], **before)
        self.assertEqual((a["status"], a["needs_action"], a["fail_kind"]), ("error", "config", "config"))
        self.assertIn(expect, a["last_error"])
        self.assertEqual((self.signed_with, self.installed, self.reached), ([], [], 0))     # nothing was contacted or signed
        self.assertGreater(a["next_attempt_at"] - time.time(), 5 * 3600)                   # waits for the user
        return a

    def test_revoked_certificate(self):
        self.blocked(dict(revoked_at=NOW - 60), "certificat importé révoqué")

    def test_expired_certificate_and_profile(self):
        self.blocked(dict(cert_not_after=NOW - DAY), "certificat importé expiré")
        self.blocked(dict(profile_expires=NOW - DAY), "profil importé expiré")

    def test_no_certificate_chosen(self):
        db.update("apps", self.app_id, cert_id=None)
        a = self.run_renew()
        self.assertIn("aucun certificat importé", a["last_error"])
        self.assertEqual(a["needs_action"], "config")

    def test_certificate_file_gone(self):
        path = config.CERT_DIR / f"{self.ident['id']}.bin"
        saved = path.read_bytes()
        path.unlink()
        try:
            a = self.run_renew()
        finally:
            path.write_bytes(saved)
        self.assertIn("certificat importé introuvable", a["last_error"])
        self.assertEqual(a["needs_action"], "config")

    def test_certificate_of_another_device_or_not_covering_the_app(self):
        other = add_device(name="Chambre", udid="AAAA-0000", pairing_udid="AAAA-0000")
        db.update("apps", self.app_id, device_id=other["id"])
        self.assertIn("autre appareil", self.run_renew()["last_error"])
        db.update("apps", self.app_id, device_id=self.tv["id"], next_attempt_at=None)
        self.blocked(dict(profile_app_id="com.example.other"), "ne couvre pas l'identifiant com.example.tv")

    # --- l'appareil refuse l'installation ---

    def test_a_revoked_identity_refused_by_the_device_is_remembered(self):
        twin = self.add_app("Autre", device_id=self.tv["id"], status="ok", sign_mode="certificate", cert_id=self.ident["id"],
                            expires_at=NOW + 100 * DAY)
        self.install_error = RuntimeError("install : Failed to verify code signature : 0xe8008018 (The identity used to sign "
                                          "the executable is no longer valid.)")
        a = self.run_renew()
        self.assertEqual((a["status"], a["needs_action"]), ("error", "config"))
        self.assertIn("certificat importé révoqué", a["last_error"])
        ident = ic.row(self.ident["id"])
        self.assertTrue(ident["revoked_at"])
        self.assertEqual(ident["notified_level"], "revoked")
        other = self.app_of(twin)                                   # the other app signed with it is flagged too, at once
        self.assertEqual((other["status"], other["needs_action"]), ("error", "config"))
        self.assertIn("révoqué", other["last_error"])
        self.assertEqual(len(self.sent), 1)                        # one alert for the certificate, not one per app
        message, priority, event, _ = self.sent[0]
        self.assertEqual((priority, event), ("urgent", "account"))
        self.assertIn("Autre", message)
        self.assertIn("Nuvio", message)
        self.install_error = None
        renew.clear_block(self.app_id)
        a = self.run_renew()                                          # blocked at once, nothing signed or sent again
        self.assertEqual(a["status"], "error")
        self.assertEqual((len(self.signed_with), len(self.sent)), (1, 1))

    def test_other_refusals_of_the_device_are_explained_without_marking_the_certificate(self):
        for text, fragment in (("install : 0xe8008015 (A valid provisioning profile for this executable was not found.)", "profil importé refusé"),
                               ("install : The provisioning profile has expired", "profil importé expiré"),
                               ("install : 0xe8008016 (invalid entitlements)", "droits de l'app refusés")):
            renew.clear_block(self.app_id)
            self.install_error = RuntimeError(text)
            a = self.run_renew()
            self.assertIn(fragment, a["last_error"])
            self.assertEqual(a["needs_action"], "config", text)
            self.assertIsNone(ic.row(self.ident["id"])["revoked_at"])
        self.assertEqual(self.sent, [])

    def test_an_ordinary_install_failure_is_an_ordinary_failure(self):
        self.install_error = RuntimeError("install : disque plein")
        a = self.run_renew()
        self.assertEqual((a["status"], a["needs_action"]), ("error", None))
        self.assertIsNone(ic.row(self.ident["id"])["revoked_at"])
        self.assertGreater(a["next_attempt_at"], time.time())
        self.assertLess(a["next_attempt_at"] - time.time(), 2 * 3600)

    # --- les alertes d'expiration, une par niveau ---

    def run_checks(self, now=None):
        asyncio.run(renew._check_identities(now or time.time()))

    def test_warned_once_at_seven_days_once_at_one_day_and_once_when_it_ends(self):
        db.update("imported_certs", self.ident["id"], expires_at=time.time() + 6 * DAY)
        self.run_checks()
        self.assertEqual(len(self.sent), 1)
        message, priority, event, _ = self.sent[0]
        self.assertEqual((priority, event), ("high", "expiry"))
        self.assertIn("Nuvio", message)
        self.assertIn(self.ident["name"], message)
        self.assertEqual(ic.row(self.ident["id"])["notified_level"], "7d")
        self.run_checks()
        self.assertEqual(len(self.sent), 1)                           # not again at the next pass
        db.update("imported_certs", self.ident["id"], expires_at=time.time() + 12 * 3600)
        self.run_checks()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(ic.row(self.ident["id"])["notified_level"], "1d")
        db.update("imported_certs", self.ident["id"], expires_at=time.time() - 60)
        self.run_checks()
        self.assertEqual(len(self.sent), 3)
        self.assertEqual((self.sent[2][1], self.sent[2][2]), ("urgent", "expiry"))
        self.run_checks()
        self.assertEqual(len(self.sent), 3)

    def test_a_replaced_certificate_starts_a_new_episode(self):
        db.update("imported_certs", self.ident["id"], expires_at=time.time() + 6 * DAY)
        self.run_checks()
        db.update("imported_certs", self.ident["id"], expires_at=time.time() + 300 * DAY)
        self.run_checks()
        self.assertIsNone(ic.row(self.ident["id"])["notified_level"])
        db.update("imported_certs", self.ident["id"], expires_at=time.time() + 6 * DAY)
        self.run_checks()
        self.assertEqual(len(self.sent), 2)

    def test_nothing_to_warn_about_when_no_app_uses_the_certificate(self):
        db.update("imported_certs", self.ident["id"], expires_at=time.time() + 2 * DAY)
        db.update("apps", self.app_id, sign_mode="apple_id")
        self.run_checks()
        db.update("apps", self.app_id, sign_mode="certificate", enabled=0)           # paused: not worth a warning either
        self.run_checks()
        self.assertEqual(self.sent, [])

    def test_a_warning_nobody_received_is_tried_again_but_logged_once(self):
        self.deliver = False
        db.update("imported_certs", self.ident["id"], expires_at=time.time() + 3 * DAY)
        self.run_checks()
        self.run_checks()
        self.assertEqual(len(self.sent), 2)
        self.assertIsNone(ic.row(self.ident["id"])["notified_level"])
        lines = [e["message"] for e in db.rows("SELECT message FROM events WHERE level = 'warn'") if "expire le" in e["message"]]
        self.assertEqual(len(lines), 1)

    # --- le contrôle de révocation ---

    def test_ocsp_revocation_flags_the_apps_and_warns_once(self):
        result = ic.Revocation("revoked", "key_compromise", NOW - 3600)
        with mock.patch.object(ic, "material_cert", return_value=(b"der", [])), \
                mock.patch.object(ic, "check_revocation", return_value=result):
            r = asyncio.run(renew.check_identity(self.ident["id"]))
            asyncio.run(renew.check_identity(self.ident["id"]))
        self.assertEqual(r.state, "revoked")
        ident = ic.row(self.ident["id"])
        self.assertEqual((ident["check_state"], ident["revoked_reason"]), ("revoked", "OCSP : key_compromise"))
        self.assertAlmostEqual(ident["revoked_at"], NOW - 3600, delta=5)
        a = self.app_of(self.app_id)
        self.assertEqual((a["status"], a["needs_action"]), ("error", "config"))
        self.assertIn("certificat importé révoqué", a["last_error"])
        self.assertEqual(len(self.sent), 1)

    def test_a_good_answer_only_moves_the_date(self):
        with mock.patch.object(ic, "material_cert", return_value=(b"der", [])), \
                mock.patch.object(ic, "check_revocation", return_value=ic.Revocation("good")):
            asyncio.run(renew.check_identity(self.ident["id"]))
        ident = ic.row(self.ident["id"])
        self.assertEqual(ident["check_state"], "good")
        self.assertIsNone(ident["revoked_at"])
        self.assertEqual(self.app_of(self.app_id)["status"], "pending")
        self.assertEqual(self.sent, [])

    def test_a_certificate_whose_file_is_gone_is_not_looked_up(self):
        with mock.patch.object(ic, "material_cert", return_value=None), mock.patch.object(ic, "check_revocation") as lookup:
            self.assertIsNone(asyncio.run(renew.check_identity(self.ident["id"])))
        lookup.assert_not_called()

    def test_a_sealed_file_that_cannot_be_opened_does_not_stop_the_other_checks(self):
        broken = ic.CertProblem("illisible", "missing")
        with mock.patch.object(ic, "material_cert", side_effect=broken), mock.patch.object(ic, "check_revocation") as lookup:
            self.assertIsNone(asyncio.run(renew.check_identity(self.ident["id"])))
        lookup.assert_not_called()
        second = make_identity(self.tv)
        self.add_app("Autre", device_id=self.tv["id"], sign_mode="certificate", cert_id=second["id"])
        seen = []

        def material(ident_id):
            seen.append(ident_id)
            if ident_id == self.ident["id"]:
                raise broken
            return b"der", []

        with mock.patch.object(ic, "material_cert", material), mock.patch.object(ic, "AVAILABLE", True),                 mock.patch.object(ic, "check_revocation", return_value=ic.Revocation("good")):
            asyncio.run(renew._check_revocations())
        self.assertEqual(sorted(seen), sorted([self.ident["id"], second["id"]]))
        self.assertEqual(ic.row(second["id"])["check_state"], "good")

    def test_the_periodic_pass_checks_what_is_in_use_every_twelve_hours(self):
        calls = []

        async def check(ident_id):
            calls.append(ident_id)

        unused = make_identity(self.tv)
        with mock.patch.object(renew, "check_identity", check), mock.patch.object(ic, "AVAILABLE", True):
            asyncio.run(renew._check_revocations())
            self.assertEqual(calls, [self.ident["id"]])               # the unused one is not worth a query
            db.update("imported_certs", self.ident["id"], checked_at=time.time() - 3600)
            asyncio.run(renew._check_revocations())
            self.assertEqual(calls, [self.ident["id"]])               # checked an hour ago
            db.update("imported_certs", self.ident["id"], checked_at=time.time() - 13 * 3600)
            asyncio.run(renew._check_revocations())
            self.assertEqual(calls, [self.ident["id"]] * 2)
            db.update("imported_certs", self.ident["id"], checked_at=None, revoked_at=NOW)
            asyncio.run(renew._check_revocations())
            self.assertEqual(len(calls), 2)                            # a revoked one is not asked again
            db.update("imported_certs", self.ident["id"], revoked_at=None)
            with mock.patch.object(renew, "CERT_CHECK_HOURS", 0):
                asyncio.run(renew._check_revocations())
            self.assertEqual(len(calls), 2)                            # disabled
        self.assertEqual(unused["id"], ic.row(unused["id"])["id"])

    # --- le reste de tvsign n'est pas touché ---

    def test_certificate_apps_do_not_count_against_the_free_account(self):
        accounts = [{"email": "a@x.com"}]
        apps = [dict(self.app_of(self.app_id), enabled=1, account_email="a@x.com")]
        self.assertEqual(renew.account_slots(apps, [self.tv], accounts, "a@x.com"), {"a@x.com": []})
        apps.append(dict(apps[0], id=77, sign_mode="apple_id"))
        self.assertEqual(renew.account_slots(apps, [self.tv], accounts, "a@x.com")["a@x.com"][0]["apps"], 1)
        for i in range(4):
            self.add_app(f"c{i}", device_id=self.tv["id"], sign_mode="certificate", cert_id=self.ident["id"])
        crowded = dict(self.app_of(self.app_id), sign_mode="apple_id")
        self.assertNotIn("slots_full", renew.add_warnings(crowded))
        self.assertEqual(renew.add_warnings(self.app_of(self.app_id)), [])

    def test_the_account_certificate_check_leaves_certificate_apps_alone(self):
        db.update("apps", self.app_id, status="ok", signed_at=NOW, expires_at=NOW + 100 * DAY, cert_serial="ABC123")
        apple = self.add_app("Gratuite", device_id=self.tv["id"], status="ok", account_email="a@x.com", cert_serial="FFFF",
                             signed_at=NOW, expires_at=NOW + 3 * DAY)
        self.set_accounts("a@x.com", selected="a@x.com")

        async def issued(email):
            return {"1"}                                              # neither serial is among the account's certificates

        with mock.patch.object(plume, "issued_cert_serials", issued):
            asyncio.run(renew._check_certificates("a@x.com"))
        self.assertEqual(self.app_of(self.app_id)["expires_at"], NOW + 100 * DAY)         # untouched
        self.assertLess(self.app_of(apple)["expires_at"], time.time())                     # the Apple ID app is renewed at once

    def test_startup_keeps_the_expiry_of_a_certificate_app(self):
        signed = renew.app_dir(self.app_id) / "signed.ipa"
        signed.write_bytes(make_signed(NOW + 300 * DAY, certs=(certdata.CERT1,)))
        db.update("imported_certs", self.ident["id"], expires_at=NOW + 40 * DAY)
        db.update("apps", self.app_id, status="ok", signed_at=signed.stat().st_mtime, expires_at=NOW + 40 * DAY)
        renew.startup()
        self.assertAlmostEqual(self.app_of(self.app_id)["expires_at"], NOW + 40 * DAY, delta=5)      # not "corrected" to the profile's


# ---------- l'API ----------

class Api(CertBase):
    @classmethod
    def setUpClass(cls):
        cls.stop_checks = mock.patch.object(renew, "CERT_CHECK_HOURS", 0)           # the loop's first pass must not look anything up
        cls.stop_checks.start()
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)
        cls.stop_checks.stop()

    def setUp(self):
        super().setUp()
        self.tv = add_device()
        self.app_id = self.add_app("Nuvio", device_id=self.tv["id"], status="ok", expires_at=NOW + 3 * DAY, signed_at=NOW,
                                   verified_at=NOW, cert_serial="AB")
        self.spawned, self.reruns = [], []
        for patch in (mock.patch.object(renew, "spawn", lambda app_id, trigger="manuel": self.spawned.append((app_id, trigger)) or True),
                      mock.patch.object(renew, "rerun", lambda app_id: self.reruns.append(app_id))):
            patch.start()
            self.addCleanup(patch.stop)

    def post_cert(self, p12=None, profile=None, password="pw", device_id=None, **form):
        key, cert = shared_pair()
        files = {"p12": ("a.p12", p12 if p12 is not None else fx.make_p12(key, cert, "pw"), "application/x-pkcs12"),
                 "profile": ("a.mobileprovision", profile if profile is not None else fx.profile_bytes([fx.der(cert)]),
                             "application/octet-stream")}
        data = {"device_id": str(device_id or self.tv["id"]), "password": password, **{k: str(v) for k, v in form.items()}}
        return self.client.post("/api/certs", headers=HEADERS, data=data, files=files)

    def patch_app(self, **body):
        return self.client.patch(f"/api/apps/{self.app_id}", headers=HEADERS, json=body)

    def row(self, app_id=None):
        return self.app_of(app_id or self.app_id)

    # --- import ---

    @needs_crypto
    def test_import_checks_stores_and_lists(self):
        r = self.post_cert(app_id=self.app_id)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        cert = body["cert"]
        self.assertEqual((cert["device_id"], cert["state"], cert["platforms"], cert["wildcard"], cert["has_material"]),
                         (self.tv["id"], "ok", ["tvOS"], True, True))
        self.assertEqual((body["warnings"], body["revocation"]), ([], "unknown"))          # the check server is out of reach here
        self.assertTrue(ic.has_material(cert["id"]))
        listed = self.client.get("/api/certs").json()
        self.assertEqual([c["id"] for c in listed["certs"]], [cert["id"]])
        self.assertTrue(listed["available"])
        state = self.client.get("/api/state").json()
        self.assertEqual([c["id"] for c in state["certs"]], [cert["id"]])
        self.assertTrue(state["capabilities"]["imported_cert"])
        events = [e["message"] for e in db.rows("SELECT message FROM events")]
        self.assertTrue(any(m.startswith("Certificat importé «") and "ajouté pour « Salon »" in m for m in events), events)

    @needs_crypto
    def test_nothing_secret_is_ever_sent_back(self):
        r = self.post_cert()
        everything = r.text + self.client.get("/api/certs").text + self.client.get("/api/state").text
        for leak in ("PRIVATE KEY", "BEGIN CERTIFICATE", "ProvisionedDevices"):
            self.assertNotIn(leak, everything)

    @needs_crypto
    def test_each_refusal_has_its_code_and_stores_nothing(self):
        der = fx.der(shared_pair()[1])
        cases = [
            (dict(password="oops"), "cert_password_wrong"),
            (dict(p12=b"not a p12"), "cert_p12_invalid"),
            (dict(profile=b"not a profile"), "cert_profile_invalid"),
            (dict(profile=fx.profile_bytes([der], days=-3)), "cert_profile_expired:"),
            (dict(profile=fx.profile_bytes([der], platform=("iOS",))), "cert_platform:iOS:tvOS"),
            (dict(profile=fx.profile_bytes([der], udids=("0000-1111",))), "cert_device_not_in_profile"),
            (dict(profile=fx.profile_bytes([certdata.CERT2])), "cert_not_in_profile"),
            (dict(profile=fx.profile_bytes([der], device_list=False)), "cert_profile_not_for_devices"),
            (dict(profile=fx.profile_bytes([der], app_id=f"{fx.TEAM}.com.other"), app_id=self.app_id),
             "cert_bundle_mismatch:com.other:com.example.tv"),
        ]
        for kwargs, code in cases:
            r = self.post_cert(**kwargs)
            self.assertEqual(r.status_code, 400, (code, r.text))
            self.assertTrue(r.json()["detail"].startswith(code), (code, r.json()))
        self.assertEqual(ic.rows(), [])
        self.assertEqual(list(config.CERT_DIR.glob("*.bin")), [])

    @needs_crypto
    def test_unknown_things(self):
        self.assertEqual(self.post_cert(device_id=99999).json()["detail"], "unknown_device")
        self.assertEqual(self.post_cert(app_id=99999).json()["detail"], "unknown_app")
        self.assertEqual(self.post_cert(replace_id=99999).status_code, 404)
        other = add_device(name="Chambre", udid="AAAA-0000", pairing_udid="AAAA-0000")
        ident = store_identity(other, udids=("AAAA-0000",))
        r = self.post_cert(replace_id=ident["id"])                       # a certificate belongs to one device
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "cert_wrong_device"))

    @needs_crypto
    def test_the_devices_own_udid_is_read_before_the_profile_is_compared(self):
        newly_paired = add_device(name="Chambre", udid="PAIRING-ID-0001", pairing_udid="PAIRING-ID-0001", os_version=None)

        async def refresh(device_id, wait=True):
            db.update("devices", device_id, udid=fx.UDID, os_version="18.0")        # the TV tells its hardware UDID
            return {}

        with mock.patch.object(devices, "refresh_info", refresh):
            r = self.post_cert(device_id=newly_paired["id"])
        self.assertEqual(r.status_code, 200, r.text)
        asleep = add_device(name="Cuisine", udid="PAIRING-ID-0002", pairing_udid="PAIRING-ID-0002", os_version=None)

        async def unreachable(device_id, wait=True):
            raise devices.Unreachable("introuvable")

        with mock.patch.object(devices, "refresh_info", unreachable):          # asleep: the identifiers already known are compared
            r = self.post_cert(device_id=asleep["id"])
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "cert_device_not_in_profile"))

    @needs_crypto
    def test_a_certificate_already_revoked_is_refused(self):
        with mock.patch.object(ic, "check_revocation", return_value=ic.Revocation("revoked", "key_compromise", NOW)):
            r = self.post_cert()
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "cert_revoked"))
        self.assertEqual(ic.rows(), [])

    @needs_crypto
    def test_a_good_answer_is_recorded_at_import(self):
        with mock.patch.object(ic, "check_revocation", return_value=ic.Revocation("good")):
            r = self.post_cert()
        self.assertEqual(r.json()["revocation"], "good")
        self.assertEqual(ic.row(r.json()["cert"]["id"])["check_state"], "good")

    def test_without_the_package_the_feature_says_so(self):
        with mock.patch.object(ic, "AVAILABLE", False):
            r = self.client.post("/api/certs", headers=HEADERS, data={"device_id": str(self.tv["id"])},
                                 files={"p12": ("a.p12", b"x"), "profile": ("a.mobileprovision", b"x")})
            self.assertEqual((r.status_code, r.json()["detail"]), (501, "cert_unavailable"))
            self.assertFalse(self.client.get("/api/certs").json()["available"])
            self.assertFalse(self.client.get("/api/state").json()["capabilities"]["imported_cert"])

    def test_the_size_of_a_request_is_bounded(self):
        r = self.client.post("/api/certs", headers=HEADERS, data={"device_id": str(self.tv["id"])},
                             files={"p12": ("a.p12", b"x" * (1024 * 1024 + 100)), "profile": ("a.mobileprovision", b"x")})
        self.assertEqual(r.status_code, 413)

    @needs_crypto
    def test_an_enterprise_profile_on_a_phone_warns_about_trust(self):
        phone = add_device(name="iPhone", family="ios", udid="00008110-AAAAAAAAAAAAAAAA", pairing_udid="00008110-AAAAAAAAAAAAAAAA")
        profile = fx.profile_bytes([fx.der(shared_pair()[1])], platform=("iOS",), device_list=False, all_devices=True)
        r = self.post_cert(profile=profile, device_id=phone["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["warnings"], ["trust"])

    # --- le choix par app ---

    def test_an_app_chooses_the_certificate_and_starts_from_scratch(self):
        ident = make_identity(self.tv)
        (renew.app_dir(self.app_id) / "signed.ipa").write_bytes(b"old signature")
        r = self.patch_app(sign_mode="certificate", cert_id=ident["id"])
        self.assertEqual(r.status_code, 200, r.text)
        a = self.row()
        self.assertEqual((a["sign_mode"], a["cert_id"], a["status"], a["expires_at"], a["signed_at"], a["verified_at"], a["cert_serial"]),
                         ("certificate", ident["id"], "pending", None, None, None, None))
        self.assertEqual((a["needs_action"], a["fail_count"], a["next_attempt_at"]), (None, 0, None))
        self.assertFalse((renew.app_dir(self.app_id) / "signed.ipa").exists())         # the old signature is no good any more
        self.assertEqual(self.reruns, [self.app_id])
        self.assertEqual(self.client.get("/api/state").json()["apps"][0]["sign_mode"], "certificate")

    def test_without_a_choice_the_fitting_certificate_is_picked(self):
        short, long = make_identity(self.tv, days=100), make_identity(self.tv, days=250)
        self.assertEqual(self.patch_app(sign_mode="certificate").status_code, 200)
        self.assertEqual(self.row()["cert_id"], long["id"])
        self.assertNotEqual(self.row()["cert_id"], short["id"])

    def test_the_certificate_must_fit(self):
        other = add_device(name="Chambre", udid="AAAA-0000", pairing_udid="AAAA-0000")
        elsewhere = make_identity(other, udids=("AAAA-0000",))
        revoked = make_identity(self.tv)
        db.update("imported_certs", revoked["id"], revoked_at=NOW)
        expired = make_identity(self.tv)
        db.update("imported_certs", expired["id"], cert_not_after=NOW - DAY)
        narrow = make_identity(self.tv, app_id=f"{fx.TEAM}.com.other")
        cases = [
            ("unknown", {"sign_mode": "certificate", "cert_id": 424242}, "unknown_cert"),
            ("another device", {"sign_mode": "certificate", "cert_id": elsewhere["id"]}, "cert_wrong_device"),
            ("revoked", {"sign_mode": "certificate", "cert_id": revoked["id"]}, "cert_revoked"),
            ("expired", {"sign_mode": "certificate", "cert_id": expired["id"]}, "cert_expired:"),
            ("profile for one identifier", {"sign_mode": "certificate", "cert_id": narrow["id"]},
             "cert_bundle_mismatch:com.other:com.example.tv"),
        ]
        for label, body, code in cases:
            r = self.patch_app(**body)
            self.assertEqual(r.status_code, 400, (label, r.text))
            self.assertTrue(r.json()["detail"].startswith(code), (label, r.json()))
            self.assertEqual(self.row()["sign_mode"], "apple_id", label)            # nothing changed

    def test_with_nothing_imported_for_this_device(self):
        r = self.patch_app(sign_mode="certificate")
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "cert_none"))
        self.assertEqual(self.row()["sign_mode"], "apple_id")

    def test_extensions_need_a_wildcard_profile(self):
        ident = make_identity(self.tv, app_id=f"{fx.TEAM}.com.example.tv")
        db.update("apps", self.app_id, warnings="appex:1")
        r = self.patch_app(sign_mode="certificate", cert_id=ident["id"])
        self.assertEqual(r.json()["detail"], "cert_needs_wildcard:com.example.tv")
        db.update("apps", self.app_id, warnings="")
        self.assertEqual(self.patch_app(sign_mode="certificate", cert_id=ident["id"]).status_code, 200)

    def test_back_to_the_apple_id(self):
        ident = make_identity(self.tv)
        self.patch_app(sign_mode="certificate", cert_id=ident["id"])
        db.update("apps", self.app_id, status="ok", expires_at=NOW + 100 * DAY)
        r = self.patch_app(sign_mode="apple_id")
        self.assertEqual(r.status_code, 200)
        a = self.row()
        self.assertEqual((a["sign_mode"], a["cert_id"], a["status"], a["expires_at"]), ("apple_id", None, "pending", None))
        self.assertTrue(ic.has_material(ident["id"]))                      # the certificate stays for later

    def test_nothing_changes_when_nothing_is_asked(self):
        ident = make_identity(self.tv)
        self.patch_app(sign_mode="certificate", cert_id=ident["id"])
        db.update("apps", self.app_id, status="ok", expires_at=NOW + 100 * DAY)
        self.reruns.clear()
        self.assertEqual(self.patch_app(sign_mode="certificate", cert_id=ident["id"]).status_code, 200)
        a = self.row()
        self.assertEqual((a["status"], a["expires_at"], a["cert_id"]), ("ok", NOW + 100 * DAY, ident["id"]))
        self.assertEqual(self.reruns, [])
        self.assertEqual(self.patch_app(enabled=True).status_code, 200)             # other changes leave the signing alone
        self.assertEqual(self.row()["sign_mode"], "certificate")

    def test_invalid_mode(self):
        r = self.patch_app(sign_mode="free")
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "invalid_sign_mode"))

    def test_moving_an_app_that_uses_a_certificate_needs_a_certificate_for_the_new_device(self):
        ident = make_identity(self.tv)
        self.patch_app(sign_mode="certificate", cert_id=ident["id"])
        other = add_device(name="Chambre", udid="AAAA-0000", pairing_udid="AAAA-0000")
        r = self.patch_app(device_id=other["id"])
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "cert_none"))
        self.assertEqual(self.row()["device_id"], self.tv["id"])
        theirs = make_identity(other, udids=("AAAA-0000",))
        r = self.patch_app(device_id=other["id"])                         # the new device's own certificate is taken
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((self.row()["device_id"], self.row()["cert_id"]), (other["id"], theirs["id"]))

    def test_a_tv_change_alone_does_not_touch_an_apple_id_app(self):
        other = add_device(name="Chambre", udid="AAAA-0000", pairing_udid="AAAA-0000")
        self.assertEqual(self.patch_app(device_id=other["id"]).status_code, 200)
        self.assertEqual(self.row()["sign_mode"], "apple_id")

    # --- suppression, remplacement ---

    def test_delete(self):
        ident = make_identity(self.tv)
        db.update("apps", self.app_id, sign_mode="certificate", cert_id=ident["id"])
        r = self.client.delete(f"/api/certs/{ident['id']}", headers=HEADERS)
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "cert_in_use:Nuvio"))
        self.assertTrue(ic.has_material(ident["id"]))
        db.update("apps", self.app_id, sign_mode="apple_id")
        self.assertEqual(self.client.delete(f"/api/certs/{ident['id']}", headers=HEADERS).status_code, 200)
        self.assertFalse(ic.has_material(ident["id"]))
        self.assertEqual(self.client.delete(f"/api/certs/{ident['id']}", headers=HEADERS).status_code, 404)

    @needs_crypto
    def test_replacing_signs_the_apps_that_use_it_again(self):
        ident = store_identity(self.tv, days=100)
        db.update("apps", self.app_id, sign_mode="certificate", cert_id=ident["id"], status="error", needs_action="config",
                  notified_level="1d")
        (renew.app_dir(self.app_id) / "signed.ipa").write_bytes(b"old")
        db.update("imported_certs", ident["id"], notified_level="1d", revoked_at=NOW)
        r = self.post_cert(replace_id=ident["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["cert"]["id"], ident["id"])
        fresh = ic.row(ident["id"])
        self.assertAlmostEqual(fresh["expires_at"], NOW + 300 * DAY, delta=120)
        self.assertEqual((fresh["revoked_at"], fresh["notified_level"]), (None, None))
        a = self.row()
        self.assertEqual((a["needs_action"], a["notified_level"]), (None, None))
        self.assertFalse((renew.app_dir(self.app_id) / "signed.ipa").exists())
        self.assertEqual(self.spawned, [(self.app_id, "nouveau certificat")])
        self.assertEqual(self.reruns, [self.app_id])

    @needs_crypto
    def test_a_replacement_must_still_cover_the_apps_that_use_the_certificate(self):
        ident = store_identity(self.tv, days=100)
        db.update("apps", self.app_id, sign_mode="certificate", cert_id=ident["id"])
        r = self.post_cert(replace_id=ident["id"], profile=fx.profile_bytes([fx.der(shared_pair()[1])], app_id=f"{fx.TEAM}.com.other"))
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "cert_bundle_mismatch:com.other:com.example.tv"))
        self.assertAlmostEqual(ic.row(ident["id"])["expires_at"], NOW + 100 * DAY, delta=120)       # the old one is untouched
        self.assertEqual(self.spawned, [])

    # --- ailleurs dans l'interface ---

    def test_copy_to_another_tv_takes_that_tvs_certificate(self):
        ident = make_identity(self.tv)
        db.update("apps", self.app_id, sign_mode="certificate", cert_id=ident["id"])
        (renew.app_dir(self.app_id) / "original.ipa").write_bytes(make_ipa())
        bedroom = add_device(name="Chambre", udid="AAAA-0000", pairing_udid="AAAA-0000")
        kitchen = add_device(name="Cuisine", udid="BBBB-0000", pairing_udid="BBBB-0000")
        theirs = make_identity(bedroom, udids=("AAAA-0000",))
        copy = self.client.post(f"/api/apps/{self.app_id}/copy", headers=HEADERS, json={"device_id": bedroom["id"]})
        self.assertEqual(copy.status_code, 200, copy.text)
        made = self.row(copy.json()["id"])
        self.assertEqual((made["sign_mode"], made["cert_id"]), ("certificate", theirs["id"]))
        plain = self.client.post(f"/api/apps/{self.app_id}/copy", headers=HEADERS, json={"device_id": kitchen["id"]})
        self.assertEqual(plain.status_code, 200, plain.text)
        self.assertEqual(self.row(plain.json()["id"])["sign_mode"], "apple_id")                     # no certificate for that TV
        self.assertTrue(any("la copie se signe avec l'Apple ID" in e["message"] for e in db.rows("SELECT message FROM events")))

    def test_removing_a_device_removes_its_certificates(self):
        ident = make_identity(self.tv)
        db.update("apps", self.app_id, sign_mode="certificate", cert_id=ident["id"])
        with mock.patch.object(devices, "forget", mock.AsyncMock()):
            r = self.client.delete(f"/api/devices/{self.tv['id']}", headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(ic.rows(), [])
        self.assertFalse((config.CERT_DIR / f"{ident['id']}.bin").exists())
        self.assertIsNone(self.row()["cert_id"])

    def test_the_card_alert_and_renewal_time_follow_the_certificate(self):
        ident = make_identity(self.tv)
        db.update("apps", self.app_id, sign_mode="certificate", cert_id=ident["id"], expires_at=NOW + 5 * DAY, status="ok")
        card = next(a for a in self.client.get("/api/state").json()["apps"] if a["id"] == self.app_id)
        self.assertEqual((card["alert"], card["renew_at"], card["window_wait"]), ("7d", None, False))

    def test_the_account_budget_leaves_certificate_apps_out(self):
        ident = make_identity(self.tv)
        db.update("apps", self.app_id, sign_mode="certificate", cert_id=ident["id"], account_email="a@x.com")
        self.set_accounts("a@x.com", selected="a@x.com")
        account = self.client.get("/api/state").json()["accounts"][0]
        self.assertEqual((account["apps"], account["app_ids"], account["slots"]), (0, 0, []))

    def test_health_mentions_a_revoked_or_nearly_expired_certificate(self):
        ident = make_identity(self.tv)
        db.update("apps", self.app_id, sign_mode="certificate", cert_id=ident["id"], expires_at=NOW + 100 * DAY)
        problems = lambda: " ".join(self.client.get("/api/health").json().get("problems", []))        # noqa: E731
        self.assertNotIn("certificat importé", problems())
        db.update("imported_certs", ident["id"], expires_at=time.time() + 5 * 3600)
        self.assertIn("expire dans moins de 24 h", problems())
        db.update("imported_certs", ident["id"], expires_at=time.time() + 100 * DAY, revoked_at=NOW)
        self.assertIn("révoqué", problems())

    def test_the_support_bundle_has_the_state_and_nothing_else(self):
        ident = make_identity(self.tv)
        db.update("apps", self.app_id, sign_mode="certificate", cert_id=ident["id"])
        saved = plume.version, support.diag.run_all

        async def version():
            return "plumesign 0.0-test"

        async def checks():
            return {"checks": [], "ok": True}
        plume.version, support.diag.run_all = version, checks
        try:
            r = self.client.get("/api/support-bundle")
        finally:
            plume.version, support.diag.run_all = saved
        z = zipfile.ZipFile(io.BytesIO(r.content))
        everything = "\n".join(z.read(n).decode("utf-8") for n in z.namelist())
        apps = json.loads(z.read("apps.json"))
        self.assertEqual(apps["apps"][0]["sign_mode"], "certificate")
        self.assertEqual([(c["id"], c["state"], c["app_id_pattern"]) for c in apps["certificates"]], [(ident["id"], "ok", "wildcard")])
        for leak in ("PRIVATE KEY", ident["name"], ident["cert_serial"], fx.TEAM, "Test profile"):
            self.assertNotIn(leak, everything, leak)


# ---------- les sauvegardes ----------

class Backups(CertBase):
    def setUp(self):
        super().setUp()
        self.tv = add_device()
        self.ident = make_identity(self.tv)
        self.app_id = self.add_app("Nuvio", device_id=self.tv["id"], sign_mode="certificate", cert_id=self.ident["id"])
        self.archives = []
        self.addCleanup(lambda: [a.unlink(missing_ok=True) for a in self.archives])
        self.drop_asides()                          # two restores within a second would set aside under the same name
        self.addCleanup(self.drop_asides)

    @staticmethod
    def drop_asides():
        for pattern in ("certs.pre-restore-*", "ipas.pre-restore-*"):
            for path in config.DATA_DIR.glob(pattern):
                shutil.rmtree(path, ignore_errors=True)

    def export(self, *args):
        path = backup.export(*args)
        self.archives.append(path)
        return path

    def database_of(self, archive) -> tuple[bytes, list, list]:
        import sqlite3
        z = zipfile.ZipFile(archive)
        data = z.read("tvsign.db")
        copy = config.TMP_DIR / f"backup-check-{time.time_ns()}.db"
        copy.write_bytes(data)
        conn = sqlite3.connect(copy)
        try:
            return (data, conn.execute("SELECT id, name FROM imported_certs").fetchall(),
                    conn.execute("SELECT sign_mode, cert_id FROM apps WHERE id = ?", (self.app_id,)).fetchall())
        finally:
            conn.close()
            copy.unlink(missing_ok=True)

    def test_by_default_the_certificates_stay_out_of_a_backup(self):
        archive = self.export()
        z = zipfile.ZipFile(archive)
        self.assertEqual([n for n in z.namelist() if n.startswith("certs")], [])
        self.assertIs(json.loads(z.read("manifest.json"))["include_certs"], False)
        everything = b"".join(z.read(n) for n in z.namelist())
        for leak in (b"PRIVATE KEY", self.ident["name"].encode(), self.ident["cert_serial"].encode(), b"Test profile"):
            self.assertNotIn(leak, everything, leak)
        data, certs, apps = self.database_of(archive)
        self.assertEqual(certs, [])
        # the app stays set to a certificate (it must not slip onto the Apple ID by itself), without one
        self.assertEqual(apps, [("certificate", None)])
        self.assertIsNotNone(ic.row(self.ident["id"]))                       # the live instance is untouched
        self.assertTrue(ic.has_material(self.ident["id"]))

    def test_deleted_rows_do_not_linger_in_the_exported_database(self):
        archive = self.export()
        data, _, _ = self.database_of(archive)
        self.assertNotIn(self.ident["name"].encode(), data)                  # erased from the file, not only marked free

    @needs_crypto
    def test_on_request_they_are_included(self):
        payload = ic.read_material(self.ident["id"])
        archive = self.export(False, True)
        z = zipfile.ZipFile(archive)
        self.assertIn(f"certs/{self.ident['id']}.json", z.namelist())
        self.assertIs(json.loads(z.read("manifest.json"))["include_certs"], True)
        self.assertEqual(json.loads(z.read(f"certs/{self.ident['id']}.json")), payload)
        _, certs, apps = self.database_of(archive)
        self.assertEqual(certs, [(self.ident["id"], self.ident["name"])])
        self.assertEqual(apps, [("certificate", self.ident["id"])])

    @needs_crypto
    def test_the_option_is_carried_by_the_api(self):
        with TestClient(main.app) as client:
            plain = client.get("/api/backup/export", headers=HEADERS)
            full = client.get("/api/backup/export?certs=true", headers=HEADERS)
        self.assertEqual(plain.status_code, 200)
        self.assertEqual([n for n in zipfile.ZipFile(io.BytesIO(plain.content)).namelist() if n.startswith("certs")], [])
        self.assertIn(f"certs/{self.ident['id']}.json", zipfile.ZipFile(io.BytesIO(full.content)).namelist())

    @needs_crypto
    def test_restoring_a_backup_that_holds_them_seals_them_again(self):
        payload = ic.read_material(self.ident["id"])
        archive = self.export(False, True)
        db.update("apps", self.app_id, sign_mode="apple_id", cert_id=None)
        ic.remove(self.ident["id"])                                         # the certificate is lost
        self.assertFalse(ic.has_material(self.ident["id"]))
        backup.restore(archive)
        self.assertEqual(ic.row(self.ident["id"])["name"], self.ident["name"])
        self.assertEqual(ic.read_material(self.ident["id"]), payload)
        self.assertEqual(sorted(p.name for p in config.CERT_DIR.iterdir()), [f"{self.ident['id']}.bin"])      # no clear copy left
        self.assertNotIn(b"PRIVATE KEY", (config.CERT_DIR / f"{self.ident['id']}.bin").read_bytes())
        self.assertEqual(db.row("SELECT sign_mode, cert_id FROM apps WHERE id = ?", self.app_id)["cert_id"], self.ident["id"])

    def test_restoring_a_backup_without_them_sets_the_current_ones_aside(self):
        archive = self.export()
        backup.restore(archive)
        self.assertEqual(ic.rows(), [])
        self.assertEqual(list(config.CERT_DIR.iterdir()), [])
        aside = [p for p in config.DATA_DIR.glob("certs.pre-restore-*") if (p / f"{self.ident['id']}.bin").exists()]
        self.assertTrue(aside)
        a = db.row("SELECT sign_mode, cert_id FROM apps WHERE id = ?", self.app_id)
        self.assertEqual((a["sign_mode"], a["cert_id"]), ("certificate", None))
        # such an app says what is missing instead of signing with the Apple ID
        with self.assertRaises(ic.CertProblem) as cm:
            ic.require(db.row("SELECT * FROM apps WHERE id = ?", self.app_id), self.tv)
        self.assertEqual(cm.exception.code, "cert_none")

    def test_a_restore_that_fails_puts_the_certificates_back(self):
        archive = self.export()
        real = backup.replace_retry
        before = (config.CERT_DIR / f"{self.ident['id']}.bin").read_bytes()

        def failing(src, dst, *a, **k):
            if Path(dst) == config.DB_PATH:
                raise PermissionError("verrouillé par un antivirus")
            return real(src, dst, *a, **k)
        with mock.patch.object(backup, "replace_retry", failing):
            with self.assertRaises(backup.BackupError):
                backup.restore(archive)
        self.assertEqual((config.CERT_DIR / f"{self.ident['id']}.bin").read_bytes(), before)
        self.assertIsNotNone(ic.row(self.ident["id"]))

    def test_old_copies_set_aside_are_pruned_with_the_others(self):
        old = config.DATA_DIR / "certs.pre-restore-1"
        old.mkdir()
        (old / "1.bin").write_bytes(b"x")
        os.utime(old, (1, 1))
        backup.prune_old()
        self.assertFalse(old.exists())


# ---------- plumesign pour de bon ----------

PLUMESIGN_BIN = ROOT / "vendor" / "Impactor" / "target" / "release" / ("plumesign.exe" if os.name == "nt" else "plumesign")


@needs_crypto
@unittest.skipUnless(PLUMESIGN_BIN.is_file(), "plumesign n'est pas construit (cd vendor/Impactor && cargo build -p plumesign --release)")
class PlumesignEndToEnd(CertBase):
    """Signe vraiment une IPA tvOS (un exécutable arm64 minimal) avec le .p12 et le profil, hors ligne, et lit ce que la
    signature contient : l'identifiant d'app d'un profil à joker doit devenir celui de l'app."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(plume, "PLUMESIGN", PLUMESIGN_BIN)
        patch.start()
        self.addCleanup(patch.stop)
        self.work = Path(tempfile.mkdtemp(prefix="tvsign-e2e-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.tv = add_device()

    def sign(self, pattern: str, bundle: str = "com.example.fixture", extension: str | None = None):
        ident = store_identity(self.tv, app_id=f"{fx.TEAM}.{pattern}")
        (self.work / "in.ipa").write_bytes(fx.signable_ipa(bundle, extension=extension))
        out = self.work / "out.ipa"

        async def go():
            with ic.signing_files(ident["id"]) as files:
                await plume.sign_with_certificate(files, self.work / "in.ipa", out)
        asyncio.run(go())
        return ident, out

    def test_a_wildcard_profile_gives_the_app_its_own_identifier(self):
        for pattern in ("*", "com.example.*", "com.example.fixture"):
            _, out = self.sign(pattern)
            ents = fx.embedded_entitlements(out)
            self.assertEqual(ents["application-identifier"], f"{fx.TEAM}.com.example.fixture", pattern)
            self.assertEqual(ents["keychain-access-groups"], [f"{fx.TEAM}.com.example.fixture"], pattern)
            self.assertEqual(ents["com.apple.developer.team-identifier"], fx.TEAM)

    def test_the_profile_is_embedded_and_the_expiry_read_back(self):
        ident, out = self.sign("*")
        self.assertAlmostEqual(ipa.profile_expiry(out), NOW + 300 * DAY, delta=120)
        self.assertAlmostEqual(ic.signature_expiry(ident, out), NOW + 300 * DAY, delta=120)
        self.assertEqual(ipa.app_info(out)["bundle_id"], "com.example.fixture")            # the identifier is left alone

    def test_an_extension_gets_its_own_identifier_too(self):
        _, out = self.sign("com.example.*", extension="com.example.fixture.widget")
        ents = fx.embedded_entitlements(out, "Payload/Fixture.app/PlugIns/Widget.appex/Widget")
        self.assertEqual(ents["application-identifier"], f"{fx.TEAM}.com.example.fixture.widget")

    def test_a_signing_failure_is_reported_with_plumesign_s_words(self):
        ident = store_identity(self.tv)
        (self.work / "in.ipa").write_bytes(fx.signable_ipa())

        async def go():
            with ic.signing_files(ident["id"]) as files:
                files.key.write_text("not a key")
                await plume.sign_with_certificate(files, self.work / "in.ipa", self.work / "out.ipa")
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(go())
        self.assertIn("signature avec le certificat importé", str(cm.exception))
        self.assertFalse((self.work / "out.ipa").exists())
        self.assertFalse((self.work / "out.tmp.ipa").exists())


if __name__ == "__main__":
    unittest.main()
