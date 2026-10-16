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
import zlib
from pathlib import Path, PurePosixPath

from . import db, settings
from .config import DATA_DIR, DB_PATH, IPA_DIR, PLUME_HOME, TMP_DIR, VERSION
from .util import replace_retry

FORMAT = "tvsign-backup"
FORMAT_VERSION = 1
STORE = PLUME_HOME / "PlumeImpactor"
# Clés propres à l'instance : conservées lors d'une restauration (mot de passe choisi ici, etc.).
# Celles d'AUTH_LOCAL sont reprises telles quelles, absence comprise : une sauvegarde venant d'une
# instance protégée ne doit pas imposer son mot de passe (ni ses sessions) à celle-ci.
KEEP_LOCAL = ("admin_password", "setup_done", "session_version", "onboarded", "language")
AUTH_LOCAL = ("admin_password", "setup_done", "session_version")


KEEP_ASIDE_DAYS = 30


def prune_old(days: float = KEEP_ASIDE_DAYS) -> None:
    """Supprime les copies mises de côté (avant restauration ou migration) de plus de `days` jours :
    elles contiennent la session Apple et les clés, elles ne doivent pas traîner indéfiniment."""
    cutoff = time.time() - days * 86400
    for base, pattern in ((DATA_DIR, "tvsign.db.pre-restore-*"), (DATA_DIR, "tvsign.db.pre-migrate-*"),
                          (DATA_DIR, "ipas.pre-restore-*"), (PLUME_HOME, "PlumeImpactor.pre-restore-*")):
        for p in base.glob(pattern):
            try:
                if p.stat().st_mtime < cutoff:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
            except OSError:
                pass


def export(include_uploads: bool = False) -> Path:
    """Builds the archive in TMP_DIR and returns its path (caller deletes it)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = TMP_DIR / f"tvsign-{stamp}.tvsign-backup"
    snap = TMP_DIR / f"db-{stamp}.sqlite"
    db.snapshot(snap)
    try:
        _strip_local(snap)
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


def _strip_local(snap: Path) -> None:
    """Retire de la copie exportée ce qui n'appartient qu'à cette instance : hash du mot de passe
    administrateur, version des sessions, sessions révoquées. Une restauration ne les reprend de toute
    façon pas (AUTH_LOCAL) ; une archive envoyée par courriel ou sauvegardée dans le cloud ne doit pas
    permettre de chercher le mot de passe hors ligne."""
    conn = sqlite3.connect(snap)
    try:
        conn.execute("DELETE FROM settings WHERE key IN ('admin_password', 'session_version', 'setup_done')")
        conn.execute("DELETE FROM revoked_sessions")
        conn.commit()
    finally:
        conn.close()


def _manifest(z: zipfile.ZipFile) -> dict:
    """manifest.json validé : un dictionnaire de format et de version reconnus, sinon BackupError."""
    try:
        manifest = json.loads(z.read("manifest.json"))
    except KeyError:
        raise BackupError("backup_not_tvsign", "ce fichier n'est pas une sauvegarde tvsign (manifest.json absent)") from None
    except (ValueError, zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as e:
        raise BackupError("backup_not_tvsign", f"manifeste illisible ({e})") from e
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError("backup_not_tvsign", "ce fichier n'est pas une sauvegarde tvsign")
    try:
        version = int(manifest.get("version") or 0)
    except (TypeError, ValueError):
        raise BackupError("backup_not_tvsign", "version de sauvegarde illisible") from None
    if version > FORMAT_VERSION:
        raise BackupError("backup_too_new", "sauvegarde faite par une version plus récente de tvsign")
    return manifest


def _safe(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name:
        raise ValueError(f"chemin refusé dans l'archive : {name}")
    return p


class BackupError(ValueError):
    """Sauvegarde refusée ou restauration annulée. `code` est renvoyé tel quel par l'API (clé
    err_<code> de l'interface) ; le message reste lisible dans le journal."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


# Une restauration est en cours : la boucle de renouvellement et l'API se mettent en retrait.
RESTORING = False


