"""Corrections issues de la relecture : retour arrière limité aux échecs d'IPA, masquage Windows du
dossier de support, mot de passe ntfy conservé, destination ntfy intégrée, causes d'échec d'envoi,
restauration d'une sauvegarde.

    python -m unittest discover -s tests
"""
import asyncio
import os
import socket
import ssl
import unittest
import urllib.error
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import test_notify_ops as base  # fixe TVSIGN_DATA avant d'importer app
from app import backup, db, devices, notifier, plume, proc, renew, settings, support
from app.util import ContentError, is_content_error


class ContentClassification(unittest.TestCase):
    def test_account_and_device_limits_are_not_content_errors(self):
        for text in ("App ID limit reached", "You have reached the maximum number of certificates",
                     "device limit exceeded", "no space left on device", "storage full",
                     "invalid entitlement, App ID limit reached"):
            self.assertFalse(is_content_error(text), text)

    def test_ipa_problems_are_content_errors(self):
        for text in ("Invalid Info.plist in bundle", "entitlements mismatch", "profile could not be verified",
                     "installd: package rejected", "MissingBundleExecutable"):
            self.assertTrue(is_content_error(text), text)

    def test_signing_failure_class(self):
        limit = plume._failure("a@b.c", "signature", proc.Result(1, "ERROR App ID limit reached"), content=True)
        bad = plume._failure("a@b.c", "signature", proc.Result(1, "ERROR invalid Info.plist"), content=True)
        self.assertNotIsInstance(limit, ContentError)
        self.assertIsInstance(bad, ContentError)
        # sans content (enregistrement de l'appareil), jamais une erreur d'IPA
        self.assertNotIsInstance(plume._failure("a@b.c", "x", proc.Result(1, "invalid Info.plist")), ContentError)

    def test_install_failure_class(self):
        saved = devices._wireless
        out = {}

        async def fake(device, command, *a, **k):
            return SimpleNamespace(lines=lambda: [], output=out["text"], err="", tail=lambda *n: out["text"])
        devices._wireless = fake
        try:
            for text, kind in (("installd: package rejected as invalid", ContentError),
                               ("no space left on device", RuntimeError)):
                out["text"] = text
                with self.assertRaises(RuntimeError) as cm:
                    asyncio.run(devices.install({"id": 987, "name": "TV"}, [Path("x.ipa")]))
                self.assertIs(type(cm.exception), kind, text)
        finally:
            devices._wireless = saved


class AutomaticRollback(base.RenewCase):
    def test_account_limits_never_roll_back(self):
        app_id = self.add_app()
        self.new_release()
        self.sign_error = RuntimeError("signature : App ID limit reached")
        for trigger in ("nouvelle version", "auto", "auto"):
            self.run_renew(app_id, trigger)
        row = self.app(app_id)
        self.assertEqual((row["source_tag"], row["bad_tag"]), ("v2", None))
        self.assertTrue(renew.pending_update(row))

    def test_install_content_errors_roll_back(self):
        app_id = self.add_app()
        self.new_release()
        calls = []

        async def install(device, ipas, on_event=None, wait_cap=None, app_name=None):
            calls.append(ipas[0].read_bytes())
            if b"new" in calls[-1]:
                raise ContentError("installation : package rejected")
        devices.install = install
        self.run_renew(app_id, "nouvelle version")
        row = self.app(app_id)
        self.assertEqual((row["status"], row["source_tag"], row["bad_tag"]), ("ok", "v1", "v2"))

    def test_discarded_version_can_be_retried_with_install_now(self):
        app_id = self.add_app()
        self.new_release()

        async def sign(email, original, signed, on_lock=None):
            data = original.read_bytes()
            if b"new" in data and self.sign_error:
                raise self.sign_error
            signed.write_bytes(data)
            return False
        plume.sign = sign
        self.sign_error = ContentError("signature : invalid Info.plist")
        self.run_renew(app_id, "nouvelle version")
        row = self.app(app_id)
        self.assertEqual((row["source_tag"], row["bad_tag"]), ("v1", "v2"))
        # la boucle ne la retélécharge pas…
        self.sign_error = None
        self.assertFalse(asyncio.run(renew.refresh_source(self.app(app_id))))
        # … mais « Installer maintenant » le fait
        self.run_renew(app_id, "mise à jour")
        row = self.app(app_id)
        self.assertEqual((row["status"], row["source_tag"], row["bad_tag"], row["update_tag"]), ("ok", "v2", None, None))


