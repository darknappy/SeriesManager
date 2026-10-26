"""Base de datos SQLite del catálogo."""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .utils import json_dumps, now_iso

SCHEMA_VERSION = 4

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    original_title TEXT,
    year INTEGER,
    provider TEXT,
    provider_id TEXT,
    tvmaze_id INTEGER,
    tvmaze_season INTEGER,
    anilist_id INTEGER,
    tmdb_id TEXT,
    imdb_id TEXT,
    tvdb_id TEXT,
    aliases TEXT,
    status TEXT DEFAULT 'unknown',
    media_type TEXT DEFAULT 'tv',
    overview TEXT,
    cover_url TEXT,
    banner_url TEXT,
    genres TEXT,
    studio TEXT,
    total_episodes INTEGER,
    season_count INTEGER,
    is_airing INTEGER DEFAULT 0,
    next_episode INTEGER,
    next_air_at TEXT,
    last_aired TEXT,
    last_synced TEXT,
    match_status TEXT DEFAULT 'unmatched',
    match_confidence REAL DEFAULT 0,
    match_reason TEXT,
    root TEXT DEFAULT 'library',
    root_override INTEGER DEFAULT 0,
    season_layout TEXT,
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_series_title ON series(title);
CREATE INDEX IF NOT EXISTS idx_series_root ON series(root);
CREATE INDEX IF NOT EXISTS idx_series_match ON series(match_status);

CREATE TABLE IF NOT EXISTS source_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_name TEXT NOT NULL UNIQUE,
    folder_path TEXT,
    series_key TEXT NOT NULL,
    season INTEGER,
    absolute INTEGER DEFAULT 0,
    ignore_filename_season INTEGER DEFAULT 0,
    position INTEGER DEFAULT 0,
    root TEXT DEFAULT 'library',
    notes TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_groups_series ON source_groups(series_key);
CREATE INDEX IF NOT EXISTS idx_groups_path ON source_groups(folder_path);

CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    title TEXT,
    total_episodes INTEGER,
    aired_episodes INTEGER,
    status TEXT,
    is_airing INTEGER DEFAULT 0,
    next_episode INTEGER,
    next_air_at TEXT,
    source TEXT,
    updated_at TEXT,
    UNIQUE(series_id, number)
);
CREATE INDEX IF NOT EXISTS idx_seasons_series ON seasons(series_id);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    season_id INTEGER REFERENCES seasons(id) ON DELETE SET NULL,
    season_number INTEGER NOT NULL,
    episode_number REAL NOT NULL,
    episode_end REAL NOT NULL DEFAULT 0,
    absolute_number REAL,
    title TEXT,
    air_date TEXT,
    overview TEXT,
    runtime INTEGER,
    image_url TEXT,
    status TEXT DEFAULT 'missing',
    is_special INTEGER DEFAULT 0,
    source TEXT,
    provider_id TEXT,
    sequence INTEGER,
    updated_at TEXT,
    UNIQUE(series_id, season_number, episode_number, episode_end)
);
CREATE INDEX IF NOT EXISTS idx_episodes_series ON episodes(series_id);
CREATE INDEX IF NOT EXISTS idx_episodes_season ON episodes(series_id, season_number);
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    rel_path TEXT,
    root TEXT DEFAULT 'library',
    group_folder TEXT,
    group_position INTEGER DEFAULT 0,
    kind TEXT NOT NULL,
    role TEXT,
    size INTEGER,
    mtime REAL,
    resolution TEXT,
    quality TEXT,
    title_hint TEXT,
    series_id INTEGER REFERENCES series(id) ON DELETE CASCADE,
    season_number INTEGER,
    episode_number REAL,
    episode_end REAL,
    parsed_season REAL,
    parsed_episode REAL,
    parsed_special INTEGER DEFAULT 0,
    parsed_unnumbered INTEGER DEFAULT 0,
    parsed_from TEXT,
    absolute_number REAL,
    absolute_end REAL,
    fingerprint TEXT,
    episode_id INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
    target_path TEXT,
    status TEXT DEFAULT 'present',
    reason TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_series ON files(series_id);
CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind);
CREATE INDEX IF NOT EXISTS idx_files_role ON files(role);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_episode ON files(episode_id);
CREATE INDEX IF NOT EXISTS idx_files_fingerprint ON files(fingerprint);

CREATE TABLE IF NOT EXISTS match_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    title TEXT,
    year INTEGER,
    media_type TEXT,
    score REAL,
    payload TEXT,
    selected INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_series ON match_candidates(series_id);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    status TEXT DEFAULT 'draft',
    mode TEXT DEFAULT 'full',
    summary TEXT,
    config_snapshot TEXT,
    applied_at TEXT,
    undone_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);

CREATE TABLE IF NOT EXISTS plan_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    action TEXT NOT NULL,
    source TEXT,
    target TEXT,
    series_id INTEGER,
    file_id INTEGER,
    episode_id INTEGER,
    kind TEXT,
    size INTEGER,
    status TEXT DEFAULT 'pending',
    error TEXT,
    reason TEXT,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_plan ON plan_actions(plan_id, seq);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER,
    applied_at TEXT,
    action TEXT,
    source TEXT,
    target TEXT,
    status TEXT,
    error TEXT,
    undo_status TEXT DEFAULT 'pending',
    undone_at TEXT,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_plan ON journal(plan_id);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    severity TEXT DEFAULT 'warning',
    code TEXT NOT NULL,
    series_id INTEGER,
    file_id INTEGER,
    episode_id INTEGER,
    message TEXT NOT NULL,
    details TEXT,
    resolved INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_code ON issues(code);
