"""Notifications, compatibilité tvOS, retour arrière, restauration, support : sans plumesign ni Apple TV.

    python -m unittest discover -s tests
"""
import asyncio
import hashlib
import io
import json
import os
import plistlib
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
import zipfile
from pathlib import Path

DATA = tempfile.mkdtemp(prefix="tvsign-test-g3-")
os.environ.update(TVSIGN_DATA=DATA, TVSIGN_AUTH="none", TVSIGN_PLUMESIGN=str(Path(DATA) / "absent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import backup, db, devices, diag, ipa, main, notifier, plume, proc, renew, settings, support  # noqa: E402
from app.config import DB_PATH, IPA_DIR, PLUME_HOME  # noqa: E402
from app.util import ContentError  # noqa: E402

HEADERS = {"X-Tvsign": "1"}
STORE = PLUME_HOME / "PlumeImpactor"


def make_ipa(min_os=None, bundle="com.example.tv", version="1.0", marker="") -> bytes:
    info = {"CFBundleName": "Test", "CFBundleIdentifier": bundle, "CFBundleShortVersionString": version,
            "DTPlatformName": "appletvos"}
    if min_os:
        info["MinimumOSVersion"] = min_os
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Payload/Test.app/Info.plist", plistlib.dumps(info))
        if marker:
            z.writestr("Payload/Test.app/marker", marker)
    return buf.getvalue()


# ---------- tvOS minimale (à l'ajout d'une app) ----------

class AddChecksOs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def tv(self, os_version):
        return db.execute("INSERT INTO devices (name, pair_identifier, os_version, created_at) VALUES (?, ?, ?, 0)",
                          "TV", f"pair-{time.time_ns()}", os_version)

    def add(self, content, device_id, bundle):
        return self.client.post("/api/apps", headers=HEADERS, data={"source_type": "upload", "device_id": device_id},
                                files={"file": (f"{bundle}.ipa", content, "application/octet-stream")})

    def test_too_old_tvos_is_refused_before_signing(self):
        r = self.add(make_ipa("26.0", "com.example.new"), self.tv("18.4"), "new")
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "tvos_too_old:26.0"))
        self.assertEqual(db.rows("SELECT id FROM apps WHERE bundle_id = 'com.example.new'"), [])

    def test_compatible_or_unknown_tvos_is_accepted(self):
        renewals = []
        saved = renew.spawn
        renew.spawn = lambda *a, **k: renewals.append(a) or True
        try:
            self.assertEqual(self.add(make_ipa("26.0", "com.example.ok1"), self.tv("26.1"), "ok1").status_code, 200)
            self.assertEqual(self.add(make_ipa("26.0", "com.example.ok2"), self.tv(None), "ok2").status_code, 200)
        finally:
            renew.spawn = saved


# ---------- harnais de renouvellement ----------

class RenewCase(unittest.TestCase):
    """Un renouvellement complet, avec plumesign, Apple TV et GitHub remplacés par des doubles."""

    def setUp(self):
        db.init()
        renew.STOPPING = False          # l'arrêt d'un TestClient précédent l'a posé
        self.sent = []
        self.installs = []
        self.sign_error = None
        self.install_error = None
        self.saved = dict(notify=renew.notify, check=devices.check_reachable, refresh=devices.refresh_info,
                          ensure=plume.ensure_device, sign=plume.sign, install=devices.install,
                          accounts=plume.accounts, default=plume.default_account, latest=renew._latest,
                          download=ipa.download, usable=renew._usable_account, preflight=renew._preflight_os)
        renew.notify = self.fake_notify
        devices.check_reachable = self.async_noop
        devices.refresh_info = self.async_noop
        plume.ensure_device = self.async_noop
        plume.sign = self.fake_sign
        devices.install = self.fake_install
        plume.accounts = lambda: [{"email": "a@b.c", "ok": 1, "selected": True}]
        plume.default_account = lambda: "a@b.c"
        renew._usable_account = self.fake_account
        renew._preflight_os = self.async_noop
        self.release = None
        renew._latest = lambda app: self.release
        ipa.download = self.fake_download
        self.device_id = db.execute(
            "INSERT INTO devices (name, pair_identifier, udid, pairing_udid, os_version, created_at) "
            "VALUES ('TV', ?, 'UDID', 'UDID', '18.4', 0)", f"pair-{time.time_ns()}")

    def tearDown(self):
        renew.notify, devices.check_reachable, devices.refresh_info = self.saved["notify"], self.saved["check"], self.saved["refresh"]
        plume.ensure_device, plume.sign, devices.install = self.saved["ensure"], self.saved["sign"], self.saved["install"]
        plume.accounts, plume.default_account, renew._latest = self.saved["accounts"], self.saved["default"], self.saved["latest"]
        renew._usable_account, renew._preflight_os = self.saved["usable"], self.saved["preflight"]
        ipa.download = self.saved["download"]

    async def async_noop(self, *a, **k):
        return None

    async def fake_account(self, app):
        return "a@b.c"

    async def fake_notify(self, message, priority="default", event="fail", **extra):
        self.sent.append((event, message))
        return True

    async def fake_sign(self, email, original, signed, on_lock=None):
        if self.sign_error:
            raise self.sign_error
        signed.write_bytes(original.read_bytes())
        return False

    async def fake_install(self, device, ipas, on_event=None, wait_cap=None, app_name=None):
        self.installs.append(wait_cap)
        if self.install_error:
            raise self.install_error

    def fake_download(self, url, dest, sha256=None, expect_bundle=None, max_os=None, unchanged_sha=None):
        if max_os and ipa.os_older(max_os, self.new_min_os):
            raise ipa.OsTooOld(self.new_min_os, max_os)
        part = dest.with_suffix(".part")
        part.write_bytes(make_ipa(marker="new", version="2.0"))
        os.replace(part, dest)
        return {"version": "2.0", "bundle_id": "com.example.tv", "sha256": "abc"}

    new_min_os = None

    def add_app(self, expires_in=3 * 86400, status="ok", **over):
        old = make_ipa(marker="old")
        digest = hashlib.sha256(old).hexdigest()
        fields = dict(name="Nuvio", source_type="github", source="o/r", source_tag="v1", source_key="k1",
                      version="1.0", bundle_id="com.example.tv", device_id=self.device_id, account_email="a@b.c",
                      status=status, expires_at=time.time() + expires_in, signed_at=time.time() - 86400,
                      enabled=1, channel="stable", created_at=0, source_sha256=digest, installed_ipa=digest,
                      installed_version="1.0", installed_tag="v1")
        fields.update(over)
        cols = ", ".join(fields)
        app_id = db.execute(f"INSERT INTO apps ({cols}) VALUES ({', '.join('?' * len(fields))})", *fields.values())
        (renew.app_dir(app_id) / "original.ipa").write_bytes(old)
        return app_id

    def app(self, app_id):
        return db.row("SELECT * FROM apps WHERE id = ?", app_id)

    def new_release(self, tag="v2"):
        self.release = {"tag": tag, "url": "https://x/" + tag, "sha256": None, "key": "k-" + tag,
                        "notes": "Fixes everything"}

    def run_renew(self, app_id, trigger):
        asyncio.run(renew._renew(app_id, trigger))


