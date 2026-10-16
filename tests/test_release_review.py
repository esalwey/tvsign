"""Relecture finale de la 1.1 : appairage USB, « Tout renouveler » une fois, blocages de version, canal et version
épinglée, restauration deux fois dans la seconde, notifications (langue, appareil), erreurs qui demandent une action,
appairage repris du store, contrôle des apps installées, plateformes à la copie et au remplacement, migration atomique.

    python -m unittest discover -s tests

Sans plumesign, sans Apple TV, sans Internet.
"""
import asyncio
import json
import os
import plistlib
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

DATA = os.environ.get("TVSIGN_DATA") or tempfile.mkdtemp(prefix="tvsign-test-")
os.environ.setdefault("TVSIGN_DATA", DATA)
os.environ.setdefault("TVSIGN_AUTH", "none")
os.environ.setdefault("TVSIGN_PLUMESIGN", str(Path(DATA) / "absent"))
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import backup, db, devices, ipa, main, notifier, plume, reasons, renew, settings  # noqa: E402
from app.config import PAIR_HOST, PLUME_HOME  # noqa: E402
from app.proc import Result, Session  # noqa: E402
from test_devices import Calls, make_app, make_device  # noqa: E402
from test_hotfix import NEVER_SIGNED_IN, NOT_WINDOWS, SMS_403  # noqa: E402
from test_ios import HEADERS, add_device, make_ipa  # noqa: E402
from test_updates import make_ipa as make_tv_ipa, sha  # noqa: E402
from test_web import i18n  # noqa: E402

STORE = PLUME_HOME / "PlumeImpactor"
HOUR = 3600


def setUpModule():
    db.init()


def clean_tables():
    db.execute("DELETE FROM apps")
    db.execute("DELETE FROM devices")


class Language:
    """Langue des notifications le temps d'un test (la base est partagée avec les autres tests)."""

    def __init__(self, lang):
        self.lang = lang

    def __enter__(self):
        self.saved = settings.raw_get("language")
        settings.raw_set("language", self.lang)
        return self

    def __exit__(self, *exc):
        settings.raw_set("language", self.saved)


# ---------- appairage USB : la session finit « finishing » puis « done » ----------

class UsbPairingSession(unittest.TestCase):
    """Une vraie Session, avec un faux plumesign (un script Python) : le test d'unité de _usb_pairing_done construisait
    la session à la main, avec l'état « done » que la vraie ne porte qu'après la fin du travail."""

    UDID = "00008110-AAAA0000BBBB9001"

    def setUp(self):
        db.init()
        self.refreshed = []

        async def refresh(device_id, wait=True):
            self.refreshed.append(device_id)
            return {}
        p = mock.patch.object(devices, "refresh_info", refresh)
        p.start()
        self.addCleanup(p.stop)

    def run_session(self, code, cancel_after=None):
        async def go():
            s = Session("pair", re.compile(r"^\0$"))
            await s.start(sys.executable, "-c", code, on_exit=devices._usb_pairing_done)
            if cancel_after is not None:
                await asyncio.sleep(cancel_after)
                s.cancel()
            await s._task
            return s
        return asyncio.run(go())

    def identity(self, **over):
        info = {"DeviceFamily": "ipados", "DeviceName": "iPad du test", "ProductType": "iPad13,4",
                "ProductVersion": "18.1", "UniqueDeviceID": self.UDID}
        info.update(over)
        return json.dumps(dict(sorted(info.items())), separators=(",", ":"))

    def test_the_device_is_saved_and_the_session_ends_done(self):
        db.execute("DELETE FROM devices WHERE pairing_udid = ?", self.UDID)
        s = self.run_session(f"print({self.identity()!r}); print('PAIRED - {self.UDID}')")
        self.assertEqual(s.state, "done")
        row = db.row("SELECT * FROM devices WHERE pairing_udid = ?", self.UDID)
        self.assertIsNotNone(row, s.lines)
        self.assertEqual((row["name"], row["family"], row["model"], row["pair_host"]),
                         ("iPad du test", "ipados", "iPad13,4", PAIR_HOST))
        self.assertEqual(s.result["device_id"], row["id"])
        self.assertEqual(self.refreshed, [row["id"]])
        paired = db.row("SELECT message FROM events ORDER BY id DESC LIMIT 1")["message"]
        self.assertIn("appairé en USB", paired)             # et pas « Appairage USB échoué »

    def test_a_failed_pairing_saves_nothing_and_says_why(self):
        before = db.row("SELECT COUNT(*) AS n FROM devices")["n"]
        s = self.run_session("import sys; print('usbmuxd is not reachable (x)'); sys.exit(1)")
        self.assertEqual(s.state, "failed")
        self.assertEqual(db.row("SELECT COUNT(*) AS n FROM devices")["n"], before)
        last = db.row("SELECT message, level FROM events ORDER BY id DESC LIMIT 1")
        self.assertEqual(last["level"], "error")
        self.assertIn("usbmuxd is not reachable", last["message"])

    def test_a_clean_exit_without_the_paired_line_is_not_a_success(self):
        s = self.run_session("print('rien')")
        self.assertEqual(s.state, "failed")
        self.assertNotIn("device_id", s.result)

    def test_cancelling_is_logged_as_a_choice_not_a_failure(self):
        s = self.run_session("import time; time.sleep(30)", cancel_after=0.5)
        self.assertEqual(s.state, "failed")
        last = db.row("SELECT message, level FROM events ORDER BY id DESC LIMIT 1")
        self.assertEqual((last["level"], last["message"]), ("info", "Appairage USB annulé"))


# ---------- « Tout renouveler » ----------

