"""Fly.io app & machine management — delegates to the Machines HTTP API."""

from __future__ import annotations

import contextlib
import subprocess
import time
from enum import Enum, auto

# Timeout (seconds) to wait for graceful subprocess shutdown before force-killing.
GRACEFUL_TIMEOUT = 5


class AppStatus(Enum):
    """Result of :func:`ensure_app_exists`."""

    CREATED = auto()
    FAILED = auto()


class AuthStatus(Enum):
    """Result of :func:`check_auth`."""

    OK = auto()
    NOT_AUTHENTICATED = auto()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def check_auth() -> tuple[AuthStatus, str]:
    """Verify Fly.io authentication via the Machines API.

    Returns ``(status, info)``.  On ``OK`` *info* is the username;
    on ``NOT_AUTHENTICATED`` it is a human-readable error description.
    """
    from flyexit.fly_api import get_client

    client = get_client()
    if client is None:
        return (
            AuthStatus.NOT_AUTHENTICATED,
            "No Fly.io API token found."
            " Press [bold]c[/] to open Settings and add one.",
        )
    ok, username = client.check_auth()
    client.close()
    if not ok:
        return (
            AuthStatus.NOT_AUTHENTICATED,
            "Fly.io API token is invalid or expired."
            " Press [bold]c[/] to open Settings.",
        )
    return AuthStatus.OK, username


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


def app_exists(app_name: str) -> bool:
    """Return True if the Fly app exists."""
    from flyexit.fly_api import get_client

    client = get_client()
    if client is None:
        return False
    exists = client.app_exists(app_name)
    client.close()
    return exists


def ensure_app_exists(app_name: str, org: str) -> tuple[AppStatus, str]:
    """Destroy any leftover app then create a fresh one.

    Returns ``(status, error)``.  On ``CREATED`` the error string is empty;
    on ``FAILED`` it contains the API error message.

    Cleanup is unconditional — also handles apps invisible to a simple
    status check (reserved names without running machines).  The retry loop
    absorbs Fly's post-deletion name-propagation lag.
    """
    from flyexit.fly_api import get_client

    client = get_client()
    if client is None:
        return (
            AppStatus.FAILED,
            "No Fly.io API token found."
            " Press [bold]c[/] to open Settings and add one.",
        )

    # Unconditional cleanup — no-op when the app is absent; also releases
    # names that are reserved without a visible app.
    with contextlib.suppress(Exception):
        client.delete_app(app_name)

    # Retry create loop: absorbs post-deletion propagation lag.
    # Only "already taken" is transient — other errors fail immediately.
    last_err = ""
    for _ in range(10):
        ok, err = client.create_app(app_name, org)
        if ok:
            client.close()
            return AppStatus.CREATED, ""
        last_err = err
        if "already" not in last_err.lower() and "taken" not in last_err.lower():
            client.close()
            return AppStatus.FAILED, last_err
        time.sleep(1)

    client.close()
    return AppStatus.FAILED, last_err


def destroy_app(app_name: str) -> bool:
    """Delete the Fly app (and force-stop all its machines). Returns True on success."""
    from flyexit.fly_api import get_client

    client = get_client()
    if client is None:
        return False
    ok = client.delete_app(app_name)
    client.close()
    return ok


def cleanup_app_sync(app_name: str) -> None:
    """Last-resort cleanup — called by atexit / signal handler outside Textual."""
    with contextlib.suppress(Exception):
        from flyexit.fly_api import get_client

        client = get_client()
        if client is not None:
            client.delete_app(app_name)
            client.close()


# ---------------------------------------------------------------------------
# Process helpers (still used for subprocess.Popen cleanup paths)
# ---------------------------------------------------------------------------


def force_kill_process(proc: subprocess.Popen[str] | None) -> None:
    """Terminate and, if needed, force-kill a subprocess."""
    if proc is None:
        return
    with contextlib.suppress(Exception):
        proc.terminate()
        try:
            proc.wait(timeout=GRACEFUL_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
