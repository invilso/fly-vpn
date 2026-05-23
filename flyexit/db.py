"""Unified SQLite connection layer for ~/.fly_vpn.db.

Schema versioning via PRAGMA user_version.  Each migration is a Migration
instance; complex ones can subclass it and override apply().
To add a migration: append to MIGRATIONS — version is derived from position.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

DB_PATH: Path = Path.home() / ".fly_vpn.db"


@dataclass(frozen=True)
class Migration:
    """A single, named, idempotent schema change.

    Priority: on_run (if provided) → sql.
    Subclasses may override apply() entirely for complex multi-step migrations.
    """

    name: str
    sql: str = ""
    on_run: Callable[[sqlite3.Connection], None] | None = field(
        default=None, compare=False
    )

    def apply(self, conn: sqlite3.Connection) -> None:
        if self.on_run is not None:
            self.on_run(conn)
        else:
            conn.executescript(self.sql)


_LEGACY_USAGE_DB: Path = Path.home() / ".fly_vpn_usage.db"


def _import_legacy_usage_db(conn: sqlite3.Connection) -> None:
    """Move sessions from ~/.fly_vpn_usage.db into the unified DB, then archive it."""
    if not _LEGACY_USAGE_DB.exists():
        return
    legacy = sqlite3.connect(_LEGACY_USAGE_DB)
    try:
        rows = legacy.execute(
            "SELECT started_at, ended_at, region, duration_s, cost_usd, memory_mb"
            " FROM sessions"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        legacy.close()

    if rows:
        conn.executemany(
            "INSERT INTO sessions"
            " (started_at, ended_at, region, duration_s, cost_usd, memory_mb)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    with contextlib.suppress(OSError):
        _LEGACY_USAGE_DB.rename(_LEGACY_USAGE_DB.with_suffix(".db.bak"))


_LEGACY_JSON_CONFIG: Path = Path.home() / ".fly_vpn_config.json"


def _import_legacy_json_config(conn: sqlite3.Connection) -> None:
    """Move settings from ~/.fly_vpn_config.json into the unified DB, then archive."""
    if not _LEGACY_JSON_CONFIG.exists():
        return
    with contextlib.suppress(Exception):
        data: dict[str, object] = json.loads(
            _LEGACY_JSON_CONFIG.read_text(encoding="utf-8")
        )
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?)"
            " ON CONFLICT(key) DO NOTHING",
            [(k, str(v)) for k, v in data.items()],
        )
        _LEGACY_JSON_CONFIG.rename(_LEGACY_JSON_CONFIG.with_suffix(".json.bak"))


# Never reorder or edit existing entries — only append.
# user_version tracks how many have been applied.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        name="001_initial_schema",
        sql="""
            CREATE TABLE settings (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                key   TEXT    NOT NULL UNIQUE,
                value TEXT    NOT NULL
            );
            CREATE TABLE sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL    NOT NULL,
                ended_at   REAL,
                region     TEXT    NOT NULL,
                duration_s INTEGER,
                cost_usd   REAL
            );
        """,
    ),
    Migration(
        name="002_sessions_add_memory_mb",
        sql="ALTER TABLE sessions ADD COLUMN memory_mb INTEGER;",
    ),
    Migration(
        name="003_import_legacy_usage_db",
        on_run=_import_legacy_usage_db,
    ),
    Migration(
        name="004_import_legacy_json_config",
        on_run=_import_legacy_json_config,
    ),
)


def connect() -> sqlite3.Connection:
    """Return an open, fully-migrated connection to DB_PATH.

    Creates the file on first call and applies chmod 600 (owner-only read/write)
    because the settings table stores API tokens in plaintext.  The chmod is
    silently skipped on Windows where it has no effect.
    """
    first = not DB_PATH.exists()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    if first:
        with contextlib.suppress(OSError):
            os.chmod(DB_PATH, 0o600)

    _apply_migrations(conn)
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, migration in enumerate(MIGRATIONS, start=1):
        if i <= current:
            continue
        try:
            migration.apply(conn)
        except sqlite3.Error as exc:
            raise RuntimeError(f"Migration {migration.name!r} failed: {exc}") from exc
        conn.execute(f"PRAGMA user_version = {i}")
        conn.commit()
