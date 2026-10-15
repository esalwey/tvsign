"""Suites de la relecture des mises à jour : recul par nature d'échec, bannière de recherche de version,
échange de fichiers transactionnel, copies hors de la boucle, réglages normalisés, erreurs GitHub.

    python -m unittest discover -s tests
"""
import asyncio
import os
import re
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

from test_updates import HEADERS, HOUR, app_row, make_ipa, sha   # fixe TVSIGN_DATA avant l'import de app

from fastapi.testclient import TestClient

from app import db, devices, ipa, main, plume, renew, settings

ROOT = Path(__file__).resolve().parent.parent


# ---------- recul : un compteur par nature d'échec ----------

class FailureKinds(unittest.TestCase):
    def test_same_kind_counts_on_and_another_restarts(self):
        app = app_row(fail_count=3, fail_kind="other")
        self.assertEqual(renew._next_fails(app, "other"), 4)
        self.assertEqual(renew._next_fails(app, "unreachable"), 1)
        self.assertEqual(renew._next_fails(app_row(fail_count=3), "other"), 1)       # base d'avant la colonne
        self.assertEqual(renew._next_fails(app_row(fail_count=3, fail_kind="update:other"), "other"), 1)

    def test_migration_adds_the_column(self):
        self.assertIn(("apps", "fail_kind", "TEXT"), db.MIGRATIONS)


class RenewalReview(unittest.TestCase):
    """_renew avec un faux plumesign (comme Renewal de test_updates)."""

    def setUp(self):
        db.init()
        self.fail_on = set()
        patches = [
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
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])

    async def fake_sign(self, email, original, out):
        data = Path(original).read_bytes()
        if sha(data) in self.fail_on:
            raise RuntimeError("signature : plumesign a échoué")
        Path(out).write_bytes(b"signed:" + data)
        return False

    def add(self, expires_in=120 * HOUR, **cols):
        device_id = db.execute("INSERT INTO devices (name, pair_identifier, udid, os_version, created_at) "
                               "VALUES ('TV', ?, 'U', '26.0', 0)", f"p{time.time_ns()}")
        data = make_ipa(version="1.0")
        app_id = db.execute(
            "INSERT INTO apps (name, source_type, source_tag, source_key, bundle_id, version, source_sha256, installed_ipa, "
            "device_id, account_email, status, expires_at, signed_at, created_at) "
            "VALUES ('R', 'upload', 'v1', 'k1', 'com.example.tv', '1.0', ?, ?, ?, 'a@b.c', 'ok', ?, ?, 0)",
            sha(data), sha(data), device_id, time.time() + expires_in, time.time())
        (renew.app_dir(app_id) / "original.ipa").write_bytes(data)
        if cols:
            db.update("apps", app_id, **cols)
        return app_id, data

    def row(self, app_id):
        return db.row("SELECT * FROM apps WHERE id = ?", app_id)

    def run_renew(self, app_id, trigger):
        asyncio.run(renew._renew(app_id, trigger))

    def test_another_failure_kind_does_not_inherit_the_cap(self):
        """Après plusieurs « TV en veille », une erreur d'une autre nature repart de 45 min, pas de 6 h."""
        app_id, old = self.add(status="error", fail_count=5, fail_kind="unreachable")
        self.fail_on = {sha(old)}
        self.run_renew(app_id, "auto")
        row = self.row(app_id)
        self.assertEqual((row["fail_count"], row["fail_kind"]), (1, "other"))
        self.assertAlmostEqual((row["next_attempt_at"] - time.time()) / 60, 45, delta=1)
        self.run_renew(app_id, "auto")                                   # même nature : le recul continue
        row = self.row(app_id)
        self.assertEqual(row["fail_count"], 2)
        self.assertAlmostEqual((row["next_attempt_at"] - time.time()) / 60, 90, delta=1)

    def test_optional_update_failures_do_not_feed_the_mandatory_backoff(self):
        app_id, old = self.add(fail_count=4, fail_kind="update:other")
        self.fail_on = {sha(old)}
        db.update("apps", app_id, expires_at=time.time() + 5 * HOUR)       # fenêtre de renouvellement : échec obligatoire
        self.run_renew(app_id, "auto")
        row = self.row(app_id)
        self.assertEqual((row["status"], row["fail_count"], row["fail_kind"]), ("error", 1, "other"))

    def test_failed_optional_update_is_tagged_as_such(self):
        app_id, old = self.add()
        d = renew.app_dir(app_id)
        new = make_ipa(version="2.0")
        (d / "original.prev.ipa").write_bytes(old)
        (d / "original.ipa").write_bytes(new)
        db.update("apps", app_id, source_tag="v2", source_key="k2", version="2.0", source_sha256=sha(new),
                  prev_tag="v1", prev_key="k1", prev_version="1.0", prev_sha256=sha(old))
        self.fail_on = {sha(new), sha(old)}
        (d / "original.prev.ipa").unlink()
        self.run_renew(app_id, "nouvelle version")
        row = self.row(app_id)
        self.assertEqual((row["status"], row["fail_count"], row["fail_kind"]), ("ok", 1, "update:other"))

    def test_optional_update_retry_never_postpones_the_renewal(self):
        """Un échec de mise à jour facultative (ici « permanent » : 24 h) ne repousse pas l'ouverture de la
        fenêtre de renouvellement, ni ne mène à renouveler à une minute de l'expiration."""
        window = settings.renew_before_hours() * HOUR
        app_id, old = self.add(expires_in=window + 30 * 60)               # la fenêtre s'ouvre dans 30 min
        d = renew.app_dir(app_id)
        new = make_ipa(version="2.0")
        (d / "original.ipa").write_bytes(new)
        db.update("apps", app_id, source_tag="v2", source_key="k2", version="2.0", source_sha256=sha(new))
        err = RuntimeError("cette app demande tvOS 99 ou plus")              # permanent : 24 h de recul
        with mock.patch.object(renew, "_preflight_os", mock.AsyncMock(side_effect=err)):
            self.run_renew(app_id, "nouvelle version")
        row = self.row(app_id)
        self.assertEqual(row["status"], "ok")
        self.assertLessEqual(row["next_attempt_at"], row["expires_at"] - window + 1)
        self.assertGreater(row["next_attempt_at"], time.time())
        # à l'ouverture de la fenêtre, la boucle renouvelle
        self.assertTrue(renew.due(row, row["expires_at"] - window + 5))


