"""Mises à jour et sources : recul des essais, certificat signataire, SSRF, retour arrière, contrôle des
versions par app, remplacement d'une IPA importée, recherche de mise à jour de tvsign.

    python -m unittest discover -s tests
"""
import asyncio
import hashlib
import io
import os
import plistlib
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from email.message import Message
from pathlib import Path
from unittest import mock

DATA = tempfile.mkdtemp(prefix="tvsign-test-")
os.environ.update(TVSIGN_DATA=DATA, TVSIGN_AUTH="none", TVSIGN_PLUMESIGN=str(Path(DATA) / "absent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, devices, ipa, main, plume, renew, selfupdate, settings  # noqa: E402

HEADERS = {"X-Tvsign": "1"}
HOUR = 3600


def make_ipa(bundle="com.example.tv", version="1.0", min_os=None, platform="appletvos", extra=b"") -> bytes:
    buf = io.BytesIO()
    plist = {"CFBundleName": "Test", "CFBundleIdentifier": bundle, "CFBundleShortVersionString": version,
             "DTPlatformName": platform}
    if min_os:
        plist["MinimumOSVersion"] = min_os
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Payload/Test.app/Info.plist", plistlib.dumps(plist))
        if extra:
            z.writestr("Payload/Test.app/blob", extra)
    return buf.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def app_row(**over) -> dict:
    row = dict(id=1, name="Test", source_type="github", source="o/r", source_tag="t1", source_key="k1",
               bad_tag=None, bad_key=None, bundle_id="com.example.tv", enabled=1, status="ok",
               expires_at=None, notified_level=None, channel="stable", preset=None, asset_name=None,
               source_filter=None, fail_count=0)
    row.update(over)
    return row


# ---------- classement des échecs (le recul lui-même : voir test_renew_engine.Backoff) ----------

class Backoff(unittest.TestCase):
    NOW = 1_000_000.0

    def delay(self, kind=None, expires=None) -> float:
        app = app_row(expires_at=expires)
        return (renew._retry_at(app, self.NOW, kind=kind) - self.NOW) / 3600

    def test_failures_that_need_the_user_wait_for_them(self):
        self.assertEqual(self.delay("config"), 6)
        self.assertEqual(self.delay("session"), 6)
        self.assertEqual(self.delay("limit"), 24)
        self.assertEqual(self.delay("config", self.NOW + 8 * HOUR), 2)         # plus près de l'échéance

    def test_last_try_just_before_expiry(self):
        exp = self.NOW + 30 * 60
        self.assertEqual(round(self.delay("limit", exp) * 60), 29)

    def test_update_failures_are_counted_apart(self):
        app = app_row(fail_count=3, fail_kind="other")
        fields = renew.failure_fields(app, self.NOW, RuntimeError("boom"), False, optional=True)
        self.assertEqual((fields["fail_count"], fields["fail_kind"]), (1, "update:other"))
        fields = renew.failure_fields(app, self.NOW, RuntimeError("boom"), False)
        self.assertEqual((fields["fail_count"], fields["fail_kind"]), (4, "other"))

    def test_failure_kinds(self):
        self.assertEqual(renew.failure_kind(plume.SessionExpired("x")), "session")
        self.assertEqual(renew.failure_kind(plume.UnknownAccount("x")), "session")
        self.assertEqual(renew.failure_kind(devices.PairingLost("x")), "pairing")
        self.assertEqual(renew.failure_kind(plume.LimitReached("x")), "limit")
        self.assertEqual(renew.failure_kind(renew.TvosTooOld("x")), "config")
        self.assertEqual(renew.failure_kind(RuntimeError("NO_TEAM")), "config")
        self.assertEqual(renew.failure_kind(RuntimeError("MULTIPLE_TEAMS: a, b")), "config")
        self.assertIsNone(renew.failure_kind(RuntimeError("signature : boom")))
        self.assertIsNone(renew.failure_kind(devices.Unreachable("x")))

    def test_transient_and_ipa_faults(self):
        for exc in (devices.Unreachable("x"), plume.Transient("x"), devices.AppInUse("x"), plume.Busy("x")):
            self.assertTrue(renew.is_transient(exc), exc)
            self.assertFalse(renew.ipa_fault(exc), exc)
        for exc in (plume.SessionExpired("x"), devices.PairingLost("x"), plume.LimitReached("x"),
                    renew.TvosTooOld("x"), plume.AppleRefused("x")):
            self.assertFalse(renew.ipa_fault(exc), exc)
        self.assertTrue(renew.ipa_fault(RuntimeError("signature : boom")))


# ---------- certificat signataire ----------

def der(tag: int, content: bytes) -> bytes:
    n = len(content)
    head = bytes([tag, n]) if n < 128 else bytes([tag, 0x80 | (n.bit_length() + 7) // 8]) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    return head + content


def indefinite(tag: int, content: bytes) -> bytes:
    return bytes([tag, 0x80]) + content + b"\0\0"


def integer(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 8) // 8, "big")        # bit de signe : un octet de tête si besoin
    return der(0x02, raw)


def cms(serial: int, ber: bool = False, other_serial: int = 0x11) -> bytes:
    wrap = indefinite if ber else der
    issuer = der(0x30, der(0x31, b""))
    signer = der(0x30, integer(1) + der(0x30, issuer + integer(serial)) + der(0x30, der(0x06, b"\x2b")) + der(0x04, b"sig"))
    # une chaîne de certificats factice (autre numéro de série) avant les signataires
    fake_cert = der(0x30, der(0x30, der(0xA0, integer(2)) + integer(other_serial)))
    signed = wrap(0x30, integer(1) + der(0x31, b"") + der(0x30, der(0x06, b"\x2a")) + wrap(0xA0, fake_cert)
                  + der(0x31, signer))
    return wrap(0x30, der(0x06, b"\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02") + wrap(0xA0, signed))


def macho(cms_blob: bytes, fat: bool = False) -> bytes:
    codedir = (0xFADE0C02).to_bytes(4, "big") + (8).to_bytes(4, "big")
    sig = (0xFADE0B01).to_bytes(4, "big") + (8 + len(cms_blob)).to_bytes(4, "big") + cms_blob
    index = 12 + 16
    superblob = ((0xFADE0CC0).to_bytes(4, "big") + (index + len(codedir) + len(sig)).to_bytes(4, "big")
                 + (2).to_bytes(4, "big") + (0).to_bytes(4, "big") + index.to_bytes(4, "big")
                 + (0x10000).to_bytes(4, "big") + (index + len(codedir)).to_bytes(4, "big") + codedir + sig)
    seg = (0x19).to_bytes(4, "little") + (24).to_bytes(4, "little") + b"\0" * 16
    dataoff = 32 + 24 + 16 + 64
    lc_sig = (0x1D).to_bytes(4, "little") + (16).to_bytes(4, "little") + dataoff.to_bytes(4, "little") + len(superblob).to_bytes(4, "little")
    header = (b"\xcf\xfa\xed\xfe" + (0x0100000C).to_bytes(4, "little") + (0).to_bytes(4, "little") + (2).to_bytes(4, "little")
              + (2).to_bytes(4, "little") + (len(seg) + len(lc_sig)).to_bytes(4, "little") + (0).to_bytes(4, "little") + b"\0" * 4)
    thin = header + seg + lc_sig + b"\0" * 64 + superblob
    if not fat:
        return thin
    offset = 4096
    table = (0xCAFEBABE).to_bytes(4, "big") + (1).to_bytes(4, "big") + (0x0100000C).to_bytes(4, "big") + (0).to_bytes(4, "big") \
        + offset.to_bytes(4, "big") + len(thin).to_bytes(4, "big") + (12).to_bytes(4, "big")
    return table + b"\0" * (offset - len(table)) + thin


def signed_ipa(path: Path, binary: bytes, profile_serials=()) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Payload/T.app/Info.plist", plistlib.dumps({"CFBundleExecutable": "T", "CFBundleIdentifier": "x"}))
        z.writestr("Payload/T.app/T", binary)
    return path


class SignerCertificate(unittest.TestCase):
    """Le certificat qui a signé, pas ceux que liste le profil : une révocation par un autre outil du compte
    ne doit pas être masquée par le certificat de cet outil qui, lui, reste valide."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=DATA))

    def serial(self, binary):
        return ipa.signer_serial(signed_ipa(self.dir / "s.ipa", binary))

    def test_definite_lengths(self):
        self.assertEqual(self.serial(macho(cms(0x6DB74483DB0BCF333747310B6E7612AC))), "6DB74483DB0BCF333747310B6E7612AC")

    def test_indefinite_lengths_like_apple_tools(self):
        self.assertEqual(self.serial(macho(cms(0xABCDEF, ber=True))), "ABCDEF")

    def test_universal_binary(self):
        self.assertEqual(self.serial(macho(cms(0x1234ABCD), fat=True)), "1234ABCD")

    def test_ignores_other_certificates_of_the_chain(self):
        self.assertEqual(self.serial(macho(cms(0x42, other_serial=0x99))), "42")

    def test_unsigned_or_garbage_gives_none(self):
        self.assertIsNone(self.serial(b"\xcf\xfa\xed\xfe" + b"\0" * 28))
        self.assertIsNone(self.serial(b"not a macho"))
        self.assertIsNone(ipa.signer_serial(self.dir / "absent.ipa"))

    def test_real_signature_from_plumesign(self):
        real = Path(__file__).resolve().parents[2].parent / "NuvioTV-signed.ipa"
        if not real.exists():
            self.skipTest("IPA signée de référence absente")
        self.assertEqual(ipa.signer_serial(real), next(iter(ipa.profile_cert_serials(real))))

    def test_serials_prefers_signer_over_profile(self):
        with mock.patch.object(ipa, "signer_serial", return_value="C1"), \
                mock.patch.object(ipa, "profile_cert_serials", return_value={"C1", "C2"}):
            self.assertEqual(renew._serials(Path("x")), "C1")
        with mock.patch.object(ipa, "signer_serial", return_value=None), \
                mock.patch.object(ipa, "profile_cert_serials", return_value={"C1", "C2"}):
            self.assertEqual(renew._serials(Path("x")), "C1,C2")

    def test_revocation_of_the_signer_is_seen_despite_another_valid_certificate(self):
        """Profil {C1 tvsign, C2 AltStore}, C1 révoqué : avant, C2 masquait la révocation."""
        app_id = db.execute("INSERT INTO apps (name, source_type, bundle_id, status, enabled, cert_serial, expires_at, "
                            "created_at) VALUES ('R', 'upload', 'c.r', 'ok', 1, 'C1', ?, 0)", time.time() + 100 * HOUR)

        async def issued(email):
            return {"C2"}

        with mock.patch.object(plume, "issued_cert_serials", issued), mock.patch.object(plume, "default_account", lambda: "a@b.c"), \
                mock.patch.object(renew, "notify", mock.AsyncMock()):
            asyncio.run(renew._check_certificates("a@b.c"))
        row = db.row("SELECT * FROM apps WHERE id = ?", app_id)
        self.assertLess(row["expires_at"], time.time())            # re-signature forcée
        db.execute("DELETE FROM apps WHERE id = ?", app_id)


# ---------- réseau : SSRF, en-têtes, erreurs GitHub ----------

class FakeResp:
    def __init__(self, body=b"", headers=None):
        self.body, self.headers = io.BytesIO(body), headers or {}

    def read(self, n=-1):
        return self.body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Ssrf(unittest.TestCase):
    def test_public_ranges(self):
        import ipaddress
        pub = lambda a: ipa._public(ipaddress.ip_address(a))       # noqa: E731
        self.assertTrue(pub("8.8.8.8"))
        self.assertTrue(pub("2606:4700:4700::1111"))
        self.assertFalse(pub("127.0.0.1"))
        self.assertFalse(pub("192.168.1.10"))
        self.assertFalse(pub("169.254.169.254"))
        self.assertFalse(pub("100.64.0.1"))                         # Tailscale / CGNAT : avant, laissé passer
        self.assertFalse(pub("198.18.0.5"))                         # DNS « fake-ip » (Clash, Surge)
        self.assertFalse(pub("::1"))
        self.assertFalse(pub("::ffff:127.0.0.1"))                   # IPv4-mappée
        self.assertTrue(pub("64:ff9b::808:808"))                    # NAT64 vers 8.8.8.8
        self.assertFalse(pub("64:ff9b::7f00:1"))                    # NAT64 vers 127.0.0.1
        self.assertFalse(pub("2002:c0a8:0101::1"))                  # 6to4 vers 192.168.1.1

    def resolve(self, *addrs):
        infos = [(2, 1, 6, "", (a, 443)) for a in addrs]
        return mock.patch.object(ipa.socket, "getaddrinfo", return_value=infos)

    def test_link_to_private_address_is_refused_with_a_code(self):
        with self.resolve("100.64.0.9"):
            with self.assertRaises(ipa.UrlRejected) as cm:
                ipa._check_url("https://nas.example/a.ipa")
        self.assertEqual(cm.exception.code, "url_private")

    def test_one_private_answer_among_public_ones_refuses(self):
        with self.resolve("8.8.8.8", "10.0.0.1"):
            with self.assertRaises(ipa.UrlRejected):
                ipa._check_url("https://rebind.example/a.ipa")

    def test_public_link_is_accepted(self):
        with self.resolve("93.184.216.34"):
            ipa._check_url("https://example.com/a.ipa")

    def test_github_hosts_skip_the_address_filter(self):
        """NAT64 / fake-ip / puits DNS ne doivent pas bloquer GitHub : aucune résolution n'est même faite."""
        with mock.patch.object(ipa.socket, "getaddrinfo", side_effect=AssertionError("résolution inutile")):
            for host in ("api.github.com", "github.com", "objects.githubusercontent.com",
                         "release-assets.githubusercontent.com"):
                ipa._check_url(f"https://{host}/x")

    def test_lookalike_hosts_are_not_trusted(self):
        for host in ("github.com.evil.example", "evilgithub.com", "api.github.com.evil.example", "githubusercontent.com.evil.io"):
            self.assertFalse(ipa._trusted_host(host), host)

    def test_userinfo_has_a_clear_error(self):
        with self.assertRaises(ipa.UrlRejected) as cm:
            ipa._check_url("https://user:token@example.com/a.ipa")
        self.assertEqual(cm.exception.code, "url_userinfo")

    def test_http_refused(self):
        with self.assertRaises(ipa.UrlRejected) as cm:
            ipa._check_url("http://example.com/a.ipa")
        self.assertEqual(cm.exception.code, "https_only")

    def test_connection_goes_to_the_checked_address_not_a_second_resolution(self):
        """DNS rebinding : la connexion refuse une adresse locale même si le contrôle d'avant a vu une publique."""
        conn = ipa._PinnedHTTPSConnection("rebind.example", 443)
        with self.resolve("127.0.0.1"), mock.patch.object(ipa.socket, "create_connection") as connect:
            with self.assertRaises(ipa.UrlRejected):
                conn.connect()
        connect.assert_not_called()

    def test_connection_pins_the_validated_address(self):
        conn = ipa._PinnedHTTPSConnection("example.com", 443)
        wrapped = mock.Mock()
        conn._context = mock.Mock(wrap_socket=mock.Mock(return_value=wrapped))
        with self.resolve("93.184.216.34"), mock.patch.object(ipa.socket, "create_connection", return_value="raw") as connect:
            conn.connect()
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))
        conn._context.wrap_socket.assert_called_once_with("raw", server_hostname="example.com")
        self.assertIs(conn.sock, wrapped)

    def test_allow_private_urls_switch(self):
        with mock.patch.object(ipa, "ALLOW_PRIVATE_URLS", True), \
                mock.patch.object(ipa.socket, "getaddrinfo", side_effect=AssertionError):
            ipa._check_url("https://192.168.1.5/a.ipa")

    def test_redirect_to_private_address_is_refused(self):
        handler = ipa._HttpsOnly()
        req = urllib.request.Request("https://example.com/a")
        with self.resolve("10.1.1.1"), self.assertRaises(ipa.UrlRejected):
            handler.redirect_request(req, None, 302, "Found", Message(), "https://internal.example/x")

    def test_github_token_does_not_follow_a_redirect_to_another_host(self):
        handler = ipa._HttpsOnly()
        req = urllib.request.Request("https://api.github.com/repos/o/r/releases/assets/1",
                                     headers={"Authorization": "Bearer secret"})
        with self.resolve("93.184.216.34"):
            new = handler.redirect_request(req, None, 302, "Found", Message(), "https://objects.githubusercontent.com/x")
            self.assertIsNone(new.get_header("Authorization"))
            same = handler.redirect_request(req, None, 302, "Found", Message(), "https://api.github.com/other")
            self.assertEqual(same.get_header("Authorization"), "Bearer secret")


class Headers(unittest.TestCase):
    def capture(self, fn):
        seen = {}

        def fake_open(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.header_items()})
            return FakeResp(b"[]", {})

        with mock.patch.object(ipa._OPENER, "open", fake_open), \
                mock.patch.object(ipa.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            fn()
        return seen

    def test_downloads_do_not_advertise_the_github_media_type(self):
        seen = self.capture(lambda: ipa._get("https://example.com/a.ipa").close() if False else ipa._get("https://example.com/a.ipa"))
        self.assertEqual(seen["accept"], "*/*")

    def test_github_json_asks_for_the_github_media_type(self):
        seen = self.capture(lambda: ipa._get_json("https://api.github.com/repos/o/hdrs/releases"))
        self.assertEqual(seen["accept"], "application/vnd.github+json")

    def test_token_only_for_the_api_host(self):
        with mock.patch.object(ipa, "GITHUB_TOKEN", "tok"):
            api = self.capture(lambda: ipa._get("https://api.github.com/x"))
            other = self.capture(lambda: ipa._get("https://example.com/x"))
        self.assertEqual(api["authorization"], "Bearer tok")
        self.assertNotIn("authorization", other)


class GithubErrors(unittest.TestCase):
    def error(self, code, headers=None):
        msg = Message()
        for k, v in (headers or {}).items():
            msg[k] = v
        return urllib.error.HTTPError("https://api.github.com/x", code, "x", msg, None)

    def test_rate_limit(self):
        e = ipa._github_error(self.error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 600)}))
        self.assertIsInstance(e, ipa.SourceUnavailable)
        self.assertEqual(e.code, "gh_rate_limit")
        self.assertIn("TVSIGN_GITHUB_TOKEN", str(e))
        self.assertEqual(ipa._github_error(self.error(429)).code, "gh_rate_limit")

    def test_plain_403_is_not_a_rate_limit(self):
        self.assertIsNone(ipa._github_error(self.error(403, {"X-RateLimit-Remaining": "12"})))

    def test_missing_repository(self):
        e = ipa._github_error(self.error(404))
        self.assertEqual(e.code, "gh_not_found")
        self.assertIn("introuvable", str(e))

    def test_get_json_raises_the_typed_error(self):
        def boom(url, timeout=30, headers=None):
            raise self.error(404)
        with mock.patch.object(ipa, "_get", boom):
            with self.assertRaises(ipa.SourceUnavailable):
                ipa._get_json("https://api.github.com/repos/o/gone/releases")

    def test_periodic_check_reports_instead_of_staying_silent(self):
        app = app_row(id=77, name="Silent")
        err = ipa.SourceUnavailable("limite de requêtes GitHub atteinte", "gh_rate_limit")
        with mock.patch.object(renew, "_latest", side_effect=err):
            self.assertFalse(asyncio.run(renew.new_release(app)))
        self.assertIn("limite de requêtes GitHub", renew.HEALTH["release_check_error"])
        logged = db.rows("SELECT message FROM events WHERE app_id = 77")
        self.assertEqual(len(logged), 1)
        self.assertIn("recherche de nouvelle version impossible", logged[0]["message"])
        with mock.patch.object(renew, "_latest", return_value=None):
            asyncio.run(renew.new_release(app))
        self.assertIsNone(renew.HEALTH["release_check_error"])           # rétabli dès que GitHub répond


