"""Regenerates app/reasons.py from the interface: the known causes of failures (CAUSES in web/errors.js) and their
English titles (web/i18n.js). The server uses them to say a failure in English in a notification; the interface
keeps being the source, and tests/test_notify_ops.py fails when the copy is out of date.

    python scripts/sync-reasons.py          # rewrites app/reasons.py (needs Node.js, to read web/i18n.js)
    python scripts/sync-reasons.py --check  # exit 1 if app/reasons.py is out of date
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB, OUT = ROOT / "web", ROOT / "app" / "reasons.py"

HEADER = '''"""Causes connues des échecs, dites en anglais, pour les notifications (le serveur écrit ses erreurs en français).

Copie des CAUSES de web/errors.js (même ordre : la première qui correspond l'emporte) avec le titre anglais de
chacune dans web/i18n.js. Fichier généré par `python scripts/sync-reasons.py` : ne pas le modifier à la main, un test
vérifie qu'il suit l'interface.
"""
import re

CAUSES: list[tuple[re.Pattern, str, str]] = [
'''


def interface() -> tuple[list[tuple[str, bool, str]], dict]:
    errors = (WEB / "errors.js").read_text(encoding="utf-8")
    block = errors.split("const CAUSES = [", 1)[1].split("\n];", 1)[0]
    causes = [(m.group(1), bool(m.group(2)), m.group(3))
              for m in re.finditer(r'\[/(.+)/(i?), "(c_\w+)"\]', block)]
    script = ("const fs=require('fs');const s=fs.readFileSync(%s,'utf8');"
              "const I=new Function(s+';return I18N;')();console.log(JSON.stringify(I));" % json.dumps(str(WEB / "i18n.js")))
    out = subprocess.run(["node", "-e", script], capture_output=True, check=True, text=True, encoding="utf-8").stdout
    return causes, json.loads(out)


def raw(pattern: str) -> str:
    """The pattern as a Python raw string literal (readable: no doubled backslashes) when one can be written."""
    for quote in ('"', "'"):
        if quote not in pattern and not pattern.endswith("\\"):
            return f"r{quote}{pattern}{quote}"
    return repr(pattern)


def render() -> str:
    causes, i18n = interface()
    lines = []
    for pattern, ignore_case, key in causes:
        title = i18n[key + "_t"][1]
        flag = ", re.I" if ignore_case else ""
        lines.append(f"    (re.compile({raw(pattern)}{flag}), {json.dumps(key)}, {json.dumps(title, ensure_ascii=False)}),\n")
    footer = ''']


def english(text: str) -> str | None:
    """Titre anglais de la première cause reconnue dans ce message d'erreur, ou None."""
    for rx, _key, title in CAUSES:
        if rx.search(text or ""):
            return title
    return None
'''
    return HEADER + "".join(lines) + footer


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        sys.exit(0 if current.replace("\r\n", "\n") == text else 1)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"{OUT.relative_to(ROOT)} written")