# ---------- bannière « recherche de version impossible » ----------

class ReleaseBanner(unittest.TestCase):
    def setUp(self):
        db.init()
        renew._RELEASE_ERR.clear()
        renew.HEALTH["release_check_error"] = None
        self.addCleanup(lambda: (renew._RELEASE_ERR.clear(), renew.HEALTH.update(release_check_error=None)))

    def fail(self, app):
        err = ipa.SourceUnavailable("limite de requêtes GitHub atteinte", "gh_rate_limit")
        with mock.patch.object(renew, "_latest", side_effect=err):
            asyncio.run(renew.new_release(app))

    def test_banner_clears_when_the_failing_app_stops_being_searched(self):
        gh = app_row(id=901, name="A")
        self.fail(gh)
        self.assertIsNotNone(renew.HEALTH["release_check_error"])
        renew._prune_release_errors([{**gh, "enabled": 0}])                          # mise en pause
        self.assertIsNone(renew.HEALTH["release_check_error"])
        self.fail(gh)
        renew._prune_release_errors([])                                              # supprimée
        self.assertIsNone(renew.HEALTH["release_check_error"])
        self.fail(gh)
        renew._prune_release_errors([{**gh, "source_type": "url"}])                  # plus une source GitHub
        self.assertIsNone(renew.HEALTH["release_check_error"])

    def test_banner_stays_while_another_app_still_fails(self):
        a, b = app_row(id=902, name="A"), app_row(id=903, name="B")
        self.fail(a)
        self.fail(b)
        renew._prune_release_errors([{**a, "enabled": 0}, b])
        self.assertIn("B", renew.HEALTH["release_check_error"])
        renew._release_ok(b)
        self.assertIsNone(renew.HEALTH["release_check_error"])

    def test_link_app_clears_its_entry(self):
        gh = app_row(id=904, name="A")
        self.fail(gh)
        self.assertFalse(asyncio.run(renew.new_release({**gh, "source_type": "url"})))
        self.assertIsNone(renew.HEALTH["release_check_error"])

    def test_delete_pause_and_source_change_clear_the_banner(self):
        with TestClient(main.app) as client, mock.patch.object(renew, "spawn", return_value=True):
            ids = []
            for name in ("A", "B", "C"):
                app_id = db.execute("INSERT INTO apps (name, source_type, source, created_at) VALUES (?, 'github', 'o/r', 0)", name)
                ids.append(app_id)
                self.fail(app_row(id=app_id, name=name))
            self.assertEqual(len(renew._RELEASE_ERR), 3)
            self.assertEqual(client.delete(f"/api/apps/{ids[0]}", headers=HEADERS).status_code, 200)
            self.assertNotIn(ids[0], renew._RELEASE_ERR)
            client.patch(f"/api/apps/{ids[1]}", headers=HEADERS, json={"enabled": False})
            self.assertNotIn(ids[1], renew._RELEASE_ERR)
            client.patch(f"/api/apps/{ids[2]}", headers=HEADERS, json={"source": "o/other"})
            self.assertNotIn(ids[2], renew._RELEASE_ERR)
            self.assertIsNone(renew.HEALTH["release_check_error"])