class Releases(unittest.TestCase):
    def releases(self):
        def rel(tag, asset="app.ipa", pre=False, date="2026-01-01T00:00:00Z"):
            return {"tag_name": tag, "prerelease": pre, "draft": False, "published_at": date, "body": f"notes {tag}",
                    "assets": [{"name": asset, "id": 7, "state": "uploaded", "size": 5, "updated_at": "u",
                                "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{asset}",
                                "url": f"https://api.github.com/repos/o/r/releases/assets/{tag}"}]}
        return [rel("v3", date="2026-03-01T00:00:00Z"), rel("v2", date="2026-02-01T00:00:00Z"), rel("v1")]

    def latest(self, **kw):
        with mock.patch.object(ipa, "_get_json", return_value=self.releases()):
            return ipa.latest_github("o/r", None, **kw)

    def test_latest_and_notes(self):
        found = self.latest()
        self.assertEqual((found["tag"], found["notes"]), ("v3", "notes v3"))

    def test_pinned_tag_selects_that_release_only(self):
        self.assertEqual(self.latest(pinned_tag="v1")["tag"], "v1")
        self.assertIsNone(self.latest(pinned_tag="v9"))

    def test_pinned_tag_ignores_channel_and_prefixes(self):
        with mock.patch.object(ipa, "_get_json", return_value=self.releases()):
            self.assertEqual(ipa.latest_github("o/r", ["nope-"], False, None, "v2")["tag"], "v2")

    def test_public_url_without_token(self):
        self.assertTrue(self.latest()["url"].startswith("https://github.com/o/r/releases/download/"))

    def test_private_repository_goes_through_the_asset_api_with_a_token(self):
        with mock.patch.object(ipa, "GITHUB_TOKEN", "tok"):
            found = self.latest()
        self.assertEqual(found["url"], "https://api.github.com/repos/o/r/releases/assets/v3")
        self.assertEqual(ipa._download_headers(found["url"]), {"Accept": "application/octet-stream"})
        self.assertIsNone(ipa._download_headers("https://example.com/a.ipa"))


