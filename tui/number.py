"""
StreamWrangler Numbering TUI — review and adjust AI-proposed channel numbering.

Keybindings:
  Tab / Shift+Tab   Switch focus between block list and channel table
  J / K             Move channel down / up within its block
  e                 Edit display name of selected channel
  #                 Edit channel number of selected channel
  m                 Move channel to a different block
  s                 Save to config/numbering.yaml
  a                 Apply numbering to channels.json
  q                 Quit (prompts to save if unsaved)
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
from textual import on
from rich.text import Text

from streamwrangler.numbering import (
    NumberingBlock,
    NumberedChannel,
    NumberingPlan,
    load_numbering,
    save_numbering,
    apply_numbering,
    NUMBERING_PATH,
)
from streamwrangler.store import load_store, save_store, STORE_PATH


# ─────────────────────────────────────────────
# Modals
# ─────────────────────────────────────────────

class EditTextModal(ModalScreen[str | None]):
    """Generic single-field text editor modal."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    DEFAULT_CSS = """
    EditTextModal { align: center middle; }
    #edit-modal {
        width: 70; height: 9;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    #edit-label { margin-bottom: 1; }
    #edit-hint { margin-top: 1; color: $text-muted; }
    """

    def __init__(self, label: str, current: str) -> None:
        super().__init__()
        self.label = label
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-modal"):
            yield Label(self.label, id="edit-label")
            yield Input(value=self.current, id="edit-input")
            yield Label("[Enter] Save  [Escape] Cancel", id="edit-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class BlockPickerModal(ModalScreen[str | None]):
    """Choose a block to move a channel to."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    DEFAULT_CSS = """
    BlockPickerModal { align: center middle; }
    #picker-modal {
        width: 40; height: auto;
        max-height: 24;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    #picker-title { margin-bottom: 1; color: $accent; }
    #picker-hint { margin-top: 1; color: $text-muted; }
    """

    def __init__(self, blocks: list[NumberingBlock], current_block: str) -> None:
        super().__init__()
        self.blocks = blocks
        self.current_block = current_block

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-modal"):
            yield Label("Move to block:", id="picker-title")
            yield ListView(id="picker-list")
            yield Label("[Enter] Select  [Escape] Cancel", id="picker-hint")

    def on_mount(self) -> None:
        lv = self.query_one("#picker-list", ListView)
        for block in self.blocks:
            if block.name != self.current_block:
                lv.append(ListItem(Static(block.name), name=block.name))

    @on(ListView.Selected, "#picker-list")
    def picked(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.name)


# ─────────────────────────────────────────────
# Block list item
# ─────────────────────────────────────────────

class BlockItem(ListItem):
    def __init__(self, block_name: str, label: str) -> None:
        super().__init__()
        self.block_name = block_name
        self._label = label

    def compose(self) -> ComposeResult:
        yield Static(self._label)

    def update_label(self, label: str) -> None:
        self.query_one(Static).update(label)


# ─────────────────────────────────────────────
# Main app
# ─────────────────────────────────────────────

class NumberTUI(App):
    """StreamWrangler channel numbering TUI."""

    CSS = """
    Screen { layout: vertical; }

    #main-area { layout: horizontal; height: 1fr; }

    #block-panel {
        width: 28;
        border: solid $primary;
        padding: 0 1;
    }
    #block-title {
        text-align: center;
        color: $accent;
        padding: 0 0 1 0;
        border-bottom: solid $primary-darken-2;
    }

    #channel-panel { width: 1fr; border: solid $primary; }

    #status-bar {
        height: 1;
        background: $primary-darken-3;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("tab",       "focus_next",        "Switch panel",  show=False),
        Binding("shift+tab", "focus_previous",     "Switch panel",  show=False),
        Binding("shift+j",   "move_down",          "Move ↓",        show=True),
        Binding("shift+k",   "move_up",            "Move ↑",        show=True),
        Binding("e",         "edit_name",          "Edit name",     show=True),
        Binding("hash",      "edit_number",        "Edit #",        show=True),
        Binding("m",         "move_block",         "Move block",    show=True),
        Binding("s",         "save",               "Save",          show=True),
        Binding("a",         "apply",              "Apply",         show=True),
        Binding("q",         "quit_app",           "Quit",          show=True),
    ]

    current_block: reactive[str] = reactive("")
    unsaved: reactive[bool] = reactive(False)

    def __init__(self, plan: NumberingPlan, store_path: Path = STORE_PATH) -> None:
        super().__init__()
        self.plan = plan
        self.store_path = store_path
        if plan.blocks:
            self._current_block_name = plan.blocks[0].name
        else:
            self._current_block_name = ""

    # ── Compose ──────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-area"):
            with Vertical(id="block-panel"):
                yield Label("Blocks", id="block-title")
                yield ListView(id="block-list")
            with Vertical(id="channel-panel"):
                yield DataTable(id="channel-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._build_block_list()
        self._build_channel_table()
        self._refresh_channel_table()
        self._update_status_bar()
        self.query_one("#channel-table").focus()

    # ── Block list ───────────────────────────

    def _block_label(self, block: NumberingBlock) -> str:
        n = len(block.channels)
        return f"{block.name[:20]}  [{n}]"

    def _build_block_list(self) -> None:
        lv = self.query_one("#block-list", ListView)
        lv.clear()
        for block in self.plan.blocks:
            lv.append(BlockItem(block.name, self._block_label(block)))

    def _refresh_block_labels(self) -> None:
        items = list(self.query_one("#block-list", ListView).query(BlockItem))
        for item in items:
            block = self._get_block(item.block_name)
            if block:
                item.update_label(self._block_label(block))

    @on(ListView.Selected, "#block-list")
    def block_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, BlockItem):
            self._current_block_name = event.item.block_name
            self._refresh_channel_table()
            self.query_one("#channel-table").focus()

    def _get_block(self, name: str) -> NumberingBlock | None:
        return next((b for b in self.plan.blocks if b.name == name), None)

    def _current_block(self) -> NumberingBlock | None:
        return self._get_block(self._current_block_name)

    # ── Channel table ────────────────────────

    def _build_channel_table(self) -> None:
        table = self.query_one("#channel-table", DataTable)
        table.clear(columns=True)
        table.add_column("#",            width=6,  key="num")
        table.add_column("Channel Name", width=54, key="name")
        table.add_column("BK",           width=3,  key="bk")

    def _refresh_channel_table(self) -> None:
        table = self.query_one("#channel-table", DataTable)
        table.clear()
        block = self._current_block()
        if not block:
            return
        for ch in block.channels:
            is_bk = ch.uid.endswith("__bk")
            table.add_row(
                Text(str(ch.number), style="bold cyan" if not is_bk else "dim cyan"),
                Text(ch.display_name, style="dim" if is_bk else ""),
                Text("◀" if is_bk else "", style="dim"),
                key=ch.uid,
            )

    def _channel_at_cursor(self) -> NumberedChannel | None:
        table = self.query_one("#channel-table", DataTable)
        block = self._current_block()
        if not block or table.row_count == 0:
            return None
        idx = table.cursor_row
        if 0 <= idx < len(block.channels):
            return block.channels[idx]
        return None

    def _cursor_index(self) -> int:
        return self.query_one("#channel-table", DataTable).cursor_row

    # ── Actions ──────────────────────────────

    def action_move_down(self) -> None:
        block = self._current_block()
        if not block:
            return
        idx = self._cursor_index()
        if idx < len(block.channels) - 1:
            block.channels[idx], block.channels[idx + 1] = (
                block.channels[idx + 1],
                block.channels[idx],
            )
            self._refresh_channel_table()
            table = self.query_one("#channel-table", DataTable)
            table.move_cursor(row=idx + 1)
            self.unsaved = True
            self._update_status_bar()

    def action_move_up(self) -> None:
        block = self._current_block()
        if not block:
            return
        idx = self._cursor_index()
        if idx > 0:
            block.channels[idx], block.channels[idx - 1] = (
                block.channels[idx - 1],
                block.channels[idx],
            )
            self._refresh_channel_table()
            table = self.query_one("#channel-table", DataTable)
            table.move_cursor(row=idx - 1)
            self.unsaved = True
            self._update_status_bar()

    def action_edit_name(self) -> None:
        ch = self._channel_at_cursor()
        if ch is None:
            return

        def apply(new_name: str | None) -> None:
            if new_name and new_name != ch.display_name:
                ch.display_name = new_name
                self._refresh_channel_table()
                self.unsaved = True
                self._update_status_bar()

        self.push_screen(EditTextModal("Edit display name:", ch.display_name), apply)

    def action_edit_number(self) -> None:
        ch = self._channel_at_cursor()
        if ch is None:
            return

        def apply(new_val: str | None) -> None:
            if new_val is None:
                return
            try:
                num = int(new_val)
            except ValueError:
                self.notify("Must be a number", severity="warning")
                return
            ch.number = num
            self._refresh_channel_table()
            self.unsaved = True
            self._update_status_bar()

        self.push_screen(EditTextModal("Edit channel number:", str(ch.number)), apply)

    def action_move_block(self) -> None:
        ch = self._channel_at_cursor()
        block = self._current_block()
        if ch is None or block is None:
            return
        if len(self.plan.blocks) < 2:
            self.notify("No other blocks to move to", severity="warning")
            return

        def apply(target_name: str | None) -> None:
            if target_name is None:
                return
            target = self._get_block(target_name)
            if target is None:
                return
            block.channels.remove(ch)
            target.channels.append(ch)
            self._refresh_channel_table()
            self._refresh_block_labels()
            self.unsaved = True
            self._update_status_bar()
            self.notify(f"Moved to {target_name}", timeout=2)

        self.push_screen(BlockPickerModal(self.plan.blocks, block.name), apply)

    def action_save(self) -> None:
        save_numbering(self.plan)
        self.unsaved = False
        self._update_status_bar()
        self.notify("Saved to config/numbering.yaml", timeout=2)

    def action_apply(self) -> None:
        channels = load_store(self.store_path)
        updated = apply_numbering(self.plan, channels)
        save_store(channels, self.store_path)
        # Also save the numbering plan
        save_numbering(self.plan)
        self.unsaved = False
        self._update_status_bar()
        self.notify(f"Applied {updated} channels to channels.json", timeout=3)

    def action_quit_app(self) -> None:
        if self.unsaved:
            save_numbering(self.plan)
            self.notify("Auto-saved before quit", timeout=1)
        self.exit()

    # ── Status bar ───────────────────────────

    def _update_status_bar(self) -> None:
        total = sum(len(b.channels) for b in self.plan.blocks)
        blocks = len(self.plan.blocks)
        dirty = "  [yellow]● unsaved[/yellow]" if self.unsaved else ""
        block = self._current_block()
        block_info = f"  [cyan]{block.name}[/cyan]  {len(block.channels)} channels" if block else ""
        self.query_one("#status-bar", Static).update(
            f"[bold]{blocks} blocks[/bold]  {total} numbered{block_info}{dirty}"
        )


def run_number_tui(plan: NumberingPlan, store_path: Path = STORE_PATH) -> None:
    app = NumberTUI(plan=plan, store_path=store_path)
    app.run()