def _reference_columns() -> dict[str, set[str]]:
    """Colonnes de base de chaque table du schéma actuel (les colonnes de MIGRATIONS s'ajoutent seules)."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(db.SCHEMA)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")} for t in tables}
    finally:
        conn.close()


def validate_db(path: Path) -> None:
    """Vérifie une base candidate AVANT de remplacer la vraie : intégrité, tables et colonnes de base
    attendues, migration vers le schéma actuel (faite sur cette copie) et requêtes de l'interface.
    Lève BackupError ; la copie `path` est migrée sur place."""
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("backup_corrupt", "base de données de la sauvegarde corrompue")
            have = {r[0]: {c[1] for c in conn.execute(f'PRAGMA table_info("{r[0]}")')}
                    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"apps", "devices"} <= have.keys():
                raise BackupError("backup_incompatible", "base de données inattendue dans la sauvegarde")
            for table, cols in _reference_columns().items():
                if table in have and not cols <= have[table]:
                    raise BackupError("backup_incompatible",
                                      f"table {table} incompatible (colonnes manquantes : {sorted(cols - have[table])})")
            db.migrate(conn)
            conn.commit()
            conn.execute("SELECT a.*, d.name AS device_name FROM apps a LEFT JOIN devices d ON d.id = a.device_id "
                         "ORDER BY a.name").fetchall()
            conn.execute("SELECT * FROM devices ORDER BY name").fetchall()
            conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 80").fetchall()
        finally:
            conn.close()
    except BackupError:
        raise
    except sqlite3.Error as e:
        raise BackupError("backup_corrupt", f"base de données illisible ({e})") from e


def _swap_dir(live: Path, staged: Path | None, aside: Path, undo: list) -> None:
    """Met `live` de côté (sous le nom `aside`) puis met `staged` à sa place, en enregistrant comment annuler."""
    had = live.exists()
    if had:
        replace_retry(live, aside)
    undo.append(lambda: (shutil.rmtree(live, ignore_errors=True), had and aside.exists() and replace_retry(aside, live)))
    if staged is not None:
        shutil.move(str(staged), str(live))


def restore(archive: Path) -> dict:
    """Validates then replaces the database and the plumesign store. The previous ones are kept
    next to them (tvsign.db.pre-restore-<ts>, plume/PlumeImpactor.pre-restore-<ts>) for 30 days.

    Tout est contrôlé et préparé avant le premier remplacement ; si un remplacement échoue ensuite
    (fichier verrouillé par un antivirus, disque plein…), ceux déjà faits sont annulés : l'instance
    reste telle qu'elle était et l'erreur est claire (BackupError)."""
    try:
        z = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as e:
        raise BackupError("backup_unreadable", f"archive illisible ({e})") from e
    with z:
        names = z.namelist()
        for n in names:
            try:
                _safe(n)
            except ValueError as e:
                raise BackupError("backup_bad_path", str(e)) from e
        manifest = _manifest(z)
        if "tvsign.db" not in names:
            raise BackupError("backup_no_db", "base de données absente de la sauvegarde")

        stamp = int(time.time())
        work = TMP_DIR / f"restore-{stamp}"
        work.mkdir(parents=True, exist_ok=True)
        undo: list = []
        try:
            new_db = work / "tvsign.db"
            try:
                new_db.write_bytes(z.read("tvsign.db"))
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as e:
                raise BackupError("backup_unreadable", f"archive illisible ({e})") from e
            validate_db(new_db)
            plume_dir = work / "PlumeImpactor"
            ipas = []
            try:
                for n in names:
                    if n.endswith("/"):
                        continue
                    p = _safe(n)
                    if p.parts[0] == "plume" and len(p.parts) > 1:
                        dest = plume_dir.joinpath(*p.parts[1:])
                    elif (p.parts[0] == "ipas" and len(p.parts) == 3 and p.parts[1].isdigit()
                          and p.parts[2] == "original.ipa"):
                        dest = work / "ipas" / p.parts[1] / p.parts[2]
                        ipas.append(p.parts[1])
                    else:
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(n) as src, open(dest, "wb") as f:
                        shutil.copyfileobj(src, f)
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as e:
                raise BackupError("backup_unreadable", f"archive illisible ({e})") from e

            keep = {k: settings.raw_get(k) for k in KEEP_LOCAL}
            # Sessions dont l'utilisateur s'est déconnecté : l'archive n'en contient pas (_strip_local), mais
            # session_version est conservé, donc sans ceci leurs cookies redeviendraient valides.
            try:
                revoked = [(r["sid"], r["until"]) for r in db.rows("SELECT sid, until FROM revoked_sessions")]
            except sqlite3.DatabaseError:
                revoked = []
            pre_db = DATA_DIR / f"tvsign.db.pre-restore-{stamp}"
            if DB_PATH.exists():
                db.snapshot(pre_db)
            try:
                # dossiers d'abord, base en dernier : tant que la base n'est pas remplacée, rien n'est perdu
                if plume_dir.is_dir():
                    _swap_dir(STORE, plume_dir, STORE.with_name(f"PlumeImpactor.pre-restore-{stamp}"), undo)
                # IPA de l'ancienne instance : mises de côté (les numéros d'app de la sauvegarde
                # désigneraient sinon des fichiers d'autres apps) ; les sources GitHub/lien se retéléchargent
                staged_ipas = work / "ipas"
                if (IPA_DIR.exists() and any(IPA_DIR.iterdir())) or staged_ipas.is_dir():
                    _swap_dir(IPA_DIR, staged_ipas if staged_ipas.is_dir() else None,
                              IPA_DIR.with_name(f"ipas.pre-restore-{stamp}"), undo)
                IPA_DIR.mkdir(parents=True, exist_ok=True)
                if pre_db.exists():
                    undo.append(lambda: _put_back_db(pre_db))
                replace_retry(new_db, DB_PATH)
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
                db.init()
                for k, v in keep.items():
                    if v is not None or k in AUTH_LOCAL:
                        settings.raw_set(k, v)
                settings.raw_set("onboarded", "1")
                for sid, until in revoked:
                    db.execute("INSERT INTO revoked_sessions (sid, until) VALUES (?, ?) "
                               "ON CONFLICT(sid) DO UPDATE SET until = excluded.until", sid, until)
                settings.migrate()      # la sauvegarde peut venir d'avant l'abandon des serveurs anisette Android
                # Apps importées dont l'IPA n'est pas dans la sauvegarde : inutilisables, on le dit tout de suite
                # (sinon l'échec n'apparaît qu'à la fenêtre de renouvellement, des jours plus tard).
                missing = []
                for a in db.rows("SELECT id, name FROM apps WHERE source_type = 'upload' AND status != 'adding'"):
                    if not (IPA_DIR / str(a["id"]) / "original.ipa").exists():
                        missing.append(a["name"])
                        db.update("apps", a["id"], status="error", next_attempt_at=None,
                                  last_error="IPA d'origine absente : réimporte l'app")
                # l'IPA précédente (original.prev.ipa) n'est jamais sauvegardée : plus de retour arrière possible
                db.execute("UPDATE apps SET prev_tag = NULL, prev_key = NULL, prev_version = NULL, prev_sha256 = NULL")
            except Exception as e:  # noqa: BLE001 - on annule ce qui a été remplacé, quelle que soit l'erreur
                for step in reversed(undo):
                    try:
                        step()
                    except Exception as ue:  # noqa: BLE001
                        print(f"[error] annulation de la restauration : {ue}", flush=True)
                raise BackupError("restore_failed",
                                  f"restauration annulée, rien n'a changé : {e}") from e
        finally:
            shutil.rmtree(work, ignore_errors=True)

    prune_old()
    counts = {
        "apps": len(db.rows("SELECT id FROM apps")),
        "devices": len(db.rows("SELECT id FROM devices")),
        "created": manifest.get("created"),
        "tvsign_version": manifest.get("tvsign_version"),
        "missing_uploads": missing,
    }
    db.log(f"Sauvegarde restaurée ({counts['apps']} app(s), {counts['devices']} Apple TV)")
    return counts


def _put_back_db(snapshot: Path) -> None:
    """Annulation : remet la base d'avant la restauration (copie faite par l'API de sauvegarde SQLite)."""
    tmp = snapshot.with_name(snapshot.name + ".undo")
    shutil.copy2(snapshot, tmp)
    replace_retry(tmp, DB_PATH)
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