# ---------- téléchargement ----------

class Download(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=DATA))
        self.dest = self.dir / "original.ipa"

    def fetch(self, data, **kw):
        resp = FakeResp(data, {"Content-Length": str(len(data))})
        with mock.patch.object(ipa, "_get", return_value=resp):
            return ipa.download("https://example.com/a.ipa", self.dest, **kw)

    def test_tvos_too_old_is_refused_and_keeps_the_current_file(self):
        self.dest.write_bytes(b"current")
        with self.assertRaises(ipa.OsTooOld) as cm:
            self.fetch(make_ipa(min_os="26.0"), max_os="18.4")
        self.assertEqual((cm.exception.min_os, cm.exception.have), ("26.0", "18.4"))
        self.assertEqual(self.dest.read_bytes(), b"current")
        self.assertIsInstance(cm.exception, ipa.SourceRejected)

    def test_recent_enough_tv_or_unknown_version_installs(self):
        self.assertEqual(self.fetch(make_ipa(min_os="18.0"), max_os="26.1")["version"], "1.0")
        self.dest.unlink()
        self.assertIsNotNone(self.fetch(make_ipa(min_os="26.0"), max_os=None))

    def test_same_checksum_leaves_the_file_alone(self):
        data = make_ipa()
        self.dest.write_bytes(data)
        self.assertIsNone(self.fetch(data, unchanged_sha=sha(data)))
        self.assertEqual(self.dest.read_bytes(), data)
        new = make_ipa(version="2.0")
        self.assertEqual(self.fetch(new, unchanged_sha=sha(data))["version"], "2.0")

    def test_keep_copy_survives_the_replacement_of_the_original(self):
        self.dest.write_bytes(b"old")
        keep = self.dir / "hold"
        ipa.keep_copy(self.dest, keep)
        part = self.dir / "x.part"
        part.write_bytes(b"new")
        os.replace(part, self.dest)                                     # ce que fait download() : un nouveau fichier
        self.assertEqual((keep.read_bytes(), self.dest.read_bytes()), (b"old", b"new"))

    def test_keep_copy_falls_back_to_a_copy_without_hard_links(self):
        self.dest.write_bytes(b"old")
        keep = self.dir / "hold2"
        with mock.patch.object(ipa.os, "link", side_effect=OSError("pas de lien")):
            ipa.keep_copy(self.dest, keep)
            self.assertEqual(keep.read_bytes(), b"old")
            keep.unlink()
            ipa.keep_copy(self.dest, keep, copy=False)
            self.assertFalse(keep.exists())

    def test_os_version_comparison(self):
        self.assertTrue(ipa.os_older("18.4", "26.0"))
        self.assertTrue(ipa.os_older("26", "26.0.1"))
        self.assertFalse(ipa.os_older("26.0", "26"))
        self.assertFalse(ipa.os_older("26.1", "26.0.9"))
        self.assertFalse(ipa.os_older(None, "26.0"))
        self.assertFalse(ipa.os_older("26.0", None))
        self.assertFalse(ipa.os_older("n/a", "26"))

    def test_redact_hides_tokens(self):
        self.assertEqual(ipa.redact("https://u:p@h.example:8443/a/b.ipa?token=abc"), "https://h.example:8443/a/b.ipa?…")
        self.assertEqual(ipa.redact("https://h.example/a.ipa"), "https://h.example/a.ipa")


# ---------- refresh_source : rejets, version, retour arrière ----------