class RenewAllOnce(unittest.TestCase):
    def test_a_second_call_while_the_first_runs_queues_nothing(self):
        clean_tables()
        dev = make_device("Salon")
        # loin de leur échéance : la boucle de renouvellement du serveur de test n'y touche pas
        ids = [make_app(dev, name=f"App{i}", bundle_id=f"com.example.n{i}", expires_at=time.time() + (100 + i) * HOUR)
               for i in range(3)]
        calls = []

        async def slow(app_id, trigger="manuel"):
            calls.append((app_id, trigger))
            await asyncio.sleep(0.3)
        with mock.patch.object(renew, "renew", slow), mock.patch.object(renew, "STOPPING", False),                 TestClient(main.app) as client:
            first = client.post("/api/apps/renew-all", headers=HEADERS).json()
            second = client.post("/api/apps/renew-all", headers=HEADERS).json()
            deadline = time.time() + 10
            while main._RENEW_ALL_PENDING and time.time() < deadline:
                time.sleep(0.05)
        self.assertEqual((first["started"], second["started"]), (3, 0))
        self.assertEqual(sorted(calls), sorted((i, "manuel") for i in ids))      # une signature par app, pas deux
        self.assertFalse(main._RENEW_ALL_PENDING)

    def test_once_the_chain_is_done_a_new_call_starts_again(self):
        clean_tables()
        dev = make_device("Salon")
        make_app(dev, name="Solo")
        calls = []

        async def quick(app_id, trigger="manuel"):
            calls.append(app_id)
        with mock.patch.object(renew, "renew", quick), mock.patch.object(renew, "STOPPING", False),                 TestClient(main.app) as client:
            for _ in range(2):
                self.assertEqual(client.post("/api/apps/renew-all", headers=HEADERS).json()["started"], 1)
                deadline = time.time() + 10
                while main._RENEW_ALL_PENDING and time.time() < deadline:
                    time.sleep(0.05)
        self.assertEqual(len(calls), 2)


# ---------- « Installer la mise à jour maintenant » après une mise à jour de la TV ----------

class OsBlockAfterAnUpgrade(unittest.TestCase):
    def setUp(self):
        db.init()
        self.dev = make_device("Salon", os_version="26.1")
        self.app_id = make_app(self.dev, name="Blocked", source_type="github", source="o/r", source_tag="v1",
                               bad_tag="v2", bad_key="k2", update_tag="v2", update_block="tvos:27.0")
        renew._OS_REFRESHED[self.dev] = time.time()         # relue il y a un instant : le délai de 6 h joue
        self.reads = []

        async def refresh(device_id, wait=True):
            self.reads.append(device_id)
            db.execute("UPDATE devices SET os_version = '27.1' WHERE id = ?", device_id)
            return {}
        p = mock.patch.object(devices, "refresh_info", refresh)
        p.start()
        self.addCleanup(p.stop)

    def row(self):
        return db.row("SELECT * FROM apps WHERE id = ?", self.app_id)

    def test_the_periodic_check_does_not_reread_the_tv_every_time(self):
        asyncio.run(renew._lift_os_block(self.row()))
        self.assertEqual(self.reads, [])
        self.assertEqual(self.row()["update_block"], "tvos:27.0")

    def test_an_explicit_request_reads_the_tv_now(self):
        asyncio.run(renew._lift_os_block(self.row(), fresh=True))
        self.assertEqual(self.reads, [self.dev])
        row = self.row()
        self.assertEqual((row["bad_tag"], row["update_tag"], row["update_block"]), (None, None, None))

    def test_refresh_source_with_force_gets_the_fresh_system_version(self):
        (renew.app_dir(self.app_id) / "original.ipa").write_bytes(make_tv_ipa(version="1.0"))
        seen = {}

        def download(url, dest, sha256=None, expect_bundle=None, **kw):
            seen.update(kw)
            data = make_tv_ipa(version="2.0")
            dest.write_bytes(data)
            return {"version": "2.0", "bundle_id": "com.example.tv", "sha256": sha(data)}
        found = {"tag": "v2", "url": "https://x/v2", "sha256": None, "key": "k2", "notes": None}
        with mock.patch.object(renew, "_latest", return_value=found), mock.patch.object(ipa, "download", download):
            self.assertTrue(asyncio.run(renew.refresh_source(self.row(), force=True)))
        self.assertEqual(seen["max_os"], "27.1")                # plus « 26.1 » : la version relue
        self.assertEqual(self.row()["source_tag"], "v2")


# ---------- changement de canal : le tag installé reste connu ----------

class ChannelSwitch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        db.init()
        p = mock.patch.object(renew, "spawn", lambda app_id, trigger="manuel": True)
        p.start()
        self.addCleanup(p.stop)
        self.app_id = db.execute(
            "INSERT INTO apps (name, source_type, source, source_tag, source_key, installed_tag, channel, "
            "source_published_at, version, bundle_id, created_at) "
            "VALUES ('Nuvio', 'github', 'o/r', 'v1.0', 'k1', 'v1.0', 'stable', '2026-01-01T00:00:00Z', '1.0', "
            "'com.example.tv', 0)")

    def row(self):
        return db.row("SELECT * FROM apps WHERE id = ?", self.app_id)

    def patch(self, **body):
        r = self.client.patch(f"/api/apps/{self.app_id}", headers=HEADERS, json=body)
        self.assertEqual(r.status_code, 200, r.text)

    def test_switching_channel_keeps_the_installed_tag(self):
        self.patch(channel="beta")
        row = self.row()
        self.assertEqual((row["source_tag"], row["installed_tag"], row["channel"]), ("v1.0", "v1.0", "beta"))
        self.assertEqual((row["source_key"], row["source_published_at"]), (None, None))   # le choix du canal n'est pas « plus ancien »

    def test_changing_the_tag_prefix_keeps_it_too(self):
        self.patch(source_filter="tvos-")
        self.assertEqual(self.row()["source_tag"], "v1.0")

    def test_another_repository_forgets_the_tag(self):
        self.patch(source="o/other")
        self.assertIsNone(self.row()["source_tag"])

    def test_the_previous_version_remembers_the_tag_after_a_switch_from_an_older_database(self):
        """Une fiche dont le canal a déjà été changé (source_tag vidé) : installed_tag dit ce qui est gardé."""
        db.update("apps", self.app_id, source_tag=None, installed_ipa="x")
        (renew.app_dir(self.app_id) / "original.ipa").write_bytes(make_tv_ipa(version="1.0"))
        found = {"tag": "v2.0", "url": "https://x/v2", "sha256": None, "key": "k2", "notes": None}

        def download(url, dest, sha256=None, expect_bundle=None, **kw):
            data = make_tv_ipa(version="2.0")
            dest.write_bytes(data)
            return {"version": "2.0", "bundle_id": "com.example.tv", "sha256": sha(data)}
        with mock.patch.object(renew, "_latest", return_value=found), mock.patch.object(ipa, "download", download):
            self.assertTrue(asyncio.run(renew.refresh_source(self.row())))
        row = self.row()
        self.assertEqual((row["prev_tag"], row["prev_version"], row["source_tag"]), ("v1.0", "1.0", "v2.0"))


