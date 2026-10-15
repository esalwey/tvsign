"""Apple TV : plusieurs TV par app, adresse saisie à la main, appairage robuste, app vraiment installée.

    python -m unittest discover -s tests

Aucune Apple TV ni plumesign : les appels à plumesign sont remplacés par des doubles.
"""
import asyncio
import io
import os
import plistlib
import re
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

DATA = tempfile.mkdtemp(prefix="tvsign-test-")
os.environ.update(TVSIGN_DATA=DATA, TVSIGN_AUTH="none", TVSIGN_PLUMESIGN=str(Path(DATA) / "absent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, devices, diag, main, plume, proc, renew  # noqa: E402
from app.proc import Result, Session  # noqa: E402

HEADERS = {"X-Tvsign": "1"}
_N = [0]


def setUpModule():
    db.init()


def make_ipa(bundle="com.example.multi") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Payload/Test.app/Info.plist", plistlib.dumps({
            "CFBundleName": "Test", "CFBundleIdentifier": bundle, "CFBundleShortVersionString": "1.0",
            "DTPlatformName": "appletvos"}))
    return buf.getvalue()


def make_device(name="Salon", **over) -> int:
    _N[0] += 1
    fields = dict(name=name, pair_identifier=f"UDID-{_N[0]}", udid=f"UDID-{_N[0]}", pairing_udid=f"UDID-{_N[0]}",
                  pair_host="tvsign", created_at=time.time(), os_version="18.0")
    fields.update(over)
    cols = ", ".join(fields)
    return db.execute(f"INSERT INTO devices ({cols}) VALUES ({', '.join('?' * len(fields))})", *fields.values())


def make_app(device_id, **over) -> int:
    fields = dict(name="Nuvio", source_type="upload", bundle_id="com.example.app", status="ok", enabled=1,
                  device_id=device_id, signed_at=time.time() - 7200, expires_at=time.time() + 5 * 86400,
                  created_at=time.time(), installed_bundle="com.example.app.TEAM")
    fields.update(over)
    cols = ", ".join(fields)
    return db.execute(f"INSERT INTO apps ({cols}) VALUES ({', '.join('?' * len(fields))})", *fields.values())


class Calls:
    """Remplace renew.spawn : note les renouvellements demandés sans rien lancer."""

    def __enter__(self):
        self.calls = []
        self._patch = mock.patch.object(renew, "spawn", lambda app_id, trigger="manuel": self.calls.append((app_id, trigger)) or True)
        self._patch.start()
        return self.calls

    def __exit__(self, *exc):
        self._patch.stop()


# ---------- oublier une TV ----------

class Forget(unittest.TestCase):
    def forget(self, result):
        async def fake_run(*a, **k):
            return result
        with mock.patch.object(devices, "run", fake_run):
            return asyncio.run(devices.forget({"name": "Salon", "pairing_udid": "X"}))

    def test_missing_pairing_on_stderr_is_not_an_error(self):
        # plumesign écrit « Error: No stored pairing for X » sur stderr et sort avec 1
        self.forget(Result(1, "", "Error: No stored pairing for X"))

    def test_other_failure_is_reported(self):
        with self.assertRaises(RuntimeError):
            self.forget(Result(1, "", "Error: permission denied"))

    def test_plumesign_that_cannot_start(self):
        async def boom(*a, **k):
            raise FileNotFoundError("plumesign.exe")
        with mock.patch.object(devices, "run", boom), self.assertRaises(RuntimeError):
            asyncio.run(devices.forget({"name": "Salon", "pairing_udid": "X"}))


class DeviceRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def test_delete_when_pairing_is_already_gone(self):
        dev = make_device("Perdue", pairing_lost=1)

        async def fake_run(*a, **k):
            return Result(1, "", "Error: No stored pairing for UDID")
        with mock.patch.object(devices, "run", fake_run):
            r = self.client.delete(f"/api/devices/{dev}", headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(db.row("SELECT id FROM devices WHERE id = ?", dev))

    def test_delete_failure_then_forced(self):
        dev = make_device("Bloquée")

        async def fake_run(*a, **k):
            return Result(1, "", "Error: access denied")
        with mock.patch.object(devices, "run", fake_run):
            self.assertEqual(self.client.delete(f"/api/devices/{dev}", headers=HEADERS).status_code, 502)
            self.assertIsNotNone(db.row("SELECT id FROM devices WHERE id = ?", dev))
            self.assertEqual(self.client.delete(f"/api/devices/{dev}?force=1", headers=HEADERS).status_code, 200)
        self.assertIsNone(db.row("SELECT id FROM devices WHERE id = ?", dev))

    def test_delete_refused_while_a_renewal_runs(self):
        dev = make_device("Occupée")
        app_id = make_app(dev)
        renew._RUNNING.add(app_id)
        try:
            r = self.client.delete(f"/api/devices/{dev}", headers=HEADERS)
            self.assertEqual((r.status_code, r.json()["detail"]), (409, "device_busy"))
        finally:
            renew._RUNNING.discard(app_id)
        self.assertIsNotNone(db.row("SELECT id FROM devices WHERE id = ?", dev))

    def test_apps_of_unknown_device_is_404(self):
        r = self.client.get("/api/devices/99999/apps")
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "unknown_device"))

    def test_busy_device_answers_409(self):
        dev = make_device("Installe")

        async def busy(*a, **k):
            raise devices.Busy("occupée")
        with mock.patch.object(devices, "refresh_info", busy), mock.patch.object(devices, "installed_apps", busy):
            for r in (self.client.post(f"/api/devices/{dev}/refresh", headers=HEADERS),
                      self.client.get(f"/api/devices/{dev}/apps")):
                self.assertEqual((r.status_code, r.json()["detail"]), (409, "device_busy"))

    def test_state_exposes_platform_and_busy(self):
        make_device("Etat")
        s = self.client.get("/api/state").json()
        self.assertIn("windows", s)
        self.assertTrue(all("busy" in d for d in s["devices"]))

    def test_manual_address_on_a_device(self):
        dev = make_device("Adresse")
        url = f"/api/devices/{dev}"
        self.assertEqual(self.client.patch(url, headers=HEADERS, json={"host": "192.168.1.20"}).status_code, 200)
        self.assertEqual(db.row("SELECT host_hint FROM devices WHERE id = ?", dev)["host_hint"], "192.168.1.20")
        r = self.client.patch(url, headers=HEADERS, json={"host": "not an address!"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "invalid_host"))
        self.assertEqual(self.client.patch(url, headers=HEADERS, json={"host": ""}).status_code, 200)
        self.assertIsNone(db.row("SELECT host_hint FROM devices WHERE id = ?", dev)["host_hint"])
        # renommer sans toucher à l'adresse (ancien format du corps)
        self.client.patch(url, headers=HEADERS, json={"host": "tv.lan"})
        self.client.patch(url, headers=HEADERS, json={"name": "Chambre"})
        row = db.row("SELECT name, host_hint FROM devices WHERE id = ?", dev)
        self.assertEqual((row["name"], row["host_hint"]), ("Chambre", "tv.lan"))

    def test_relocate_saves_the_address_even_when_the_tv_sleeps(self):
        dev = make_device("Dort")

        async def asleep(device_id, wait=True):
            raise devices.Unreachable("en veille")
        with mock.patch.object(devices, "refresh_info", asleep):
            r = self.client.post(f"/api/devices/{dev}/relocate", headers=HEADERS, json={"host": "10.0.0.5"})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(db.row("SELECT host_hint FROM devices WHERE id = ?", dev)["host_hint"], "10.0.0.5")

        async def found(device_id, wait=True):
            db.update("devices", device_id, last_ip="10.0.0.5", last_port=49152)
            return {}
        with mock.patch.object(devices, "refresh_info", found):
            r = self.client.post(f"/api/devices/{dev}/relocate", headers=HEADERS, json={})
        self.assertEqual((r.status_code, r.json()["ip"]), (200, "10.0.0.5"))

    def test_discover_with_an_address(self):
        seen = {}

        async def fake_discover(seconds=6, ip=None):
            seen["ip"] = ip
            return [{"kind": "pairable", "name": "Salon", "identifier": "zzz", "ip": ip, "port": 49999}]
        with mock.patch.object(devices.plume, "discover", fake_discover):
            r = self.client.post("/api/devices/discover", headers=HEADERS, json={"host": "192.168.1.20"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(seen["ip"], "192.168.1.20")
            self.assertEqual(r.json()["pairable"][0]["port"], 49999)
            self.assertEqual(self.client.post("/api/devices/discover", headers=HEADERS, json={"host": "bad host"}).status_code, 400)
            self.client.post("/api/devices/discover", headers=HEADERS)      # sans corps : recherche multicast
            self.assertIsNone(seen["ip"])

    def test_pairing_when_plumesign_cannot_start_fails_at_once(self):
        r = self.client.post("/api/devices/pair", headers=HEADERS, json={"ip": "192.168.1.9", "port": 49000, "name": "Test"})
        self.assertEqual(r.status_code, 200, r.text)
        sid = r.json()["id"]
        for _ in range(50):
            s = self.client.get(f"/api/sessions/{sid}").json()
            if s["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(s["state"], "failed")
        self.assertTrue(s["log"], "le message d'erreur doit être visible")
        self.assertFalse(proc.busy(), "une session en échec ne doit pas bloquer l'import de sauvegarde")


# ---------- sessions et tâches de fond ----------

class SessionLaunch(unittest.TestCase):
    def test_start_failure_marks_the_session_failed(self):
        seen = []

        async def on_exit(s):
            seen.append(s.state)
        s = Session("pair", re.compile("never"))
        asyncio.run(s.start(str(Path(DATA) / "does-not-exist"), on_exit=on_exit))
        self.assertEqual(s.state, "failed")
        self.assertEqual(len(s.lines), 1)
        self.assertEqual(seen, ["failed"])
        self.assertFalse(s.alive)

    def test_unhandled_background_error_is_logged(self):
        async def boom():
            raise ValueError("oups")

        async def go():
            proc.spawn(boom())
            await asyncio.sleep(0.05)
        with mock.patch("builtins.print") as p:
            asyncio.run(go())
        self.assertTrue(any("oups" in str(c) for c in p.call_args_list))


# ---------- plusieurs TV pour une même app ----------

class SeveralTvs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def add(self, device_id, bundle="com.example.multi"):
        data = {"source_type": "upload", "device_id": str(device_id)}
        return self.client.post("/api/apps", headers=HEADERS, data=data,
                                files={"file": ("a.ipa", make_ipa(bundle), "application/octet-stream")})

    def test_same_app_on_two_tvs_but_not_twice_on_one(self):
        salon, chambre = make_device("Salon"), make_device("Chambre")
        with Calls():
            first = self.add(salon, "com.example.multi1")
            self.assertEqual(first.status_code, 200, first.text)
            second = self.add(chambre, "com.example.multi1")
            self.assertEqual(second.status_code, 200, second.text)
            self.assertNotEqual(first.json()["id"], second.json()["id"])
            again = self.add(salon, "com.example.multi1")
            self.assertEqual((again.status_code, again.json()["detail"]), (409, "already_added:Salon"))

    def test_preset_style_duplicate_is_refused_before_downloading(self):
        salon = make_device("Salon2")
        make_app(salon, source_type="github", source="o/r", bundle_id="com.x.pre", account_email="a@b.c")
        with mock.patch.object(renew, "refresh_source", side_effect=AssertionError("téléchargement")):
            r = self.client.post("/api/apps", headers=HEADERS,
                                 data={"source_type": "github", "source": "o/r", "device_id": str(salon),
                                       "account_email": "a@b.c"})
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "already_added:Salon2"))

    def test_copy_to_another_tv(self):
        salon, chambre = make_device("SalonC"), make_device("ChambreC")
        with Calls() as calls:
            a = self.add(salon, "com.example.copy").json()
            r = self.client.post(f"/api/apps/{a['id']}/copy", headers=HEADERS, json={"device_id": chambre})
            self.assertEqual(r.status_code, 200, r.text)
            copy = r.json()
            self.assertEqual((copy["device_id"], copy["bundle_id"], copy["enabled"]), (chambre, "com.example.copy", 1))
            self.assertTrue((renew.app_dir(copy["id"]) / "original.ipa").exists())
            self.assertIn((copy["id"], "ajout"), calls)
            # déjà là, et la TV d'origine : refusé
            r = self.client.post(f"/api/apps/{a['id']}/copy", headers=HEADERS, json={"device_id": chambre})
            self.assertEqual((r.status_code, r.json()["detail"]), (409, "already_added:ChambreC"))
            self.assertEqual(self.client.post(f"/api/apps/{a['id']}/copy", headers=HEADERS,
                                              json={"device_id": salon}).status_code, 409)
            self.assertEqual(self.client.post(f"/api/apps/{a['id']}/copy", headers=HEADERS,
                                              json={"device_id": 99999}).status_code, 400)

    def test_copy_of_an_app_still_being_added_is_refused(self):
        salon, chambre = make_device("SalonP"), make_device("ChambreP")
        half = make_app(salon, enabled=0, bundle_id=None)
        nobundle = make_app(salon, bundle_id=None)
        for app_id in (half, nobundle):
            r = self.client.post(f"/api/apps/{app_id}/copy", headers=HEADERS, json={"device_id": chambre})
            self.assertEqual((r.status_code, r.json()["detail"]), (409, "app_not_ready"))

    def test_failed_copy_does_not_leak_server_paths(self):
        salon, chambre = make_device("SalonF"), make_device("ChambreF")
        with Calls():
            a = self.add(salon, "com.example.copyfail").json()
            with mock.patch.object(main.shutil, "copy2", side_effect=OSError("/data/apps/9/original.ipa: disque plein")):
                r = self.client.post(f"/api/apps/{a['id']}/copy", headers=HEADERS, json={"device_id": chambre})
        self.assertEqual((r.status_code, r.json()["detail"]), (500, "copy_failed"))
        self.assertNotIn("/data/", r.text)
        self.assertIsNone(db.row("SELECT id FROM apps WHERE device_id = ?", chambre))      # fiche à moitié créée retirée
        self.assertTrue(any("disque plein" in e["message"] for e in db.rows("SELECT message FROM events")))

    def test_moving_an_app_to_a_tv_that_has_it_is_refused(self):
        salon, chambre = make_device("SalonM"), make_device("ChambreM")
        with Calls():
            a = self.add(salon, "com.example.move").json()
            self.add(chambre, "com.example.move")
            r = self.client.patch(f"/api/apps/{a['id']}", headers=HEADERS, json={"device_id": chambre})
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "already_added:ChambreM"))
        self.assertEqual(db.row("SELECT device_id FROM apps WHERE id = ?", a["id"])["device_id"], salon)