class Sources(unittest.TestCase):
    def setUp(self):
        db.init()
        self.calls = []
        self.saved = ipa.latest_github, ipa.download, ipa.url_key
        ipa.download = self.fake_download
        self.content = make_ipa(version="2.0")
        self.info = lambda: {"version": "2.0", "bundle_id": "com.example.tv", "sha256": sha(self.content)}

    def tearDown(self):
        ipa.latest_github, ipa.download, ipa.url_key = self.saved

    def fake_download(self, url, dest, sha256=None, expect_bundle=None, **kw):
        self.calls.append((url, kw))
        if isinstance(getattr(self, "raises", None), Exception):
            raise self.raises
        if kw.get("unchanged_sha") and getattr(self, "same", False):
            return None
        part = dest.with_suffix(".part")
        part.write_bytes(self.content)
        os.replace(part, dest)                         # comme ipa.download : un nouveau fichier, pas une écriture en place
        return self.info()

    def add(self, source_type="github", source="o/r", tag="v1", key="k1", device_os=None, **cols):
        device_id = None
        if device_os:
            device_id = db.execute("INSERT INTO devices (name, pair_identifier, os_version, created_at) VALUES ('TV', ?, ?, 0)",
                                   f"p{time.time_ns()}", device_os)
        old = make_ipa(version="1.0")
        app_id = db.execute(
            "INSERT INTO apps (name, source_type, source, source_tag, source_key, bundle_id, version, source_sha256, "
            "device_id, created_at) VALUES ('R', ?, ?, ?, ?, 'com.example.tv', '1.0', ?, ?, 0)",
            source_type, source, tag, key, sha(old), device_id)
        (renew.app_dir(app_id) / "original.ipa").write_bytes(old)
        if cols:
            db.update("apps", app_id, **cols)
        return app_id

    def row(self, app_id):
        return db.row("SELECT * FROM apps WHERE id = ?", app_id)

    def refresh(self, app_id, force=False):
        return asyncio.run(renew.refresh_source(self.row(app_id), force=force))

    def release(self, tag="v2", key="k2", notes="Fixes things"):
        ipa.latest_github = lambda *a, **k: {"tag": tag, "url": "https://x/" + tag, "sha256": None, "key": key, "notes": notes}

    # -- clé inconnue : plus de joker
    def test_rejected_needs_the_same_key(self):
        self.assertTrue(renew._rejected(app_row(bad_tag="u", bad_key=None), "u", None))
        self.assertFalse(renew._rejected(app_row(bad_tag="u", bad_key=None), "u", "etag-new"))
        self.assertFalse(renew._rejected(app_row(bad_tag="u", bad_key="old"), "u", None))

    def test_link_error_without_a_key_does_not_block_future_files(self):
        url = "https://example.com/latest.ipa"
        app_id = self.add("url", url, url, "etag1")
        ipa.url_key = lambda u: (_ for _ in ()).throw(ipa.SourceRejected("adresse locale refusée (10.0.0.1)"))
        self.assertFalse(self.refresh(app_id))
        self.assertIsNone(self.row(app_id)["bad_tag"])                       # pas de refus mémorisé
        ipa.url_key = lambda u: "etag2"
        self.assertTrue(self.refresh(app_id))                                 # le fichier corrigé est téléchargé
        self.assertEqual(self.row(app_id)["source_key"], "etag2")

    def test_link_with_a_key_and_bad_content_is_remembered_per_content(self):
        url = "https://example.com/latest.ipa"
        app_id = self.add("url", url, url, "etag1")
        ipa.url_key = lambda u: "etag-bad"
        self.raises = ipa.SourceRejected("ce fichier n'est pas une IPA")
        with mock.patch.object(renew, "notify", mock.AsyncMock()):
            self.assertFalse(self.refresh(app_id))
        row = self.row(app_id)
        self.assertEqual((row["bad_tag"], row["bad_key"]), (url, "etag-bad"))
        self.raises = None
        self.calls.clear()
        self.assertFalse(self.refresh(app_id))                                # même contenu : pas retéléchargé
        self.assertEqual(self.calls, [])
        ipa.url_key = lambda u: "etag-fixed"
        self.assertTrue(self.refresh(app_id))

    def test_logs_never_contain_link_tokens(self):
        url = "https://example.com/latest.ipa?token=SECRET"
        app_id = self.add("url", url, url, "etag1")
        ipa.url_key = lambda u: "etag2"
        self.refresh(app_id)
        messages = " ".join(r["message"] for r in db.rows("SELECT message FROM events WHERE app_id = ?", app_id))
        self.assertIn("téléchargement", messages)
        self.assertNotIn("SECRET", messages)

    # -- liens sans marqueur
    def test_link_without_validators_is_compared_by_checksum(self):
        url = "https://example.com/latest.ipa"
        app_id = self.add("url", url, url, None)
        ipa.url_key = lambda u: None
        renew._LINK_CHECKED.pop(app_id, None)
        self.same = True
        self.assertFalse(self.refresh(app_id))
        self.assertEqual(self.calls[0][1]["unchanged_sha"], self.row(app_id)["source_sha256"])
        self.calls.clear()
        self.assertFalse(self.refresh(app_id))                                # contrôlé il y a moins de 6 h
        self.assertEqual(self.calls, [])
        self.same = False
        self.assertTrue(self.refresh(app_id, force=True))                     # fichier différent : mis à jour
        self.assertEqual(self.row(app_id)["version"], "2.0")

    # -- mise à jour automatique coupée
    def test_auto_update_off_flags_the_release_without_installing(self):
        app_id = self.add(auto_update=0)
        self.release()
        self.assertFalse(self.refresh(app_id))
        self.assertEqual(self.calls, [])
        row = self.row(app_id)
        self.assertEqual((row["update_tag"], row["update_notes"], row["update_block"]), ("v2", "Fixes things", "manual"))
        self.assertEqual(row["source_tag"], "v1")

    def test_auto_update_off_still_downloads_the_first_time(self):
        app_id = self.add(auto_update=0)
        (renew.app_dir(app_id) / "original.ipa").unlink()
        self.release()
        self.assertTrue(self.refresh(app_id))

    def test_forced_update_installs_despite_the_toggle_and_stores_notes(self):
        app_id = self.add(auto_update=0)
        self.release()
        self.assertTrue(self.refresh(app_id, force=True))
        row = self.row(app_id)
        self.assertEqual((row["source_tag"], row["notes"], row["update_tag"]), ("v2", "Fixes things", None))

    def test_new_release_respects_the_toggle_and_records_the_update(self):
        app_id = self.add(auto_update=0)
        found = {"tag": "v2", "key": "k2", "url": "u", "sha256": None, "notes": "N"}
        with mock.patch.object(renew, "_latest", return_value=found):
            self.assertFalse(asyncio.run(renew.new_release(self.row(app_id))))
            self.assertEqual(self.row(app_id)["update_tag"], "v2")
            db.update("apps", app_id, auto_update=1)
            self.assertTrue(asyncio.run(renew.new_release(self.row(app_id))))
        found["tag"], found["key"] = "v1", "k1"
        with mock.patch.object(renew, "_latest", return_value=found):
            self.assertFalse(asyncio.run(renew.new_release(self.row(app_id))))
            self.assertIsNone(self.row(app_id)["update_tag"])                # à jour : indication retirée

    def test_pinned_tag_is_passed_to_the_search(self):
        seen = {}

        def fake(repo, prefixes, prereleases=False, asset=None, pinned=None):
            seen["pinned"] = pinned
            return None
        ipa.latest_github = fake
        app_id = self.add(pinned_tag="v1")
        self.refresh(app_id)
        self.assertEqual(seen["pinned"], "v1")

    # -- version précédente gardée
    def test_previous_ipa_is_kept_when_an_update_replaces_it(self):
        app_id = self.add(installed_ipa=sha(make_ipa(version="1.0")))
        self.release()
        self.assertTrue(self.refresh(app_id))
        d = renew.app_dir(app_id)
        self.assertEqual(ipa.app_info(d / "original.prev.ipa")["version"], "1.0")
        self.assertEqual(ipa.app_info(d / "original.ipa")["version"], "2.0")
        row = self.row(app_id)
        self.assertEqual((row["prev_tag"], row["prev_version"], row["prev_key"]), ("v1", "1.0", "k1"))
        self.assertTrue(renew.pending_update(row))                            # IPA 2.0 téléchargée, pas encore installée

    def test_an_uninstalled_update_does_not_overwrite_the_last_installed_one(self):
        d1 = make_ipa(version="1.0")
        app_id = self.add(installed_ipa=sha(d1))
        self.release("v2", "k2")
        self.assertTrue(self.refresh(app_id))
        self.content = make_ipa(version="3.0")
        self.release("v3", "k3")
        self.assertTrue(self.refresh(app_id))
        self.assertEqual(ipa.app_info(renew.app_dir(app_id) / "original.prev.ipa")["version"], "1.0")

    def test_failed_download_keeps_everything_and_leaves_no_hold_file(self):
        app_id = self.add(installed_ipa="x")
        self.release()
        self.raises = ConnectionError("coupure")
        self.assertFalse(self.refresh(app_id))
        d = renew.app_dir(app_id)
        self.assertFalse((d / "original.hold").exists())
        self.assertFalse((d / "original.prev.ipa").exists())

    # -- MinimumOSVersion
    def test_tvos_too_old_keeps_the_app_and_reports_the_blocked_version(self):
        app_id = self.add(device_os="18.4")
        self.release()
        self.raises = ipa.OsTooOld("26.0", "18.4")
        with mock.patch.object(renew, "notify", mock.AsyncMock()) as notify:
            self.assertFalse(self.refresh(app_id))
        row = self.row(app_id)
        self.assertEqual((row["update_tag"], row["update_block"], row["bad_tag"]), ("v2", "tvos:26.0", "v2"))
        self.assertEqual(row["source_tag"], "v1")
        self.assertEqual(self.calls[0][1]["max_os"], "18.4")
        notify.assert_awaited_once()
        self.raises = None
        self.calls.clear()
        self.assertFalse(self.refresh(app_id))                                # pas retéléchargée à chaque passage
        self.assertEqual(self.calls, [])

    def test_blocked_version_is_retried_once_the_tv_is_updated(self):
        app_id = self.add(device_os="18.4", bad_tag="v2", bad_key="k2", update_tag="v2", update_block="tvos:26.0")
        db.execute("UPDATE devices SET os_version = '26.1' WHERE id = (SELECT device_id FROM apps WHERE id = ?)", app_id)
        self.release()
        self.assertTrue(self.refresh(app_id))
        row = self.row(app_id)
        self.assertEqual((row["source_tag"], row["bad_tag"], row["update_tag"], row["update_block"]), ("v2", None, None, None))

    def test_stale_tv_version_is_reread_before_lifting_a_block(self):
        app_id = self.add(device_os="18.4", bad_tag="v2", bad_key="k2", update_tag="v2", update_block="tvos:26.0")
        device_id = self.row(app_id)["device_id"]
        renew._OS_REFRESHED.pop(device_id, None)

        async def fake_refresh(dev_id):
            db.execute("UPDATE devices SET os_version = '26.2' WHERE id = ?", dev_id)
            return {}
        with mock.patch.object(devices, "refresh_info", fake_refresh):
            app = self.row(app_id)
            asyncio.run(renew._lift_os_block(app))
        self.assertIsNone(self.row(app_id)["bad_tag"])

    def test_first_download_too_new_is_an_error_not_a_silent_skip(self):
        app_id = self.add(device_os="18.4")
        (renew.app_dir(app_id) / "original.ipa").unlink()
        self.release()
        self.raises = ipa.OsTooOld("26.0", "18.4")
        with self.assertRaises(ipa.OsTooOld):
            self.refresh(app_id)