class IncompatibleRelease(RenewCase):
    def test_release_needing_newer_tvos_is_refused_and_announced(self):
        app_id = self.add_app()
        self.new_release()
        self.new_min_os = "26.0"
        self.run_renew(app_id, "nouvelle version")
        row = self.app(app_id)
        self.assertEqual((row["status"], row["source_tag"], row["bad_tag"]), ("ok", "v1", "v2"))
        self.assertEqual(row["update_block"], "tvos:26.0")
        self.assertTrue(any(e == "update" and "26.0" in m and "18.4" in m for e, m in self.sent), self.sent)

    def test_new_tvos_makes_the_refused_version_a_candidate_again(self):
        app_id = self.add_app(bad_tag="v2", bad_key="k-v2", update_tag="v2", update_block="tvos:26.0")
        db.update("devices", self.device_id, os_version="26.1")
        asyncio.run(renew._lift_os_block(self.app(app_id)))
        row = self.app(app_id)
        self.assertEqual((row["bad_tag"], row["update_block"]), (None, None))


class Rollback(RenewCase):
    def test_new_release_installed_confirms_the_update_and_announces_notes(self):
        app_id = self.add_app()
        self.new_release()
        self.run_renew(app_id, "nouvelle version")
        row = self.app(app_id)
        self.assertEqual((row["status"], row["source_tag"], row["installed_version"]), ("ok", "v2", "2.0"))
        self.assertFalse(renew.pending_update(row))
        events = [(e, m) for e, m in self.sent if e == "update"]
        self.assertEqual(len(events), 1)
        self.assertIn("2.0", events[0][1])
        self.assertIn("Fixes everything", events[0][1])
        self.assertTrue(renew.has_previous(app_id))                          # retour arrière manuel possible

    def test_only_faults_of_the_ipa_justify_giving_up_a_new_version(self):
        self.assertTrue(renew.ipa_fault(ContentError("signature : invalid Info.plist in bundle")))
        self.assertTrue(renew.ipa_fault(RuntimeError("installation de x.ipa : paquet illisible")))
        self.assertFalse(renew.ipa_fault(plume.Transient("anisette indisponible")))
        self.assertFalse(renew.ipa_fault(devices.AppInUse("installd ne progresse plus")))
        self.assertFalse(renew.ipa_fault(plume.LimitReached("limite d'App ID")))
        self.assertFalse(renew.ipa_fault(RuntimeError("installation : no space left on device")))
        self.assertFalse(renew.ipa_fault(plume.SessionExpired("session expirée")))

    def test_a_content_error_on_the_new_release_reinstalls_the_previous_one(self):
        app_id = self.add_app()
        self.new_release()

        async def sign(email, original, signed, on_lock=None):
            data = original.read_bytes()
            if b"new" in data:
                raise ContentError("signature : invalid Info.plist in bundle")
            signed.write_bytes(data)
            return False
        plume.sign = sign
        self.run_renew(app_id, "nouvelle version")
        row = self.app(app_id)
        self.assertEqual((row["status"], row["source_tag"], row["version"], row["bad_tag"]), ("ok", "v1", "1.0", "v2"))
        self.assertTrue(any(e == "update" and "v2" in m for e, m in self.sent), self.sent)

    def test_account_limits_never_roll_back(self):
        app_id = self.add_app()
        self.new_release()
        self.sign_error = plume.LimitReached("limite du compte gratuit")
        self.run_renew(app_id, "nouvelle version")
        row = self.app(app_id)
        self.assertEqual(row["source_tag"], "v2")                            # la nouvelle version reste en place
        self.assertEqual(row["needs_action"], "limit")


class AutoUpdateOff(RenewCase):
    def test_new_version_is_only_announced_once(self):
        app_id = self.add_app(auto_update=0)
        self.new_release()
        self.assertFalse(asyncio.run(renew.new_release(self.app(app_id))))
        self.assertFalse(asyncio.run(renew.new_release(self.app(app_id))))       # une seule annonce par version
        row = self.app(app_id)
        self.assertEqual((row["update_tag"], row["update_block"], row["source_tag"]), ("v2", "manual", "v1"))
        self.assertEqual([e for e, _ in self.sent], ["update"])
        self.assertIn("v2", self.sent[0][1])

    def test_renewal_keeps_the_version_until_asked(self):
        app_id = self.add_app(auto_update=0)
        self.new_release()
        self.run_renew(app_id, "auto")
        self.assertEqual(self.app(app_id)["source_tag"], "v1")
        self.run_renew(app_id, "mise à jour")
        row = self.app(app_id)
        self.assertEqual((row["source_tag"], row["update_tag"]), ("v2", None))


