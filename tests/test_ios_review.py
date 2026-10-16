"""iPhone / iPad, suite de la relecture : ligne d'identité de l'appairage USB, adresse d'appairage,
validation des champs d'import, analyse de l'IPA hors de la boucle, textes de l'interface.

    python -m unittest discover -s tests
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

DATA = os.environ.get("TVSIGN_DATA") or tempfile.mkdtemp(prefix="tvsign-test-")
os.environ.setdefault("TVSIGN_DATA", DATA)
os.environ.setdefault("TVSIGN_AUTH", "none")
os.environ.setdefault("TVSIGN_PLUMESIGN", str(Path(DATA) / "absent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, devices, ipa, main  # noqa: E402
from app.proc import Result, Session  # noqa: E402
from test_ios import HEADERS, make_ipa  # noqa: E402

WEB = Path(__file__).resolve().parent.parent / "web"
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
I18N_JS = (WEB / "i18n.js").read_text(encoding="utf-8")


def plumesign_identity(**over) -> str:
    """Ligne d'identité telle que plumesign l'écrit : serde_json trie les clés par ordre alphabétique."""
    info = {"DeviceFamily": "ipados", "DeviceName": "iPad de Zoé", "ProductType": "iPad13,4",
            "ProductVersion": "18.1", "UniqueDeviceID": "00008110-AAAA0000BBBB0042"}
    info.update(over)
    return json.dumps(dict(sorted(info.items())), separators=(",", ":"))


class UsbPairingResult(unittest.TestCase):
    def setUp(self):
        db.init()
        self.saved = devices.refresh_info

        async def no_info(device_id):
            return {}
        devices.refresh_info = no_info

    def tearDown(self):
        devices.refresh_info = self.saved

    def session(self, *lines, state="done") -> Session:
        s = Session("pair", re.compile(r"^\0$"))
        s.lines.extend(lines)
        s.state = state
        return s

    def test_identity_line_is_found_whatever_the_key_order(self):
        line = plumesign_identity()
        self.assertTrue(line.startswith('{"DeviceFamily"'))           # pas UniqueDeviceID en premier
        found = devices._identity_line(["Recherche…", line, "PAIRED - 00008110-AAAA0000BBBB0042"])
        self.assertEqual(found["DeviceName"], "iPad de Zoé")
        self.assertEqual(devices._identity_line(["{pas du json UniqueDeviceID", "rien"]), {})
        self.assertEqual(devices._identity_line(['["UniqueDeviceID"]']), {})

    def test_usb_pairing_records_name_model_version_and_ipad_family(self):
        s = self.session(plumesign_identity(), "PAIRED - 00008110-AAAA0000BBBB0042")
        asyncio.run(devices._usb_pairing_done(s))
        row = db.row("SELECT * FROM devices WHERE id = ?", s.result["device_id"])
        self.assertEqual((row["name"], row["model"], row["os_version"], row["family"]),
                         ("iPad de Zoé", "iPad13,4", "18.1", "ipados"))
        self.assertEqual(s.result["family"], "ipados")

    def test_usb_pairing_without_identity_falls_back_to_the_model_free_defaults(self):
        s = self.session("PAIRED - 00008110-AAAA0000BBBB0043")
        asyncio.run(devices._usb_pairing_done(s))
        row = db.row("SELECT * FROM devices WHERE id = ?", s.result["device_id"])
        self.assertEqual((row["name"], row["family"]), ("iPhone", "ios"))

    def test_failed_usb_pairing_creates_nothing(self):
        before = len(db.rows("SELECT id FROM devices"))
        s = self.session("usbmuxd is not reachable (x)", state="failed")
        asyncio.run(devices._usb_pairing_done(s))
        self.assertEqual(len(db.rows("SELECT id FROM devices")), before)


class PairAddress(unittest.TestCase):
    def test_local_and_link_local_addresses_pass(self):
        for ip in ("192.168.1.20", "10.0.0.5", "172.16.3.4", "169.254.1.1", "fe80::1", "fd12:3456::7"):
            self.assertEqual(main._pair_address(ip, 49152), ip, ip)

    def test_global_ipv6_of_an_ipv6_only_lan_passes(self):
        self.assertEqual(main._pair_address("2a01:e0a:1:2::3", 49152), "2a01:e0a:1:2::3")

    def test_unwanted_addresses_are_refused(self):
        for ip in ("8.8.8.8", "127.0.0.1", "::1", "0.0.0.0", "::", "224.0.0.1", "ff02::1", "--version", "tv.local",
                   "", "::ffff:8.8.8.8"):
            with self.assertRaises(HTTPException, msg=ip) as cm:
                main._pair_address(ip, 49152)
            self.assertEqual(cm.exception.detail, "invalid_address")
        for port in (0, 70000):
            with self.assertRaises(HTTPException):
                main._pair_address("192.168.1.2", port)


class ImportFieldChecks(unittest.TestCase):
    def setUp(self):
        db.init()
        self.saved = devices.run
        self.calls = []

        async def fake(*args, **k):
            self.calls.append([str(a) for a in args])
            return Result(1, "ERROR NOT_FOUND no device") if "info" in args else Result(0, "PAIRED - x\n")
        devices.run = fake

    def tearDown(self):
        devices.run = self.saved

    def run_import(self, **kw):
        args = dict(data=b"<plist/>", filename="pairing.plist", udid="00008110-AAAA0000BBBB0101", name="Mon iPhone",
                    model="iPhone15,2", family=None)
        args.update(kw)
        return asyncio.run(devices.import_pairing(**args))

    def code(self, **kw) -> str:
        with self.assertRaises(ValueError) as cm:
            self.run_import(**kw)
        self.assertEqual(self.calls, [], "plumesign ne doit pas être lancé pour une entrée refusée")
        return str(cm.exception)

    def test_udid_must_start_with_an_alphanumeric_character(self):
        self.assertEqual(self.code(udid="-xxxxxxxx"), "invalid_udid")
        self.assertEqual(self.code(udid="--version-1"), "invalid_udid")
        self.assertTrue(devices.valid_udid("00008110-AAAA0000BBBB0101"))
        self.assertFalse(devices.valid_udid("-00008110"))
        self.assertFalse(devices.valid_udid("short"))

    def test_name_and_model_are_bounded(self):
        self.assertEqual(self.code(name="x" * 65), "invalid_name")
        self.assertEqual(self.code(name="a" * 1_000_000), "invalid_name")
        self.assertEqual(self.code(name="bad\nname"), "invalid_name")
        self.assertEqual(self.code(model="m" * 33), "invalid_model")
        self.assertEqual(self.code(model="-iPhone"), "invalid_model")
        self.assertEqual(self.code(model="iPhone;rm"), "invalid_model")

    def test_unknown_family_is_refused_instead_of_ignored(self):
        self.assertEqual(self.code(family="macos"), "invalid_family")
        out = self.run_import(family="ipados", udid="00008110-AAAA0000BBBB0102")
        self.assertEqual(out["family"], "ipados")
        out = self.run_import(family="", udid="00008110-AAAA0000BBBB0103", model="iPad13,4")
        self.assertEqual(out["family"], "ipados")                      # vide : déduite du modèle, comme avant

    def test_name_of_64_characters_is_accepted(self):
        out = self.run_import(name="n" * 64, udid="00008110-AAAA0000BBBB0104")
        self.assertIn("device_id", out)


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def test_import_endpoint_reports_the_new_codes(self):
        r = self.client.post("/api/devices/import-pairing", headers=HEADERS,
                             data={"udid": "00008110-AAAA0000BBBB0201", "family": "macos"},
                             files={"file": ("p.plist", b"<plist/>", "application/octet-stream")})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "invalid_family"))
        r = self.client.post("/api/devices/import-pairing", headers=HEADERS,
                             data={"udid": "-00008110-AAAA"},
                             files={"file": ("p.plist", b"<plist/>", "application/octet-stream")})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "invalid_udid"))
        r = self.client.post("/api/devices/pair-usb", headers=HEADERS, json={"udid": "-00008110-AAAA"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "invalid_udid"))

    def test_app_info_runs_outside_the_event_loop(self):
        """L'analyse d'une IPA (lecture de l'exécutable) ne doit pas bloquer l'interface."""
        seen = []
        real = ipa.app_info

        def spy(path):
            try:
                asyncio.get_running_loop()
                seen.append("loop")
            except RuntimeError:
                seen.append("thread")
            seen.append(threading.current_thread() is not threading.main_thread())
            return real(path)
        ipa.app_info = spy
        try:
            r = self.client.post("/api/apps", headers=HEADERS, data={"source_type": "upload"},
                                 files={"file": ("a.ipa", make_ipa("appletvos", "com.example.review1"),
                                                 "application/octet-stream")})
        finally:
            ipa.app_info = real
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen[0], "thread")


class Interface(unittest.TestCase):
    def test_every_app_card_uses_the_window_of_its_own_device(self):
        helper = "const fenetreRenouv = (a, s) => a.renew_before_hours ?? s.renew_before_hours;"
        self.assertIn(helper, APP_JS)
        rest = APP_JS.replace(helper, "")
        self.assertNotIn("s.renew_before_hours * 3600", rest)
        self.assertIn("fenetreRenouv(a, s) * 3600", rest)

    def test_device_limit_error_only_above_three_apps(self):
        self.assertIn("d.app_count > 3", APP_JS)
        self.assertNotIn("d.app_count >= 3", APP_JS)
        self.assertIn("dev_apps_full", I18N_JS)

    def test_unused_key_is_gone(self):
        self.assertNotIn("ios_old:", I18N_JS)
        self.assertNotIn('"ios_old"', APP_JS)

    def test_wizard_offers_the_phone_route(self):
        self.assertIn('data-pair-kind="phone">${t("wiz_pair_phone")}', APP_JS)
        self.assertIn('t("wiz_no_phone_preset")', APP_JS)

    def test_setup_steps_come_in_a_possible_order(self):
        """Le Mode développeur ne peut être activé qu'après la première installation."""
        entry = re.search(r'phone_steps: \["(.*?)", "(.*?)"\],\r?\n', I18N_JS, re.S)
        for text in entry.groups():
            steps = re.findall(r"<li>(.*?)</li>", text)
            devmode = next(i for i, s in enumerate(steps) if "<b>Developer Mode</b>" in s or "<b>Mode développeur</b>" in s)
            pair = next(i for i, s in enumerate(steps) if "USB" in s and "<b>" in s)
            install = next(i for i, s in enumerate(steps) if "<b>" not in s and ("app" in s))
            self.assertLess(pair, install)
            self.assertLess(install, devmode)
            self.assertEqual(devmode, len(steps) - 1)

    def test_readmes_do_not_ask_for_developer_mode_first(self):
        for rel, marker in (("README.md", "**One-time setup, per device**"),
                            ("README.md", "**Mise en place, une fois par appareil**"),
                            ("windows/README-windows.md", "### Appairer un iPhone")):
            text = (Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")
            section = text.split(marker, 1)[1]
            first = re.search(r"^1\. (.*)$", section, re.M).group(1)
            self.assertNotRegex(first, r"(?i)developer mode|mode développeur", rel)
            self.assertRegex(section[:3000], r"info\.plist" if rel == "README.md" else r"(?i)mode développeur")


if __name__ == "__main__":
    unittest.main()