# ---------- renouvellement : mise à jour facultative, retour arrière, tvOS ----------

class Renewal(unittest.TestCase):
    """_renew avec un faux plumesign : la mise à jour qui échoue ne rend pas l'app rouge."""

    def setUp(self):
        db.init()
        self.signed = []
        self.fail_on = set()           # contenus d'IPA que la signature refuse
        self.patches = [
            mock.patch.object(plume, "accounts", lambda: [{"email": "a@b.c", "ok": 1}]),
            mock.patch.object(plume, "default_account", lambda: "a@b.c"),
            mock.patch.object(plume, "ensure_device", mock.AsyncMock()),
            mock.patch.object(plume, "sign", self.fake_sign),
            mock.patch.object(devices, "check_reachable", mock.AsyncMock()),
            mock.patch.object(devices, "install", mock.AsyncMock()),
            mock.patch.object(devices, "refresh_info", mock.AsyncMock(return_value={})),
            mock.patch.object(ipa, "profile_expiry", lambda p: time.time() + 168 * HOUR),
            mock.patch.object(renew, "notify", mock.AsyncMock()),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    async def fake_sign(self, email, original, out, on_lock=None):
        data = Path(original).read_bytes()
        self.signed.append(ipa.app_info(Path(original))["version"])
        if sha(data) in self.fail_on:
            raise RuntimeError("signature : plumesign a échoué")
        Path(out).write_bytes(b"signed:" + data)
        return False

    def add(self, version="1.0", os_version="26.0", **cols):
        device_id = db.execute("INSERT INTO devices (name, pair_identifier, udid, os_version, created_at) "
                               "VALUES ('TV', ?, 'U', ?, 0)", f"p{time.time_ns()}", os_version)
        data = make_ipa(version=version)
        app_id = db.execute(
            "INSERT INTO apps (name, source_type, source_tag, source_key, bundle_id, version, source_sha256, installed_ipa, "
            "device_id, account_email, status, expires_at, signed_at, created_at) "
            "VALUES ('R', 'upload', 'v1', 'k1', 'com.example.tv', ?, ?, ?, ?, 'a@b.c', 'ok', ?, ?, 0)",
            version, sha(data), sha(data), device_id, time.time() + 120 * HOUR, time.time())
        (renew.app_dir(app_id) / "original.ipa").write_bytes(data)
        if cols:
            db.update("apps", app_id, **cols)
        return app_id, data

    def row(self, app_id):
        return db.row("SELECT * FROM apps WHERE id = ?", app_id)

    def swap_in_update(self, app_id, old, new_version="2.0"):
        """Simule une mise à jour téléchargée : l'ancienne IPA devient la précédente, la nouvelle est en attente."""
        d = renew.app_dir(app_id)
        new = make_ipa(version=new_version)
        (d / "original.prev.ipa").write_bytes(old)
        (d / "original.ipa").write_bytes(new)
        db.update("apps", app_id, source_tag="v2", source_key="k2", version=new_version, source_sha256=sha(new),
                  prev_tag="v1", prev_key="k1", prev_version="1.0", prev_sha256=sha(old))
        return new

    def run_renew(self, app_id, trigger):
        asyncio.run(renew._renew(app_id, trigger))

    def test_failed_update_does_not_turn_a_valid_app_red(self):
        app_id, old = self.add()
        new = self.swap_in_update(app_id, old)
        self.fail_on = {sha(new), sha(old)}                                   # rien ne passe : ni la nouvelle, ni l'ancienne
        (renew.app_dir(app_id) / "original.prev.ipa").unlink()                # pas de retour arrière possible
        self.run_renew(app_id, "nouvelle version")
        row = self.row(app_id)
        self.assertEqual(row["status"], "ok")
        self.assertIsNone(row["last_error"])
        self.assertIn("signature", row["update_error"])
        self.assertEqual(row["fail_count"], 1)
        self.assertGreater(row["next_attempt_at"], time.time())
        self.assertTrue(renew.pending_update(row))                            # retentée plus tard par la boucle
        self.assertLess(row["expires_at"] - time.time(), 121 * HOUR)

    def test_same_failure_during_the_renewal_window_is_a_real_failure(self):
        app_id, old = self.add(expires_at=time.time() + 20 * HOUR)
        self.fail_on = {sha(old)}
        self.run_renew(app_id, "auto")
        row = self.row(app_id)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["fail_count"], 1)

    def test_broken_update_falls_back_to_the_previous_version(self):
        app_id, old = self.add()
        new = self.swap_in_update(app_id, old)
        self.fail_on = {sha(new)}
        self.run_renew(app_id, "nouvelle version")
        row, d = self.row(app_id), renew.app_dir(app_id)
        self.assertEqual(self.signed, ["2.0", "1.0"])                         # la nouvelle d'abord, puis l'ancienne
        self.assertEqual(row["status"], "ok")
        self.assertEqual((row["source_tag"], row["version"], row["source_sha256"]), ("v1", "1.0", sha(old)))
        self.assertEqual((row["bad_tag"], row["bad_key"]), ("v2", "k2"))      # la version cassée n'est pas retéléchargée
        self.assertEqual(row["installed_ipa"], sha(old))
        self.assertFalse(renew.pending_update(row))
        self.assertEqual((d / "original.ipa").read_bytes(), old)
        self.assertFalse((d / "original.prev.ipa").exists())
        self.assertIsNone(row["prev_tag"])
        self.assertEqual(row["fail_count"], 0)
        self.assertIsNone(row["update_error"])

    def test_no_fallback_when_the_previous_version_fails_too(self):
        """Si l'ancienne échoue aussi, le problème n'est pas la nouvelle version : tout est remis en place."""
        app_id, old = self.add()
        new = self.swap_in_update(app_id, old)
        self.fail_on = {sha(new), sha(old)}
        self.run_renew(app_id, "nouvelle version")
        row, d = self.row(app_id), renew.app_dir(app_id)
        self.assertEqual(self.signed, ["2.0", "1.0"])
        self.assertEqual((d / "original.ipa").read_bytes(), new)
        self.assertEqual((d / "original.prev.ipa").read_bytes(), old)
        self.assertEqual((row["source_tag"], row["bad_tag"], row["prev_tag"]), ("v2", None, "v1"))
        self.assertIn("signature", row["update_error"])
        self.assertEqual(row["status"], "ok")

    def test_transient_failures_never_discard_a_release(self):
        app_id, old = self.add()
        self.swap_in_update(app_id, old)

        async def boom(email, original, out):
            raise plume.Transient("anisette indisponible")
        with mock.patch.object(plume, "sign", boom):
            self.run_renew(app_id, "nouvelle version")
        row = self.row(app_id)
        self.assertEqual((row["source_tag"], row["bad_tag"]), ("v2", None))
        self.assertEqual(row["status"], "ok")
        self.assertEqual((row["next_attempt_at"] - time.time()) // 60, 9)      # transitoire : 10 min

    def test_success_records_what_is_installed(self):
        app_id, old = self.add()
        new = self.swap_in_update(app_id, old)
        db.update("apps", app_id, update_error="avant", fail_count=3)
        self.run_renew(app_id, "nouvelle version")
        row = self.row(app_id)
        self.assertEqual((row["status"], row["installed_ipa"], row["fail_count"], row["update_error"]), ("ok", sha(new), 0, None))
        self.assertTrue(renew.has_previous(app_id))                           # gardée pour « version précédente »
        self.assertFalse(renew.pending_update(row))

    def test_startup_adopts_the_present_ipa_as_the_installed_one(self):
        app_id, old = self.add()
        db.update("apps", app_id, installed_ipa=None, source_sha256=None)
        renew.startup()
        row = self.row(app_id)
        self.assertEqual((row["installed_ipa"], row["source_sha256"]), (sha(old), sha(old)))
        self.assertFalse(renew.pending_update(row))

    def test_nothing_new_does_not_resign(self):
        app_id, old = self.add()
        with mock.patch.object(renew, "refresh_source", mock.AsyncMock(return_value=False)):
            self.run_renew(app_id, "nouvelle version")
        self.assertEqual(self.signed, [])
        self.assertEqual(self.row(app_id)["status"], "ok")

    def test_tvos_preflight_blocks_before_any_apple_call(self):
        app_id, _ = self.add(os_version="18.4")
        data = make_ipa(min_os="26.0")
        (renew.app_dir(app_id) / "original.ipa").write_bytes(data)
        db.update("apps", app_id, expires_at=time.time() + 100 * HOUR)
        self.run_renew(app_id, "auto")
        row = self.row(app_id)
        self.assertEqual(row["status"], "error")
        self.assertIn("demande tvOS 26.0", row["last_error"])
        self.assertEqual(self.signed, [])
        self.assertEqual(plume.ensure_device.await_count, 0)
        self.assertGreaterEqual((row["next_attempt_at"] - time.time()) / 3600, 5)   # action de l'utilisateur : espacé
        self.assertEqual(row["needs_action"], "config")

    def test_tvos_preflight_rereads_a_stale_tv_version(self):
        app_id, _ = self.add(os_version="18.4")
        (renew.app_dir(app_id) / "original.ipa").write_bytes(make_ipa(min_os="26.0"))

        async def updated(dev_id):
            db.execute("UPDATE devices SET os_version = '26.1' WHERE id = ?", dev_id)
            return {}
        with mock.patch.object(devices, "refresh_info", updated):
            self.run_renew(app_id, "manuel")
        self.assertEqual(self.row(app_id)["status"], "ok")
        self.assertEqual(self.signed, ["1.0"])

    def test_failures_back_off_instead_of_repeating(self):
        app_id, old = self.add(expires_at=time.time() + 100 * HOUR)
        self.fail_on = {sha(old)}
        waits = []
        for _ in range(4):
            self.run_renew(app_id, "auto")
            waits.append((self.row(app_id)["next_attempt_at"] - time.time()) / 60)
        self.assertEqual(self.row(app_id)["fail_count"], 4)
        self.assertEqual([round(w) for w in waits], [10, 20, 40, 45])          # le délai double jusqu'à RETRY_MINUTES
        self.assertEqual(renew.due(self.row(app_id), time.time()), False)    # pas avant l'heure

    def test_profile_inside_the_renewal_window_does_not_loop(self):
        app_id, _ = self.add()
        with mock.patch.object(ipa, "profile_expiry", lambda p: time.time() + 30 * HOUR):
            self.run_renew(app_id, "manuel")
        row = self.row(app_id)
        self.assertEqual(row["status"], "ok")
        self.assertGreater(row["next_attempt_at"], time.time() + 5 * HOUR)
        self.assertFalse(renew.due(row, time.time()))

    def test_manual_renew_resets_the_backoff(self):
        app_id, _ = self.add(status="error", fail_count=5, next_attempt_at=time.time() + 6 * HOUR)
        with TestClient(main.app) as client, mock.patch.object(renew, "spawn", return_value=True):
            self.assertEqual(client.post(f"/api/apps/{app_id}/renew", headers=HEADERS).status_code, 200)
        row = self.row(app_id)
        self.assertEqual((row["fail_count"], row["next_attempt_at"]), (0, None))


# ---------- API : remplacement d'IPA, retour arrière, réglages par app ----------

class AppApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        self.spawned = []
        patcher = mock.patch.object(renew, "spawn", lambda app_id, trigger="manuel": self.spawned.append((app_id, trigger)) or True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, data, name="a.ipa"):
        return self.client.post("/api/apps", headers=HEADERS, data={"source_type": "upload"},
                                files={"file": (name, data, "application/octet-stream")})

    def replace(self, app_id, data):
        return self.client.post(f"/api/apps/{app_id}/ipa", headers=HEADERS,
                                files={"file": ("b.ipa", data, "application/octet-stream")})

    def new_app(self, bundle):
        r = self.upload(make_ipa(bundle=bundle, version="1.0"))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def row(self, app_id):
        return db.row("SELECT * FROM apps WHERE id = ?", app_id)

    def test_upload_stores_the_checksum(self):
        data = make_ipa(bundle="com.example.sha")
        app_id = self.upload(data).json()["id"]
        self.assertEqual(self.row(app_id)["source_sha256"], sha(data))

    def test_replace_uploaded_ipa_keeps_history_and_previous_version(self):
        app_id = self.new_app("com.example.replace")
        old = (renew.app_dir(app_id) / "original.ipa").read_bytes()
        db.update("apps", app_id, installed_ipa=sha(old), status="ok", expires_at=time.time() + 100 * HOUR,
                  account_email="a@b.c", cert_serial="C1")
        db.log("ancienne ligne de journal", app_id=app_id)
        new = make_ipa(bundle="com.example.replace", version="2.0")
        r = self.replace(app_id, new)
        self.assertEqual(r.status_code, 200, r.text)
        row, d = self.row(app_id), renew.app_dir(app_id)
        self.assertEqual((row["version"], row["source_sha256"], row["account_email"]), ("2.0", sha(new), "a@b.c"))
        self.assertEqual((d / "original.ipa").read_bytes(), new)
        self.assertEqual((d / "original.prev.ipa").read_bytes(), old)
        self.assertEqual((row["prev_version"], row["prev_sha256"]), ("1.0", sha(old)))
        self.assertFalse((d / "signed.ipa").exists())
        self.assertEqual(self.spawned[-1], (app_id, "nouvelle IPA"))
        self.assertTrue(renew.pending_update(row))
        self.assertIn("ancienne ligne de journal", [e["message"] for e in db.rows("SELECT message FROM events WHERE app_id = ?", app_id)])

    def test_replace_fixes_a_missing_ipa_after_a_restore(self):
        app_id = self.new_app("com.example.missing")
        (renew.app_dir(app_id) / "original.ipa").unlink()
        db.update("apps", app_id, status="error", last_error="IPA d'origine absente : réimporte l'app")
        r = self.replace(app_id, make_ipa(bundle="com.example.missing", version="3.0"))
        self.assertEqual(r.status_code, 200, r.text)
        row = self.row(app_id)
        self.assertEqual((row["status"], row["last_error"], row["version"]), ("pending", None, "3.0"))
        self.assertFalse(renew.has_previous(app_id))

    def test_replace_checks_the_file(self):
        app_id = self.new_app("com.example.checks")
        self.assertEqual(self.replace(app_id, b"nope").json()["detail"], "not_an_ipa")
        self.assertEqual(self.replace(app_id, make_ipa(bundle="com.example.checks", platform="iphoneos")).json()["detail"],
                         "not_tvos:iphoneos")
        r = self.replace(app_id, make_ipa(bundle="com.example.other"))
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "bundle_mismatch:com.example.checks"))
        r = self.replace(app_id, make_ipa(bundle="com.example.checks", min_os="99.0"))
        self.assertEqual(r.status_code, 200)                                    # TV inconnue : pas de blocage

    def test_replace_refuses_too_recent_ipa_for_a_known_tv(self):
        app_id = self.new_app("com.example.oldtv")
        device_id = db.execute("INSERT INTO devices (name, pair_identifier, os_version, created_at) VALUES ('TV', ?, '18.4', 0)",
                               f"p{time.time_ns()}")
        db.update("apps", app_id, device_id=device_id)
        r = self.replace(app_id, make_ipa(bundle="com.example.oldtv", min_os="26.0"))
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "tvos_too_old:26.0"))

    def test_replace_is_for_uploaded_apps_only_and_not_while_running(self):
        gh = db.execute("INSERT INTO apps (name, source_type, source, created_at) VALUES ('G', 'github', 'o/r', 0)")
        r = self.replace(gh, make_ipa())
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "not_an_upload"))
        app_id = self.new_app("com.example.busy")
        renew._RUNNING.add(app_id)
        try:
            self.assertEqual(self.replace(app_id, make_ipa(bundle="com.example.busy")).json()["detail"], "app_busy")
        finally:
            renew._RUNNING.discard(app_id)
        self.assertEqual(self.replace(99999, make_ipa()).status_code, 404)

    def test_replace_is_not_stopped_by_the_small_body_limit(self):
        app_id = self.new_app("com.example.big")
        big = make_ipa(bundle="com.example.big", version="9.0", extra=os.urandom(2 * 1024 * 1024))   # plus de 1 Mo
        self.assertEqual(self.replace(app_id, big).status_code, 200)

    def test_rollback_restores_the_previous_version_and_skips_the_other(self):
        app_id = self.new_app("com.example.rollback")
        d = renew.app_dir(app_id)
        old = (d / "original.ipa").read_bytes()
        new = make_ipa(bundle="com.example.rollback", version="2.0")
        (d / "original.prev.ipa").write_bytes(old)
        (d / "original.ipa").write_bytes(new)
        db.update("apps", app_id, source_type="github", source="o/r", source_tag="v2", source_key="k2", version="2.0",
                  source_sha256=sha(new), prev_tag="v1", prev_key="k1", prev_version="1.0", prev_sha256=sha(old),
                  notes="new notes")
        r = self.client.post(f"/api/apps/{app_id}/rollback", headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        row = self.row(app_id)
        self.assertEqual((d / "original.ipa").read_bytes(), old)
        self.assertEqual((row["source_tag"], row["version"], row["source_sha256"]), ("v1", "1.0", sha(old)))
        self.assertEqual((row["bad_tag"], row["bad_key"], row["update_tag"], row["update_block"]), ("v2", "k2", "v2", "rollback"))
        self.assertFalse(renew.has_previous(app_id))
        self.assertEqual(self.spawned[-1], (app_id, "retour arrière"))
        self.assertEqual(self.client.post(f"/api/apps/{app_id}/rollback", headers=HEADERS).json()["detail"], "no_previous")

    def test_rollback_marker_survives_the_release_check(self):
        app = app_row(source_tag="v1", source_key="k1", bad_tag="v2", bad_key="k2", update_tag="v2", update_block="rollback")
        found = {"tag": "v2", "key": "k2", "url": "u", "sha256": None}
        with mock.patch.object(renew, "_latest", return_value=found):
            self.assertFalse(asyncio.run(renew.new_release(app)))
        self.assertEqual(app["update_tag"], "v2")
        found = {"tag": "v3", "key": "k3", "url": "u", "sha256": None}          # une vraie nouvelle release : installée
        with mock.patch.object(renew, "_latest", return_value=found):
            self.assertTrue(asyncio.run(renew.new_release(app)))

    def test_update_now_endpoint(self):
        app_id = self.new_app("com.example.update")
        self.assertEqual(self.client.post(f"/api/apps/{app_id}/update", headers=HEADERS).json()["detail"], "not_followed")
        db.update("apps", app_id, source_type="github", source="o/r", fail_count=3, next_attempt_at=time.time() + 5 * HOUR)
        r = self.client.post(f"/api/apps/{app_id}/update", headers=HEADERS)
        self.assertEqual(r.json(), {"ok": True, "started": True})
        self.assertEqual(self.spawned[-1], (app_id, "mise à jour"))
        row = self.row(app_id)
        self.assertEqual((row["fail_count"], row["next_attempt_at"]), (0, None))

    def test_patch_auto_update_and_pin(self):
        app_id = self.new_app("com.example.pin")
        self.spawned.clear()
        db.update("apps", app_id, source_type="github", source="o/r", source_tag="v2", update_tag="v3")
        r = self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"auto_update": False})
        self.assertEqual(r.json()["auto_update"], 0)
        self.assertEqual(self.spawned, [])                                       # couper la mise à jour auto n'installe rien
        r = self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"pinned_tag": " v1.0 "})
        self.assertEqual(r.json()["pinned_tag"], "v1.0")
        self.assertIsNone(r.json()["update_tag"])
        self.assertEqual(self.spawned[-1], (app_id, "mise à jour"))              # appliquée tout de suite
        self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"pinned_tag": ""})
        self.assertIsNone(self.row(app_id)["pinned_tag"])
        self.assertEqual(self.spawned[-1], (app_id, "nouvelle version"))
        n = len(self.spawned)
        self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"pinned_tag": ""})
        self.assertEqual(len(self.spawned), n)                                   # inchangé : rien à relancer

    def test_pin_validation(self):
        up = self.new_app("com.example.pinbad")
        r = self.client.patch(f"/api/apps/{up}", headers=HEADERS, json={"pinned_tag": "v1"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "pin_github_only"))
        gh = db.execute("INSERT INTO apps (name, source_type, source, created_at) VALUES ('G', 'github', 'o/r', 0)")
        r = self.client.patch(f"/api/apps/{gh}", headers=HEADERS, json={"pinned_tag": "x" * 300})
        self.assertEqual(r.json()["detail"], "invalid_tag")

    def test_state_exposes_the_new_fields(self):
        app_id = self.new_app("com.example.state")
        d = renew.app_dir(app_id)
        (d / "original.prev.ipa").write_bytes(b"x")
        db.update("apps", app_id, source_type="url", source="https://example.com/a.ipa", source_tag="https://example.com/a.ipa",
                  source_key=None, update_tag="v9", update_notes="N")
        a = next(x for x in self.client.get("/api/state").json()["apps"] if x["id"] == app_id)
        self.assertTrue(a["can_rollback"])
        self.assertTrue(a["untracked_link"])
        self.assertEqual((a["update_tag"], a["update_notes"], a["auto_update"]), ("v9", "N", 1))
        self.assertIn("update", self.client.get("/api/state").json())

    def test_add_link_with_userinfo_has_a_clear_error(self):
        r = self.client.post("/api/apps", headers=HEADERS, data={"source_type": "url", "source": "https://u:p@example.com/a.ipa"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "url_userinfo"))

    def test_add_with_github_rate_limit_has_a_code(self):
        err = ipa.SourceUnavailable("limite de requêtes GitHub atteinte", "gh_rate_limit")
        with mock.patch.object(ipa, "latest_github", side_effect=err):
            r = self.client.post("/api/apps", headers=HEADERS, data={"source_type": "github", "source": "o/limited"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "gh_rate_limit"))
        self.assertEqual(db.rows("SELECT id FROM apps WHERE source = 'o/limited'"), [])

    def test_add_refuses_an_app_too_recent_for_the_tv(self):
        device_id = db.execute("INSERT INTO devices (name, pair_identifier, os_version, created_at) VALUES ('TV', ?, '18.4', 0)",
                               f"p{time.time_ns()}")
        r = self.client.post("/api/apps", headers=HEADERS, data={"source_type": "upload", "device_id": device_id},
                             files={"file": ("a.ipa", make_ipa(bundle="com.example.new", min_os="26.0"), "application/octet-stream")})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "tvos_too_old:26.0"))

    # -- corps sans taille
    def test_chunked_body_without_length_is_refused_on_public_routes(self):
        def body():
            yield b'{"password": "' + b"x" * 1000
            yield b'"}'
        r = self.client.post("/api/auth/login", headers=HEADERS, content=body())
        self.assertEqual((r.status_code, r.json()["detail"]), (411, "content_length_required"))

    def test_bodyless_requests_and_normal_posts_still_work(self):
        self.assertEqual(self.client.post("/api/onboarding/complete", headers=HEADERS).status_code, 200)
        self.assertEqual(self.client.delete("/api/apps/987654", headers=HEADERS).status_code, 200)

    def test_password_length_is_bounded(self):
        r = self.client.post("/api/auth/login", headers=HEADERS, json={"password": "x" * 5000})
        self.assertEqual(r.status_code, 422)