class QuietFailures(RenewCase):
    def test_app_in_use_far_from_expiry_is_quiet(self):
        app_id = self.add_app(expires_in=50 * 3600)
        self.install_error = devices.AppInUse("installd ne progresse plus")
        self.run_renew(app_id, "auto")
        row = self.app(app_id)
        self.assertEqual((row["status"], row["fail_kind"]), ("error", "transient"))
        self.assertEqual(self.sent, [])

    def test_app_in_use_close_to_expiry_notifies(self):
        app_id = self.add_app(expires_in=10 * 3600)
        self.install_error = devices.AppInUse("installd ne progresse plus")
        self.run_renew(app_id, "auto")
        self.assertEqual([e for e, _ in self.sent], ["expiry"])

    def test_real_failure_far_from_expiry_notifies(self):
        app_id = self.add_app(expires_in=50 * 3600)
        self.sign_error = RuntimeError("signature : refusée par Apple")
        self.run_renew(app_id, "auto")
        self.assertEqual([e for e, _ in self.sent], ["fail"])

    def test_optional_update_blip_keeps_app_green_and_silent(self):
        app_id = self.add_app()
        self.new_release()
        for error in (devices.AppInUse("occupée"), plume.Transient("anisette"), devices.Unreachable("TV en veille")):
            self.install_error = self.sign_error = None
            if isinstance(error, plume.Transient):
                self.sign_error = error
            else:
                self.install_error = error
            self.run_renew(app_id, "nouvelle version")
            row = self.app(app_id)
            self.assertEqual((row["status"], row["last_error"]), ("ok", None), error)
        self.assertEqual(self.sent, [])
        self.assertTrue(renew.pending_update(self.app(app_id)))               # sera retentée

    def test_optional_update_waits_less_than_a_renewal(self):
        app_id = self.add_app()
        self.new_release()
        self.run_renew(app_id, "nouvelle version")
        self.run_renew(app_id, "manuel")
        self.assertEqual(self.installs[0], renew.UPDATE_WAIT_MINUTES * 60)
        self.assertGreater(self.installs[1], self.installs[0])


class RecoveryPush(RenewCase):
    def test_no_renewed_push_after_a_quiet_failure(self):
        app_id = self.add_app(expires_in=50 * 3600)
        self.install_error = devices.Unreachable("TV en veille")
        self.run_renew(app_id, "auto")
        self.assertEqual(self.app(app_id)["status"], "error")
        self.install_error = None
        self.run_renew(app_id, "auto")
        self.assertEqual(self.sent, [])

    def test_renewed_push_after_a_failure_push(self):
        app_id = self.add_app(expires_in=50 * 3600)
        self.sign_error = RuntimeError("signature : refusée")
        self.run_renew(app_id, "auto")
        db.update("apps", app_id, notified_level="fail")        # l'alerte est bien partie
        self.sign_error = None
        self.run_renew(app_id, "auto")
        self.assertEqual([e for e, _ in self.sent], ["fail", "recovered"])


class History(RenewCase):
    def test_every_attempt_is_recorded(self):
        app_id = self.add_app()
        self.sign_error = RuntimeError("boom")
        asyncio.run(renew.renew(app_id, "manuel"))
        self.sign_error = None
        asyncio.run(renew.renew(app_id, "manuel"))
        rows = db.rows("SELECT * FROM renewals WHERE app_id = ? ORDER BY id", app_id)
        self.assertEqual([(r["outcome"], r["cause"]) for r in rows], [("error", "manuel"), ("ok", "manuel")])
        self.assertIn("boom", rows[0]["error"])


# ---------- notifications ----------

class Providers(unittest.TestCase):
    def build(self, target, event="fail", message="msg", priority="high", extra=None):
        return notifier.build(notifier.clean(target), event, message, priority, extra)

    def test_ntfy(self):
        url, data, headers = self.build({"type": "ntfy", "url": "https://ntfy.example.org/topic", "token": "tk_abcdef"})
        self.assertEqual((url, data, headers["Priority"]), ("https://ntfy.example.org/topic", b"msg", "high"))
        self.assertEqual(headers["Authorization"], "Bearer tk_abcdef")

    def test_ntfy_basic_auth_and_ipv6(self):
        url, _, headers = self.build({"type": "ntfy", "url": "http://user:p%40ss@[::1]:8080/t"})
        self.assertEqual(url, "http://[::1]:8080/t")
        self.assertTrue(headers["Authorization"].startswith("Basic "))

    def test_bare_topic_expands(self):
        self.assertEqual(notifier.clean({"type": "ntfy", "url": "my-topic"})["url"], "https://ntfy.sh/my-topic")

    def test_webhook(self):
        url, data, headers = self.build({"type": "webhook", "url": "https://hooks.example.org/x"},
                                        extra={"app": "Nuvio"})
        body = json.loads(data)
        self.assertEqual((url, body["message"], body["event"], body["app"], body["priority"]),
                         ("https://hooks.example.org/x", "msg", "fail", "Nuvio", "high"))
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_discord_never_pings_everyone(self):
        _, data, _ = self.build({"type": "discord", "url": "https://discord.com/api/webhooks/123/abcDEF"},
                                message="@everyone " + "x" * 3000)
        body = json.loads(data)
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertLessEqual(len(body["content"]), 1900)

    def test_telegram(self):
        token = "123456789:" + "A" * 30
        url, data, _ = self.build({"type": "telegram", "token": token, "chat_id": "-100123"})
        self.assertEqual(url, f"https://api.telegram.org/bot{token}/sendMessage")
        self.assertEqual(json.loads(data)["chat_id"], "-100123")

    def test_gotify(self):
        url, data, headers = self.build({"type": "gotify", "url": "https://gotify.example.org/", "token": "Atoken123"},
                                        priority="urgent")
        self.assertEqual(url, "https://gotify.example.org/message")
        self.assertEqual((headers["X-Gotify-Key"], json.loads(data)["priority"]), ("Atoken123", 10))

    def test_invalid_input_is_refused_with_codes(self):
        bad = [({"type": "ntfy", "url": "http://a b"}, "url_expected"),
               ({"type": "ntfy", "url": ""}, "url_expected"),
               ({"type": "webhook", "url": "ftp://x/y"}, "url_expected"),
               ({"type": "discord", "url": "https://evil.example/api/webhooks/1/2"}, "discord_url_invalid"),
               ({"type": "discord", "url": "http://discord.com/api/webhooks/1/2"}, "discord_url_invalid"),
               ({"type": "telegram", "token": "nope", "chat_id": "1"}, "telegram_token_invalid"),
               ({"type": "telegram", "token": "1234:" + "A" * 30, "chat_id": "abc"}, "telegram_chat_invalid"),
               ({"type": "gotify", "url": "https://g.example", "token": ""}, "gotify_token_invalid"),
               ({"type": "mail"}, "notify_type_invalid"),
               ({"type": "ntfy", "url": "https://n.example/t", "events": ["bogus"]}, "events_invalid")]
        for raw, code in bad:
            with self.assertRaises(ValueError, msg=raw) as cm:
                notifier.clean(raw)
            self.assertEqual(str(cm.exception), code, raw)


