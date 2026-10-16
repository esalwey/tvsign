"""Windows launcher: keep-awake and wake-up decisions, tray toasts, autostart flag, log rotation, drain time.

The decisions live in windows/launcher/winlogic.py (no Windows dependency); the parts that touch Windows
(registry, labels, log filter) run on Windows only.

    python -m unittest discover -s tests
"""
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "windows" / "launcher"
sys.path.insert(0, str(LAUNCHER))

import winlogic as w  # noqa: E402

WINDOWS = os.name == "nt"
NOW = 1_000_000.0
H = 3600.0


def app(**over):
    row = dict(id=1, name="NuvioTV", enabled=1, device_id=1, status="ok", running=False,
               expires_at=NOW + 100 * H, renew_at=NOW + 40 * H, next_attempt_at=None, alert=None)
    row.update(over)
    return row


class KeepAwake(unittest.TestCase):
    def test_running_renewal_keeps_the_pc_awake(self):
        self.assertTrue(w.keep_awake_needed([app(running=True)], NOW))

    def test_nothing_due_lets_the_pc_sleep(self):
        self.assertFalse(w.keep_awake_needed([app()], NOW))
        self.assertFalse(w.keep_awake_needed([], NOW))

    def test_awake_shortly_before_and_after_the_planned_time(self):
        due = app(renew_at=NOW + 20 * 60)
        self.assertTrue(w.keep_awake_needed([due], NOW))                      # inside the 30 min lead
        self.assertFalse(w.keep_awake_needed([app(renew_at=NOW + 2 * H)], NOW))
        self.assertTrue(w.keep_awake_needed([app(renew_at=NOW - 10 * 60)], NOW))   # the loop is about to run it
        self.assertFalse(w.keep_awake_needed([app(renew_at=NOW - 2 * H)], NOW))    # long overdue and idle: not our job

    def test_paused_or_unpaired_apps_never_hold_the_pc(self):
        soon = dict(renew_at=NOW + 60)
        self.assertFalse(w.keep_awake_needed([app(enabled=0, **soon)], NOW))
        self.assertFalse(w.keep_awake_needed([app(device_id=None, **soon)], NOW))

    def test_failing_app_far_from_expiry_does_not_keep_the_pc_awake(self):
        # Apple TV off for days: retried every 45 min, but staying awake all that time would be absurd
        failing = app(status="error", expires_at=NOW + 3 * 86400, next_attempt_at=NOW + 10 * 60)
        self.assertFalse(w.keep_awake_needed([failing], NOW))

    def test_failing_app_close_to_expiry_does(self):
        failing = app(status="error", expires_at=NOW + 10 * H, next_attempt_at=NOW + 10 * 60)
        self.assertTrue(w.keep_awake_needed([failing], NOW))

    def test_expired_app_is_left_alone(self):
        self.assertFalse(w.keep_awake_needed([app(expires_at=NOW - 1, status="error")], NOW))

    def test_first_install_in_progress_counts(self):
        self.assertTrue(w.keep_awake_needed([app(expires_at=None, renew_at=None, status="pending")], NOW))


class WakeUp(unittest.TestCase):
    def test_wakes_five_minutes_before_the_earliest_renewal(self):
        apps = [app(id=1, renew_at=NOW + 40 * H), app(id=2, renew_at=NOW + 12 * H)]
        self.assertEqual(w.next_wake_at(apps, NOW), NOW + 12 * H - w.WAKE_MARGIN)

    def test_nothing_to_wake_for(self):
        self.assertIsNone(w.next_wake_at([], NOW))
        self.assertIsNone(w.next_wake_at([app(enabled=0)], NOW))
        self.assertIsNone(w.next_wake_at([app(running=True)], NOW))

    def test_imminent_renewal_is_covered_by_keep_awake_instead(self):
        self.assertIsNone(w.next_wake_at([app(renew_at=NOW + 2 * 60)], NOW))

    def test_retry_time_wins_over_the_planned_time(self):
        retry = app(renew_at=NOW + 1 * H, next_attempt_at=NOW + 5 * H)
        self.assertEqual(w.next_wake_at([retry], NOW), NOW + 5 * H - w.WAKE_MARGIN)

    def test_task_xml_is_valid_and_wakes_the_pc(self):
        when = time.mktime((2031, 3, 4, 2, 55, 10, 0, 0, -1))
        xml = w.wake_task_xml(when, r"C:\Users\A & B\tvsign.exe", "--tray")
        root = ET.fromstring(xml.encode("utf-16"))                              # well formed, '&' escaped
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find("t:Settings/t:WakeToRun", ns).text, "true")
        self.assertEqual(root.find("t:Settings/t:DisallowStartIfOnBatteries", ns).text, "false")
        self.assertEqual(root.find("t:Triggers/t:TimeTrigger/t:StartBoundary", ns).text, "2031-03-04T02:55:10")
        self.assertEqual(root.find("t:Actions/t:Exec/t:Command", ns).text, r"C:\Users\A & B\tvsign.exe")
        self.assertEqual(root.find("t:Actions/t:Exec/t:Arguments", ns).text, "--tray")
        self.assertEqual(root.find("t:Principals/t:Principal/t:RunLevel", ns).text, "LeastPrivilege")   # no admin
        # no time limit: the instance started by the task may be the one real tvsign, never killed after minutes
        self.assertEqual(root.find("t:Settings/t:ExecutionTimeLimit", ns).text, "PT0S")
        self.assertEqual(datetime.fromtimestamp(when).strftime("%Y-%m-%dT%H:%M:%S"), "2031-03-04T02:55:10")

    def test_task_xml_without_arguments(self):
        root = ET.fromstring(w.wake_task_xml(NOW, "x.exe").encode("utf-16"))
        self.assertIsNone(root.find("{http://schemas.microsoft.com/windows/2004/02/mit/task}Actions/"
                                    "{http://schemas.microsoft.com/windows/2004/02/mit/task}Exec/"
                                    "{http://schemas.microsoft.com/windows/2004/02/mit/task}Arguments"))


