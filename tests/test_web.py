"""Contrôles de l'interface web (traductions, erreurs en clair, saisies) : tests/web_check.js, lancé avec Node.

    python -m unittest discover -s tests

Sans Node installé, ces contrôles sont ignorés.
"""
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which("node"), "Node.js absent")
class WebChecks(unittest.TestCase):
    def test_web_check(self):
        r = subprocess.run(["node", str(HERE / "web_check.js")], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