# ---------- adresse saisie à la main ----------

class ManualAddress(unittest.TestCase):
    def test_clean_host(self):
        self.assertIsNone(devices.clean_host("  "))
        self.assertIsNone(devices.clean_host(None))
        self.assertEqual(devices.clean_host(" 192.168.1.20 "), "192.168.1.20")
        self.assertEqual(devices.clean_host("salon.lan"), "salon.lan")
        self.assertEqual(devices.clean_host("fe80::1%eth0"), "fe80::1%eth0")
        for bad in ("0.0.0.0", "127.0.0.1", "224.0.0.251", "a b", "-x.lan", "http://tv", "a" * 300):
            with self.assertRaises(ValueError, msg=bad):
                devices.clean_host(bad)

    def test_ip_is_not_resolved_names_are(self):
        self.assertEqual(asyncio.run(devices.resolve_host("10.1.2.3")), "10.1.2.3")
        self.assertIsNone(asyncio.run(devices.resolve_host(None)))
        self.assertEqual(asyncio.run(devices.resolve_host("localhost")) in ("127.0.0.1", "::1"), True)

    def test_zone_is_stripped_before_plumesign_sees_the_address(self):
        self.assertEqual(asyncio.run(devices.resolve_host("fe80::1%eth0")), "fe80::1")
        self.assertEqual(asyncio.run(devices.resolve_host("10.1.2.3%eth0")), "10.1.2.3")

    def test_discover_passes_a_zone_free_ip(self):
        seen = []

        async def fake_run(*args, **k):
            seen.append([str(a) for a in args])
            return Result(0, "")
        with mock.patch.object(plume, "run", fake_run):
            try:
                asyncio.run(plume.discover(1, ip=asyncio.run(devices.resolve_host("fe80::1%eth0"))))
            except Exception:  # noqa: BLE001 - seule la ligne de commande compte ici
                pass
        flat = [a for call in seen for a in call]
        self.assertIn("fe80::1", flat)
        self.assertFalse(any("%" in a for a in flat))

    def test_hint_prefers_manual_address_then_last_ip(self):
        dev = {"host_hint": "10.9.9.9", "last_ip": "192.168.1.7"}
        self.assertEqual(asyncio.run(devices.hint_ip(dev)), "10.9.9.9")
        self.assertEqual(asyncio.run(devices.hint_ip({"host_hint": None, "last_ip": "192.168.1.7"})), "192.168.1.7")
        self.assertIsNone(asyncio.run(devices.hint_ip({})))
        with mock.patch.object(devices, "UNICAST_HINT", False):
            self.assertIsNone(asyncio.run(devices.hint_ip(dev)))

    def wireless(self, results, device):
        calls = []

        async def fake_run(*args, **k):
            calls.append([str(a) for a in args])
            return results.pop(0)
        with mock.patch.object(devices, "run", fake_run):
            res = asyncio.run(devices._wireless(device, "check", timeout=5))
        return res, calls

    def test_targets_carry_the_hint(self):
        dev = {"id": make_device("Hint"), "name": "Hint", "pairing_udid": "U1", "pair_host": "tvsign",
               "last_ip": "192.168.1.7", "host_hint": None}
        _, calls = self.wireless([Result(0, "REACHABLE a b c d")], dev)
        self.assertEqual(calls[0][calls[0].index("--hint-ip") + 1], "192.168.1.7")

    def test_old_plumesign_without_the_hint_option_still_works(self):
        dev = {"id": make_device("Vieux"), "name": "Vieux", "pairing_udid": "U1", "pair_host": "tvsign",
               "last_ip": "192.168.1.7", "host_hint": None}
        old = Result(2, "", "error: unexpected argument '--hint-ip' found")
        res, calls = self.wireless([old, Result(0, "REACHABLE a b c d")], dev)
        self.assertTrue(res.ok)
        self.assertIn("--hint-ip", calls[0])
        self.assertNotIn("--hint-ip", calls[1])


