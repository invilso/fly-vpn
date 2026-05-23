"""Settings modal — in-app key management for Fly.io and Tailscale credentials."""

from __future__ import annotations

import os
import platform
import webbrowser
from typing import TYPE_CHECKING, ClassVar

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

from flyexit import keystore

# Headless = Linux without a display server (SSH, CI, VPS).
# macOS and Windows always have a display, so buttons are always shown there.
_HEADLESS = (
    platform.system() == "Linux"
    and not os.environ.get("DISPLAY")
    and not os.environ.get("WAYLAND_DISPLAY")
)

_FIELDS: list[dict[str, str]] = [
    {
        "id": "fly_api_token",
        "label": "Fly.io API token",
        "placeholder": "fly_v1_…",
        "url": "https://fly.io/tokens",
        "hint": "fly.io/tokens",
    },
    {
        "id": "ts_api_key",
        "label": "Tailscale API key",
        "placeholder": "tskey-api-…",
        "url": "https://login.tailscale.com/admin/settings/keys",
        "hint": "tailscale.com/admin/settings/keys",
    },
    {
        "id": "ts_auth_key",
        "label": "Tailscale pre-auth key  (blank = auto-generate via API key)",
        "placeholder": "tskey-auth-… or leave blank",
        "url": "https://login.tailscale.com/admin/settings/authkeys",
        "hint": "tailscale.com/admin/settings/authkeys",
    },
]


class SettingsScreen(ModalScreen[bool]):
    """Modal settings screen for API credentials.

    Dismissed with ``True`` if any keys were saved, ``False`` otherwise.
    """

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    SettingsScreen {
        align: center middle;
    }
    #settings-dialog {
        width: 72;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #settings-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }
    .field-block {
        height: auto;
        margin-bottom: 1;
    }
    .field-label {
        margin-bottom: 0;
    }
    .url-row {
        height: auto;
        margin-top: 0;
    }
    .url-hint {
        width: 1fr;
        height: auto;
        color: $text-muted;
    }
    .btn-url {
        min-width: 8;
        height: auto;
    }
    #settings-actions {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    #headless-note {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-dialog"):
            yield Static("⚙  Settings", id="settings-title")

            for field in _FIELDS:
                fid = field["id"]
                saved = keystore.get(fid)
                with Vertical(classes="field-block"):
                    yield Label(field["label"], classes="field-label")
                    yield Input(
                        value=saved,
                        placeholder=field["placeholder"],
                        password=True,
                        id=f"input-{fid}",
                    )
                    if _HEADLESS:
                        yield Static(
                            f"[dim]{field['hint']}[/]",
                            classes="url-hint",
                        )
                    else:
                        with Horizontal(classes="url-row"):
                            yield Static(
                                f"[dim]{field['hint']}[/]",
                                classes="url-hint",
                            )
                            yield Button(
                                "Copy",
                                variant="default",
                                classes="btn-url",
                                id=f"btn-copy-{fid}",
                            )
                            yield Button(
                                "Open",
                                variant="default",
                                classes="btn-url",
                                id=f"btn-open-{fid}",
                            )

            if _HEADLESS:
                yield Static(
                    "[dim]Running headless — configure keys via .env or"
                    " environment variables.[/]",
                    id="headless-note",
                )

            with Horizontal(id="settings-actions"):
                yield Button("Save", variant="success", id="btn-save")
                yield Button("Cancel", variant="default", id="btn-cancel")

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    @on(Button.Pressed, "#btn-save")
    def _save(self) -> None:
        saved_any = False
        for field in _FIELDS:
            fid = field["id"]
            value = self.query_one(f"#input-{fid}", Input).value.strip()
            if value:
                keystore.set(fid, value)
                saved_any = True
        self.dismiss(saved_any)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""

        if bid.startswith("btn-copy-"):
            fid = bid.removeprefix("btn-copy-")
            url = next(
                (f["url"] for f in _FIELDS if f["id"] == fid), ""
            )
            if url:
                self._copy_to_clipboard(url, event.button)

        elif bid.startswith("btn-open-"):
            fid = bid.removeprefix("btn-open-")
            url = next(
                (f["url"] for f in _FIELDS if f["id"] == fid), ""
            )
            if url:
                webbrowser.open(url)

    def _copy_to_clipboard(self, text: str, btn: Button) -> None:
        original_label = str(btn.label)
        try:
            self.app.copy_to_clipboard(text)
            btn.label = "Copied!"
        except Exception:  # noqa: BLE001
            btn.label = "No clipboard"
        self.set_timer(1.5, lambda: setattr(btn, "label", original_label))
