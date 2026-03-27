"""Regression tests for race conditions in worker launch/stop logic (PR #2).

1. Permanent-lock - _launching / _stopping remained True after any
   uncaught exception in a @work(thread=True) worker, because the flag
   was cleared inline (not inside a finally block).

2. Ordering hazard - call_from_thread(_set_buttons, launching=False) was
   enqueued while _launching was still True.  The main thread could
   re-enable the button and accept a second click before the worker had
   cleared the flag.

   Fixed ordering in the finally block::

       self._launching = False                         # cleared first
       self.call_from_thread(self._set_buttons, ...)   # then enqueued
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from collections.abc import Callable

import pytest
from textual.worker import WorkerFailed

from flyexit.session import PreflightResult, PreflightStatus

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    """FlyVPNApp with all external I/O neutralised."""
    from flyexit import config as cfg_module

    monkeypatch.setattr(cfg_module, "load", lambda: {"region": "ams"})
    monkeypatch.setattr(cfg_module, "save", lambda _: None)

    # Ensure no real Tailscale client is constructed.
    monkeypatch.setenv("TAILSCALE_AUTHKEY", "")
    monkeypatch.setenv("TAILSCALE_API_KEY", "")

    from flyexit.app import FlyVPNApp

    return FlyVPNApp()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _poll(condition: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Busy-poll until *condition()* is True or raise TimeoutError."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"condition never became True within {timeout}s")


# ---------------------------------------------------------------------------
# Bug 1 - permanent-lock: flags cleared even when worker raises
# ---------------------------------------------------------------------------


async def test_launching_cleared_when_preflight_raises(app):
    """_launching must be False after _run_launch exits via exception."""
    app._session.preflight = MagicMock(side_effect=RuntimeError("network boom"))

    # WorkerFailed is re-raised by run_test on exit — expected and suppressed.
    with contextlib.suppress(WorkerFailed):
        async with app.run_test():
            app._do_launch()
            await _poll(lambda: not app._launching)

    assert not app._launching, "_launching was not cleared after worker exit"


async def test_launching_cleared_when_session_launch_raises(app):
    """_launching must be False even when session.launch raises unexpectedly."""
    app._session.preflight = MagicMock(
        return_value=PreflightResult(status=PreflightStatus.OK, username="u")
    )
    app._session.launch = MagicMock(side_effect=RuntimeError("process died"))

    with contextlib.suppress(WorkerFailed):
        async with app.run_test():
            app._do_launch()
            await _poll(lambda: not app._launching)

    assert not app._launching, "_launching was not cleared after launch exception"


async def test_stopping_cleared_when_teardown_raises(app):
    """_stopping must be False after _run_stop exits via exception."""
    app._session.app_name = "fly-vpn-test"  # make is_active → True
    app._session.teardown = MagicMock(side_effect=RuntimeError("fly CLI gone"))

    with contextlib.suppress(WorkerFailed):
        async with app.run_test():
            app._do_stop()
            await _poll(lambda: not app._stopping)

    assert not app._stopping, "_stopping was not cleared after worker exit"


# ---------------------------------------------------------------------------
# Bug 2 - ordering: flag must be False before the UI callback fires
# ---------------------------------------------------------------------------


async def test_launching_false_before_set_buttons_reset_fires(app):
    """_launching must already be False when _set_buttons(launching=False) runs.

    Old (buggy) ordering inside the worker::

        self.call_from_thread(self._set_buttons, launching=False)  # enqueue
        self._launching = False  # cleared too late — button clickable early

    Fixed ordering::

        self._launching = False  # cleared first (still in worker thread)
        self.call_from_thread(self._set_buttons, launching=False)  # then enqueue
    """
    app._session.preflight = MagicMock(side_effect=RuntimeError("boom"))

    launching_on_reset: list[bool] = []
    _real_set_buttons = app._set_buttons

    def _spy(*, launching: bool) -> None:
        if not launching:
            # Record _launching at the moment the main-thread callback runs.
            launching_on_reset.append(app._launching)
        _real_set_buttons(launching=launching)

    with contextlib.suppress(WorkerFailed):
        async with app.run_test():
            app._set_buttons = _spy
            app._do_launch()
            # Wait until the reset callback has been processed by the main thread.
            await _poll(lambda: bool(launching_on_reset))

    assert launching_on_reset, "_set_buttons(launching=False) was never called"
    assert not any(launching_on_reset), (
        f"_launching was still True when _set_buttons(launching=False) ran: "
        f"{launching_on_reset}"
    )
