"""Hotfix 1.0.1 : Apple refuse les données anisette Android (depuis le 12 octobre 2026).

Classement des erreurs (-80045, HTTP 503, SMS 403, -22411, -45061), migration du serveur anisette,
alerte unique d'un compte à reconnecter. Les textes d'erreur ci-dessous sont ceux que plumesign écrit
(tests Rust de vendor/Impactor : plume_core et omnisette). Sans plumesign ni Apple TV ni Internet :

    python -m unittest discover -s tests
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

if "app.config" not in sys.modules:
    DATA = tempfile.mkdtemp(prefix="tvsign-test-")
    os.environ.update(TVSIGN_DATA=DATA, TVSIGN_AUTH="none", TVSIGN_PLUMESIGN=str(Path(DATA) / "absent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, diag, plume, renew, settings  # noqa: E402
from app.proc import Result  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ANISETTE_ENVS = ("TVSIGN_ANISETTE_URL", "PLUMESIGN_ANISETTE_URL", "ANISETTE_URL_V3")
LEGACY = ["ani.sidestore.app", "ani.stikstore.app", "anisette.crystall1ne.dev",
          "ani3server.fly.dev", "ani.jaydenha.uk", "omni.ethxn.xyz"]


class AnisetteCase(unittest.TestCase):
    """Isole le réglage anisette (base partagée avec les autres tests, environnement du processus)
    et enregistre les lignes de journal au lieu de les écrire (db.log fusionne les répétitions)."""

    def setUp(self):
        db.init()
        self.saved_env = {n: os.environ.pop(n, None) for n in ANISETTE_ENVS}
        settings.raw_set("anisette_url", None)
        self.logged = []
        self.real_log = db.log
        db.log = lambda message, level="info", app_id=None: self.logged.append((level, message))

    def tearDown(self):
        db.log = self.real_log
        settings.raw_set("anisette_url", None)
        for name, value in self.saved_env.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value


class LegacyServers(AnisetteCase):
    def test_the_six_android_servers_are_recognised(self):
        for host in LEGACY:
            for url in (f"https://{host}", f"https://{host}/", f"http://{host}:8080/v3/client_info",
                        f"HTTPS://{host.upper()}", f"https://user:pw@{host}/x", host):
                self.assertTrue(settings.is_legacy_anisette(url), url)

    def test_other_servers_are_not(self):
        for url in ("https://ani.jkcoxson.com", "https://ani2.stikstore.app", "https://anisette.example.org",
                    "https://ani.sidestore.app.example.com", "https://example.com/ani.sidestore.app",
                    "https://myani.sidestore.app", "", "   "):
            self.assertFalse(settings.is_legacy_anisette(url), url)

    def test_the_defaults(self):
        self.assertEqual(settings.DEFAULT_ANISETTE, "https://ani.jkcoxson.com")
        self.assertEqual(settings.FALLBACK_ANISETTE, "https://ani2.stikstore.app")
        self.assertEqual(diag.DEFAULT_ANISETTE, settings.DEFAULT_ANISETTE)
        self.assertEqual(settings.anisette_servers(), [settings.DEFAULT_ANISETTE, settings.FALLBACK_ANISETTE])
        self.assertEqual(settings.describe()["anisette_url"]["value"], "")      # vide = serveur par défaut


class Migration(AnisetteCase):
    def test_stored_android_server_becomes_the_default(self):
        for host in LEGACY:
            settings.raw_set("anisette_url", f"https://{host}")
            self.logged.clear()
            settings.migrate()
            self.assertIsNone(settings.raw_get("anisette_url"), host)
            self.assertEqual(settings.anisette_url(), "")
            self.assertEqual(settings.anisette_servers(), [settings.DEFAULT_ANISETTE, settings.FALLBACK_ANISETTE])
            self.assertEqual(len(self.logged), 1, host)
            level, message = self.logged[0]
            self.assertEqual(level, "warn")
            self.assertIn(f"https://{host}", message)
            self.assertIn(settings.DEFAULT_ANISETTE, message)

    def test_custom_server_is_kept(self):
        settings.raw_set("anisette_url", "https://anisette.example.org")
        settings.migrate()
        self.assertEqual(settings.raw_get("anisette_url"), "https://anisette.example.org")
        self.assertEqual(settings.anisette_url(), "https://anisette.example.org")
        self.assertEqual(settings.anisette_servers(), ["https://anisette.example.org"])   # pas de repli imposé
        self.assertEqual(self.logged, [])

    def test_empty_setting_and_default_server_are_left_alone(self):
        settings.migrate()
        settings.raw_set("anisette_url", settings.DEFAULT_ANISETTE)
        settings.migrate()
        self.assertEqual(settings.raw_get("anisette_url"), settings.DEFAULT_ANISETTE)
        self.assertEqual(self.logged, [])

    def test_migration_runs_once(self):
        settings.raw_set("anisette_url", "https://ani.sidestore.app")
        settings.migrate()
        self.assertEqual(len(self.logged), 1)
        settings.migrate()
        self.assertEqual(len(self.logged), 1)

    def test_android_server_in_the_environment_is_ignored_and_reported(self):
        os.environ["TVSIGN_ANISETTE_URL"] = "https://ani.sidestore.app"
        settings.migrate()
        self.assertEqual(settings.anisette_url(), "")
        info = settings.describe()["anisette_url"]
        self.assertFalse(info["locked"])                  # ignorée : le réglage reste modifiable
        self.assertEqual(len(self.logged), 1)
        level, message = self.logged[0]
        self.assertIn("TVSIGN_ANISETTE_URL", message)
        self.assertEqual(level, "warn")

    def test_custom_server_in_the_environment_wins(self):
        os.environ["PLUMESIGN_ANISETTE_URL"] = "https://anisette.example.org"
        self.assertEqual(settings.anisette_url(), "https://anisette.example.org")
        self.assertTrue(settings.describe()["anisette_url"]["locked"])

    def test_restoring_an_old_backup_migrates_too(self):
        # backup.restore() rappelle settings.migrate() une fois la base remplacée
        text = (ROOT / "app" / "backup.py").read_text(encoding="utf-8")
        self.assertIn("settings.migrate()", text)
        self.assertIn("settings.migrate()", (ROOT / "app" / "main.py").read_text(encoding="utf-8"))


class LegacyIdentity(AnisetteCase):
    """L'identité anisette Android de l'ancienne version : signalée une fois, jamais gardée."""

    def setUp(self):
        super().setUp()
        plume.STORE.mkdir(parents=True, exist_ok=True)
        self.old = plume.STORE / "state.plist"
        self.new = plume.STORE / plume.ANISETTE_STATE

    def tearDown(self):
        self.old.unlink(missing_ok=True)
        self.new.unlink(missing_ok=True)
        super().tearDown()

    def test_the_android_identity_is_mentioned(self):
        self.old.write_bytes(b"old")
        self.assertTrue(plume.note_legacy_anisette())
        self.assertEqual(len(self.logged), 1)
        self.assertIn("Android", self.logged[0][1])
        self.assertIn("Windows", self.logged[0][1])

    def test_nothing_to_say_without_it_or_once_the_new_one_exists(self):
        self.assertFalse(plume.note_legacy_anisette())
        self.old.write_bytes(b"old")
        self.new.write_bytes(b"new")
        self.assertFalse(plume.note_legacy_anisette())
        self.assertEqual(self.logged, [])

    def test_the_state_file_name_is_the_one_plumesign_writes(self):
        omnisette = (ROOT / "vendor" / "Impactor" / "3rdparty" / "omnisette" / "src" / "remote_anisette_v3.rs")
        self.assertIn(f'"{plume.ANISETTE_STATE}"', omnisette.read_text(encoding="utf-8"))

    def test_resetting_the_identity_removes_only_the_new_file_and_not_twice_an_hour(self):
        self.old.write_bytes(b"old")
        self.new.write_bytes(b"new")
        plume._RESET_AT = 0.0
        try:
            self.assertTrue(plume.reset_anisette("test"))
            self.assertFalse(self.new.exists())
            self.assertTrue(self.old.exists())
            self.new.write_bytes(b"newer")
            self.assertFalse(plume.reset_anisette("test again"))       # moins d'une heure plus tard
            self.assertTrue(self.new.exists())
        finally:
            plume._RESET_AT = 0.0


class SettingValidation(AnisetteCase):
    def test_the_interface_refuses_an_android_server(self):
        with self.assertRaises(ValueError) as ctx:
            settings.update({"anisette_url": "https://ani.sidestore.app"})
        errors = ctx.exception.args[0]
        self.assertIn("anisette_url", errors)
        # the validation errors are stable codes, the interface translates them (err_anisette_android)
        self.assertEqual(errors["anisette_url"], "anisette_android:ani.sidestore.app")
        i18n = (ROOT / "web" / "i18n.js").read_text(encoding="utf-8")
        line = next(l for l in i18n.splitlines() if l.lstrip().startswith("err_anisette_android:"))
        self.assertIn("Android", line)
        self.assertIn("12 octobre 2026", line)
        self.assertIsNone(settings.raw_get("anisette_url"))

    def test_custom_default_and_empty_are_accepted(self):
        for value in ("https://anisette.example.org", settings.DEFAULT_ANISETTE, settings.FALLBACK_ANISETTE):
            settings.update({"anisette_url": value})
            self.assertEqual(settings.anisette_url(), value)
        settings.update({"anisette_url": ""})
        self.assertIsNone(settings.raw_get("anisette_url"))
        self.assertEqual(settings.anisette_url(), "")

    def test_a_bad_address_is_still_refused(self):
        with self.assertRaises(ValueError):
            settings.update({"anisette_url": "ani.jkcoxson.com"})


class SeveralServers(AnisetteCase):
    """Une liste de serveurs (secours choisis par l'utilisateur) et les règles Windows / Android du correctif."""

    def test_a_list_with_an_android_server_is_refused_as_a_whole(self):
        with self.assertRaises(ValueError) as ctx:
            settings.update({"anisette_url": "https://anisette.example.org https://omni.ethxn.xyz"})
        self.assertEqual(ctx.exception.args[0]["anisette_url"], "anisette_android:omni.ethxn.xyz")

    def test_a_chosen_list_is_used_in_order_without_the_default_fallback(self):
        settings.raw_set("anisette_url", "https://a.example https://b.example")
        self.assertEqual(settings.anisette_urls(), ["https://a.example", "https://b.example"])
        self.assertEqual(settings.anisette_servers(), ["https://a.example", "https://b.example"])

    def test_the_default_inside_a_list_keeps_its_own_fallback(self):
        settings.raw_set("anisette_url", f"{settings.DEFAULT_ANISETTE} https://b.example")
        self.assertEqual(settings.anisette_servers(),
                         [settings.DEFAULT_ANISETTE, settings.FALLBACK_ANISETTE, "https://b.example"])

    def test_migration_drops_only_the_android_entries_of_a_stored_list(self):
        settings.raw_set("anisette_url", "https://ani.sidestore.app https://b.example")
        settings.migrate()
        self.assertEqual(settings.raw_get("anisette_url"), "https://b.example")
        settings.raw_set("anisette_url", "https://ani.sidestore.app https://ani.stikstore.app")
        settings.migrate()
        self.assertIsNone(settings.raw_get("anisette_url"))
        self.assertEqual(len(self.logged), 2)

    def test_a_refusal_from_apple_does_not_switch_servers(self):
        calls = []

        async def fake_run(*args, env=None, **kw):
            calls.append(env.get("PLUMESIGN_ANISETTE_URL"))
            return Result(1, "Error: Apple answered -80045 (cannot be completed at this time)\nanisette")
        settings.raw_set("anisette_url", "https://a.example https://b.example")
        saved = plume.run
        plume.run = fake_run
        try:
            res = asyncio.run(plume._plume("account", "devices"))
        finally:
            plume.run = saved
        self.assertFalse(res.ok)
        self.assertEqual(calls, ["https://a.example"])

    def test_diagnostic_lists_every_chosen_server(self):
        settings.raw_set("anisette_url", "https://a.example https://b.example")
        windows = AnisetteDiagnostic.WINDOWS
        saved = diag._client_info
        diag._client_info = lambda url: (windows, "") if url == "https://a.example" else (None, "injoignable")
        try:
            r = asyncio.run(diag._anisette())
        finally:
            diag._client_info = saved
        self.assertEqual(r["status"], "ok")
        self.assertIn("https://a.example", r["detail"])
        self.assertIn("https://b.example : injoignable", r["detail"])


class SettingsApi(AnisetteCase):
    """Ce que voit l'interface : le réglage du serveur anisette par l'API."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app import main
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    HEADERS = {"X-Tvsign": "1"}

    def test_an_android_server_is_refused_with_the_reason(self):
        r = self.client.patch("/api/settings", headers=self.HEADERS, json={"anisette_url": "https://ani.stikstore.app"})
        self.assertEqual(r.status_code, 400)
        detail = r.json()["detail"]
        self.assertEqual(detail["anisette_url"], "anisette_android:ani.stikstore.app")
        self.assertIsNone(settings.raw_get("anisette_url"))

    def test_a_custom_server_is_saved_and_the_empty_value_returns_to_the_default(self):
        r = self.client.patch("/api/settings", headers=self.HEADERS, json={"anisette_url": "https://anisette.example.org"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["settings"]["anisette_url"]["value"], "https://anisette.example.org")
        r = self.client.patch("/api/settings", headers=self.HEADERS, json={"anisette_url": None})
        self.assertEqual(r.json()["settings"]["anisette_url"]["value"], "")
        self.assertEqual(r.json()["settings"]["anisette_url"]["source"], "default")

    def test_a_legacy_value_found_in_the_database_shows_as_the_default(self):
        settings.raw_set("anisette_url", "https://omni.ethxn.xyz")
        info = self.client.get("/api/settings").json()["settings"]["anisette_url"]
        self.assertEqual(info["value"], "")
        settings.migrate()
        self.assertEqual(self.client.get("/api/settings").json()["settings"]["anisette_url"]["source"], "default")


class PlumesignEnvironment(AnisetteCase):
    def test_default_server_sets_nothing(self):
        e = plume.env()
        self.assertNotIn("PLUMESIGN_ANISETTE_URL", e)
        self.assertNotIn("ANISETTE_URL_V3", e)

    def test_custom_server_is_passed(self):
        settings.raw_set("anisette_url", "https://anisette.example.org")
        self.assertEqual(plume.env()["PLUMESIGN_ANISETTE_URL"], "https://anisette.example.org")

    def test_an_android_server_is_never_inherited(self):
        os.environ["PLUMESIGN_ANISETTE_URL"] = "https://ani.sidestore.app"
        os.environ["ANISETTE_URL_V3"] = "https://ani.stikstore.app"
        try:
            e = plume.env()
            self.assertNotIn("PLUMESIGN_ANISETTE_URL", e)
            self.assertNotIn("ANISETTE_URL_V3", e)
        finally:
            os.environ.pop("PLUMESIGN_ANISETTE_URL", None)
            os.environ.pop("ANISETTE_URL_V3", None)


# Sorties de plumesign (Display des erreurs Rust : vendor/Impactor, tests de plume_core et omnisette)
SRP_80045 = "Logging in...\nError: Authentication SRP error -80045: This action cannot be completed at this time."
GSA_503 = ("[2026-10-15T19:42:20Z WARN  plume_core::auth::account] GrandSlam is busy (HTTP 503): trying again in 2 s (1/3)\n"
           "Error: Apple GrandSlam answered HTTP 503 without a usable reply: 503 Service Temporarily Unavailable")
# 503 pages met on the way (a retry, a fallback server), then a different final error
BUSY_THEN_WRONG_CODE = (
    "[2026-10-15T19:42:20Z WARN  plume_core::auth::account] GrandSlam is busy (HTTP 503): trying again in 2 s (1/3)\n"
    "[2026-10-15T19:42:22Z WARN  omnisette::remote_anisette_v3] anisette server https://ani.jkcoxson.com is not usable: "
    "Apple answered HTTP 503 to the anisette provisioning: 503 Service Temporarily Unavailable\n"
    "[2026-10-15T19:42:30Z INFO  plumesign::commands::account] Enter 2FA code: \n"
    "Error: Authentication SRP error -21669: Incorrect verification code.")
PROVISIONING_503 = ("Error: Anisette error: Apple answered HTTP 503 to the anisette provisioning: "
                    "503 Service Temporarily Unavailable")
SMS_403 = ("Error: SMS 2FA refused by Apple (HTTP 403: Access to this Apple Developer Service was denied): "
           "use the code shown on a trusted Apple device instead")
NEVER_SIGNED_IN = "Error: Authentication SRP error -22411: Your Apple ID cannot be used."
SYNC_22421 = "Error: Authentication SRP error -22421: This action could not be completed. Try again."
BAD_MD_22409 = "Error: Authentication SRP error -22409: This action could not be completed. Try again."
MID_80009 = "Error: Authentication SRP error -80009: MID is invalid."
NOT_PROVISIONED = "Error: Anisette error: Anisette not provisioned!"
SERVER_45061 = "Error: Anisette error: anisette server error: ADI error -45061"
PORTAL_1100 = ("Error: Developer API error 1100 (HTTP Some(200)): Your session has expired. Please log in. "
               "[URL: https://developerservices2.apple.com/services/QH65B2/listTeams.action]")
BAD_PASSWORD = "Error: Authentication SRP error -22406: Your Apple ID or password was entered incorrectly."
NETWORK = ("Error: Anisette error: Request Error error sending request for url "
           "(https://ani.jkcoxson.com/v3/client_info)")
GSA_429 = "Error: Apple GrandSlam answered HTTP 429 without a usable reply: Too Many Requests"
PORTAL_503 = ("Error: Apple developer services answered HTTP 503 without a usable reply: "
              "503 Service Temporarily Unavailable")
PORTAL_429 = "Error: Apple developer services answered HTTP 429 without a usable reply: Too Many Requests"
NOT_WINDOWS = ("Error: Anisette error: anisette server not usable: https://anisette.example.org announces "
               "`<MacBookPro13,2> <macOS;13.1;22C65> <com.apple.AuthKit/1 (com.apple.akd/1.0)>`: Apple only accepts "
               "Windows anisette since 2026-10-12 (use the default server, or one that serves Windows anisette)")
NOT_WINDOWS_BOTH = ("Error: Anisette error: no anisette server could be used: https://ani.jkcoxson.com: anisette server "
                    "not usable: https://ani.jkcoxson.com announces `<Mac15,7> <macOS;27.0>`: Apple only accepts "
                    "Windows anisette since 2026-10-12 | https://ani2.stikstore.app: anisette server not usable: ...")


class Classification(unittest.TestCase):
    CASES = [
        (SRP_80045, "apple_refused"),
        (GSA_503, "apple_503"),
        (PROVISIONING_503, "apple_503"),
        (PORTAL_503, "apple_503"),
        (GSA_429, "apple_rate_limited"),
        (PORTAL_429, "apple_rate_limited"),
        (SMS_403, "sms_denied"),
        (NEVER_SIGNED_IN, "never_signed_in"),
        (SYNC_22421, "anisette_refused"),
        (BAD_MD_22409, "anisette_refused"),
        (MID_80009, "anisette_refused"),
        (NOT_WINDOWS, "anisette_unsupported"),
        (NOT_WINDOWS_BOTH, "anisette_unsupported"),
        (NOT_PROVISIONED, "anisette_reprovision"),
        (SERVER_45061, "anisette_reprovision"),
    ]

    def test_each_error_has_its_class(self):
        for output, kind in self.CASES:
            self.assertEqual(plume.classify(output), kind, output)

    def test_none_of_them_is_a_session_problem(self):
        # -80045 avait la même forme que « Authentication SRP error » : session expirée à tort
        for output, kind in self.CASES:
            self.assertFalse(plume.session_expired(output), output)

    def test_real_session_problems_are_still_found(self):
        for output in (PORTAL_1100, BAD_PASSWORD,
                       "Error: Authentication SRP error -20356: Invalid Requester (person or application), "
                       "possible invalid/expired session",
                       "Error: No account selected. Please login first using 'plumesign account login'"):
            self.assertIsNone(plume.classify(output), output)
            self.assertTrue(plume.session_expired(output), output)

    def test_log_lines_do_not_decide_why_a_command_failed(self):
        # the 503 pages of a retry or of a fallback are in the log, the failure is something else
        self.assertIsNone(plume.classify(BUSY_THEN_WRONG_CODE))
        self.assertEqual(plume._errors(BUSY_THEN_WRONG_CODE), "Error: Authentication SRP error -21669: Incorrect verification code.")
        # ... and the same lines do not turn an ordinary failure into a transient one
        error = plume._failure("a@b.c", "signature", Result(1, "[2026-10-15T19:42:22Z WARN  omnisette::remote_anisette_v3] "
                               "anisette server https://ani.jkcoxson.com is not usable: boom\nError: NO_TEAM: this Apple ID has no developer team"))
        self.assertIs(type(error), RuntimeError)
        db.execute("DELETE FROM account_status WHERE email = 'a@b.c'")

    def test_network_problems_are_neither(self):
        self.assertIsNone(plume.classify(NETWORK))
        self.assertFalse(plume.session_expired(NETWORK))
        self.assertFalse(plume.session_expired("Error: Reqwest error: operation timed out"))


class Failures(unittest.TestCase):
    def setUp(self):
        db.init()
        self.email = "classify@example.com"
        self.reset_calls = []
        self.saved = plume.reset_anisette
        plume.reset_anisette = lambda reason: self.reset_calls.append(reason) or True

    def tearDown(self):
        plume.reset_anisette = self.saved
        plume.ALERTED.discard(self.email)
        db.execute("DELETE FROM account_status WHERE email = ?", self.email)

    def fail(self, output):
        return plume._failure(self.email, "signature", Result(1, output))

    def test_minus_80045_is_calm_and_slow(self):
        e = self.fail(SRP_80045)
        self.assertIsInstance(e, plume.AppleRefused)
        self.assertTrue(e.slow)
        self.assertIn("-80045", str(e))
        self.assertIn("n'insiste pas", str(e))
        self.assertNotIn("expirée", str(e))
        self.assertIsNone(db.row("SELECT ok FROM account_status WHERE email = ?", self.email))   # session intacte

    def test_503_is_refused_but_not_slow(self):
        for output in (GSA_503, PROVISIONING_503, PORTAL_503):
            e = self.fail(output)
            self.assertIsInstance(e, plume.AppleRefused)
            self.assertFalse(e.slow)
            self.assertIn("503", str(e))
            self.assertIn("anisette", str(e))
            # the usual delay of a renewal, not a longer one: the message does not pretend otherwise
            self.assertNotIn("espacé", str(e))

    def test_429_asks_to_slow_down_like_minus_80045(self):
        for output in (GSA_429, PORTAL_429):
            e = self.fail(output)
            self.assertIsInstance(e, plume.AppleRefused)
            self.assertEqual(e.kind, "apple_rate_limited")
            self.assertTrue(e.slow)
            self.assertIn("429", str(e))
            self.assertIn("n'insiste pas", str(e))
            self.assertNotIn("expirée", str(e))
        self.assertIsNone(db.row("SELECT ok FROM account_status WHERE email = ?", self.email))   # session intacte
        # the next try of a renewal is hours away
        now = 1_000_000.0
        self.assertGreaterEqual(renew._retry_at({"expires_at": None}, now, slow=True) - now, 3 * 3600)

    def test_a_server_that_is_not_windows_waits_for_the_user_not_for_a_retry(self):
        for output in (NOT_WINDOWS, NOT_WINDOWS_BOTH):
            e = self.fail(output)
            self.assertIs(type(e), RuntimeError, output)
            self.assertNotIsInstance(e, (plume.Transient, plume.SessionExpired, plume.AppleRefused))
            self.assertIn("Windows", str(e))
            self.assertIn("12 octobre 2026", str(e))
            self.assertNotIn("passager", str(e))
        self.assertEqual(self.reset_calls, [])

    def test_expired_session_names_the_reason_and_the_account(self):
        e = self.fail(PORTAL_1100)
        self.assertIsInstance(e, plume.SessionExpired)
        self.assertEqual(e.email, self.email)
        row = db.row("SELECT ok, message FROM account_status WHERE email = ?", self.email)
        self.assertEqual(row["ok"], 0)
        self.assertIn("deux facteurs", row["message"])
        self.assertIn(plume.REAUTH_REASON, row["message"])

    def test_actions_for_the_user_are_not_retried_by_themselves(self):
        for output, text in ((SMS_403, "SMS"), (NEVER_SIGNED_IN, "jamais été connecté")):
            e = self.fail(output)
            self.assertNotIsInstance(e, (plume.Transient, plume.SessionExpired, plume.AppleRefused))
            self.assertIn(text, str(e))

    def test_anisette_problems_are_transient_and_refused_data_reset_the_identity(self):
        for output in (NOT_PROVISIONED, SERVER_45061, NETWORK):
            self.assertIsInstance(self.fail(output), plume.Transient, output)
        self.assertEqual(self.reset_calls, [])
        for output in (SYNC_22421, BAD_MD_22409, MID_80009):
            self.assertIsInstance(self.fail(output), plume.Transient, output)
        self.assertEqual(len(self.reset_calls), 3)

    def test_anything_else_stays_a_plain_error(self):
        e = self.fail("Error: NO_TEAM: this Apple ID has no developer team")
        self.assertIs(type(e), RuntimeError)


class CheckAccount(unittest.TestCase):
    """check_account : -80045 et consorts ne déconnectent pas le compte."""

    def setUp(self):
        db.init()
        self.email = "check@example.com"
        plume._set_status(self.email, True, "session valide")

        class Lock:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        self.saved = plume._locked, plume._as, plume.run
        plume._locked, plume._as = (lambda *a, **k: Lock()), (lambda e: Lock())
        # an account has to be in the store for check_account to look at it
        plume.STORE.mkdir(parents=True, exist_ok=True)
        (plume.STORE / "accounts.json").write_text(json.dumps({
            "selected_account": self.email,
            "accounts": {self.email: {"first_name": "C", "team_id": "TEAM"}}}), encoding="utf-8")
        plume._LAST = {}

    def tearDown(self):
        plume._locked, plume._as, plume.run = self.saved
        (plume.STORE / "accounts.json").unlink(missing_ok=True)
        plume._LAST = {}
        plume.ALERTED.discard(self.email)
        db.execute("DELETE FROM account_status WHERE email = ?", self.email)

    def check(self, output, code=1):
        async def fake_run(*a, **k):
            return Result(code, output)
        plume.run = fake_run
        return asyncio.run(plume.check_account(self.email))

    def status(self):
        return db.row("SELECT ok, message FROM account_status WHERE email = ?", self.email)

    def test_refusals_leave_the_state_alone(self):
        for output in (SRP_80045, GSA_503, SMS_403, NEVER_SIGNED_IN, SYNC_22421, NETWORK):
            self.assertIsNone(self.check(output), output)
            self.assertEqual(self.status()["ok"], 1, output)

    def test_message_explains_the_refusal(self):
        self.check(SRP_80045)
        self.assertIn("-80045", self.status()["message"])

    def test_expired_session_asks_for_a_reconnection_with_the_reason(self):
        self.assertIs(self.check(PORTAL_1100), False)
        row = self.status()
        self.assertEqual(row["ok"], 0)
        self.assertIn("deux facteurs", row["message"])

    def test_valid_session(self):
        self.assertIs(self.check("", code=0), True)
        self.assertEqual(self.status()["ok"], 1)


class Backoff(unittest.TestCase):
    APP = {"expires_at": None}

    def test_default_transient_and_slow_delays(self):
        now = 1_000_000.0
        normal = renew._retry_at(self.APP, now) - now
        transient = renew._retry_at(self.APP, now, transient=True) - now
        slow = renew._retry_at(self.APP, now, slow=True) - now
        # first failure of a row: the renewal engine's growing delay (renew.backoff_seconds)
        self.assertEqual(normal, renew.backoff_seconds(1))
        self.assertEqual(transient, renew.backoff_seconds(1, True))
        self.assertLessEqual(transient, min(normal, 15 * 60))
        self.assertGreaterEqual(slow, renew.APPLE_BACKOFF_MINUTES * 60)
        self.assertGreaterEqual(slow, 3 * 3600)

    def test_close_to_expiry_slow_retries_stay_spaced(self):
        now = 1_000_000.0
        close = {"expires_at": now + 6 * 3600}
        self.assertEqual(renew._retry_at(close, now) - now, 10 * 60)
        self.assertEqual(renew._retry_at(close, now, slow=True) - now, 30 * 60)
        # jamais après l'expiration
        very_close = {"expires_at": now + 600}
        self.assertLess(renew._retry_at(very_close, now, slow=True), very_close["expires_at"])


class OneNotificationPerEpisode(unittest.TestCase):
    """Un compte à reconnecter : une alerte, quel que soit le nombre d'apps et de chemins qui le découvrent."""

    def setUp(self):
        db.init()
        self.email = "reconnect@example.com"
        self.notes = []

        async def notify(message, priority="default", event="fail", **extra):
            self.notes.append((message, priority))
            return True
        self.saved_notify = renew.notify
        renew.notify = notify

        store = plume.STORE
        store.mkdir(parents=True, exist_ok=True)
        (store / "accounts.json").write_text(json.dumps({
            "selected_account": self.email,
            "accounts": {self.email: {"first_name": "R", "team_id": "TEAM"}}}), encoding="utf-8")
        plume._LAST = {}
        plume._set_status(self.email, False, "session expirée, reconnexion nécessaire : " + plume.REAUTH_REASON)
        plume.ALERTED.discard(self.email)
        self.device = db.execute("INSERT INTO devices (name, pair_identifier, udid, os_version, created_at) "
                                 "VALUES ('Salon', 'pid-reconnect', 'UDID-RECONNECT', '18.0', 0)")
        self.apps = [db.execute(
            "INSERT INTO apps (name, source_type, device_id, account_email, created_at, enabled, status) "
            "VALUES (?, 'upload', ?, ?, 0, 1, 'pending')", f"App{i}", self.device, self.email) for i in range(2)]

    def tearDown(self):
        renew.notify = self.saved_notify
        (plume.STORE / "accounts.json").unlink(missing_ok=True)
        plume._LAST = {}
        plume.ALERTED.discard(self.email)
        db.execute("DELETE FROM account_status WHERE email = ?", self.email)
        for app_id in self.apps:
            db.execute("DELETE FROM apps WHERE id = ?", app_id)
            db.execute("DELETE FROM events WHERE app_id = ?", app_id)
        db.execute("DELETE FROM devices WHERE id = ?", self.device)

    def test_two_apps_one_notification(self):
        for app_id in self.apps:
            asyncio.run(renew._renew(app_id, "auto"))
        self.assertEqual(len(self.notes), 1, self.notes)
        message, priority = self.notes[0]
        self.assertIn(self.email, message)
        self.assertIn("deux facteurs", message)
        self.assertEqual(priority, "high")
        for app_id in self.apps:
            app = db.row("SELECT * FROM apps WHERE id = ?", app_id)
            self.assertEqual(app["status"], "error")
            self.assertIn("reconnecte-le", app["last_error"])
            self.assertGreater(app["next_attempt_at"], app["last_attempt_at"])

    def test_periodic_check_and_renewal_share_the_episode(self):
        asyncio.run(renew._renew(self.apps[0], "auto"))
        self.assertEqual(len(self.notes), 1)

        async def still_down(email):
            return False
        saved = plume.check_account
        plume.check_account = still_down
        try:
            asyncio.run(renew._check_accounts())
        finally:
            plume.check_account = saved
        self.assertEqual(len(self.notes), 1, "le contrôle périodique ne répète pas l'alerte")

    def test_reconnecting_ends_the_episode(self):
        asyncio.run(renew._renew(self.apps[0], "auto"))
        plume._set_status(self.email, True, "connecté")          # fin d'une connexion réussie
        self.assertNotIn(self.email, plume.ALERTED)
        plume._set_status(self.email, False, "session expirée")
        asyncio.run(renew._renew(self.apps[1], "auto"))
        self.assertEqual(len(self.notes), 2, "un nouvel épisode est signalé de nouveau")


class RefusedRenewals(unittest.TestCase):
    """Un renouvellement que refuse Apple : -80045 et 429 attendent des heures, un 503 réessaie à l'allure
    habituelle, aucun ne passe pour une session expirée."""

    def setUp(self):
        db.init()
        self.email = "refused@example.com"
        store = plume.STORE
        store.mkdir(parents=True, exist_ok=True)
        (store / "accounts.json").write_text(json.dumps({
            "selected_account": self.email,
            "accounts": {self.email: {"first_name": "R", "team_id": "TEAM"}}}), encoding="utf-8")
        plume._LAST = {}
        plume._set_status(self.email, True, "session valide")
        self.device = db.execute("INSERT INTO devices (name, pair_identifier, udid, os_version, created_at) "
                                 "VALUES ('Salon', 'pid-refused', 'UDID-REFUSED', '18.0', 0)")
        self.app = db.execute(
            "INSERT INTO apps (name, source_type, device_id, account_email, created_at, enabled, status) "
            "VALUES ('Refused', 'upload', ?, ?, 0, 1, 'pending')", self.device, self.email)
        (renew.app_dir(self.app) / "original.ipa").write_bytes(b"not read: the signing is refused first")
        self.kind = "apple_refused"

        async def reachable(device):
            return None

        async def refused(*args, **kwargs):
            raise plume.AppleRefused("signature : refus d'Apple", self.kind)
        self.saved = renew.devices.check_reachable, plume.ensure_device
        renew.devices.check_reachable, plume.ensure_device = reachable, refused

    def tearDown(self):
        renew.devices.check_reachable, plume.ensure_device = self.saved
        (plume.STORE / "accounts.json").unlink(missing_ok=True)
        plume._LAST = {}
        plume.ALERTED.discard(self.email)
        db.execute("DELETE FROM account_status WHERE email = ?", self.email)
        db.execute("DELETE FROM events WHERE app_id = ?", self.app)
        db.execute("DELETE FROM apps WHERE id = ?", self.app)
        db.execute("DELETE FROM devices WHERE id = ?", self.device)
        (renew.app_dir(self.app) / "original.ipa").unlink(missing_ok=True)

    def wait_after_failure(self, kind):
        self.kind = kind
        asyncio.run(renew._renew(self.app, "auto"))
        app = db.row("SELECT * FROM apps WHERE id = ?", self.app)
        self.assertEqual(app["status"], "error")
        self.assertEqual(db.row("SELECT ok FROM account_status WHERE email = ?", self.email)["ok"], 1)   # session intacte
        return app["next_attempt_at"] - app["last_attempt_at"]

    def test_minus_80045_and_429_wait_hours_a_503_does_not(self):
        hours = renew.APPLE_BACKOFF_MINUTES * 60
        self.assertGreaterEqual(self.wait_after_failure("apple_refused"), hours)
        self.assertGreaterEqual(self.wait_after_failure("apple_rate_limited"), hours)
        usual = self.wait_after_failure("apple_503")
        fails = db.row("SELECT fail_count FROM apps WHERE id = ?", self.app)["fail_count"]
        self.assertAlmostEqual(usual, renew.backoff_seconds(fails), delta=30)    # the ordinary growing delay
        self.assertLessEqual(usual, renew.RETRY_MINUTES * 60)
        self.assertLess(usual, hours)


class AnisetteDiagnostic(AnisetteCase):
    WINDOWS = {"client_info": "<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>",
               "user_agent": "akd/1.0 CFNetwork/808.1.4"}
    MAC = {"client_info": "<MacBookPro13,2> <macOS;13.1;22C65> <com.apple.AuthKit/1 (com.apple.akd/1.0)>",
           "user_agent": "akd/1.0 CFNetwork/808.1.4"}

    def check(self, answers):
        saved = diag._client_info
        diag._client_info = lambda url: answers.get(url, (None, "injoignable"))
        try:
            return asyncio.run(diag._anisette())
        finally:
            diag._client_info = saved

    def test_default_server_answers(self):
        r = self.check({settings.DEFAULT_ANISETTE: (self.WINDOWS, "")})
        self.assertEqual(r["status"], "ok")
        self.assertIn("Windows", r["detail"])

    def test_fallback_takes_over(self):
        r = self.check({settings.FALLBACK_ANISETTE: (self.WINDOWS, "")})
        self.assertEqual(r["status"], "warn")
        self.assertIn(settings.DEFAULT_ANISETTE, r["detail"])
        self.assertIn(settings.FALLBACK_ANISETTE, r["detail"])

    def test_nothing_answers(self):
        r = self.check({})
        self.assertEqual(r["status"], "error")

    def test_custom_server_does_not_get_a_fallback(self):
        settings.raw_set("anisette_url", "https://anisette.example.org")
        r = self.check({settings.FALLBACK_ANISETTE: (self.WINDOWS, "")})
        self.assertEqual(r["status"], "error")
        self.assertNotIn(settings.FALLBACK_ANISETTE, r["detail"])

    def test_a_server_that_is_not_windows_is_an_error_because_plumesign_refuses_it(self):
        settings.raw_set("anisette_url", "https://anisette.example.org")
        r = self.check({"https://anisette.example.org": (self.MAC, "")})
        self.assertEqual(r["status"], "error")
        self.assertIn("Windows", r["detail"])
        self.assertIn("n'utilisera pas", r["detail"])

    def test_an_odd_answer_never_raises(self):
        for odd in ({"client_info": 42}, {"client_info": ["Windows"]}, {"client_info": None}, {}, []):
            self.assertFalse(diag._windows(odd), odd)
        self.assertTrue(diag._windows(self.WINDOWS))
        self.assertFalse(diag._windows(None))
        # the check itself refuses such an answer before it gets that far
        import io
        import urllib.request

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        saved = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: Answer(b'{"client_info": 42, "user_agent": "x"}')
        try:
            data, error = diag._client_info("https://anisette.example.org")
        finally:
            urllib.request.urlopen = saved
        self.assertIsNone(data)
        self.assertIn("anisette", error)

    def test_the_default_server_asked_explicitly_keeps_its_fallback(self):
        # plumesign does the same with PLUMESIGN_ANISETTE_URL=https://ani.jkcoxson.com
        for value in (settings.DEFAULT_ANISETTE, settings.DEFAULT_ANISETTE + "/", settings.DEFAULT_ANISETTE.upper()):
            settings.raw_set("anisette_url", value)
            self.assertEqual(settings.anisette_servers(), [settings.DEFAULT_ANISETTE, settings.FALLBACK_ANISETTE], value)
        settings.raw_set("anisette_url", settings.FALLBACK_ANISETTE)
        self.assertEqual(settings.anisette_servers(), [settings.FALLBACK_ANISETTE])    # a chosen server is alone
        r = self.check({settings.FALLBACK_ANISETTE: (self.WINDOWS, "")})
        self.assertEqual(r["status"], "ok")


FAKE_PLUMESIGN = r'''
import sys

def say(text):
    print(text, flush=True)

scenario = sys.argv[1]
say("Logging in...")
if scenario == "sms-refused":
    say("[2026-10-15T19:40:01Z INFO  plumesign::commands::account] Enter the code sent to your device, "
        "or type 'sms' to receive it by text message: ")
    sys.stdin.readline()
    say("[2026-10-15T19:40:03Z WARN  plume_core::auth::account::login] Apple refuses the code by SMS (HTTP 403): "
        "enter the code shown on a trusted Apple device")
    say("[2026-10-15T19:40:03Z INFO  plumesign::commands::account] Enter 2FA code from a trusted Apple device "
        "(SMS refused by Apple): ")
    code = sys.stdin.readline().strip()
    if code == "123456":
        say("[2026-10-15T19:40:05Z INFO  plumesign::commands::account] Successfully logged in and account saved.")
        sys.exit(0)
    say("Error: Authentication SRP error -21669: Incorrect verification code.")
    sys.exit(1)
if scenario == "80045":
    say("Error: Authentication SRP error -80045: This action cannot be completed at this time.")
    sys.exit(1)
if scenario == "22421":
    say("Error: Authentication SRP error -22421: This action could not be completed. Try again.")
    sys.exit(1)
if scenario == "other":
    say("[2026-10-15T19:40:03Z INFO  plume_core::auth::account::login] Apple asks the anisette identity to synchronize (hsc 434)")
    say("Error: Authentication SRP error -20970: Your account information was entered incorrectly.")
    sys.exit(1)
sys.exit(2)
'''


class LoginSession(unittest.TestCase):
    """start_login pilote plumesign par ses lignes : l'invite du code, l'échec, l'état du compte."""

    def setUp(self):
        from app import proc
        db.init()
        self.proc = proc
        self.email = "login@example.com"
        self.script = Path(tempfile.mkdtemp(prefix="tvsign-fake-")) / "fake_plumesign.py"
        self.script.write_text(FAKE_PLUMESIGN, encoding="utf-8")
        self.real_exec = proc._exec
        self.scenario = None
        self.resets = []
        self.real_reset = plume.reset_anisette
        plume.reset_anisette = lambda reason: self.resets.append(reason) or True
        self.logged = []
        self.real_log = db.log
        db.log = lambda message, level="info", app_id=None: self.logged.append((level, message))
        scenario_of = lambda: self.scenario     # noqa: E731

        async def fake_exec(*args, **kw):
            return await self.real_exec(sys.executable, "-I", str(self.script), scenario_of(), **kw)
        proc._exec = fake_exec

    def tearDown(self):
        self.proc._exec = self.real_exec
        plume.reset_anisette = self.real_reset
        db.log = self.real_log
        self.proc.SESSIONS.clear()
        plume.ALERTED.discard(self.email)
        db.execute("DELETE FROM account_status WHERE email = ?", self.email)

    async def until(self, session, *states, seconds=15):
        for _ in range(int(seconds / 0.05)):
            if session.state in states:
                return
            await asyncio.sleep(0.05)
        self.fail(f"session still {session.state}: {session.lines}")

    async def settled(self, done, seconds=10):
        """Attend la fin du rappel de fin de session (l'état change avant qu'il ne s'exécute)."""
        for _ in range(int(seconds / 0.05)):
            if done():
                return
            await asyncio.sleep(0.05)
        self.fail(f"callback never completed: {self.logged}")

    def test_sms_refused_prompt_and_success(self):
        self.scenario = "sms-refused"

        async def scenario():
            session = plume.start_login(self.email, "not-a-real-password")
            await self.until(session, "waiting")
            self.assertIn("type 'sms'", session.prompt)
            await session.send("sms")
            await asyncio.sleep(0.2)
            await self.until(session, "waiting")
            # la nouvelle invite : sans l'offre du SMS, avec la raison (l'interface la reconnaît à « SMS refused »)
            self.assertIn("Enter 2FA code from a trusted Apple device", session.prompt)
            self.assertIn("SMS refused", session.prompt)
            self.assertNotIn("type 'sms'", session.prompt)
            await session.send("123456")
            await self.until(session, "done", "failed")
            self.assertEqual(session.state, "done")
            await self.settled(lambda: any("connecté" in m for _, m in self.logged))
            row = db.row("SELECT ok FROM account_status WHERE email = ?", self.email)
            self.assertEqual(row["ok"], 1)
        asyncio.run(scenario())
        self.assertTrue(any("connecté" in m for _, m in self.logged), self.logged)

    def test_minus_80045_fails_calmly(self):
        self.scenario = "80045"

        async def scenario():
            session = plume.start_login(self.email, "not-a-real-password")
            await self.until(session, "failed", "done")
            self.assertEqual(session.state, "failed")
            self.assertIn("-80045", "\n".join(session.lines))
            await self.settled(lambda: any("échouée" in m for _, m in self.logged))
        asyncio.run(scenario())
        level, message = self.logged[-1]
        self.assertEqual(level, "error")
        self.assertIn("-80045", message)
        self.assertIn("n'insiste pas", message)
        self.assertIsNone(db.row("SELECT ok FROM account_status WHERE email = ?", self.email))
        self.assertEqual(self.resets, [])

    def test_the_failure_line_of_the_journal_shows_the_error_not_a_log_line(self):
        self.scenario = "other"

        async def scenario():
            session = plume.start_login(self.email, "not-a-real-password")
            await self.until(session, "failed", "done")
            await self.settled(lambda: any("échouée" in m for _, m in self.logged))
        asyncio.run(scenario())
        message = self.logged[-1][1]
        self.assertIn("-20970", message)
        self.assertNotIn("INFO", message)
        self.assertNotIn("synchronize", message)
        self.assertEqual(self.resets, [])

    def test_refused_anisette_data_reset_the_identity(self):
        self.scenario = "22421"

        async def scenario():
            session = plume.start_login(self.email, "not-a-real-password")
            await self.until(session, "failed", "done")
            await self.settled(lambda: any("échouée" in m for _, m in self.logged))
        asyncio.run(scenario())
        self.assertEqual(len(self.resets), 1)
        self.assertIn("anisette", self.logged[-1][1])


def web_causes() -> list[tuple]:
    """Les règles CAUSES de web/app.js (expression, clé), lues dans le fichier : les expressions simples de
    JavaScript sont aussi des expressions régulières de Python."""
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    start = js.index("const CAUSES = [")
    block = js[start:js.index("\n];", start)]
    rules = []
    for m in re.finditer(r'^\s*\[/(.+)/([a-z]*), "(c_[a-z0-9_]+)"\],?\s*$', block, re.M):
        rules.append((re.compile(m.group(1), re.I if "i" in m.group(2) else 0), m.group(3)))
    return rules


def web_cause(text: str) -> str | None:
    return next((key for rx, key in web_causes() if rx.search(text)), None)


class WebCauses(unittest.TestCase):
    """Le message qu'affiche l'interface pour une erreur : celui de plumesign, et celui que tvsign en fait."""

    def test_the_rules_are_all_read(self):
        keys = [key for _, key in web_causes()]
        self.assertGreater(len(keys), 20)
        for key in ("c_apple_blocked", "c_rate_limited", "c_gsa_503", "c_anisette_unsupported", "c_session"):
            self.assertIn(key, keys)

    def test_what_plumesign_writes(self):
        for output, key in ((SRP_80045, "c_apple_blocked"), (GSA_503, "c_gsa_503"), (PORTAL_503, "c_gsa_503"),
                            (PROVISIONING_503, "c_gsa_503"), (GSA_429, "c_rate_limited"),
                            (PORTAL_429, "c_rate_limited"), (SMS_403, "c_sms_denied"),
                            (NEVER_SIGNED_IN, "c_never_signed_in"), (NOT_WINDOWS, "c_anisette_unsupported"),
                            (NOT_WINDOWS_BOTH, "c_anisette_unsupported"), (SYNC_22421, "c_anisette_refused"),
                            (NOT_PROVISIONED, "c_anisette_refused"), (PORTAL_1100, "c_session")):
            self.assertEqual(web_cause(output), key, output)

    def test_what_tvsign_says_about_it(self):
        for kind, key in (("apple_refused", "c_apple_blocked"), ("apple_rate_limited", "c_rate_limited"),
                          ("apple_503", "c_gsa_503"), ("sms_denied", "c_sms_denied"),
                          ("never_signed_in", "c_never_signed_in"), ("anisette_unsupported", "c_anisette_unsupported"),
                          ("anisette_refused", "c_anisette_refused"), ("anisette_reprovision", "c_anisette_refused")):
            self.assertEqual(web_cause(plume.explain(kind, "Error: x")), key, kind)

    def test_every_cause_of_the_rules_has_a_title_and_an_advice(self):
        i18n = (ROOT / "web" / "i18n.js").read_text(encoding="utf-8")
        for _, key in web_causes():
            for suffix in ("_t", "_a"):
                self.assertIn(f"  {key}{suffix}: [", i18n, key + suffix)


class DocumentationMatchesDefaults(unittest.TestCase):
    """Les textes de l'interface et le README disent le même serveur par défaut que le code."""

    def test_readme_in_both_languages(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        english, french = readme.split("## Français")
        for text in (english, french):
            self.assertIn(settings.DEFAULT_ANISETTE.removeprefix("https://"), text)
            self.assertIn(settings.FALLBACK_ANISETTE.removeprefix("https://"), text)
            self.assertNotIn("`ani.sidestore.app` by default", text)
            self.assertNotIn("`ani.sidestore.app` par défaut", text)

    def test_settings_help_text(self):
        i18n = (ROOT / "web" / "i18n.js").read_text(encoding="utf-8")
        note = next(l for l in i18n.splitlines() if l.lstrip().startswith("anisette_note"))
        self.assertIn("ani.jkcoxson.com", note)
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f'placeholder="{settings.DEFAULT_ANISETTE}"', html)
        self.assertNotIn("ani.sidestore.app", html)

    def test_every_new_cause_is_translated(self):
        i18n = (ROOT / "web" / "i18n.js").read_text(encoding="utf-8")
        for key in ("c_apple_blocked", "c_gsa_503", "c_rate_limited", "c_sms_denied", "c_never_signed_in",
                    "c_anisette_unsupported", "c_anisette_refused"):
            for suffix in ("_t", "_a"):
                self.assertIn(f"  {key}{suffix}: [", i18n, key + suffix)
        self.assertIn("prompt_device_sms_denied", i18n)
        self.assertIn("acc_reauth_reason", i18n)


if __name__ == "__main__":
    unittest.main()
