"""Textual TUI application for Fly VPN.

This module is a **pure UI shell**.  All business logic (auth checks,
subprocess management, Tailscale connection) lives in
:mod:`flyexit.session`.  The app only reads structured results and
formats them for display.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import (
    Button,
    Footer,
    Header,
    RichLog,
    Select,
    Sparkline,
    Static,
)

from flyexit import config
from flyexit.constants import (
    DEFAULT_APP_NAME,
    DEFAULT_ORG,
    FLY_REGIONS,
    TS_EXIT_HOSTNAME,
)
from flyexit.session import (
    ConnectStatus,
    LaunchResult,
    LaunchStatus,
    PreflightStatus,
    VPNSession,
)
from flyexit.styles import APP_CSS


class FlyVPNApp(App[None]):
    """Ephemeral Tailscale exit-node launcher on Fly.io."""

    TITLE = "Fly VPN"
    SUB_TITLE = "Tailscale Exit Node on Fly.io"
    CSS = APP_CSS

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("l", "launch", "Launch", priority=True),
        Binding("s", "stop", "Stop", priority=True),
        Binding("t", "toggle_dark", "Theme", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._cfg = config.load()
        self._session = VPNSession(
            ts_auth_key=os.environ.get("TAILSCALE_AUTHKEY", ""),
            ts_api_key=os.environ.get("TAILSCALE_API_KEY", ""),
            ts_login_server=os.environ.get("TS_LOGIN_SERVER", ""),
        )
        self._launching = False
        self._stopping = False
        self._quitting = False

        atexit.register(self._session.emergency_cleanup)

    def _on_signal(self, signum: int, _frame: object) -> None:
        """Handle SIGINT / SIGTERM — clean up and exit."""
        self._session.emergency_cleanup()
        raise SystemExit(128 + signum)

    def on_mount(self) -> None:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGHUP, self._on_signal)

        log = self._rich_log
        log.write("[bold green]🛡  Fly VPN[/] initialized")
        log.write("")
        self._refresh_stats()
        self.set_interval(1, self._refresh_stats)
        if not self._session.has_auth:
            log.write("[bold red]⚠  No Tailscale auth configured![/]")
            log.write("Set [bold]TAILSCALE_API_KEY[/] (recommended)")
            log.write("or [bold]TAILSCALE_AUTHKEY[/] in your .env file.")
            self.query_one("#btn-launch", Button).disabled = True

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="main"):
            with Container(id="controls"), Horizontal(id="top-row"):
                with Vertical(id="region-col"):
                    yield Select(
                        [(f"{flag}  {code}", code) for code, flag in FLY_REGIONS],
                        value=self._cfg.get("region", "ams"),
                        id="region-select",
                    )
                    with Horizontal(id="button-row"):
                        yield Button(
                            "🚀 Launch",
                            variant="success",
                            id="btn-launch",
                        )
                        yield Button(
                            "🛑 Stop",
                            variant="error",
                            id="btn-stop",
                            disabled=True,
                        )
                with Vertical(id="stats-col"):
                    yield Static("", id="stats-text")
                    yield Sparkline([], id="cost-spark")
                    with Horizontal(id="update-row"):
                        yield Button("↑ Update", variant="default", id="btn-update")
            yield Static("Ready", id="status-bar")
            with Container(id="log-box"):
                yield RichLog(highlight=True, markup=True, id="log")
        yield Footer()

    @property
    def _rich_log(self) -> RichLog:
        return self.query_one("#log", RichLog)

    def _log(self, msg: str) -> None:
        self._rich_log.write(msg)

    def _set_status(self, text: str) -> None:
        self.query_one("#status-bar", Static).update(text)

    def _set_buttons(self, *, launching: bool) -> None:
        self.query_one("#btn-launch", Button).disabled = launching
        self.query_one("#btn-stop", Button).disabled = not launching

    def _refresh_stats(self) -> None:
        """Reload usage stats and sparkline data from the DB."""
        try:
            from flyexit.usage_db import (
                format_cost,
                format_duration,
                get_daily_costs,
                get_live_cost,
                get_stats,
            )

            stats = get_stats()
            total_s = stats.total_seconds
            total_c = stats.total_cost

            # Add live running session cost.
            sid = self._session._db_session_id
            live_suffix = ""
            if sid is not None:
                live_dur, live_cost = get_live_cost(sid)
                total_s += live_dur
                total_c += live_cost
                live_suffix = "  [bold yellow]⚡ live[/]"

            if stats.total_sessions > 0 or sid is not None:
                self.query_one("#stats-text", Static).update(
                    f"[bold]📊 Usage[/]  "
                    f"{format_duration(total_s)} · "
                    f"{format_cost(total_c)}"
                    f"{live_suffix}"
                )
                self.query_one("#cost-spark", Sparkline).data = get_daily_costs()
            else:
                self.query_one("#stats-text", Static).update("[dim]No sessions yet[/]")
                self.query_one("#cost-spark", Sparkline).data = []
        except Exception:  # noqa: BLE001, S110
            pass

    def action_launch(self) -> None:
        self._do_launch()

    def action_stop(self) -> None:
        self._do_stop()

    def action_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        if self._session.is_active:
            self._set_status("⏳ Cleaning up before exit…")
            self._log("[dim]🧹 Cleaning up before exit…[/]")
        self._teardown_with_log()
        super().action_quit()

    @on(Button.Pressed, "#btn-launch")
    def _handle_launch(self) -> None:
        self._do_launch()

    @on(Button.Pressed, "#btn-stop")
    def _handle_stop(self) -> None:
        self._do_stop()

    @on(Button.Pressed, "#btn-update")
    def _handle_update(self) -> None:
        self.query_one("#btn-update", Button).disabled = True
        self.query_one("#btn-update", Button).label = "⏳ Updating…"
        self._run_update()

    @work(thread=True)
    def _run_update(self) -> None:
        """Pull latest code and sync dependencies in the background."""
        import subprocess
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        self.call_from_thread(self._log, "[dim]⬆  Checking for updates…[/]")
        try:
            pull = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=30,
            )
            msg = pull.stdout.strip() or pull.stderr.strip()
            if pull.returncode != 0:
                self.call_from_thread(
                    self._log, f"[bold red]❌ git pull failed:[/] {msg}"
                )
                return

            if "Already up to date" in msg:
                self.call_from_thread(self._log, "[dim]✔ Already up to date[/]")
                return

            self.call_from_thread(self._log, f"[dim]{msg}[/]")
            self.call_from_thread(self._log, "[dim]📦 Syncing dependencies…[/]")
            sync = subprocess.run(
                ["uv", "sync"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if sync.returncode != 0:
                self.call_from_thread(
                    self._log,
                    f"[bold red]❌ uv sync failed:[/] {sync.stderr.strip()}",
                )
                return
            self.call_from_thread(
                self._log,
                "[bold green]✅ Updated! Restart Fly VPN to apply changes.[/]",
            )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._log, f"[bold red]❌ Update error:[/] {exc}")
        finally:
            self.call_from_thread(
                lambda: (
                    setattr(self.query_one("#btn-update", Button), "disabled", False),
                    setattr(self.query_one("#btn-update", Button), "label", "↑ Update"),
                ),
            )

    def _do_launch(self) -> None:
        if self._launching:
            return
        if self._session.is_active:
            self._log("[dim]Already running[/]")
            return
        self._launching = True
        self._set_buttons(launching=True)
        self._set_status("⏳ Preparing…")
        self._log("[dim]🔄 Starting launch sequence…[/]")
        self._run_launch()

    @work(thread=True)
    def _run_launch(self) -> None:
        """Single worker: preflight → launch → connect."""
        try:
            app_name = self._cfg.get("app_name", DEFAULT_APP_NAME)
            org = self._cfg.get("org", DEFAULT_ORG)

            pf = self._session.preflight(app_name, org)
            if pf.username:
                self.call_from_thread(
                    self._log, f"[dim]Authenticated as {pf.username}[/]"
                )
            if pf.status is not PreflightStatus.OK:
                self.call_from_thread(self._log, f"[bold red]⚠  {pf.error}[/]")
                return

            self.call_from_thread(
                self._log, f"[dim]App [bold]{app_name}[/bold] created ✅[/]"
            )

            region = self.query_one("#region-select", Select).value
            if region is Select.BLANK:
                region = self._cfg.get("region", "ams")
            region = str(region)
            self._cfg["region"] = region
            config.save(self._cfg)

            self.call_from_thread(self._set_buttons, launching=True)
            self.call_from_thread(self._set_status, f"🔄 Launching in {region}…")
            self.call_from_thread(
                self._log,
                f"\n[bold cyan]>>> Launching exit node in"
                f" [yellow]{region}[/yellow]…[/]",
            )

            result = self._session.launch(
                app_name,
                region,
                on_output=lambda line: self.call_from_thread(self._log, line),
            )

            if result.status is LaunchStatus.OK:
                self.call_from_thread(
                    self._log,
                    "[bold green]✅ Node launched successfully![/]",
                )
                self.call_from_thread(self._set_status, f"✅ Running in {region}")
                self._show_connect_result(region)
                return

            self._log_launch_error(result)

            self.call_from_thread(self._log, "[dim]🧹 Cleaning up remote resources…[/]")
            app_d, ok = self._session.teardown()
            self._log_teardown(app_d, ok, from_thread=True)
        finally:
            self._launching = False
            if not self._session.is_active:
                self.call_from_thread(self._set_buttons, launching=False)
                self.call_from_thread(self._set_status, "Ready")

    def _show_connect_result(self, region: str) -> None:
        """Wait for Tailscale exit node and display the outcome."""
        self.call_from_thread(
            self._log,
            f"[dim]⏳ Waiting for [bold]{TS_EXIT_HOSTNAME}[/bold]"
            " to appear in tailnet…[/]",
        )
        self.call_from_thread(
            self._set_status,
            f"⏳ Waiting for exit node in {region}…",
        )

        status = self._session.wait_and_connect()

        if status is ConnectStatus.CONNECTED:
            self.call_from_thread(
                self._log,
                f"[bold green]🔗 Connected![/] Traffic routed"
                f" through [bold]{TS_EXIT_HOSTNAME}[/]"
                f" ({region})\n"
                "[dim]  Press [bold]Stop[/bold] (or [bold]s[/bold])"
                " to disconnect and clean up.[/]",
            )
            self.call_from_thread(
                self._set_status,
                f"🟢 VPN active via {region} — press Stop to end",
            )
        elif status is ConnectStatus.TIMEOUT:
            self.call_from_thread(
                self._log,
                "[yellow]⚠  Exit node didn't appear in time. "
                "Connect manually via Tailscale.[/]\n"
                "[dim]  Press [bold]Stop[/bold] (or [bold]s[/bold])"
                " to terminate and clean up.[/]",
            )
            self.call_from_thread(
                self._set_status,
                f"🟡 Running in {region} — manual connect needed",
            )
        else:  # ConnectStatus.FAILED
            self.call_from_thread(
                self._log,
                "[yellow]⚠  Node is online but auto-connect"
                " failed.\n  Try manually: tailscale set"
                f" --exit-node={TS_EXIT_HOSTNAME}[/]",
            )
            self.call_from_thread(
                self._set_status,
                f"🟡 Running in {region} — manual connect needed",
            )

    def _do_stop(self) -> None:
        if self._stopping:
            return
        if self._session.is_active:
            self._stopping = True
            self._launching = False
            self._set_buttons(launching=True)
            self.query_one("#btn-stop", Button).disabled = True
            self._set_status("⏳ Stopping…")
            self._log("[bold yellow]⏹  Stopping node…[/]")
            self._run_stop()
        else:
            self._log("[dim]Nothing to stop[/]")

    @work(thread=True)
    def _run_stop(self) -> None:
        """Teardown session in a background thread."""
        try:
            app_name, ok = self._session.teardown()
            self.call_from_thread(self._log, "[dim]🔌 Disconnected from exit node[/]")
            self._log_teardown(app_name, ok, from_thread=True)
            if ok and app_name:
                if self._session._client is not None:
                    self.call_from_thread(
                        self._log,
                        "[dim]🗑  Tailscale node removed from tailnet.[/]",
                    )
                else:
                    self.call_from_thread(
                        self._log,
                        "[dim]💨 Tailscale ephemeral node will auto-remove"
                        " within a few minutes.[/]",
                    )
            try:
                from flyexit.usage_db import (
                    format_cost,
                    format_duration,
                    get_stats,
                )

                stats = get_stats()
                if stats.total_sessions > 0:
                    cost_line = (
                        f"[dim]💰 {stats.total_sessions} session(s),"
                        f" {format_duration(stats.total_seconds)},"
                        f" {format_cost(stats.total_cost)} spent[/]"
                    )
                    self.call_from_thread(self._log, cost_line)
            except Exception:  # noqa: BLE001, S110
                pass
            self.call_from_thread(self._refresh_stats)
        finally:
            self._stopping = False
            if not self._session.is_active:
                self.call_from_thread(self._set_buttons, launching=False)
                self.call_from_thread(self._set_status, "Ready")
            else:
                self.call_from_thread(self._set_buttons, launching=True)
                self.call_from_thread(
                    self._set_status,
                    "⚠️ Stop failed; tunnel still running",
                )

    def _teardown_with_log(self) -> None:
        """Synchronous teardown used by action_quit."""
        app_name, ok = self._session.teardown()
        self._log_teardown(app_name, ok, from_thread=False)

    def _log_launch_error(self, result: LaunchResult) -> None:
        """Format and log a failed LaunchResult."""
        if result.status is LaunchStatus.CLI_MISSING:
            self.call_from_thread(
                self._log,
                "[bold red]❌ 'fly' CLI not found.[/]  "
                "Install → curl -L https://fly.io/install.sh | sh",
            )
        elif result.status is LaunchStatus.ERROR:
            self.call_from_thread(self._log, f"[bold red]❌ Error: {result.error}[/]")
        else:  # LaunchStatus.PROCESS_FAILED
            self.call_from_thread(
                self._log,
                f"[bold red]❌ Exited with code {result.return_code}[/]",
            )
            if result.hint:
                self.call_from_thread(self._log, result.hint)

    def _log_teardown(
        self, app_name: str | None, ok: bool, *, from_thread: bool
    ) -> None:
        """Log the outcome of a session teardown."""
        if not app_name:
            return
        msg = (
            f"[bold green]🗑  App [bold]{app_name}[/bold] deleted (no charges)[/]"
            if ok
            else f"[yellow]⚠  Could not delete app {app_name}[/]"
        )
        if from_thread:
            self.call_from_thread(self._log, msg)
        else:
            with contextlib.suppress(Exception):
                self._log(msg)