class ManualUpdateFeedback(base.RenewCase):
    def test_manual_update_failure_is_visible_on_the_card(self):
        app_id = self.add_app()
        self.new_release()
        self.install_error = devices.Unreachable("TV en veille")
        self.run_renew(app_id, "mise à jour")
        row = self.app(app_id)
        self.assertEqual(row["status"], "ok")                       # l'app installée reste valable
        self.assertIn("TV en veille", row["update_error"])         # mais l'échec est affiché

    def test_automatic_update_blip_stays_silent(self):
        app_id = self.add_app()
        self.new_release()
        self.install_error = devices.Unreachable("TV en veille")
        self.run_renew(app_id, "nouvelle version")
        self.assertEqual((self.app(app_id)["status"], self.app(app_id)["last_error"]), ("ok", None))
        self.assertEqual(self.sent, [])


class UpdateEndpoint(unittest.TestCase):
    def test_busy_renewal_is_reported(self):
        from fastapi.testclient import TestClient
        app_id = db.execute("INSERT INTO apps (name, source_type, source, status, enabled, created_at) "
                            "VALUES ('B', 'github', 'o/r', 'ok', 1, 0)")
        saved = renew.spawn
        renew.spawn = lambda *a, **k: False
        try:
            with TestClient(base.main.app) as client:
                r = client.post(f"/api/apps/{app_id}/update", headers=base.HEADERS)
        finally:
            renew.spawn = saved
        self.assertEqual((r.status_code, r.json()["started"]), (200, False))


class WindowsRedaction(unittest.TestCase):
    def setUp(self):
        self.saved = support.Path
        support.Path = SimpleNamespace(home=lambda: PureWindowsPath("C:\\Users\\bob"))

    def tearDown(self):
        support.Path = self.saved

    def test_home_is_masked_in_every_spelling(self):
        import json
        raw = "C:\\Users\\bob\\AppData\\Local\\tvsign"
        dumped = json.dumps({"data_dir": raw, "detail": "free space on C:/Users/bob/AppData"})
        for text in (raw, dumped, "C:/Users/bob/AppData"):
            out = support.redact(text)
            self.assertNotIn("bob", out, text)
            self.assertIn("~", out)

    def test_short_home_is_ignored(self):
        support.Path = SimpleNamespace(home=lambda: PureWindowsPath("C:\\"))
        self.assertEqual(support.redact("C:\\Windows"), "C:\\Windows")


class NtfyCase(unittest.TestCase):
    def setUp(self):
        db.init()
        settings.raw_set(notifier.TARGETS_KEY, None)
        settings.update({"ntfy_url": None, "ntfy_events": None})
        os.environ.pop("TVSIGN_NTFY_URL", None)
        os.environ.pop("TVSIGN_NTFY_EVENTS", None)

    def tearDown(self):
        os.environ.pop("TVSIGN_NTFY_URL", None)
        os.environ.pop("TVSIGN_NTFY_EVENTS", None)
        settings.raw_set(notifier.TARGETS_KEY, None)
        settings.update({"ntfy_url": None, "ntfy_events": None})


class NtfyPassword(NtfyCase):
    def test_changing_the_topic_keeps_the_password(self):
        settings.update({"ntfy_url": "https://user:hunter2@ntfy.example/t"})
        shown = settings.describe()["ntfy_url"]["value"]
        settings.update({"ntfy_url": shown.replace("/t", "/other")})
        self.assertEqual(settings.ntfy_url(), "https://user:hunter2@ntfy.example/other")

    def test_clean_restores_the_password_for_an_edited_destination(self):
        settings.update({"ntfy_url": "https://user:hunter2@ntfy.example/t"})
        old = notifier.legacy_target()
        new = notifier.clean({"type": "ntfy", "url": "https://user:***@other.example/t2"}, old)
        self.assertEqual(new["url"], "https://user:hunter2@other.example/t2")
        # une saisie avec un vrai mot de passe reste telle quelle
        new = notifier.clean({"type": "ntfy", "url": "https://user:fresh@other.example/t2"}, old)
        self.assertEqual(new["url"], "https://user:fresh@other.example/t2")

    def test_builtin_destination_edit_through_save_target(self):
        settings.update({"ntfy_url": "https://user:hunter2@ntfy.example/t"})
        masked = notifier.public(notifier.legacy_target())["url"]
        notifier.save_target({"type": "ntfy", "url": masked.replace("/t", "/t3"), "events": ["fail"]}, "ntfy")
        self.assertEqual(settings.ntfy_url(), "https://user:hunter2@ntfy.example/t3")


