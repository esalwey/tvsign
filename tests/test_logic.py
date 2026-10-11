"""Contrôles de la logique de renouvellement, sans plumesign ni Apple TV.

    python -m unittest discover -s tests

Dépendances : celles de requirements.txt, plus httpx pour le client de test de FastAPI.
"""
import asyncio
import io
import os
import plistlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

DATA = tempfile.mkdtemp(prefix="tvsign-test-")
os.environ.update(TVSIGN_DATA=DATA, TVSIGN_AUTH="none", TVSIGN_PLUMESIGN=str(Path(DATA) / "absent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, ipa, main, plume, renew  # noqa: E402

HEADERS = {"X-Tvsign": "1"}


def make_ipa(platform="appletvos", bundle="com.example.tv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Payload/Test.app/Info.plist", plistlib.dumps({
            "CFBundleName": "Test", "CFBundleIdentifier": bundle, "CFBundleShortVersionString": "1.0",
            "DTPlatformName": platform}))
    return buf.getvalue()


def app_row(**over) -> dict:
    row = dict(id=1, name="Test", source_type="github", source="o/r", source_tag="t1", source_key="k1",
               bad_tag=None, bad_key=None, bundle_id="com.example.tv", enabled=1, status="ok",
               expires_at=None, notified_level=None, channel="stable", preset=None, asset_name=None,
               source_filter=None)
    row.update(over)
    return row


class SourceIdentity(unittest.TestCase):
    """Un tag identique ne suffit pas : un asset remplacé sous le même tag doit être vu."""

    def test_same_tag_same_key(self):
        self.assertTrue(renew._unchanged(app_row(), "t1", "k1"))

    def test_same_tag_new_key(self):
        self.assertFalse(renew._unchanged(app_row(), "t1", "k2"))

    def test_new_tag(self):
        self.assertFalse(renew._unchanged(app_row(), "t2", "k1"))

    def test_legacy_row_without_key_is_not_a_change(self):
        self.assertTrue(renew._unchanged(app_row(source_key=None), "t1", "k2"))

    def test_rejected_is_per_content(self):
        bad = app_row(bad_tag="t1", bad_key="k1")
        self.assertTrue(renew._rejected(bad, "t1", "k1"))
        self.assertFalse(renew._rejected(bad, "t1", "k2"))      # IPA republiée : on réessaie

    def test_new_release_detects_replaced_asset(self):
        found = {"tag": "t1", "key": "k2", "url": "u", "sha256": None}
        orig = renew._latest
        renew._latest = lambda app: found
        try:
            self.assertTrue(asyncio.run(renew.new_release(app_row())))
            self.assertFalse(asyncio.run(renew.new_release(app_row(source_key="k2"))))
            self.assertFalse(asyncio.run(renew.new_release(app_row(source_key=None))))
        finally:
            renew._latest = orig


class Refresh(unittest.TestCase):
    """refresh_source télécharge quand le contenu change, pas quand seul le tag est identique."""

    def setUp(self):
        db.init()
        self.calls = []
        self.saved = ipa.latest_github, ipa.download, ipa.url_key
        ipa.download = self.fake_download

    def tearDown(self):
        ipa.latest_github, ipa.download, ipa.url_key = self.saved

    def fake_download(self, url, dest, sha256=None, expect_bundle=None):
        self.calls.append(url)
        dest.write_bytes(make_ipa())
        return {"version": "2.0", "bundle_id": "com.example.tv", "sha256": "abc"}

    def add(self, source_type, source, tag, key):
        app_id = db.execute(
            "INSERT INTO apps (name, source_type, source, source_tag, source_key, bundle_id, created_at) "
            "VALUES ('R', ?, ?, ?, ?, 'com.example.tv', 0)", source_type, source, tag, key)
        (renew.app_dir(app_id) / "original.ipa").write_bytes(make_ipa())
        return app_id

    def run_refresh(self, app_id):
        return asyncio.run(renew.refresh_source(db.row("SELECT * FROM apps WHERE id = ?", app_id)))

    def release(self, tag, key):
        ipa.latest_github = lambda *a, **k: {"tag": tag, "url": "https://x/" + tag, "sha256": None, "key": key}

    def test_fixed_tag_replaced_asset(self):
        app_id = self.add("github", "o/r", "nightly", "k1")
        self.release("nightly", "k1")
        self.assertFalse(self.run_refresh(app_id))
        self.assertEqual(self.calls, [])
        self.release("nightly", "k2")
        self.assertTrue(self.run_refresh(app_id))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(db.row("SELECT source_key FROM apps WHERE id = ?", app_id)["source_key"], "k2")

    def test_legacy_row_adopts_key_without_download(self):
        app_id = self.add("github", "o/r", "v1", None)
        self.release("v1", "k1")
        self.assertFalse(self.run_refresh(app_id))
        self.assertEqual(self.calls, [])
        self.assertEqual(db.row("SELECT source_key FROM apps WHERE id = ?", app_id)["source_key"], "k1")

    def test_link_followed_by_validators(self):
        url = "https://example.com/latest.ipa"
        app_id = self.add("url", url, url, "etag1")
        ipa.url_key = lambda u: "etag1"
        self.assertFalse(self.run_refresh(app_id))
        ipa.url_key = lambda u: "etag2"
        self.assertTrue(self.run_refresh(app_id))
        self.assertEqual(self.calls, [url])

    def test_link_without_validators_is_kept(self):
        url = "https://example.com/latest.ipa"
        app_id = self.add("url", url, url, None)
        ipa.url_key = lambda u: None
        self.assertFalse(self.run_refresh(app_id))
        self.assertEqual(self.calls, [])


class Alerts(unittest.TestCase):
    def setUp(self):
        db.init()
        self.app_id = db.execute("INSERT INTO apps (name, source_type, created_at) VALUES ('A', 'upload', 0)")
        self.orig = renew.notify

    def tearDown(self):
        renew.notify = self.orig

    def level(self):
        return db.row("SELECT notified_level FROM apps WHERE id = ?", self.app_id)["notified_level"]

    def test_level_kept_only_when_sent(self):
        async def fail(*a, **k):
            return False

        async def sent(*a, **k):
            return True
        app = db.row("SELECT * FROM apps WHERE id = ?", self.app_id)
        renew.notify = fail
        asyncio.run(renew.alert(app, "x", 0))
        self.assertIsNone(self.level())          # non envoyée : sera retentée
        renew.notify = sent
        asyncio.run(renew.alert(app, "x", 0))
        self.assertEqual(self.level(), "fail")

    def test_quiet_fail_sends_nothing_far_from_expiry(self):
        calls = []

        async def sent(*a, **k):
            calls.append(a)
            return True
        renew.notify = sent
        app = db.row("SELECT * FROM apps WHERE id = ?", self.app_id)
        asyncio.run(renew.alert(app, "TV en veille", 0, quiet_fail=True))
        self.assertEqual(calls, [])
        asyncio.run(renew.alert({**app, "expires_at": 3600}, "TV en veille", 0, quiet_fail=True))
        self.assertEqual(len(calls), 1)          # moins de 6 h : prévenu quand même


class CertificateList(unittest.TestCase):
    def serials(self, output, ok=True):
        class Lock:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        async def fake_run(*a, **k):
            return SimpleNamespace(ok=ok, output=output)
        saved = plume._locked, plume._as, plume.run
        plume._locked, plume._as, plume.run = (lambda: Lock()), (lambda e: Lock()), fake_run
        try:
            return asyncio.run(plume.issued_cert_serials("a@b.c"))
        finally:
            plume._locked, plume._as, plume.run = saved

    ENTRY = 'Certificate {{ name: "x", certificate_id: "i", serial_number: "{}", status: "{}" }}'

    def test_issued_serials(self):
        out = "[INFO] [\n" + self.ENTRY.format("0AB", "Issued") + ",\n" + self.ENTRY.format("0CD", "Revoked") + "\n]"
        self.assertEqual(self.serials(out), {"AB"})

    def test_unparsed_entries_conclude_nothing(self):
        # le mot serial_number est là mais le format a changé : jamais « aucun certificat valide »
        self.assertIsNone(self.serials('[ Certificate { serial_number: 0AB, state: Issued } ]'))

    def test_partly_unparsed_entries_conclude_nothing(self):
        out = self.ENTRY.format("0AB", "Issued") + ' Certificate { serial_number: 12, state: Issued }'
        self.assertIsNone(self.serials(out))

    def test_explicit_empty_list(self):
        self.assertEqual(self.serials("INFO plumesign: []"), set())

    def test_unknown_output(self):
        self.assertIsNone(self.serials("something else entirely"))

    def test_failure(self):
        self.assertIsNone(self.serials("", ok=False))


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def add(self, content, name="a.ipa"):
        return self.client.post("/api/apps", headers=HEADERS, data={"source_type": "upload"},
                                files={"file": (name, content, "application/octet-stream")})

    def test_not_an_ipa(self):
        r = self.add(b"not a zip")
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "not_an_ipa"))

    def test_not_tvos(self):
        r = self.add(make_ipa("iphoneos"))
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "not_tvos:iphoneos"))
        self.assertEqual(db.rows("SELECT id FROM apps WHERE bundle_id = 'com.example.tv'"), [])

    def test_add_duplicate_patch_delete(self):
        r = self.add(make_ipa(bundle="com.example.dup"))
        self.assertEqual(r.status_code, 200, r.text)
        app_id = r.json()["id"]
        self.assertEqual(r.json()["enabled"], 1)
        self.assertEqual(self.add(make_ipa(bundle="com.example.dup")).json()["detail"], "already_added")
        self.assertEqual(self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"enabled": None}).status_code, 200)
        renew._RUNNING.add(app_id)
        try:
            r = self.client.delete(f"/api/apps/{app_id}", headers=HEADERS)
            self.assertEqual((r.status_code, r.json()["detail"]), (409, "app_busy"))
        finally:
            renew._RUNNING.discard(app_id)
        self.assertEqual(self.client.delete(f"/api/apps/{app_id}", headers=HEADERS).status_code, 200)

    def test_unknown_account(self):
        r = self.client.post("/api/accounts/nobody@example.com/select", headers=HEADERS)
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "unknown_account"))
        r = self.client.delete("/api/accounts/nobody@example.com", headers=HEADERS)
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "unknown_account"))

    def test_setup_code_non_ascii(self):
        from app import auth
        auth.SETUP_CODE = "ABCD2345"
        try:
            self.assertFalse(auth.check_setup_code("ABCD–2345"))       # tiret demi-cadratin : pas de TypeError
            self.assertTrue(auth.check_setup_code("abcd-2345"))
        finally:
            auth.SETUP_CODE = None


class EventLog(unittest.TestCase):
    def test_repeated_error_is_not_duplicated(self):
        db.init()
        before = len(db.rows("SELECT id FROM events"))
        for _ in range(3):
            db.log("X : échec, TV introuvable", "error", app_id=999)
        db.log("X : renouvelée", "info", app_id=999)
        db.log("X : renouvelée", "info", app_id=999)
        self.assertEqual(len(db.rows("SELECT id FROM events")) - before, 3)


if __name__ == "__main__":
    unittest.main()
