"""Moteur de renouvellement : concurrence, délais croissants, erreurs qui demandent une action,
limites du compte gratuit, reprise après reconnexion, santé de la boucle, journal, validations.

    python -m unittest discover -s tests

Aucun plumesign ni Apple TV : les appels externes sont remplacés.
"""
import asyncio
import builtins
import io
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

DATA = tempfile.mkdtemp(prefix="tvsign-test-")
os.environ.update(TVSIGN_DATA=DATA, TVSIGN_AUTH="none", TVSIGN_PLUMESIGN=str(Path(DATA) / "absent"))
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import certdata  # noqa: E402
from app import auth, config, db, devices, ipa, main, plume, proc, renew, settings  # noqa: E402

HEADERS = {"X-Tvsign": "1"}
NOW = time.time()


def make_ipa(bundle="com.example.tv", info=None, platform="appletvos") -> bytes:
    plist = {"CFBundleName": "Test", "CFBundleIdentifier": bundle, "CFBundleShortVersionString": "1.0",
             "DTPlatformName": platform}
    plist.update(info or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Payload/Test.app/Info.plist", plistlib.dumps(plist))
    return buf.getvalue()


def make_signed(expiry: float, certs=(certdata.CERT1, certdata.CERT2)) -> bytes:
    """IPA « signée » : seul embedded.mobileprovision compte pour tvsign."""
    profile = plistlib.dumps({"ExpirationDate": datetime.fromtimestamp(expiry, timezone.utc).replace(tzinfo=None),
                              "DeveloperCertificates": list(certs)})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Payload/Test.app/Info.plist", plistlib.dumps({"CFBundleIdentifier": "com.example.tv"}))
        z.writestr("Payload/Test.app/embedded.mobileprovision", b"\x00junk" + profile + b"\x00")
    return buf.getvalue()


class Base(unittest.TestCase):
    """État propre : tables vidées, mémoire du moteur remise à zéro, aucun compte Apple."""

    def setUp(self):
        db.init()
        for t in ("apps", "devices", "events", "account_status", "account_devices", "revoked_sessions"):
            db.execute(f"DELETE FROM {t}")
        for key in ("renew_window_start", "renew_window_end", "anisette_url", "anisette_fallback", "anisette_servers",
                    "admin_password", "pair_host"):
            settings.raw_set(key, None)
        renew.STOPPING = False          # les tests d'API ferment l'application, qui lève ce drapeau
        renew.TASK = None
        for s in (renew._RUNNING, renew._RERUN, plume.ALERTED, renew._EXPIRED_LOGGED, renew._NO_MATCH_LOGGED):
            s.clear()
        renew.PHASE.clear()
        plume._LAST.clear()
        self.addCleanup(self.clear_accounts)
        self.clear_accounts()

    def clear_accounts(self):
        (plume.STORE / "accounts.json").unlink(missing_ok=True)

    def set_accounts(self, *emails, selected=None, team="TEAM1"):
        plume.STORE.mkdir(parents=True, exist_ok=True)
        (plume.STORE / "accounts.json").write_text(json.dumps({
            "selected_account": selected,
            "accounts": {e: {"first_name": e.split("@")[0], "team_id": team} for e in emails}}), encoding="utf-8")

    def add_device(self, name="Salon", udid="UDID1") -> int:
        return db.execute("INSERT INTO devices (name, pair_identifier, udid, pairing_udid, os_version, created_at) "
                          "VALUES (?, ?, ?, ?, '18.0', 0)", name, udid, udid, udid)

    def add_app(self, name="App", **over) -> int:
        cols = dict(name=name, source_type="upload", created_at=0, enabled=1, status="ok", version="1.0",
                    bundle_id="com.example.tv")
        cols.update(over)
        keys = ", ".join(cols)
        app_id = db.execute(f"INSERT INTO apps ({keys}) VALUES ({', '.join('?' * len(cols))})", *cols.values())
        return app_id

    def app(self, app_id) -> dict:
        return db.row("SELECT * FROM apps WHERE id = ?", app_id)


# ---------- délais croissants et classement des échecs ----------

class Backoff(Base):
    def test_delay_doubles_up_to_the_cap(self):
        self.assertEqual([renew.backoff_seconds(n) / 60 for n in range(1, 7)], [10, 20, 40, 45, 45, 45])

    def test_transient_failures_retry_sooner(self):
        self.assertEqual([renew.backoff_seconds(n, transient=True) / 60 for n in range(1, 6)], [5, 10, 15, 15, 15])

    def test_close_to_expiry_caps_at_ten_minutes(self):
        self.assertEqual([renew.backoff_seconds(n, left=6 * 3600) / 60 for n in range(1, 4)], [10, 10, 10])
        self.assertEqual(renew.backoff_seconds(1, transient=True, left=6 * 3600) / 60, 5)

    def test_never_retries_after_expiry(self):
        app = {"expires_at": NOW + 300}
        self.assertEqual(round(renew._retry_at(app, NOW, fails=4) - NOW), 240)     # expiration - 60 s
        app = {"expires_at": NOW + 20}
        self.assertEqual(round(renew._retry_at(app, NOW) - NOW), 60)

    def test_blocked_failures_wait_for_the_user(self):
        app = {"expires_at": NOW + 5 * 86400}
        self.assertEqual(round((renew._retry_at(app, NOW, kind="session") - NOW) / 3600), 6)
        self.assertEqual(round((renew._retry_at(app, NOW, kind="limit") - NOW) / 3600), 24)
        soon = {"expires_at": NOW + 8 * 3600}
        self.assertEqual(round((renew._retry_at(soon, NOW, kind="session") - NOW) / 3600), 2)

    def test_failure_classification(self):
        self.assertEqual(renew.failure_kind(plume.SessionExpired("x")), "session")
        self.assertEqual(renew.failure_kind(devices.PairingLost("x")), "pairing")
        self.assertEqual(renew.failure_kind(plume.LimitReached("x")), "limit")
        self.assertEqual(renew.failure_kind(renew.NeedsAction("x", "config")), "config")
        for transient in (plume.Transient("x"), devices.Unreachable("x"), devices.AppInUse("x"), RuntimeError("x")):
            self.assertIsNone(renew.failure_kind(transient))

    def test_failure_fields_count_consecutive_failures(self):
        app = {"expires_at": None, "fail_count": 2}
        f = renew.failure_fields(app, NOW, RuntimeError("x"), False)
        self.assertEqual((f["fail_count"], f["needs_action"]), (3, None))
        self.assertEqual(round((f["next_attempt_at"] - NOW) / 60), 40)
        f = renew.failure_fields(app, NOW, plume.SessionExpired("x"), False)
        self.assertEqual(f["needs_action"], "session")
        self.assertGreater(f["next_attempt_at"] - NOW, 5 * 3600)


# ---------- limites du compte gratuit ----------

class FreeAccountLimits(Base):
    DEVICE_MSG = "ERROR install This device has reached the maximum number of installed apps using a free developer profile"

    def test_limit_messages_are_recognised(self):
        for text in (self.DEVICE_MSG, "Maximum number of App IDs reached", "too many app ids", "App ID limit exceeded",
                     "You have reached the maximum number of App IDs for this team"):
            self.assertTrue(plume.is_limit(text), text)
        for text in ("Anisette error: timeout", "session expired", "installd waiting", ""):
            self.assertFalse(plume.is_limit(text), text)

    def test_portal_limit_error_is_its_own_class(self):
        res = SimpleNamespace(output="Developer API error 9400 (HTTP Some(200)): maximum number of App IDs",
                              err="", code=1, tail=lambda n=3: "maximum number of App IDs")
        self.assertIsInstance(plume._failure("a@b.c", "signature", res), plume.LimitReached)

    def test_device_limit_becomes_limit_reached(self):
        e = renew._as_limit(RuntimeError("install : " + self.DEVICE_MSG))
        self.assertIsInstance(e, plume.LimitReached)
        self.assertIn("limite du compte gratuit", str(e))
        plain = RuntimeError("boom")
        self.assertIs(renew._as_limit(plain), plain)

    def test_slots_count_enabled_apps_per_account_and_tv(self):
        self.set_accounts("a@x.com", "b@x.com", selected="a@x.com")
        tv1, tv2 = self.add_device("Salon", "U1"), self.add_device("Chambre", "U2")
        for i in range(3):
            self.add_app(f"A{i}", device_id=tv1, account_email="A@x.com")      # casse différente : même compte
        self.add_app("paused", device_id=tv1, account_email="a@x.com", enabled=0)
        self.add_app("default-account", device_id=tv2)
        self.add_app("other", device_id=tv1, account_email="b@x.com")
        apps = db.rows("SELECT * FROM apps")
        slots = renew.account_slots(apps, db.rows("SELECT * FROM devices"), plume.accounts(), "a@x.com")
        self.assertEqual({(s["device_name"], s["apps"]) for s in slots["a@x.com"]}, {("Salon", 3), ("Chambre", 1)})
        self.assertEqual([(s["apps"], s["max"]) for s in slots["b@x.com"]], [(1, 3)])

    def test_fourth_app_on_a_tv_warns_without_refusing(self):
        self.set_accounts("a@x.com", selected="a@x.com")
        tv = self.add_device()
        ids = [self.add_app(f"A{i}", device_id=tv, account_email="a@x.com") for i in range(3)]
        self.assertEqual(renew.add_warnings(self.app(ids[-1])), [])
        fourth = self.add_app("A4", device_id=tv, account_email="a@x.com")
        self.assertEqual(renew.add_warnings(self.app(fourth)), ["slots_full"])


# ---------- session Apple ----------

class SessionState(Base):
    def test_session_classification(self):
        # un 401 du portail est une session refusée, quel que soit le code Apple
        self.assertTrue(plume.session_expired("Developer API error 9100 (HTTP Some(401)): Authentication required"))
        self.assertTrue(plume.session_expired("Developer API error 1100 (HTTP Some(200)): session expired"))
        # panne réseau / anisette : jamais « reconnecte-toi »
        self.assertFalse(plume.session_expired("Anisette error: server returned HTTP Some(401)"))
        self.assertFalse(plume.session_expired("Authentication SRP error: connection reset by peer"))
        self.assertFalse(plume.session_expired("Developer API error 3000 (HTTP Some(500)): server error"))
        self.assertTrue(plume.session_expired("Authentication extra step required"))

    def test_expired_flag_is_rechecked_before_refusing(self):
        self.set_accounts("a@x.com", selected="a@x.com")
        plume._set_status("a@x.com", False, "session expirée")
        db.execute("UPDATE account_status SET checked_at = ?", NOW - 3600)
        calls = []

        async def check(email):
            calls.append(email)
            return True
        app = {"account_email": "a@x.com"}
        with mock.patch.object(plume, "check_account", check):
            self.assertEqual(asyncio.run(renew._usable_account(app)), "a@x.com")
        self.assertEqual(calls, ["a@x.com"])

    def test_still_expired_after_recheck(self):
        self.set_accounts("a@x.com")
        plume._set_status("a@x.com", False, "x")
        db.execute("UPDATE account_status SET checked_at = ?", NOW - 3600)

        async def check(email):
            return False
        with mock.patch.object(plume, "check_account", check):
            with self.assertRaises(plume.SessionExpired):
                asyncio.run(renew._usable_account({"account_email": "a@x.com"}))

    def test_undecided_recheck_is_transient(self):
        self.set_accounts("a@x.com")
        plume._set_status("a@x.com", False, "x")
        db.execute("UPDATE account_status SET checked_at = ?", NOW - 3600)

        async def check(email):
            return None
        with mock.patch.object(plume, "check_account", check):
            with self.assertRaises(plume.Transient):
                asyncio.run(renew._usable_account({"account_email": "a@x.com"}))

    def test_fresh_negative_result_short_circuits(self):
        self.set_accounts("a@x.com")
        plume._set_status("a@x.com", False, "x")           # vérifié à l'instant

        async def check(email):
            raise AssertionError("pas d'appel à Apple pour un échec tout frais")
        with mock.patch.object(plume, "check_account", check):
            with self.assertRaises(plume.SessionExpired):
                asyncio.run(renew._usable_account({"account_email": "a@x.com"}))

    def test_missing_account_needs_action(self):
        self.set_accounts("a@x.com")
        with self.assertRaises(renew.NeedsAction) as cm:
            asyncio.run(renew._usable_account({"account_email": "gone@x.com"}))
        self.assertEqual(cm.exception.kind, "session")
        with self.assertRaises(renew.NeedsAction):
            self.clear_accounts()
            asyncio.run(renew._usable_account({"account_email": None}))

    def test_account_name_case_does_not_matter(self):
        self.set_accounts("Foo@iCloud.com", selected="Foo@iCloud.com")
        plume._set_status("Foo@iCloud.com", True, "ok")
        self.assertEqual(asyncio.run(renew._usable_account({"account_email": "foo@icloud.com"})), "Foo@iCloud.com")
        self.assertEqual(plume.find_account(" FOO@icloud.com "), "Foo@iCloud.com")
        self.assertIsNone(plume.find_account("bar@icloud.com"))

    def test_default_account_is_deterministic(self):
        accs = [{"email": e, "selected": False} for e in ("b@x.com", "C@x.com", "a@x.com")]
        self.assertEqual(plume.pick_default(accs), "a@x.com")
        accs[1]["selected"] = True
        self.assertEqual(plume.pick_default(accs), "C@x.com")
        self.assertIsNone(plume.pick_default([]))
        self.set_accounts("z@x.com", "m@x.com", "b@x.com", selected=None)
        self.assertEqual(plume.default_account(), "b@x.com")

    def test_reconnection_resumes_waiting_apps(self):
        self.set_accounts("a@x.com", "b@x.com")
        tv = self.add_device()
        waiting = self.add_app("W", device_id=tv, account_email="A@x.com", status="error", fail_count=4,
                               needs_action="session", next_attempt_at=NOW + 6 * 3600)
        backoff = self.add_app("B", device_id=tv, account_email="a@x.com", status="error", fail_count=2,
                               next_attempt_at=NOW + 2400)
        limited = self.add_app("L", device_id=tv, account_email="a@x.com", status="error", needs_action="limit",
                               next_attempt_at=NOW + 86400)
        other = self.add_app("O", device_id=tv, account_email="b@x.com", status="error", needs_action="session",
                             next_attempt_at=NOW + 3600)
        plume.ALERTED.add("a@x.com")
        started = []

        async def go():
            with mock.patch.object(renew, "spawn", lambda i, t="manuel": started.append((i, t)) or True):
                renew._account_valid("a@x.com")
        asyncio.run(go())
        self.assertNotIn("a@x.com", plume.ALERTED)
        for app_id in (waiting, backoff):
            a = self.app(app_id)
            self.assertEqual((a["next_attempt_at"], a["needs_action"], a["fail_count"]), (None, None, 0))
        self.assertIsNotNone(self.app(limited)["next_attempt_at"])          # limite : pas levée par la connexion
        self.assertIsNotNone(self.app(other)["next_attempt_at"])            # autre compte
        self.assertEqual({t for _, t in started}, {"reconnexion"})
        self.assertEqual({i for i, _ in started}, {waiting, backoff})

    def test_pairing_resumes_waiting_apps(self):
        tv, other_tv = self.add_device("A", "U1"), self.add_device("B", "U2")
        waiting = self.add_app("W", device_id=tv, status="error", needs_action="pairing", fail_count=3,
                               next_attempt_at=NOW + 5 * 3600)
        elsewhere = self.add_app("E", device_id=other_tv, status="error", needs_action="pairing",
                                 next_attempt_at=NOW + 5 * 3600)
        started = []

        async def go():
            with mock.patch.object(renew, "spawn", lambda i, t="manuel": started.append((i, t)) or True):
                renew._device_paired(tv)
        asyncio.run(go())
        self.assertIsNone(self.app(waiting)["next_attempt_at"])
        self.assertIsNotNone(self.app(elsewhere)["next_attempt_at"])
        self.assertEqual(started, [(waiting, "appairage")])


# ---------- plage horaire ----------

def local_ts(hour: int) -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 30, 0, 0, 0, -1))


