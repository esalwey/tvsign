"""SQLite storage: devices, apps, log, settings. Apple accounts stay in the plumesign store."""
import sqlite3
import time
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    pair_identifier TEXT NOT NULL UNIQUE,   -- clé d'appairage : UDID plumesign (ancien : identifiant pymobiledevice3)
    udid            TEXT,
    model           TEXT,
    os_version      TEXT,
    last_ip         TEXT,
    last_port       INTEGER,
    last_seen       REAL,
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS apps (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    bundle_id       TEXT,
    version         TEXT,
    source_type     TEXT NOT NULL,          -- upload | github | url
    source          TEXT,                   -- dépôt owner/name ou URL
    source_filter   TEXT,                   -- préfixe de tag (github)
    source_tag      TEXT,                   -- version de la source actuellement téléchargée
    account_email   TEXT,
    device_id       INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'pending',
    last_error      TEXT,
    signed_at       REAL,
    expires_at      REAL,
    next_attempt_at REAL,
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account_devices (
    email TEXT NOT NULL,
    udid  TEXT NOT NULL,
    PRIMARY KEY (email, udid)
);
CREATE TABLE IF NOT EXISTS account_status (
    email      TEXT PRIMARY KEY,
    ok         INTEGER,
    message    TEXT,
    checked_at REAL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    level   TEXT NOT NULL,
    app_id  INTEGER,
    message TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Colonnes ajoutées après coup : (table, colonne, type). Ajoutées si absentes.
MIGRATIONS = [
    ("apps", "notified_level", "TEXT"),      # dernier niveau d'alerte envoyé (fail, 24h, 6h, expired)
    ("apps", "bad_tag", "TEXT"),             # release refusée (IPA invalide) : ne pas la retélécharger
    ("apps", "cert_serial", "TEXT"),         # certificat de la dernière signature
    ("apps", "last_attempt_at", "REAL"),
    ("apps", "last_trigger", "TEXT"),        # auto | manuel | nouvelle version | ...
    ("devices", "last_error", "TEXT"),       # dernier échec de connexion (tunnel)
    ("devices", "last_error_at", "REAL"),
    ("devices", "pairing_lost", "INTEGER NOT NULL DEFAULT 0"),
    ("devices", "pairing_udid", "TEXT"),     # UDID du store d'appairage plumesign (cible de pair -i)
    ("devices", "pair_host", "TEXT"),        # nom d'hôte présenté à la TV lors de l'appairage
    ("devices", "legacy_pairing", "INTEGER NOT NULL DEFAULT 0"),   # appairage pymobiledevice3 : réappairer
    # canal des releases GitHub ; « beta » pour les apps existantes (comportement d'avant : prereleases incluses)
    ("apps", "channel", "TEXT NOT NULL DEFAULT 'beta'"),
    ("apps", "preset", "TEXT"),              # identifiant du préréglage (presets.json)
    ("apps", "asset_name", "TEXT"),          # nom exact de l'asset IPA (préréglage)
    ("apps", "source_sha256", "TEXT"),       # empreinte vérifiée de l'IPA téléchargée
]


def _pending(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    todo = []
    for table, col, kind in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            todo.append((table, col, kind))
    return todo


def snapshot(dest) -> None:
    """Consistent copy of the database (SQLite backup API, safe while in use)."""
    src = sqlite3.connect(DB_PATH, timeout=30)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def init() -> None:
    existed = DB_PATH.exists() and DB_PATH.stat().st_size > 0
    with tx() as conn:
        if existed and _pending(conn):
            # copie de sécurité avant de modifier le schéma (retour arrière possible)
            snapshot(DB_PATH.with_name(f"tvsign.db.pre-migrate-{int(time.time())}"))
        conn.executescript(SCHEMA)
        for table, col, kind in _pending(conn):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")


def rows(sql: str, *args) -> list[dict]:
    with tx() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def row(sql: str, *args) -> dict | None:
    found = rows(sql, *args)
    return found[0] if found else None


def execute(sql: str, *args) -> int:
    with tx() as conn:
        return conn.execute(sql, args).lastrowid


def update(table: str, key: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE {table} SET {cols} WHERE id = ?", *fields.values(), key)


def log(message: str, level: str = "info", app_id: int | None = None) -> None:
    execute("INSERT INTO events (ts, level, app_id, message) VALUES (?, ?, ?, ?)",
            time.time(), level, app_id, message)
    execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)")
    print(f"[{level}] {message}", flush=True)
