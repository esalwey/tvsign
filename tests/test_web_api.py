"""Côté serveur de l'interface web : fiches à moitié ajoutées, présence sur l'Apple TV, version refusée,
remplacement d'IPA, codes d'erreur stables, dépôt GitHub collé en URL, migrations.

    python -m unittest discover -s tests
"""
import asyncio
import os
import sqlite3
import time
import unittest
from unittest import mock

from test_logic import HEADERS, make_ipa  # noqa: F401 - prépare l'environnement (dossier de données temporaire)

from fastapi.testclient import TestClient  # noqa: E402

from app import db, diag, main, proc, renew, settings  # noqa: E402
from app.util import normalize_repo  # noqa: E402


class Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def add_upload(self, content, name="a.ipa"):
        return self.client.post("/api/apps", headers=HEADERS, data={"source_type": "upload"},
                                files={"file": (name, content, "application/octet-stream")})

    def listed(self):
        return {a["id"]: a for a in self.client.get("/api/state").json()["apps"]}


class HalfAddedApps(Client):
    def test_row_is_hidden_while_the_ipa_is_downloaded_and_removed_on_failure(self):
        seen = {}

        async def fake_refresh(app):
            seen["listed"] = app["id"] in {a["id"] for a in main.state()["apps"]}
            seen["row"] = db.row("SELECT enabled, status FROM apps WHERE id = ?", app["id"])
            raise RuntimeError("aucune IPA trouvée dans les releases")

        with mock.patch.object(renew, "refresh_source", fake_refresh):
            r = self.client.post("/api/apps", headers=HEADERS, data={"source_type": "github", "source": "owner/project"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(seen["listed"])
        self.assertEqual((seen["row"]["enabled"], seen["row"]["status"]), (0, "adding"))
        self.assertEqual(db.rows("SELECT id FROM apps WHERE source = 'owner/project'"), [])

    def test_row_appears_complete_after_the_add(self):
        async def fake_refresh(app):
            dest = renew.app_dir(app["id"]) / "original.ipa"
            dest.write_bytes(make_ipa(bundle="com.example.hidden"))
            return True

        with mock.patch.object(renew, "refresh_source", fake_refresh), mock.patch.object(renew, "spawn", lambda *a: True):
            r = self.client.post("/api/apps", headers=HEADERS, data={"source_type": "github", "source": "owner/shown"})
        self.assertEqual(r.status_code, 200, r.text)
        a = self.listed()[r.json()["id"]]
        self.assertEqual((a["enabled"], a["status"], a["bundle_id"]), (1, "pending", "com.example.hidden"))
        self.client.delete(f"/api/apps/{a['id']}", headers=HEADERS)

    def test_rows_left_by_an_interrupted_add_are_purged_at_startup(self):
        app_id = db.execute("INSERT INTO apps (name, source_type, created_at, enabled, status) "
                            "VALUES ('x', 'github', ?, 0, 'adding')", time.time())
        self.assertNotIn(app_id, self.listed())
        main._purge_half_added()
        self.assertIsNone(db.row("SELECT id FROM apps WHERE id = ?", app_id))

    def test_paused_app_without_bundle_is_not_hidden(self):
        # une app mise en pause reste visible, même si son identifiant d'app a été effacé (version acceptée)
        app_id = db.execute("INSERT INTO apps (name, source_type, created_at, enabled, status) "
                            "VALUES ('pausée', 'github', ?, 0, 'ok')", time.time())
        try:
            self.assertIn(app_id, self.listed())
        finally:
            db.execute("DELETE FROM apps WHERE id = ?", app_id)


class GithubSource(Client):
    def test_normalize_repo(self):
        for pasted, want in [
            ("owner/project", "owner/project"),
            ("https://github.com/owner/project", "owner/project"),
            ("https://www.github.com/owner/project/", "owner/project"),
            ("https://github.com/owner/project.git", "owner/project"),
            ("https://github.com/owner/project/releases/tag/v1", "owner/project"),
            ("http://github.com/owner/my.repo?tab=x", "owner/my.repo"),
            ("git@github.com:owner/project.git", "owner/project"),
            ("  owner/project.git  ", "owner/project"),
            ("owner/project/", "owner/project"),
        ]:
            self.assertEqual(normalize_repo(pasted), want, pasted)
        self.assertEqual(normalize_repo(None), "")

    def test_pasted_url_is_accepted_and_stored_as_owner_project(self):
        self.assertEqual(asyncio.run(main._check_source("github", "https://github.com/o/p.git")), "o/p")
        with self.assertRaises(main.HTTPException):
            asyncio.run(main._check_source("github", "pas un dépôt"))


class TvPresence(Client):
    def setUp(self):
        self.dev = db.execute("INSERT INTO devices (name, pair_identifier, created_at) VALUES ('Salon', ?, ?)",
                              f"pid-{time.time()}", time.time())
        self.app_id = db.execute(
            "INSERT INTO apps (name, bundle_id, source_type, device_id, created_at, enabled, status, signed_at, expires_at) "
            "VALUES ('Gate', 'com.example.gate', 'upload', ?, ?, 1, 'ok', ?, ?)",
            self.dev, time.time(), time.time() - 3600, time.time() + 86400)

    def tearDown(self):
        db.execute("DELETE FROM apps WHERE id = ?", self.app_id)
        db.execute("DELETE FROM devices WHERE id = ?", self.dev)

    def verify(self, installed):
        async def fake(device, bundle_id=None):
            return installed

        with mock.patch.object(main.devices, "installed_apps", fake):
            return self.client.post(f"/api/devices/{self.dev}/verify", headers=HEADERS)

    def test_installed_under_the_team_suffixed_identifier(self):
        r = self.verify([{"bundle_id": "com.example.gate.ABCDE12345"}, {"bundle_id": "com.other"}])
        self.assertEqual(r.json()["apps"], [{"app_id": self.app_id, "present": True}])
        a = self.listed()[self.app_id]
        self.assertIs(a["tv_present"], True)
        self.assertTrue(a["tv_checked_at"])

    def test_absent_from_the_tv(self):
        r = self.verify([{"bundle_id": "com.example.gateway"}, {"bundle_id": "com.example.gate.extra"}, {"name": "sans identifiant"}])
        self.assertEqual(r.json()["apps"], [{"app_id": self.app_id, "present": False}])
        self.assertIs(self.listed()[self.app_id]["tv_present"], False)

    def test_check_is_stale_once_the_app_was_installed_again(self):
        self.verify([])
        self.assertIs(self.listed()[self.app_id]["tv_present"], False)
        db.update("apps", self.app_id, signed_at=time.time() + 5)     # réinstallée depuis : le résultat ne vaut plus
        a = self.listed()[self.app_id]
        self.assertIsNone(a["tv_present"])
        self.assertIsNone(a["tv_checked_at"])

    def test_running_app_is_not_judged(self):
        renew._RUNNING.add(self.app_id)
        try:
            self.assertEqual(self.verify([]).json()["apps"], [])
        finally:
            renew._RUNNING.discard(self.app_id)

    def test_tv_unreachable_is_a_502_and_changes_nothing(self):
        async def boom(device, bundle_id=None):
            raise RuntimeError("TV injoignable")

        with mock.patch.object(main.devices, "installed_apps", boom):
            r = self.client.post(f"/api/devices/{self.dev}/verify", headers=HEADERS)
        self.assertEqual(r.status_code, 502)
        self.assertIsNone(self.listed()[self.app_id]["tv_present"])

    def test_changing_the_destination_forgets_the_check(self):
        self.verify([])
        other = db.execute("INSERT INTO devices (name, pair_identifier, created_at) VALUES ('Chambre', ?, ?)",
                           f"pid2-{time.time()}", time.time())
        try:
            with mock.patch.object(renew, "rerun", lambda *a: None):
                self.client.patch(f"/api/apps/{self.app_id}", headers=HEADERS, json={"device_id": other})
            self.assertIsNone(self.listed()[self.app_id]["tv_present"])
        finally:
            db.update("apps", self.app_id, device_id=self.dev)
            db.execute("DELETE FROM devices WHERE id = ?", other)

    def test_unknown_device(self):
        r = self.client.post("/api/devices/999999/verify", headers=HEADERS)
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "unknown_device"))


