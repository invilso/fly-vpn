"""Tests for flyexit/db.py — Migration class, migration engine, and legacy imports."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from flyexit import db
from flyexit.db import MIGRATIONS, Migration, _apply_migrations

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Migration dataclass
# ---------------------------------------------------------------------------


def test_migration_applies_sql():
    conn = sqlite3.connect(":memory:")
    Migration(name="t", sql="CREATE TABLE t (id INTEGER PRIMARY KEY);").apply(conn)
    conn.execute("INSERT INTO t DEFAULT VALUES")  # raises if table absent


def test_migration_on_run_is_called_with_connection():
    conn = sqlite3.connect(":memory:")
    received = []
    Migration(name="t", sql="UNUSED", on_run=received.append).apply(conn)
    assert received == [conn]


def test_migration_on_run_takes_priority_over_sql():
    conn = sqlite3.connect(":memory:")
    ran = []
    Migration(
        name="t",
        sql="THIS IS NOT VALID SQL AND WOULD FAIL;",
        on_run=lambda _: ran.append(True),
    ).apply(conn)
    assert ran == [True]


def test_migration_subclass_can_override_apply():
    class Custom(Migration):
        def apply(self, conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE custom (x INTEGER);")

    conn = sqlite3.connect(":memory:")
    Custom(name="custom").apply(conn)
    conn.execute("INSERT INTO custom VALUES (1)")


# ---------------------------------------------------------------------------
# _apply_migrations
# ---------------------------------------------------------------------------


def _mem_migrations(*sqls: str) -> tuple[sqlite3.Connection, tuple[Migration, ...]]:
    conn = sqlite3.connect(":memory:")
    ms = tuple(Migration(name=f"{i:03d}_m", sql=s) for i, s in enumerate(sqls, 1))
    return conn, ms


def test_apply_migrations_fresh_db():
    conn, ms = _mem_migrations(
        "CREATE TABLE a (id INTEGER PRIMARY KEY);",
        "CREATE TABLE b (id INTEGER PRIMARY KEY);",
    )
    with patch.object(db, "MIGRATIONS", ms):
        _apply_migrations(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.execute("SELECT 1 FROM a")
    conn.execute("SELECT 1 FROM b")


def test_apply_migrations_skips_already_applied():
    conn, ms = _mem_migrations(
        "CREATE TABLE a (id INTEGER PRIMARY KEY);",
        "CREATE TABLE b (id INTEGER PRIMARY KEY);",
    )
    conn.execute("CREATE TABLE a (id INTEGER PRIMARY KEY);")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    with patch.object(db, "MIGRATIONS", ms):
        _apply_migrations(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.execute("SELECT 1 FROM b")


def test_apply_migrations_already_at_latest():
    conn, ms = _mem_migrations("CREATE TABLE a (id INTEGER PRIMARY KEY);")
    conn.execute("CREATE TABLE a (id INTEGER PRIMARY KEY);")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    with patch.object(db, "MIGRATIONS", ms):
        _apply_migrations(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_apply_migrations_failure_names_migration():
    conn = sqlite3.connect(":memory:")
    bad = Migration(name="007_broken", sql="NOT VALID SQL !!!;")
    with (
        patch.object(db, "MIGRATIONS", (bad,)),
        pytest.raises(RuntimeError, match="007_broken"),
    ):
        _apply_migrations(conn)


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


def test_connect_creates_settings_and_sessions(tmp_path: Path):
    with patch.object(db, "DB_PATH", tmp_path / "test.db"):
        conn = db.connect()
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"settings", "sessions"} <= tables


def test_connect_sets_user_version_to_migration_count(tmp_path: Path):
    with patch.object(db, "DB_PATH", tmp_path / "test.db"):
        conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_connect_idempotent(tmp_path: Path):
    with patch.object(db, "DB_PATH", tmp_path / "test.db"):
        db.connect()
        db.connect()  # must not raise


# ---------------------------------------------------------------------------
# _import_legacy_usage_db
# ---------------------------------------------------------------------------


def _make_legacy_usage_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at REAL, ended_at REAL,
            region TEXT, duration_s INTEGER,
            cost_usd REAL, memory_mb INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO sessions"
        " (started_at, ended_at, region, duration_s, cost_usd, memory_mb)"
        " VALUES (1000.0, 1060.0, 'ams', 60, 0.0001, 512)"
    )
    conn.commit()
    conn.close()


def _sessions_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at REAL, ended_at REAL,
            region TEXT, duration_s INTEGER,
            cost_usd REAL, memory_mb INTEGER
        )
        """
    )
    return conn


def test_import_legacy_usage_db_noop_when_missing(tmp_path: Path):
    conn = _sessions_conn()
    with patch.object(db, "_LEGACY_USAGE_DB", tmp_path / "nope.db"):
        db._import_legacy_usage_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_import_legacy_usage_db_copies_rows(tmp_path: Path):
    legacy = tmp_path / "legacy.db"
    _make_legacy_usage_db(legacy)
    conn = _sessions_conn()
    with patch.object(db, "_LEGACY_USAGE_DB", legacy):
        db._import_legacy_usage_db(conn)
    rows = conn.execute("SELECT region, memory_mb FROM sessions").fetchall()
    assert rows == [("ams", 512)]


def test_import_legacy_usage_db_archives_old_file(tmp_path: Path):
    legacy = tmp_path / "legacy.db"
    _make_legacy_usage_db(legacy)
    conn = _sessions_conn()
    with patch.object(db, "_LEGACY_USAGE_DB", legacy):
        db._import_legacy_usage_db(conn)
    assert not legacy.exists()
    assert (tmp_path / "legacy.db.bak").exists()


# ---------------------------------------------------------------------------
# _import_legacy_json_config
# ---------------------------------------------------------------------------


def _settings_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE settings (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            key   TEXT    NOT NULL UNIQUE,
            value TEXT    NOT NULL
        )
        """
    )
    return conn


def test_import_legacy_json_config_noop_when_missing(tmp_path: Path):
    conn = _settings_conn()
    with patch.object(db, "_LEGACY_JSON_CONFIG", tmp_path / "nope.json"):
        db._import_legacy_json_config(conn)
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


def test_import_legacy_json_config_inserts_all_keys(tmp_path: Path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"region": "ams", "org": "personal"}), encoding="utf-8")
    conn = _settings_conn()
    with patch.object(db, "_LEGACY_JSON_CONFIG", cfg):
        db._import_legacy_json_config(conn)
    rows = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM settings")}
    assert rows == {"region": "ams", "org": "personal"}


def test_import_legacy_json_config_does_not_overwrite_existing(tmp_path: Path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"region": "ams"}), encoding="utf-8")
    conn = _settings_conn()
    conn.execute("INSERT INTO settings(key, value) VALUES ('region', 'lax')")
    conn.commit()
    with patch.object(db, "_LEGACY_JSON_CONFIG", cfg):
        db._import_legacy_json_config(conn)
    value = conn.execute("SELECT value FROM settings WHERE key='region'").fetchone()[0]
    assert value == "lax"


def test_import_legacy_json_config_archives_file(tmp_path: Path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"region": "ams"}), encoding="utf-8")
    conn = _settings_conn()
    with patch.object(db, "_LEGACY_JSON_CONFIG", cfg):
        db._import_legacy_json_config(conn)
    assert not cfg.exists()
    assert (tmp_path / "config.json.bak").exists()
