"""Tailscale exit-node management helpers."""

from __future__ import annotations

import contextlib
import json
import subprocess

from flyexit.constants import TS_CONNECT_TIMEOUT, TS_EXIT_HOSTNAME, TS_POLL_INTERVAL


def check_tailscale() -> bool:
    """Return True if the ``tailscale`` CLI is accessible on this system."""
    try:
        subprocess.run(
            ["tailscale", "version"],
            capture_output=True,
            timeout=5,
        )
        return True
    except FileNotFoundError:
        return False


def disconnect_exit_node() -> None:
    """Tell local Tailscale to stop using the remote exit node."""
    with contextlib.suppress(Exception):
        subprocess.run(
            ["tailscale", "set", "--exit-node="],
            capture_output=True,
            timeout=10,
        )


def connect_exit_node(hostname: str = TS_EXIT_HOSTNAME) -> bool:
    """Tell local Tailscale to route traffic through the exit node."""
    try:
        result = subprocess.run(
            ["tailscale", "set", f"--exit-node={hostname}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def is_exit_node_online(hostname: str = TS_EXIT_HOSTNAME) -> bool:
    """Check if the exit node is visible in the local tailnet."""
    try:
        result = subprocess.run(
            ["tailscale", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == hostname:
            return "offline" not in line
    return False


def wait_for_exit_node(
    hostname: str = TS_EXIT_HOSTNAME,
    timeout: int = TS_CONNECT_TIMEOUT,
    poll_interval: int = TS_POLL_INTERVAL,
) -> bool:
    """Block until the exit node appears in tailnet. Returns True if found."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_exit_node_online(hostname):
            return True
        time.sleep(poll_interval)
    return False


def get_device_id(hostname: str = TS_EXIT_HOSTNAME) -> str | None:
    """Find the Tailscale device ID by hostname via ``tailscale status --json``."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):  # fmt: skip
        return None
    for peer in (data.get("Peer") or {}).values():
        if peer.get("HostName") == hostname:
            return peer.get("ID") or peer.get("PublicKey")
    return None
