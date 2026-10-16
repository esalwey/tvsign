"""Packaging group: update check, alert level exposed to the tray, suspend detection, Job object,
runtime DLL placement, settings and i18n of the new strings.

    python -m unittest discover -s tests
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("TVSIGN_DATA", tempfile.mkdtemp(prefix="tvsign-test-"))
os.environ.setdefault("TVSIGN_AUTH", "none")
os.environ.setdefault("TVSIGN_PLUMESIGN", str(Path(os.environ["TVSIGN_DATA"]) / "absent"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "windows" / "tools"))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main, proc, renew, selfupdate, settings, winjob  # noqa: E402

HEADERS = {"X-Tvsign": "1"}
WINDOWS = os.name == "nt"


class VersionCompare(unittest.TestCase):
    """The single update check lives in app/selfupdate.py; these cases come from the packaging group."""

    def test_ordering(self):
        newer = selfupdate.newer
        self.assertTrue(newer("v1.0.1", "1.0.0"))
        self.assertTrue(newer("1.1.0", "1.0.9"))
        self.assertTrue(newer("2.0.0", "1.99.99"))
        self.assertTrue(newer("1.10.0", "1.9.0"))            # numeric, not alphabetical
        self.assertFalse(newer("1.0.0", "1.0.0"))
        self.assertFalse(newer("1.0.0", "1.0.1"))

    def test_prerelease(self):
        newer = selfupdate.newer
        self.assertTrue(newer("1.0.0", "1.0.0-dev"))          # the release comes after its pre-version
        self.assertTrue(newer("1.0.0", "1.0.0-beta.2"))
        self.assertFalse(newer("1.0.0-beta.1", "1.0.0"))

    def test_unreadable_versions_never_announce(self):
        self.assertFalse(selfupdate.newer("2.0.0", "edge-abc1234"))
        self.assertFalse(selfupdate.newer("nightly", "1.0.0"))
        self.assertFalse(selfupdate.newer(None, "1.0.0"))
        self.assertFalse(selfupdate.newer("1.0.1", None))


class UpdateSetting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)
        renew.STOPPING = False            # the app's shutdown drain sets it; other tests start renewals

    def test_setting_is_exposed_normalised_and_can_be_switched_off(self):
        try:
            s = self.client.get("/api/settings").json()["settings"]["update_check"]
            self.assertEqual((s["value"], s["default"]), ("on", "on"))
            r = self.client.patch("/api/settings", headers=HEADERS, json={"update_check": "off"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertFalse(self.client.get("/api/settings").json()["update"]["enabled"])
            r = self.client.patch("/api/settings", headers=HEADERS, json={"update_check": "maybe"})
            self.assertEqual(r.status_code, 400)
        finally:
            settings.raw_set("update_check", None)

    def test_auth_status_tells_the_edition(self):
        body = self.client.get("/api/auth/status").json()
        self.assertEqual(body["edition"], "windows" if WINDOWS else "docker")

    def test_env_spellings_of_the_setting(self):
        for raw, want in (("0", "off"), ("false", "off"), ("OFF", "off"), ("1", "on"), ("yes", "on")):
            os.environ["TVSIGN_UPDATE_CHECK"] = raw
            try:
                self.assertEqual(settings.get("update_check"), want, raw)
            finally:
                del os.environ["TVSIGN_UPDATE_CHECK"]
        os.environ["TVSIGN_UPDATE_CHECK"] = "perhaps"       # invalid: ignored, default kept
        try:
            self.assertEqual(settings.get("update_check"), "on")
        finally:
            del os.environ["TVSIGN_UPDATE_CHECK"]


class AlertLevel(unittest.TestCase):
    """The tray toasts follow the backend's alert level, which is as quiet as ntfy for a sleeping Apple TV
    (the nature of the last failure is the one failure_fields() records)."""

    NOW = 1_000_000.0
    TV_OFF = "Apple TV « Salon » introuvable sur le réseau (éteinte ou en veille ?) [x]"

    def app(self, **over):
        row = dict(status="error", expires_at=self.NOW + 5 * 86400, last_error=self.TV_OFF, fail_kind="transient",
                   needs_action=None)
        row.update(over)
        return row

    def test_transient_failure_far_from_expiry_is_not_an_alert(self):
        self.assertIsNone(renew.alert_state(self.app(), self.NOW))

    def test_transient_failure_close_to_expiry_is_an_alert(self):
        self.assertEqual(renew.alert_state(self.app(expires_at=self.NOW + 20 * 3600), self.NOW), "24h")
        self.assertEqual(renew.alert_state(self.app(expires_at=self.NOW + 3 * 3600), self.NOW), "6h")

    def test_other_failure_is_an_alert_at_once(self):
        self.assertEqual(renew.alert_state(self.app(last_error="plumesign : boom", fail_kind="other"), self.NOW),
                         "fail")

    def test_session_to_reopen_is_quiet_like_the_account_alert(self):
        self.assertIsNone(renew.alert_state(self.app(fail_kind="session", needs_action="session"), self.NOW))
        self.assertEqual(renew.alert_state(self.app(fail_kind="pairing", needs_action="pairing"), self.NOW), "fail")

    def test_the_rule_is_the_one_of_the_notifications(self):
        # quiet_fail in the failure handler is transient or reconnect: same decision as alert_state
        for exc in (renew.plume.Transient("anisette"), renew.plume.SessionExpired("a@b.c")):
            fields = renew.failure_fields({"expires_at": self.NOW + 5 * 86400}, self.NOW, exc,
                                          renew.is_transient(exc))
            app = self.app(fail_kind=fields["fail_kind"], needs_action=fields["needs_action"])
            self.assertTrue(renew.quiet_failure(app), type(exc).__name__)

    def test_expired_is_always_an_alert(self):
        self.assertEqual(renew.alert_state(self.app(status="ok", expires_at=self.NOW - 1), self.NOW), "expired")

    def test_healthy_and_running_have_no_alert(self):
        self.assertIsNone(renew.alert_state(self.app(status="ok"), self.NOW))
        self.assertIsNone(renew.alert_state(self.app(status="running"), self.NOW))

    def test_state_exposes_the_level(self):
        db.init()
        app_id = db.execute("INSERT INTO apps (name, source_type, created_at, status, last_error, fail_kind, expires_at) "
                            "VALUES ('Quiet', 'upload', 0, 'error', ?, 'transient', ?)", self.TV_OFF,
                            time.time() + 5 * 86400)
        bad_id = db.execute("INSERT INTO apps (name, source_type, created_at, status, last_error, expires_at) "
                            "VALUES ('Loud', 'upload', 0, 'error', 'boom', ?)", time.time() + 5 * 86400)
        try:
            with TestClient(main.app) as client:
                apps = {a["id"]: a for a in client.get("/api/state").json()["apps"]}
            self.assertIsNone(apps[app_id]["alert"])
            self.assertEqual(apps[bad_id]["alert"], "fail")
        finally:
            renew.STOPPING = False
            db.execute("DELETE FROM apps WHERE id IN (?, ?)", app_id, bad_id)


class SuspendDetection(unittest.TestCase):
    def test_on_time_is_not_a_suspend(self):
        self.assertEqual(renew.suspended_for(1000.0, 1010.0), 0.0)
        self.assertEqual(renew.suspended_for(1000.0, 1000.0 + 180), 0.0)
        self.assertEqual(renew.suspended_for(None, 5000.0), 0.0)

    def test_late_pass_is_a_suspend(self):
        self.assertEqual(renew.suspended_for(1000.0, 1000.0 + 8 * 3600), 8 * 3600)

    def test_log_message_is_neutral_and_level_follows_the_edition(self):
        msg, level = renew.suspended_message(8 * 3600, True)
        self.assertEqual(level, "warn")
        self.assertNotIn("PC", msg)
        self.assertIn("8 h", msg)
        msg, level = renew.suspended_message(8 * 3600, False)
        self.assertEqual(level, "info")                         # NTP step, paused container or VM: not a warning

    def test_duration_text(self):
        self.assertEqual(renew._duration(5 * 60), "5 min")
        self.assertEqual(renew._duration(8 * 3600 + 12 * 60), "8 h 12 min")
        self.assertEqual(renew._duration(3 * 86400 + 5 * 3600), "3 j 5 h")


@unittest.skipUnless(WINDOWS, "Job objects exist on Windows only")
class ChildrenDieWithTvsign(unittest.TestCase):
    def test_child_is_placed_in_the_job(self):
        async def go():
            child = await proc._exec(sys.executable, "-c", "import time; time.sleep(30)",
                                     stdin=asyncio.subprocess.DEVNULL)
            try:
                return winjob.in_job(child.pid)
            finally:
                child.kill()
                await child.wait()
        self.assertTrue(asyncio.run(go()))

    def test_child_is_killed_when_the_parent_exits(self):
        code = (
            "import os, subprocess, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from app import winjob\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "assert winjob.adopt(child.pid)\n"
            "print(child.pid, flush=True)\n"
            "os._exit(0)\n")                                    # no cleanup at all, like a crash or a task kill
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        pid = int(out.stdout.split()[0])
        for _ in range(50):
            alive = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
            if str(pid) not in alive:
                break
            time.sleep(0.2)
        else:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            self.fail("the child outlived its parent")

    def test_adopt_is_harmless_elsewhere(self):
        self.assertFalse(winjob.adopt(None))
        self.assertFalse(winjob.adopt(0))


class RuntimeDlls(unittest.TestCase):
    def setUp(self):
        import pe_deps
        self.pe = pe_deps
        self.orig = pe_deps.imported_dlls
        self.tmp = Path(tempfile.mkdtemp(prefix="tvsign-pe-"))
        (self.tmp / "dist").mkdir()
        (self.tmp / "dist" / "plumesign.exe").write_bytes(b"MZ")
        (self.tmp / "internal").mkdir()
        (self.tmp / "internal" / "VCRUNTIME140.dll").write_bytes(b"vc")
        pe_deps.imported_dlls = lambda path: ["KERNEL32.dll", "VCRUNTIME140.dll", "api-ms-win-crt-heap-l1-1-0.dll"]

    def tearDown(self):
        self.pe.imported_dlls = self.orig

    def test_only_the_visual_cpp_runtime_counts(self):
        names = ["KERNEL32.dll", "VCRUNTIME140.dll", "vcruntime140_1.dll", "MSVCP140.dll", "ucrtbase.dll",
                 "api-ms-win-crt-heap-l1-1-0.dll", "ws2_32.dll", "python313.dll"]
        self.assertEqual(self.pe.runtime_dlls(names), ["VCRUNTIME140.dll", "vcruntime140_1.dll", "MSVCP140.dll"])

    def test_missing_then_placed_then_complete(self):
        exe = self.tmp / "dist" / "plumesign.exe"
        self.assertEqual(self.pe.missing_next_to(exe), ["VCRUNTIME140.dll"])
        self.assertEqual(self.pe.place_runtime(exe, self.tmp / "internal"), ["VCRUNTIME140.dll"])
        self.assertTrue((self.tmp / "dist" / "VCRUNTIME140.dll").is_file())
        self.assertEqual(self.pe.missing_next_to(exe), [])
        self.assertEqual(self.pe.place_runtime(exe, self.tmp / "internal"), [])      # idempotent

    def test_case_insensitive(self):
        exe = self.tmp / "dist" / "plumesign.exe"
        (self.tmp / "dist" / "vcruntime140.DLL").write_bytes(b"vc")
        self.assertEqual(self.pe.missing_next_to(exe), [])

    def test_unfindable_dll_fails_the_build(self):
        exe = self.tmp / "dist" / "plumesign.exe"
        self.pe.imported_dlls = lambda path: ["VCRUNTIME140.dll", "MSVCP999.dll"]
        with self.assertRaises(FileNotFoundError):
            self.pe.place_runtime(exe, self.tmp / "internal")

    @unittest.skipUnless(WINDOWS, "needs a real PE file")
    def test_reads_a_real_pe_file(self):
        self.pe.imported_dlls = self.orig
        names = self.pe.imported_dlls(Path(sys.base_prefix) / "python.exe")
        self.assertTrue(any(re.fullmatch(r"python3\d+\.dll", n, re.I) for n in names), names)


class Translations(unittest.TestCase):
    """Every UI string exists in French and English with the same placeholders (all keys, old and new)."""

    @classmethod
    def setUpClass(cls):
        text = (ROOT / "web" / "i18n.js").read_text(encoding="utf-8")
        cls.keys = set(re.findall(r"^\s{2}(\w+): \[", text, re.M))
        cls.keys = set(re.findall(r"^\s{2}(\w+): \[", text, re.M))
        cls.entries = {}          # entries written on one line (the others span several)
        for m in re.finditer(r'^\s{2}(\w+): (\[.*\]),?\s*$', text, re.M):
            cls.entries[m.group(1)] = json.loads(m.group(2))
        cls.app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        cls.index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_all_entries_have_two_strings(self):
        self.assertGreater(len(self.entries), 300)
        for key, pair in self.entries.items():
            self.assertEqual(len(pair), 2, key)
            self.assertTrue(all(isinstance(x, str) and x.strip() for x in pair), key)

    def test_placeholders_match(self):
        for key, (fr, en) in self.entries.items():
            self.assertEqual(set(re.findall(r"\{(\w+)\}", fr)), set(re.findall(r"\{(\w+)\}", en)), key)

    NEW = ["self_update_t", "self_update_windows", "self_update_docker", "release_page", "dismiss", "update_check",
           "update_check_note"]

    def test_new_keys_exist(self):
        for key in self.NEW:
            self.assertIn(key, self.entries)
            self.assertIn(key, self.keys)

    def test_windows_wording_is_chosen_by_edition(self):
        self.assertIn("wiz_done_p2_win", self.keys)            # picked by conseilEdition("wiz_done_p2", edition)

    def test_french_update_texts_use_the_informal_register(self):
        for key in ("self_update_windows", "self_update_docker", "update_check_note"):
            fr = self.entries[key][0]
            self.assertNotRegex(fr, r"[Vv]ous|[Vv]os|[Vv]otre|Téléchargez|Vérifiez", key)

    def test_new_keys_are_used(self):
        for key in self.NEW:
            self.assertTrue(f'"{key}"' in self.app_js or f'data-i18n="{key}"' in self.index, key)

    def test_keys_used_by_the_page_exist(self):
        used = set(re.findall(r'\bt\("(\w+)"\s*[,)]', self.app_js)) | set(re.findall(r'data-i18n(?:-\w+)?="(\w+)"', self.index))
        missing = sorted(k for k in used if k not in self.keys and f"{k}_one" not in self.keys)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