class Targets(unittest.TestCase):
    def setUp(self):
        db.init()
        settings.raw_set(notifier.TARGETS_KEY, None)
        settings.update({"ntfy_url": None, "ntfy_events": None})
        self.saved = notifier.send
        self.calls = []
        notifier.send = lambda t, event, message, priority="default", extra=None: self.calls.append((t["type"], event, message))

    def tearDown(self):
        notifier.send = self.saved
        settings.raw_set(notifier.TARGETS_KEY, None)
        settings.update({"ntfy_url": None, "ntfy_events": None})

    def test_events_select_destinations(self):
        notifier.save_target({"type": "webhook", "url": "https://h.example/a", "events": ["fail"]})
        notifier.save_target({"type": "gotify", "url": "https://g.example", "token": "token-1234", "events": ["update", "fail"]})
        settings.update({"ntfy_url": "https://ntfy.example/t", "ntfy_events": "expiry"})
        self.assertTrue(asyncio.run(notifier.dispatch("fail", "m1")))
        self.assertTrue(asyncio.run(notifier.dispatch("update", "m2")))
        self.assertTrue(asyncio.run(notifier.dispatch("expiry", "m3")))
        self.assertFalse(asyncio.run(notifier.dispatch("account", "m4")))
        self.assertEqual(self.calls, [("webhook", "fail", "m1"), ("gotify", "fail", "m1"),
                                      ("gotify", "update", "m2"), ("ntfy", "expiry", "m3")])

    def test_one_failing_destination_does_not_block_the_others(self):
        notifier.save_target({"type": "webhook", "url": "https://h.example/a"})
        notifier.save_target({"type": "gotify", "url": "https://g.example", "token": "token-1234"})

        def send(t, event, message, priority="default", extra=None):
            if t["type"] == "webhook":
                raise urllib.error.URLError("connection refused")
            self.calls.append(t["type"])
        notifier.send = send
        self.assertTrue(asyncio.run(notifier.dispatch("fail", "m")))
        self.assertEqual(self.calls, ["gotify"])
        self.assertIn("connection refused", notifier.last_error())

    def test_legacy_ntfy_url_is_a_destination_with_all_events(self):
        settings.update({"ntfy_url": "https://ntfy.example/t"})
        self.assertTrue(notifier.configured())
        [t] = notifier.targets()
        self.assertEqual((t["id"], t["type"], t["events"]), ("ntfy", "ntfy", list(settings.NOTIFY_EVENTS)))

    def test_secrets_are_masked_and_kept(self):
        t = notifier.save_target({"type": "gotify", "url": "https://g.example", "token": "secret-token-1"})
        self.assertEqual(t["token"], notifier.MASK)
        self.assertNotIn("secret-token-1", json.dumps(notifier.listing()))
        notifier.save_target({"type": "gotify", "url": "https://g.example", "token": notifier.MASK, "name": "Home"}, t["id"])
        self.assertEqual(notifier.get_target(t["id"])["token"], "secret-token-1")
        self.assertEqual(notifier.get_target(t["id"])["name"], "Home")
        notifier.save_target({"type": "gotify", "url": "https://g.example", "token": "other-token-2"}, t["id"])
        self.assertEqual(notifier.get_target(t["id"])["token"], "other-token-2")

    def test_webhook_url_is_a_secret(self):
        t = notifier.save_target({"type": "discord", "url": "https://discord.com/api/webhooks/123456/SECRETSECRETSECRET"})
        shown = json.dumps(notifier.listing())
        self.assertNotIn("SECRETSECRET", shown)
        self.assertEqual(t["summary"], "discord.com")

    def test_ntfy_password_is_masked_and_kept(self):
        settings.update({"ntfy_url": "https://user:hunter2@ntfy.example/t"})
        shown = settings.describe()["ntfy_url"]["value"]
        self.assertEqual(shown, "https://user:***@ntfy.example/t")
        settings.update({"ntfy_url": shown})                               # le formulaire renvoie la valeur masquée
        self.assertEqual(settings.ntfy_url(), "https://user:hunter2@ntfy.example/t")
        self.assertNotIn("hunter2", json.dumps(notifier.listing()))
        settings.update({"ntfy_url": "https://user:other@ntfy.example/t"})
        self.assertEqual(settings.ntfy_url(), "https://user:other@ntfy.example/t")

    def test_errors_never_contain_secrets(self):
        t = notifier.clean({"type": "telegram", "token": "123456789:" + "S" * 30, "chat_id": "42424"})
        e = urllib.error.URLError("failed for https://api.telegram.org/bot123456789:" + "S" * 30 + "/sendMessage")
        self.assertNotIn("SSSSSSSS", notifier.describe_error(e, t))

    def test_save_limits(self):
        for i in range(notifier.MAX_TARGETS):
            notifier.save_target({"type": "webhook", "url": f"https://h.example/{i}"})
        with self.assertRaises(ValueError):
            notifier.save_target({"type": "webhook", "url": "https://h.example/more"})

    def test_message_catalogue_is_complete_and_formats(self):
        params = dict(name="N", reason="r", h=6, until="u", tag="t", why="w", version="v", notes="", prev="p",
                      email="e@x.y", need="26.0", have="18.4", system="tvOS", who="l'Apple TV", times=3,
                      d=7, apps="A, B")
        for key in notifier.MESSAGES:
            for lang in ("fr", "en"):
                self.assertTrue(notifier.text(key, lang, **params), (key, lang))

    def test_time_has_an_explicit_offset(self):
        self.assertRegex(notifier.fmt_time(time.time()), r"^\d{4}-\d\d-\d\d \d\d:\d\d UTC[+-]\d\d:\d\d$")

    def test_language_follows_the_ui_when_auto(self):
        settings.update({"language": None})
        settings.raw_set("ui_lang", "fr")
        self.assertEqual(notifier.lang(), "fr")
        settings.update({"language": "en"})
        self.assertEqual(notifier.lang(), "en")
        settings.update({"language": None})
        settings.raw_set("ui_lang", None)


