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
CREATE TABLE IF NOT EXISTS revoked_sessions (
    sid   TEXT PRIMARY KEY,                -- identifiant de session (cookie) déconnecté
    until REAL NOT NULL                    -- au-delà, le cookie a de toute façon expiré
);
CREATE TABLE IF NOT EXISTS renewals (
    id       INTEGER PRIMARY KEY,
    app_id   INTEGER NOT NULL,
    started  REAL NOT NULL,
    finished REAL NOT NULL,
    cause    TEXT,                          -- déclencheur : auto, manuel, nouvelle version…
    outcome  TEXT NOT NULL,                 -- ok | error | skipped
    error    TEXT
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
    ("apps", "deferred_reason", "TEXT"),     # ancien report « app ouverte » (plus utilisé, colonne conservée)
    ("apps", "deferred_since", "REAL"),      # idem
    ("apps", "source_key", "TEXT"),          # identité du contenu téléchargé (empreinte, ETag…) : tag fixe, IPA remplacée
    ("apps", "bad_key", "TEXT"),             # identité de la release refusée (avec bad_tag)
    ("apps", "fail_count", "INTEGER NOT NULL DEFAULT 0"),   # échecs d'affilée (délai croissant entre essais)
    ("apps", "needs_action", "TEXT"),        # session | pairing | limit | config : inutile de réessayer avant une action
    ("apps", "installed_version", "TEXT"),   # version réellement installée sur la TV (version = dernière téléchargée)
    ("apps", "installed_tag", "TEXT"),
    ("events", "repeat", "INTEGER NOT NULL DEFAULT 1"),     # même événement répété (échecs d'affilée)
    ("events", "first_ts", "REAL"),          # première occurrence d'un événement répété
    # --- contrôle des mises à jour d'une app ---
    ("apps", "auto_update", "INTEGER NOT NULL DEFAULT 1"),   # 0 : renouveler seulement, ne pas installer les nouvelles versions
    ("apps", "pinned_tag", "TEXT"),          # version GitHub épinglée (seul ce tag est suivi)
    ("apps", "notes", "TEXT"),               # notes de la release installée (GitHub)
    ("apps", "update_tag", "TEXT"),          # version disponible mais non installée (mise à jour auto coupée, tvOS trop ancien…)
    ("apps", "update_notes", "TEXT"),        # ses notes de version
    ("apps", "update_block", "TEXT"),        # pourquoi elle n'est pas installée : manual | tvos:<minimum> | rollback
    ("apps", "update_error", "TEXT"),        # dernier échec d'une mise à jour facultative (l'app installée reste valable)
    ("apps", "installed_ipa", "TEXT"),       # empreinte SHA-256 de l'IPA réellement installée sur la TV
    ("apps", "prev_tag", "TEXT"),            # version précédente gardée (original.prev.ipa) pour le retour arrière
    ("apps", "prev_key", "TEXT"),
    ("apps", "prev_version", "TEXT"),
    ("apps", "prev_sha256", "TEXT"),
    ("apps", "fail_kind", "TEXT"),           # nature du dernier échec (le compteur d'échecs repart de 1 quand elle change)
    ("apps", "verified_at", "REAL"),         # dernière fois où l'app a été vue installée sur l'Apple TV
    ("apps", "installed_bundle", "TEXT"),    # bundle ID installé (celui de la signature, pas de l'IPA d'origine)
    ("devices", "host_hint", "TEXT"),        # adresse IP ou nom d'hôte saisi à la main (mDNS bloqué)
    ("apps", "bad_reason", "TEXT"),          # pourquoi cette release est refusée (affiché sur la carte)
    ("apps", "on_tv", "INTEGER"),            # app trouvée (1) ou non (0) sur l'Apple TV à la dernière vérification
    ("apps", "on_tv_checked_at", "REAL"),    # date de cette vérification
    # iPhone / iPad : famille de l'appareil (tvos | ios | ipados | visionos), tvos pour l'existant
    ("devices", "family", "TEXT NOT NULL DEFAULT 'tvos'"),
    ("devices", "dev_mode", "INTEGER"),      # mode développeur : 1 activé, 0 désactivé, NULL inconnu
    ("devices", "dev_mode_at", "REAL"),
    ("devices", "renew_before_hours", "REAL"),   # délai de renouvellement propre à l'appareil (sinon réglage)
    ("apps", "platform", "TEXT"),            # DTPlatformName de l'IPA (appletvos, iphoneos…)
    ("apps", "ui_family", "TEXT"),           # UIDeviceFamily de l'IPA, ex. « 1,2 »
    ("apps", "warnings", "TEXT"),            # limites du compte gratuit détectées dans l'IPA (codes séparés par des virgules)
    ("apps", "source_published_at", "TEXT"), # date de publication de la release installée (jamais de retour en arrière)
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


def migrate(conn: sqlite3.Connection) -> None:
    """Crée les tables manquantes et ajoute les colonnes de MIGRATIONS (sans rien supprimer)."""
    conn.executescript(SCHEMA)
    todo = _pending(conn)
    for table, col, kind in todo:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
    if ("apps", "platform", "TEXT") in todo:
        # avant l'arrivée des iPhone/iPad, toute app suivie était une app tvOS
        conn.execute("UPDATE apps SET platform = 'appletvos' WHERE platform IS NULL")


def init() -> None:
    existed = DB_PATH.exists() and DB_PATH.stat().st_size > 0
    with tx() as conn:
        if existed and _pending(conn):
            # copie de sécurité avant de modifier le schéma (retour arrière possible)
            snapshot(DB_PATH.with_name(f"tvsign.db.pre-migrate-{int(time.time())}"))
        migrate(conn)


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


# Conservation du journal : global (sans app) et par app séparément, pour qu'une app qui échoue en
# boucle n'efface ni les événements des autres ni les siens plus anciens ; plafond général en plus.
KEEP_PER_APP = 250
KEEP_TOTAL = 1200


def log(message: str, level: str = "info", app_id: int | None = None, journal: bool = True) -> None:
    """Écrit au journal (et sur la sortie du service). journal=False : sortie seulement, pour le détail
    d'un nouvel essai qui encombrerait le journal sans rien apprendre."""
    if journal:
        _record(message, level, app_id)
    try:
        print(f"[{level}] {message}", flush=True)
    except (UnicodeError, OSError):
        # sortie redirigée en cp1252 (Windows) ou fermée : le journal en base est déjà écrit, et un
        # message qu'on ne peut pas afficher ne doit jamais faire échouer l'opération qu'il décrit
        try:
            print(f"[{level}] " + message.encode("ascii", "replace").decode("ascii"), flush=True)
        except (UnicodeError, OSError):
            pass


def _record(message: str, level: str, app_id: int | None) -> None:
    now = time.time()
    if level != "info":
        # même avertissement/erreur que le dernier problème de cette app (échec répété) : on compte la
        # répétition au lieu d'ajouter une ligne ; la première occurrence reste visible (first_ts)
        last = row("SELECT id, ts, level, message, repeat, first_ts FROM events "
                   "WHERE app_id IS ? AND level != 'info' ORDER BY id DESC LIMIT 1", app_id)
        if last and last["level"] == level and last["message"] == message:
            # seulement si rien de neuf n'a été écrit depuis (succès, autre erreur, début d'un nouvel
            # épisode) : sinon c'est un nouvel épisode. Les nouveaux essais d'un même échec n'écrivent
            # que l'erreur (journal=False pour le reste).
            newer = row("SELECT COUNT(*) AS n FROM events WHERE app_id IS ? AND id > ?", app_id, last["id"])
            if not newer["n"]:
                execute("UPDATE events SET ts = ?, repeat = repeat + 1, first_ts = COALESCE(first_ts, ts) "
                        "WHERE id = ?", now, last["id"])
                return
    execute("INSERT INTO events (ts, level, app_id, message) VALUES (?, ?, ?, ?)", now, level, app_id, message)
    execute("DELETE FROM events WHERE app_id IS ? AND id NOT IN "
            "(SELECT id FROM events WHERE app_id IS ? ORDER BY id DESC LIMIT ?)", app_id, app_id, KEEP_PER_APP)
    execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)", KEEP_TOTAL)