class RenewalWindow(Base):
    def test_window_arithmetic(self):
        w = settings.in_renew_window
        self.assertTrue(w(NOW, None))
        self.assertTrue(w(NOW, (2, 6), tz_hour=2))
        self.assertTrue(w(NOW, (2, 6), tz_hour=5))
        self.assertFalse(w(NOW, (2, 6), tz_hour=6))
        self.assertFalse(w(NOW, (2, 6), tz_hour=23))
        self.assertTrue(w(NOW, (23, 5), tz_hour=23))                 # passe minuit
        self.assertTrue(w(NOW, (23, 5), tz_hour=3))
        self.assertFalse(w(NOW, (23, 5), tz_hour=12))
        self.assertTrue(w(NOW, (7, 7), tz_hour=12))                  # début = fin : toute la journée

    def test_setting_needs_both_ends(self):
        self.assertIsNone(settings.renew_window())
        settings.raw_set("renew_window_start", "2")
        self.assertIsNone(settings.renew_window())
        settings.raw_set("renew_window_end", "6")
        self.assertEqual(settings.renew_window(), (2, 6))

    def test_hour_validation(self):
        for bad in ("24", "-1", "x", "1.5"):
            with self.assertRaises(ValueError) as cm:
                settings.update({"renew_window_start": bad})
            self.assertEqual(cm.exception.args[0], {"renew_window_start": "hour_expected"})
        settings.update({"renew_window_start": "3", "renew_window_end": "5"})
        self.assertEqual(settings.renew_window(), (3, 5))

    def due_app(self, left_hours):
        return {"id": 1, "enabled": 1, "status": "ok", "expires_at": NOW + left_hours * 3600,
                "next_attempt_at": None}

    def test_non_urgent_renewal_waits_for_the_window(self):
        settings.raw_set("renew_window_start", "2")
        settings.raw_set("renew_window_end", "6")
        inside, outside = local_ts(3), local_ts(14)
        app = lambda now: {**self.due_app(0), "expires_at": now + 50 * 3600}      # 50 h : dans la marge de 60 h
        self.assertTrue(renew.due(app(inside), inside))
        self.assertFalse(renew.due(app(outside), outside))

    def test_urgent_renewal_ignores_the_window(self):
        settings.raw_set("renew_window_start", "2")
        settings.raw_set("renew_window_end", "6")
        outside = local_ts(14)
        soon = {**self.due_app(0), "expires_at": outside + 10 * 3600}
        self.assertTrue(renew.due(soon, outside))
        never_installed = {**self.due_app(0), "expires_at": None, "status": "pending"}
        self.assertTrue(renew.due(never_installed, outside))

    def test_no_window_means_always(self):
        self.assertTrue(renew.due(self.due_app(50), local_ts(14)))


