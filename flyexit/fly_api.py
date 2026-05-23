"""Fly.io Machines REST API client.

Replaces all ``subprocess(["fly", …])`` calls with direct HTTP requests to
``https://api.machines.dev``.

Token discovery order
---------------------
1. ``keystore.get("fly_api_token")``
2. ``FLY_API_TOKEN`` environment variable
3. ``~/.fly/config.yml`` — the fly CLI's stored token (``access_token`` field)
"""

from __future__ import annotations

import contextlib
import os
import platform
import re
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

_BASE = "https://api.machines.dev"
_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _fly_config_path() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", "")) / "fly" / "config.yml"
    return Path.home() / ".fly" / "config.yml"


def _token_from_fly_config() -> str:
    path = _fly_config_path()
    if not path.exists():
        return ""
    with contextlib.suppress(Exception):
        text = path.read_text(encoding="utf-8")
        m = re.search(r"^access_token:\s*\"?([^\s\"]+)\"?", text, re.MULTILINE)
        if m:
            return m.group(1)
    return ""


def resolve_token() -> str:
    """Return the best available Fly.io API token, or empty string."""
    from flyexit import keystore

    return (
        keystore.get("fly_api_token")
        or os.environ.get("FLY_API_TOKEN", "")
        or _token_from_fly_config()
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class FlyAPIClient:
    """Thin httpx wrapper around the Fly.io Machines REST API."""

    def __init__(self, token: str) -> None:
        self._http = httpx.Client(
            base_url=_BASE,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def check_auth(self) -> tuple[bool, str]:
        """Return ``(ok, username)``.

        Uses ``GET /v1/apps?org_slug=personal`` — the only Machines API
        endpoint that reliably distinguishes valid tokens (200) from invalid
        ones (401).  Plain ``/v1/apps`` returns 404 regardless of token.
        """
        with contextlib.suppress(httpx.HTTPError):
            r = self._http.get("/v1/apps", params={"org_slug": "personal"})
            if r.status_code == 200:
                with contextlib.suppress(Exception):
                    slugs = {
                        app["organization"]["slug"]
                        for app in r.json().get("apps", [])
                        if app.get("organization")
                    }
                    return True, next(iter(slugs), "")
                return True, ""
        return False, ""

    # ------------------------------------------------------------------
    # Apps
    # ------------------------------------------------------------------

    def app_exists(self, app_name: str) -> bool:
        with contextlib.suppress(httpx.HTTPError):
            return self._http.get(f"/v1/apps/{app_name}").status_code == 200
        return False

    def create_app(self, app_name: str, org_slug: str) -> tuple[bool, str]:
        """Return ``(success, error_message)``."""
        with contextlib.suppress(httpx.HTTPError):
            r = self._http.post(
                "/v1/apps",
                json={"app_name": app_name, "org_slug": org_slug},
            )
            if r.status_code in (200, 201):
                return True, ""
            with contextlib.suppress(Exception):
                err = r.json().get("error") or r.text
                return False, err
            return False, r.text
        return False, "HTTP error contacting Fly.io API"

    def delete_app(self, app_name: str) -> bool:
        """Delete the app and force-stop all its machines in one call."""
        with contextlib.suppress(httpx.HTTPError):
            r = self._http.delete(
                f"/v1/apps/{app_name}",
                params={"force": "true"},
                timeout=45.0,
            )
            return r.status_code in (200, 202, 204)
        return False

    # ------------------------------------------------------------------
    # Machines
    # ------------------------------------------------------------------

    def list_machines(self, app_name: str) -> list[dict]:
        with contextlib.suppress(httpx.HTTPError):
            r = self._http.get(f"/v1/apps/{app_name}/machines")
            if r.status_code == 200:
                return r.json()
        return []

    def create_machine(
        self,
        app_name: str,
        region: str,
        auth_key: str,
        hostname: str,
        *,
        login_server: str = "",
        vm_memory: int = 512,
    ) -> tuple[str, str]:
        """Launch a Tailscale exit-node machine.

        Returns ``(machine_id, error)``.  On success *error* is empty;
        on failure *machine_id* is empty and *error* describes the problem.
        """
        extra_args = "--advertise-exit-node --advertise-tags=tag:ephemeral-vpn"
        if login_server:
            extra_args += f" --login-server={login_server}"

        body = {
            "name": "ephemeral-exit-node",
            "region": region,
            "config": {
                "image": "tailscale/tailscale:latest",
                "env": {
                    "TS_AUTHKEY": auth_key,
                    "TS_EXTRA_ARGS": extra_args,
                    "TS_HOSTNAME": hostname,
                },
                "guest": {
                    "cpu_kind": "shared",
                    "cpus": 1,
                    "memory_mb": vm_memory,
                },
            },
        }
        with contextlib.suppress(httpx.HTTPError):
            r = self._http.post(f"/v1/apps/{app_name}/machines", json=body)
            if r.status_code in (200, 201):
                return r.json().get("id", ""), ""
            with contextlib.suppress(Exception):
                err = r.json().get("error") or r.text
                return "", err
            return "", r.text
        return "", "HTTP error contacting Fly.io API"

    def wait_machine_started(
        self,
        app_name: str,
        machine_id: str,
        *,
        timeout: int = 60,
        on_output: Callable[[str], None] | None = None,
    ) -> bool:
        """Block until the machine reaches ``started`` state.

        Uses Fly's dedicated ``/wait`` endpoint — no client-side polling.
        """
        if on_output:
            on_output("[dim]⏳ Waiting for machine to start…[/]")
        with contextlib.suppress(httpx.HTTPError):
            r = self._http.get(
                f"/v1/apps/{app_name}/machines/{machine_id}/wait",
                params={"state": "started", "timeout": timeout},
                timeout=float(timeout + 10),
            )
            if r.status_code == 200:
                if on_output:
                    on_output("[dim]✅ Machine started[/]")
                return True
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> FlyAPIClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def get_client() -> FlyAPIClient | None:
    """Return a configured client, or ``None`` if no token is available."""
    token = resolve_token()
    return FlyAPIClient(token) if token else None
