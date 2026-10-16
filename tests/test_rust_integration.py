"""Côté Python des corrections plumesign : révocation de certificats, équipes, copies de travail,
nom d'hôte d'appairage propre à chaque installation.

    python -m unittest discover -s tests
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

if "app.config" not in sys.modules:       # même environnement isolé que test_logic.py
    DATA = tempfile.mkdtemp(prefix="tvsign-test-")
    os.environ.update(TVSIGN_DATA=DATA, TVSIGN_AUTH="none", TVSIGN_PLUMESIGN=str(Path(DATA) / "absent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, devices, diag, main, plume, settings  # noqa: E402

HEADERS = {"X-Tvsign": "1"}


class FakeLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def patched(**names):
    """Remplace des attributs de app.plume le temps d'un test ; renvoie la fonction de remise en état."""
    saved = {k: getattr(plume, k) for k in names}
    for k, v in names.items():
        setattr(plume, k, v)

    def restore():
        for k, v in saved.items():
            setattr(plume, k, v)
    return restore


class CertRevokeSetting(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PLUMESIGN_REVOKE", None)
        db.init()
        settings.update({"cert_revoke": None})

    def tearDown(self):
        os.environ.pop("PLUMESIGN_REVOKE", None)      # avant update() : une variable la verrouille
        settings.update({"cert_revoke": None})

    def test_default_policy_given_to_plumesign_is_own(self):
        # La logique de révocation elle-même est testée côté Rust (plume_core, utils/certificate.rs).
        self.assertEqual(settings.cert_revoke(), "own")
        self.assertEqual(plume.env()["PLUMESIGN_REVOKE"], "own")

    def test_value_is_validated(self):
        with self.assertRaises(ValueError):
            settings.update({"cert_revoke": "sometimes"})
        for value in ("never", "own", "any"):
            settings.update({"cert_revoke": value})
            self.assertEqual(plume.env()["PLUMESIGN_REVOKE"], value)

    def test_environment_wins_and_is_locked(self):
        os.environ["PLUMESIGN_REVOKE"] = "any"
        self.assertEqual(plume.env()["PLUMESIGN_REVOKE"], "any")
        self.assertTrue(settings.describe()["cert_revoke"]["locked"])
        with self.assertRaises(ValueError):
            settings.update({"cert_revoke": "never"})

    def test_invalid_environment_is_ignored(self):
        os.environ["PLUMESIGN_REVOKE"] = "whatever"
        self.assertEqual(plume.env()["PLUMESIGN_REVOKE"], "own")

    def test_visible_in_the_settings_api(self):
        with TestClient(main.app) as client:
            data = client.get("/api/settings", headers=HEADERS).json()["settings"]
            self.assertEqual(data["cert_revoke"]["value"], "own")
            r = client.patch("/api/settings", headers=HEADERS, json={"cert_revoke": "never"})
            self.assertEqual(r.json()["settings"]["cert_revoke"]["value"], "never")


class CertificateExpiry(unittest.TestCase):
    def test_parsed_from_plumesign_output(self):
        out = "Logging...\nCERT 0AB12F 2027-03-14\nsigned"
        self.assertEqual(plume.cert_expiry(out), "2027-03-14")

    def test_last_line_wins_and_garbage_is_ignored(self):
        self.assertEqual(plume.cert_expiry("CERT AA 2026-01-01\nCERT BB 2026-02-02\n"), "2026-02-02")
        self.assertIsNone(plume.cert_expiry("CERT zz notadate"))
        self.assertIsNone(plume.cert_expiry(""))
        self.assertIsNone(plume.cert_expiry(None))

    def test_sign_remembers_the_expiry_per_account(self):
        db.init()
        out_dir = Path(tempfile.mkdtemp(prefix="tvsign-sign-"))

        async def fake_run(*args, **kwargs):
            Path(args[6]).write_bytes(b"signed")        # -o <tmp>
            return SimpleNamespace(ok=True, output="CERT 0AB 2027-05-06\n", tail=lambda n=1: "")

        restore = patched(_locked=lambda *a, **k: FakeLock(), _as=lambda e: FakeLock(), run=fake_run)
        try:
            revoked = asyncio.run(plume.sign("sign@example.com", out_dir / "in.ipa", out_dir / "out.ipa"))
        finally:
            restore()
        self.assertFalse(revoked)
        self.assertEqual((out_dir / "out.ipa").read_bytes(), b"signed")
        self.assertEqual(settings.raw_get("cert_expires:sign@example.com"), "2027-05-06")

    def _sign_with_output(self, output: str) -> bool:
        db.init()
        out_dir = Path(tempfile.mkdtemp(prefix="tvsign-sign-"))

        async def fake_run(*args, **kwargs):
            Path(args[6]).write_bytes(b"x")
            return SimpleNamespace(ok=True, output=output, tail=lambda n=1: "")

        restore = patched(_locked=lambda *a, **k: FakeLock(), _as=lambda e: FakeLock(), run=fake_run)
        try:
            return asyncio.run(plume.sign("sign@example.com", out_dir / "in.ipa", out_dir / "o.ipa"))
        finally:
            restore()

    def test_sign_reports_a_foreign_certificate_revoked(self):
        self.assertTrue(self._sign_with_output(
            "WARN Revoked certificate (FOREIGN) with serial number 12 [Xcode]\n"))

    def test_sign_ignores_dead_and_own_certificate_removals(self):
        # Seule la révocation d'un certificat étranger casse d'autres apps : pas de fausse alerte.
        self.assertFalse(self._sign_with_output(
            "WARN Removed dead certificate with serial number 12 [x]\n"
            "WARN Revoked own certificate with serial number 34 [y]\n"))

    def test_logout_and_team_change_forget_the_certificate_date(self):
        db.init()
        email = "forget@example.com"

        async def fake_run(*args, **kwargs):
            return SimpleNamespace(ok=True, output="", tail=lambda n=1: "")

        restore = patched(_locked=lambda *a, **k: FakeLock(), _as=lambda e: FakeLock(), run=fake_run)
        try:
            settings.raw_set("cert_expires:" + email, "2027-01-01")
            asyncio.run(plume.set_team(email, "ABCDE12345"))
            self.assertIsNone(settings.raw_get("cert_expires:" + email))
            settings.raw_set("cert_expires:" + email, "2027-01-01")
            asyncio.run(plume.logout(email))
            self.assertIsNone(settings.raw_get("cert_expires:" + email))
        finally:
            restore()


class Teams(unittest.TestCase):
    LINES = (
        '{"team_id":"ABCDE12345","name":"Personal Team","type":"Individual","status":"active","selected":true}\n'
        '[INFO] noise\n'
        '{"team_id":"ZZZZZ99999","name":"Company","type":"Company","status":"active","selected":false}\n'
        '{"team_id":"bad","name":"Broken"}\n'
        '{not json\n'
    )

    def test_parse_keeps_valid_teams_only(self):
        teams = plume.parse_teams(self.LINES)
        self.assertEqual([t["team_id"] for t in teams], ["ABCDE12345", "ZZZZZ99999"])
        self.assertTrue(teams[0]["selected"])
        self.assertFalse(teams[1]["selected"])

    def test_listing_runs_account_teams(self):
        seen = []

        async def fake_run(*args, **kwargs):
            seen.append(args[1:])
            return SimpleNamespace(ok=True, output=self.LINES)

        restore = patched(_locked=lambda *a, **k: FakeLock(), _as=lambda e: FakeLock(), run=fake_run)
        try:
            teams = asyncio.run(plume.teams("a@b.c"))
        finally:
            restore()
        self.assertEqual(seen, [("account", "teams", "--json")])
        self.assertEqual(len(teams), 2)

    def test_listing_failure_is_classified_from_the_log_too(self):
        # La session expirée s'écrit sur stderr : sans fusion avec stdout, elle serait une erreur générique.
        async def fake_run(*args, **kwargs):
            self.assertFalse(kwargs.get("split"))
            return SimpleNamespace(ok=False, code=1, tail=lambda n=1: "x",
                                   output="ERROR Developer API error 1100: session expired\n")

        restore = patched(_locked=lambda *a, **k: FakeLock(), _as=lambda e: FakeLock(), run=fake_run)
        try:
            db.init()
            with self.assertRaises(plume.SessionExpired):
                asyncio.run(plume.teams("a@b.c"))
        finally:
            restore()

    def test_set_team_rejects_a_malformed_id_before_running_anything(self):
        calls = []

        async def fake_run(*args, **kwargs):
            calls.append(args)
            return SimpleNamespace(ok=True, output="")

        restore = patched(_locked=lambda *a, **k: FakeLock(), _as=lambda e: FakeLock(), run=fake_run)
        try:
            with self.assertRaises(ValueError):
                asyncio.run(plume.set_team("a@b.c", "x; rm -rf"))
            self.assertEqual(calls, [])
            db.init()
            asyncio.run(plume.set_team("a@b.c", "ABCDE12345"))
        finally:
            restore()
        self.assertEqual(calls[0][1:], ("account", "set-team", "ABCDE12345"))

    def test_api_routes(self):
        async def fake_teams(email):
            return plume.parse_teams(self.LINES)

        async def fake_set(email, team_id):
            if team_id == "NOPE000000":
                raise plume.UnknownAccount("compte introuvable")
            self.chosen = (email, team_id)

        restore = patched(teams=fake_teams, set_team=fake_set)
        try:
            with TestClient(main.app) as client:
                r = client.get("/api/accounts/a@b.c/teams", headers=HEADERS)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(len(r.json()["teams"]), 2)
                r = client.post("/api/accounts/a@b.c/team", headers=HEADERS, json={"team_id": " zzzzz99999 "})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(self.chosen, ("a@b.c", "ZZZZZ99999"))
                r = client.post("/api/accounts/a@b.c/team", headers=HEADERS, json={"team_id": "NOPE000000"})
                self.assertEqual((r.status_code, r.json()["detail"]), (404, "unknown_account"))
        finally:
            restore()

    def test_listing_says_when_the_environment_forces_the_team(self):
        async def fake_teams(email):
            return plume.parse_teams(self.LINES)

        restore = patched(teams=fake_teams)
        saved = os.environ.pop("PLUMESIGN_TEAM_ID", None)
        try:
            with TestClient(main.app) as client:
                self.assertIsNone(client.get("/api/accounts/a@b.c/teams", headers=HEADERS).json()["env_team"])
                os.environ["PLUMESIGN_TEAM_ID"] = "  ZZZZZ99999 "
                self.assertEqual(plume.env_team(), "ZZZZZ99999")
                self.assertEqual(client.get("/api/accounts/a@b.c/teams", headers=HEADERS).json()["env_team"],
                                 "ZZZZZ99999")
                os.environ["PLUMESIGN_TEAM_ID"] = "   "
                self.assertIsNone(plume.env_team())
        finally:
            os.environ.pop("PLUMESIGN_TEAM_ID", None)
            if saved is not None:
                os.environ["PLUMESIGN_TEAM_ID"] = saved
            restore()

    def test_api_reports_a_failure_as_bad_gateway(self):
        async def failing(email):
            raise RuntimeError("liste des équipes : TEAM_NOT_FOUND")

        restore = patched(teams=failing)
        try:
            with TestClient(main.app) as client:
                r = client.get("/api/accounts/a@b.c/teams", headers=HEADERS)
                self.assertEqual(r.status_code, 502)
                self.assertIn("TEAM_NOT_FOUND", r.json()["detail"])
        finally:
            restore()


class StageFolders(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tvsign-stage-"))
        self.saved = plume.STAGE_DIR
        plume.STAGE_DIR = self.root

    def tearDown(self):
        plume.STAGE_DIR = self.saved

    def test_env_points_plumesign_temp_files_to_our_folder(self):
        env = plume._tmp_env()
        key = "TEMP" if os.name == "nt" else "TMPDIR"
        self.assertEqual(env[key], str(self.root))

    def test_only_old_stage_folders_are_swept(self):
        old, fresh, other = self.root / "plume_stage_OLD", self.root / "plume_stage_NEW", self.root / "keep"
        for d in (old, fresh, other):
            (d / "Payload").mkdir(parents=True)
        past = time.time() - 7200
        os.utime(old, (past, past))
        os.utime(other, (past, past))
        self.assertEqual(plume.sweep_stage(3600), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(other.exists())


class PairHost(unittest.TestCase):
    def setUp(self):
        self.saved = config.DATA_DIR
        config.DATA_DIR = Path(tempfile.mkdtemp(prefix="tvsign-host-"))
        os.environ.pop("TVSIGN_PAIR_HOST", None)

    def tearDown(self):
        config.DATA_DIR = self.saved
        os.environ.pop("TVSIGN_PAIR_HOST", None)

    def test_each_install_gets_its_own_stable_name(self):
        first = config._pair_host()
        self.assertRegex(first, r"^tvsign-[0-9a-f]{8}$")
        self.assertEqual(config._pair_host(), first)          # mémorisé
        config.DATA_DIR = Path(tempfile.mkdtemp(prefix="tvsign-host-"))
        self.assertNotEqual(config._pair_host(), first)       # une autre installation

    def test_a_damaged_file_is_replaced(self):
        (config.DATA_DIR / "pair_host").write_text("junk", encoding="utf-8")
        self.assertRegex(config._pair_host(), r"^tvsign-[0-9a-f]{8}$")

    def test_environment_forces_a_name(self):
        os.environ["TVSIGN_PAIR_HOST"] = "salon"
        self.assertEqual(config._pair_host(), "salon")
        self.assertFalse((config.DATA_DIR / "pair_host").exists())

    def test_read_only_folder_keeps_the_historic_name(self):
        config.DATA_DIR = Path(tempfile.mkdtemp(prefix="tvsign-host-")) / "missing"
        self.assertEqual(config._pair_host(), "tvsign")


class AccountList(unittest.TestCase):
    def test_accounts_expose_the_certificate_expiry(self):
        db.init()
        settings.raw_set("cert_expires:one@example.com", "2027-01-02")
        restore = patched(_store=lambda: {"accounts": {"one@example.com": {"first_name": "A", "team_id": "ABCDE12345"},
                                                       "two@example.com": {"first_name": "B", "team_id": ""}},
                                          "selected_account": "one@example.com"})
        try:
            accounts = {a["email"]: a for a in plume.accounts()}
        finally:
            restore()
        self.assertEqual(accounts["one@example.com"]["cert_expires"], "2027-01-02")
        self.assertIsNone(accounts["two@example.com"]["cert_expires"])


class AnisetteClientInfoCheck(unittest.TestCase):
    """What the diagnostic accepts as an anisette-v3 server (the Windows decision is in test_hotfix)."""

    def fetch(self, body: bytes):
        import io
        import urllib.request

        seen = []

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            seen.append(req.full_url)
            return Answer(body)

        saved = urllib.request.urlopen
        urllib.request.urlopen = fake
        try:
            return diag._client_info("http://anisette:6969/"), seen
        finally:
            urllib.request.urlopen = saved

    def test_a_real_server_answers_client_info(self):
        (data, error), seen = self.fetch(b'{"client_info": "<x>", "user_agent": "akd"}')
        self.assertEqual(data["user_agent"], "akd")
        self.assertEqual(seen, ["http://anisette:6969/v3/client_info"])      # no double slash

    def test_any_other_web_server_is_not_an_anisette_server(self):
        for body in (b'{"hello": "world"}', b'{"client_info": "<x>"}', b'{"client_info": "<x>", "user_agent": ""}'):
            (data, error), _ = self.fetch(body)
            self.assertIsNone(data, body)
            self.assertIn("anisette", error)


class KilledPlumesign(unittest.TestCase):
    class Res:
        def __init__(self, code, text=""):
            self.code, self.text = code, text

        def lines(self):
            return self.text.splitlines()

        def tail(self, n=1):
            return " | ".join(self.lines()[-n:])

    def test_silent_kill_is_explained(self):
        why = devices._error_line(self.Res(-9))
        self.assertIn("mémoire", why)
        self.assertIn("-9", why)

    def test_ordinary_failures_are_untouched(self):
        self.assertEqual(devices._error_line(self.Res(1, "ERROR FAILED boom")), "boom")
        self.assertEqual(devices._error_line(self.Res(1, "some output")), "some output")
        self.assertEqual(devices._error_line(self.Res(1, "")), "")
        self.assertEqual(devices._error_line(self.Res(-1, "")), "")       # délai : message propre ailleurs


if __name__ == "__main__":
    unittest.main()