class NoRedirect(unittest.TestCase):
    def test_redirect_is_an_error_not_followed(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            seen = []

            def do_POST(self):
                Handler.seen.append((self.path, self.headers.get("Authorization")))
                # The body is read before answering: closing a socket that still holds unread data resets the
                # connection (WinError 10053 on Windows), and the client then never sees the 307.
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(307)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            target = notifier.clean({"type": "ntfy", "url": f"http://u:pw@127.0.0.1:{server.server_port}/t"})
            with self.assertRaises(urllib.error.HTTPError):
                notifier.send(target, "fail", "m")
            self.assertEqual(len(Handler.seen), 1)
        finally:
            server.shutdown()


class SettingsValidation(unittest.TestCase):
    def setUp(self):
        db.init()

    def err(self, values):
        with self.assertRaises(ValueError) as cm:
            settings.update(values)
        return cm.exception.args[0]

    def test_url_with_space_is_refused(self):
        self.assertEqual(self.err({"ntfy_url": "http://a b"}), {"ntfy_url": "url_expected"})
        self.assertEqual(self.err({"anisette_url": "ftp://x"}), {"anisette_url": "url_expected"})
        self.assertEqual(self.err({"anisette_url": "http://host:99999/x"}), {"anisette_url": "url_expected"})

    def test_hours_must_be_a_plain_number(self):
        self.assertEqual(self.err({"renew_before_hours": "1_00"}), {"renew_before_hours": "number_expected"})
        self.assertEqual(self.err({"renew_before_hours": "nan"}), {"renew_before_hours": "number_expected"})
        self.assertEqual(self.err({"renew_before_hours": "5"}), {"renew_before_hours": "range:12:150"})
        settings.update({"renew_before_hours": "48"})
        self.assertEqual(settings.renew_before_hours(), 48.0)
        settings.update({"renew_before_hours": None})

    def test_other_codes(self):
        self.assertEqual(self.err({"nope": "1"}), {"nope": "unknown_setting"})
        self.assertEqual(self.err({"language": "de"}), {"language": "language_expected"})
        self.assertEqual(self.err({"ntfy_events": "fail,bogus"}), {"ntfy_events": "events_invalid"})

    def test_ntfy_events_default_to_all(self):
        self.assertEqual(settings.events_of(""), list(settings.NOTIFY_EVENTS))
        self.assertEqual(settings.events_of("none"), [])
        self.assertEqual(settings.events_of("fail,update"), ["fail", "update"])


# ---------- sauvegarde ----------

def backup_zip(path: Path, db_bytes: bytes | None, manifest=None, extra=None) -> Path:
    manifest = {"format": backup.FORMAT, "version": 1, "created": 1} if manifest is None else manifest
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        if db_bytes is not None:
            z.writestr("tvsign.db", db_bytes)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


def sqlite_bytes(statements: list[str]) -> bytes:
    path = Path(tempfile.mkdtemp()) / "x.db"
    conn = sqlite3.connect(path)
    for s in statements:
        conn.execute(s)
    conn.commit()
    conn.close()
    return path.read_bytes()


class RestoreValidation(unittest.TestCase):
    def setUp(self):
        db.init()
        self.tmp = Path(tempfile.mkdtemp())
        self.before = db.rows("SELECT id FROM apps")

    def code(self, archive):
        with self.assertRaises(backup.BackupError) as cm:
            backup.restore(archive)
        return cm.exception.code

    def assertUntouched(self):
        self.assertEqual(db.rows("SELECT id FROM apps"), self.before)
        db.rows("SELECT a.*, d.name AS device_name FROM apps a LEFT JOIN devices d ON d.id = a.device_id")

    def test_missing_columns_are_refused_without_touching_anything(self):
        data = sqlite_bytes(["CREATE TABLE apps (id INTEGER PRIMARY KEY, foo TEXT)", "CREATE TABLE devices (id INTEGER)"])
        self.assertEqual(self.code(backup_zip(self.tmp / "a.zip", data)), "backup_incompatible")
        self.assertUntouched()

    def test_garbage_database(self):
        self.assertEqual(self.code(backup_zip(self.tmp / "b.zip", b"this is not a database" * 50)), "backup_corrupt")
        self.assertUntouched()

    def test_manifest_oddities(self):
        data = sqlite_bytes(["CREATE TABLE apps (id INTEGER)", "CREATE TABLE devices (id INTEGER)"])
        self.assertEqual(self.code(backup_zip(self.tmp / "c.zip", data, manifest=[1])), "backup_not_tvsign")
        self.assertEqual(self.code(backup_zip(self.tmp / "d.zip", data, manifest={"format": backup.FORMAT, "version": "x"})),
                         "backup_not_tvsign")
        self.assertEqual(self.code(backup_zip(self.tmp / "e.zip", data, manifest={"format": backup.FORMAT, "version": 99})),
                         "backup_too_new")
        self.assertEqual(self.code(backup_zip(self.tmp / "f.zip", None)), "backup_no_db")
        self.assertEqual(self.code(backup_zip(self.tmp / "g.zip", data, extra={"../evil": b"x"})), "backup_bad_path")
        (self.tmp / "h.zip").write_bytes(b"PK nothing")
        self.assertEqual(self.code(self.tmp / "h.zip"), "backup_unreadable")
        self.assertUntouched()

    def test_old_schema_is_migrated_before_the_swap(self):
        # une base d'une version antérieure (sans les colonnes de MIGRATIONS) se migre sur la copie
        base = db.SCHEMA
        path = self.tmp / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(base)
        conn.close()
        backup.validate_db(path)
        conn = sqlite3.connect(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(apps)")}
        conn.close()
        self.assertIn("auto_update", cols)


class RestoreAtomic(unittest.TestCase):
    def setUp(self):
        db.init()
        STORE.mkdir(parents=True, exist_ok=True)
        (STORE / "accounts.json").write_text("{}")
        (STORE / "live-marker").unlink(missing_ok=True)
        self.tmp = Path(tempfile.mkdtemp())
        self.archive = backup.export()
        self.addCleanup(self.archive.unlink, missing_ok=True)

    def test_failure_midway_rolls_everything_back(self):
        (STORE / "live-marker").write_text("live")
        (IPA_DIR / "777").mkdir(parents=True, exist_ok=True)
        (IPA_DIR / "777" / "original.ipa").write_bytes(b"live ipa")
        apps_before = db.rows("SELECT id FROM apps")
        real = backup.replace_retry

        def failing(src, dst, *a, **k):
            if Path(dst) == DB_PATH:
                raise PermissionError("verrouillé par un antivirus")
            return real(src, dst, *a, **k)
        backup.replace_retry = failing
        try:
            with self.assertRaises(backup.BackupError) as cm:
                backup.restore(self.archive)
        finally:
            backup.replace_retry = real
        self.assertEqual(cm.exception.code, "restore_failed")
        self.assertEqual((STORE / "live-marker").read_text(), "live")
        self.assertEqual((IPA_DIR / "777" / "original.ipa").read_bytes(), b"live ipa")
        self.assertEqual(db.rows("SELECT id FROM apps"), apps_before)

    def test_roundtrip_keeps_the_previous_state_aside(self):
        (STORE / "live-marker").write_text("live")
        counts = backup.restore(self.archive)
        self.assertIn("apps", counts)
        self.assertFalse((STORE / "live-marker").exists())
        aside = list(PLUME_HOME.glob("PlumeImpactor.pre-restore-*"))
        self.assertTrue(any((p / "live-marker").exists() for p in aside))
        self.assertTrue(IPA_DIR.is_dir())


class RestoreApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def upload(self, content):
        return self.client.post("/api/backup/import", headers=HEADERS, files={"file": ("b.tvsign-backup", content)})

    def test_bad_archives_answer_400_with_a_code_not_500(self):
        data = sqlite_bytes(["CREATE TABLE apps (id INTEGER PRIMARY KEY, foo TEXT)", "CREATE TABLE devices (id INTEGER)"])
        path = backup_zip(Path(tempfile.mkdtemp()) / "x.zip", data)
        r = self.upload(path.read_bytes())
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "backup_incompatible"))
        r = self.upload(backup_zip(Path(tempfile.mkdtemp()) / "y.zip", b"garbage" * 40).read_bytes())
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "backup_corrupt"))
        r = self.upload(backup_zip(Path(tempfile.mkdtemp()) / "z.zip", data, manifest=[1]).read_bytes())
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "backup_not_tvsign"))
        self.assertEqual(self.client.get("/api/state").status_code, 200)       # l'instance fonctionne toujours
        self.assertFalse(backup.RESTORING)

    def test_busy_is_a_code(self):
        renew._RUNNING.add(424242)
        try:
            r = self.upload(b"x")
            self.assertEqual((r.status_code, r.json()["detail"]), (409, "busy"))
        finally:
            renew._RUNNING.discard(424242)

    def test_api_pauses_while_restoring(self):
        backup.RESTORING = True
        try:
            r = self.client.get("/api/state")
            self.assertEqual((r.status_code, r.json()["detail"]), (503, "restoring"))
            self.assertEqual(self.client.get("/api/livez").status_code, 200)
            self.assertFalse(renew.spawn(1, "manuel"))
        finally:
            backup.RESTORING = False

    def test_valid_backup_restores_through_the_api(self):
        archive = backup.export()
        try:
            r = self.upload(archive.read_bytes())
        finally:
            archive.unlink(missing_ok=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("apps", r.json())


# ---------- boucle, diagnostic ----------

class Supervision(unittest.TestCase):
    def test_handler_survives_a_failing_journal(self):
        saved = db.log
        db.log = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
        try:
            renew._log_quietly("x", "error")          # ne lève pas
        finally:
            db.log = saved

    def test_watchdog_warns_once_per_stall(self):
        db.init()
        sent = []

        async def fake_notify(message, priority="default", event="fail", **extra):
            sent.append((event, priority))
            return True
        saved = renew.notify, renew.HEALTH["last_loop_at"]
        renew.notify = fake_notify
        renew.HEALTH["last_loop_at"] = time.time() - 10 ** 6
        renew._STALL_ALERTED = False

        async def run():
            task = asyncio.create_task(renew.watchdog(period=0.01))
            await asyncio.sleep(0.2)
            task.cancel()
        try:
            asyncio.run(run())
        finally:
            renew.notify, renew.HEALTH["last_loop_at"] = saved
            renew._STALL_ALERTED = False
        self.assertEqual(sent, [("fail", "urgent")])


class Diagnostics(unittest.TestCase):
    def test_malformed_date_header_does_not_fail_the_probe(self):
        class Resp:
            status = 200
            headers = {"Date": "garbage"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        saved = diag.urllib.request.urlopen
        diag.urllib.request.urlopen = lambda *a, **k: Resp()
        try:
            self.assertEqual(diag._head("https://example.org"), (200, None, ""))
        finally:
            diag.urllib.request.urlopen = saved

    def test_a_raising_probe_becomes_an_error_row(self):
        async def boom():
            raise ValueError("Invalid date")
        saved = diag._plumesign
        diag._plumesign = boom
        try:
            result = asyncio.run(diag.run_all())
        finally:
            diag._plumesign = saved
        row = next(c for c in result["checks"] if c["id"] == "plumesign")
        self.assertEqual(row["status"], "error")
        self.assertIn("Invalid date", row["detail"])


# ---------- support ----------

class Redaction(unittest.TestCase):
    def test_masks_identifying_data(self):
        text = ("login jean.dupont@example.com on 192.168.1.42 udid 00008110-001A2B3C4D5E6F01 "
                "https://user:pw@ntfy.sh/t?token=abc123 bot123456789:" + "Z" * 30 + " Bearer abcdefghijklmnop "
                "fe80:0:0:0:1:2:3:4 40hex " + "a" * 40)
        out = support.redact(text, secrets=["topic-secret"])
        for leak in ("jean.dupont", "192.168.1.42", "001A2B3C4D5E6F01", "pw@", "abc123", "ZZZZZZZ", "abcdefghijklmnop",
                     "fe80:0:0:0", "a" * 40):
            self.assertNotIn(leak, out)
        self.assertIn("192.168.x.x", out)
        self.assertNotIn("topic-secret", support.redact("x topic-secret y", secrets=["topic-secret"]))

    def test_plain_text_is_untouched(self):
        self.assertEqual(support.redact("Nuvio : renouvelée, version 1.0.5 (tvOS 26.0)"),
                         "Nuvio : renouvelée, version 1.0.5 (tvOS 26.0)")


class SupportApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        self.saved = plume.version, diag.run_all
        self.proc_ring = list(proc.RING)

        async def version():
            return "plumesign 0.0-test"

        async def run_all():
            return {"checks": [{"id": "apple", "status": "ok", "detail": "https://developerservices2.apple.com/ (HTTP 200)"}],
                    "ok": True, "version": "t", "python": "3", "platform": "x", "data_dir": DATA}
        plume.version = version
        diag.run_all = run_all
        db.log("renewal failed for jean@example.com via 10.1.2.3", "error")

    def tearDown(self):
        plume.version, diag.run_all = self.saved

    def test_bundle_is_a_zip_without_secrets(self):
        settings.update({"ntfy_url": "https://u:topsecretpw@ntfy.example/mytopicsecret"})
        proc.RING.append((time.time(), "plumesign sign", 1, ["error for jean@example.com on 10.1.2.3"]))
        try:
            r = self.client.get("/api/support-bundle")
        finally:
            settings.update({"ntfy_url": None})
        self.assertEqual(r.status_code, 200)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertEqual(set(z.namelist()), {"README.txt", "system.json", "diagnostics.json", "apps.json",
                                              "renewals.json", "events.txt", "plumesign-output.txt"})
        everything = "\n".join(z.read(n).decode("utf-8") for n in z.namelist())
        for leak in ("jean@example.com", "10.1.2.3", "topsecretpw", "mytopicsecret"):
            self.assertNotIn(leak, everything)
        self.assertIn("plumesign 0.0-test", z.read("system.json").decode())
        self.assertIn("<email>", z.read("events.txt").decode())
        self.assertIn("plumesign sign", z.read("plumesign-output.txt").decode())

    def test_log_export_is_masked_text(self):
        r = self.client.get("/api/log/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertNotIn("jean@example.com", r.text)

    def test_summary_for_copy(self):
        text = self.client.get("/api/support-summary").json()["text"]
        self.assertIn("[ok] apple", text)
        self.assertNotIn("jean@example.com", text)

    def test_events_are_paginated_and_filterable(self):
        for i in range(5):
            db.log(f"page test {i}", "warn" if i % 2 else "info", app_id=555)
        first = self.client.get("/api/events", params={"app_id": 555, "limit": 2}).json()
        self.assertEqual((len(first["events"]), first["more"]), (2, True))
        rest = self.client.get("/api/events", params={"app_id": 555, "before": first["events"][-1]["id"]}).json()
        self.assertEqual((len(rest["events"]), rest["more"]), (3, False))
        problems = self.client.get("/api/events", params={"app_id": 555, "level": "problems"}).json()["events"]
        self.assertTrue(all(e["level"] != "info" for e in problems))
        db.execute("DELETE FROM events WHERE app_id = 555")

    def test_process_output_is_remembered(self):
        before = len(proc.RING)
        res = asyncio.run(proc.run(sys.executable, "-c", "import sys; sys.stderr.write('oops\\n')", split=True))
        self.assertEqual(res.code, 0)
        self.assertEqual(len(proc.RING), min(before + 1, proc.RING.maxlen))
        self.assertIn("oops", proc.RING[-1][3])


class NotificationApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        settings.raw_set(notifier.TARGETS_KEY, None)
        settings.update({"ntfy_url": None, "ntfy_events": None})
        self.saved = notifier.send
        self.sent = []
        notifier.send = lambda t, event, message, priority="default", extra=None: self.sent.append((t["type"], message))

    def tearDown(self):
        notifier.send = self.saved
        settings.raw_set(notifier.TARGETS_KEY, None)
        settings.update({"ntfy_url": None, "ntfy_events": None})

    def test_crud_with_masking_and_test_button(self):
        r = self.client.post("/api/notifications", headers=HEADERS, json={
            "type": "gotify", "url": "https://g.example", "token": "api-token-xyz", "events": ["fail", "update"]})
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["id"]
        listing = self.client.get("/api/notifications").json()
        self.assertEqual([t["id"] for t in listing["targets"]], [tid])
        self.assertNotIn("api-token-xyz", json.dumps(listing))
        # le bouton Test réutilise le secret enregistré quand le formulaire renvoie la valeur masquée
        masked = listing["targets"][0]
        self.assertEqual(self.client.post("/api/notifications/test", headers=HEADERS, json=masked).status_code, 200)
        self.assertEqual(self.sent[0][0], "gotify")
        r = self.client.put(f"/api/notifications/{tid}", headers=HEADERS, json={**masked, "events": ["expiry"]})
        self.assertEqual(r.json()["events"], ["expiry"])
        self.assertEqual(self.client.delete(f"/api/notifications/{tid}", headers=HEADERS).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/notifications/{tid}", headers=HEADERS).status_code, 404)

    def test_validation_codes_and_failures(self):
        r = self.client.post("/api/notifications", headers=HEADERS, json={"type": "ntfy", "url": "http://a b"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "url_expected"))

        def failing(*a, **k):
            raise urllib.error.HTTPError("https://g.example/message?token=zzz-token-1", 401, "no", {}, None)
        notifier.send = failing
        r = self.client.post("/api/notifications/test", headers=HEADERS,
                             json={"type": "gotify", "url": "https://g.example", "token": "zzz-token-1"})
        self.assertEqual((r.status_code, r.json()["detail"]), (502, "notify_http_401:HTTP 401"))

    def test_legacy_ntfy_is_listed_and_editable(self):
        self.client.patch("/api/settings", headers=HEADERS, json={"ntfy_url": "my-topic"})
        [t] = self.client.get("/api/notifications").json()["targets"]
        self.assertEqual((t["id"], t["url"], t["builtin"]), ("ntfy", "https://ntfy.sh/my-topic", True))
        r = self.client.put("/api/notifications/ntfy", headers=HEADERS, json={"type": "ntfy", "url": t["url"], "events": ["fail"]})
        self.assertEqual(r.json()["events"], ["fail"])
        self.assertEqual(settings.ntfy_events(), ["fail"])
        self.assertEqual(self.client.delete("/api/notifications/ntfy", headers=HEADERS).status_code, 200)
        self.assertEqual(settings.ntfy_url(), "")

    def test_settings_errors_are_codes(self):
        r = self.client.patch("/api/settings", headers=HEADERS, json={"renew_before_hours": "2"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, {"renew_before_hours": "range:12:150"}))

    def test_old_test_endpoint_is_hardened(self):
        r = self.client.post("/api/settings/test-notification", headers=HEADERS, json={"url": "http://a b"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "url_expected"))
        r = self.client.post("/api/settings/test-notification", headers=HEADERS, json={"url": "https://ntfy.example/t"})
        self.assertEqual(r.status_code, 200)

    def test_ui_language_is_remembered(self):
        self.client.post("/api/ui-language", headers=HEADERS, json={"lang": "fr"})
        self.assertEqual(settings.raw_get("ui_lang"), "fr")
        self.client.post("/api/ui-language", headers=HEADERS, json={"lang": "xx"})
        self.assertEqual(settings.raw_get("ui_lang"), "fr")


class StateNotices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def test_state_exposes_the_revocation_notice_until_dismissed(self):
        settings.raw_set("revocation_notice", json.dumps({"email": "a@b.c", "app": "V", "at": 1}))
        state = self.client.get("/api/state").json()
        self.assertEqual(state["notices"][0]["id"], "revocation")
        self.client.post("/api/notices/revocation/dismiss", headers=HEADERS)
        self.assertEqual(self.client.get("/api/state").json()["notices"], [])


class Migration(unittest.TestCase):
    def test_old_database_keeps_its_data(self):
        """Une base 1.0.0 (sans les colonnes ni les tables ajoutées ici) se migre sans perdre de données."""
        path = Path(tempfile.mkdtemp()) / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("DROP TABLE renewals")
        conn.execute("INSERT INTO devices (name, pair_identifier, created_at) VALUES ('TV', 'p', 0)")
        conn.execute("INSERT INTO apps (name, source_type, created_at) VALUES ('Old', 'upload', 0)")
        conn.commit()
        conn.close()
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        conn.commit()
        row = dict(conn.execute("SELECT * FROM apps").fetchone())
        self.assertEqual((row["name"], row["auto_update"], row["fail_count"]), ("Old", 1, 0))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM renewals").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT name FROM devices").fetchone()[0], "TV")
        conn.close()


if __name__ == "__main__":
    unittest.main()