# ---------- version épinglée introuvable ----------

class PinnedTagMissing(unittest.TestCase):
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
        p = mock.patch.object(renew, "spawn", lambda app_id, trigger="manuel": self.spawned.append(trigger) or True)
        p.start()
        self.addCleanup(p.stop)
        self.dev = make_device("Salon")
        self.app_id = make_app(self.dev, name="Pinned", source_type="github", source="o/r", source_tag="v1", source_key="k1",
                               installed_tag="v1", status="ok")
        (renew.app_dir(self.app_id) / "original.ipa").write_bytes(make_tv_ipa(version="1.0"))
        renew._NO_MATCH_LOGGED.discard(self.app_id)

    def row(self):
        return db.row("SELECT * FROM apps WHERE id = ?", self.app_id)

    def test_pinning_an_unknown_tag_is_shown_on_the_card_and_in_the_log(self):
        r = self.client.patch(f"/api/apps/{self.app_id}", headers=HEADERS, json={"pinned_tag": "no-such-tag"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.spawned, ["mise à jour"])
        with mock.patch.object(renew, "_latest", return_value=None):
            self.assertFalse(asyncio.run(renew.refresh_source(self.row(), force=True)))
        row = self.row()
        self.assertEqual((row["update_block"], row["update_tag"]), ("pin_missing", None))
        self.assertEqual(row["source_tag"], "v1")                   # l'app installée reste telle quelle
        logs = [e["message"] for e in db.rows("SELECT message FROM events WHERE app_id = ?", self.app_id)]
        self.assertTrue(any("épinglée « no-such-tag »" in m and "introuvable" in m for m in logs), logs)

    def test_the_periodic_search_keeps_the_note_and_a_renewal_does_not_say_up_to_date(self):
        db.update("apps", self.app_id, pinned_tag="no-such-tag")
        with mock.patch.object(renew, "_latest", return_value=None):
            self.assertFalse(asyncio.run(renew.new_release(self.row())))
        self.assertEqual(self.row()["update_block"], "pin_missing")

    def test_clearing_or_fixing_the_pin_removes_the_note(self):
        db.update("apps", self.app_id, pinned_tag="no-such-tag", update_block="pin_missing")
        self.client.patch(f"/api/apps/{self.app_id}", headers=HEADERS, json={"pinned_tag": ""})
        row = self.row()
        self.assertEqual((row["pinned_tag"], row["update_block"]), (None, None))

    def test_a_release_that_exists_again_clears_the_note(self):
        db.update("apps", self.app_id, pinned_tag="v1", update_block="pin_missing")
        found = {"tag": "v1", "url": "https://x/v1", "sha256": None, "key": "k1", "notes": None}
        with mock.patch.object(renew, "_latest", return_value=found):
            asyncio.run(renew.refresh_source(self.row()))
        self.assertIsNone(self.row()["update_block"])

    def test_the_log_line_comes_once_per_episode(self):
        db.update("apps", self.app_id, pinned_tag="nope")
        with mock.patch.object(renew, "_latest", return_value=None):
            for _ in range(3):
                asyncio.run(renew.refresh_source(self.row(), force=True))
        logs = [e["message"] for e in db.rows("SELECT message FROM events WHERE app_id = ?", self.app_id)]
        self.assertEqual(len([m for m in logs if "épinglée" in m]), 1)


# ---------- restauration : deux fois dans la même seconde, et la cause au journal ----------

class RestoreTwiceInASecond(unittest.TestCase):
    def setUp(self):
        db.init()
        STORE.mkdir(parents=True, exist_ok=True)
        (STORE / "accounts.json").write_text("{}")
        self.archive = backup.export()
        self.addCleanup(self.archive.unlink, missing_ok=True)

    def test_two_restores_with_the_same_clock_second_both_succeed(self):
        fixed = int(time.time()) + 1000          # une seconde jamais utilisée par un autre test
        (STORE / "marker").write_text("first")
        with mock.patch.object(backup.time, "time", return_value=float(fixed)):
            backup.restore(self.archive)
            (STORE / "marker").write_text("second")
            backup.restore(self.archive)         # « PlumeImpactor.pre-restore-<fixed> » existe déjà
        first, second = (PLUME_HOME / f"PlumeImpactor.pre-restore-{fixed + n}" for n in (0, 1))
        self.assertEqual(((first / "marker").read_text(), (second / "marker").read_text()), ("first", "second"))
        self.assertTrue(Path(str(backup.DB_PATH.parent / f"tvsign.db.pre-restore-{fixed + 1}")).exists())

    def test_a_leftover_copy_of_the_same_second_does_not_block_a_restore(self):
        fixed = int(time.time()) + 2000
        leftover = PLUME_HOME / f"PlumeImpactor.pre-restore-{fixed}"
        leftover.mkdir(parents=True, exist_ok=True)
        (leftover / "old").write_text("x")
        self.addCleanup(shutil.rmtree, leftover, ignore_errors=True)
        with mock.patch.object(backup.time, "time", return_value=float(fixed)):
            self.assertEqual(backup._aside_stamp(), fixed + 1)

    def test_a_failed_restore_puts_its_cause_in_the_log(self):
        real = backup.replace_retry

        def failing(src, dst, *a, **k):
            if Path(dst) == backup.DB_PATH:
                raise PermissionError("verrouillé par un antivirus")
            return real(src, dst, *a, **k)
        with mock.patch.object(backup, "replace_retry", failing), TestClient(main.app) as client:
            r = client.post("/api/backup/import", headers=HEADERS, files={"file": ("b.tvsign-backup", self.archive.read_bytes())})
        self.assertEqual((r.status_code, r.json()["detail"]), (500, "restore_failed"))
        last = db.row("SELECT message, level FROM events WHERE message LIKE 'Restauration refusée%' ORDER BY id DESC LIMIT 1")
        self.assertEqual(last["level"], "error")
        self.assertIn("restore_failed", last["message"])
        self.assertIn("antivirus", last["message"])


# ---------- notifications : langue de la raison, appareil, type invalide ----------

class NotificationText(unittest.TestCase):
    UNREACHABLE = "Apple TV « Salon » introuvable sur le réseau (éteinte ou en veille ?) [ERROR NOT_FOUND]"

    def setUp(self):
        db.init()
        clean_tables()
        self.sent = []

        async def notify(message, priority="default", event="fail", **extra):
            self.sent.append((message, priority, event, extra))
            return True
        p = mock.patch.object(renew, "notify", notify)
        p.start()
        self.addCleanup(p.stop)

    def test_the_reason_is_said_in_the_language_of_the_message(self):
        with Language("en"):
            self.assertEqual(notifier.reason(self.UNREACHABLE), "Device unreachable")
            self.assertEqual(notifier.reason("renouvellement trop long, abandonné"), "Timed out")
            self.assertEqual(notifier.reason("quelque chose d'inconnu"), "quelque chose d'inconnu")      # jamais deviné
        with Language("fr"):
            self.assertEqual(notifier.reason(self.UNREACHABLE), self.UNREACHABLE)
        self.assertEqual(notifier.reason(RuntimeError("signature : CERT_LIMIT: x"), "en"),
                         "Apple account full: a certificate has to be freed")

    def alert(self, lang, devices_count=2):
        dev = make_device("Salon")
        for i in range(devices_count - 1):
            make_device(f"Chambre{i}")
        app_id = make_app(dev, name="App One", expires_at=time.time() + 10 * HOUR, status="error",
                          notified_level=None)
        app = db.row("SELECT * FROM apps WHERE id = ?", app_id)
        with Language(lang):
            asyncio.run(renew.alert(app, self.UNREACHABLE, time.time()))
        return self.sent[-1]

    def test_an_english_message_has_an_english_reason_and_names_the_tv(self):
        message, priority, event, extra = self.alert("en")
        self.assertEqual(message, "App One (Salon) expires in less than 24 h and renewal is failing: Device unreachable")
        self.assertEqual((priority, event), ("high", "expiry"))
        self.assertEqual((extra["app"], extra["device"]), ("App One", "Salon"))     # le webhook garde les deux séparés

    def test_a_french_message_keeps_the_french_reason(self):
        message, *_ = self.alert("fr")
        self.assertIn("introuvable sur le réseau", message)
        self.assertIn("App One (Salon)", message)

    def test_with_one_device_the_name_stays_short(self):
        message, *_ = self.alert("en", devices_count=1)
        self.assertTrue(message.startswith("App One expires"), message)

    def test_the_account_alert_speaks_english_in_english(self):
        plume.ALERTED.discard("a@b.c")
        with Language("en"):
            asyncio.run(renew.account_down("a@b.c", plume.REAUTH_REASON))
        message = self.sent[-1][0]
        self.assertIn("two-factor", message)
        self.assertNotIn("deux facteurs", message)
        plume.ALERTED.discard("a@b.c")
        with Language("fr"):
            asyncio.run(renew.account_down("a@b.c", plume.REAUTH_REASON))
        self.assertIn("deux facteurs", self.sent[-1][0])
        plume.ALERTED.discard("a@b.c")

    def test_a_destination_type_that_is_not_a_string_is_refused_not_a_500(self):
        with TestClient(main.app) as client:
            for kind in (["ntfy"], {"a": 1}, 5, None):
                for path in ("/api/notifications", "/api/notifications/test"):
                    r = client.post(path, headers=HEADERS, json={"type": kind, "url": "https://ntfy.sh/x"})
                    self.assertEqual((r.status_code, r.json()["detail"]), (400, "notify_type_invalid"), (path, kind))


class ReasonsFollowTheInterface(unittest.TestCase):
    def test_the_generated_module_is_up_to_date(self):
        import subprocess
        if not __import__("shutil").which("node"):
            self.skipTest("Node.js absent")
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "sync-reasons.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "app/reasons.py n'est plus à jour : python scripts/sync-reasons.py")

    def test_every_known_cause_has_an_english_title(self):
        self.assertGreaterEqual(len(reasons.CAUSES), 50)
        for rx, key, title in reasons.CAUSES:
            self.assertTrue(key.startswith("c_") and title.strip(), key)


# ---------- erreurs qui demandent une action : pas de nouvel essai automatique ----------

class ActionsForTheUser(unittest.TestCase):
    def fail(self, output):
        return plume._failure("classify@example.com", "signature", Result(1, output))

    def tearDown(self):
        db.execute("DELETE FROM account_status WHERE email = 'classify@example.com'")

    def test_sms_never_signed_in_and_non_windows_servers_need_the_user(self):
        for output in (SMS_403, NEVER_SIGNED_IN, NOT_WINDOWS):
            e = self.fail(output)
            self.assertIs(type(e), RuntimeError, output)          # même classe et même texte qu'en 1.0.1
            self.assertEqual(renew.failure_kind(e), "config", output)
            self.assertFalse(renew.ipa_fault(e))                  # jamais la faute de la nouvelle version de l'app

    def test_they_wait_for_the_user_not_for_the_ordinary_backoff(self):
        now = 1_000_000.0
        app = {"expires_at": None, "fail_count": 3, "fail_kind": "other"}
        fields = renew.failure_fields(app, now, self.fail(SMS_403), False)
        self.assertEqual((fields["needs_action"], fields["fail_kind"], fields["fail_count"]), ("config", "config", 1))
        self.assertEqual(fields["next_attempt_at"] - now, renew.BLOCKED_RETRY_HOURS["config"] * 3600)

    def test_a_full_account_waits_for_the_setting_not_for_the_backoff(self):
        e = RuntimeError("signature : CERT_LIMIT: the Apple account holds the maximum number of development certificates")
        self.assertEqual(renew.failure_kind(e), "config")
        self.assertIsNone(renew.failure_kind(RuntimeError("signature : boom")))

    def test_changing_the_setting_restarts_the_apps_it_blocked(self):
        clean_tables()
        dev = make_device("Salon")
        blocked = make_app(dev, name="Full", status="error", needs_action="config", fail_kind="config",
                           next_attempt_at=time.time() + 5 * HOUR, last_error="signature : CERT_LIMIT: full")
        other = make_app(dev, name="Other", status="error", needs_action="config", fail_kind="config",
                         next_attempt_at=time.time() + 5 * HOUR, last_error="NO_TEAM: nothing")
        with Calls() as calls:
            self.assertEqual(renew.certificate_policy_changed(), 1)
        self.assertEqual(calls, [])                                # pas de boucle d'événements ici : l'état seul est remis à zéro
        self.assertIsNone(db.row("SELECT next_attempt_at FROM apps WHERE id = ?", blocked)["next_attempt_at"])
        self.assertIsNotNone(db.row("SELECT next_attempt_at FROM apps WHERE id = ?", other)["next_attempt_at"])

    def test_the_settings_route_does_it(self):
        clean_tables()
        dev = make_device("Salon")
        blocked = make_app(dev, name="Full", status="error", needs_action="config", fail_kind="config",
                           next_attempt_at=time.time() + 5 * HOUR, last_error="signature : CERT_LIMIT: full")
        saved = settings.raw_get("cert_revoke")
        try:
            with TestClient(main.app) as client:
                r = client.patch("/api/settings", headers=HEADERS, json={"cert_revoke": "any"})
            self.assertEqual(r.status_code, 200, r.text)
        finally:
            settings.raw_set("cert_revoke", saved)
        self.assertIsNone(db.row("SELECT next_attempt_at FROM apps WHERE id = ?", blocked)["next_attempt_at"])


class PausedApps(unittest.TestCase):
    def test_a_paused_app_raises_no_alert_whatever_its_state(self):
        now = time.time()
        app = dict(enabled=0, expires_at=now - 100, status="error", fail_kind="other", needs_action=None)
        self.assertIsNone(renew.alert_state(app, now))
        self.assertEqual(renew.alert_state({**app, "enabled": 1}, now), "expired")
        self.assertEqual(renew.alert_state({k: v for k, v in app.items() if k != "enabled"}, now), "expired")


# ---------- appairage repris du store ----------

class StoreAdoption(unittest.TestCase):
    def setUp(self):
        db.init()
        clean_tables()
        (STORE / "remotepairing").mkdir(parents=True, exist_ok=True)
        self.written = []
        self.addCleanup(lambda: [p.unlink(missing_ok=True) for p in self.written])

    def pairing(self, udid, host):
        path = STORE / "remotepairing" / f"{udid}.plist"
        ident = str(uuid.uuid3(uuid.NAMESPACE_DNS, host)).upper() if host else "NOT-A-HOST"
        path.write_bytes(plistlib.dumps({"identifier": ident, "public_key": b"", "private_key": b""}))
        self.written.append(path)

    def sync(self, records):
        async def listing():
            return records
        with mock.patch.object(devices, "stored_pairings", listing):
            asyncio.run(devices.sync_store())

    def test_the_host_name_is_recovered_from_the_pairing_file(self):
        self.pairing("UDID-OWN-0001", PAIR_HOST)
        self.pairing("UDID-OLD-0002", "tvsign")
        self.pairing("UDID-CLI-0003", "plume")
        self.pairing("UDID-FOREIGN-4", "Impactor")
        self.pairing("UDID-NONE-0005", None)
        records = [{"name": n, "model": "AppleTV11,1", "udid": u} for n, u in
                   (("Own", "UDID-OWN-0001"), ("Old", "UDID-OLD-0002"), ("Cli", "UDID-CLI-0003"),
                    ("Foreign", "UDID-FOREIGN-4"), ("None", "UDID-NONE-0005"))]
        self.sync(records)
        hosts = {r["name"]: r["pair_host"] for r in db.rows("SELECT name, pair_host FROM devices")}
        self.assertEqual(hosts, {"Own": PAIR_HOST, "Old": "tvsign", "Cli": "plume", "Foreign": "plume", "None": "plume"})

    def test_a_known_tv_without_pairing_adopts_it_with_the_right_host(self):
        self.pairing("UDID-KNOWN-0006", PAIR_HOST)
        dev = make_device("Salon", udid="UDID-KNOWN-0006", pairing_udid=None, pair_host=None, pair_identifier="pid-known-6")
        self.sync([{"name": "Salon", "model": "AppleTV11,1", "udid": "UDID-KNOWN-0006"}])
        row = db.row("SELECT pairing_udid, pair_host FROM devices WHERE id = ?", dev)
        self.assertEqual((row["pairing_udid"], row["pair_host"]), ("UDID-KNOWN-0006", PAIR_HOST))


# ---------- contrôle des apps installées ----------

class InstalledAudit(unittest.TestCase):
    def setUp(self):
        db.init()
        clean_tables()

    def audit(self, listing):
        dev = make_device("Audit")
        app_id = make_app(dev)
        devices._AUDIT_NEXT.pop(dev, None)

        async def fake(device, bundle_id=None, wait=True, **k):
            return listing
        with Calls() as calls, mock.patch.object(devices, "installed_apps", fake):
            asyncio.run(devices.audit_installed())
        return dev, app_id, calls

    def test_an_empty_listing_proves_nothing_and_reinstalls_nothing(self):
        for listing in ([], [{"foo": 1}], [None, "x"]):
            dev, app_id, calls = self.audit(listing)
            self.assertEqual(calls, [], listing)
            self.assertEqual(db.row("SELECT status FROM apps WHERE id = ?", app_id)["status"], "ok")
            self.assertGreater(devices._AUDIT_NEXT[dev], time.time())        # revue dans une heure
            self.assertLess(devices._AUDIT_NEXT[dev], time.time() + 2 * HOUR)

    def test_a_real_listing_without_the_app_still_reinstalls_it(self):
        dev, app_id, calls = self.audit([{"bundle_id": "com.other"}])
        self.assertEqual(calls, [(app_id, "app absente")])

    def test_confirm_installed_does_not_condemn_an_install_on_an_empty_listing(self):
        async def fast(_):
            return None
        answers = [[], []]

        async def fake(device, bundle_id=None, wait=True, **k):
            return answers.pop(0)
        with mock.patch.object(devices, "installed_apps", fake), mock.patch.object(devices.asyncio, "sleep", fast):
            self.assertIsNone(asyncio.run(devices.confirm_installed({"id": 1, "name": "Salon"}, "com.x")))
        answers[:] = [[{"bundle_id": "com.other"}], []]
        with mock.patch.object(devices, "installed_apps", fake), mock.patch.object(devices.asyncio, "sleep", fast):
            with self.assertRaises(devices.NotInstalled):
                asyncio.run(devices.confirm_installed({"id": 1, "name": "Salon"}, "com.x"))


# ---------- adresses saisies, import d'un fichier d'appairage ----------

class Addresses(unittest.TestCase):
    def test_a_failed_ip_address_is_not_taken_for_a_host_name(self):
        for bad in ("999.1.1.1", "1.2.3", "192.168.1.256", "1.2.3.4.5", "10.0.0"):
            with self.assertRaises(ValueError, msg=bad):
                devices.clean_host(bad)
        for good in ("192.168.1.20", "fe80::1%12", "tv.local", "apple-tv", "salon.example.com"):
            self.assertEqual(devices.clean_host(good), good)

    def test_the_routes_answer_400_and_store_nothing(self):
        dev = make_device("Salon", host_hint=None)
        with TestClient(main.app) as client:
            r = client.post("/api/devices/discover", headers=HEADERS, json={"host": "999.1.1.1"})
            self.assertEqual((r.status_code, r.json()["detail"]), (400, "invalid_host"))
            r = client.post(f"/api/devices/{dev}/relocate", headers=HEADERS, json={"host": "999.1.1.1"})
            self.assertEqual((r.status_code, r.json()["detail"]), (400, "invalid_host"))
            r = client.patch(f"/api/devices/{dev}", headers=HEADERS, json={"host": "999.1.1.1"})
            self.assertEqual((r.status_code, r.json()["detail"]), (400, "invalid_host"))
        self.assertIsNone(db.row("SELECT host_hint FROM devices WHERE id = ?", dev)["host_hint"])

    def test_importing_a_pairing_file_without_plumesign_is_a_502_with_a_cause(self):
        async def boom(*a, **k):
            raise FileNotFoundError(2, "No such file or directory", "plumesign")
        with mock.patch.object(devices, "run", boom), TestClient(main.app) as client:
            r = client.post("/api/devices/import-pairing", headers=HEADERS, data={"udid": "00008110-AAAA0000BBBB7001"},
                            files={"file": ("p.plist", b"<plist/>", "application/octet-stream")})
        self.assertEqual(r.status_code, 502, r.text)
        self.assertIn("plumesign introuvable", r.json()["detail"])
        self.assertIsNone(db.row("SELECT id FROM devices WHERE pairing_udid = '00008110-AAAA0000BBBB7001'"))


# ---------- remplacement et copie d'une app : la plateforme de l'appareil décide ----------

class PlatformOnReplaceAndCopy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        db.init()
        clean_tables()
        p = mock.patch.object(renew, "spawn", lambda app_id, trigger="manuel": True)
        p.start()
        self.addCleanup(p.stop)
        self.tv = add_device("Salon", "tvos", "AppleTV11,1", "26.0")
        self.phone = add_device("iPhone", "ios", "iPhone15,2", "17.5")

    def add(self, device_id, content, name="a.ipa"):
        r = self.client.post("/api/apps", headers=HEADERS, data={"source_type": "upload", "device_id": str(device_id)},
                             files={"file": (name, content, "application/octet-stream")})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def replace(self, app_id, content):
        return self.client.post(f"/api/apps/{app_id}/ipa", headers=HEADERS,
                                files={"file": ("b.ipa", content, "application/octet-stream")})

    def test_the_ipa_of_an_app_on_an_iphone_can_be_replaced_and_its_columns_follow(self):
        app_id = self.add(self.phone, make_ipa("iphoneos", "com.example.phoneapp", ui_family=[1, 2]))
        r = self.replace(app_id, make_ipa("iphoneos", "com.example.phoneapp", ui_family=[1], appex=1))
        self.assertEqual(r.status_code, 200, r.text)
        row = db.row("SELECT * FROM apps WHERE id = ?", app_id)
        self.assertEqual((row["platform"], row["ui_family"], row["warnings"]), ("iphoneos", "1", "appex:1"))

    def test_a_tvos_ipa_is_refused_for_an_app_on_an_iphone_and_the_other_way_round(self):
        phone_app = self.add(self.phone, make_ipa("iphoneos", "com.example.phoneapp"))
        r = self.replace(phone_app, make_ipa("appletvos", "com.example.phoneapp"))
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "not_ios:appletvos"))
        tv_app = self.add(self.tv, make_tv_ipa(bundle="com.example.tvapp"))
        r = self.replace(tv_app, make_ipa("iphoneos", "com.example.tvapp"))
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "not_tvos:iphoneos"))

    def test_copying_to_another_family_is_refused(self):
        tv_app = self.add(self.tv, make_tv_ipa(bundle="com.example.tvapp"))
        r = self.client.post(f"/api/apps/{tv_app}/copy", headers=HEADERS, json={"device_id": self.phone})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "not_ios:appletvos"))
        self.assertEqual(db.row("SELECT COUNT(*) AS n FROM apps WHERE device_id = ?", self.phone)["n"], 0)

    def test_a_copy_keeps_the_columns_of_the_ipa(self):
        pad = add_device("iPad", "ipados", "iPad13,4", "17.5")
        phone_app = self.add(self.phone, make_ipa("iphoneos", "com.example.phoneapp", ui_family=[1, 2], appex=2))
        r = self.client.post(f"/api/apps/{phone_app}/copy", headers=HEADERS, json={"device_id": pad})
        self.assertEqual(r.status_code, 200, r.text)
        copy = db.row("SELECT * FROM apps WHERE id = ?", r.json()["id"])
        self.assertEqual((copy["platform"], copy["ui_family"], copy["warnings"]), ("iphoneos", "1,2", "appex:2"))
        # et la fiche copiée est contrôlée à son tour quand on la déplace
        r = self.client.patch(f"/api/apps/{copy['id']}", headers=HEADERS, json={"device_id": self.tv})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "not_tvos:iphoneos"))