# ---------- échange de fichiers transactionnel ----------

class SwapFiles(unittest.TestCase):
    def setUp(self):
        db.init()
        self.app_id = db.execute("INSERT INTO apps (name, source_type, created_at) VALUES ('S', 'github', 0)")
        self.d = renew.app_dir(self.app_id)
        (self.d / "original.ipa").write_bytes(b"NEW")
        (self.d / "original.prev.ipa").write_bytes(b"OLD")

    def files(self):
        return {p.name: p.read_bytes() for p in self.d.iterdir()}

    def test_swap_works(self):
        renew._swap_files(self.app_id)
        self.assertEqual(self.files(), {"original.ipa": b"OLD", "original.prev.ipa": b"NEW"})

    def test_failure_at_any_step_leaves_both_files_in_place(self):
        # chaque os.replace est tenté 6 fois (verrou passager) : on échoue de façon persistante à partir de l'étape n
        for step in (1, 2, 3):
            with self.subTest(step=step):
                (self.d / "original.ipa").write_bytes(b"NEW")
                (self.d / "original.prev.ipa").write_bytes(b"OLD")
                (self.d / "original.swap").unlink(missing_ok=True)
                real = os.replace

                def fake(src, dst):
                    name = (Path(src).name, Path(dst).name)
                    # étape 1 : ipa->swap ; étape 2 : prev->ipa ; étape 3 : swap->prev
                    wanted = {1: ("original.ipa", "original.swap"), 2: ("original.prev.ipa", "original.ipa"),
                              3: ("original.swap", "original.prev.ipa")}[step]
                    if name == wanted:
                        raise PermissionError("verrouillé")
                    return real(src, dst)
                with mock.patch.object(os, "replace", fake), mock.patch.object(time, "sleep", lambda s: None):
                    with self.assertRaises(OSError):
                        renew._swap_files(self.app_id)
                self.assertEqual(self.files(), {"original.ipa": b"NEW", "original.prev.ipa": b"OLD"})

    def test_startup_restores_an_orphaned_swap(self):
        (self.d / "original.ipa").rename(self.d / "original.swap")
        renew.recover_swaps()
        self.assertEqual(self.files(), {"original.ipa": b"NEW", "original.prev.ipa": b"OLD"})
        (self.d / "original.swap").write_bytes(b"STALE")       # original.ipa présent : on ne touche à rien
        renew.recover_swaps()
        self.assertEqual((self.d / "original.ipa").read_bytes(), b"NEW")

    def test_rollback_endpoint_reports_locked_files_and_changes_nothing(self):
        db.update("apps", self.app_id, source_tag="v2", version="2.0", prev_tag="v1", prev_version="1.0")
        before = self.files()
        with TestClient(main.app) as client, mock.patch.object(renew, "_swap_files", side_effect=PermissionError("x")), \
                mock.patch.object(renew, "spawn", return_value=True):
            r = client.post(f"/api/apps/{self.app_id}/rollback", headers=HEADERS)
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "files_locked"))
        row = db.row("SELECT * FROM apps WHERE id = ?", self.app_id)
        self.assertEqual((row["source_tag"], row["prev_tag"]), ("v2", "v1"))
        self.assertEqual(self.files(), before)

    def test_fall_back_keeps_the_original_error_when_the_swap_fails(self):
        db.update("apps", self.app_id, source_tag="v2", version="2.0", prev_tag="v1", source_sha256="x", installed_ipa="y")
        first = RuntimeError("signature : boom")

        async def deliver():
            raise AssertionError("ne doit pas être appelé")
        with mock.patch.object(renew, "_swap_files", side_effect=PermissionError("verrou")):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(renew._fall_back(self.app_id, first, deliver, self.d / "signed.ipa"))
        self.assertIs(ctx.exception, first)


