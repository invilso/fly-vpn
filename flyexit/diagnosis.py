"""Error diagnosis for Fly.io machine launch failures."""

from __future__ import annotations

import re

_ERROR_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"status:\s*408", re.IGNORECASE),
        "[bold yellow]💡 Tip:[/] Region [bold]{region}[/] timed out (HTTP 408).\n"
        "   This usually means the region is overloaded.\n"
        "   → Try another region (e.g. ams, fra, iad).",
    ),
    (
        re.compile(r"status:\s*413", re.IGNORECASE),
        "[bold yellow]💡 Tip:[/] Payload too large (413). Try a smaller image.",
    ),
    (
        re.compile(r"status:\s*429", re.IGNORECASE),
        "[bold yellow]💡 Tip:[/] Rate limited (429). Wait a minute and try again.",
    ),
    (
        re.compile(r"status:\s*5\d{2}", re.IGNORECASE),
        "[bold yellow]💡 Tip:[/] Fly.io server error. Try again in a few minutes.\n"
        "   Status: https://status.flyio.net",
    ),
    (
        re.compile(r"could not find.*organization", re.IGNORECASE),
        "[bold yellow]💡 Tip:[/] Organization not found.\n"
        "   Check the [bold]org[/] value in your config (~/.fly_vpn_config.json).",
    ),
    (
        re.compile(r"billing|payment|credit card|card on file", re.IGNORECASE),
        "[bold yellow]💡 Tip:[/] Fly.io requires a credit card to launch machines.\n"
        "   Add one at: https://fly.io/dashboard/personal/billing",
    ),
    (
        re.compile(r"no capacity|insufficient capacity", re.IGNORECASE),
        "[bold yellow]💡 Tip:[/] No capacity in [bold]{region}[/].\n"
        "   → Try a different region.",
    ),
    (
        re.compile(r"unauthorized|not authorized|permission denied", re.IGNORECASE),
        "[bold yellow]💡 Tip:[/] Auth issue. Try [bold]fly auth login[/] again.",
    ),
]


def diagnose_fly_error(output: str, region: str) -> str | None:
    """Match output against known error patterns and return a friendly hint."""
    for pattern, template in _ERROR_PATTERNS:
        if pattern.search(output):
            return template.format(region=region)
    return None