# ---------- l'app est-elle bien sur la TV ? ----------

def clean_tables():
    """Table rase : ces contrôles parcourent toutes les TV et toutes les apps."""
    db.execute("DELETE FROM apps")
    db.execute("DELETE FROM devices")


class Installed(unittest.TestCase):
    def setUp(self):
        clean_tables()

    def installed(self, answers):
        async def fake(device, bundle_id=None, wait=True, **k):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        return mock.patch.object(devices, "installed_apps", fake)

    def confirm(self, answers, bundle="com.x"):
        async def fast(_):
            return None
        with self.installed(answers), mock.patch.object(devices.asyncio, "sleep", fast):
            return asyncio.run(devices.confirm_installed({"id": 1, "name": "Salon"}, bundle))

    def test_present(self):
        self.assertIsNotNone(self.confirm([[{"bundle_id": "COM.X"}]]))

    def test_appears_a_moment_later(self):
        self.assertIsNotNone(self.confirm([[], [{"bundle_id": "com.x"}]]))

    def test_confirmed_absent(self):
        with self.assertRaises(devices.NotInstalled):
            self.confirm([[{"bundle_id": "com.other"}], []])

    def test_unverifiable_is_not_a_failure(self):
        self.assertIsNone(self.confirm([devices.Unreachable("veille")]))
        self.assertIsNone(self.confirm([], bundle=None))

    def audit(self, answer, **app_over):
        dev = make_device("Audit")
        app_id = make_app(dev, **app_over)
        devices._AUDIT_NEXT.pop(dev, None)

        async def fake(device, bundle_id=None, wait=True, **k):
            if isinstance(answer, Exception):
                raise answer
            return answer
        with Calls() as calls, mock.patch.object(devices, "installed_apps", fake):
            asyncio.run(devices.audit_installed())
        return dev, app_id, calls

    def test_audit_keeps_a_present_app_valid(self):
        dev, app_id, calls = self.audit([{"bundle_id": "com.example.app.TEAM"}])
        row = db.row("SELECT * FROM apps WHERE id = ?", app_id)
        self.assertEqual(row["status"], "ok")
        self.assertIsNotNone(row["verified_at"])
        self.assertEqual(calls, [])

    def test_audit_flags_an_app_the_tv_no_longer_has(self):
        dev, app_id, calls = self.audit([{"bundle_id": "com.other"}])
        row = db.row("SELECT * FROM apps WHERE id = ?", app_id)
        self.assertEqual(row["status"], "error")
        self.assertIn("n'est plus installée", row["last_error"])
        self.assertIsNone(row["next_attempt_at"])
        self.assertEqual(calls, [(app_id, "app absente")])

    def test_audit_skips_a_sleeping_tv(self):
        dev, app_id, calls = self.audit(devices.Unreachable("veille"))
        self.assertEqual(db.row("SELECT status FROM apps WHERE id = ?", app_id)["status"], "ok")
        self.assertEqual(calls, [])
        self.assertGreater(devices._AUDIT_NEXT[dev], time.time())     # revue plus tard, pas à chaque passage

    def test_audit_of_a_sleeping_tv_leaves_no_error_on_its_card(self):
        dev = make_device("Dort")
        make_app(dev)
        devices._AUDIT_NEXT.pop(dev, None)

        async def fake_run(*args, **k):
            return Result(devices.EXIT_NOT_FOUND, "", "ERROR NOT_FOUND introuvable")
        with mock.patch.object(devices, "run", fake_run):
            asyncio.run(devices.audit_installed())
        row = db.row("SELECT last_error, last_error_at FROM devices WHERE id = ?", dev)
        self.assertEqual((row["last_error"], row["last_error_at"]), (None, None))
        self.assertGreater(devices._AUDIT_NEXT[dev], time.time())     # revue dans une heure

    def test_a_normal_probe_still_records_the_failure(self):
        dev = make_device("Dort2")
        device = db.row("SELECT * FROM devices WHERE id = ?", dev)

        async def fake_run(*args, **k):
            return Result(devices.EXIT_NOT_FOUND, "", "ERROR NOT_FOUND introuvable")
        with mock.patch.object(devices, "run", fake_run):
            with self.assertRaises(devices.Unreachable):
                asyncio.run(devices.installed_apps(device))
        self.assertTrue(db.row("SELECT last_error FROM devices WHERE id = ?", dev)["last_error"])

    def test_audit_ignores_a_fresh_install_and_running_apps(self):
        dev, app_id, calls = self.audit([], signed_at=time.time() - 30)
        self.assertEqual(db.row("SELECT status FROM apps WHERE id = ?", app_id)["status"], "ok")

    def test_audit_can_be_disabled(self):
        with mock.patch.object(devices, "INSTALL_CHECK_HOURS", 0):
            dev, app_id, calls = self.audit([{"bundle_id": "com.other"}])
        self.assertEqual(db.row("SELECT status FROM apps WHERE id = ?", app_id)["status"], "ok")


