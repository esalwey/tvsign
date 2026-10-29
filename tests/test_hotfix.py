"""Hotfixes 1.0.1 and 1.0.2 : Apple refuse les données anisette selon l'ère (Android jusqu'au 12 octobre 2026,
Windows jusqu'au 22, macOS depuis) ; aucune plateforme n'est donc imposée.

Classement des erreurs (-80045, HTTP 503, SMS 403, -22411, -45061), liste des serveurs anisette (par défaut,
choisi par l'utilisateur, repli désactivé), identité client de secours, alerte unique d'un compte à reconnecter.
Les textes d'erreur ci-dessous sont ceux que plumesign écrit (tests Rust de vendor/Impactor : plume_core et
omnisette). Sans plumesign ni Apple TV ni Internet :

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
ANISETTE_ENVS = ("TVSIGN_ANISETTE_URL", "PLUMESIGN_ANISETTE_URL", "ANISETTE_URL_V3", "TVSIGN_ANISETTE_FALLBACK",
                 "TVSIGN_ANISETTE_CLIENT_INFO", "PLUMESIGN_ANISETTE_CLIENT_INFO", "TVSIGN_ANISETTE_USER_AGENT",
                 "PLUMESIGN_ANISETTE_USER_AGENT", "PLUMESIGN_ANISETTE_FALLBACK_URL")
ANISETTE_SETTINGS = ("anisette_url", "anisette_fallback", "anisette_client_info", "anisette_user_agent")
DEFAULTS = ["https://mac-ani.jkcoxson.com", "https://ani.iloader.app", "https://new.ani.sidestore.io",
            "https://ani3.stikstore.app", "https://ani.jkcoxson.com"]
# Les serveurs que 1.0.1 refusait d'après leur nom (Android), et les deux serveurs « Windows » de ses défauts :
# ce sont des serveurs comme les autres, qui répondent ou non, et dont Apple accepte ou non les données.
OLD_ANDROID = ["ani.sidestore.app", "ani.stikstore.app", "anisette.crystall1ne.dev",
               "ani3server.fly.dev", "ani.jaydenha.uk", "omni.ethxn.xyz"]
OLD_WINDOWS_DEFAULTS = ["ani.jkcoxson.com", "ani2.stikstore.app"]


class AnisetteCase(unittest.TestCase):
    """Isole les réglages anisette (base partagée avec les autres tests, environnement du processus)
    et enregistre les lignes de journal au lieu de les écrire (db.log fusionne les répétitions)."""

    def setUp(self):
        db.init()
        self.saved_env = {n: os.environ.pop(n, None) for n in ANISETTE_ENVS}
        for key in ANISETTE_SETTINGS:
            settings.raw_set(key, None)
        self.logged = []
        self.real_log = db.log
        db.log = lambda message, level="info", app_id=None: self.logged.append((level, message))

    def tearDown(self):
        db.log = self.real_log
        for key in ANISETTE_SETTINGS:
            settings.raw_set(key, None)
        for name, value in self.saved_env.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value


class ServerList(AnisetteCase):
    def test_the_defaults(self):
        self.assertEqual(list(settings.DEFAULT_ANISETTE_SERVERS), DEFAULTS)
        self.assertEqual(settings.DEFAULT_ANISETTE, "https://mac-ani.jkcoxson.com")
        self.assertEqual(diag.DEFAULT_ANISETTE, settings.DEFAULT_ANISETTE)
        self.assertEqual(settings.anisette_servers(), DEFAULTS)
        for key in ("anisette_url", "anisette_client_info", "anisette_user_agent"):
            self.assertEqual(settings.describe()[key]["value"], "", key)                # vides par défaut
        self.assertEqual(settings.describe()["anisette_fallback"]["value"], "on")

    def test_the_defaults_are_the_ones_plumesign_has(self):
        rust = (ROOT / "vendor" / "Impactor" / "3rdparty" / "omnisette" / "src" / "lib.rs").read_text(encoding="utf-8")
        block = rust[rust.index("pub const DEFAULT_ANISETTE_SERVERS_V3"):]
        block = block[:block.index("];")]
        self.assertEqual(re.findall(r'"(https://[^"]+)"', block), DEFAULTS)

    def test_no_server_is_judged_by_its_name(self):
        # 1.0.1 remplaçait ou refusait ces adresses d'après leur nom : plus maintenant
        for host in OLD_ANDROID + OLD_WINDOWS_DEFAULTS:
            url = f"https://{host}"
            settings.update({"anisette_url": url})
            self.assertEqual(settings.anisette_url(), url, host)
            servers = settings.anisette_servers()
            self.assertEqual(servers[0], url, host)
            self.assertEqual([s for s in servers[1:]], [d for d in DEFAULTS if not settings.same_server(d, url)], host)
            self.assertEqual(plume.env()["PLUMESIGN_ANISETTE_URL"], url, host)
        self.assertEqual(self.logged, [])

    def test_the_server_of_the_user_goes_first_and_the_defaults_follow(self):
        settings.update({"anisette_url": "https://anisette.example.org"})
        self.assertEqual(settings.anisette_servers(), ["https://anisette.example.org"] + DEFAULTS)

    def test_a_default_chosen_by_the_user_moves_to_the_front_once(self):
        for value in (DEFAULTS[2], DEFAULTS[2] + "/", DEFAULTS[2].replace("new.", "NEW.")):
            settings.raw_set("anisette_url", value)
            servers = settings.anisette_servers()
            self.assertEqual(servers[0], value.strip(), value)
            self.assertEqual(len(servers), len(DEFAULTS), value)
            self.assertEqual(sum(settings.same_server(s, DEFAULTS[2]) for s in servers), 1, value)

    def test_fallback_off_leaves_the_server_of_the_user_alone(self):
        settings.update({"anisette_url": "https://anisette.example.org", "anisette_fallback": "off"})
        self.assertFalse(settings.anisette_fallback())
        self.assertEqual(settings.anisette_servers(), ["https://anisette.example.org"])
        # sans serveur choisi, le premier des serveurs par défaut, seul
        settings.update({"anisette_url": ""})
        self.assertEqual(settings.anisette_servers(), [DEFAULTS[0]])
        settings.update({"anisette_fallback": None})
        self.assertTrue(settings.anisette_fallback())
        self.assertEqual(settings.anisette_servers(), DEFAULTS)

    def test_same_server_ignores_case_and_the_trailing_slash(self):
        self.assertTrue(settings.same_server("https://A.example/", "https://a.example"))
        self.assertFalse(settings.same_server("https://a.example", "https://b.example"))

    def test_nothing_is_migrated_and_nothing_is_said_at_start(self):
        # 1.0.1 remplaçait un serveur Android enregistré par le défaut, avec une ligne dans le journal
        self.assertFalse(hasattr(settings, "migrate"))
        for name in ("main.py", "backup.py"):
            self.assertNotIn("settings.migrate", (ROOT / "app" / name).read_text(encoding="utf-8"), name)
        for host in OLD_ANDROID + OLD_WINDOWS_DEFAULTS:
            settings.raw_set("anisette_url", f"https://{host}")
            self.assertEqual(settings.anisette_url(), f"https://{host}", host)         # gardé tel quel
            self.assertEqual(settings.describe()["anisette_url"]["source"], "db", host)
        self.assertEqual(self.logged, [])

    def test_old_values_in_the_environment_are_used_not_ignored(self):
        for name in ("TVSIGN_ANISETTE_URL", "PLUMESIGN_ANISETTE_URL", "ANISETTE_URL_V3"):
            os.environ[name] = "https://ani.sidestore.app"
            try:
                info = settings.describe()["anisette_url"]
                self.assertEqual(info["value"], "https://ani.sidestore.app", name)
                self.assertTrue(info["locked"], name)
                self.assertEqual(info["env"], name)
                self.assertEqual(plume.env()["PLUMESIGN_ANISETTE_URL"], "https://ani.sidestore.app", name)
            finally:
                os.environ.pop(name)
        self.assertEqual(self.logged, [])

    def test_custom_server_in_the_environment_wins(self):
        os.environ["PLUMESIGN_ANISETTE_URL"] = "https://anisette.example.org"
        self.assertEqual(settings.anisette_url(), "https://anisette.example.org")
        self.assertTrue(settings.describe()["anisette_url"]["locked"])


class LegacyIdentity(AnisetteCase):
    """L'identité anisette unique des versions précédentes : signalée une fois, jamais gardée. Il y en a une
    par serveur maintenant, dans le dossier anisette/ (voir vendor/Impactor/PATCHES.md)."""

    def setUp(self):
        super().setUp()
        plume.STORE.mkdir(parents=True, exist_ok=True)
        self.dir = plume.STORE / plume.ANISETTE_DIR
        self.dir.mkdir(exist_ok=True)
        self.legacy = [plume.STORE / name for name in plume.LEGACY_ANISETTE_FILES]
        self.states = [self.dir / "mac-ani.jkcoxson.com.plist", self.dir / "ani.iloader.app.plist"]
        self.last = self.dir / plume.LAST_SERVER_FILE

    def tearDown(self):
        for f in self.legacy + self.states + [self.last]:
            f.unlink(missing_ok=True)
        super().tearDown()

    def test_the_old_identities_are_mentioned_once_without_blaming_a_platform(self):
        for old in self.legacy:
            old.write_bytes(b"old")
            self.logged.clear()
            self.assertTrue(plume.note_legacy_anisette(), old.name)
            self.assertEqual(len(self.logged), 1)
            message = self.logged[0][1]
            self.assertIn("identité", message)
            self.assertNotIn("Android", message)
            self.assertNotIn("Windows", message)
            old.unlink()

    def test_the_two_files_of_the_earlier_versions_are_the_ones_plumesign_removes(self):
        self.assertEqual(plume.LEGACY_ANISETTE_FILES, ("state.plist", "anisette-state-windows.plist"))
        rust = (ROOT / "vendor" / "Impactor" / "3rdparty" / "omnisette" / "src" / "remote_anisette_v3.rs").read_text(encoding="utf-8")
        for name in plume.LEGACY_ANISETTE_FILES:
            self.assertIn(f'"{name}"', rust)

    def test_nothing_to_say_without_it_or_once_the_new_ones_exist(self):
        self.assertFalse(plume.note_legacy_anisette())
        for old in self.legacy:
            old.write_bytes(b"old")
        self.states[0].write_bytes(b"new")
        self.assertFalse(plume.note_legacy_anisette())
        self.assertEqual(self.logged, [])

    def test_the_folder_and_file_names_are_the_ones_plumesign_writes(self):
        rust = (ROOT / "vendor" / "Impactor" / "3rdparty" / "omnisette" / "src" / "remote_anisette_v3.rs").read_text(encoding="utf-8")
        self.assertIn(f'pub const STATE_DIR: &str = "{plume.ANISETTE_DIR}";', rust)
        self.assertIn(f'pub const LAST_SERVER_FILE: &str = "{plume.LAST_SERVER_FILE}";', rust)
        self.assertIn('format!("{}.plist", server_key(server))', rust)

    def test_resetting_the_identities_removes_all_of_them_and_not_twice_an_hour(self):
        for f in self.states:
            f.write_bytes(b"new")
        self.last.write_text("{}", encoding="utf-8")
        self.legacy[0].write_bytes(b"old")
        plume._RESET_AT = 0.0
        try:
            self.assertTrue(plume.reset_anisette("test"))
            for f in self.states:
                self.assertFalse(f.exists(), f.name)
            self.assertTrue(self.last.exists())          # quel serveur a marché n'est pas une identité
            self.assertTrue(self.legacy[0].exists())
            self.states[0].write_bytes(b"newer")
            self.assertFalse(plume.reset_anisette("test again"))       # moins d'une heure plus tard
            self.assertTrue(self.states[0].exists())
        finally:
            plume._RESET_AT = 0.0
            self.legacy[0].unlink(missing_ok=True)

    def test_nothing_to_reset_is_not_a_reset(self):
        plume._RESET_AT = 0.0
        self.assertFalse(plume.reset_anisette("test"))
        self.assertEqual(plume._RESET_AT, 0.0)

    def test_the_server_that_worked_last_is_read_from_plumesigns_file(self):
        servers = settings.anisette_servers()
        self.last.write_text(json.dumps({"server": servers[2], "servers": servers, "at": "2026-10-29T10:00:00+00:00"}),
                             encoding="utf-8")
        self.assertEqual(plume.last_server(), {"server": servers[2], "at": "2026-10-29T10:00:00+00:00"})
        # une autre liste de serveurs : plumesign l'ignorerait, tvsign ne la montre pas non plus
        self.last.write_text(json.dumps({"server": servers[2], "servers": servers[::-1]}), encoding="utf-8")
        self.assertIsNone(plume.last_server())
        for odd in ("", "{", "[]", '{"server": 3}', '{"server": "x", "servers": "y"}'):
            self.last.write_text(odd, encoding="utf-8")
            self.assertIsNone(plume.last_server(), odd)
        self.last.unlink()
        self.assertIsNone(plume.last_server())


class SettingValidation(AnisetteCase):
    def test_the_interface_accepts_any_address_even_an_old_android_one(self):
        for host in OLD_ANDROID:
            settings.update({"anisette_url": f"https://{host}"})
            self.assertEqual(settings.anisette_url(), f"https://{host}", host)

    def test_custom_default_and_empty_are_accepted(self):
        for value in ("https://anisette.example.org", settings.DEFAULT_ANISETTE, "https://ani2.stikstore.app"):
            settings.update({"anisette_url": value})
            self.assertEqual(settings.anisette_url(), value)
        settings.update({"anisette_url": ""})
        self.assertIsNone(settings.raw_get("anisette_url"))
        self.assertEqual(settings.anisette_url(), "")

    def test_a_bad_address_is_still_refused(self):
        with self.assertRaises(ValueError):
            settings.update({"anisette_url": "ani.jkcoxson.com"})

    def test_the_fallback_is_on_or_off(self):
        for value in ("on", "off"):
            settings.update({"anisette_fallback": value})
            self.assertEqual(settings.get("anisette_fallback"), value)
        for bad in ("maybe", "true", "1"):
            with self.assertRaises(ValueError) as ctx:
                settings.update({"anisette_fallback": bad})
            self.assertIn("anisette_fallback", ctx.exception.args[0])
        self.assertEqual(settings.get("anisette_fallback"), "off")      # le refus n'a rien changé

    def test_the_client_info_override_is_empty_by_default_and_only_takes_header_text(self):
        self.assertEqual(settings.anisette_client_info(), "")
        self.assertEqual(settings.anisette_user_agent(), "")
        pair = ("<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>", "akd/1.0 CFNetwork/811.5.4")
        settings.update({"anisette_client_info": pair[0], "anisette_user_agent": pair[1]})
        self.assertEqual((settings.anisette_client_info(), settings.anisette_user_agent()), pair)
        for bad in ("a\nb", "café", "x" * 301):
            for key in ("anisette_client_info", "anisette_user_agent"):
                with self.assertRaises(ValueError, msg=f"{key} {bad!r}"):
                    settings.update({key: bad})
        settings.update({"anisette_client_info": "", "anisette_user_agent": None})
        self.assertEqual((settings.anisette_client_info(), settings.anisette_user_agent()), ("", ""))

    def test_the_environment_locks_the_override_and_the_fallback(self):
        os.environ["TVSIGN_ANISETTE_CLIENT_INFO"] = "<X> <Y> <Z>"
        os.environ["PLUMESIGN_ANISETTE_USER_AGENT"] = "akd/1.0 X"
        os.environ["TVSIGN_ANISETTE_FALLBACK"] = "off"
        info = settings.describe()
        self.assertEqual(info["anisette_client_info"]["value"], "<X> <Y> <Z>")
        self.assertEqual(info["anisette_client_info"]["env"], "TVSIGN_ANISETTE_CLIENT_INFO")
        self.assertEqual(info["anisette_user_agent"]["env"], "PLUMESIGN_ANISETTE_USER_AGENT")
        self.assertTrue(all(info[k]["locked"] for k in ("anisette_client_info", "anisette_user_agent", "anisette_fallback")))
        self.assertFalse(settings.anisette_fallback())
        with self.assertRaises(ValueError):
            settings.update({"anisette_client_info": "<other>"})


class SettingsApi(AnisetteCase):
    """Ce que voit l'interface : les réglages du serveur anisette par l'API."""

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

    def test_an_old_android_server_is_saved_like_any_other(self):
        r = self.client.patch("/api/settings", headers=self.HEADERS, json={"anisette_url": "https://ani.stikstore.app"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["settings"]["anisette_url"]["value"], "https://ani.stikstore.app")
        self.assertEqual(settings.raw_get("anisette_url"), "https://ani.stikstore.app")

    def test_a_custom_server_is_saved_and_the_empty_value_returns_to_the_default(self):
        r = self.client.patch("/api/settings", headers=self.HEADERS, json={"anisette_url": "https://anisette.example.org"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["settings"]["anisette_url"]["value"], "https://anisette.example.org")
        r = self.client.patch("/api/settings", headers=self.HEADERS, json={"anisette_url": None})
        self.assertEqual(r.json()["settings"]["anisette_url"]["value"], "")
        self.assertEqual(r.json()["settings"]["anisette_url"]["source"], "default")

    def test_the_fallback_and_the_override_are_in_the_settings(self):
        info = self.client.get("/api/settings").json()["settings"]
        self.assertEqual(info["anisette_fallback"]["value"], "on")
        self.assertEqual(info["anisette_client_info"]["value"], "")
        self.assertEqual(info["anisette_user_agent"]["value"], "")
        r = self.client.patch("/api/settings", headers=self.HEADERS, json={
            "anisette_fallback": "off", "anisette_client_info": "<Mac15,7> <macOS;27.0> <com.apple.AuthKit/1>",
            "anisette_user_agent": "akd/1.0 CFNetwork/811.5.4"})
        self.assertEqual(r.status_code, 200, r.text)
        info = r.json()["settings"]
        self.assertEqual(info["anisette_fallback"]["value"], "off")
        self.assertEqual(info["anisette_client_info"]["source"], "db")
        r = self.client.patch("/api/settings", headers=self.HEADERS, json={"anisette_fallback": "sometimes"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("anisette_fallback", r.json()["detail"])

    def test_the_settings_form_has_a_field_for_each_of_them(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for name in ("anisette_url", "anisette_fallback", "anisette_client_info", "anisette_user_agent"):
            self.assertIn(f'name="{name}"', html, name)


class PlumesignEnvironment(AnisetteCase):
    def test_default_servers_set_nothing(self):
        e = plume.env()
        for name in ("PLUMESIGN_ANISETTE_URL", "ANISETTE_URL_V3", "PLUMESIGN_ANISETTE_FALLBACK_URL",
                     "PLUMESIGN_ANISETTE_CLIENT_INFO", "PLUMESIGN_ANISETTE_USER_AGENT"):
            self.assertNotIn(name, e)

    def test_custom_server_is_passed_and_the_defaults_stay_behind_it(self):
        settings.raw_set("anisette_url", "https://anisette.example.org")
        e = plume.env()
        self.assertEqual(e["PLUMESIGN_ANISETTE_URL"], "https://anisette.example.org")
        self.assertNotIn("PLUMESIGN_ANISETTE_FALLBACK_URL", e)

    def test_disabled_fallback_is_none_for_plumesign(self):
        settings.raw_set("anisette_url", "https://anisette.example.org")
        settings.raw_set("anisette_fallback", "off")
        e = plume.env()
        self.assertEqual(e["PLUMESIGN_ANISETTE_URL"], "https://anisette.example.org")
        self.assertEqual(e["PLUMESIGN_ANISETTE_FALLBACK_URL"], "none")

    def test_the_override_goes_to_plumesign_when_there_is_one(self):
        settings.raw_set("anisette_client_info", "<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>")
        e = plume.env()
        self.assertEqual(e["PLUMESIGN_ANISETTE_CLIENT_INFO"], settings.anisette_client_info())
        self.assertNotIn("PLUMESIGN_ANISETTE_USER_AGENT", e)
        settings.raw_set("anisette_user_agent", "akd/1.0 CFNetwork/811.5.4")
        self.assertEqual(plume.env()["PLUMESIGN_ANISETTE_USER_AGENT"], "akd/1.0 CFNetwork/811.5.4")

    def test_the_server_list_of_the_environment_reaches_plumesign_untouched(self):
        # la façon de changer la liste par défaut sans recompiler, documentée dans le README
        os.environ["PLUMESIGN_ANISETTE_FALLBACK_URL"] = "https://a.example https://b.example"
        self.assertEqual(plume.env()["PLUMESIGN_ANISETTE_FALLBACK_URL"], "https://a.example https://b.example")

    def test_what_the_settings_do_not_accept_is_not_passed_blindly(self):
        # une valeur invalide de l'environnement est ignorée par les réglages, donc par plumesign aussi
        os.environ["ANISETTE_URL_V3"] = "not a url"
        os.environ["PLUMESIGN_ANISETTE_CLIENT_INFO"] = "café"
        e = plume.env()
        self.assertNotIn("ANISETTE_URL_V3", e)
        self.assertNotIn("PLUMESIGN_ANISETTE_URL", e)
        self.assertNotIn("PLUMESIGN_ANISETTE_CLIENT_INFO", e)

    def test_the_old_platform_flag_is_not_ours_to_judge(self):
        # plumesign l'accepte et l'ignore : tvsign ne le lit ni ne le retire
        os.environ["PLUMESIGN_ANISETTE_ALLOW_ANY_PLATFORM"] = "1"
        try:
            self.assertEqual(plume.env().get("PLUMESIGN_ANISETTE_ALLOW_ANY_PLATFORM"), "1")
        finally:
            os.environ.pop("PLUMESIGN_ANISETTE_ALLOW_ANY_PLATFORM")


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
# every server failed (down, bad answer, provisioning refused...): the error lists them all
ALL_SERVERS_FAILED = ("Error: Anisette error: no anisette server could be used: https://mac-ani.jkcoxson.com: "
                      "Request Error error sending request for url (https://mac-ani.jkcoxson.com/v3/client_info) | "
                      "https://ani.iloader.app: anisette server error: https://ani.iloader.app/v3/client_info answered "
                      "HTTP 502 without client info: 502 Bad Gateway")


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

    def test_the_windows_only_class_is_gone(self):
        # 1.0.1 had a class for a server that did not announce Windows; plumesign never says so any more
        self.assertFalse(hasattr(plume, "_ANISETTE_UNSUPPORTED"))
        self.assertNotIn("anisette_unsupported", plume.USER_KINDS)
        old = ("Error: Anisette error: anisette server not usable: https://x announces `<Mac>`: "
               "Apple only accepts Windows anisette since 2026-10-12")
        self.assertNotEqual(plume.classify(old), "anisette_unsupported")
        self.assertIsNone(plume.explain("anisette_unsupported"))

    def test_every_server_failing_is_a_passing_problem(self):
        # not a class of its own: it only says "anisette", and the usual automatic retry follows
        self.assertIsNone(plume.classify(ALL_SERVERS_FAILED))
        self.assertFalse(plume.session_expired(ALL_SERVERS_FAILED))

    def test_a_page_of_an_anisette_server_is_not_a_refusal_of_apple(self):
        # an anisette server that is overloaded or limits requests says 502, 503 or 429 in its own words;
        # none of it is Apple answering, and a 429 must not make tvsign wait hours
        for what in ("429 Too Many Requests", "Too Many Requests", "503 Service Temporarily Unavailable",
                     "502 Bad Gateway", "504 Gateway Time-out"):
            for step in ("client info", "headers"):
                path = "client_info" if step == "client info" else "get_headers"
                output = (f"Error: Anisette error: anisette server error: https://ani.iloader.app/v3/{path} "
                          f"answered HTTP 502 without {step}: {what}")
                self.assertIsNone(plume.classify(output), output)
                self.assertIsInstance(plume._failure("a@b.c", "signature", Result(1, output)), plume.Transient)
        # ... while Apple's own pages still are
        self.assertEqual(plume.classify(GSA_503), "apple_503")
        self.assertEqual(plume.classify(GSA_429), "apple_rate_limited")
        # and a page of an anisette server in the middle of the line does not hide what comes after it
        mixed = ("Error: Anisette error: no anisette server could be used: https://a.example: anisette server error: "
                 "https://a.example/v3/client_info answered HTTP 429 without client info: Too Many Requests | "
                 "https://b.example: anisette server error: ADI error -45061")
        self.assertEqual(plume.classify(mixed), "anisette_reprovision")
        db.execute("DELETE FROM account_status WHERE email = 'a@b.c'")

    def test_network_problems_are_neither(self):
        self.assertIsNone(plume.classify(NETWORK))
        self.assertFalse(plume.session_expired(NETWORK))
        self.assertFalse(plume.session_expired("Error: Reqwest error: operation timed out"))


# what plumesign says (log lines) when it leaves a server of the list: a server that cannot be reached, then a
# server whose data Apple refuses; the other lines are not about the servers
SERVER_CHANGES = (
    "[2026-10-29T10:00:00Z INFO  omnisette::remote_anisette_v3] anisette server https://ani.iloader.app announces "
    "`<MacBookPro13,3> <Mac OS X;10.12.6;16G29> <com.apple.AuthKit/1 (com.apple.akd/1.0)>` with user agent `akd/1.0`\n"
    "[2026-10-29T10:00:01Z WARN  omnisette::remote_anisette_v3] anisette server https://mac-ani.jkcoxson.com is not usable: "
    "Request Error error sending request for url (https://mac-ani.jkcoxson.com/v3/client_info)\n"
    "[2026-10-29T10:00:02Z WARN  plume_core::auth::account] GrandSlam is busy (HTTP 503): trying again in 3 s (1/1)\n"
    "[2026-10-29T10:00:05Z WARN  plume_core::auth::account::login] Apple refuses the data (-80045): the data of "
    "https://ani.iloader.app are given up, using https://new.ani.sidestore.io instead\n"
    "[2026-10-29T10:00:06Z WARN  omnisette::remote_anisette_v3] anisette server https://mac-ani.jkcoxson.com is not usable: "
    "Request Error error sending request for url (https://mac-ani.jkcoxson.com/v3/client_info)\n")


class JournalOfServerChanges(unittest.TestCase):
    """Un serveur anisette quitté par plumesign se voit dans le journal de tvsign."""

    def setUp(self):
        self.logged = []
        self.real_log = db.log
        db.log = lambda message, level="info", app_id=None: self.logged.append((level, message))

    def tearDown(self):
        db.log = self.real_log

    def test_only_the_lines_about_a_server_that_was_left_are_reported_once_each(self):
        self.assertEqual(plume.note_anisette_events(SERVER_CHANGES), 2)
        self.assertEqual([level for level, _ in self.logged], ["warn", "warn"])
        self.assertTrue(self.logged[0][1].startswith("Serveur anisette : anisette server https://mac-ani.jkcoxson.com"))
        self.assertIn("are given up", self.logged[1][1])

    def test_nothing_to_report_without_such_lines(self):
        for output in ("", SRP_80045, GSA_503, "[2026-10-29T10:00:00Z INFO  omnisette::x] anisette server y"):
            self.assertEqual(plume.note_anisette_events(output), 0, output)
        self.assertEqual(self.logged, [])

    def test_the_fallback_line_of_a_retried_sign_in_is_one_of_them(self):
        self.assertEqual(plume.note_anisette_events(BUSY_THEN_WRONG_CODE), 1)
        self.assertIn("https://ani.jkcoxson.com is not usable", self.logged[0][1])

    def test_a_long_message_is_cut(self):
        long = "[2026-10-29T10:00:00Z WARN  omnisette::remote_anisette_v3] anisette server https://x is not usable: " + "y" * 900
        plume.note_anisette_events(long)
        self.assertLessEqual(len(self.logged[0][1]), 330)


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

    def test_every_anisette_server_failing_is_retried_by_itself(self):
        e = self.fail(ALL_SERVERS_FAILED)
        self.assertIsInstance(e, plume.Transient)
        self.assertIn("passager", str(e))
        self.assertNotIn("Windows", str(e))
        self.assertEqual(self.reset_calls, [])

    def test_the_503_message_does_not_blame_a_platform(self):
        for output in (GSA_503, PROVISIONING_503, PORTAL_503):
            text = str(self.fail(output))
            for banned in ("Android", "Windows", "12 octobre 2026"):
                self.assertNotIn(banned, text, banned)

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
        plume._locked, plume._as = (lambda: Lock()), (lambda e: Lock())

    def tearDown(self):
        plume._locked, plume._as, plume.run = self.saved
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

    def test_a_server_that_plumesign_gave_up_is_in_the_journal_even_when_the_check_works(self):
        logged = []
        real_log = db.log
        db.log = lambda message, level="info", app_id=None: logged.append((level, message))
        try:
            self.assertIs(self.check(SERVER_CHANGES, code=0), True)
        finally:
            db.log = real_log
        self.assertEqual(self.status()["ok"], 1)
        self.assertEqual([level for level, _ in logged], ["warn", "warn"])
        self.assertIn("https://mac-ani.jkcoxson.com", logged[0][1])
        self.assertIn("using https://new.ani.sidestore.io instead", logged[1][1])

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
        self.assertEqual(normal, renew.RETRY_MINUTES * 60)
        self.assertEqual(transient, min(normal, 15 * 60))
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

        async def notify(message, priority="default"):
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
        self.device = db.execute("INSERT INTO devices (name, pair_identifier, udid, created_at) "
                                 "VALUES ('Salon', 'pid-reconnect', 'UDID-RECONNECT', 0)")
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
        self.device = db.execute("INSERT INTO devices (name, pair_identifier, udid, created_at) "
                                 "VALUES ('Salon', 'pid-refused', 'UDID-REFUSED', 0)")
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
        self.assertAlmostEqual(usual, renew.RETRY_MINUTES * 60, delta=30)
        self.assertLess(usual, hours)


class AnisetteDiagnostic(AnisetteCase):
    """Settings › Diagnostics : chaque serveur, ce qu'il annonce, joignable ou non. Jamais « inutilisable
    parce que pas Windows » : n'importe quel serveur anisette v3 qui répond est utilisable."""

    MAC = {"client_info": "<MacBookPro13,3> <Mac OS X;10.12.6;16G29> <com.apple.AuthKit/1 (com.apple.akd/1.0)>",
           "user_agent": "akd/1.0 CFNetwork/811.5.4 Darwin/16.7.0"}
    WINDOWS = {"client_info": "<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>",
               "user_agent": "akd/1.0 CFNetwork/808.1.4"}
    ANDROID = {"client_info": "<Pixel 7> <Android;13;TQ3A.230901.001> <com.apple.AuthKit/1 (com.apple.akd/1.0)>",
               "user_agent": "akd/1.0 CFNetwork/978.0.7"}

    def check(self, answers):
        saved = diag._client_info
        diag._client_info = lambda url: answers.get(url, (None, "injoignable"))
        try:
            return asyncio.run(diag._anisette())
        finally:
            diag._client_info = saved

    def everyone(self, answer):
        return {url: (answer, "") for url in DEFAULTS}

    def test_every_server_answers(self):
        r = self.check(self.everyone(self.MAC))
        self.assertEqual(r["status"], "ok")
        servers = r["value"]["servers"]
        self.assertEqual([s["url"] for s in servers], DEFAULTS)
        self.assertTrue(all(s["reachable"] for s in servers))
        self.assertTrue(all(s["client_info"] == self.MAC["client_info"] for s in servers))
        self.assertTrue(all(s["user_agent"] == self.MAC["user_agent"] for s in servers))
        for url in DEFAULTS:
            self.assertIn(url, r["detail"])                       # chaque serveur est nommé dans le texte
        self.assertIn("MacBookPro13,3", r["detail"])             # ... avec ce qu'il annonce
        self.assertIsNone(r["value"]["override"])
        self.assertIsNone(r["value"]["last_good"])

    def test_the_first_server_down_is_a_warning_and_the_next_is_named(self):
        answers = self.everyone(self.MAC)
        answers[DEFAULTS[0]] = (None, "HTTP 502")
        r = self.check(answers)
        self.assertEqual(r["status"], "warn")
        self.assertIn(DEFAULTS[0], r["detail"])
        self.assertIn("HTTP 502", r["detail"])
        self.assertIn(DEFAULTS[1], r["detail"])
        first = r["value"]["servers"][0]
        self.assertEqual((first["reachable"], first["error"]), (False, "HTTP 502"))
        self.assertTrue(r["value"]["servers"][1]["reachable"])

    def test_nothing_answers(self):
        r = self.check({})
        self.assertEqual(r["status"], "error")
        for url in DEFAULTS:
            self.assertIn(url, r["detail"])
        self.assertFalse(any(s["reachable"] for s in r["value"]["servers"]))

    def test_a_server_of_the_user_comes_first_and_the_defaults_back_it_up(self):
        settings.raw_set("anisette_url", "https://anisette.example.org")
        r = self.check({"https://anisette.example.org": (self.MAC, ""), DEFAULTS[0]: (self.MAC, "")})
        self.assertEqual(r["status"], "ok")
        self.assertEqual([s["url"] for s in r["value"]["servers"]], ["https://anisette.example.org"] + DEFAULTS)
        # its own server down: the defaults take over, which is a warning, not an error
        r = self.check({DEFAULTS[0]: (self.MAC, "")})
        self.assertEqual(r["status"], "warn")
        self.assertIn("https://anisette.example.org", r["detail"])
        self.assertIn(DEFAULTS[0], r["detail"])

    def test_with_the_fallback_off_the_server_of_the_user_is_alone(self):
        settings.raw_set("anisette_url", "https://anisette.example.org")
        settings.raw_set("anisette_fallback", "off")
        r = self.check({DEFAULTS[0]: (self.MAC, "")})
        self.assertEqual(r["status"], "error")
        self.assertEqual([s["url"] for s in r["value"]["servers"]], ["https://anisette.example.org"])
        for url in DEFAULTS:
            self.assertNotIn(url, r["detail"])

    def test_whatever_a_server_announces_it_is_not_judged(self):
        # Windows and Android are announced by servers of the earlier months, something unseen by a new one
        quantum = {"client_info": "<Quantum9000> <QuantumOS;42.0> <com.apple.AuthKit/1 (com.apple.akd/9.9)>",
                   "user_agent": "akd/9.9 Whatever/1.0"}
        for announced in (self.WINDOWS, self.ANDROID, quantum, self.MAC):
            settings.raw_set("anisette_url", "https://anisette.example.org")
            settings.raw_set("anisette_fallback", "off")
            r = self.check({"https://anisette.example.org": (announced, "")})
            self.assertEqual(r["status"], "ok", announced)
            self.assertIn(announced["client_info"], r["detail"])
            self.assertEqual(r["value"]["servers"][0]["client_info"], announced["client_info"])
            for banned in ("n'utilisera pas", "inutilisable", "n'accepte que", "non Windows", "pas Windows"):
                self.assertNotIn(banned, r["detail"], banned)

    def test_an_odd_answer_never_raises(self):
        self.assertFalse(hasattr(diag, "_windows"))              # plus de jugement sur la plateforme
        import io
        import urllib.request

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        saved = urllib.request.urlopen
        for body in (b'{"client_info": 42, "user_agent": "x"}', b'{"client_info": ["Windows"]}', b"[]", b"not json",
                     b'{"client_info": "  "}', b'{"client_info": null}', b"{}"):
            urllib.request.urlopen = lambda *a, _body=body, **k: Answer(_body)
            try:
                data, error = diag._client_info("https://anisette.example.org")
            finally:
                urllib.request.urlopen = saved
            self.assertIsNone(data, body)
            self.assertTrue(error, body)
            # ... and what the check makes of it is a server that does not answer, not an exception
            line = diag._server_line("https://anisette.example.org", data, error)
            self.assertFalse(line["reachable"], body)
        # a user agent that is not text does not break the line either
        line = diag._server_line("https://x", {"client_info": "<X>", "user_agent": 7}, "")
        self.assertEqual((line["reachable"], line["client_info"], line["user_agent"]), (True, "<X>", None))

    def test_the_override_is_visible(self):
        settings.raw_set("anisette_client_info", "<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>")
        r = self.check(self.everyone(self.MAC))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["value"]["override"]["client_info"], settings.anisette_client_info())
        self.assertIsNone(r["value"]["override"]["user_agent"])
        self.assertIn("forcée", r["detail"])

    def test_the_server_plumesign_remembers_is_shown(self):
        plume.STORE.mkdir(parents=True, exist_ok=True)
        folder = plume.STORE / plume.ANISETTE_DIR
        folder.mkdir(exist_ok=True)
        memory = folder / plume.LAST_SERVER_FILE
        try:
            memory.write_text(json.dumps({"server": DEFAULTS[3], "servers": DEFAULTS, "at": "2026-10-29T10:00:00+00:00"}),
                              encoding="utf-8")
            r = self.check(self.everyone(self.MAC))
            self.assertEqual(r["value"]["last_good"], {"server": DEFAULTS[3], "at": "2026-10-29T10:00:00+00:00"})
        finally:
            memory.unlink(missing_ok=True)

    def test_the_interface_lists_the_servers(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('c.id === "anisette"', js)
        self.assertIn(".servers", js)


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
        for key in ("c_apple_blocked", "c_rate_limited", "c_gsa_503", "c_session"):
            self.assertIn(key, keys)
        self.assertNotIn("c_anisette_unsupported", keys)       # plus de cause « serveur pas Windows »

    def test_what_plumesign_writes(self):
        for output, key in ((SRP_80045, "c_apple_blocked"), (GSA_503, "c_gsa_503"), (PORTAL_503, "c_gsa_503"),
                            (PROVISIONING_503, "c_gsa_503"), (GSA_429, "c_rate_limited"),
                            (PORTAL_429, "c_rate_limited"), (SMS_403, "c_sms_denied"),
                            (NEVER_SIGNED_IN, "c_never_signed_in"), (SYNC_22421, "c_anisette_refused"),
                            (NOT_PROVISIONED, "c_anisette_refused"), (PORTAL_1100, "c_session")):
            self.assertEqual(web_cause(output), key, output)

    def test_what_tvsign_says_about_it(self):
        for kind, key in (("apple_refused", "c_apple_blocked"), ("apple_rate_limited", "c_rate_limited"),
                          ("apple_503", "c_gsa_503"), ("sms_denied", "c_sms_denied"),
                          ("never_signed_in", "c_never_signed_in"),
                          ("anisette_refused", "c_anisette_refused"), ("anisette_reprovision", "c_anisette_refused")):
            self.assertEqual(web_cause(plume.explain(kind, "Error: x")), key, kind)

    def test_every_cause_of_the_rules_has_a_title_and_an_advice(self):
        i18n = (ROOT / "web" / "i18n.js").read_text(encoding="utf-8")
        for _, key in web_causes():
            for suffix in ("_t", "_a"):
                self.assertIn(f"  {key}{suffix}: [", i18n, key + suffix)


class DocumentationMatchesDefaults(unittest.TestCase):
    """Les textes de l'interface et le README disent les mêmes serveurs par défaut que le code, et plus
    nulle part qu'un serveur « doit fournir des données Windows »."""

    OLD_RULES = ("fournit des données Windows", "serves Windows", "provide Windows", "Windows data", "données Windows",
                 "Windows anisette", "anisette Windows", "Android servers", "serveurs Android", "n'accepte que")

    def test_readme_in_both_languages(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        english, french = readme.split("## Français")
        for text in (english, french):
            for url in settings.DEFAULT_ANISETTE_SERVERS:
                self.assertIn(url.removeprefix("https://"), text, url)
            self.assertNotIn("`ani.sidestore.app` by default", text)
            self.assertNotIn("`ani.sidestore.app` par défaut", text)
            self.assertNotIn("ani2.stikstore.app", text)             # le serveur de repli de 1.0.1
            for old in self.OLD_RULES:
                self.assertNotIn(old, text, old)
            # the escape hatches are documented
            for name in ("TVSIGN_ANISETTE_CLIENT_INFO", "TVSIGN_ANISETTE_USER_AGENT", "TVSIGN_ANISETTE_FALLBACK",
                         "PLUMESIGN_ANISETTE_FALLBACK_URL", "PLUMESIGN_ANISETTE_CLIENT_INFO"):
                self.assertIn(name, text, name)

    def test_settings_help_text(self):
        i18n = (ROOT / "web" / "i18n.js").read_text(encoding="utf-8")
        note = next(l for l in i18n.splitlines() if l.lstrip().startswith("anisette_note"))
        self.assertIn("mac-ani.jkcoxson.com", note)
        for old in self.OLD_RULES + ("ani2.stikstore.app",):
            self.assertNotIn(old, i18n, old)
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f'placeholder="{settings.DEFAULT_ANISETTE}"', html)
        self.assertNotIn("ani.sidestore.app", html)
        for old in self.OLD_RULES + ("ani2.stikstore.app",):
            self.assertNotIn(old, html, old)
        # the advanced fields say what they are for, in both languages
        for key in ("anisette_fallback", "anisette_client_info", "anisette_user_agent", "anisette_advanced"):
            self.assertRegex(i18n, rf"\n  {key}[a-z_]*: \[", key)

    def test_every_new_cause_is_translated(self):
        i18n = (ROOT / "web" / "i18n.js").read_text(encoding="utf-8")
        for key in ("c_apple_blocked", "c_gsa_503", "c_rate_limited", "c_sms_denied", "c_never_signed_in",
                    "c_anisette_refused"):
            for suffix in ("_t", "_a"):
                self.assertIn(f"  {key}{suffix}: [", i18n, key + suffix)
        self.assertNotIn("c_anisette_unsupported", i18n)
        self.assertIn("prompt_device_sms_denied", i18n)
        self.assertIn("acc_reauth_reason", i18n)

    def test_the_building_notes_and_the_patches_describe_the_servers(self):
        patches = (ROOT / "vendor" / "Impactor" / "PATCHES.md").read_text(encoding="utf-8")
        for url in settings.DEFAULT_ANISETTE_SERVERS:
            self.assertIn(url, patches, url)
        for name in ("PLUMESIGN_ANISETTE_CLIENT_INFO", "PLUMESIGN_ANISETTE_USER_AGENT", "PLUMESIGN_ANISETTE_FALLBACK_URL",
                     "PLUMESIGN_ANISETTE_ALLOW_ANY_PLATFORM", "last-server.json"):
            self.assertIn(name, patches, name)


if __name__ == "__main__":
    unittest.main()