class RefusedVersion(Client):
    def setUp(self):
        self.app_id = db.execute(
            "INSERT INTO apps (name, bundle_id, source_type, source, source_tag, created_at, enabled, status, bad_tag, bad_key, bad_reason) "
            "VALUES ('Gate', 'com.example.gate', 'github', 'o/p', 'v1', ?, 1, 'ok', 'v2', 'k2', 'identifiant d''app changé (a vers b)')",
            time.time())
        self.spawned = []
        self.patch = mock.patch.object(renew, "spawn", lambda app_id, trigger="manuel": self.spawned.append((app_id, trigger)) or True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        db.execute("DELETE FROM apps WHERE id = ?", self.app_id)

    def test_reason_is_exposed_to_the_ui(self):
        a = self.listed()[self.app_id]
        self.assertEqual((a["bad_tag"], a["bad_reason"]), ("v2", "identifiant d'app changé (a vers b)"))

    def test_retry_clears_the_refusal_and_looks_for_the_version_again(self):
        r = self.client.post(f"/api/apps/{self.app_id}/retry-version", headers=HEADERS)
        self.assertEqual(r.json(), {"ok": True, "started": True})
        a = db.row("SELECT * FROM apps WHERE id = ?", self.app_id)
        self.assertEqual((a["bad_tag"], a["bad_key"], a["bad_reason"], a["bundle_id"]), (None, None, None, "com.example.gate"))
        self.assertEqual(self.spawned, [(self.app_id, "nouvelle version")])

    def test_retry_of_an_app_in_error_is_a_normal_renewal(self):
        db.update("apps", self.app_id, status="error")
        self.client.post(f"/api/apps/{self.app_id}/retry-version", headers=HEADERS)
        self.assertEqual(self.spawned, [(self.app_id, "manuel")])

    def test_accepting_a_new_identifier_lifts_the_guard(self):
        self.client.post(f"/api/apps/{self.app_id}/accept-version", headers=HEADERS)
        a = db.row("SELECT * FROM apps WHERE id = ?", self.app_id)
        self.assertEqual((a["bundle_id"], a["bad_tag"], a["bad_reason"]), (None, None, None))
        self.assertEqual(self.spawned, [(self.app_id, "nouvelle version")])
        self.assertIn(self.app_id, self.listed())             # toujours visible

    def test_changing_the_source_forgets_the_refusal(self):
        self.client.patch(f"/api/apps/{self.app_id}", headers=HEADERS, json={"source": "https://github.com/o/fork"})
        a = db.row("SELECT * FROM apps WHERE id = ?", self.app_id)
        self.assertEqual((a["source"], a["bad_tag"], a["bad_reason"]), ("o/fork", None, None))

    def test_unknown_app(self):
        self.assertEqual(self.client.post("/api/apps/999999/retry-version", headers=HEADERS).status_code, 404)
        self.assertEqual(self.client.post("/api/apps/999999/accept-version", headers=HEADERS).status_code, 404)

    def test_refresh_source_stores_the_reason(self):
        async def run():
            app = db.row("SELECT * FROM apps WHERE id = ?", self.app_id)
            db.update("apps", self.app_id, bad_tag=None, bad_key=None, bad_reason=None, source_key="k1")
            (renew.app_dir(self.app_id) / "original.ipa").write_bytes(make_ipa())
            app = db.row("SELECT * FROM apps WHERE id = ?", self.app_id)
            found = {"tag": "v2", "url": "https://example.org/a.ipa", "sha256": None, "key": "k2"}

            def refuse(*a, **k):
                raise renew.ipa.SourceRejected("identifiant d'app changé (a vers b)")

            with mock.patch.object(renew, "_latest", lambda app: found), mock.patch.object(renew.ipa, "download", refuse), \
                    mock.patch.object(renew, "notify", mock.AsyncMock()):
                return await renew.refresh_source(app)

        self.assertFalse(asyncio.run(run()))
        a = db.row("SELECT bad_tag, bad_reason FROM apps WHERE id = ?", self.app_id)
        self.assertEqual((a["bad_tag"], a["bad_reason"]), ("v2", "identifiant d'app changé (a vers b)"))


class RenewAll(Client):
    def test_renews_one_after_the_other_the_active_apps_that_have_a_tv(self):
        dev = db.execute("INSERT INTO devices (name, pair_identifier, created_at) VALUES ('TV', ?, ?)", f"p-{time.time()}", time.time())
        ids = [db.execute("INSERT INTO apps (name, source_type, device_id, created_at, enabled, status) VALUES (?, 'upload', ?, ?, ?, 'ok')",
                          n, d, time.time(), e) for n, d, e in (("a", dev, 1), ("b", dev, 1), ("pausée", dev, 0), ("sans tv", None, 1))]
        order, spawned = [], []

        async def fake_renew(app_id, trigger="manuel"):
            order.append((app_id, trigger))

        def fake_spawn(coro):
            spawned.append(coro)
            return coro

        try:
            with mock.patch.object(renew, "renew", fake_renew), mock.patch.object(proc, "spawn", fake_spawn),                     mock.patch.object(renew, "STOPPING", False):
                r = self.client.post("/api/apps/renew-all", headers=HEADERS)
                self.assertEqual(r.json(), {"ok": True, "started": 2})
                asyncio.run(spawned[0])
        finally:
            for i in ids:
                db.execute("DELETE FROM apps WHERE id = ?", i)
            db.execute("DELETE FROM devices WHERE id = ?", dev)
        self.assertEqual(order, [(ids[0], "manuel"), (ids[1], "manuel")])

    def test_nothing_to_renew(self):
        with mock.patch.object(proc, "spawn", lambda coro: coro.close()) as sp:
            self.assertEqual(self.client.post("/api/apps/renew-all", headers=HEADERS).json()["started"], len(
                [a for a in db.rows("SELECT id FROM apps WHERE enabled = 1 AND device_id IS NOT NULL")]))
            del sp


class ReplaceIpa(Client):
    def test_replace_keeps_the_app_and_installs_the_new_build(self):
        r = self.add_upload(make_ipa(bundle="com.example.rep"))
        app_id = r.json()["id"]
        folder = renew.app_dir(app_id)
        (folder / "signed.ipa").write_bytes(b"old signature")
        spawned = []
        try:
            with mock.patch.object(renew, "spawn", lambda i, t="manuel": spawned.append((i, t)) or True):
                new = self.client.post(f"/api/apps/{app_id}/ipa", headers=HEADERS,
                                       files={"file": ("b.ipa", make_ipa(bundle="com.example.rep"), "application/octet-stream")})
            self.assertEqual(new.status_code, 200, new.text)
            self.assertFalse((folder / "signed.ipa").exists())
            self.assertEqual(spawned, [(app_id, "manuel")])
            self.assertEqual(db.row("SELECT id FROM apps WHERE id = ?", app_id)["id"], app_id)
        finally:
            self.client.delete(f"/api/apps/{app_id}", headers=HEADERS)

    def test_other_identifier_other_platform_other_kind(self):
        app_id = self.add_upload(make_ipa(bundle="com.example.rep2")).json()["id"]
        try:
            post = lambda content: self.client.post(f"/api/apps/{app_id}/ipa", headers=HEADERS,   # noqa: E731
                                                    files={"file": ("b.ipa", content, "application/octet-stream")})
            self.assertEqual((post(make_ipa(bundle="com.example.autre")).status_code, post(make_ipa(bundle="com.example.autre")).json()["detail"]),
                             (409, "bundle_changed"))
            self.assertEqual(post(b"pas une ipa").json()["detail"], "not_an_ipa")
            self.assertEqual(post(make_ipa("iphoneos", "com.example.rep2")).json()["detail"], "not_tvos:iphoneos")
            db.update("apps", app_id, source_type="github")
            self.assertEqual(post(make_ipa(bundle="com.example.rep2")).json()["detail"], "not_uploaded_app")
        finally:
            self.client.delete(f"/api/apps/{app_id}", headers=HEADERS)

    def test_upload_limit_applies_to_the_replacement_route(self):
        self.assertTrue(main.UPLOAD_PATTERN.match("/api/apps/12/ipa"))
        self.assertFalse(main.UPLOAD_PATTERN.match("/api/apps/12/renew"))


class StableErrorCodes(Client):
    def test_settings_errors_are_codes_per_field(self):
        r = self.client.patch("/api/settings", headers=HEADERS, json={"renew_before_hours": 5, "ntfy_url": "ftp://x", "language": "de", "nope": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], {"renew_before_hours": "hours_range", "ntfy_url": "invalid_url",
                                              "language": "language_value", "nope": "unknown_setting"})
        r = self.client.patch("/api/settings", headers=HEADERS, json={"renew_before_hours": "abc"})
        self.assertEqual(r.json()["detail"], {"renew_before_hours": "number_expected"})

    def test_setting_set_by_the_environment(self):
        with mock.patch.dict(os.environ, {"TVSIGN_NTFY_URL": "https://ntfy.example.org/t"}):
            r = self.client.patch("/api/settings", headers=HEADERS, json={"ntfy_url": "https://ntfy.sh/x"})
        self.assertEqual(r.json()["detail"], {"ntfy_url": "locked_by_env:TVSIGN_NTFY_URL"})

    def test_notification_test(self):
        post = lambda body: self.client.post("/api/settings/test-notification", headers=HEADERS, json=body)   # noqa: E731
        self.assertEqual(post({"url": "ftp://x"}).json()["detail"], "invalid_url")
        with mock.patch.object(settings, "ntfy_url", lambda: ""):
            self.assertEqual(post({}).json()["detail"], "no_ntfy_url")

    def test_session_and_input(self):
        self.assertEqual(self.client.get("/api/sessions/inconnue").json()["detail"], "session_expired")
        self.assertEqual(self.client.post("/api/sessions/inconnue/answer", headers=HEADERS, json={"value": "1"}).json()["detail"], "session_expired")

    def test_no_input_expected(self):
        s = proc.Session.__new__(proc.Session)
        s.id, s.state = "t1", "running"
        proc.SESSIONS["t1"] = s
        try:
            r = self.client.post("/api/sessions/t1/answer", headers=HEADERS, json={"value": "1"})
            self.assertEqual((r.status_code, r.json()["detail"]), (409, "no_input_expected"))
        finally:
            proc.SESSIONS.pop("t1", None)

    def test_pairing_address_and_restore_busy(self):
        r = self.client.post("/api/devices/pair", headers=HEADERS, json={"ip": "pas une ip!", "port": 1234, "name": "x"})
        self.assertEqual(r.json()["detail"], "invalid_address")
        renew._RUNNING.add(424242)
        try:
            r = self.client.post("/api/backup/import", headers=HEADERS, files={"file": ("b.zip", b"x", "application/zip")})
            self.assertEqual((r.status_code, r.json()["detail"]), (409, "restore_busy"))
        finally:
            renew._RUNNING.discard(424242)

    def test_no_french_literal_left_in_error_details(self):
        import re
        src = (main.ROOT / "app" / "main.py").read_text(encoding="utf-8")
        for m in re.finditer(r'HTTPException\(\d+,\s*(f?"[^"]*")', src):
            self.assertRegex(m.group(1), r'^f?"[a-z_]+(:\{[^}]*\})?"$', m.group(1))


class EditionAndDiagnostics(Client):
    def test_edition_in_auth_status(self):
        self.assertEqual(self.client.get("/api/auth/status").json()["edition"], "windows" if os.name == "nt" else "docker")

    def test_diagnostic_codes(self):
        self.assertEqual(diag._disk()["code"], "free")
        self.assertIn("code", diag._check("x", "ok"))
        self.assertEqual(asyncio.run(diag._plumesign())["code"], "missing")      # plumesign absent des tests


class Migrations(unittest.TestCase):
    def test_new_columns_are_added_to_an_existing_database(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        # base de la 1.0.0 : aucune des colonnes ajoutées après coup
        conn.executescript(db.SCHEMA)
        before = {(t, c) for t, c, _ in db._pending(conn)}
        for col in ("bad_reason", "on_tv", "on_tv_checked_at"):
            self.assertIn(("apps", col), before)
        for table, col, kind in db._pending(conn):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
        self.assertEqual(db._pending(conn), [])
        conn.execute("INSERT INTO apps (name, source_type, created_at) VALUES ('ancienne', 'upload', 1)")
        row = conn.execute("SELECT status, bad_reason, on_tv FROM apps").fetchone()
        self.assertEqual((row["status"], row["bad_reason"], row["on_tv"]), ("pending", None, None))     # l'existant reste valide

    def test_existing_rows_stay_visible(self):
        db.init()
        app_id = db.execute("INSERT INTO apps (name, source_type, created_at, enabled, status) VALUES ('ancienne', 'upload', ?, 1, 'pending')", time.time())
        try:
            self.assertIn(app_id, {a["id"] for a in main.state()["apps"]})
        finally:
            db.execute("DELETE FROM apps WHERE id = ?", app_id)


if __name__ == "__main__":
    unittest.main()