# ---------- recherche de mise à jour de tvsign ----------

class SelfUpdate(unittest.TestCase):
    def setUp(self):
        db.init()
        selfupdate.STATUS.update(latest=None, url=None, checked_at=None, error=None)
        selfupdate._last = 0.0
        for key in ("update_dismissed", "update_seen", "update_check"):
            settings.raw_set(key, None)
        patcher = mock.patch.object(selfupdate, "VERSION", "1.0.0")
        patcher.start()
        self.addCleanup(patcher.stop)

    def release(self, tag="v1.0.1", **over):
        data = {"tag_name": tag, "html_url": f"https://github.com/esalwey/tvsign/releases/tag/{tag}", "draft": False, "prerelease": False}
        data.update(over)
        return data

    def check(self, data=None, error=None, force=False):
        fake = mock.Mock(side_effect=error) if error else mock.Mock(return_value=data)
        with mock.patch.object(ipa, "_get_json", fake):
            asyncio.run(selfupdate.check(force=force))
        return fake

    def test_version_comparison(self):
        self.assertTrue(selfupdate.newer("1.0.1", "1.0.0"))
        self.assertTrue(selfupdate.newer("v1.1", "1.0.9"))
        self.assertTrue(selfupdate.newer("2.0.0", "1.9.9"))
        self.assertFalse(selfupdate.newer("1.0.0", "1.0.0"))
        self.assertFalse(selfupdate.newer("1.0.0-rc1", "1.0.0"))
        self.assertTrue(selfupdate.newer("1.0.0", "1.0.0-rc1"))
        self.assertFalse(selfupdate.newer("garbage", "1.0.0"))

    def test_available_update(self):
        self.check(self.release())
        info = selfupdate.available()
        self.assertEqual(info["version"], "1.0.1")
        self.assertIn(info["edition"], ("docker", "windows"))
        self.assertTrue(info["url"].startswith("https://github.com/"))

    def test_up_to_date_shows_nothing(self):
        self.check(self.release("v1.0.0"))
        self.assertIsNone(selfupdate.available())
        self.assertTrue(selfupdate.summary()["checked_at"])

    def test_foreign_link_is_never_shown(self):
        self.check(self.release(html_url="https://evil.example/x"))
        self.assertIsNone(selfupdate.available()["url"])

    def test_checks_once_a_day(self):
        fake = self.check(self.release())
        self.assertEqual(fake.call_count, 1)
        fake = self.check(self.release("v1.0.2"))
        self.assertEqual(fake.call_count, 0)
        fake = self.check(self.release("v1.0.2"), force=True)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(selfupdate.available()["version"], "1.0.2")

    def test_failure_is_soft_and_retried_within_the_hour(self):
        self.check(error=OSError("hors ligne"))
        self.assertIsNone(selfupdate.available())
        self.assertEqual(selfupdate.summary()["error"], "hors ligne")
        # la prochaine recherche est due dans une heure, pas dans un jour
        self.assertAlmostEqual(selfupdate._last + selfupdate.CHECK_EVERY - time.time(), selfupdate.RETRY_AFTER, delta=5)
        selfupdate._last -= selfupdate.RETRY_AFTER
        self.check(self.release())
        self.assertIsNone(selfupdate.summary()["error"])
        self.assertEqual(selfupdate.available()["version"], "1.0.1")

    def test_repository_without_release_is_not_an_error(self):
        self.check(error=ipa.SourceUnavailable("dépôt GitHub introuvable", "gh_not_found"))
        self.assertIsNone(selfupdate.available())
        self.assertIsNone(selfupdate.summary()["error"])

    def test_pre_releases_and_drafts_are_ignored(self):
        self.check(self.release("v2.0.0", prerelease=True))
        self.assertIsNone(selfupdate.available())
        self.check(self.release("v2.0.0", draft=True), force=True)
        self.assertIsNone(selfupdate.available())

    def test_dismiss_hides_this_version_but_not_the_next(self):
        self.check(self.release())
        selfupdate.dismiss()
        self.assertIsNone(selfupdate.available())
        self.check(self.release("v1.0.2"), force=True)
        self.assertEqual(selfupdate.available()["version"], "1.0.2")

    def test_opt_out_sends_no_request(self):
        settings.update({"update_check": "off"})
        try:
            fake = self.check(self.release())
            self.assertEqual(fake.call_count, 0)
            self.assertIsNone(selfupdate.available())
            self.assertFalse(selfupdate.summary()["enabled"])
        finally:
            settings.update({"update_check": None})
        self.assertTrue(selfupdate.enabled())

    def test_setting_validation_and_environment(self):
        with self.assertRaises(ValueError):
            settings.update({"update_check": "maybe"})
        with mock.patch.dict(os.environ, {"TVSIGN_UPDATE_CHECK": "0"}):
            self.assertFalse(selfupdate.enabled())
        self.assertTrue(selfupdate.enabled())

    def test_dev_build_does_not_check(self):
        with mock.patch.object(selfupdate, "VERSION", "1.0.0-dev"):
            fake = self.check(self.release())
        self.assertEqual(fake.call_count, 0)

    def test_api(self):
        with TestClient(main.app) as client:
            with mock.patch.object(ipa, "_get_json", return_value=self.release("v1.0.5")):
                r = client.post("/api/update/check", headers=HEADERS)
            self.assertEqual((r.json()["latest"], r.json()["available"]), ("1.0.5", True))
            self.assertEqual(client.get("/api/state").json()["update"]["version"], "1.0.5")
            self.assertEqual(client.get("/api/settings").json()["update"]["latest"], "1.0.5")
            self.assertEqual(client.post("/api/update/dismiss", headers=HEADERS).status_code, 200)
            self.assertIsNone(client.get("/api/state").json()["update"])