# ---------- copies d'IPA hors de la boucle d'événements ----------

class OffTheEventLoop(unittest.TestCase):
    def test_refresh_source_copies_in_a_worker_thread(self):
        db.init()
        old = make_ipa(version="1.0")
        new = make_ipa(version="2.0")
        app_id = db.execute(
            "INSERT INTO apps (name, source_type, source, source_tag, source_key, bundle_id, version, source_sha256, created_at) "
            "VALUES ('T', 'github', 'o/r', 'v1', 'k1', 'com.example.tv', '1.0', ?, 0)", sha(old))
        (renew.app_dir(app_id) / "original.ipa").write_bytes(old)
        threads = []
        real_keep = ipa.keep_copy

        def keep(src, dst, copy=True):
            import threading
            threads.append(threading.current_thread().name)
            return real_keep(src, dst, copy=copy)

        def download(url, dest, sha256=None, expect_bundle=None, **kw):
            Path(str(dest) + ".part").write_bytes(new)
            os.replace(str(dest) + ".part", dest)
            return {"version": "2.0", "bundle_id": "com.example.tv", "sha256": sha(new)}
        found = {"tag": "v2", "url": "https://x/v2", "sha256": None, "key": "k2", "notes": None}
        with mock.patch.object(ipa, "keep_copy", keep), mock.patch.object(ipa, "download", download), \
                mock.patch.object(renew, "_latest", return_value=found):
            changed = asyncio.run(renew.refresh_source(db.row("SELECT * FROM apps WHERE id = ?", app_id)))
        self.assertTrue(changed)
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].startswith("asyncio_"), threads)
        self.assertEqual((renew.app_dir(app_id) / "original.prev.ipa").read_bytes(), old)

    def test_replace_ipa_copies_in_a_worker_thread(self):
        threads = []
        real_keep = ipa.keep_copy

        def keep(src, dst, copy=True):
            import threading
            threads.append(threading.current_thread().name)
            return real_keep(src, dst, copy=copy)
        with TestClient(main.app) as client, mock.patch.object(renew, "spawn", return_value=True), \
                mock.patch.object(ipa, "keep_copy", keep):
            r = client.post("/api/apps", headers=HEADERS, data={"source_type": "upload"},
                            files={"file": ("a.ipa", make_ipa(bundle="com.example.thread"), "application/octet-stream")})
            app_id = r.json()["id"]
            r = client.post(f"/api/apps/{app_id}/ipa", headers=HEADERS,
                            files={"file": ("b.ipa", make_ipa(bundle="com.example.thread", version="2.0"), "application/octet-stream")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].startswith("asyncio_"), threads)

    def test_replace_ipa_reports_locked_files(self):
        with TestClient(main.app) as client, mock.patch.object(renew, "spawn", return_value=True):
            r = client.post("/api/apps", headers=HEADERS, data={"source_type": "upload"},
                            files={"file": ("a.ipa", make_ipa(bundle="com.example.locked"), "application/octet-stream")})
            app_id = r.json()["id"]
            before = (renew.app_dir(app_id) / "original.ipa").read_bytes()
            with mock.patch.object(main, "replace_retry", side_effect=PermissionError("x")):
                r = client.post(f"/api/apps/{app_id}/ipa", headers=HEADERS,
                                files={"file": ("b.ipa", make_ipa(bundle="com.example.locked", version="2.0"), "application/octet-stream")})
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "files_locked"))
        self.assertEqual((renew.app_dir(app_id) / "original.ipa").read_bytes(), before)