class Toasts(unittest.TestCase):
    def snap(self, *apps, accounts=()):
        return w.snapshot({"apps": list(apps), "accounts": list(accounts)})

    def test_first_snapshot_is_silent(self):
        self.assertEqual(w.toasts(None, self.snap(app(alert="expired"))), [])

    def test_sleeping_apple_tv_raises_nothing_however_often_it_is_retried(self):
        # the backend reports no alert for an unreachable TV far from the expiry: ok -> error -> running -> error
        seq = [app(status="ok"), app(status="error"), app(status="running"), app(status="error"),
               app(status="running"), app(status="error")]
        snaps = [self.snap(a) for a in seq]
        for before, after in zip(snaps, snaps[1:]):
            self.assertEqual(w.toasts(before, after), [])

    def test_real_failure_toasts_once_then_recovery_once(self):
        ok, failed = self.snap(app(status="ok")), self.snap(app(status="error", alert="fail"))
        retry = self.snap(app(status="running", alert="fail"))
        self.assertEqual(w.toasts(ok, failed), [("app_failed", "NuvioTV")])
        self.assertEqual(w.toasts(failed, retry), [])                          # same level while retrying
        self.assertEqual(w.toasts(retry, failed), [])
        self.assertEqual(w.toasts(failed, ok), [("app_recovered", "NuvioTV")])

    def test_escalation_toasts_each_worsening_only(self):
        levels = ["fail", "24h", "6h", "expired"]
        snaps = [self.snap(app(status="error", alert=lvl)) for lvl in levels]
        got = [w.toasts(a, b) for a, b in zip(snaps, snaps[1:])]
        self.assertEqual(got, [[("app_expiring", "NuvioTV")], [("app_expiring", "NuvioTV")],
                               [("app_expired", "NuvioTV")]])
        self.assertEqual(w.toasts(snaps[3], snaps[2]), [])                     # an improvement is not a toast

    def test_paused_failing_app_does_not_claim_a_recovery(self):
        failed = self.snap(app(status="error", alert="fail"))
        gone_quiet = self.snap(app(status="error", alert=None))
        self.assertEqual(w.toasts(failed, gone_quiet), [])

    def test_new_app_and_lost_account(self):
        base = self.snap(app())
        self.assertEqual(w.toasts(base, self.snap(app(), app(id=2, name="New", alert="fail", status="error"))),
                         [("app_failed", "New")])
        lost = self.snap(app(), accounts=[{"email": "me@example.com", "ok": 0}])
        self.assertEqual(w.toasts(base, lost), [("account_lost", "me@example.com")])
        self.assertEqual(w.toasts(lost, lost), [])


class AutostartFlag(unittest.TestCase):
    def test_flag_values(self):
        self.assertTrue(w.startup_approved(None))                              # no value: enabled
        self.assertTrue(w.startup_approved(b""))
        for enabled in (b"\x02" + b"\x00" * 11, b"\x06" + b"\x00" * 11):
            self.assertTrue(w.startup_approved(enabled))
        for disabled in (b"\x03" + b"\x00" * 11, b"\x07" + b"\x00" * 11):
            self.assertFalse(w.startup_approved(disabled))


