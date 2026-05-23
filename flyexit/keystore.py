"""Settings and credentials — thin wrapper around the shared SQLite store.

All connection / schema logic lives in ``flyexit.db``.  This module only
owns the settings-table CRUD.
"""

from __future__ import annotations

from flyexit.db import DB_PATH, connect

# Re-export so external callers (fly_api.resolve_token) can read DB_PATH
# without importing flyexit.db directly.
__all__ = ["DB_PATH", "get", "set"]


def get(key: str, default: str = "") -> str:
    """Return the stored value for *key*, or *default* if absent."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else default


def set(key: str, value: str) -> None:  # noqa: A001
    """Upsert *key* → *value*."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