# ---------- appairage ----------

class Pairing(unittest.TestCase):
    def test_apps_in_error_are_retried_after_a_new_pairing(self):
        dev = make_device("Reappairee")
        soon = time.time() + 2700
        lost = make_app(dev, status="error", next_attempt_at=soon)
        fine = make_app(dev, status="ok", next_attempt_at=None)
        paused = make_app(dev, status="error", enabled=0, next_attempt_at=soon)
        elsewhere = make_app(make_device("Autre"), status="error", next_attempt_at=soon)
        with Calls() as calls:
            devices.retry_apps(dev)
        self.assertEqual(calls, [(lost, "nouvel appairage")])
        self.assertIsNone(db.row("SELECT next_attempt_at FROM apps WHERE id = ?", lost)["next_attempt_at"])
        self.assertEqual(db.row("SELECT next_attempt_at FROM apps WHERE id = ?", paused)["next_attempt_at"], soon)
        self.assertEqual(db.row("SELECT next_attempt_at FROM apps WHERE id = ?", elsewhere)["next_attempt_at"], soon)
        self.assertEqual(db.row("SELECT status FROM apps WHERE id = ?", fine)["status"], "ok")

    def test_pairing_lost_is_detected_and_cleared(self):
        dev = make_device("Store", pairing_udid="STORE-1")
        row = lambda: db.row("SELECT * FROM devices WHERE id = ?", dev)    # noqa: E731
        devices._reconcile(row(), {})
        self.assertEqual((row()["pairing_lost"], row()["last_error"]), (1, devices.STORE_MISSING))
        devices._reconcile(row(), {"store-1": {}})          # remis dans le store (sauvegarde restaurée)
        self.assertEqual((row()["pairing_lost"], row()["last_error"]), (0, None))

    def test_a_pairing_lost_for_another_reason_is_not_cleared_by_the_store(self):
        dev = make_device("Perdue2", pairing_udid="STORE-2", pairing_lost=1, last_error="appairage perdu : TV réinitialisée")
        devices._reconcile(db.row("SELECT * FROM devices WHERE id = ?", dev), {"store-2": {}})
        self.assertEqual(db.row("SELECT pairing_lost FROM devices WHERE id = ?", dev)["pairing_lost"], 1)


