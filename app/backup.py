"""Backup export / import: one zip holding the database (SQLite backup API) and the plumesign
store (Apple sessions, certificate keys, Apple TV pairings). Same format on Docker and Windows,
so an instance can move between machines without pairing again.

The archive contains secrets: whoever holds it can use the Apple ID session.
"""
import json
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path, PurePosixPath

from . import db, settings
from .config import DATA_DIR, DB_PATH, IPA_DIR, PLUME_HOME, TMP_DIR, VERSION
from .util import replace_retry

FORMAT = "tvsign-backup"
FORMAT_VERSION = 1
STORE = PLUME_HOME / "PlumeImpactor"
# Clés propres à l'instance : conservées lors d'une restauration (mot de passe choisi ici, etc.).
KEEP_LOCAL = ("admin_password", "setup_done", "session_version", "onboarded", "language")


def export(include_uploads: bool = False) -> Path:
    """Builds the archive in TMP_DIR and returns its path (caller deletes it)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = TMP_DIR / f"tvsign-{stamp}.tvsign-backup"
    snap = TMP_DIR / f"db-{stamp}.sqlite"
    db.snapshot(snap)
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps({
                "format": FORMAT, "version": FORMAT_VERSION, "created": time.time(),
                "tvsign_version": VERSION, "include_uploads": include_uploads,
            }, indent=1))
            z.write(snap, "tvsign.db")
            if STORE.is_dir():
                for f in sorted(STORE.rglob("*")):
                    if f.is_file() and not f.name.endswith((".tmp", ".lock")):
                        z.write(f, "plume/" + f.relative_to(STORE).as_posix())
            if include_uploads:
                for a in db.rows("SELECT id FROM apps WHERE source_type = 'upload'"):
                    f = IPA_DIR / str(a["id"]) / "original.ipa"
                    if f.exists():
                        z.write(f, f"ipas/{a['id']}/original.ipa")
    finally:
        snap.unlink(missing_ok=True)
    return out


def _safe(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name:
        raise ValueError(f"chemin refusé dans l'archive : {name}")
    return p


def restore(archive: Path) -> dict:
    """Validates then replaces the database and the plumesign store. The previous ones are kept
    next to them (tvsign.db.pre-restore-<ts>, plume/PlumeImpactor.pre-restore-<ts>)."""
    try:
        z = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as e:
        raise ValueError(f"archive illisible ({e})") from e
    with z:
        names = z.namelist()
        for n in names:
            _safe(n)
        try:
            manifest = json.loads(z.read("manifest.json"))
        except KeyError:
            raise ValueError("ce fichier n'est pas une sauvegarde tvsign (manifest.json absent)") from None
        if manifest.get("format") != FORMAT:
            raise ValueError("ce fichier n'est pas une sauvegarde tvsign")
        if int(manifest.get("version", 0)) > FORMAT_VERSION:
            raise ValueError("sauvegarde faite par une version plus récente de tvsign")
        if "tvsign.db" not in names:
            raise ValueError("base de données absente de la sauvegarde")

        stamp = int(time.time())
        work = TMP_DIR / f"restore-{stamp}"
        work.mkdir(parents=True)
        try:
            new_db = work / "tvsign.db"
            new_db.write_bytes(z.read("tvsign.db"))
            conn = sqlite3.connect(new_db)
            try:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("base de données de la sauvegarde corrompue")
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"apps", "devices"} <= tables:
                    raise ValueError("base de données inattendue dans la sauvegarde")
            finally:
                conn.close()
            plume_dir = work / "PlumeImpactor"
            ipas = []
            for n in names:
                if n.endswith("/"):
                    continue
                p = _safe(n)
                if p.parts[0] == "plume" and len(p.parts) > 1:
                    dest = plume_dir.joinpath(*p.parts[1:])
                elif p.parts[0] == "ipas" and len(p.parts) == 3 and p.parts[1].isdigit():
                    dest = work / "ipas" / p.parts[1] / p.parts[2]
                    ipas.append(p.parts[1])
                else:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(n) as src, open(dest, "wb") as f:
                    shutil.copyfileobj(src, f)

            keep = {k: settings.raw_get(k) for k in KEEP_LOCAL}
            if DB_PATH.exists():
                db.snapshot(DATA_DIR / f"tvsign.db.pre-restore-{stamp}")
            replace_retry(new_db, DB_PATH)
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
            if plume_dir.is_dir():
                if STORE.exists():
                    STORE.rename(STORE.with_name(f"PlumeImpactor.pre-restore-{stamp}"))
                shutil.move(str(plume_dir), str(STORE))
            # IPA de l'ancienne instance : mises de côté (les numéros d'app de la sauvegarde
            # désigneraient sinon des fichiers d'autres apps) ; les sources GitHub/lien se retéléchargent
            if IPA_DIR.exists() and any(IPA_DIR.iterdir()):
                IPA_DIR.rename(IPA_DIR.with_name(f"ipas.pre-restore-{stamp}"))
            IPA_DIR.mkdir(parents=True, exist_ok=True)
            for app_id in set(ipas):
                d = IPA_DIR / app_id
                d.mkdir(parents=True, exist_ok=True)
                replace_retry(work / "ipas" / app_id / "original.ipa", d / "original.ipa")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    db.init()
    for k, v in keep.items():
        if v is not None:
            settings.raw_set(k, v)
    settings.raw_set("onboarded", "1")
    counts = {
        "apps": len(db.rows("SELECT id FROM apps")),
        "devices": len(db.rows("SELECT id FROM devices")),
        "created": manifest.get("created"),
        "tvsign_version": manifest.get("tvsign_version"),
    }
    db.log(f"Sauvegarde restaurée ({counts['apps']} app(s), {counts['devices']} Apple TV)")
    return counts