# ---------- migration d'une base existante ----------

class Migration(unittest.TestCase):
    def test_existing_database_keeps_its_data_and_gets_safe_defaults(self):
        path = Path(tempfile.mkdtemp(dir=DATA)) / "tvsign.db"
        conn = sqlite3.connect(path)
        conn.executescript(db.SCHEMA)          # schéma 1.0.0 : sans aucune des colonnes ajoutées après coup
        conn.execute("INSERT INTO devices (name, pair_identifier, created_at) VALUES ('Salon', 'P1', 1)")
        conn.execute("INSERT INTO apps (name, source_type, source, bundle_id, device_id, account_email, status, "
                     "expires_at, created_at) VALUES ('Nuvio', 'github', 'o/r', 'com.n', 1, 'a@b.c', 'ok', 5000, 1)")
        conn.execute("INSERT INTO account_devices VALUES ('a@b.c', 'U1')")
        conn.commit()
        conn.close()
        with mock.patch.object(db, "DB_PATH", path):
            db.init()
            db.init()                          # idempotent
            app = db.row("SELECT * FROM apps")
            self.assertEqual(db.row("SELECT * FROM account_devices")["udid"], "U1")
            self.assertEqual(db.row("SELECT name FROM devices")["name"], "Salon")
        self.assertEqual((app["name"], app["account_email"], app["device_id"], app["expires_at"]), ("Nuvio", "a@b.c", 1, 5000))
        self.assertEqual((app["auto_update"], app["fail_count"]), (1, 0))      # par défaut : comme avant
        for col in ("pinned_tag", "notes", "update_tag", "update_notes", "update_block", "update_error", "installed_ipa",
                    "prev_tag", "prev_key", "prev_version", "prev_sha256"):
            self.assertIn(col, app)
            self.assertIsNone(app[col])
        self.assertFalse(renew.pending_update(app))                             # rien d'installé n'est « en attente »
        self.assertTrue(list(path.parent.glob("tvsign.db.pre-migrate-*")))      # copie de sécurité avant migration


if __name__ == "__main__":
    unittest.main()