# ---------- renouvellement : identité de la TV, boucle par TV ----------

class RenewCallSites(unittest.TestCase):
    def test_unverified_tv_identity_is_read_before_registering_the_udid(self):
        dev = make_device("Inconnue", os_version=None)
        app_id = make_app(dev, status="pending", account_email="me@example.com", expires_at=None)
        calls = []

        async def info(device_id, wait=True):
            calls.append("info")
            raise devices.Unreachable("stop")

        async def check(device, wait=True):
            calls.append("check")
        with mock.patch.object(devices, "refresh_info", info), mock.patch.object(devices, "check_reachable", check), \
                mock.patch.object(renew.plume, "accounts", lambda: [{"email": "me@example.com", "ok": 1}]), \
                mock.patch.object(renew, "alert", mock.AsyncMock()):
            asyncio.run(renew._renew(app_id, "manuel"))
        self.assertEqual(calls, ["info"])

    def test_a_known_tv_is_only_checked(self):
        dev = make_device("Connue")
        app_id = make_app(dev, status="pending", account_email="me@example.com", expires_at=None)
        calls = []

        async def check(device, wait=True):
            calls.append("check")
            raise devices.Unreachable("stop")
        with mock.patch.object(devices, "check_reachable", check), \
                mock.patch.object(renew.plume, "accounts", lambda: [{"email": "me@example.com", "ok": 1}]), \
                mock.patch.object(renew, "alert", mock.AsyncMock()):
            asyncio.run(renew._renew(app_id, "manuel"))
        self.assertEqual(calls, ["check"])

    def test_app_in_use_is_not_retried_as_a_transient_failure(self):
        dev = make_device("Ouverte")
        app_id = make_app(dev, status="pending", account_email="me@example.com", expires_at=None)

        async def check(device, wait=True):
            raise devices.AppInUse("app ouverte")
        with mock.patch.object(devices, "check_reachable", check), \
                mock.patch.object(renew.plume, "accounts", lambda: [{"email": "me@example.com", "ok": 1}]), \
                mock.patch.object(renew, "alert", mock.AsyncMock()):
            before = time.time()
            asyncio.run(renew._renew(app_id, "manuel"))
        retry = db.row("SELECT next_attempt_at FROM apps WHERE id = ?", app_id)["next_attempt_at"]
        self.assertGreater(retry - before, 40 * 60)       # le délai normal, pas les 15 min d'une panne passagère

    def test_loop_pass_renews_a_due_app(self):
        dev = make_device("Boucle")
        app_id = make_app(dev, status="error", expires_at=time.time() + 3600)
        seen = []

        async def fake_renew(i, trigger="manuel"):
            seen.append((i, trigger))
        with mock.patch.object(renew, "renew", fake_renew), mock.patch.object(renew, "STOPPING", False):
            asyncio.run(renew._loop_app(app_id, False))
        self.assertEqual(seen, [(app_id, "auto")])


