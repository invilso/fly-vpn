"""Tests for ensure_app_exists() in fly_ops.py.

The implementation now uses the Fly.io Machines HTTP API (FlyAPIClient)
instead of subprocess calls.  Tests mock at the fly_api module boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from flyexit.fly_ops import AppStatus, ensure_app_exists


def _make_client(
    *,
    delete_ok: bool = True,
    create_results: list[tuple[bool, str]] | None = None,
) -> MagicMock:
    """Build a minimal mock FlyAPIClient."""
    m = MagicMock()
    m.delete_app.return_value = delete_ok
    m.create_app.side_effect = create_results or [(True, "")]
    return m


# ---------------------------------------------------------------------------
# Happy path: first create attempt succeeds
# ---------------------------------------------------------------------------


def test_creates_on_first_attempt():
    """delete_app runs unconditionally; create succeeds immediately; no sleep."""
    client = _make_client()
    with (
        patch("flyexit.fly_api.get_client", return_value=client),
        patch("flyexit.fly_ops.time.sleep") as mock_sleep,
    ):
        status, err = ensure_app_exists("fly-vpn-node", "personal")

    assert status is AppStatus.CREATED
    assert err == ""
    client.delete_app.assert_called_once_with("fly-vpn-node")
    client.create_app.assert_called_once_with("fly-vpn-node", "personal")
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Post-deletion lag: "already taken" on first two attempts
# ---------------------------------------------------------------------------


def test_waits_for_name_release_before_creating():
    """Ordering proof: 2 'already taken' failures → 2 sleeps → 3rd create succeeds."""
    client = _make_client(
        create_results=[
            (False, "Name has already been taken"),
            (False, "Name has already been taken"),
            (True, ""),
        ]
    )
    with (
        patch("flyexit.fly_api.get_client", return_value=client),
        patch("flyexit.fly_ops.time.sleep") as mock_sleep,
    ):
        status, err = ensure_app_exists("fly-vpn-node", "personal")

    assert status is AppStatus.CREATED
    assert err == ""
    assert mock_sleep.call_count == 2
    assert client.create_app.call_count == 3


# ---------------------------------------------------------------------------
# Max retries exhausted
# ---------------------------------------------------------------------------


def test_fails_after_max_retries():
    """10 consecutive 'already taken' failures → FAILED."""
    client = _make_client(
        create_results=[(False, "Name has already been taken")] * 10
    )
    with (
        patch("flyexit.fly_api.get_client", return_value=client),
        patch("flyexit.fly_ops.time.sleep"),
    ):
        status, err = ensure_app_exists("fly-vpn-node", "personal")

    assert status is AppStatus.FAILED
    assert "already been taken" in err


# ---------------------------------------------------------------------------
# Non-transient error — bail out immediately
# ---------------------------------------------------------------------------


def test_returns_failed_on_non_transient_error():
    """A non-'taken' error (e.g. billing) fails on the first attempt, no retry."""
    client = _make_client(
        create_results=[(False, "requires a credit card")]
    )
    with (
        patch("flyexit.fly_api.get_client", return_value=client),
        patch("flyexit.fly_ops.time.sleep") as mock_sleep,
    ):
        status, err = ensure_app_exists("fly-vpn-node", "personal")

    assert status is AppStatus.FAILED
    assert "credit card" in err
    client.create_app.assert_called_once()
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# No API token available
# ---------------------------------------------------------------------------


def test_returns_failed_when_no_token():
    """No Fly.io token configured → FAILED with descriptive message."""
    with patch("flyexit.fly_api.get_client", return_value=None):
        status, err = ensure_app_exists("fly-vpn-node", "personal")

    assert status is AppStatus.FAILED
    assert "token" in err.lower()


# ---------------------------------------------------------------------------
# diagnosis.py hint wiring
# ---------------------------------------------------------------------------


def test_diagnose_returns_hint_for_name_taken():
    """diagnose_fly_error matches 'already been taken' and formats app_name."""
    from flyexit.diagnosis import diagnose_fly_error

    hint = diagnose_fly_error(
        "Validation failed: Name has already been taken",
        "",
        app_name="fly-vpn-node",
    )

    assert hint is not None
    assert "fly-vpn-node" in hint
    assert "watchdog" in hint