CREATE INDEX IF NOT EXISTS idx_issues_series ON issues(series_id);
CREATE INDEX IF NOT EXISTS idx_issues_file ON issues(file_id);

CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    title TEXT,
    year INTEGER,
    status TEXT,
    next_episode INTEGER,
    next_air_at TEXT,
    last_aired TEXT,
    total_episodes INTEGER,
    cover_url TEXT,
    banner_url TEXT,
    genres TEXT,
    notes TEXT,
    watch_status TEXT DEFAULT 'Viendo',
    progress_episode REAL,
    rating INTEGER,
    last_watched_at TEXT,
    added_at TEXT,
    last_synced TEXT,
    updated_at TEXT,
    UNIQUE(provider, provider_id)
);

CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    fetched_at TEXT,
    ttl INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT DEFAULT 'queued',
    progress INTEGER DEFAULT 0,
    message TEXT,
    result TEXT,
    error TEXT,
    created_at TEXT,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS jellyfin_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    series_title TEXT,
    season_number INTEGER,
    episode_number REAL,
    episode_title TEXT,
    played INTEGER DEFAULT 0,
    position_ticks INTEGER DEFAULT 0,
    backed_up_at TEXT,
    restored_at TEXT,
    UNIQUE(user_id, item_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

JOBS_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT DEFAULT 'queued',
    progress INTEGER DEFAULT 0,
    message TEXT,
    result TEXT,
    error TEXT,
    created_at TEXT,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


@contextlib.contextmanager
def session(path: str | Path) -> Iterator[sqlite3.Connection]:
    con = connect(path)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _migrate_watchlist(con: sqlite3.Connection) -> None:
    """Añade los campos de seguimiento personal a bases de datos antiguas."""
    columns = {
        row["name"]
        for row in con.execute("PRAGMA table_info(watchlist)").fetchall()
    }
    migrations = {
        "watch_status": "ALTER TABLE watchlist ADD COLUMN watch_status TEXT DEFAULT 'Viendo'",
        "progress_episode": "ALTER TABLE watchlist ADD COLUMN progress_episode REAL",
        "rating": "ALTER TABLE watchlist ADD COLUMN rating INTEGER",
        "last_watched_at": "ALTER TABLE watchlist ADD COLUMN last_watched_at TEXT",
        "updated_at": "ALTER TABLE watchlist ADD COLUMN updated_at TEXT",
    }
    for name, statement in migrations.items():
        if name not in columns:
            con.execute(statement)
    con.execute(
        "UPDATE watchlist SET watch_status='Viendo' "
        "WHERE watch_status IS NULL OR watch_status=''"
    )


def _migrate_files(con: sqlite3.Connection) -> None:
    """Añade el fin absoluto de los archivos multi-episodio a bases antiguas."""
    columns = {
        row["name"]
        for row in con.execute("PRAGMA table_info(files)").fetchall()
    }
    if "absolute_end" not in columns:
        con.execute("ALTER TABLE files ADD COLUMN absolute_end REAL")


def init_db(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with session(path) as con:
        con.executescript(SCHEMA)
        _migrate_watchlist(con)
        _migrate_files(con)
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def init_jobs_db(path: str | Path) -> None:
    """Base separada para el progreso de trabajos (evita bloqueos de escritura)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with session(path) as con:
        con.executescript(JOBS_SCHEMA)


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value) if value is not None else ""),
    )


def add_issue(
    con: sqlite3.Connection,
    code: str,
    message: str,
    severity: str = "warning",
    series_id: int | None = None,
    file_id: int | None = None,
    episode_id: int | None = None,
    details: Any = None,
) -> None:
    con.execute(
        "INSERT INTO issues(severity, code, series_id, file_id, episode_id, message, details, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            severity,
            code,
            series_id,
            file_id,
            episode_id,
            message,
            json_dumps(details) if details is not None else None,
            now_iso(),
        ),
    )


def clear_issues(con: sqlite3.Connection, code: str | None = None) -> None:
    if code:
        con.execute("DELETE FROM issues WHERE code=?", (code,))
    else:
        con.execute("DELETE FROM issues")


def get_or_create_series(
    con: sqlite3.Connection,
    series_key: str,
    defaults: dict[str, Any] | None = None,
) -> int:
    row = con.execute("SELECT id FROM series WHERE series_key=?", (series_key,)).fetchone()
    if row:
        return int(row["id"])
    data = dict(defaults or {})
    data.setdefault("series_key", series_key)
    data.setdefault("title", series_key.replace("-", " ").title())
    data.setdefault("created_at", now_iso())
    data.setdefault("updated_at", now_iso())
    columns = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    con.execute(f"INSERT INTO series({columns}) VALUES({placeholders})", tuple(data.values()))
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def upsert_episode(
    con: sqlite3.Connection,
    series_id: int,
    season_number: int,
    episode_number: float,
    episode_end: float = 0,
    **values: Any,
) -> int:
    episode_end = episode_end or 0
    row = con.execute(
        "SELECT id FROM episodes WHERE series_id=? AND season_number=? AND episode_number=? AND episode_end=?",
        (series_id, season_number, float(episode_number), float(episode_end)),
    ).fetchone()
    if row:
        if values:
            assignments = ", ".join(f"{key}=?" for key in values)
            con.execute(
                f"UPDATE episodes SET {assignments}, updated_at=? WHERE id=?",
                (*values.values(), now_iso(), int(row["id"])),
            )
        return int(row["id"])
    data = {
        "series_id": series_id,
        "season_number": int(season_number),
        "episode_number": float(episode_number),
        "episode_end": float(episode_end),
        "updated_at": now_iso(),
        **values,
    }
    columns = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    con.execute(f"INSERT INTO episodes({columns}) VALUES({placeholders})", tuple(data.values()))
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