# ---------- concurrence et supervision ----------

class Scheduler(Base):
    def due_apps(self, *lefts):
        return [self.add_app(f"A{i}", expires_at=NOW + h * 3600) for i, h in enumerate(lefts)]

    def test_a_long_renewal_does_not_block_the_others(self):
        # A attend installd (longtemps) ; B, plus proche de l'expiration, doit démarrer tout de suite
        a, b = self.due_apps(40, 10)
        starts = {}

        async def fake(app_id, trigger):
            starts[app_id] = time.monotonic()
            if app_id == a:
                await asyncio.sleep(0.4)

        async def go():
            t0 = time.monotonic()
            with mock.patch.object(renew, "_renew", fake), mock.patch.object(renew, "notify", _no_notify):
                await renew.tick(NOW, NOW, NOW)       # contrôles de fond déjà « faits »
                took = time.monotonic() - t0
                await asyncio.sleep(0.1)
                at_100ms = dict(starts)
                await asyncio.sleep(0.5)
            return took, at_100ms
        took, early = asyncio.run(go())
        self.assertLess(took, 0.3, "la passe ne doit pas attendre les renouvellements")
        self.assertEqual(set(early), {a, b}, "les deux apps ont démarré sans attendre la fin de la première")
        self.assertLess(starts[b], starts[a], "la plus proche de l'expiration d'abord")

    def test_most_urgent_first_and_expired_before_everything(self):
        ids = self.due_apps(30, 5, -2, 50)
        order = [a["id"] for a in renew.by_urgency(db.rows("SELECT * FROM apps"), NOW)]
        self.assertEqual(order, [ids[2], ids[1], ids[0], ids[3]])

    def test_long_account_check_does_not_hold_the_loop(self):
        started = asyncio.Event()

        async def slow_pass():
            started.set()
            await asyncio.sleep(0.4)

        async def go():
            with mock.patch.object(renew, "_account_pass", slow_pass), mock.patch.object(renew, "notify", _no_notify):
                t0 = time.monotonic()
                await renew.tick(NOW, 0.0, NOW)       # contrôle des comptes dû
                took = time.monotonic() - t0
                await asyncio.wait_for(started.wait(), 1)
                again = renew._background("accounts", slow_pass)      # déjà en cours : pas de doublon
                await asyncio.sleep(0.5)
            return took, again
        took, again = asyncio.run(go())
        self.assertLess(took, 0.3)
        self.assertFalse(again)

    def test_expiry_alerts_run_during_a_long_renewal(self):
        a, = self.due_apps(40)
        self.add_app("Dead", expires_at=NOW - 60, status="error", next_attempt_at=NOW + 3600)
        sent = []

        async def notify(msg, priority="default", event="fail", **extra):
            sent.append(msg)
            return True

        async def fake(app_id, trigger):
            await asyncio.sleep(0.5)

        async def go():
            with mock.patch.object(renew, "_renew", fake), mock.patch.object(renew, "notify", notify):
                await renew.tick(NOW, NOW, NOW)
                self.assertTrue(any("Dead" in m for m in sent), "alerte d'expiration envoyée malgré le renouvellement")
                await asyncio.sleep(0.6)
        asyncio.run(go())

    def test_release_scan_runs_apart_and_respects_pauses_and_the_window(self):
        fresh = self.add_app("Fresh", source_type="github", source="o/r", expires_at=NOW + 5 * 86400)
        paused = self.add_app("Paused", source_type="github", source="o/r", enabled=0)
        waiting = self.add_app("Waiting", source_type="github", source="o/r", next_attempt_at=NOW + 3600)
        link = self.add_app("Link", source_type="url", source="https://x/y.ipa")
        started, asked = [], []

        async def newer(app):
            asked.append(app["id"])
            return True

        async def scan():
            with mock.patch.object(renew, "new_release", newer),                     mock.patch.object(renew, "spawn", lambda i, t="manuel": started.append((i, t)) or True):
                await renew._release_pass()
        asyncio.run(scan())
        self.assertEqual(started, [(fresh, "nouvelle version")])
        self.assertEqual(asked, [fresh])
        self.assertIsNotNone(renew.HEALTH["last_release_check_at"])
        # hors de la plage horaire, une mise à jour non urgente attend
        settings.raw_set("renew_window_start", "2")
        settings.raw_set("renew_window_end", "3")
        started.clear()
        with mock.patch.object(settings, "in_renew_window", lambda now, window=None, tz_hour=None: False):
            asyncio.run(scan())
        self.assertEqual(started, [])
        self.assertTrue({paused, waiting, link}.isdisjoint(asked))

    def test_supervisor_restarts_a_crashed_loop(self):
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("boom")
            await asyncio.sleep(30)

        async def go():
            with mock.patch.object(renew, "loop", flaky), mock.patch.object(renew, "SUPERVISE_DELAY", 0.01):
                task = asyncio.create_task(renew.supervise())
                await asyncio.sleep(0.2)
                task.cancel()
        before = renew.HEALTH["restarts"]
        asyncio.run(go())
        self.assertEqual(len(calls), 2)
        self.assertEqual(renew.HEALTH["restarts"], before + 1)
        self.assertEqual(self.event_count("boucle de renouvellement arrêtée"), 1)

    def event_count(self, text):
        return len(db.rows("SELECT id FROM events WHERE message LIKE ?", f"%{text}%"))

    def test_health_reports_a_dead_or_silent_loop(self):
        saved = dict(renew.HEALTH)
        self.addCleanup(lambda: renew.HEALTH.update(saved))
        renew.HEALTH.update(last_loop_at=NOW, started_at=NOW - 10)
        self.assertFalse(renew.health(NOW + 60)["loop_stalled"])
        renew.TASK = SimpleNamespace(done=lambda: True)
        self.assertTrue(renew.health(NOW + 60)["loop_stalled"], "tâche morte")
        self.assertFalse(renew.health(NOW + 60)["loop_alive"])
        renew.TASK = SimpleNamespace(done=lambda: False)
        self.assertFalse(renew.health(NOW + 60)["loop_stalled"])
        limit = 3 * config.CHECK_INTERVAL + renew.LOOP_SLACK_SECONDS
        self.assertTrue(renew.health(NOW + limit + 5)["loop_stalled"], "aucune passe depuis trop longtemps")
        renew.HEALTH["last_loop_at"] = None                        # jamais passée depuis le démarrage
        self.assertTrue(renew.health(renew.HEALTH["started_at"] + limit + 5)["loop_stalled"])


async def _no_notify(*a, **k):
    return False


# ---------- un renouvellement de bout en bout (appels externes remplacés) ----------