# ---------- base de données : migration atomique, ligne de journal unique ----------

class MigrationIsAtomic(unittest.TestCase):
    OLD = """
    CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT NOT NULL, pair_identifier TEXT NOT NULL UNIQUE,
        udid TEXT, model TEXT, os_version TEXT, last_ip TEXT, last_port INTEGER, last_seen REAL, created_at REAL NOT NULL);
    CREATE TABLE apps (id INTEGER PRIMARY KEY, name TEXT NOT NULL, bundle_id TEXT, version TEXT, source_type TEXT NOT NULL,
        source TEXT, source_filter TEXT, source_tag TEXT, account_email TEXT, device_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending', last_error TEXT, signed_at REAL, expires_at REAL, next_attempt_at REAL,
        created_at REAL NOT NULL);
    INSERT INTO apps (id, name, source_type, created_at) VALUES (1, 'Nuvio', 'github', 1);
    """

    def old_database(self):
        path = Path(tempfile.mkdtemp(dir=DATA)) / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(self.OLD)
        conn.commit()
        conn.close()
        return path

    def test_a_crash_between_the_columns_and_the_backfill_leaves_the_database_as_it_was(self):
        path = self.old_database()
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        real = conn.execute

        class Dying:
            """La connexion, qui « meurt » au remplissage de la plateforme (après avoir ajouté toutes les colonnes)."""

            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *args):
                if sql.startswith("UPDATE apps SET platform"):
                    raise sqlite3.OperationalError("disk I/O error")
                return real(sql, *args)
        with self.assertRaises(sqlite3.OperationalError):
            db.migrate(Dying())
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(apps)")}
        self.assertNotIn("platform", cols)                       # aucune colonne à moitié ajoutée
        db.migrate(conn)                                           # le démarrage suivant refait tout
        conn.commit()
        self.assertEqual(conn.execute("SELECT platform FROM apps WHERE id = 1").fetchone()[0], "appletvos")
        conn.close()

    def test_a_later_null_platform_is_still_not_rewritten(self):
        path = self.old_database()
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        conn.execute("INSERT INTO apps (name, source_type, created_at) VALUES ('Phone', 'upload', 2)")
        db.migrate(conn)
        conn.commit()
        self.assertIsNone(conn.execute("SELECT platform FROM apps WHERE name = 'Phone'").fetchone()[0])
        conn.close()


