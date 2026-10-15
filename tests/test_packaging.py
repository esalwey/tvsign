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

from app import db, main, proc, renew, settings, updates, winjob  # noqa: E402

HEADERS = {"X-Tvsign": "1"}
WINDOWS = os.name == "nt"


class VersionCompare(unittest.TestCase):
    def test_ordering(self):
        newer = updates.is_newer
        self.assertTrue(newer("v1.0.1", "1.0.0"))
        self.assertTrue(newer("1.1.0", "1.0.9"))
        self.assertTrue(newer("2.0.0", "1.99.99"))
        self.assertTrue(newer("1.10.0", "1.9.0"))            # numeric, not alphabetical
        self.assertFalse(newer("1.0.0", "1.0.0"))
        self.assertFalse(newer("1.0.0", "1.0.1"))

    def test_prerelease(self):
        newer = updates.is_newer
        self.assertTrue(newer("1.0.0", "1.0.0-dev"))          # the release comes after its pre-version
        self.assertTrue(newer("1.0.0", "1.0.0-beta.2"))
        self.assertTrue(newer("1.0.0-beta.10", "1.0.0-beta.2"))
        self.assertTrue(newer("1.0.0-rc.1", "1.0.0-beta.9"))
        self.assertFalse(newer("1.0.0-beta.1", "1.0.0"))

    def test_unreadable_versions_never_announce(self):
        self.assertFalse(updates.is_newer("2.0.0", "edge-abc1234"))
        self.assertFalse(updates.is_newer("nightly", "1.0.0"))
        self.assertFalse(updates.is_newer(None, "1.0.0"))
        self.assertFalse(updates.is_newer("1.0.1", None))


class UpdateCheck(unittest.TestCase):
    def setUp(self):
        db.init()
        self.orig_fetch = updates._fetch
        self.orig_version = updates.VERSION
        updates._STATE.clear()
        settings.raw_set("update_cache", None)
        settings.raw_set("update_check", None)
        self.calls = 0
        updates.VERSION = "1.0.0"

    def tearDown(self):
        updates._fetch = self.orig_fetch
        updates.VERSION = self.orig_version
        updates._STATE.clear()
        settings.raw_set("update_cache", None)
        settings.raw_set("update_check", None)

    def fake(self, tag="v1.2.0", fail=None):
        def fetch():
            self.calls += 1
            if fail:
                raise fail
            return {"latest": tag, "url": f"https://github.com/o/r/releases/tag/{tag}"}
        updates._fetch = fetch

    def test_newer_release_is_reported_once_per_cache_period(self):
        self.fake()
        a = asyncio.run(updates.check())
        b = asyncio.run(updates.check())
        self.assertEqual((a["newer"], a["latest"], a["current"]), (True, "v1.2.0", "1.0.0"))
        self.assertEqual(b, a)
        self.assertEqual(self.calls, 1)                       # served from the cache the second time
        self.assertEqual(a["edition"], "windows" if WINDOWS else "docker")

    def test_same_version_is_not_newer(self):
        self.fake("v1.0.0")
        self.assertFalse(asyncio.run(updates.check())["newer"])

    def test_cache_survives_a_restart(self):
        self.fake()
        asyncio.run(updates.check())
        updates._STATE.clear()                                # new process: memory empty, database kept
        asyncio.run(updates.check())
        self.assertEqual(self.calls, 1)

    def test_force_is_rate_limited(self):
        self.fake()
        asyncio.run(updates.check())
        asyncio.run(updates.check(force=True))
        self.assertEqual(self.calls, 1)                       # less than a minute after the last request
        updates._STATE["checked_at"] = time.time() - 120
        asyncio.run(updates.check(force=True))
        self.assertEqual(self.calls, 2)

    def test_failure_is_reported_not_raised_and_retried_later(self):
        self.fake(fail=OSError("offline"))
        r = asyncio.run(updates.check())
        self.assertEqual((r["newer"], r["latest"]), (False, None))
        self.assertIn("offline", r["error"])
        asyncio.run(updates.check())
        self.assertEqual(self.calls, 1)                       # not hammered: next try after RETRY_SECONDS
        updates._STATE["checked_at"] = time.time() - updates.RETRY_SECONDS - 1
        self.fake()
        self.assertTrue(asyncio.run(updates.check())["newer"])

    def test_disabled_makes_no_request_and_announces_nothing(self):
        self.fake()
        settings.update({"update_check": "off"})
        r = asyncio.run(updates.check())
        self.assertEqual((r["enabled"], r["newer"], r["latest"], self.calls), (False, False, None, 0))

    def test_link_never_leaves_github(self):
        orig = updates.ipa._get_json
        updates.ipa._get_json = lambda url: {"tag_name": "v9.0.0", "html_url": "https://evil.example/x"}
        try:
            found = self.orig_fetch()
        finally:
            updates.ipa._get_json = orig
        self.assertTrue(found["url"].startswith("https://github.com/"))
        self.assertEqual(found["latest"], "v9.0.0")


class UpdateApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)
        renew.STOPPING = False            # the app's shutdown drain sets it; other tests start renewals

    def test_update_endpoint_and_setting(self):
        orig = updates._fetch
        updates._STATE.clear()
        settings.raw_set("update_cache", None)
        updates._fetch = lambda: {"latest": "v99.0.0", "url": "https://github.com/o/r/releases/tag/v99.0.0"}
        try:
            r = self.client.get("/api/update")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertTrue(body["newer"] and body["enabled"])
            self.assertEqual(body["latest"], "v99.0.0")
            # the setting is exposed, normalised, and can be switched off from the UI
            s = self.client.get("/api/settings").json()["settings"]["update_check"]
            self.assertEqual((s["value"], s["default"]), ("on", "on"))
            r = self.client.patch("/api/settings", headers=HEADERS, json={"update_check": "off"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertFalse(self.client.get("/api/update").json()["enabled"])
            r = self.client.patch("/api/settings", headers=HEADERS, json={"update_check": "maybe"})
            self.assertEqual(r.status_code, 400)
        finally:
            updates._fetch = orig
            settings.raw_set("update_check", None)
            updates._STATE.clear()
            settings.raw_set("update_cache", None)

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
    """The tray toasts follow the backend's alert level, which is as quiet as ntfy for a sleeping Apple TV."""

    NOW = 1_000_000.0
    TV_OFF = "Apple TV « Salon » introuvable sur le réseau (éteinte ou en veille ?) [x]"

    def app(self, **over):
        row = dict(status="error", expires_at=self.NOW + 5 * 86400, last_error=self.TV_OFF)
        row.update(over)
        return row

    def test_unreachable_tv_far_from_expiry_is_not_an_alert(self):
        self.assertIsNone(renew.alert_state(self.app(), self.NOW))

    def test_unreachable_tv_close_to_expiry_is_an_alert(self):
        self.assertEqual(renew.alert_state(self.app(expires_at=self.NOW + 20 * 3600), self.NOW), "24h")
        self.assertEqual(renew.alert_state(self.app(expires_at=self.NOW + 3 * 3600), self.NOW), "6h")

    def test_other_failure_is_an_alert_at_once(self):
        self.assertEqual(renew.alert_state(self.app(last_error="plumesign : boom"), self.NOW), "fail")

    def test_timeout_towards_the_tv_counts_as_unreachable(self):
        err = "Apple TV « Salon » : délai dépassé (install)"
        self.assertTrue(renew.unreachable_error(err))
        self.assertIsNone(renew.alert_state(self.app(last_error=err), self.NOW))

    def test_expired_is_always_an_alert(self):
        self.assertEqual(renew.alert_state(self.app(status="ok", expires_at=self.NOW - 1), self.NOW), "expired")

    def test_healthy_and_running_have_no_alert(self):
        self.assertIsNone(renew.alert_state(self.app(status="ok"), self.NOW))
        self.assertIsNone(renew.alert_state(self.app(status="running"), self.NOW))

    def test_not_unreachable(self):
        self.assertFalse(renew.unreachable_error(None))
        self.assertFalse(renew.unreachable_error("plumesign : Apple TV introuvable"))

    def test_state_exposes_the_level(self):
        db.init()
        app_id = db.execute("INSERT INTO apps (name, source_type, created_at, status, last_error, expires_at) "
                            "VALUES ('Quiet', 'upload', 0, 'error', ?, ?)", self.TV_OFF, time.time() + 5 * 86400)
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

    NEW = ["update_t", "update_win", "update_docker", "update_view", "update_dismiss", "update_check",
           "update_check_on", "update_check_off", "update_check_note", "wiz_done_p2_win"]

    def test_new_keys_exist(self):
        for key in self.NEW:
            self.assertIn(key, self.entries)
            self.assertIn(key, self.keys)

    def test_french_update_texts_use_the_informal_register(self):
        for key in ("update_win", "update_docker", "update_check_note"):
            fr = self.entries[key][0]
            self.assertNotRegex(fr, r"[Vv]ous|[Vv]os|[Vv]otre|Téléchargez|Vérifiez", key)
        self.assertIn("la version {current}", self.entries["update_win"][0])

    def test_saving_the_update_setting_refreshes_the_banner(self):
        i = self.app_js.index('await api("/api/settings", { method: "PATCH", json: body });')
        block = self.app_js[i:i + 700]
        self.assertIn('"update_check" in body', block)
        self.assertIn("verifierMaj()", block)

    def test_new_keys_are_used(self):
        for key in self.NEW:
            self.assertTrue(f'"{key}"' in self.app_js or f'data-i18n="{key}"' in self.index, key)

    def test_keys_used_by_the_page_exist(self):
        used = set(re.findall(r'\bt\("(\w+)"\s*[,)]', self.app_js)) | set(re.findall(r'data-i18n(?:-\w+)?="(\w+)"', self.index))
        missing = sorted(k for k in used if k not in self.keys and f"{k}_one" not in self.keys)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