class RenewRun(Base):
    def setUp(self):
        super().setUp()
        self.set_accounts("a@x.com", selected="a@x.com")
        plume._set_status("a@x.com", True, "ok")
        self.tv = self.add_device()
        self.expiry = NOW + 7 * 86400
        self.app_id = self.add_app("Nuvio", device_id=self.tv, account_email="a@x.com", status="pending",
                                   version="2.0", source_tag="v2")
        (renew.app_dir(self.app_id) / "original.ipa").write_bytes(make_ipa())
        self.phases = []
        self.reach_error = self.install_error = self.sign_error = None
        orig_phase = renew._phase

        def phase(app_id, step, **info):
            self.phases.append(step)
            orig_phase(app_id, step, **info)

        async def check_reachable(device):
            if self.reach_error:
                raise self.reach_error

        async def ensure_device(email, udid, name, on_lock=None):
            if on_lock:
                on_lock("waiting")          # un autre détenteur tient plumesign, puis le verrou est obtenu
                on_lock("acquired")
            if self.sign_error:
                raise self.sign_error

        async def sign(email, src, out, on_lock=None):
            out.write_bytes(make_signed(self.expiry))
            return False

        async def install(device, ipas, on_event=None, wait_cap=None, app_name=None):
            if self.install_error:
                raise self.install_error

        async def refresh(app, force=False):
            return False

        async def confirm_installed(device, bundle_id):
            return time.time()          # la TV a l'app (la liste des apps de la TV a ses propres tests)
        patches = [mock.patch.object(devices, "confirm_installed", confirm_installed), mock.patch.object(renew, "_phase", phase), mock.patch.object(devices, "check_reachable", check_reachable),
                   mock.patch.object(plume, "ensure_device", ensure_device), mock.patch.object(plume, "sign", sign),
                   mock.patch.object(devices, "install", install), mock.patch.object(renew, "refresh_source", refresh),
                   mock.patch.object(renew, "notify", _no_notify),
                   mock.patch.object(plume, "signing_key", lambda email: certdata.KEY1)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_renew(self, trigger="auto"):
        asyncio.run(renew.renew(self.app_id, trigger))
        return self.app(self.app_id)

    def test_success_records_what_is_installed(self):
        a = self.run_renew()
        self.assertEqual((a["status"], a["fail_count"], a["needs_action"], a["next_attempt_at"]), ("ok", 0, None, None))
        self.assertEqual((a["installed_version"], a["installed_tag"]), ("2.0", "v2"))
        self.assertAlmostEqual(a["expires_at"], self.expiry, delta=2)
        # le profil liste les certificats de l'équipe : seul celui de notre clé est retenu
        self.assertEqual(a["cert_serial"], "A1B2C")

    def test_queue_then_signing_phase(self):
        self.run_renew()
        self.assertEqual(self.phases[:3], ["checking", "queued", "signing"])

    def test_pending_version_while_install_fails(self):
        db.update("apps", self.app_id, installed_version="1.0", installed_tag="v1", status="ok",
                  expires_at=NOW + 3600)
        self.install_error = devices.AppInUse("app ouverte")
        a = self.run_renew("nouvelle version")
        self.assertEqual((a["version"], a["installed_version"]), ("2.0", "1.0"))
        self.install_error = None
        a = self.run_renew("manuel")
        self.assertEqual((a["version"], a["installed_version"]), ("2.0", "2.0"))

    def test_repeated_failures_back_off_and_stay_quiet_in_the_journal(self):
        self.install_error = RuntimeError("install : boom")
        delays = []
        for _ in range(3):
            before = time.time()
            a = self.run_renew()
            delays.append(round((a["next_attempt_at"] - before) / 60))
            db.update("apps", self.app_id, next_attempt_at=None)
        self.assertEqual(a["fail_count"], 3)
        self.assertEqual(delays, [10, 20, 40])
        rows = db.rows("SELECT * FROM events WHERE app_id = ? ORDER BY id", self.app_id)
        errors = [r for r in rows if r["level"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["repeat"], 3)
        self.assertLessEqual(errors[0]["first_ts"], errors[0]["ts"])
        # premier essai : signature + installation + erreur ; les suivants n'ajoutent que l'erreur comptée
        self.assertEqual(len(rows), 3)

    def test_success_ends_the_episode(self):
        self.install_error = RuntimeError("install : boom")
        self.run_renew()
        db.update("apps", self.app_id, next_attempt_at=None)
        self.install_error = None
        self.run_renew()
        self.install_error = RuntimeError("install : boom")
        db.update("apps", self.app_id, expires_at=NOW + 3600)
        self.run_renew()
        self.assertEqual(len([r for r in db.rows("SELECT * FROM events WHERE level = 'error'")]), 2)

    def test_free_account_limit_waits_for_the_user(self):
        self.install_error = RuntimeError("install : This device has reached the maximum number of installed apps "
                                          "using a free developer profile")
        a = self.run_renew()
        self.assertEqual((a["status"], a["needs_action"], a["fail_count"]), ("error", "limit", 1))
        self.assertIn("limite du compte gratuit", a["last_error"])
        self.assertGreater(a["next_attempt_at"] - time.time(), 23 * 3600)
        self.assertFalse(renew.due(a, time.time()))
        # l'utilisateur libère une place et touche Renouveler
        self.install_error = None
        renew.clear_block(self.app_id)
        a = self.run_renew("manuel")
        self.assertEqual((a["status"], a["needs_action"]), ("ok", None))

    def test_expired_session_is_a_blocked_failure(self):
        self.sign_error = plume.SessionExpired("session Apple expirée")
        a = self.run_renew()
        self.assertEqual((a["status"], a["needs_action"]), ("error", "session"))
        self.assertGreater(a["next_attempt_at"] - time.time(), 5 * 3600)

    def test_lost_pairing_is_a_blocked_failure(self):
        self.reach_error = devices.PairingLost("appairage perdu")
        a = self.run_renew()
        self.assertEqual(a["needs_action"], "pairing")

    def test_sleeping_tv_is_retried_soon(self):
        self.reach_error = devices.Unreachable("introuvable")
        a = self.run_renew()
        self.assertIsNone(a["needs_action"])
        self.assertLess(a["next_attempt_at"] - time.time(), 6 * 60)

    def test_unreachable_tv_keeps_a_valid_app_ok(self):
        db.update("apps", self.app_id, status="ok", expires_at=NOW + 5 * 86400)
        self.reach_error = devices.Unreachable("introuvable")
        a = self.run_renew("nouvelle version")
        self.assertEqual((a["status"], a["fail_count"]), ("ok", 1))

    def test_missing_ipa_and_tv_need_action(self):
        (renew.app_dir(self.app_id) / "original.ipa").unlink()
        self.assertEqual(self.run_renew()["needs_action"], "config")
        db.update("apps", self.app_id, device_id=None, next_attempt_at=None)
        a = self.run_renew()
        self.assertEqual(a["needs_action"], "config")
        self.assertIn("Apple TV", a["last_error"])


# ---------- certificats ----------

class CertificateWatch(Base):
    def setUp(self):
        super().setUp()
        self.set_accounts("a@x.com", selected="a@x.com")
        self.tv = self.add_device()

    def app_with_signature(self, serial):
        app_id = self.add_app("Nuvio", device_id=self.tv, account_email="a@x.com", cert_serial=serial,
                              signed_at=NOW, expires_at=NOW + 86400 * 5)
        (renew.app_dir(app_id) / "signed.ipa").write_bytes(make_signed(NOW + 86400 * 5))
        return app_id

    def check(self, issued):
        async def issued_serials(email):
            return issued
        with mock.patch.object(plume, "issued_cert_serials", issued_serials), \
                mock.patch.object(plume, "signing_key", lambda e: certdata.KEY1), \
                mock.patch.object(renew, "notify", _no_notify):
            asyncio.run(renew._check_certificates("a@x.com"))

    def test_signing_serial_picks_our_certificate(self):
        signed = Path(tempfile.mkdtemp(dir=DATA)) / "s.ipa"
        signed.write_bytes(make_signed(NOW + 100))
        self.assertEqual(ipa.signing_serial(signed, certdata.KEY1), "A1B2C")
        self.assertEqual(ipa.signing_serial(signed, certdata.KEY1_PKCS1), "A1B2C")
        self.assertEqual(ipa.signing_serial(signed, certdata.KEY2), "7F")
        self.assertIsNone(ipa.signing_serial(signed, "not a key"))
        other = Path(tempfile.mkdtemp(dir=DATA)) / "o.ipa"
        other.write_bytes(make_signed(NOW + 100, certs=(certdata.CERT2,)))
        self.assertIsNone(ipa.signing_serial(other, certdata.KEY1), "notre clé n'est pas dans le profil")

    def test_revoked_own_certificate_is_caught_when_another_is_valid(self):
        app_id = self.app_with_signature("A1B2C")
        self.check({"7F"})                       # l'autre outil garde le sien, le nôtre est révoqué
        a = self.app(app_id)
        self.assertLess(a["expires_at"], time.time())
        self.assertFalse((renew.app_dir(app_id) / "signed.ipa").exists())

    def test_valid_certificate_is_left_alone(self):
        app_id = self.app_with_signature("A1B2C")
        self.check({"A1B2C", "7F"})
        self.assertGreater(self.app(app_id)["expires_at"], time.time() + 86400)

    def test_legacy_serial_list_is_narrowed_to_the_signer(self):
        app_id = self.app_with_signature("7F,A1B2C")        # ancienne valeur : tous les certificats du profil
        self.check({"7F"})
        a = self.app(app_id)
        self.assertEqual(a["cert_serial"], "A1B2C")
        self.assertLess(a["expires_at"], time.time())


# ---------- démarrage ----------

class Startup(Base):
    def test_interrupted_add_leaves_no_ghost(self):
        ghost = self.add_app("https://dl.example/x.ipa", enabled=0, bundle_id=None, status="pending",
                             created_at=NOW - 600, version=None)
        fresh = self.add_app("https://dl.example/y.ipa", enabled=0, bundle_id=None, status="pending",
                             created_at=NOW - 5, version=None)
        paused = self.add_app("Paused", enabled=0)
        (renew.app_dir(ghost) / "original.ipa").write_bytes(b"x")
        renew.startup()
        self.assertIsNone(self.app(ghost))
        self.assertFalse((renew.IPA_DIR / str(ghost)).exists())
        self.assertIsNotNone(self.app(fresh), "un ajout en cours ne doit pas être supprimé")
        self.assertIsNotNone(self.app(paused))

    def test_installed_version_is_backfilled(self):
        ok = self.add_app("Ok", signed_at=NOW, version="3.1")
        err = self.add_app("Err", signed_at=NOW, status="error", version="3.1")
        renew.startup()
        self.assertEqual(self.app(ok)["installed_version"], "3.1")
        self.assertIsNone(self.app(err)["installed_version"])

    def test_forget_clears_per_app_memory(self):
        renew._EXPIRED_LOGGED.add(7)
        renew._NO_MATCH_LOGGED.add(7)
        renew._phase(7, "signing")
        renew.forget(7)
        self.assertFalse(renew._EXPIRED_LOGGED or renew._NO_MATCH_LOGGED or renew.PHASE)

    def test_migration_keeps_existing_data(self):
        """Une base 1.0.0 (sans les colonnes récentes) est complétée sans rien perdre."""
        old = Path(tempfile.mkdtemp(dir=DATA)) / "old.db"
        conn = sqlite3.connect(old)
        conn.executescript(db.SCHEMA.replace("CREATE TABLE IF NOT EXISTS revoked_sessions", "CREATE TABLE IF NOT EXISTS _skip"))
        conn.execute("INSERT INTO apps (name, source_type, created_at) VALUES ('Vieille', 'upload', 0)")
        conn.commit()
        conn.close()
        with mock.patch.object(db, "DB_PATH", old):
            db.init()
            cols = {r["name"] for r in db.rows("PRAGMA table_info(apps)")}
            self.assertTrue({"fail_count", "needs_action", "installed_version", "installed_tag"} <= cols)
            row = db.row("SELECT * FROM apps")
            self.assertEqual((row["name"], row["fail_count"], row["needs_action"]), ("Vieille", 0, None))
            self.assertIn("repeat", {r["name"] for r in db.rows("PRAGMA table_info(events)")})


# ---------- journal ----------

class Journal(Base):
    def test_repeated_error_is_counted_not_duplicated(self):
        for _ in range(4):
            db.log("X : échec, TV introuvable", "error", app_id=3)
        rows = db.rows("SELECT * FROM events WHERE app_id = 3")
        self.assertEqual((len(rows), rows[0]["repeat"]), (1, 4))
        self.assertIsNotNone(rows[0]["first_ts"])

    def test_new_episode_after_other_events_is_a_new_row(self):
        db.log("X : échec", "error", app_id=3)
        db.log("X : renouvelée", app_id=3)
        db.log("X : échec", "error", app_id=3)
        self.assertEqual(len(db.rows("SELECT * FROM events WHERE app_id = 3")), 3)

    def test_unjournaled_lines_do_not_reach_the_table(self):
        db.log("détail d'essai", app_id=3, journal=False)
        self.assertEqual(db.rows("SELECT * FROM events WHERE app_id = 3"), [])

    def test_retention_is_per_app_and_global(self):
        with mock.patch.object(db, "KEEP_PER_APP", 5), mock.patch.object(db, "KEEP_TOTAL", 1000):
            db.log("démarrage", app_id=None)
            for i in range(20):
                db.log(f"ligne {i}", app_id=9)
            db.log("autre app", app_id=10)
        self.assertEqual(len(db.rows("SELECT * FROM events WHERE app_id = 9")), 5)
        self.assertEqual(len(db.rows("SELECT * FROM events WHERE app_id IS NULL")), 1)
        self.assertEqual(len(db.rows("SELECT * FROM events WHERE app_id = 10")), 1)
        with mock.patch.object(db, "KEEP_PER_APP", 100), mock.patch.object(db, "KEEP_TOTAL", 4):
            for i in range(10):
                db.log(f"total {i}", app_id=11)
        self.assertEqual(len(db.rows("SELECT * FROM events")), 4)

    def test_unencodable_console_never_breaks_the_operation(self):
        def boom(*a, **k):
            raise UnicodeEncodeError("charmap", "→", 0, 1, "cannot encode")
        with mock.patch.object(builtins, "print", boom):
            db.log("identifiant d'app changé (a → b)", "warn", app_id=4)
        self.assertEqual(len(db.rows("SELECT * FROM events WHERE app_id = 4")), 1)


# ---------- configuration ----------

class EnvParsing(unittest.TestCase):
    def num(self, value, *a, **k):
        with mock.patch.dict(os.environ, {"TVSIGN_X": value}):
            return config._num("TVSIGN_X", *a, **k)

    def test_unreadable_values_fall_back_to_the_default(self):
        self.assertEqual(self.num("10min", 600, 30), 600)
        self.assertEqual(self.num("abc", 8765, 1, 65535), 8765)
        self.assertEqual(self.num("nan", 6, 1, kind=float), 6.0)
        self.assertEqual(self.num("inf", 6, 1, kind=float), 6.0)

    def test_out_of_range_values_are_clamped(self):
        self.assertEqual(self.num("0", 600, 30), 30)
        self.assertEqual(self.num("-5", 45, 5), 5)
        self.assertEqual(self.num("0.2", 6, 1, kind=float), 1.0)
        self.assertEqual(self.num("99999", 8765, 1, 65535), 65535)

    def test_valid_values_pass(self):
        self.assertEqual(self.num("120", 600, 30), 120)
        self.assertEqual(self.num("2.5", 6, 1, kind=float), 2.5)
        self.assertEqual(self.num("0", 30, 0, kind=float), 0.0)       # 0 = jamais, pour la recherche de versions

    def test_bad_environment_does_not_crash_the_import(self):
        env = {**os.environ, "TVSIGN_DATA": tempfile.mkdtemp(dir=DATA), "TVSIGN_CHECK_INTERVAL": "0",
               "TVSIGN_PORT": "abc", "TVSIGN_RETRY_MINUTES": "x"}
        out = subprocess.run([sys.executable, "-c", "from app import config; print(config.CHECK_INTERVAL, config.PORT, "
                              "config.RETRY_MINUTES)"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("30 8765 45", out.stdout)
        self.assertIn("[warn] TVSIGN_PORT", out.stdout)

    def test_unreadable_password_file_is_loud(self):
        env = {**os.environ, "TVSIGN_DATA": tempfile.mkdtemp(dir=DATA), "TVSIGN_PASSWORD_FILE": str(Path(DATA) / "nope")}
        env.pop("TVSIGN_PASSWORD", None)
        out = subprocess.run([sys.executable, "-c", "from app import config; print(repr(config.PASSWORD_FILE_ERROR))"],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("[error] TVSIGN_PASSWORD_FILE", out.stdout)
        self.assertIn("N'EST PAS appliqué", out.stdout)

    def test_empty_password_file_is_reported_too(self):
        empty = Path(tempfile.mkdtemp(dir=DATA)) / "pw"
        empty.write_text("\n", encoding="utf-8")
        env = {**os.environ, "TVSIGN_DATA": tempfile.mkdtemp(dir=DATA), "TVSIGN_PASSWORD_FILE": str(empty)}
        env.pop("TVSIGN_PASSWORD", None)
        out = subprocess.run([sys.executable, "-c", "from app import config; print(config.PASSWORD_FILE_ERROR)"],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("est vide", out.stdout)

    def test_readable_password_file_is_silent(self):
        good = Path(tempfile.mkdtemp(dir=DATA)) / "pw"
        good.write_text("correct horse battery\n", encoding="utf-8")
        env = {**os.environ, "TVSIGN_DATA": tempfile.mkdtemp(dir=DATA), "TVSIGN_PASSWORD_FILE": str(good)}
        env.pop("TVSIGN_PASSWORD", None)
        out = subprocess.run([sys.executable, "-c", "from app import config; print(config.PASSWORD_FILE_ERROR, "
                              "config.PASSWORD_ENV)"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "None correct horse battery")


class SettingsCodes(Base):
    def test_validation_errors_are_stable_codes(self):
        with self.assertRaises(ValueError) as cm:
            settings.update({"renew_before_hours": 5, "ntfy_url": "ftp://x", "language": "de", "nope": 1})
        self.assertEqual(cm.exception.args[0], {"renew_before_hours": "range:12:150", "ntfy_url": "url_expected",
                                                "language": "language_expected", "nope": "unknown_setting"})
        with self.assertRaises(ValueError) as cm:
            settings.update({"renew_before_hours": "abc"})
        self.assertEqual(cm.exception.args[0], {"renew_before_hours": "number_expected"})

    def test_env_locked_setting_code(self):
        with mock.patch.dict(os.environ, {"TVSIGN_NTFY_URL": "https://ntfy.sh/x"}):
            with self.assertRaises(ValueError) as cm:
                settings.update({"ntfy_url": "https://other.example/y"})
        self.assertEqual(cm.exception.args[0], {"ntfy_url": "set_by_env:TVSIGN_NTFY_URL"})

    def test_anisette_accepts_an_ordered_list(self):
        settings.update({"anisette_url": "https://a.example, https://b.example\nhttps://c.example"})
        self.assertEqual(settings.anisette_urls(), ["https://a.example", "https://b.example", "https://c.example"])
        self.assertEqual(settings.anisette_url(), "https://a.example")
        with self.assertRaises(ValueError):
            settings.update({"anisette_url": "https://a.example nonsense"})
        settings.raw_set("anisette_url", None)
        self.assertEqual(settings.anisette_urls(), [])


class AnisetteServersReachPlumesign(Base):
    """tvsign donne à plumesign la liste complète des serveurs anisette ; c'est plumesign qui passe de l'un à
    l'autre (injoignable, en erreur, données refusées par Apple), en une seule exécution de la commande."""

    def fake_run(self, code=0, output="ok"):
        calls = []

        async def run(*args, env=None, **kw):
            calls.append((env.get("PLUMESIGN_ANISETTE_URL"), env.get("PLUMESIGN_ANISETTE_FALLBACK_URL")))
            return proc.Result(code, output)
        return run, calls

    def test_the_chosen_servers_go_first_in_one_run(self):
        settings.raw_set("anisette_url", "https://a.example https://b.example")
        run, calls = self.fake_run()
        with mock.patch.object(plume, "run", run):
            res = asyncio.run(plume._plume("account", "devices"))
        self.assertTrue(res.ok)
        self.assertEqual(len(calls), 1)
        first, behind = calls[0]
        self.assertEqual(first, "https://a.example")
        self.assertEqual(behind.split()[:1], ["https://b.example"])
        self.assertEqual(behind.split()[1:], list(settings.DEFAULT_ANISETTE_SERVERS))

    def test_the_chosen_servers_alone_when_the_fallback_is_off(self):
        settings.raw_set("anisette_url", "https://a.example https://b.example")
        settings.raw_set("anisette_fallback", "off")
        run, calls = self.fake_run()
        with mock.patch.object(plume, "run", run):
            asyncio.run(plume._plume("account", "devices"))
        self.assertEqual(calls, [("https://a.example", "https://b.example")])

    def test_a_failure_is_not_run_again_from_here(self):
        settings.raw_set("anisette_url", "https://a.example https://b.example")
        for output in ("Anisette error: connection refused", "Developer API error 9000: nope",
                       "Error: Apple answered -80045 (cannot be completed at this time)"):
            run, calls = self.fake_run(1, output)
            with mock.patch.object(plume, "run", run):
                res = asyncio.run(plume._plume("account", "devices"))
            self.assertFalse(res.ok)
            self.assertIn(output, res.output)
            self.assertEqual(len(calls), 1, output)

    def test_default_servers_when_none_configured(self):
        run, calls = self.fake_run()
        with mock.patch.object(plume, "run", run):
            asyncio.run(plume._plume("account", "devices"))
        servers = list(settings.DEFAULT_ANISETTE_SERVERS)
        self.assertEqual(calls, [(servers[0], " ".join(servers[1:]))])


# ---------- IPA, dépôt GitHub ----------

class IpaValidation(unittest.TestCase):
    def info(self, plist_bytes=None, **kw):
        path = Path(tempfile.mkdtemp(dir=DATA)) / "a.ipa"
        if plist_bytes is None:
            path.write_bytes(make_ipa(**kw))
        else:
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("Payload/T.app/Info.plist", plist_bytes)
        return ipa.app_info(path)

    def test_plist_that_is_not_a_dictionary(self):
        with self.assertRaises(ValueError):
            self.info(plistlib.dumps(["a", "b"]))

    def test_broken_plist(self):
        with self.assertRaises(ValueError):
            self.info(b"<?xml version='1.0'?><plist><dict><key>a")
        with self.assertRaises(ValueError):
            self.info(b"garbage")

    def test_bundle_identifier_required_and_textual(self):
        with self.assertRaises(ValueError):
            self.info(plistlib.dumps({"CFBundleName": "x", "DTPlatformName": "appletvos"}))
        with self.assertRaises(ValueError):
            self.info(plistlib.dumps({"CFBundleIdentifier": 12345}))

    def test_wrong_typed_fields_fall_back(self):
        i = self.info(info={"CFBundleDisplayName": ["a"], "CFBundleShortVersionString": 3})
        self.assertEqual((i["name"], i["version"]), ("Test", None))


class RepoNames(unittest.TestCase):
    def test_valid_names(self):
        for given, wanted in (("owner/repo", "owner/repo"), (" o.w-n_er/re.po ", "o.w-n_er/re.po"),
                              ("https://github.com/owner/repo", "owner/repo"),
                              ("https://github.com/owner/repo.git", "owner/repo"),
                              ("http://www.github.com/owner/repo/", "owner/repo"), ("owner/repo.git", "owner/repo")):
            self.assertEqual(ipa.normalize_repo(given), wanted, given)

    def test_rejected_names(self):
        for bad in ("../..", "./.", "a/..", "../b", ".", "..", "owner", "owner/", "/repo", "é/é", "a/b/c", "a b/c",
                    "", None, "owner/repo?x=1", "x" * 101 + "/r"):
            self.assertIsNone(ipa.normalize_repo(bad), bad)

    def test_server_check_normalises_and_rejects(self):
        self.assertEqual(asyncio.run(main._check_source("github", "https://github.com/o/r.git")), "o/r")
        for bad in ("../..", "é/é"):
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(main._check_source("github", bad))
            self.assertEqual(cm.exception.detail, "invalid_repo")

    def test_latest_github_never_builds_a_traversal_url(self):
        with self.assertRaises(ipa.SourceRejected):
            ipa.latest_github("../..", None)


# ---------- sessions interactives ----------

class Sessions(unittest.TestCase):
    def test_done_only_after_the_finishing_work(self):
        async def go():
            s = proc.Session("pair", re.compile("never"))
            seen = []

            async def on_exit(sess):
                seen.append((sess.state, sess.exit_ok))
                await asyncio.sleep(0.3)

            await s.start(sys.executable, "-c", "print('PAIRED a b')", on_exit=on_exit)
            await asyncio.sleep(0.15)
            during = (s.state, proc.busy() if s.id in proc.SESSIONS else None)
            await s._task
            return seen, during, s.state
        seen, during, final = asyncio.run(go())
        self.assertEqual(seen, [("finishing", True)])
        self.assertEqual(during[0], "finishing")
        self.assertEqual(final, "done")

    def test_user_cancel_is_not_a_failure(self):
        async def go():
            s = proc.Session("pair", re.compile("never"))
            await s.start(sys.executable, "-c", "import time; time.sleep(30)")
            await asyncio.sleep(0.2)
            s.cancel()
            await asyncio.wait_for(s._task, 10)
            return s
        s = asyncio.run(go())
        self.assertEqual(s.state, "failed")
        self.assertTrue(s.cancelled and s.user_cancelled)
        self.assertFalse(s.exit_ok)

    def test_timeout_is_not_a_user_cancel(self):
        s = proc.Session("login", re.compile("never"))
        s.lines.append("délai dépassé, connexion abandonnée")
        s.cancelled = True
        self.assertFalse(s.user_cancelled)

    def test_finishing_session_counts_as_busy(self):
        s = proc.Session("pair", re.compile("x"))
        s.state = "finishing"
        proc.SESSIONS["t-fin"] = s
        try:
            self.assertTrue(proc.busy())
        finally:
            proc.SESSIONS.pop("t-fin", None)


# ---------- API ----------

class Api(Base):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def add(self, content, name="a.ipa", **data):
        return self.client.post("/api/apps", headers=HEADERS, data={"source_type": "upload", **data},
                                files={"file": (name, content, "application/octet-stream")})

    def test_unknown_account_is_refused_at_creation_and_edit(self):
        self.set_accounts("a@x.com", selected="a@x.com")
        r = self.add(make_ipa(bundle="com.example.acc"), account_email="nobody@x.com")
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "unknown_account"))
        self.assertEqual(db.rows("SELECT id FROM apps"), [])
        ok = self.add(make_ipa(bundle="com.example.acc"), account_email="A@X.COM")
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["account_email"], "a@x.com")           # écriture du store
        self.assertEqual(ok.json()["warnings"], [])
        app_id = ok.json()["id"]
        r = self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"account_email": "nobody@x.com"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "unknown_account"))

    def test_malformed_ipas_leave_no_ghost_row(self):
        for plist in (plistlib.dumps(["x"]), b"<plist><dict><key>a", plistlib.dumps({"CFBundleName": "x"})):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("Payload/T.app/Info.plist", plist)
            r = self.add(buf.getvalue())
            self.assertEqual((r.status_code, r.json()["detail"]), (400, "not_an_ipa"))
        self.assertEqual(db.rows("SELECT id FROM apps"), [])

    def test_odd_typed_name_does_not_crash(self):
        r = self.add(make_ipa(bundle="com.example.odd", info={"CFBundleDisplayName": ["a"]}))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["name"], "Test")

    def test_fourth_app_is_added_with_a_warning(self):
        self.set_accounts("a@x.com", selected="a@x.com")
        tv = self.add_device()
        for i in range(3):
            self.add_app(f"A{i}", device_id=tv, account_email="a@x.com", bundle_id=f"com.example.n{i}")
        r = self.add(make_ipa(bundle="com.example.fourth"), device_id=str(tv), account_email="a@x.com")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["warnings"], ["slots_full"])

    def test_cancelled_add_removes_its_row(self):
        async def cancelled(*a, **k):
            raise asyncio.CancelledError()
        with mock.patch.object(ipa, "app_info", side_effect=asyncio.CancelledError()):
            with self.assertRaises(BaseException):
                self.add(make_ipa(bundle="com.example.cancel"))
        self.assertEqual(db.rows("SELECT id FROM apps"), [])

    def test_account_routes_use_the_store_spelling(self):
        self.set_accounts("Foo@iCloud.com", selected="Foo@iCloud.com")
        r = self.client.post("/api/accounts/nobody@x.com/check", headers=HEADERS)
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "unknown_account"))
        self.assertEqual(db.rows("SELECT * FROM account_status"), [], "pas de ligne d'état pour un compte inconnu")
        r = self.client.post("/api/accounts/foo@icloud.com/select", headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)

    def test_busy_plumesign_is_a_503_not_a_500(self):
        self.set_accounts("a@x.com", "b@x.com", selected="a@x.com")

        async def busy(*a, **k):
            raise plume.Busy("occupé")
        with mock.patch.object(plume, "select", busy):
            r = self.client.post("/api/accounts/b@x.com/select", headers=HEADERS)
        self.assertEqual((r.status_code, r.json()["detail"]), (503, "plumesign_busy"))

    def test_state_exposes_engine_fields(self):
        self.set_accounts("a@x.com", selected="a@x.com")
        tv = self.add_device()
        self.add_app("Nuvio", device_id=tv, account_email="a@x.com", fail_count=2, needs_action="limit",
                     installed_version="1.0")
        s = self.client.get("/api/state").json()
        app = s["apps"][0]
        self.assertEqual((app["fail_count"], app["needs_action"], app["installed_version"]), (2, "limit", "1.0"))
        self.assertEqual(s["accounts"][0]["slots"], [{"device_id": tv, "device_name": "Salon", "apps": 1, "max": 3}])
        self.assertEqual(s["max_active_apps"], 3)
        self.assertIn("loop_alive", s["health"])
        self.assertTrue(s["health"]["loop_alive"])

    def test_settings_errors_are_codes(self):
        r = self.client.patch("/api/settings", headers=HEADERS, json={"renew_before_hours": 5})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, {"renew_before_hours": "range:12:150"}))
        r = self.client.post("/api/settings/test-notification", headers=HEADERS, json={"url": "ftp://x"})
        self.assertEqual(r.json()["detail"], "url_expected")
        r = self.client.post("/api/devices/pair", headers=HEADERS, json={"ip": "bad ip!", "port": 1, "name": "x"})
        self.assertEqual(r.json()["detail"], "invalid_address")

    def test_manual_renewal_and_edits_clear_the_wait(self):
        tv = self.add_device()
        app_id = self.add_app("Nuvio", device_id=tv, status="error", fail_count=5, needs_action="limit",
                              next_attempt_at=NOW + 86400)
        with mock.patch.object(renew, "spawn", lambda i, t="manuel": True):
            r = self.client.post(f"/api/apps/{app_id}/renew", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        a = self.app(app_id)
        self.assertEqual((a["fail_count"], a["needs_action"], a["next_attempt_at"]), (0, None, None))
        db.update("apps", app_id, fail_count=3, needs_action="session", next_attempt_at=NOW + 5000, enabled=0)
        self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"enabled": True})
        a = self.app(app_id)
        self.assertEqual((a["fail_count"], a["needs_action"], a["next_attempt_at"]), (0, None, None))

    def test_delete_forgets_the_app_in_memory(self):
        app_id = self.add_app("Nuvio")
        renew._EXPIRED_LOGGED.add(app_id)
        renew._NO_MATCH_LOGGED.add(app_id)
        self.assertEqual(self.client.delete(f"/api/apps/{app_id}", headers=HEADERS).status_code, 200)
        self.assertNotIn(app_id, renew._EXPIRED_LOGGED | renew._NO_MATCH_LOGGED)

    def test_old_temp_directories_are_swept_at_startup(self):
        old = config.TMP_DIR / "restore-1"
        (old / "PlumeImpactor").mkdir(parents=True, exist_ok=True)
        (old / "PlumeImpactor" / "accounts.json").write_text("{}", encoding="utf-8")
        os.utime(old, (NOW - 7200, NOW - 7200))
        recent = config.TMP_DIR / "restore-2"
        recent.mkdir(exist_ok=True)
        with TestClient(main.app):
            pass
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        recent.rmdir()
        renew.STOPPING = False


