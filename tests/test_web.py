"""Interface : table des causes d'erreur et textes fr/en (lue dans web/, sans navigateur).

    python -m unittest discover -s tests
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")


def i18n() -> dict:
    """I18N de web/i18n.js, évalué par node."""
    script = (f"const fs=require('fs');const s=fs.readFileSync({json.dumps(str(WEB / 'i18n.js'))},'utf8');"
              "const I=new Function(s+';return I18N;')();console.log(JSON.stringify(I));")
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, check=True, text=True,
                                     encoding="utf-8").stdout)


def causes() -> list[tuple[re.Pattern, str]]:
    """Les lignes [/regex/i, "c_xxx"] de CAUSES dans app.js, converties en regex Python."""
    block = APP_JS.split("const CAUSES = [", 1)[1].split("];", 1)[0]
    out = []
    for m in re.finditer(r'\[/(.+)/(i?), "(c_\w+)"\]', block):
        out.append((re.compile(m.group(1), re.I if m.group(2) else 0), m.group(3)))
    return out


def cause_of(message: str) -> str | None:
    return next((k for rx, k in causes() if rx.search(message)), None)


class ErrorTable(unittest.TestCase):
    def test_xcode_free_account_device_limit(self):
        # libellé d'Xcode / installd : « maximum number of installed apps using a free developer profile »
        for msg in ("This device has reached the maximum number of installed apps using a free developer profile.",
                    "maximum number of installed apps",
                    "Your maximum App ID limit has been reached",
                    "You have reached the maximum number of apps",
                    "too many app ids"):
            self.assertEqual(cause_of(msg), "c_limit", msg)

    def test_unrelated_messages_are_not_limits(self):
        self.assertNotEqual(cause_of("installd ne progresse plus"), "c_limit")
        self.assertIsNone(cause_of("maximum speed reached"))

    def test_phone_causes(self):
        self.assertEqual(cause_of("iPhone « X » verrouillé depuis son démarrage : déverrouille-le une fois"), "c_locked")
        self.assertEqual(cause_of("l'installation n'avance plus depuis 30 min sur « X »"), "c_stalled")
        self.assertEqual(cause_of("app pour « iphoneos », seules les apps tvOS sont acceptées sur une Apple TV"),
                         "c_platform")
        self.assertEqual(cause_of("app pour « appletvos », seules les apps iPhone/iPad (iphoneos) sont acceptées "
                                  "sur iPhone"), "c_platform")
        self.assertEqual(cause_of("cette app demande iPhone 18.0 ou plus récent, l'appareil est en 17.5"), "c_platform")
        self.assertEqual(cause_of("iPhone « X » introuvable sur le réseau (pas à la maison, Wi-Fi coupé)"),
                         "c_unreachable")
        # une TV garde son explication « app ouverte »
        self.assertEqual(cause_of("installd ne progresse plus depuis 30 min sur « TV »"), "c_app_open")

    def test_usb_pairing_failures_are_explained(self):
        # messages de plumesign pair usb (wireless/usb.rs), affichés tels quels dans la sortie de l'appairage
        for msg, key in (
                ("usbmuxd is not reachable (os error 2): install iTunes or Apple Devices on Windows, or the usbmuxd "
                 "package on Linux", "c_usbmuxd"),
                ("No iPhone or iPad connected over USB (plug it in, unlock it and accept \"Trust this computer\")",
                 "c_usb_none"),
                ("No device 00008110-AAAA0000BBBB0001 connected over USB (unlock it and accept \"Trust this "
                 "computer\")", "c_usb_none"),
                ("2 devices are connected over USB: pass the one to pair with --udid", "c_usb_many"),
                ("The device offers no tunnel service (iOS 17 or later is required)", "c_ios17")):
            self.assertEqual(cause_of(msg), key, msg)


@unittest.skipUnless(shutil.which("node"), "node absent")
class Strings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.I18N = i18n()

    def test_every_entry_has_both_languages_with_the_same_placeholders(self):
        holes = lambda s: sorted(set(re.findall(r"\{(\w+)\}", s)))     # noqa: E731
        for key, pair in self.I18N.items():
            self.assertEqual(len(pair), 2, key)
            fr, en = pair
            self.assertTrue(fr.strip() and en.strip(), key)
            self.assertEqual(holes(fr), holes(en), key)

    def test_every_cause_has_a_title_and_advice(self):
        for _, key in causes():
            self.assertIn(key + "_t", self.I18N, key)
            self.assertIn(key + "_a", self.I18N, key)

    def test_keys_used_by_the_interface_exist(self):
        used = set(re.findall(r'\bt\("([a-z0-9_]+)"', APP_JS))
        used |= set(re.findall(r'\btn\("([a-z0-9_]+)"', APP_JS))
        used |= set(re.findall(r'data-i18n(?:-\w+)?="([a-z0-9_]+)"', HTML))
        used = {k for k in used if not k.endswith("_")}          # t("diag_" + id) : clé construite
        missing = sorted(k for k in used if k not in self.I18N and f"{k}_one" not in self.I18N)
        self.assertEqual(missing, [])

    def test_api_codes_of_the_device_families_are_translated(self):
        for code in ("not_tvos", "not_ios", "ipad_only", "os_too_old", "unsupported_device", "unknown_account",
                     "upload_has_no_source", "invalid_address", "invalid_udid", "udid_required",
                     "invalid_pairing_file", "not_mobile", "invalid_hours", "invalid_family", "unknown_device"):
            self.assertIn("err_" + code, self.I18N, code)

    def test_the_interface_no_longer_hardcodes_apple_tv_where_any_device_goes(self):
        self.assertNotIn('<h2 class="group__header" id="tv-titre">Apple TV</h2>', HTML)
        self.assertNotIn('<span>Apple TV</span><select name="device_id">', HTML)


if __name__ == "__main__":
    unittest.main()
