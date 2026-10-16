"""Contrôles de l'interface web : tests/web_check.js (traductions, erreurs en clair, saisies), lancé avec Node,
et la table des causes d'erreur des appareils (lue dans web/errors.js, sans navigateur).

    python -m unittest discover -s tests

Sans Node installé, les contrôles qui en ont besoin sont ignorés.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
APP = HERE.parent / "app"
ERRORS_JS = (WEB / "errors.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")


@unittest.skipUnless(shutil.which("node"), "Node.js absent")
class WebChecks(unittest.TestCase):
    def test_web_check(self):
        r = subprocess.run(["node", str(HERE / "web_check.js")], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


def i18n() -> dict:
    """I18N de web/i18n.js, évalué par node."""
    script = (f"const fs=require('fs');const s=fs.readFileSync({json.dumps(str(WEB / 'i18n.js'))},'utf8');"
              "const I=new Function(s+';return I18N;')();console.log(JSON.stringify(I));")
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, check=True, text=True,
                                     encoding="utf-8").stdout)


def causes() -> list[tuple[re.Pattern, str]]:
    """Les lignes [/regex/i, "c_xxx"] de CAUSES dans errors.js, converties en regex Python."""
    block = ERRORS_JS.split("const CAUSES = [", 1)[1].split("\n];", 1)[0]
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
        self.assertEqual(cause_of("installation bloquée à 40 % depuis 12 min (appareil verrouillé ou occupé ?)"),
                         "c_stalled")
        self.assertEqual(cause_of("app pour « iphoneos », seules les apps tvOS sont acceptées sur une Apple TV"),
                         "c_platform")
        self.assertEqual(cause_of("app pour « appletvos », seules les apps iPhone/iPad (iphoneos) sont acceptées "
                                  "sur iPhone"), "c_platform")
        self.assertEqual(cause_of("cette app demande iOS 18.0 ou plus, cet iPhone est en iOS 17.5"), "c_tvos")
        self.assertEqual(cause_of("iPhone « X » introuvable sur le réseau (pas à la maison, Wi-Fi coupé)"),
                         "c_unreachable")
        # une TV garde son explication « app ouverte »
        self.assertEqual(cause_of("installd ne progresse plus depuis 30 min sur « TV »"), "c_app_open")
        self.assertEqual(cause_of("installation bloquée à 40 % depuis 12 min (app ouverte sur la TV ?)"), "c_app_open")

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


class ImportedCertificateCauses(unittest.TestCase):
    """The French sentences of the imported-certificate route (app/imported_cert.py) read as known causes."""

    def test_blocking_messages(self):
        for msg, key in (
                ("aucun certificat importé choisi pour cette app : importes-en un (réglages de l'app › Avancé)", "c_ic_none"),
                ("certificat importé révoqué « Mon certificat » : Apple ne le reconnaît plus", "c_ic_revoked"),
                ("certificat importé expiré le 2026-10-01 « X » : importe un nouveau certificat", "c_ic_expired"),
                ("profil importé expiré le 2026-10-01 « X » : importe un nouveau profil", "c_ic_expired"),
                ("certificat ou profil importé expiré : importe-en un nouveau", "c_ic_expired"),
                ("certificat importé introuvable sur ce serveur « X » (sauvegarde restaurée ?)", "c_ic_missing"),
                ("certificat importé illisible (clé vault.key changée ou fichier abîmé)", "c_ic_missing"),
                ("le certificat importé « X » est fait pour un autre appareil : importe-le pour « Salon »", "c_ic_device"),
                ("le profil importé « X » est fait pour iOS, cet appareil demande tvOS", "c_ic_profile"),
                ("le profil importé « X » ne couvre pas l'identifiant com.a.b (profil : com.c.d)", "c_ic_profile"),
                ("profil importé refusé par l'appareil « X » : aucun profil valide pour cette app", "c_ic_profile"),
                ("droits de l'app refusés par le profil importé « X »", "c_ic_profile"),
                ("identifiant de l'app refusé par le profil importé « X »", "c_ic_profile")):
            self.assertEqual(cause_of(msg), key, msg)

    def test_every_message_of_the_server_is_a_known_cause(self):
        # the French sentences of app/imported_cert.py and the regular expressions of web/errors.js stay in step
        import sys
        sys.path.insert(0, str(HERE.parent))
        from unittest import mock
        try:
            from app import imported_cert as ic
        except Exception as e:  # noqa: BLE001 - the backend cannot be imported here: nothing to compare
            self.skipTest(str(e))
        now = 1_800_000_000.0
        ident = {"id": 1, "name": "Mon certificat", "device_id": 1, "cert_not_after": now + 99 * 86400, "profile_expires": now + 99 * 86400,
                 "profile_platforms": "tvOS", "profile_app_id": "*", "revoked_at": None}
        tv, app = {"id": 1, "name": "Salon", "family": "tvos"}, {"bundle_id": "com.example.tv", "warnings": ""}
        with mock.patch.object(ic, "has_material", lambda ident_id: ident_id != 9999):
            samples = [ic.problem_for(None, tv, app, now), ic.problem_for({**ident, "revoked_at": now}, tv, app, now),
                       ic.problem_for({**ident, "cert_not_after": now - 86400}, tv, app, now),
                       ic.problem_for({**ident, "profile_expires": now - 86400}, tv, app, now),
                       ic.problem_for({**ident, "id": 9999}, tv, app, now), ic.problem_for(ident, {**tv, "id": 2}, app, now),
                       ic.problem_for(ident, {**tv, "family": "ios"}, app, now),
                       ic.problem_for({**ident, "profile_app_id": "com.example.other"}, tv, app, now),
                       ic.problem_for({**ident, "profile_app_id": "com.example.tv"}, tv, {**app, "warnings": "appex:1"}, now)]
            samples += [(kind, str(ic.explain_install_failure(text, ident))) for kind, text in
                        (("revoked", "0xe8008018"), ("expired", "provisioning profile has expired"),
                         ("no profile", "0xe8008015"), ("entitlements", "0xe8008016"),
                         ("identifier", "MismatchedApplicationIdentifierEntitlement"))]
        samples.append(("when the app expires", "certificat ou profil importé expiré : importe-en un nouveau (réglages de l'app › Avancé)"))
        for code, message in samples:
            self.assertTrue((cause_of(message) or "").startswith("c_ic_"), (code, message, cause_of(message)))

    def test_they_do_not_swallow_the_apple_id_causes(self):
        self.assertEqual(cause_of("cette app demande tvOS 26.1 ou plus, l'Apple TV est en tvOS 18.4"), "c_tvos")
        self.assertEqual(cause_of("aucun compte Apple connecté"), "c_no_account")
        self.assertEqual(cause_of("certificat révoqué par un autre outil"), "c_cert")


@unittest.skipUnless(shutil.which("node"), "node absent")
class Strings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.I18N = i18n()

    def test_api_codes_of_the_device_families_are_translated(self):
        for code in ("not_tvos", "not_ios", "ipad_only", "os_too_old", "tvos_too_old", "unsupported_device",
                     "unknown_account", "upload_has_no_source", "invalid_address", "invalid_udid", "udid_required",
                     "invalid_pairing_file", "not_mobile", "invalid_hours", "invalid_family", "unknown_device"):
            self.assertIn("err_" + code, self.I18N, code)

    def test_api_codes_of_the_imported_certificates_are_translated(self):
        codes = ("cert_unavailable", "cert_p12_invalid", "cert_password_wrong", "cert_p12_no_key", "cert_p12_no_cert",
                 "cert_key_unsupported", "cert_key_mismatch", "cert_expired", "cert_not_yet_valid", "cert_profile_invalid",
                 "cert_profile_expired", "cert_not_in_profile", "cert_platform", "cert_profile_not_for_devices",
                 "cert_device_unknown", "cert_device_not_in_profile", "cert_bundle_mismatch", "cert_needs_wildcard",
                 "cert_wrong_device", "cert_revoked", "cert_none", "cert_missing", "cert_in_use", "cert_needs_device",
                 "cert_store_failed", "unknown_cert", "invalid_sign_mode")
        for code in codes:
            self.assertIn("err_" + code, self.I18N, code)
        # and every such code the server can answer is in that list
        sources = "\n".join((APP / name).read_text(encoding="utf-8") for name in ("routes_cert.py", "main.py", "imported_cert.py"))
        used = set(re.findall(r'HTTPException\(\d+, f?"((?:cert_|unknown_cert|invalid_sign_mode)[a-z0-9_]*)', sources))
        used |= set(re.findall(r'CertError\(f?"(cert_[a-z0-9_]+)', sources))
        used |= set(re.findall(r'return \(f?"(cert_[a-z0-9_]+)', sources))
        used |= set(re.findall(r'return f?"(cert_[a-z0-9_]+)', sources))
        self.assertTrue(len(used) >= 10, f"search pattern out of date: {sorted(used)}")
        self.assertEqual(sorted(c for c in used if "err_" + c not in self.I18N), [])

    def test_details_with_parameters_read_well(self):
        script = (
            "const fs=require('fs');const vm=require('vm');const ctx=vm.createContext({console,URL,navigator:{languages:['en']}});"
            f"vm.runInContext(fs.readFileSync({json.dumps(str(WEB / 'i18n.js'))},'utf8')+fs.readFileSync({json.dumps(str(WEB / 'errors.js'))},'utf8'),ctx);"
            "const out={};for(const [d,l] of [['cert_platform:iOS:tvOS','en'],['cert_bundle_mismatch:com.a.*:com.b.c','en'],"
            "['cert_expired:2026-10-01','fr'],['cert_in_use:Nuvio, Autre','fr']]){vm.runInContext('setLang(\"'+l+'\")',ctx);"
            "out[d]=vm.runInContext('apiError('+JSON.stringify(d)+',400)',ctx);}console.log(JSON.stringify(out));")
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, check=True, text=True, encoding="utf-8").stdout)
        self.assertEqual(out["cert_platform:iOS:tvOS"], "This profile is made for iOS, this device needs tvOS.")
        self.assertIn("“com.a.*”", out["cert_bundle_mismatch:com.a.*:com.b.c"])
        self.assertIn("“com.b.c”", out["cert_bundle_mismatch:com.a.*:com.b.c"])
        self.assertEqual(out["cert_expired:2026-10-01"], "Ce certificat a expiré le 2026-10-01.")
        self.assertIn("Nuvio, Autre", out["cert_in_use:Nuvio, Autre"])

    def test_the_interface_no_longer_hardcodes_apple_tv_where_any_device_goes(self):
        self.assertNotIn('<h2 class="group__header" id="tv-titre">Apple TV</h2>', HTML)
        self.assertNotIn('<span>Apple TV</span><select name="device_id">', HTML)