class ApiBackup(Base):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def restore(self, manifest_bytes, extra=None):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            if manifest_bytes is not None:
                z.writestr("manifest.json", manifest_bytes)
            for name, data in (extra or {}).items():
                z.writestr(name, data)
        return self.client.post("/api/backup/import", headers=HEADERS,
                                files={"file": ("b.tvsign-backup", buf.getvalue(), "application/zip")})

    def test_odd_manifests_are_a_400(self):
        for manifest in (b"[]", b"null", b"3", b"{not json", b'{"format": "tvsign-backup", "version": "abc"}',
                         b'{"format": "tvsign-backup", "version": null}'):
            r = self.restore(manifest)
            self.assertEqual(r.status_code, 400, (manifest, r.text))
        self.assertEqual(self.restore(None).status_code, 400)

    def test_unreadable_database_is_a_400(self):
        r = self.restore(b'{"format": "tvsign-backup", "version": 1}', {"tvsign.db": b"not a database at all" * 50})
        self.assertEqual(r.status_code, 400, r.text)

    def test_import_refused_while_busy(self):
        renew._RUNNING.add(99)
        try:
            r = self.restore(b"{}")
        finally:
            renew._RUNNING.discard(99)
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "busy"))

    def test_export_leaves_local_secrets_out_and_cleans_up(self):
        auth.set_password("a very long password")
        try:
            r = self.client.get("/api/backup/export")
            self.assertEqual(r.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                dbfile = Path(tempfile.mkdtemp(dir=DATA)) / "x.db"
                dbfile.write_bytes(z.read("tvsign.db"))
            conn = sqlite3.connect(dbfile)
            keys = {row[0] for row in conn.execute("SELECT key FROM settings")}
            conn.close()
            self.assertFalse(keys & {"admin_password", "session_version", "setup_done"}, keys)
            self.assertEqual(list(config.TMP_DIR.glob("tvsign-*.tvsign-backup")), [])
            self.assertTrue(settings.raw_get("admin_password"), "l'instance garde son mot de passe")
        finally:
            settings.raw_set("admin_password", None)

    def test_export_fails_fast_during_an_apple_sign_in(self):
        s = proc.Session("login", re.compile("x"))
        s.state = "waiting"
        proc.SESSIONS["t-login"] = s
        try:
            r = self.client.get("/api/backup/export")
        finally:
            proc.SESSIONS.pop("t-login", None)
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "busy"))

    def test_export_reports_a_stuck_plumesign(self):
        async def stuck(*a, **k):
            raise plume.Busy("occupé")

        class Locked:
            async def __aenter__(self):
                raise plume.Busy("occupé")

            async def __aexit__(self, *a):
                return False
        with mock.patch.object(plume, "_locked", lambda state=None: Locked()):
            r = self.client.get("/api/backup/export")
        self.assertEqual((r.status_code, r.json()["detail"]), (503, "plumesign_busy"))