class QueuesPerTv(unittest.TestCase):
    """Une file par Apple TV, qui continue d'un passage à l'autre : une TV lente ne retient pas les autres."""

    def setUp(self):
        clean_tables()
        renew._QUEUES.clear()
        renew._RELEASES_MISSED.clear()

    def tearDown(self):
        renew._QUEUES.clear()
        renew._RELEASES_MISSED.clear()

    def test_a_waiting_tv_does_not_hold_back_the_others(self):
        tv_a, tv_b = make_device("A"), make_device("B")
        app_a = make_app(tv_a, status="error", expires_at=time.time() + 3600)
        app_b = make_app(tv_b, status="error", expires_at=time.time() + 3600)
        seen = []

        async def scenario():
            gate = asyncio.Event()

            async def fake_renew(i, trigger="manuel"):
                seen.append(i)
                if i == app_a:
                    await gate.wait()           # la TV A attend qu'on ferme une app
            with mock.patch.object(renew, "renew", fake_renew), mock.patch.object(renew, "STOPPING", False):
                first = renew.start_queues(False)
                await asyncio.sleep(0.05)
                self.assertEqual(sorted(seen), sorted([app_a, app_b]))
                # passage suivant : la file de A tourne encore, elle n'est pas relancée ; celle de B repart
                second = renew.start_queues(False)
                await asyncio.sleep(0.05)
                self.assertEqual((seen.count(app_a), seen.count(app_b)), (1, 2))
                self.assertEqual(len([t for t in second if not t.done()]), 1)     # seule la file de A est encore en cours
                gate.set()
                await asyncio.gather(*first)
        asyncio.run(scenario())

    def test_a_crashing_queue_does_not_stop_the_others(self):
        tv_a, tv_b = make_device("A2"), make_device("B2")
        app_a, app_b = make_app(tv_a), make_app(tv_b)
        seen = []

        async def fake_loop_app(app_id, check_releases):
            if app_id == app_a:
                raise RuntimeError("base de données verrouillée")
            seen.append(app_id)

        async def scenario():
            with mock.patch.object(renew, "_loop_app", fake_loop_app), mock.patch.object(renew, "STOPPING", False):
                tasks = renew.start_queues(False)
                await asyncio.gather(*tasks)           # ne lève rien : l'erreur est journalisée
        asyncio.run(scenario())
        self.assertEqual(seen, [app_b])
        self.assertIn("verrouillée", renew.HEALTH["loop_error"])

    def test_release_check_is_not_lost_when_the_queue_was_busy(self):
        tv = make_device("Lente")
        make_app(tv)
        flags = []

        async def scenario():
            gate = asyncio.Event()

            async def fake_loop_app(i, check_releases):
                flags.append(check_releases)
                await gate.wait()
            with mock.patch.object(renew, "_loop_app", fake_loop_app), mock.patch.object(renew, "STOPPING", False):
                first = renew.start_queues(False)
                await asyncio.sleep(0.02)
                renew.start_queues(True)               # file occupée : la vérification est mise de côté
                self.assertIn(tv, renew._RELEASES_MISSED)
                gate.set()
                await asyncio.gather(*first)
                gate.clear()
                again = renew.start_queues(False)      # libre : la vérification manquée est faite maintenant
                await asyncio.sleep(0.02)
                gate.set()
                await asyncio.gather(*again)
        asyncio.run(scenario())
        self.assertEqual(flags, [False, True])


