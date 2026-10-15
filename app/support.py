"""Aide au support : journal exportable et dossier de diagnostic à joindre à un rapport de bogue.

Tout ce qui sort d'ici passe par redact() : adresses e-mail, adresses IP, identifiants d'appareil,
jeton et mot de passe d'adresse, secrets des destinations de notification sont remplacés. Le
dossier ne contient ni la base, ni la session Apple, ni les clés (contrairement à la sauvegarde).
"""
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

from . import db, diag, notifier, plume, proc, renew, settings
from .config import DATA_DIR, VERSION

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IPV4 = re.compile(r"\b((?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d))\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}\b")
_UDID = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b|\b[0-9a-fA-F]{40}\b")
_USERINFO = re.compile(r"(\w+://)[^/\s:@]+:[^/\s@]+@")
_QUERY_SECRET = re.compile(r"(?i)([?&](?:token|key|apikey|api_key|access_token|password|secret|auth)=)[^&\s\"']+")
_TELEGRAM = re.compile(r"bot\d+:[\w-]{20,}")
_DISCORD = re.compile(r"(?i)(discord(?:app)?\.com/api/webhooks/)\S+")
_AUTH_HEADER = re.compile(r"(?i)\b(bearer|basic)\s+[\w.=+/-]{8,}")


def _homes() -> list[str]:
    """Dossier personnel sous toutes ses écritures, la plus longue d'abord : tel quel, avec des barres
    obliques, et avec les barres inverses doublées (json.dumps double les barres inverses d'un chemin
    Windows avant que redact() ne voie le texte du dossier de support)."""
    home = str(Path.home())
    if len(home) <= 3:
        return []
    return sorted({home, home.replace("\\", "\\\\"), home.replace("\\", "/"), Path.home().as_posix()},
                  key=len, reverse=True)


def redact(value: str, secrets: list[str] | None = None) -> str:
    """Masque ce qui identifie l'utilisateur, son réseau ou ses comptes."""
    out = str(value)
    for secret in sorted(set(secrets or []), key=len, reverse=True):
        if len(secret) >= 4:
            out = out.replace(secret, "***")
    for home in _homes():
        out = out.replace(home, "~")
    out = _USERINFO.sub(r"\1***@", out)
    out = _QUERY_SECRET.sub(r"\1***", out)
    out = _TELEGRAM.sub("bot***", out)
    out = _DISCORD.sub(r"\1***", out)
    out = _AUTH_HEADER.sub(r"\1 ***", out)
    out = _EMAIL.sub("<email>", out)
    out = _UDID.sub("<udid>", out)
    out = _IPV4.sub(r"\1.x.x", out)
    out = _IPV6.sub("<ipv6>", out)
    return out


def _known_secrets() -> list[str]:
    values = []
    for t in notifier.targets():
        values += notifier._secrets(t)
    return values


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


def log_text(app_id: int | None = None, level: str | None = None) -> str:
    """Journal (au plus 1000 lignes gardées par la base) en texte, du plus ancien au plus récent."""
    where, args = [], []
    if app_id is not None:
        where.append("app_id = ?")
        args.append(app_id)
    if level == "problems":
        where.append("level != 'info'")
    rows = db.rows("SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
                   + " ORDER BY id DESC LIMIT 1000", *args)
    secrets = _known_secrets()
    names = {a["id"]: a["name"] for a in db.rows("SELECT id, name FROM apps")}
    lines = []
    for r in reversed(rows):
        who = f" [{names.get(r['app_id'], r['app_id'])}]" if r["app_id"] else ""
        lines.append(redact(f"{_iso(r['ts'])} {r['level'].upper():5}{who} {r['message']}", secrets))
    return "\n".join(lines) + ("\n" if lines else "")


def _mask_email(email: str | None, table: dict[str, str]) -> str | None:
    if not email:
        return None
    return table.setdefault(email, f"account-{len(table) + 1}")


