"""Les messages d'erreur du serveur sont classés par web/app.js (CAUSES) et traduits par web/i18n.js :
vérifie, sans navigateur, que chaque cause a ses deux langues et que l'ordre des motifs est sûr.

    python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def _causes() -> list[tuple[str, str]]:
    src = _read("errors.js")
    block = src[src.index("const CAUSES = ["):src.index("];", src.index("const CAUSES = ["))]
    return re.findall(r'^\s*\[(/.*/[a-z]*), "(c_\w+)"\],\s*$', block, re.M)


def _i18n_keys() -> dict[str, int]:
    """Clé -> nombre de langues fournies (deux chaînes attendues : fr, en)."""
    out = {}
    for m in re.finditer(r'^\s*(\w+):\s*\[((?:"(?:[^"\\]|\\.)*"\s*,?\s*)+)\]', _read("i18n.js"), re.M):
        out[m.group(1)] = len(re.findall(r'"(?:[^"\\]|\\.)*"', m.group(2)))
    return out


class Causes(unittest.TestCase):
    def test_every_cause_is_translated_in_both_languages(self):
        keys = _i18n_keys()
        causes = _causes()
        self.assertGreater(len(causes), 20)
        for _, k in causes:
            for suffix in ("_t", "_a"):
                self.assertEqual(keys.get(k + suffix), 2, f"{k}{suffix} manque en fr ou en en")

    def test_messages_listing_serials_and_team_names_are_tested_first(self):
        # CERT_LIMIT liste des numéros de série (un « 8220 » y est possible), TEAM_* des noms d'équipe.
        causes = [k for _, k in _causes()]
        self.assertEqual(causes[:2], ["c_certlimit", "c_team"])
        self.assertLess(causes.index("c_certlimit"), causes.index("c_8220"))
        self.assertLess(causes.index("c_certlimit"), causes.index("c_ntfy"))

    def test_the_memory_kill_message_of_the_server_is_recognised(self):
        from app import devices  # noqa: F401  (le texte vient de devices._error_line)
        source = (Path(devices.__file__)).read_text(encoding="utf-8")
        self.assertIn("arrêté par le système", source)
        patterns = dict((k, p) for p, k in _causes())
        self.assertIn("arrêté par le système", patterns["c_oom"])

    def test_generic_certificate_advice_does_not_send_people_to_revoke(self):
        i18n = _read("i18n.js")
        line = next(l for l in i18n.splitlines() if l.lstrip().startswith("c_cert_a:"))
        self.assertNotRegex(line, r"(?i)révoque|revoke")

    def test_account_full_card_has_a_button_to_the_settings(self):
        src = _read("app.js")
        self.assertRegex(src, r'e\.cle === "c_certlimit"[^\n]*href="#/settings"')


if __name__ == "__main__":
    unittest.main()