# ---------- changement de source ----------

class SourceChange(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        db.init()
        self.spawned = []
        p = mock.patch.object(renew, "spawn", lambda app_id, trigger="manuel": self.spawned.append((app_id, trigger)) or True)
        p.start()
        self.addCleanup(p.stop)
        self.app_id = db.execute(
            "INSERT INTO apps (name, source_type, source, source_tag, pinned_tag, fail_count, next_attempt_at, prev_tag, "
            "prev_version, created_at) VALUES ('P', 'github', 'o/old', 'v3', 'v3', 4, ?, 'v2', '2.0', 0)", time.time() + 5 * HOUR)
        (renew.app_dir(self.app_id) / "original.ipa").write_bytes(b"CUR")
        (renew.app_dir(self.app_id) / "original.prev.ipa").write_bytes(b"PREV")

    def row(self):
        return db.row("SELECT * FROM apps WHERE id = ?", self.app_id)

    def patch(self, **body):
        r = self.client.patch(f"/api/apps/{self.app_id}", headers=HEADERS, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_new_repository_drops_pin_backoff_and_previous_version(self):
        self.patch(source="o/new")
        row = self.row()
        self.assertIsNone(row["pinned_tag"])
        self.assertEqual((row["fail_count"], row["next_attempt_at"]), (0, None))
        self.assertEqual((row["prev_tag"], row["prev_version"]), (None, None))
        self.assertFalse(renew.has_previous(self.app_id))
        self.assertEqual(self.spawned[-1], (self.app_id, "nouvelle source"))

    def test_explicit_pin_in_the_same_request_is_kept(self):
        self.patch(source="o/new", pinned_tag="v9")
        self.assertEqual(self.row()["pinned_tag"], "v9")

    def test_channel_change_keeps_the_pin_and_the_previous_version(self):
        self.patch(channel="beta")
        row = self.row()
        self.assertEqual((row["pinned_tag"], row["prev_tag"]), ("v3", "v2"))
        self.assertTrue(renew.has_previous(self.app_id))
        self.assertEqual((row["fail_count"], row["next_attempt_at"]), (0, None))


# ---------- lien sans marqueur de version ----------

class KeylessLink(unittest.TestCase):
    URL = "https://example.com/latest.ipa"

    def setUp(self):
        db.init()
        self.saved = ipa.url_key, ipa.download
        ipa.url_key = lambda u: None
        self.calls = []

        def download(url, dest, sha256=None, expect_bundle=None, **kw):
            self.calls.append(kw)
            return None if kw.get("unchanged_sha") else {"version": "1", "bundle_id": "com.example.tv", "sha256": "z"}
        ipa.download = download
        self.addCleanup(lambda: (setattr(ipa, "url_key", self.saved[0]), setattr(ipa, "download", self.saved[1])))
        data = make_ipa()
        self.app_id = db.execute(
            "INSERT INTO apps (name, source_type, source, source_tag, bundle_id, version, source_sha256, auto_update, created_at) "
            "VALUES ('L', 'url', ?, ?, 'com.example.tv', '1', ?, 0, 0)", self.URL, self.URL, sha(data))
        (renew.app_dir(self.app_id) / "original.ipa").write_bytes(data)
        renew._LINK_CHECKED.pop(self.app_id, None)

    def refresh(self, force):
        return asyncio.run(renew.refresh_source(db.row("SELECT * FROM apps WHERE id = ?", self.app_id), force=force))

    def test_check_now_compares_the_file_even_with_auto_update_off(self):
        self.assertFalse(self.refresh(force=True))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0].get("unchanged_sha"))

    def test_without_a_request_nothing_is_compared_when_auto_update_is_off(self):
        self.assertFalse(self.refresh(force=False))
        self.assertEqual(self.calls, [])

    def test_auto_update_on_still_compares_periodically(self):
        db.update("apps", self.app_id, auto_update=1)
        self.refresh(force=False)
        self.assertEqual(len(self.calls), 1)
        self.refresh(force=False)                  # moins de 6 h après : pas de nouveau téléchargement
        self.assertEqual(len(self.calls), 1)