# ---------- sessions web, mot de passe ----------

class WebSessions(Base):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def test_cookie_carries_a_session_id_that_survives_renewal(self):
        c = auth.make_cookie()
        data = auth.read_cookie(c)
        self.assertTrue(data["sid"])
        renewed = auth.read_cookie(auth.make_cookie(data["sid"]))
        self.assertEqual(renewed["sid"], data["sid"])
        self.assertNotEqual(auth.read_cookie(auth.make_cookie())["sid"], data["sid"])

    def test_sign_out_revokes_this_cookie_only(self):
        mine, other = auth.make_cookie(), auth.make_cookie()
        self.client.cookies.set(auth.COOKIE, mine)
        r = self.client.post("/api/auth/logout", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        self.client.cookies.clear()
        self.assertIsNone(auth.read_cookie(mine), "un exemplaire copié du cookie ne vaut plus rien")
        self.assertIsNotNone(auth.read_cookie(other), "les autres sessions restent ouvertes")
        # même après renouvellement glissant du cookie volé
        stolen_renewed = auth.make_cookie(auth.read_cookie(other)["sid"])
        self.assertIsNotNone(auth.read_cookie(stolen_renewed))

    def test_revocation_survives_a_restart(self):
        c = auth.make_cookie()
        auth.revoke(auth.read_cookie(c)["sid"])
        auth._REVOKED.clear()
        auth.load_revoked()
        self.assertIsNone(auth.read_cookie(c))

    def test_cookie_without_session_id_still_valid(self):
        """Cookie émis avant cette version : accepté jusqu'à son expiration normale."""
        import base64
        payload = base64.urlsafe_b64encode(json.dumps({"v": auth._version(), "iat": int(time.time())}).encode()).decode().rstrip("=")
        legacy = f"{payload}.{auth._sign(payload.encode())}"
        self.assertIsNotNone(auth.read_cookie(legacy))

    def test_reset_password_command(self):
        from app import __main__ as cli
        with mock.patch.object(auth, "enabled", lambda: True), \
                mock.patch.dict(os.environ, {"TVSIGN_NEW_PASSWORD": "brand new password"}), \
                mock.patch.object(os, "geteuid", lambda: 1000, create=True):
            before = auth.make_cookie()
            self.assertEqual(cli.reset_password(), 0)
        try:
            self.assertTrue(auth.verify_password("brand new password", auth.stored_hash()))
            self.assertIsNone(auth.read_cookie(before), "les sessions ouvertes sont coupées")
        finally:
            settings.raw_set("admin_password", None)

    def test_reset_password_refuses_short_password_and_env_password(self):
        from app import __main__ as cli
        with mock.patch.object(auth, "enabled", lambda: True), \
                mock.patch.dict(os.environ, {"TVSIGN_NEW_PASSWORD": "short"}), \
                mock.patch.object(os, "geteuid", lambda: 1000, create=True):
            self.assertEqual(cli.reset_password(), 1)
            with mock.patch.object(auth, "PASSWORD_ENV", "imposed by env"):
                self.assertEqual(cli.reset_password(), 2)
        self.assertFalse(settings.raw_get("admin_password"))


class Interface(unittest.TestCase):
    """L'interface sait traduire chaque code d'erreur du serveur et expose chaque réglage."""

    I18N = (ROOT / "web" / "i18n.js").read_text(encoding="utf-8")
    HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_error_codes_have_translations(self):
        for code in ("busy", "plumesign_busy", "invalid_address", "url_expected", "range", "number_expected",
                     "language_expected", "hour_expected", "unknown_setting", "set_by_env", "credentials_required",
                     "session_expired", "no_input_expected", "unknown_account", "invalid_repo", "not_an_ipa"):
            self.assertRegex(self.I18N, rf"(?m)^\s+err_{code}: \[", f"err_{code}")

    def test_new_strings_exist_in_both_languages(self):
        # chaque entrée « clé: ["fr", "en"] » : deux chaînes non vides
        for key in ("st_queued", "st_action", "retry_blocked", "retry_at_n", "version_pending", "slots_line",
                    "warn_slots_full", "journal_repeat", "renew_window_from", "renew_window_to", "renew_window_note",
                    "diag_password_file", "diag_password_file_fix", "finishing", "tr_reconnect", "tr_paired"):
            m = re.search(rf'^\s+{key}: \["[^"]+",\s*"[^"]+"\]', self.I18N, re.M)
            self.assertTrue(m, key)

    def test_every_setting_has_a_field(self):
        # ntfy_url et ntfy_events se règlent dans la liste des destinations de notification (extras.js)
        for key in settings.PUBLIC:
            if key not in ("language", "ntfy_url", "ntfy_events"):
                self.assertIn(f'name="{key}"', self.HTML, key)
        self.assertIn('id="notif-liste"', self.HTML)



# ---------- corrections de la revue ----------

class ReviewFixes(Base):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    # -- alerte « expiré » pendant le démarrage d'une re-signature
    def test_no_expiry_alert_for_an_app_whose_renewal_is_starting(self):
        self.add_app("Revoked", expires_at=NOW - 1, notified_level=None)
        sent = []

        async def notify(msg, priority="default", event="fail", **extra):
            sent.append(msg)
            return True

        async def fake(i, trigger):
            await asyncio.sleep(0.2)
            db.update("apps", i, expires_at=NOW + 7 * 86400, status="ok")

        async def go():
            with mock.patch.object(renew, "_renew", fake), mock.patch.object(renew, "notify", notify):
                await renew.tick(NOW, NOW, NOW)
                await asyncio.sleep(0.4)
        asyncio.run(go())
        self.assertEqual(sent, [])
        self.assertEqual([e for e in db.rows("SELECT * FROM events WHERE level = 'error'")
                          if "expiré" in e["message"]], [])

    # -- compte déconnecté entre-temps
    def test_account_removed_between_lookup_and_listing_needs_action(self):
        self.set_accounts("a@x.com")
        with mock.patch.object(plume, "find_account", lambda e: "a@x.com"), \
                mock.patch.object(plume, "accounts", lambda: []):
            with self.assertRaises(renew.NeedsAction) as cm:
                asyncio.run(renew._usable_account({"account_email": "a@x.com"}))
        self.assertEqual(cm.exception.kind, "session")

    # -- journal de reprise d'un compte
    def test_recovery_found_by_the_periodic_check_is_journaled(self):
        self.set_accounts("a@x.com")
        plume.ALERTED.add("a@x.com")

        async def check(email):
            renew._account_valid(email)         # ce que fait plume._valid() en cas de succès
            return True

        async def certs(email):
            return None
        with mock.patch.object(plume, "check_account", check), mock.patch.object(renew, "_check_certificates", certs):
            asyncio.run(renew._check_accounts())
        self.assertTrue(any("de nouveau valide" in e["message"] for e in db.rows("SELECT * FROM events")))
        self.assertNotIn("a@x.com", plume.ALERTED)

    # -- plage horaire visible de l'interface
    def test_next_window_start(self):
        w = (2, 6)
        self.assertEqual(settings.next_window_start(local_ts(3), w), local_ts(3))
        got = settings.next_window_start(local_ts(14), w)
        self.assertEqual(time.localtime(got).tm_hour, 2)
        self.assertEqual((time.localtime(got).tm_min, time.localtime(got).tm_sec), (0, 0))
        self.assertGreater(got, local_ts(14))
        self.assertLess(got - local_ts(14), 13 * 3600)
        self.assertEqual(settings.next_window_start(local_ts(14), None), local_ts(14))
        got = settings.next_window_start(local_ts(12), (23, 5))        # plage passant minuit
        self.assertEqual(time.localtime(got).tm_hour, 23)

    def test_window_view_reports_the_real_renewal_time(self):
        settings.raw_set("renew_window_start", "2")
        settings.raw_set("renew_window_end", "6")
        now = local_ts(14)
        app_id = self.add_app("W", expires_at=now + 50 * 3600)         # renouvellement dû depuis 10 h
        v = renew.window_view(self.app(app_id), now, 60.0)
        self.assertTrue(v["window_wait"])
        self.assertGreater(v["renew_at"], now)
        self.assertEqual(v["renew_at"], v["window_at"])
        self.assertEqual(time.localtime(v["window_at"]).tm_hour, 2)
        # dans la plage : rien n'attend
        inside = local_ts(3)
        v = renew.window_view({**self.app(app_id), "expires_at": inside + 50 * 3600}, inside, 60.0)
        self.assertFalse(v["window_wait"])
        self.assertLess(v["renew_at"], inside)
        # urgent (moins de 24 h) : la plage ne compte plus
        v = renew.window_view({**self.app(app_id), "expires_at": now + 10 * 3600}, now, 60.0)
        self.assertFalse(v["window_wait"])
        # pas encore dû : le repère de renouvellement tombe à l'ouverture de la plage suivante
        v = renew.window_view({**self.app(app_id), "expires_at": now + 80 * 3600}, now, 60.0)
        self.assertFalse(v["window_wait"])
        self.assertGreaterEqual(v["renew_at"], now + 20 * 3600)
        self.assertTrue(settings.in_renew_window(v["renew_at"]))
        # sans plage : comportement d'origine
        settings.raw_set("renew_window_start", None)
        settings.raw_set("renew_window_end", None)
        v = renew.window_view({**self.app(app_id), "expires_at": now + 50 * 3600}, now, 60.0)
        self.assertEqual((v["window_wait"], v["renew_at"]), (False, now + 50 * 3600 - 60 * 3600))

    def test_failed_app_outside_the_window_waits_for_it(self):
        settings.raw_set("renew_window_start", "2")
        settings.raw_set("renew_window_end", "6")
        now = local_ts(14)
        app_id = self.add_app("F", expires_at=now + 100 * 3600, status="error", next_attempt_at=now + 600)
        v = renew.window_view(self.app(app_id), now, 60.0)
        self.assertTrue(v["window_wait"])
        self.assertEqual(time.localtime(v["window_at"]).tm_hour, 2)

    def test_state_exposes_window_wait(self):
        lt = time.localtime()
        settings.raw_set("renew_window_start", str((lt.tm_hour + 2) % 24))
        settings.raw_set("renew_window_end", str((lt.tm_hour + 3) % 24))
        now = time.time()
        self.add_app("S", expires_at=now + 50 * 3600)
        a = main.state()["apps"][0]
        self.assertTrue(a["window_wait"])
        self.assertGreater(a["renew_at"], now)
        self.assertEqual(a["renew_at"], a["window_at"])

    # -- une seule borne de la plage
    def test_half_a_window_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            settings.update({"renew_window_start": "2", "renew_window_end": ""})
        self.assertEqual(set(cm.exception.args[0].values()), {"window_incomplete"})
        self.assertIsNone(settings.raw_get("renew_window_start"))
        with self.assertRaises(ValueError):
            settings.update({"renew_window_end": "6"})
        settings.update({"renew_window_start": "2", "renew_window_end": "6"})
        self.assertEqual(settings.renew_window(), (2, 6))
        with self.assertRaises(ValueError):
            settings.update({"renew_window_end": ""})          # retirer une borne seulement
        settings.update({"renew_window_start": "", "renew_window_end": ""})
        self.assertIsNone(settings.renew_window())
        # sans toucher à la plage, un autre réglage passe toujours
        settings.raw_set("renew_window_start", "2")
        settings.update({"renew_before_hours": 48})
        settings.update({"renew_before_hours": None})

    def test_window_incomplete_over_the_api(self):
        r = self.client.patch("/api/settings", headers=HEADERS, json={"renew_window_start": "3"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("window_incomplete", json.dumps(r.json()))

    # -- restauration : sessions déconnectées conservées
    def test_restore_keeps_revoked_sessions(self):
        cookie = auth.make_cookie()
        sid = auth.read_cookie(cookie)["sid"]
        auth.revoke(sid)
        self.assertIsNone(auth.read_cookie(cookie))
        archive = self.client.get("/api/backup/export")
        self.assertEqual(archive.status_code, 200)
        r = self.client.post("/api/backup/import", headers=HEADERS,
                             files={"file": ("b.tvsign-backup", archive.content, "application/zip")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(auth.read_cookie(cookie), "la session déconnectée reste révoquée après restauration")
        self.assertTrue(db.row("SELECT 1 FROM revoked_sessions WHERE sid = ?", sid))

    # -- cookie d'avant les sessions individuelles
    def test_sign_out_revokes_a_cookie_without_session_id(self):
        import base64

        def legacy(salt):
            payload = base64.urlsafe_b64encode(json.dumps(
                {"v": auth._version(), "iat": int(time.time()) - salt}).encode()).decode().rstrip("=")
            return f"{payload}.{auth._sign(payload.encode())}"
        mine, other = legacy(1), legacy(2)
        self.assertIsNotNone(auth.read_cookie(mine))
        self.client.cookies.set(auth.COOKIE, mine)
        try:
            r = self.client.post("/api/auth/logout", headers=HEADERS)
        finally:
            self.client.cookies.clear()
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(auth.read_cookie(mine), "un cookie copié ne vaut plus rien")
        self.assertIsNotNone(auth.read_cookie(other), "les autres sessions restent ouvertes")

    # -- interface et déploiement
    def test_new_interface_strings_and_hooks(self):
        i18n = (ROOT / "web" / "i18n.js").read_text(encoding="utf-8")
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for key in ("err_window_incomplete", "st_window", "retry_window", "set_lbl_renew_window_start",
                    "set_lbl_renew_window_end"):
            self.assertRegex(i18n, rf'(?m)^\s+{key}: \["[^"]+",\s*"[^"]+"\]', key)
        self.assertIn("window_wait", js)
        self.assertIn("libelleReglage", (ROOT / "web" / "errors.js").read_text(encoding="utf-8"))
        self.assertIn('$("#backup-export").onclick', js)

    def test_anisette_service_uses_a_named_volume_and_a_pinned_image(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("./anisette-data", compose)
        self.assertRegex(compose, r"(?m)^volumes:\s*\n\s+anisette_data:")
        self.assertNotIn("anisette-v3-server:latest", compose)
        self.assertRegex(compose, r"anisette-v3-server:[\w.-]+@sha256:[0-9a-f]{64}")


if __name__ == "__main__":
    unittest.main()
