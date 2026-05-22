"""VPN session — state, lifecycle, and all business operations.

The session owns subprocess management, Fly API calls, and Tailscale
connection logic.  The UI layer only calls high-level methods and
reacts to structured enum-based results — no ``subprocess``,
``fly_ops``, or ``tailscale`` imports needed in the UI.

Public API
----------
* ``preflight(app_name, org)``    → ``PreflightResult``
* ``launch(app_name, region, …)`` → ``LaunchResult``
* ``wait_and_connect()``           → ``ConnectStatus``
* ``teardown()``                   → ``(app_name | None, ok)``
* ``emergency_cleanup()``         — sync, no UI, safe for atexit/signals
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

from flyexit.constants import FLY_ENV, TS_EXIT_HOSTNAME
from flyexit.diagnosis import diagnose_fly_error
from flyexit.fly_ops import (
    AppStatus,
    AuthStatus,
    build_fly_cmd,
    check_auth,
    cleanup_app_sync,
    destroy_app,
    ensure_app_exists,
    force_kill_process,
    kill_machine_by_name,
)
from flyexit.tailscale import (
    connect_exit_node,
    disconnect_exit_node,
    get_device_id,
    wait_for_exit_node,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# Re-export so the UI only imports from session
__all__ = [
    "AppStatus",
    "ConnectStatus",
    "LaunchResult",
    "LaunchStatus",
    "PreflightResult",
    "PreflightStatus",
    "VPNSession",
]


class PreflightStatus(Enum):
    """Overall outcome of :meth:`VPNSession.preflight`."""

    OK = auto()
    AUTH_FAILED = auto()
    APP_FAILED = auto()


class LaunchStatus(Enum):
    """Outcome of :meth:`VPNSession.launch`."""

    OK = auto()
    PROCESS_FAILED = auto()
    CLI_MISSING = auto()
    ERROR = auto()


class ConnectStatus(Enum):
    """Outcome of :meth:`VPNSession.wait_and_connect`."""

    CONNECTED = auto()
    TIMEOUT = auto()
    FAILED = auto()


@dataclass(slots=True)
class PreflightResult:
    """Structured result of :meth:`VPNSession.preflight`."""

    status: PreflightStatus
    username: str = ""
    app_status: AppStatus = field(default=AppStatus.FAILED)
    error: str = ""


@dataclass(slots=True)
class LaunchResult:
    """Structured result of :meth:`VPNSession.launch`."""

    status: LaunchStatus
    return_code: int = 0
    hint: str | None = None
    error: str | None = None


class VPNSession:
    """Tracks the state of one ephemeral VPN session."""

    def __init__(
        self,
        *,
        ts_auth_key: str = "",
        ts_api_key: str = "",
        ts_login_server: str = "",
    ) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.app_name: str | None = None
        self._ts_auth_key = ts_auth_key
        self._ts_login_server = ts_login_server

        # SaaS-only: API client for auth-key generation & device cleanup.
        if ts_api_key and not ts_login_server:
            from flyexit.tailscale_api import TailscaleAPIClient

            self._client: TailscaleAPIClient | None = TailscaleAPIClient(
                ts_api_key,
            )
        else:
            self._client = None
        self._db_session_id: int | None = None

    @property
    def has_auth(self) -> bool:
        """True when Tailscale auth is available (explicit key or API)."""
        return bool(self._ts_auth_key) or self._client is not None

    @property
    def is_active(self) -> bool:
        """True when a machine is launching or running."""
        return self.process is not None or self.app_name is not None

    def _start_usage_log(self, region: str, memory_mb: int = 256) -> None:
        """Record session start in the usage database."""
        try:
            from flyexit.usage_db import log_start

            self._db_session_id = log_start(region, memory_mb)
        except Exception:  # noqa: BLE001, S110
            pass

    def _end_usage_log(self) -> None:
        """Finalize the usage record with end time and cost."""
        if self._db_session_id is None:
            return
        try:
            from flyexit.usage_db import log_end

            log_end(self._db_session_id)
        except Exception:  # noqa: BLE001, S110
            pass
        finally:
            self._db_session_id = None

    def preflight(
        self,
        app_name: str,
        org: str,
    ) -> PreflightResult:
        """Verify Fly auth, ensure ACL is configured, and ensure the Fly app exists."""
        auth_status, info = check_auth()
        if auth_status is AuthStatus.NOT_AUTHENTICATED:
            return PreflightResult(
                status=PreflightStatus.AUTH_FAILED,
                error=info,
            )

        username = info

        # SaaS + API client → ensure ACL is ready (idempotent).
        if self._client is not None:
            from flyexit.acl_setup import setup_acl

            setup_acl(self._client)

        app_status, err = ensure_app_exists(app_name, org)
        if app_status is AppStatus.FAILED:
            return PreflightResult(
                status=PreflightStatus.APP_FAILED,
                username=username,
                error=err,
            )

        return PreflightResult(
            status=PreflightStatus.OK,
            username=username,
            app_status=app_status,
        )

    def launch(
        self,
        app_name: str,
        region: str,
        *,
        vm_memory: int = 512,
        on_output: Callable[[str], None] | None = None,
    ) -> LaunchResult:
        """Spawn a Fly machine and stream its stdout.

        If no explicit ``ts_auth_key`` was provided but an API client
        is available, a short-lived auth key is generated automatically.

        *on_output* is called for every line of process output so the
        UI can display it in real time without knowing anything about
        subprocesses.
        """
        self.app_name = app_name

        # Resolve auth key: explicit > auto-generated via API.
        auth_key = self._ts_auth_key
        if not auth_key and self._client is not None:
            try:
                auth_key = self._client.create_auth_key()
                if on_output:
                    on_output("[dim]🔑 Auth key generated automatically[/]")
            except Exception as exc:  # noqa: BLE001
                return LaunchResult(
                    status=LaunchStatus.ERROR,
                    error=f"Failed to generate Tailscale auth key: {exc}",
                )
        if not auth_key:
            return LaunchResult(
                status=LaunchStatus.ERROR,
                error="No Tailscale auth key available.",
            )

        try:
            cmd = build_fly_cmd(
                app_name,
                region,
                auth_key,
                TS_EXIT_HOSTNAME,
                login_server=self._ts_login_server,
                vm_memory=vm_memory,
            )
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=FLY_ENV,
            )

            output_lines: list[str] = []
            assert self.process.stdout is not None  # noqa: S101
            for line in self.process.stdout:
                stripped = line.rstrip()
                output_lines.append(stripped)
                if on_output:
                    on_output(stripped)

            self.process.wait()
            code = self.process.returncode
            self.process = None

            if code == 0:
                self._start_usage_log(region, vm_memory)
                return LaunchResult(status=LaunchStatus.OK)

            full_output = "\n".join(output_lines)
            hint = diagnose_fly_error(full_output, region)
            return LaunchResult(
                status=LaunchStatus.PROCESS_FAILED,
                return_code=code,
                hint=hint,
            )

        except FileNotFoundError:
            self.process = None
            return LaunchResult(status=LaunchStatus.CLI_MISSING)
        except Exception as exc:  # noqa: BLE001
            self.process = None
            return LaunchResult(
                status=LaunchStatus.ERROR,
                error=str(exc),
            )

    def wait_and_connect(self) -> ConnectStatus:
        """Block until the exit node appears in tailnet, then connect."""
        if not wait_for_exit_node():
            return ConnectStatus.TIMEOUT
        if connect_exit_node():
            return ConnectStatus.CONNECTED
        return ConnectStatus.FAILED

    def emergency_cleanup(self) -> None:
        """Kill process & destroy app synchronously.

        Handles SIGINT, SIGTERM, SIGHUP, and atexit.
        Disconnects Tailscale, destroys Fly app, and removes
        the device from the tailnet.  No UI, no exceptions.
        """
        self._end_usage_log()
        disconnect_exit_node()
        force_kill_process(self.process)
        self.process = None
        if self.app_name:
            cleanup_app_sync(self.app_name)
            if self._client is not None:
                device_id = get_device_id()
                if device_id:
                    self._client.delete_device(device_id)
            self.app_name = None

    def teardown(self) -> tuple[str | None, bool]:
        """Disconnect TS → kill process → kill machines → destroy app.

        Returns ``(app_name, success)``.  If there was no active app,
        returns ``(None, True)``.
        """
        self._end_usage_log()
        disconnect_exit_node()
        force_kill_process(self.process)
        self.process = None

        app_name = self.app_name
        if not app_name:
            return None, True

        kill_machine_by_name(app_name)
        ok = destroy_app(app_name)
        self.app_name = None

        if self._client is not None:
            device_id = get_device_id()
            if device_id:
                self._client.delete_device(device_id)

        return app_name, ok
