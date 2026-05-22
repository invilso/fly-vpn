"""Fly.io app & machine management helpers."""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from enum import Enum, auto

from flyexit.constants import FLY_ENV

# Timeout (seconds) to wait for graceful subprocess shutdown before force-killing.
GRACEFUL_TIMEOUT = 5

# Fly machine states that should be killed during cleanup.
_KILLABLE_STATES: frozenset[str] = frozenset({"started", "starting", "replacing"})


class AppStatus(Enum):
    """Result of :func:`ensure_app_exists`."""

    CREATED = auto()
    FAILED = auto()


class AuthStatus(Enum):
    """Result of :func:`check_auth`."""

    OK = auto()
    NOT_AUTHENTICATED = auto()


def cleanup_app_sync(app_name: str) -> None:
    """Last-resort cleanup — called by atexit / signal handler outside Textual."""
    with contextlib.suppress(Exception):
        subprocess.run(
            ["fly", "apps", "destroy", app_name, "--yes"],
            capture_output=True,
            timeout=15,
            env=FLY_ENV,
        )


def app_exists(app_name: str) -> bool:
    """Return True if the Fly app exists."""
    result = subprocess.run(
        ["fly", "status", "--app", app_name],
        capture_output=True,
        text=True,
        env=FLY_ENV,
    )
    return result.returncode == 0


def ensure_app_exists(app_name: str, org: str) -> tuple[AppStatus, str]:
    """Destroy any leftover app, then create a fresh one.

    Returns ``(status, error)``.  On ``CREATED`` the error string is
    empty; on ``FAILED`` it contains stderr.

    Always starts from a clean slate — if the app exists (e.g. from a
    crashed session), it is destroyed first so there are no stale
    machines or configs.
    """
    if app_exists(app_name):
        kill_all_machines(app_name)
        destroy_app(app_name)

    create = subprocess.run(
        ["fly", "apps", "create", app_name, "--org", org],
        capture_output=True,
        text=True,
        env=FLY_ENV,
    )
    if create.returncode != 0:
        return AppStatus.FAILED, create.stderr.strip()

    # Fly's internal DB may lag behind `apps create` returning 0.
    # Poll until `fly status` sees the app (up to ~5 s).
    for _ in range(5):
        if app_exists(app_name):
            return AppStatus.CREATED, ""
        time.sleep(1)

    return AppStatus.CREATED, ""


def destroy_app(app_name: str) -> bool:
    """Delete the Fly app. Returns True on success."""
    result = subprocess.run(
        ["fly", "apps", "destroy", app_name, "--yes"],
        capture_output=True,
        text=True,
        timeout=30,
        env=FLY_ENV,
    )
    return result.returncode == 0


MACHINE_NAME = "ephemeral-exit-node"


def kill_all_machines(app_name: str) -> int:
    """Force-stop any running machines in the Fly app.

    Returns the number of machines successfully killed.
    """
    ls = subprocess.run(
        ["fly", "machines", "list", "--app", app_name, "--json"],
        capture_output=True,
        text=True,
        timeout=15,
        env=FLY_ENV,
    )
    if ls.returncode != 0:
        return 0

    try:
        machines = json.loads(ls.stdout)
    except (json.JSONDecodeError, TypeError):  # fmt: skip
        return 0

    killed = 0
    for m in machines:
        mid = m.get("id")
        state = m.get("state", "")
        if mid and state in _KILLABLE_STATES:
            r = subprocess.run(
                ["fly", "machines", "kill", mid, "--app", app_name],
                capture_output=True,
                timeout=15,
                env=FLY_ENV,
            )
            if r.returncode == 0:
                killed += 1
    return killed


def kill_machine_by_name(
    app_name: str,
    name: str = MACHINE_NAME,
) -> bool:
    """Find the machine we created by *name* and force-kill it."""
    ls = subprocess.run(
        ["fly", "machines", "list", "--app", app_name, "--json"],
        capture_output=True,
        text=True,
        timeout=15,
        env=FLY_ENV,
    )
    if ls.returncode != 0:
        return False

    try:
        machines = json.loads(ls.stdout)
    except (json.JSONDecodeError, TypeError):  # fmt: skip
        return False

    for m in machines:
        if m.get("name") == name and m.get("state", "") in _KILLABLE_STATES:
            mid = m["id"]
            r = subprocess.run(
                ["fly", "machines", "kill", mid, "--app", app_name],
                capture_output=True,
                timeout=15,
                env=FLY_ENV,
            )
            return r.returncode == 0
    return False


def check_auth() -> tuple[AuthStatus, str]:
    """Verify fly CLI authentication.

    Returns ``(status, info)``.  On ``OK`` *info* is the username;
    on ``NOT_AUTHENTICATED`` it is a human-readable error description.
    """
    result = subprocess.run(
        ["fly", "auth", "whoami"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return (
            AuthStatus.NOT_AUTHENTICATED,
            "Fly.io not authenticated! Run 'fly auth login' first.",
        )
    return AuthStatus.OK, result.stdout.strip()


def build_fly_cmd(
    app_name: str,
    region: str,
    auth_key: str,
    hostname: str,
    *,
    login_server: str = "",
    vm_memory: int = 512,
) -> list[str]:
    """Build the ``fly m run`` command for launching an exit node."""
    extra_args = "--advertise-exit-node --advertise-tags=tag:ephemeral-vpn"
    if login_server:
        extra_args += f" --login-server={login_server}"

    return [
        "fly",
        "m",
        "run",
        "tailscale/tailscale:latest",
        "--app",
        app_name,
        "--region",
        region,
        "--vm-memory",
        f"{vm_memory}mb",
        "--name",
        "ephemeral-exit-node",
        "-e",
        f"TS_AUTHKEY={auth_key}",
        "-e",
        f"TS_EXTRA_ARGS={extra_args}",
        "-e",
        f"TS_HOSTNAME={hostname}",
    ]


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