# ---------- diagnostic réseau ----------

class NetworkDiagnostic(unittest.TestCase):
    def setUp(self):
        clean_tables()

    def mdns(self, found, direct=0):
        async def fake_discover(seconds=6, ip=None):
            return found if ip is None else [{}] * direct
        make_device("Diag", last_ip="192.168.1.50")
        with mock.patch.object(diag.plume, "discover", fake_discover):
            return asyncio.run(diag._mdns())

    def test_announcements_are_fine(self):
        r = self.mdns([{"kind": "paired", "udid": "u"}])
        self.assertEqual(r["status"], "ok")

    def test_blocked_multicast_but_tv_answers_directly(self):
        r = self.mdns([], direct=1)
        self.assertEqual((r["status"], r["value"]["direct"]), ("warn", 1))

    def test_nothing_at_all(self):
        r = self.mdns([])
        self.assertEqual((r["status"], r["value"]["direct"]), ("warn", 0))

    def test_not_a_docker_bridge_here(self):
        self.assertFalse(diag._bridge_network())


# ---------- migration ----------

class Migration(unittest.TestCase):
    def test_existing_install_keeps_its_data(self):
        path = Path(tempfile.mkdtemp(prefix="tvsign-mig-")) / "tvsign.db"
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT NOT NULL, pair_identifier TEXT NOT NULL UNIQUE,
                udid TEXT, model TEXT, os_version TEXT, last_ip TEXT, last_port INTEGER, last_seen REAL,
                created_at REAL NOT NULL);
            CREATE TABLE apps (id INTEGER PRIMARY KEY, name TEXT NOT NULL, bundle_id TEXT, version TEXT,
                source_type TEXT NOT NULL, source TEXT, source_filter TEXT, source_tag TEXT, account_email TEXT,
                device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL, enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending', last_error TEXT, signed_at REAL, expires_at REAL,
                next_attempt_at REAL, created_at REAL NOT NULL);
            INSERT INTO devices (id, name, pair_identifier, udid, created_at) VALUES (1, 'Salon', 'P1', 'U1', 1);
            INSERT INTO apps (id, name, source_type, device_id, created_at) VALUES (7, 'Nuvio', 'github', 1, 1);
        """)
        old.commit()
        old.close()
        with mock.patch.object(db, "DB_PATH", path):
            db.init()
            db.init()          # relancé : sans effet
            tv = db.row("SELECT * FROM devices WHERE id = 1")
            app = db.row("SELECT * FROM apps WHERE id = 7")
        self.assertEqual((tv["name"], tv["udid"], tv["host_hint"]), ("Salon", "U1", None))
        self.assertEqual((app["name"], app["device_id"], app["verified_at"], app["installed_bundle"]),
                         ("Nuvio", 1, None, None))
        self.assertEqual(len(list(path.parent.glob("tvsign.db.pre-migrate-*"))), 1)     # copie de sécurité


if __name__ == "__main__":
    unittest.main()
