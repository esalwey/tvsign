"""iPhone / iPad : familles d'appareils, contrôle des IPA, choix des releases, présence, migrations.

    python -m unittest discover -s tests
"""
import asyncio
import io
import os
import plistlib
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

DATA = os.environ.get("TVSIGN_DATA") or tempfile.mkdtemp(prefix="tvsign-test-")
os.environ.setdefault("TVSIGN_DATA", DATA)
os.environ.setdefault("TVSIGN_AUTH", "none")
os.environ.setdefault("TVSIGN_PLUMESIGN", str(Path(DATA) / "absent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, devices, families, ipa, main, plume, renew, settings  # noqa: E402
from app.proc import Result  # noqa: E402

HEADERS = {"X-Tvsign": "1"}


def make_ipa(platform="iphoneos", bundle="com.example.phone", ui_family=None, min_os=None, appex=0,
             binary=b"", executable="Phone") -> bytes:
    info = {"CFBundleName": "Phone", "CFBundleIdentifier": bundle, "CFBundleShortVersionString": "1.0",
            "DTPlatformName": platform, "CFBundleExecutable": executable}
    if ui_family is not None:
        info["UIDeviceFamily"] = ui_family
    if min_os:
        info["MinimumOSVersion"] = min_os
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Payload/Phone.app/Info.plist", plistlib.dumps(info))
        z.writestr(f"Payload/Phone.app/{executable}", binary)
        for i in range(appex):
            z.writestr(f"Payload/Phone.app/PlugIns/Ext{i}.appex/Info.plist", plistlib.dumps({}))
            z.writestr(f"Payload/Phone.app/PlugIns/Ext{i}.appex/Ext{i}", b"x")
    return buf.getvalue()


def add_device(name="iPhone", family="ios", model="iPhone15,2", os_version="17.5", udid=None, **extra) -> int:
    udid = udid or f"00008110-{int(time.time_ns()) % 10 ** 16:016X}"
    cols = dict(name=name, pair_identifier=udid, pairing_udid=udid, udid=udid, model=model, os_version=os_version,
                family=family, created_at=time.time(), **extra)
    return db.execute(f"INSERT INTO devices ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      *cols.values())


class Families(unittest.TestCase):
    def test_family_of(self):
        f = families.family_of
        self.assertEqual(f("AppleTV11,1"), "tvos")
        self.assertEqual(f("iPhone15,2"), "ios")
        self.assertEqual(f("iPod9,1"), "ios")
        self.assertEqual(f("iPad13,4"), "ipados")
        self.assertEqual(f("RealityDevice14,1"), "visionos")
        self.assertEqual(f(None, "iPad"), "ipados")              # la classe prime sur le modèle
        self.assertIsNone(f("Watch6,1"))
        self.assertIsNone(f(None))

    def info(self, platform, **over):
        return {"platform": platform, "ui_family": [], "min_os": None, **over}

    def code(self, info, family="tvos", os_version=None, model=None):
        try:
            families.check_platform(info, family, os_version, model)
        except families.PlatformMismatch as e:
            return e.code
        return None

    def test_platform_matrix(self):
        ok, bad = None, object()
        cases = {
            ("appletvos", "tvos"): ok,
            ("appletvos", "ios"): "not_ios:appletvos",
            ("appletvos", "ipados"): "not_ios:appletvos",
            ("iphoneos", "tvos"): "not_tvos:iphoneos",
            ("iphoneos", "ios"): ok,
            ("iphoneos", "ipados"): ok,
            ("xros", "tvos"): "not_tvos:xros",
            ("xros", "ios"): "not_ios:xros",
            ("xros", "visionos"): "unsupported_device",     # pas encore pris en charge
            ("iphoneos", "visionos"): "unsupported_device",
            (None, "tvos"): "not_tvos:unknown",
            (None, "ios"): "not_ios:unknown",
        }
        for (platform, family), expected in cases.items():
            self.assertEqual(self.code(self.info(platform), family), expected, (platform, family))
        self.assertNotEqual(bad, ok)

    def test_ipad_only_app_refused_on_iphone(self):
        info = self.info("iphoneos", ui_family=[2])
        self.assertEqual(self.code(info, "ios", model="iPhone15,2"), "ipad_only")
        self.assertIsNone(self.code(info, "ipados", model="iPad13,4"))
        self.assertIsNone(self.code(self.info("iphoneos", ui_family=[1, 2]), "ios"))
        self.assertIsNone(self.code(self.info("iphoneos", ui_family=[]), "ios"))

    def test_minimum_os_against_device(self):
        info = self.info("iphoneos", min_os="17.4")
        self.assertEqual(self.code(info, "ios", "17.3.1"), "os_too_old:17.4")
        self.assertIsNone(self.code(info, "ios", "17.4"))
        self.assertIsNone(self.code(info, "ios", "18.0"))
        self.assertIsNone(self.code(info, "ios", None))          # version inconnue : jamais bloquant
        self.assertEqual(self.code(self.info("appletvos", min_os="26.0"), "tvos", "18.4"), "os_too_old:26.0")
        self.assertIsNone(self.code(self.info("appletvos", min_os="17.0"), "tvos", "26.0"))

    def test_version_tuple(self):
        self.assertEqual(families.version_tuple("17.4.1"), (17, 4, 1))
        self.assertEqual(families.version_tuple("26"), (26,))
        self.assertLess(families.version_tuple("9.3"), families.version_tuple("17.0"))
        self.assertEqual(families.version_tuple(None), ())

    def test_udid_from_filename(self):
        u = "00008110-001A2B3C00000000"
        self.assertEqual(families.udid_from_name(f"{u}.plist"), u)
        self.assertEqual(families.udid_from_name(f"plume_{u}.plist"), u)
        self.assertEqual(families.udid_from_name("a" * 40 + ".plist"), "a" * 40)
        self.assertIsNone(families.udid_from_name("pairing.plist"))
        self.assertIsNone(families.udid_from_name(None))

    def test_ui_family_parsing(self):
        self.assertEqual(families.parse_ui_family([2, 1]), [1, 2])
        self.assertEqual(families.parse_ui_family(3), [3])
        self.assertEqual(families.parse_ui_family(["1", "x"]), [1])
        self.assertEqual(families.parse_ui_family(None), [])


class IpaInspection(unittest.TestCase):
    def info(self, data):
        p = Path(tempfile.mkdtemp(dir=DATA)) / "a.ipa"
        p.write_bytes(data)
        return ipa.app_info(p)

    def test_ui_family_and_platform(self):
        i = self.info(make_ipa(ui_family=[1, 2], min_os="16.0"))
        self.assertEqual((i["platform"], i["ui_family"], i["min_os"]), ("iphoneos", [1, 2], "16.0"))

    def test_extensions_and_entitlements_are_reported(self):
        blob = b"\x00" * 3000 + b"<key>aps-environment</key>" + b"\x00" * 10 + b"com.apple.developer.associated-domains"
        i = self.info(make_ipa(appex=2, binary=blob))
        self.assertEqual(i["warnings"], ["appex:2", "associated-domains", "push"])
        self.assertEqual(renew.app_ids_used({"warnings": ",".join(i["warnings"])}), 3)

    def test_needle_split_across_chunks_is_found(self):
        needle = b"com.apple.developer.icloud-services"
        data = b"\x00" * ((1 << 20) - 10) + needle + b"\x00" * 100
        self.assertEqual(self.info(make_ipa(binary=data))["warnings"], ["icloud"])

    def test_clean_app_has_no_warning(self):
        self.assertEqual(self.info(make_ipa(binary=b"nothing here"))["warnings"], [])

    def test_tvos_apps_are_not_scanned(self):
        i = self.info(make_ipa("appletvos", binary=b"aps-environment"))
        self.assertEqual(i["warnings"], [])
        self.assertEqual(renew.app_ids_used({"warnings": None}), 1)


class AssetChoice(unittest.TestCase):
    def test_name_rank_tvos(self):
        r = lambda n: ipa._name_rank(n, "tvos")           # noqa: E731
        self.assertEqual(r("NuvioTV.ipa"), 0)
        self.assertEqual(r("App-tvOS.ipa"), 0)
        self.assertEqual(r("app_apple-tv.ipa"), 0)
        self.assertEqual(r("App.ipa"), 1)
        self.assertEqual(r("settings.ipa"), 1)             # « tv » au milieu d'un mot ne compte pas
        self.assertEqual(r("App-iOS.ipa"), 2)
        self.assertEqual(r("App-iphone.ipa"), 2)
        self.assertEqual(r("Outbound.ipa"), 1)             # « ios » dans un mot non plus

    def test_name_rank_ios(self):
        r = lambda n: ipa._name_rank(n, "ios")            # noqa: E731
        self.assertEqual(r("App-iOS.ipa"), 0)
        self.assertEqual(r("App-iPad.ipa"), 0)
        self.assertEqual(r("App.ipa"), 1)
        self.assertEqual(r("NuvioTV.ipa"), 2)

    def release(self, tag, published, names, prerelease=False):
        return {"tag_name": tag, "published_at": published, "prerelease": prerelease, "draft": False,
                "assets": [{"name": n, "state": "uploaded", "id": i, "updated_at": published, "size": 1,
                            "browser_download_url": f"https://x/{tag}/{n}", "digest": f"sha256:{'a' * 63}{i}"}
                           for i, n in enumerate(names)]}

    def latest(self, releases, *a, **k):
        saved = ipa._get_json
        ipa._get_json = lambda url: releases
        try:
            return ipa.latest_github("o/r", *a, **k)
        finally:
            ipa._get_json = saved

    def test_family_picks_the_right_ipa(self):
        rel = [self.release("v1", "2026-01-02T00:00:00Z", ["App-iOS.ipa", "App-tvOS.ipa"])]
        tv = self.latest(rel, [], False)
        self.assertEqual(tv["asset"], "App-tvOS.ipa")
        phone = self.latest(rel, [], False, None, "ios")
        self.assertEqual(phone["asset"], "App-iOS.ipa")
        # l'autre IPA reste en recours
        self.assertEqual([c["asset"] for c in tv["candidates"]], ["App-tvOS.ipa", "App-iOS.ipa"])

    def test_newest_release_without_ipa_is_flagged_not_hidden(self):
        rel = [self.release("v2", "2026-02-01T00:00:00Z", []),
               self.release("v1", "2026-01-01T00:00:00Z", ["App.ipa"])]
        found = self.latest(rel, [], False)
        self.assertEqual((found["tag"], found["newer_without_ipa"]), ("v1", "v2"))
        self.assertEqual(found["published_at"], "2026-01-01T00:00:00Z")

    def test_no_release_at_all(self):
        self.assertIsNone(self.latest([], [], False))


class NoDowngrade(unittest.TestCase):
    """Une release plus récente sans IPA (publication en cours) ne ramène pas à une version plus ancienne."""

    def setUp(self):
        db.init()
        self.saved = renew._latest, ipa.download
        self.calls = []

        def fake_download(url, dest, sha256=None, expect_bundle=None, **kw):
            self.calls.append((url, kw))
            dest.write_bytes(make_ipa("appletvos", "com.example.tvdown"))
            return {"version": "1", "bundle_id": "com.example.tvdown", "sha256": "z", "platform": "appletvos",
                    "ui_family": [], "warnings": []}
        ipa.download = fake_download

    def tearDown(self):
        renew._latest, ipa.download = self.saved

    def app(self, **over):
        cols = dict(name="N", source_type="github", source="o/r", source_tag="v2", source_key="k2",
                    source_published_at="2026-02-01T00:00:00Z", bundle_id="com.example.tvdown", created_at=0,
                    enabled=1, status="ok")
        cols.update(over)
        app_id = db.execute(f"INSERT INTO apps ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            *cols.values())
        (renew.app_dir(app_id) / "original.ipa").write_bytes(make_ipa("appletvos"))
        return db.row("SELECT * FROM apps WHERE id = ?", app_id)

    def found(self, tag, published, key="k"):
        return {"tag": tag, "url": "https://x/" + tag, "sha256": None, "key": key, "published_at": published}

    def test_older_release_is_not_a_new_release(self):
        renew._latest = lambda app: self.found("v1", "2026-01-01T00:00:00Z", "k1")
        self.assertFalse(asyncio.run(renew.new_release(self.app())))

    def test_newer_release_is_one(self):
        renew._latest = lambda app: self.found("v3", "2026-03-01T00:00:00Z", "k3")
        self.assertTrue(asyncio.run(renew.new_release(self.app())))

    def test_legacy_row_without_date_keeps_old_behaviour(self):
        renew._latest = lambda app: self.found("v1", "2026-01-01T00:00:00Z", "k1")
        self.assertTrue(asyncio.run(renew.new_release(self.app(source_published_at=None))))

    def test_refresh_does_not_download_the_older_release(self):
        renew._latest = lambda app: self.found("v1", "2026-01-01T00:00:00Z", "k1")
        a = self.app()
        self.assertFalse(asyncio.run(renew.refresh_source(a)))
        self.assertEqual(self.calls, [])
        self.assertEqual(db.row("SELECT source_tag FROM apps WHERE id = ?", a["id"])["source_tag"], "v2")

    def test_refresh_records_the_publication_date(self):
        renew._latest = lambda app: self.found("v3", "2026-03-01T00:00:00Z", "k3")
        a = self.app()
        self.assertTrue(asyncio.run(renew.refresh_source(a)))
        row = db.row("SELECT * FROM apps WHERE id = ?", a["id"])
        self.assertEqual((row["source_tag"], row["source_published_at"], row["platform"]),
                         ("v3", "2026-03-01T00:00:00Z", "appletvos"))
        self.assertEqual(self.calls[0][1], {})              # Apple TV : appel d'origine, sans argument en plus

    def test_next_asset_is_tried_when_the_first_is_for_another_platform(self):
        calls = []

        def download(url, dest, sha256=None, expect_bundle=None, **kw):
            calls.append((url, kw))
            if "ios" in url:
                raise ipa.PlatformRejected("pas tvOS", "not_tvos:iphoneos")
            dest.write_bytes(make_ipa("appletvos", "com.example.tvdown"))
            return {"version": "1", "bundle_id": "com.example.tvdown", "sha256": "z", "platform": "appletvos",
                    "ui_family": [], "warnings": []}
        ipa.download = download
        renew._latest = lambda app: {**self.found("v3", "2026-03-01T00:00:00Z", "k-ios"), "url": "https://x/ios.ipa",
                                     "candidates": [{"url": "https://x/ios.ipa", "sha256": None, "key": "k-ios"},
                                                    {"url": "https://x/tv.ipa", "sha256": None, "key": "k-tv"}]}
        a = self.app()
        self.assertTrue(asyncio.run(renew.refresh_source(a)))
        self.assertEqual([c[0] for c in calls], ["https://x/ios.ipa", "https://x/tv.ipa"])
        row = db.row("SELECT * FROM apps WHERE id = ?", a["id"])
        self.assertEqual((row["source_key"], row["bad_tag"]), ("k-tv", None))

    def test_all_assets_wrong_marks_the_tag_bad(self):
        def download(url, dest, sha256=None, expect_bundle=None, **kw):
            raise ipa.PlatformRejected("pas tvOS", "not_tvos:iphoneos")
        ipa.download = download
        renew._latest = lambda app: {**self.found("v3", "2026-03-01T00:00:00Z", "k3"),
                                     "candidates": [{"url": "https://x/a.ipa", "sha256": None, "key": "k3"}]}
        a = self.app()
        self.assertFalse(asyncio.run(renew.refresh_source(a)))
        self.assertEqual(db.row("SELECT bad_tag FROM apps WHERE id = ?", a["id"])["bad_tag"], "v3")

    def test_phone_downloads_carry_the_device_family(self):
        dev = add_device(os_version="17.5", model="iPhone15,2")
        renew._latest = lambda app: self.found("v3", "2026-03-01T00:00:00Z", "k3")
        a = self.app(device_id=dev)
        asyncio.run(renew.refresh_source(a))
        self.assertEqual(self.calls[0][1], {"family": "ios", "os_version": "17.5", "model": "iPhone15,2"})

    def test_download_refuses_a_platform_that_does_not_fit_the_family(self):
        ipa.download = self.saved[1]

        class Resp:
            headers = {"Content-Length": "0"}

            def __init__(self, data):
                self.data, self.pos = data, 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                chunk, self.pos = self.data[self.pos:self.pos + n], self.pos + n
                return chunk
        saved_get = ipa._get
        ipa._get = lambda url, timeout=30, headers=None: Resp(make_ipa("iphoneos"))
        try:
            dest = Path(tempfile.mkdtemp(dir=DATA)) / "o.ipa"
            with self.assertRaises(ipa.PlatformRejected) as cm:
                ipa.download("https://x/a.ipa", dest)                       # Apple TV (défaut)
            self.assertEqual(cm.exception.code, "not_tvos:iphoneos")
            info = ipa.download("https://x/a.ipa", dest, family="ios")
            self.assertEqual(info["platform"], "iphoneos")
            self.assertTrue(dest.exists())
        finally:
            ipa._get = saved_get


class DueAndPresence(unittest.TestCase):
    def setUp(self):
        db.init()
        self.saved = renew.renew, plume.discover
        self.spawned = []

        async def fake_renew(app_id, trigger="manuel"):
            self.spawned.append((app_id, trigger))
        renew.renew = fake_renew
        renew._ABSENT.clear()
        renew.STOPPING = False                   # un arrêt de service précédent (lifespan) l'a peut-être posé
        db.execute("DELETE FROM apps")           # chaque essai part d'une base sans autre app due

    def tearDown(self):
        renew.renew, plume.discover = self.saved
        renew._ABSENT.clear()

    def app(self, device_id, **over):
        cols = dict(name="P", source_type="upload", device_id=device_id, enabled=1, status="ok",
                    expires_at=time.time() + 70 * 3600, created_at=0)
        cols.update(over)
        return db.execute(f"INSERT INTO apps ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                          *cols.values())

    def test_phones_renew_earlier_than_tvs(self):
        self.assertEqual(settings.renew_before_hours_for(None), 60)
        self.assertEqual(settings.renew_before_hours_for({"family": "tvos"}), 60)
        self.assertEqual(settings.renew_before_hours_for({"family": "ios"}), 96)
        self.assertEqual(settings.renew_before_hours_for({"family": "ipados", "renew_before_hours": 120}), 120)
        self.assertEqual(settings.renew_before_hours_for({"family": "tvos", "renew_before_hours": 100}), 100)

    def test_due_uses_the_device_window(self):
        now = time.time()
        tv, phone = add_device("TV", "tvos", "AppleTV11,1"), add_device("Phone")
        a_tv, a_phone = self.app(tv), self.app(phone)        # expirent dans 70 h
        row = lambda i: db.row("SELECT * FROM apps WHERE id = ?", i)   # noqa: E731
        self.assertFalse(renew.due(row(a_tv), now))           # 70 h > 60 h
        self.assertTrue(renew.due(row(a_phone), now))         # 70 h < 96 h

    def test_ignore_retry(self):
        now = time.time()
        dev = add_device()
        a = self.app(dev, status="error", next_attempt_at=now + 3000)
        row = db.row("SELECT * FROM apps WHERE id = ?", a)
        self.assertFalse(renew.due(row, now))
        self.assertTrue(renew.due(row, now, ignore_retry=True))

    def test_returning_phone_is_renewed_at_once(self):
        dev = add_device(udid="PRESENT-0001")
        a = self.app(dev, status="error", next_attempt_at=time.time() + 3000, last_attempt_at=time.time() - 3600)
        renew._ABSENT.add(a)

        async def discover(seconds=6):
            return [{"kind": "paired", "udid": "present-0001", "ip": "192.168.1.9", "port": 49152}]
        plume.discover = discover
        asyncio.run(renew.presence_check())
        self.assertEqual(self.spawned, [(a, "présence")])
        self.assertEqual(db.row("SELECT last_ip FROM devices WHERE id = ?", dev)["last_ip"], "192.168.1.9")

    def test_other_failures_keep_their_retry_delay(self):
        dev = add_device(udid="PRESENT-0002")
        self.app(dev, status="error", next_attempt_at=time.time() + 3000)     # pas dans _ABSENT

        async def discover(seconds=6):
            raise AssertionError("aucune recherche réseau : rien à faire")
        plume.discover = discover
        asyncio.run(renew.presence_check())
        self.assertEqual(self.spawned, [])

    def test_no_search_when_nothing_is_due(self):
        dev = add_device(udid="PRESENT-0003")
        self.app(dev, expires_at=time.time() + 150 * 3600)

        async def discover(seconds=6):
            raise AssertionError("pas de recherche si rien n'est dû")
        plume.discover = discover
        asyncio.run(renew.presence_check())
        self.assertEqual(self.spawned, [])

    def test_absent_phone_is_left_alone(self):
        dev = add_device(udid="PRESENT-0004")
        a = self.app(dev, status="error", next_attempt_at=time.time() + 3000)
        renew._ABSENT.add(a)

        async def discover(seconds=6):
            return [{"kind": "paired", "udid": "someone-else"}, {"kind": "pairable", "name": "TV"}]
        plume.discover = discover
        asyncio.run(renew.presence_check())
        self.assertEqual(self.spawned, [])

    def test_recent_attempt_is_not_repeated(self):
        dev = add_device(udid="PRESENT-0005")
        a = self.app(dev, status="error", next_attempt_at=time.time() + 3000, last_attempt_at=time.time() - 60)
        renew._ABSENT.add(a)

        async def discover(seconds=6):
            return [{"kind": "paired", "udid": "PRESENT-0005"}]
        plume.discover = discover
        asyncio.run(renew.presence_check())
        self.assertEqual(self.spawned, [])

    def test_tv_apps_are_not_watched(self):
        dev = add_device("TV2", "tvos", "AppleTV11,1", udid="PRESENT-0006")
        a = self.app(dev, status="error", next_attempt_at=time.time() + 3000)
        renew._ABSENT.add(a)

        async def discover(seconds=6):
            raise AssertionError("une Apple TV garde le rythme habituel")
        plume.discover = discover
        asyncio.run(renew.presence_check())

    def test_revocation_loop_is_counted_over_seven_days(self):
        now = time.time()
        email = "loop@example.com"
        self.assertEqual(renew.note_revocation(email, now - 8 * 86400), 1)
        self.assertEqual(renew.note_revocation(email, now), 1)          # la plus ancienne est oubliée
        self.assertEqual(renew.note_revocation(email, now + 1), 2)
        self.assertEqual(renew.note_revocation(email, now + 2), 3)


class DeviceMessages(unittest.TestCase):
    """Un téléphone absent n'est pas une TV en veille : autres mots, et « verrouillé » est reconnu."""

    def setUp(self):
        db.init()
        self.saved = devices.run

    def tearDown(self):
        devices.run = self.saved

    def call(self, family, code, output="", err=""):
        dev_id = add_device("Mon appareil", family, "iPhone15,2" if family != "tvos" else "AppleTV11,1")
        dev = db.row("SELECT * FROM devices WHERE id = ?", dev_id)

        async def fake(*a, **k):
            return Result(code, output, err)
        devices.run = fake
        try:
            asyncio.run(devices._wireless(dev, "check"))
        except Exception as e:  # noqa: BLE001
            return e
        return None

    def test_absent_phone(self):
        e = self.call("ios", 3, "ERROR NOT_FOUND no device")
        self.assertIsInstance(e, devices.Unreachable)
        self.assertIn("iPhone", str(e))
        self.assertIn("pas à la maison", str(e))

    def test_absent_tv_keeps_its_message(self):
        e = self.call("tvos", 3, "ERROR NOT_FOUND no device")
        self.assertIn("Apple TV « Mon appareil » introuvable sur le réseau (éteinte ou en veille ?)", str(e))

    def test_locked_since_boot(self):
        e = self.call("ios", 1, "ERROR FAILED lockdown: PasswordProtected")
        self.assertIsInstance(e, devices.DeviceLocked)
        self.assertIsInstance(e, devices.Unreachable)              # même traitement qu'une TV en veille
        self.assertIn("verrouillé", str(e))

    def test_locked_message_means_nothing_on_a_tv(self):
        e = self.call("tvos", 1, "ERROR FAILED PasswordProtected")
        self.assertNotIsInstance(e, devices.DeviceLocked)

    def test_lost_pairing_on_a_phone_points_to_usb_or_import(self):
        e = self.call("ios", 2, "ERROR PAIRING_LOST x")
        self.assertIsInstance(e, devices.PairingLost)
        self.assertIn("importe", str(e))

    def test_devmode_is_parsed_and_stored(self):
        dev_id = add_device("DevM", "ios")
        dev = db.row("SELECT * FROM devices WHERE id = ?", dev_id)

        async def fake(*a, **k):
            return Result(0, "CONNECTED x\nDEVMODE off\n")
        devices.run = fake
        self.assertIs(asyncio.run(devices.check_devmode(dev)), False)
        row = db.row("SELECT dev_mode, dev_mode_at FROM devices WHERE id = ?", dev_id)
        self.assertEqual(row["dev_mode"], 0)
        self.assertTrue(row["dev_mode_at"])

        async def fake_on(*a, **k):
            return Result(0, "DEVMODE on\n")
        devices.run = fake_on
        self.assertIs(asyncio.run(devices.check_devmode(dev)), True)
        self.assertEqual(db.row("SELECT dev_mode FROM devices WHERE id = ?", dev_id)["dev_mode"], 1)

    def test_old_plumesign_without_devmode_is_not_an_error(self):
        dev_id = add_device("DevOld", "ios")
        dev = db.row("SELECT * FROM devices WHERE id = ?", dev_id)

        async def fake(*a, **k):
            return Result(2, "", "error: unrecognized subcommand 'devmode'")
        devices.run = fake
        self.assertIsNone(asyncio.run(devices.check_devmode(dev)))

    def test_refresh_info_records_family_and_version(self):
        dev_id = add_device("Info", "ios", None, None)
        info = ('{"UniqueDeviceID":"U-1","ProductType":"iPad13,4","ProductVersion":"18.1",'
                '"DeviceClass":"iPad","DeviceFamily":"ipados","ip":"10.0.0.7","port":1234}')

        async def fake(*a, **k):
            return Result(0, info + "\n")
        devices.run = fake
        asyncio.run(devices.refresh_info(dev_id))
        row = db.row("SELECT * FROM devices WHERE id = ?", dev_id)
        self.assertEqual((row["family"], row["model"], row["os_version"]), ("ipados", "iPad13,4", "18.1"))


class ImportPairing(unittest.TestCase):
    def setUp(self):
        db.init()
        self.saved = devices.run
        self.calls = []

        async def fake(*args, **k):
            self.calls.append([str(a) for a in args])
            if "info" in args:
                return Result(1, "ERROR NOT_FOUND no device")
            return Result(0, "PAIRED - x\n")
        devices.run = fake

    def tearDown(self):
        devices.run = self.saved

    def run_import(self, **kw):
        args = dict(data=b"<plist/>", filename="pairing.plist", udid="00008110-AAAA0000BBBB0001", name="Mon iPhone",
                    model="iPhone15,2")
        args.update(kw)
        return asyncio.run(devices.import_pairing(**args))

    def test_import_creates_the_device(self):
        out = self.run_import()
        row = db.row("SELECT * FROM devices WHERE id = ?", out["device_id"])
        self.assertEqual((row["name"], row["family"], row["model"], row["pair_host"]),
                         ("Mon iPhone", "ios", "iPhone15,2", devices.CLI_HOST))
        self.assertEqual(row["pairing_udid"], "00008110-AAAA0000BBBB0001")
        self.assertIsNotNone(out["info_error"])                      # injoignable ici : signalé, appareil gardé
        self.assertTrue(any(c[1:3] == ["pair", "import"] for c in self.calls))

    def test_ipad_model_gives_ipados(self):
        out = self.run_import(udid="00008110-AAAA0000BBBB0002", model="iPad13,4", name=None)
        row = db.row("SELECT * FROM devices WHERE id = ?", out["device_id"])
        self.assertEqual((row["family"], row["name"]), ("ipados", "iPad"))

    def test_udid_from_the_file_name(self):
        out = self.run_import(udid=None, filename="plume_00008110-AAAA0000BBBB0003.plist")
        self.assertEqual(out["udid"], "00008110-AAAA0000BBBB0003")

    def test_missing_or_bad_input(self):
        for kw, code in (({"udid": None, "filename": "x.plist"}, "udid_required"),
                         ({"udid": "../../etc"}, "invalid_udid"),
                         ({"data": b""}, "invalid_pairing_file"),
                         ({"data": b"x" * 70000}, "invalid_pairing_file")):
            with self.assertRaises(ValueError) as cm:
                self.run_import(**kw)
            self.assertEqual(str(cm.exception), code)

    def test_unreadable_file_is_refused_by_plumesign(self):
        async def bad(*a, **k):
            return Result(1, "ERROR FAILED Not a valid pairing file (x): io on plist")
        devices.run = bad
        with self.assertRaises(ValueError) as cm:
            self.run_import(udid="00008110-AAAA0000BBBB0004")
        self.assertEqual(str(cm.exception), "invalid_pairing_file")
        self.assertIsNone(db.row("SELECT id FROM devices WHERE pairing_udid = '00008110-AAAA0000BBBB0004'"))

    def test_reimport_updates_the_same_device(self):
        a = self.run_import(udid="00008110-AAAA0000BBBB0005")
        b = self.run_import(udid="00008110-AAAA0000BBBB0005", name="Renommé")
        self.assertEqual(a["device_id"], b["device_id"])
        self.assertEqual(db.row("SELECT name FROM devices WHERE id = ?", a["device_id"])["name"], "Renommé")


class Migration(unittest.TestCase):
    """Une base de la 1.0.0 passe en place : rien n'est perdu, les nouvelles colonnes ont une valeur sûre."""

    OLD_SCHEMA = """
    CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT NOT NULL, pair_identifier TEXT NOT NULL UNIQUE,
        udid TEXT, model TEXT, os_version TEXT, last_ip TEXT, last_port INTEGER, last_seen REAL, created_at REAL NOT NULL,
        last_error TEXT, last_error_at REAL, pairing_lost INTEGER NOT NULL DEFAULT 0, pairing_udid TEXT, pair_host TEXT,
        legacy_pairing INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE apps (id INTEGER PRIMARY KEY, name TEXT NOT NULL, bundle_id TEXT, version TEXT, source_type TEXT NOT NULL,
        source TEXT, source_filter TEXT, source_tag TEXT, account_email TEXT, device_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending', last_error TEXT, signed_at REAL, expires_at REAL, next_attempt_at REAL,
        created_at REAL NOT NULL, channel TEXT NOT NULL DEFAULT 'beta');
    INSERT INTO devices (id, name, pair_identifier, udid, created_at) VALUES (1, 'Salon', 'UDID-TV', 'UDID-TV', 1);
    INSERT INTO apps (id, name, source_type, device_id, created_at) VALUES (1, 'Nuvio', 'github', 1, 1);
    """

    def test_old_database_is_upgraded_in_place(self):
        path = Path(tempfile.mkdtemp(dir=DATA)) / "tvsign.db"
        conn = sqlite3.connect(path)
        conn.executescript(self.OLD_SCHEMA)
        conn.commit()
        conn.close()
        saved = db.DB_PATH
        db.DB_PATH = path
        try:
            db.init()
            dev = db.row("SELECT * FROM devices WHERE id = 1")
            app = db.row("SELECT * FROM apps WHERE id = 1")
            self.assertEqual((dev["name"], dev["family"], dev["dev_mode"], dev["renew_before_hours"]),
                             ("Salon", "tvos", None, None))
            self.assertEqual((app["name"], app["platform"], app["ui_family"], app["warnings"]),
                             ("Nuvio", "appletvos", None, None))
            db.init()                                           # idempotent
            self.assertEqual(db.row("SELECT platform FROM apps WHERE id = 1")["platform"], "appletvos")
            db.execute("INSERT INTO apps (name, source_type, created_at) VALUES ('Phone', 'upload', 2)")
            db.init()                                           # un NULL ultérieur n'est pas réécrit
            self.assertIsNone(db.row("SELECT platform FROM apps WHERE name = 'Phone'")["platform"])
            self.assertEqual(len(list(path.parent.glob("tvsign.db.pre-migrate-*"))), 1)
        finally:
            db.DB_PATH = saved


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(main.app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def add(self, content, device_id=None, name="a.ipa", **extra):
        data = {"source_type": "upload", **extra}
        if device_id is not None:
            data["device_id"] = str(device_id)
        return self.client.post("/api/apps", headers=HEADERS, data=data,
                                files={"file": (name, content, "application/octet-stream")})

    def detail(self, r):
        return r.json()["detail"]

    def test_platform_family_matrix_on_upload(self):
        tv = add_device("TV", "tvos", "AppleTV11,1", "26.0")
        phone = add_device("Phone", "ios", "iPhone15,2", "17.5")
        pad = add_device("Pad", "ipados", "iPad13,4", "17.5")
        r = self.add(make_ipa("iphoneos", "com.example.m1"), tv)
        self.assertEqual((r.status_code, self.detail(r)), (400, "not_tvos:iphoneos"))
        r = self.add(make_ipa("appletvos", "com.example.m2"), phone)
        self.assertEqual((r.status_code, self.detail(r)), (400, "not_ios:appletvos"))
        r = self.add(make_ipa("iphoneos", "com.example.m3", ui_family=[2]), phone)
        self.assertEqual((r.status_code, self.detail(r)), (400, "ipad_only"))
        r = self.add(make_ipa("iphoneos", "com.example.m4", min_os="18.0"), phone)
        self.assertEqual((r.status_code, self.detail(r)), (400, "os_too_old:18.0"))
        r = self.add(make_ipa("appletvos", "com.example.m5"), tv)
        self.assertEqual(r.status_code, 200, r.text)
        r = self.add(make_ipa("iphoneos", "com.example.m6", ui_family=[1, 2], appex=1,
                              binary=b"aps-environment"), phone)
        self.assertEqual(r.status_code, 200, r.text)
        row = r.json()
        self.assertEqual((row["platform"], row["ui_family"], row["warnings"]), ("iphoneos", "1,2", "appex:1,push"))
        r = self.add(make_ipa("iphoneos", "com.example.m7", ui_family=[2]), pad)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(db.rows("SELECT id FROM apps WHERE bundle_id IN "
                                 "('com.example.m1','com.example.m2','com.example.m3','com.example.m4')"), [])

    def test_visionos_device_is_not_supported_yet(self):
        vision = add_device("Vision", "visionos", "RealityDevice14,1", "2.0")
        r = self.add(make_ipa("xros", "com.example.v1"), vision)
        self.assertEqual((r.status_code, self.detail(r)), (400, "unsupported_device"))

    def test_moving_an_app_to_another_family_is_refused(self):
        tv = add_device("TV3", "tvos", "AppleTV11,1", "26.0")
        phone = add_device("Phone3", "ios")
        app_id = self.add(make_ipa("appletvos", "com.example.p1"), tv).json()["id"]
        r = self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"device_id": phone})
        self.assertEqual((r.status_code, self.detail(r)), (400, "not_ios:appletvos"))
        self.assertEqual(db.row("SELECT device_id FROM apps WHERE id = ?", app_id)["device_id"], tv)
        tv2 = add_device("TV4", "tvos", "AppleTV11,1", "26.0")
        self.assertEqual(self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"device_id": tv2}).status_code, 200)

    def test_patch_rejects_unknown_account_and_upload_source(self):
        app_id = self.add(make_ipa("appletvos", "com.example.v2")).json()["id"]
        r = self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"account_email": "ghost@x.y"})
        self.assertEqual((r.status_code, self.detail(r)), (400, "unknown_account"))
        r = self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"source": "owner/repo"})
        self.assertEqual((r.status_code, self.detail(r)), (400, "upload_has_no_source"))
        r = self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"channel": "beta"})
        self.assertEqual((r.status_code, self.detail(r)), (400, "upload_has_no_source"))
        self.assertIsNone(db.row("SELECT source FROM apps WHERE id = ?", app_id)["source"])
        saved = plume.accounts
        plume.accounts = lambda: [{"email": "real@x.y", "ok": 1}]
        try:
            r = self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"account_email": "real@x.y"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["account_email"], "real@x.y")
            r = self.client.patch(f"/api/apps/{app_id}", headers=HEADERS, json={"enabled": False})
            self.assertEqual(r.status_code, 200)
        finally:
            plume.accounts = saved

    def test_create_with_unknown_account(self):
        r = self.add(make_ipa("appletvos", "com.example.v3"), account_email="ghost@x.y")
        self.assertEqual((r.status_code, self.detail(r)), (400, "unknown_account"))

    def test_pair_address_validation(self):
        for ip in ("--version", "-h", "localhost", "127.0.0.1", "0.0.0.0", "8.8.8.8", "224.0.0.1", "::1",
                   "1.2.3.4; ls", ""):
            r = self.client.post("/api/devices/pair", headers=HEADERS, json={"ip": ip, "port": 49152, "name": "TV"})
            self.assertEqual((r.status_code, self.detail(r)), (400, "invalid_address"), ip)
        r = self.client.post("/api/devices/pair", headers=HEADERS, json={"ip": "192.168.1.20", "port": 0, "name": "TV"})
        self.assertEqual(r.status_code, 400)
        saved = devices.start_pairing
        seen = []
        devices.start_pairing = lambda *a: seen.append(a) or SimpleNamespace(public=lambda: {"id": "s"})
        try:
            for ip in ("192.168.1.20", "10.1.2.3", "fe80::1", "100.64.0.9"):
                r = self.client.post("/api/devices/pair", headers=HEADERS, json={"ip": ip, "port": 49152, "name": " "})
                self.assertEqual(r.status_code, 200, ip)
            self.assertEqual(seen[0][1:], ("192.168.1.20", 49152, "Apple TV"))
        finally:
            devices.start_pairing = saved

    def test_ntfy_url_must_be_one_url(self):
        r = self.client.patch("/api/settings", headers=HEADERS, json={"ntfy_url": "http://a b"})
        self.assertEqual(r.status_code, 400)
        r = self.client.patch("/api/settings", headers=HEADERS, json={"ntfy_url": "https://ntfy.sh/my-topic?x=1"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/settings/test-notification", headers=HEADERS, json={"url": "http://a b"})
        self.assertEqual(r.status_code, 400)
        self.client.patch("/api/settings", headers=HEADERS, json={"ntfy_url": None})

    def test_import_pairing_endpoint(self):
        saved = devices.import_pairing

        async def fake(data, filename, udid, name, model=None, family=None):
            if not udid and not families.udid_from_name(filename):
                raise ValueError("udid_required")
            return {"device_id": 7, "udid": udid, "family": family or "ios", "info_error": None}
        devices.import_pairing = fake
        try:
            files = {"file": ("pairing.plist", b"<plist/>", "application/octet-stream")}
            r = self.client.post("/api/devices/import-pairing", headers=HEADERS, files=files)
            self.assertEqual((r.status_code, self.detail(r)), (400, "udid_required"))
            r = self.client.post("/api/devices/import-pairing", headers=HEADERS, files=files,
                                 data={"udid": "00008110-AAAA0000BBBB0009", "name": "x"})
            self.assertEqual((r.status_code, r.json()["device_id"]), (200, 7))
        finally:
            devices.import_pairing = saved

    def test_usb_pairing_endpoint_validates_the_udid(self):
        r = self.client.post("/api/devices/pair-usb", headers=HEADERS, json={"udid": "--help"})
        self.assertEqual((r.status_code, self.detail(r)), (400, "invalid_udid"))
        saved = devices.start_usb_pairing
        devices.start_usb_pairing = lambda udid=None: SimpleNamespace(public=lambda: {"id": "u", "udid": udid})
        try:
            r = self.client.post("/api/devices/pair-usb", headers=HEADERS, json={})
            self.assertEqual(r.status_code, 200)
            r = self.client.post("/api/devices/pair-usb", headers=HEADERS, json={"udid": "00008110-AAAA0000BBBB0009"})
            self.assertEqual(r.json()["udid"], "00008110-AAAA0000BBBB0009")
        finally:
            devices.start_usb_pairing = saved

    def test_device_patch_and_devmode(self):
        phone = add_device("Phone5", "ios")
        tv = add_device("TV5", "tvos", "AppleTV11,1")
        r = self.client.patch(f"/api/devices/{phone}", headers=HEADERS, json={"renew_before_hours": 120, "name": " Moi "})
        self.assertEqual(r.status_code, 200)
        row = db.row("SELECT * FROM devices WHERE id = ?", phone)
        self.assertEqual((row["renew_before_hours"], row["name"]), (120, "Moi"))
        r = self.client.patch(f"/api/devices/{phone}", headers=HEADERS, json={"renew_before_hours": 5})
        self.assertEqual((r.status_code, self.detail(r)), (400, "invalid_hours"))
        r = self.client.patch(f"/api/devices/{phone}", headers=HEADERS, json={"renew_before_hours": None})
        self.assertIsNone(db.row("SELECT renew_before_hours FROM devices WHERE id = ?", phone)["renew_before_hours"])
        r = self.client.patch(f"/api/devices/{phone}", headers=HEADERS, json={"family": "windows"})
        self.assertEqual((r.status_code, self.detail(r)), (400, "invalid_family"))
        r = self.client.post(f"/api/devices/{tv}/devmode", headers=HEADERS)
        self.assertEqual((r.status_code, self.detail(r)), (400, "not_mobile"))
        saved = devices.check_devmode

        async def fake(device):
            return False
        devices.check_devmode = fake
        try:
            r = self.client.post(f"/api/devices/{phone}/devmode", headers=HEADERS)
            self.assertEqual((r.status_code, r.json()), (200, {"dev_mode": False}))
        finally:
            devices.check_devmode = saved

    def test_family_change_checks_the_apps_already_on_the_device(self):
        tv = add_device("TV6", "tvos", "AppleTV11,1")
        self.add(make_ipa("appletvos", "com.example.fam1"), tv)
        r = self.client.patch(f"/api/devices/{tv}", headers=HEADERS, json={"family": "ios"})
        self.assertEqual((r.status_code, self.detail(r)), (400, "not_ios:appletvos"))

    def test_state_exposes_families_and_budget(self):
        phone = add_device("Phone7", "ios")
        db.execute("DELETE FROM apps")           # le compte par défaut (simulé) héberge toutes les apps sans compte
        saved = plume.accounts
        plume.accounts = lambda: [{"email": "acc@x.y", "ok": 1, "selected": True}]
        try:
            r = self.add(make_ipa("iphoneos", "com.example.st1", appex=2), phone, account_email="acc@x.y")
            self.assertEqual(r.status_code, 200, r.text)
            state = self.client.get("/api/state").json()
        finally:
            plume.accounts = saved
        d = next(x for x in state["devices"] if x["id"] == phone)
        self.assertEqual((d["family"], d["renew_before_effective"], d["app_count"]), ("ios", 96, 1))
        a = next(x for x in state["apps"] if x["bundle_id"] == "com.example.st1")
        self.assertEqual((a["device_family"], a["renew_before_hours"]), ("ios", 96))
        acc = next(x for x in state["accounts"] if x["email"] == "acc@x.y")
        self.assertEqual((acc["apps"], acc["app_ids"]), (1, 3))
        self.assertIn("usb_pairing", state["capabilities"])


class Presets(unittest.TestCase):
    def test_presets_carry_a_platform(self):
        listed = ipa.presets()
        self.assertTrue(listed)
        self.assertTrue(all(p["platform"] in ("appletvos", "iphoneos") for p in listed))


if __name__ == "__main__":
    unittest.main()