class LegacyAnisetteIsMentionedOnce(unittest.TestCase):
    def setUp(self):
        db.init()
        STORE.mkdir(parents=True, exist_ok=True)
        self.old = STORE / "state.plist"
        self.new = STORE / plume.ANISETTE_STATE
        self.old.write_bytes(b"old")
        self.new.unlink(missing_ok=True)
        settings.raw_set(plume._LEGACY_NOTED, None)
        self.logged = []
        p = mock.patch.object(plume.db, "log", lambda message, level="info", **k: self.logged.append(message))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: (self.old.unlink(missing_ok=True), settings.raw_set(plume._LEGACY_NOTED, None)))

    def test_every_start_before_the_first_contact_says_it_only_the_first_time(self):
        self.assertTrue(plume.note_legacy_anisette())
        self.assertTrue(plume.note_legacy_anisette())              # toujours là, mais déjà dit
        self.assertTrue(plume.note_legacy_anisette())
        self.assertEqual(len(self.logged), 1)


# ---------- versions ----------

class VersionStrings(unittest.TestCase):
    def test_the_three_places_that_name_the_version_agree(self):
        config = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
        in_config = re.search(r'os\.environ\.get\("TVSIGN_VERSION", "([^"]+)"\)', config).group(1)
        in_docker = re.search(r"^ARG TVSIGN_VERSION=(\S+)$", (ROOT / "Dockerfile").read_text(encoding="utf-8"), re.M).group(1)
        in_windows = (ROOT / "windows" / "version.txt").read_text(encoding="utf-8").strip()
        self.assertEqual(in_docker, in_config)
        self.assertEqual(in_config, in_windows + "-dev")
        self.assertRegex(in_windows, r"^\d+\.\d+\.\d+$")