class BuiltinDestination(NtfyCase):
    def test_name_and_token_are_refused_not_dropped(self):
        settings.update({"ntfy_url": "https://ntfy.example/t"})
        for extra in ({"token": "tk_abcdef"}, {"name": "Maison"}):
            with self.assertRaises(ValueError) as cm:
                notifier.save_target({"type": "ntfy", "url": "https://ntfy.example/t", **extra}, "ntfy")
            self.assertEqual(str(cm.exception), "builtin_no_token")
            with self.assertRaises(ValueError):
                asyncio.run(notifier.test({"id": "ntfy", "type": "ntfy", "url": "https://ntfy.example/t", **extra}))
        notifier.save_target({"type": "ntfy", "url": "https://ntfy.example/t", "token": "", "name": ""}, "ntfy")

    def test_env_preset_url_does_not_block_the_events(self):
        os.environ["TVSIGN_NTFY_URL"] = "https://ntfy.example/env"
        out = notifier.save_target({"type": "ntfy", "url": "https://ntfy.example/env", "events": ["fail"]}, "ntfy")
        self.assertEqual(out["events"], ["fail"])
        self.assertEqual(settings.ntfy_events(), ["fail"])
        with self.assertRaises(ValueError) as cm:
            notifier.save_target({"type": "ntfy", "url": "https://ntfy.example/other"}, "ntfy")
        self.assertEqual(str(cm.exception), "set_by_env:TVSIGN_NTFY_URL")

    def test_env_preset_events_are_locked(self):
        os.environ["TVSIGN_NTFY_URL"] = "https://ntfy.example/env"
        os.environ["TVSIGN_NTFY_EVENTS"] = "fail"
        with self.assertRaises(ValueError) as cm:
            notifier.save_target({"type": "ntfy", "url": "https://ntfy.example/env", "events": ["update"]}, "ntfy")
        self.assertEqual(str(cm.exception), "set_by_env:TVSIGN_NTFY_EVENTS")


class SendFailureCauses(unittest.TestCase):
    def test_codes(self):
        http = lambda code: urllib.error.HTTPError("https://x.example", code, "m", {}, None)  # noqa: E731
        cases = [
            (http(401), "http_401"), (http(403), "http_403"), (http(404), "http_404"), (http(429), "http_429"),
            (http(500), "http_other"), (http(302), "redirect"),
            (urllib.error.URLError(ConnectionRefusedError(111, "refused")), "refused"),
            (urllib.error.URLError(socket.gaierror(-2, "Name or service not known")), "dns"),
            (urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")), "tls"),
            (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
            (TimeoutError("timed out"), "timeout"),
            (RuntimeError("boom"), "other"),
        ]
        for error, code in cases:
            self.assertEqual(notifier.error_code(error), code, error)

    def test_api_detail_is_translatable(self):
        self.assertEqual(notifier.NotifyFailed("HTTP 401", "http_401").api_detail, "notify_http_401:HTTP 401")
        self.assertEqual(notifier.NotifyFailed("boom").api_detail, "notify_failed:boom")

    def test_every_code_has_a_message_in_both_languages(self):
        text = (Path(__file__).resolve().parent.parent / "web" / "i18n.js").read_text(encoding="utf-8")
        for code in ("http_401", "http_403", "http_404", "http_429", "http_other", "redirect", "timeout",
                     "refused", "dns", "tls", "failed"):
            self.assertIn(f"err_notify_{code}:", text)
        self.assertIn("err_builtin_no_token:", text)


class RestoreClearsVersionState(unittest.TestCase):
    def test_previous_version_is_cleared(self):
        db.init()
        app_id = db.execute(
            "INSERT INTO apps (name, source_type, source, source_tag, version, status, enabled, created_at, "
            "prev_tag, prev_key, prev_version, prev_sha256) "
            "VALUES ('R', 'github', 'o/r', 'v2', '2.0', 'ok', 1, 0, 'v1', 'k1', '1.0', 'abc')")
        archive = backup.export()
        self.addCleanup(archive.unlink, missing_ok=True)
        backup.restore(archive)
        row = db.row("SELECT * FROM apps WHERE id = ?", app_id)
        self.assertEqual((row["prev_tag"], row["prev_key"], row["prev_version"], row["prev_sha256"]),
                         (None, None, None, None))
        self.assertEqual(row["source_tag"], "v2")


if __name__ == "__main__":
    unittest.main()