async def build_bundle() -> bytes:
    """Zip en mémoire : système, diagnostic, réglages (secrets masqués), apps, journal, historique des
    renouvellements et dernières sorties de plumesign."""
    secrets = _known_secrets()
    accounts: dict[str, str] = {}

    def clean(obj) -> str:
        return redact(json.dumps(obj, indent=1, ensure_ascii=False, default=str), secrets)

    apps = []
    for a in db.rows("SELECT * FROM apps ORDER BY id"):
        apps.append({
            "id": a["id"], "name": a["name"], "bundle_id": a["bundle_id"], "version": a["version"],
            "source_type": a["source_type"], "source": a["source"] if a["source_type"] != "upload" else None,
            "channel": a["channel"], "preset": a["preset"], "source_tag": a["source_tag"],
            "enabled": a["enabled"], "status": a["status"], "error_kind": a.get("error_kind"),
            "last_error": a["last_error"], "last_trigger": a["last_trigger"],
            "signed_at": _iso(a["signed_at"]), "expires_at": _iso(a["expires_at"]),
            "last_attempt_at": _iso(a["last_attempt_at"]), "next_attempt_at": _iso(a["next_attempt_at"]),
            "notified_level": a["notified_level"], "auto_update": a.get("auto_update"),
            "update_pending": a.get("update_pending"), "previous_version": a.get("prev_version"),
            "available": a.get("avail_tag"), "bad_tag": a["bad_tag"],
            "account": _mask_email(a["account_email"], accounts), "device_id": a["device_id"],
        })
    devices = [{"id": d["id"], "model": d["model"], "os_version": d["os_version"],
                "last_seen": _iso(d["last_seen"]), "last_error": d["last_error"],
                "pairing_lost": d["pairing_lost"], "legacy_pairing": d["legacy_pairing"]}
               for d in db.rows("SELECT * FROM devices ORDER BY id")]
    accs = [{"account": _mask_email(a["email"], accounts), "ok": a["ok"], "message": a.get("message"),
             "checked_at": _iso(a.get("checked_at"))} for a in plume.accounts()]
    renewals = [{**r, "started": _iso(r["started"]), "finished": _iso(r["finished"])}
                for r in db.rows("SELECT * FROM renewals ORDER BY id DESC LIMIT 200")]
    shown = {k: v["value"] for k, v in settings.describe().items()}
    shown["ntfy_url"] = notifier.summary({"type": "ntfy", "url": settings.ntfy_url()})   # hôte seulement : le sujet fait office de mot de passe
    shown["notification_destinations"] = [
        {"type": t["type"], "events": t["events"], "host": notifier.summary(t) if t["type"] != "telegram" else "telegram"}
        for t in notifier.targets()]
    health = {k: (_iso(v) if k.endswith("_at") else v) for k, v in renew.health().items()}
    system = {
        "created": _iso(time.time()), "tvsign": VERSION, "python": sys.version.split()[0],
        "platform": sys.platform, "plumesign": await plume.version(), "data_dir": str(DATA_DIR),
        "language": notifier.lang(), "settings": shown, "health": health,
    }
    try:
        diagnostics = await diag.run_all()
    except Exception as e:  # noqa: BLE001 - le dossier doit sortir même si une sonde casse
        diagnostics = {"error": str(e)}
    tails = []
    for ts, label, code, lines in list(proc.RING):
        tails.append(f"=== {_iso(ts)} {label} (code {code})\n" + "\n".join(lines))

    readme = (
        "tvsign support bundle\n"
        "Created by Settings > Diagnostics > Support bundle. E-mail addresses, IP addresses, device\n"
        "identifiers, URL passwords/tokens and notification secrets are masked. It contains no\n"
        "database, Apple session or key. Attach it to your bug report.\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", readme)
        z.writestr("system.json", clean(system))
        z.writestr("diagnostics.json", clean(diagnostics))
        z.writestr("apps.json", clean({"apps": apps, "devices": devices, "accounts": accs}))
        z.writestr("renewals.json", clean(renewals))
        z.writestr("events.txt", log_text())
        z.writestr("plumesign-output.txt", redact("\n\n".join(tails), secrets) or "(none yet)\n")
    return buf.getvalue()


def summary_text(diagnostics: dict, events: int = 30) -> str:
    """Texte court à coller dans un ticket (bouton « Copier »), même masquage que le dossier."""
    secrets = _known_secrets()
    lines = [f"tvsign {diagnostics.get('version', VERSION)} / python {diagnostics.get('python')} / "
             f"{diagnostics.get('platform')}"]
    for c in diagnostics.get("checks", []):
        lines.append(f"[{c['status']}] {c['id']}: {c.get('detail', '')}")
    lines.append("")
    lines += log_text().splitlines()[-events:]
    return redact("\n".join(lines), secrets) + "\n"
