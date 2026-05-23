"""Persistent configuration helpers — backed by the SQLite keystore."""

from __future__ import annotations

from typing import Any

from flyexit import keystore
from flyexit.constants import (
    DEFAULT_APP_NAME,
    DEFAULT_ORG,
    DEFAULT_REGION,
    DEFAULT_VM_MEMORY,
)

_DEFAULTS: dict[str, Any] = {
    "region": DEFAULT_REGION,
    "app_name": DEFAULT_APP_NAME,
    "org": DEFAULT_ORG,
    "vm_memory": DEFAULT_VM_MEMORY,
}


def load() -> dict[str, Any]:
    """Read config from the keystore, falling back to built-in defaults."""
    result: dict[str, Any] = {}
    for key, default in _DEFAULTS.items():
        raw = keystore.get(key)
        if raw:
            result[key] = int(raw) if isinstance(default, int) else raw
        else:
            result[key] = default
    return result


def save(config: dict[str, Any]) -> None:
    """Persist *config* to the keystore."""
    for key, value in config.items():
        keystore.set(key, str(value))