# ---------- la documentation parle comme l'interface ----------

@unittest.skipUnless(shutil.which("node"), "Node.js absent")
class DocsMatchTheInterface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = i18n()
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.guide = (ROOT / "windows" / "README-windows.md").read_text(encoding="utf-8")
        cls.en, cls.fr = cls.readme.split("\n## Français", 1)

    def test_the_labels_the_docs_quote_are_the_labels_of_the_interface(self):
        for key, where in (("pair_manual", "readme"), ("sign_out_all", "readme"), ("replace_ipa", "readme"),
                           ("test_connection", "readme"), ("st_not_on_tv", "readme"), ("update_check", "guide")):
            en, fr = self.ui[key][1], self.ui[key][0]
            doc_en, doc_fr = ((self.en, self.fr) if where == "readme"
                              else self.guide.split("\n## Français", 1))
            self.assertIn(en.rstrip("?"), doc_en, key)
            self.assertIn(fr, doc_fr, key)

    def test_no_name_left_over_from_older_wording(self):
        for stale in ("Apple TV section", "section Apple TV", "Settings › New versions", "Réglages › Nouvelles versions",
                      "tvsign-setup-1.0.0.exe", "App Distante", "provider you chose", "fournisseur de votre choix",
                      "Se déconnecter partout", "Test de connexion", "Try *Renew now*", "Essayer *Renouveler",
                      'alt="tvOS 26+"', "From version 1.0.1", "including one issued before this version"):
            for name, text in (("README", self.readme), ("guide", self.guide)):
                self.assertNotIn(stale, text, f"{name}: {stale}")

    def test_the_default_certificate_policy_is_described_as_it_is(self):
        self.assertNotIn("Signing **revokes the certificate", self.readme)
        self.assertNotIn("La signature **révoque le certificat créé par", self.readme)
        self.assertIn("By default tvsign only replaces its own", self.en)
        self.assertIn("Par défaut, tvsign ne remplace que ses propres", self.fr)

    def test_the_readme_opens_with_the_two_ways_in_and_the_smart_app_control_page(self):
        for text, title, anchor in ((self.en, "### Quick start", "windows-blocks-the-installer-smart-app-control-antivirus"),
                                    (self.fr, "### Démarrage rapide", "windows-bloque-linstalleur-smart-app-control-antivirus")):
            self.assertIn(title, text)
            self.assertIn("install.sh --install", text.split(title, 1)[1][:1500])
            self.assertIn(anchor, text.split(title, 1)[1][:1500])
        # et ces ancres existent dans le guide
        self.assertIn("#### Windows blocks the installer (Smart App Control, antivirus)", self.guide)
        self.assertIn("#### Windows bloque l'installeur (Smart App Control, antivirus)", self.guide)

    def test_the_quit_wait_is_the_same_in_the_guide_and_the_installer(self):
        iss = (ROOT / "windows" / "installer" / "tvsign.iss").read_text(encoding="utf-8")
        self.assertRegex(iss, r"en\.QuitWait=.*up to 5 minutes")
        self.assertIn("gets up to 5 minutes to finish first", self.guide)
        self.assertIn("dispose de 5 minutes au plus", self.guide)

    def test_iphone_support_is_called_experimental_in_both_documents(self):
        self.assertIn("treat it as experimental", self.readme)
        self.assertIn("support is **experimental**", self.guide)
        self.assertIn("est **expérimentale**", self.guide)

    def test_what_leaves_the_machine_and_how_to_go_back_are_written_down(self):
        for title in ("### What leaves your machine", "### Ce qui sort de votre machine", "**Going back to an older version.**",
                      "**Revenir à une version plus ancienne.**"):
            self.assertIn(title, self.readme)
        self.assertIn("**Going back to an older version.**", self.guide)
        self.assertIn("**Revenir à une version plus ancienne.**", self.guide)

    def test_the_badge_says_what_tvos_26_is(self):
        self.assertIn('alt="NuvioTV: tvOS 26+"', self.readme)

    def test_the_changelog_covers_this_release(self):
        log = (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
        version = (ROOT / "windows" / "version.txt").read_text(encoding="utf-8").strip()
        self.assertTrue(log.startswith("# Changelog"))
        self.assertIn(f"## {version}", log)
        self.assertLess(log.index(f"## {version}"), log.index("## 1.0.1"))
        for word in ("iPhone and iPad", "Several Apple TVs", "Going back to 1.0.x", "Apple account full", "TZ"):
            self.assertIn(word, log)
        self.assertNotIn("provider", log)

    def test_the_imported_certificate_stays_a_secondary_route(self):
        section = self.en.split("### Signing with an imported certificate (advanced)", 1)[1].split("###", 1)[0]
        self.assertIn("never needs this", section)
        self.assertIn("does not recommend or contact any signing service", section)
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<details class="details avance" id="edit-advanced">', html)       # closed unless an imported certificate is in use
        self.assertIn('<div id="backup-certs-row" hidden>', html)


if __name__ == "__main__":
    unittest.main()