class LogRotation(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="tvsign-log-"))
        self.path = self.dir / "tvsign.log"

    def names(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_log_stays_bounded_and_keeps_two_backups(self):
        log = w.RotatingLog(self.path, max_bytes=1000, backups=2)
        for i in range(200):
            log.write(f"line {i:04d} " + "x" * 40 + "\n")
        log.close()
        self.assertEqual(self.names(), ["tvsign.log", "tvsign.log.1", "tvsign.log.2"])
        for p in self.dir.iterdir():
            self.assertLess(p.stat().st_size, 1000 + 100)
        newest = self.path.read_text(encoding="utf-8").strip().splitlines()[-1]
        self.assertTrue(newest.startswith("line 0199"))                        # nothing lost at the end

    def test_existing_big_file_is_rotated_on_first_write(self):
        self.path.write_text("old\n" * 1000, encoding="utf-8")
        log = w.RotatingLog(self.path, max_bytes=1000)
        log.write("new\n")
        log.close()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "new\n")
        self.assertTrue((self.dir / "tvsign.log.1").read_text(encoding="utf-8").startswith("old"))

    def test_behaves_like_stdout(self):
        log = w.RotatingLog(self.path)
        print("héllo", file=log, flush=True)
        self.assertFalse(log.isatty())
        log.flush()
        log.close()
        self.assertIn("héllo", self.path.read_text(encoding="utf-8"))

    def test_threads_do_not_lose_lines(self):
        log = w.RotatingLog(self.path, max_bytes=10_000_000)

        def work(n):
            for i in range(300):
                log.write(f"{n}:{i}\n")
        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        log.close()
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1200)

    def test_polling_requests_stay_out_of_the_access_log(self):
        for path in ("/api/state", "/api/settings", "/api/livez", "/api/auth/status", "/api/state?x=1"):
            self.assertTrue(w.quiet_access_path(path), path)
        for path in ("/auth/launch?token=abc", "/api/apps/3/renew", "/static/app.js"):
            self.assertFalse(w.quiet_access_path(path), path)


class DrainTime(unittest.TestCase):
    def test_default_is_the_backend_drain_plus_a_margin(self):
        self.assertEqual(w.drain_wait_seconds({}), 270.0)                      # 240 s + 30 s, not the old 15 s

    def test_follows_the_environment(self):
        self.assertEqual(w.drain_wait_seconds({"TVSIGN_SHUTDOWN_DRAIN_SECONDS": "60"}), 90.0)
        self.assertEqual(w.drain_wait_seconds({"TVSIGN_SHUTDOWN_DRAIN_SECONDS": "garbage"}), 270.0)
        self.assertEqual(w.drain_wait_seconds({"TVSIGN_SHUTDOWN_DRAIN_SECONDS": "-5"}), 30.0)

    def test_matches_the_backend_default(self):
        sys.path.insert(0, str(ROOT))
        os.environ.setdefault("TVSIGN_DATA", tempfile.mkdtemp(prefix="tvsign-test-"))
        from app import config
        if "TVSIGN_SHUTDOWN_DRAIN_SECONDS" not in os.environ:
            self.assertEqual(w.drain_wait_seconds() - 30, config.SHUTDOWN_DRAIN_SECONDS)


@unittest.skipUnless(WINDOWS, "the launcher is a Windows program")
class LauncherModule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tvsign_app
        cls.app = tvsign_app

    def test_labels_are_complete_and_formattable_in_both_languages(self):
        fr, en = self.app.LABELS["fr"], self.app.LABELS["en"]
        self.assertEqual(set(fr), set(en))
        args = dict(names="A", err="E", name="N", email="e@x", version="1.2.3", log="L")
        for lang in (fr, en):
            for key, text in lang.items():
                text.format(**args)
        # every label the toast rules can ask for exists
        for key in ("app_failed", "app_expiring", "app_expired", "app_recovered", "account_lost"):
            self.assertIn(key, fr)

    def test_access_log_filter_drops_successful_polls_only(self):
        f = self.app._QuietPolling()

        def rec(path, status):
            return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                     ("127.0.0.1:1", "GET", path, "1.1", status), None)
        self.assertFalse(f.filter(rec("/api/state", 200)))
        self.assertTrue(f.filter(rec("/api/state", 500)))
        self.assertTrue(f.filter(rec("/api/apps/1/renew", 200)))

    def test_autostart_follows_the_windows_startup_flag(self):
        import winreg
        a = self.app
        base = r"Software\tvsign-unittest-" + str(os.getpid())
        orig = (a.RUN_KEY, a.APPROVED_KEY)
        a.RUN_KEY, a.APPROVED_KEY = base + r"\Run", base + r"\Approved"
        try:
            winreg.CreateKey(winreg.HKEY_CURRENT_USER, a.RUN_KEY).Close()
            self.assertFalse(a.autostart_enabled())
            a.set_autostart(True)
            self.assertTrue(a.autostart_enabled())
            self.assertFalse(a.autostart_blocked())
            # the user disables it in Task Manager / Settings: flag 03
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, a.APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, a.APP_NAME, 0, winreg.REG_BINARY, b"\x03" + b"\x00" * 11)
            self.assertFalse(a.autostart_enabled())
            self.assertTrue(a.autostart_blocked())
            # switching it off then on again from our menu clears the stale "disabled" flag
            a.set_autostart(False)
            self.assertFalse(a.autostart_blocked())
            a.set_autostart(True)
            self.assertTrue(a.autostart_enabled())
        finally:
            a.RUN_KEY, a.APPROVED_KEY = orig
            for sub in (r"\Run", r"\Approved"):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + sub, 0, winreg.KEY_SET_VALUE) as k:
                        try:
                            winreg.DeleteValue(k, "tvsign")
                        except OSError:
                            pass
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base + sub)
                except OSError:
                    pass
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