# ---------- réglages et erreurs GitHub ----------

class UpdateCheckSetting(unittest.TestCase):
    def setUp(self):
        db.init()
        settings.raw_set("update_check", None)
        self.addCleanup(lambda: settings.raw_set("update_check", None))

    def test_every_spelling_is_normalised_to_on_or_off(self):
        for raw, want in (("false", "off"), ("0", "off"), ("No", "off"), ("OFF", "off"),
                          ("true", "on"), ("1", "on"), ("yes", "on"), ("on", "on")):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"TVSIGN_UPDATE_CHECK": raw}):
                self.assertEqual(settings.get("update_check"), want)
                self.assertEqual(settings.describe()["update_check"]["value"], want)
                self.assertEqual(settings.update_check(), want == "on")

    def test_stored_value_is_normalised_too(self):
        settings.update({"update_check": "false"})
        self.assertEqual(settings.raw_get("update_check"), "off")
        self.assertEqual(settings.get("update_check"), "off")
        settings.raw_set("update_check", "0")                           # ancienne valeur brute
        self.assertEqual(settings.describe()["update_check"]["value"], "off")

    def test_invalid_value_is_still_refused(self):
        with self.assertRaises(ValueError):
            settings.update({"update_check": "maybe"})


class GithubTokenError(unittest.TestCase):
    def error(self, code):
        return urllib.error.HTTPError("https://api.github.com/x", code, "x", Message(), None)

    def test_rejected_token_has_its_own_code(self):
        with mock.patch.object(ipa, "GITHUB_TOKEN", "ghp_expired"):
            e = ipa._github_error(self.error(401))
            self.assertEqual(e.code, "gh_bad_token")
            self.assertIn("TVSIGN_GITHUB_TOKEN", str(e))
            self.assertEqual(ipa._github_error(self.error(404)).code, "gh_not_found")

    def test_without_a_token_401_is_still_not_found(self):
        with mock.patch.object(ipa, "GITHUB_TOKEN", ""):
            self.assertEqual(ipa._github_error(self.error(401)).code, "gh_not_found")

    def test_add_app_reports_the_code(self):
        err = ipa.SourceUnavailable("jeton GitHub refusé", "gh_bad_token")
        with TestClient(main.app) as client, mock.patch.object(ipa, "latest_github", side_effect=err):
            r = client.post("/api/apps", headers=HEADERS, data={"source_type": "github", "source": "o/token"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "gh_bad_token"))


# ---------- interface et documentation (contrôles statiques) ----------

class FrontAndDocs(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_replace_input_is_permanent_and_outside_the_rendered_sections(self):
        html, js = self.read("web/index.html"), self.read("web/app.js")
        self.assertIn('id="replace-input"', html)
        self.assertNotIn('<input type="file" data-replace', js)             # plus d'input dans les cartes redessinées
        self.assertIn('$("#replace-input").addEventListener("change"', js)
        self.assertNotIn('closest?.("input[data-replace]")', js)
        for sec in ("apps", "alertes"):                                      # l'input n'est pas dans une section de rendu
            i = html.index(f'id="{sec}"')
            self.assertNotIn("replace-input", html[i:i + 200])

    def test_new_strings_exist_in_both_languages(self):
        i18n = self.read("web/i18n.js")
        for key in ("untracked_link_manual", "err_gh_bad_token", "c_gh_token_t", "c_gh_token_a", "err_files_locked",
                    "self_update_docker"):
            m = re.search(rf"^\s*{key}: \[(.+)\],$", i18n, re.M)
            self.assertTrue(m, key)
            self.assertGreaterEqual(len(re.findall(r'"(?:[^"\\]|\\.)*"', m.group(1))), 2, key)
        self.assertIn("{major}", i18n)

    def test_docker_banner_mentions_the_tag(self):
        m = re.search(r"^\s*self_update_docker: \[(.+)\],$", self.read("web/i18n.js"), re.M)
        self.assertIn("ghcr.io/esalwey/tvsign:{major}", m.group(1))

    def test_windows_readme_has_no_stray_tab(self):
        text = self.read("windows/README-windows.md")
        self.assertNotIn("\t", text)
        self.assertIn("%LOCALAPPDATA%\\tvsign", text)
