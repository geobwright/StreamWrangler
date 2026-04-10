"""
StreamWrangler TUI — channel curation interface.

Pass 1: Include / Exclude decisions.
Pass 2 (separate): Channel number assignment.

Keybindings:
  Tab / Shift+Tab   Switch focus between group list and channel table
  Space             Toggle include/exclude on selected channel
  i                 Include selected channel
  x                 Exclude selected channel
  A                 Include ALL pending in current group
  X                 Exclude ALL pending in current group
  e                 Edit display name of selected channel
  /                 Search/filter channels by name
  Escape            Clear search
  s                 Save to channels.json
  q                 Quit (prompts to save if unsaved changes)
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)
from textual import on, work
from rich.text import Text

from streamwrangler.store import ChannelRecord, load_store, save_store, store_summary


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

STATUS_ICON = {"pending": "·", "included": "✓", "excluded": "✗"}
STATUS_STYLE = {"pending": "dim", "included": "bold green", "excluded": "dim red"}
ALL_GROUP = "__ALL__"


def _status_text(status: str) -> Text:
    icon = STATUS_ICON.get(status, "?")
    style = STATUS_STYLE.get(status, "")
    return Text(icon, style=style)


# ─────────────────────────────────────────────
# Edit Name Modal
# ─────────────────────────────────────────────

class EditNameModal(ModalScreen[str | None]):
    """Simple modal for editing a channel display name."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
    ]

    def __init__(self, current_name: str) -> None:
        super().__init__()
        self.current_name = current_name

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-modal"):
            yield Label("Edit display name:", id="edit-label")
            yield Input(value=self.current_name, id="edit-input")
            yield Label("[Enter] Save  [Escape] Cancel", id="edit-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


# ─────────────────────────────────────────────
# Group List Item
# ─────────────────────────────────────────────

class GroupItem(ListItem):
    def __init__(self, group: str, label: str) -> None:
        super().__init__()
        self.group = group
        self._label = label

    def compose(self) -> ComposeResult:
        yield Static(self._label)

    def update_label(self, label: str) -> None:
        self.query_one(Static).update(label)


# ─────────────────────────────────────────────
# Main App
# ─────────────────────────────────────────────

class WrangleTUI(App):
    """StreamWrangler channel curation TUI."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #main-area {
        layout: horizontal;
        height: 1fr;
    }

    #group-panel {
        width: 28;
        border: solid $primary;
        padding: 0 1;
    }

    #group-title {
        text-align: center;
        color: $accent;
        padding: 0 0 1 0;
        border-bottom: solid $primary-darken-2;
    }

    #channel-panel {
        width: 1fr;
        border: solid $primary;
    }

    #search-bar {
        height: 3;
        padding: 0 1;
        display: none;
        border-bottom: solid $primary-darken-2;
    }

    #search-bar.visible {
        display: block;
    }

    #status-bar {
        height: 1;
        background: $primary-darken-3;
        color: $text-muted;
        padding: 0 1;
    }

    EditNameModal {
        align: center middle;
    }

    #edit-modal {
        width: 60;
        height: 9;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }

    #edit-label {
        margin-bottom: 1;
    }

    #edit-hint {
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("tab", "focus_next", "Switch panel", show=False),
        Binding("shift+tab", "focus_previous", "Switch panel", show=False),
        Binding("space", "toggle_channel", "Include/Exclude", show=True),
        Binding("i", "include_channel", "Include", show=False),
        Binding("x", "exclude_channel", "Exclude", show=True),
        Binding("shift+a", "include_all_group", "Include group", show=True),
        Binding("shift+x", "exclude_all_group", "Exclude group", show=True),
        Binding("e", "edit_name", "Edit name", show=True),
        Binding("/", "start_search", "Search", show=True),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("s", "save", "Save", show=True),
        Binding("q", "quit_app", "Quit", show=True),
    ]

    # Reactive state
    current_group: reactive[str] = reactive(ALL_GROUP)
    search_query: reactive[str] = reactive("")
    unsaved: reactive[bool] = reactive(False)

    def __init__(self, store_path: Path = Path("data/channels.json")) -> None:
        super().__init__()
        self.store_path = store_path
        self.channels: list[ChannelRecord] = load_store(store_path)
        self._uid_to_row: dict[str, int] = {}   # channel_uid -> DataTable row key

    # ── Compose ──────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-area"):
            with Vertical(id="group-panel"):
                yield Label("Groups", id="group-title")
                yield ListView(id="group-list")
            with Vertical(id="channel-panel"):
                yield Input(placeholder="Search channels… (Esc to clear)", id="search-bar")
                yield DataTable(id="channel-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._build_group_list()
        self._build_channel_table()
        self._refresh_channel_table()
        self._update_status_bar()
        self.query_one("#channel-table").focus()

    # ── Group list ───────────────────────────

    def _groups(self) -> list[str]:
        seen = []
        for c in self.channels:
            if c.target_group not in seen:
                seen.append(c.target_group)
        return seen

    def _group_label(self, group: str) -> str:
        if group == ALL_GROUP:
            summary = store_summary(self.channels)
            done = summary["included"] + summary["excluded"]
            return f"All  [{done}/{summary['total']}]"
        chs = [c for c in self.channels if c.target_group == group]
        done = sum(1 for c in chs if c.is_decided())
        inc = sum(1 for c in chs if c.status == "included")
        short = group[:20]
        return f"{short}  [{inc}/{len(chs)}]"

    def _build_group_list(self) -> None:
        lv = self.query_one("#group-list", ListView)
        lv.clear()
        lv.append(GroupItem(ALL_GROUP, self._group_label(ALL_GROUP)))
        for g in self._groups():
            lv.append(GroupItem(g, self._group_label(g)))

    def _refresh_group_labels(self) -> None:
        lv = self.query_one("#group-list", ListView)
        for item in lv.query(GroupItem):
            item.update_label(self._group_label(item.group))

    @on(ListView.Selected, "#group-list")
    def group_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, GroupItem):
            self.current_group = event.item.group
            self._refresh_channel_table()
            self.query_one("#channel-table").focus()

    # ── Channel table ────────────────────────

    def _build_channel_table(self) -> None:
        table = self.query_one("#channel-table", DataTable)
        table.clear(columns=True)
        table.add_column("St", width=3)
        table.add_column("Channel Name", width=40)
        table.add_column("Group", width=18)
        table.add_column("Source Name", width=35)

    def _visible_channels(self) -> list[ChannelRecord]:
        chs = self.channels
        if self.current_group != ALL_GROUP:
            chs = [c for c in chs if c.target_group == self.current_group]
        if self.search_query:
            q = self.search_query.lower()
            chs = [c for c in chs if q in c.display_name.lower()]
        return chs

    def _refresh_channel_table(self) -> None:
        table = self.query_one("#channel-table", DataTable)
        table.clear()
        self._uid_to_row.clear()
        for ch in self._visible_channels():
            row_key = table.add_row(
                _status_text(ch.status),
                Text(ch.display_name, style=STATUS_STYLE.get(ch.status, "")),
                Text(ch.target_group, style="dim"),
                Text(ch.raw_display_name, style="dim italic"),
                key=ch.channel_uid,
            )
            self._uid_to_row[ch.channel_uid] = row_key

    def _refresh_row(self, uid: str) -> None:
        """Refresh the status icon and name style for a single row."""
        table = self.query_one("#channel-table", DataTable)
        ch = next((c for c in self.channels if c.channel_uid == uid), None)
        if ch is None:
            return
        try:
            table.update_cell(uid, "st", _status_text(ch.status))
            table.update_cell(uid, "channel_name",
                              Text(ch.display_name, style=STATUS_STYLE.get(ch.status, "")))
        except Exception:
            # Fall back to full table refresh if cell update fails
            self._refresh_channel_table()

    def _channel_at_cursor(self) -> ChannelRecord | None:
        """Get the channel at the current cursor row using simple index lookup."""
        table = self.query_one("#channel-table", DataTable)
        if table.row_count == 0:
            return None
        visible = self._visible_channels()
        cursor = table.cursor_row
        if 0 <= cursor < len(visible):
            return visible[cursor]
        return None

    # ── Actions ──────────────────────────────

    def _set_status(self, ch: ChannelRecord, status: str) -> None:
        ch.status = status
        self._refresh_row(ch.channel_uid)
        self._refresh_group_labels()
        self._update_status_bar()
        self.unsaved = True

    def action_toggle_channel(self) -> None:
        ch = self._channel_at_cursor()
        if ch is None:
            return
        new_status = "excluded" if ch.status == "included" else "included"
        self._set_status(ch, new_status)

    def action_include_channel(self) -> None:
        ch = self._channel_at_cursor()
        if ch:
            self._set_status(ch, "included")

    def action_exclude_channel(self) -> None:
        ch = self._channel_at_cursor()
        if ch:
            self._set_status(ch, "excluded")

    def action_include_all_group(self) -> None:
        for ch in self._visible_channels():
            if ch.status == "pending":
                ch.status = "included"
        self._refresh_channel_table()
        self._refresh_group_labels()
        self._update_status_bar()
        self.unsaved = True

    def action_exclude_all_group(self) -> None:
        for ch in self._visible_channels():
            if ch.status == "pending":
                ch.status = "excluded"
        self._refresh_channel_table()
        self._refresh_group_labels()
        self._update_status_bar()
        self.unsaved = True

    def action_edit_name(self) -> None:
        ch = self._channel_at_cursor()
        if ch is None:
            return

        def apply_edit(new_name: str | None) -> None:
            if new_name and new_name != ch.display_name:
                ch.display_name = new_name
                self._refresh_row(ch.channel_uid)
                self.unsaved = True

        self.push_screen(EditNameModal(ch.display_name), apply_edit)

    def action_start_search(self) -> None:
        bar = self.query_one("#search-bar")
        bar.add_class("visible")
        self.query_one("#search-bar", Input).focus()

    def action_clear_search(self) -> None:
        self.search_query = ""
        bar = self.query_one("#search-bar")
        bar.remove_class("visible")
        self.query_one("#search-bar", Input).value = ""
        self._refresh_channel_table()
        self.query_one("#channel-table").focus()

    @on(Input.Changed, "#search-bar")
    def search_changed(self, event: Input.Changed) -> None:
        self.search_query = event.value
        self._refresh_channel_table()

    @on(Input.Submitted, "#search-bar")
    def search_submitted(self, _: Input.Submitted) -> None:
        self.query_one("#channel-table").focus()

    def action_save(self) -> None:
        save_store(self.channels, self.store_path)
        self.unsaved = False
        self._update_status_bar()
        self.notify("Saved to channels.json", severity="information")

    def action_quit_app(self) -> None:
        if self.unsaved:
            self.notify("Unsaved changes — press S to save first, or Q again to force quit",
                        severity="warning", timeout=4)
            self.BINDINGS = [b for b in self.BINDINGS if b.key != "q"] + [
                Binding("q", "force_quit", "Force Quit", show=False)
            ]
        else:
            self.exit()

    def action_force_quit(self) -> None:
        self.exit()

    # ── Status bar ───────────────────────────

    def _update_status_bar(self) -> None:
        s = store_summary(self.channels)
        dirty = " [yellow]●unsaved[/yellow]" if self.unsaved else ""
        text = (
            f"Total: {s['total']}  "
            f"[green]✓ {s['included']}[/green]  "
            f"[red]✗ {s['excluded']}[/red]  "
            f"[dim]· {s['pending']} pending[/dim]"
            f"{dirty}"
        )
        self.query_one("#status-bar", Static).update(text)

    def on_reactive_changed(self) -> None:
        self._update_status_bar()


def run_tui(store_path: Path = Path("data/channels.json")) -> None:
    """Launch the TUI."""
    app = WrangleTUI(store_path=store_path)
    app.run()
