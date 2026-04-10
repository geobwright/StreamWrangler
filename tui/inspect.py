"""
StreamWrangler Inspect TUI — browse raw feed variants before normalization/deduplication.

Use this before `wrangle curate` to understand what the provider sends for a channel
family (e.g. all EuroSport variants), probe individual or bulk streams, and build up
the probe cache so curate arrives pre-populated.

Keybindings:
  p             Probe channel at cursor
  Space         Mark / unmark channel for bulk probe
  b             Probe all marked channels (concurrent)
  a             Probe all visible channels (concurrent, max 4 at a time)
  n             Toggle sort by normalized name (clusters variants together)
  u             Show stream URL
  s             Save probe cache to disk
  q             Quit (auto-saves if there are unsaved probe results)
  Escape        Clear search / return focus to table
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Input, Static
from textual import on
from rich.text import Text

from streamwrangler.probe_cache import record_probe, save_probe_cache


# ─────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────

@dataclass
class InspectEntry:
    """One raw feed variant, enriched with normalization preview and probe results."""
    raw_name: str
    normalized_name: str
    source_group: str
    target_group: str
    url: str
    channel_id: str          # stable ID extracted from URL (probe cache key)
    detected_quality: str    # quality tier detected from raw name
    detected_codec: str      # codec hint detected from raw name
    # Populated after probing
    probe_quality: str = ""
    probe_codec: str = ""
    probe_width: int = 0
    probe_height: int = 0
    probe_bitrate_kbps: int = 0
    probed: bool = False
    probe_failed: bool = False   # probed but got no usable response (offline/geo-blocked)
    # Ranking within normalized-name group (set by _recompute_ranks, probe-only)
    probe_rank: int = 0          # 1 = best in tier; 0 = not ranked yet
    probe_tier: int = 0          # 1 = high-BW (4K/FHD), 2 = low-BW (HD/SD/Unk)
    probe_group_size: int = 0    # total probed entries in this tier
    # UI state
    marked: bool = False
    probing: bool = False        # currently in-flight probe


# ─────────────────────────────────────────────
# Rendering helpers
# ─────────────────────────────────────────────

QUALITY_STYLE = {
    "4K": "bold magenta", "FHD": "bold cyan",
    "HD": "white",        "SD":  "dim yellow",
    "":   "dim",
}


def _quality_from_resolution(w: int, h: int) -> str:
    if w >= 3840 or h >= 2160:
        return "4K"
    if w >= 1920 or h >= 1080:
        return "FHD"
    if w >= 1280 or h >= 720:
        return "HD"
    return "SD"


def _mark_cell(e: InspectEntry) -> Text:
    return Text("●", style="bold yellow") if e.marked else Text(" ")


def _qual_cell(e: InspectEntry) -> Text:
    label = e.detected_quality or "Unk"
    return Text(label, style=QUALITY_STYLE.get(e.detected_quality, "dim"))


def _codec_cell(e: InspectEntry) -> Text:
    if not e.detected_codec:
        return Text("-", style="dim")
    style = "bold yellow" if e.detected_codec == "hevc" else ""
    return Text(e.detected_codec, style=style)


def _probe_cell(e: InspectEntry) -> Text:
    if e.probing:
        return Text("⟳", style="bold yellow")
    if not e.probed:
        return Text("-", style="dim")
    if e.probe_failed:
        return Text("✗ offline", style="bold red")
    parts = []
    if e.probe_width and e.probe_height:
        parts.append(f"{e.probe_width}x{e.probe_height}")
    if e.probe_quality:
        parts.append(e.probe_quality)
    if e.probe_codec:
        parts.append(e.probe_codec)
    return Text("  ".join(parts), style=QUALITY_STYLE.get(e.probe_quality, ""))


def _rank_cell(e: InspectEntry) -> Text:
    """
    Show post-probe rank within tier. Blank until probed.
      ★  (green) — Tier 1 winner (best 4K/FHD in group)
      ◆  (cyan)  — Tier 2 winner (best HD/SD in group)
      #N (dim)   — ranked but not winner within its tier
    """
    if not e.probed or e.probe_failed or e.probe_rank == 0:
        return Text(" ")
    if e.probe_rank == 1 and e.probe_tier == 1:
        return Text("★", style="bold green")
    if e.probe_rank == 1 and e.probe_tier == 2:
        return Text("◆", style="bold cyan")
    return Text(f"#{e.probe_rank}", style="dim")


# ─────────────────────────────────────────────
# Main App
# ─────────────────────────────────────────────

class InspectTUI(App):
    """StreamWrangler feed inspector."""

    CSS = """
    Screen { layout: vertical; }

    #search-bar {
        height: 3;
        padding: 0 1;
        border-bottom: solid $primary-darken-2;
    }

    #channel-table { height: 1fr; }

    #status-bar {
        height: 1;
        background: $primary-darken-3;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("p",      "probe_single",  "Probe",        show=True),
        Binding("space",  "toggle_mark",   "Mark",         show=True),
        Binding("b",      "probe_marked",  "Probe marked", show=True),
        Binding("a",      "probe_visible", "Probe all",    show=True),
        Binding("n",      "toggle_sort",   "Sort name",    show=True),
        Binding("u",      "show_url",      "URL",          show=True),
        Binding("escape", "clear_search",  "Clear",        show=False),
        Binding("s",      "save_cache",    "Save",         show=True),
        Binding("q",      "quit_app",      "Quit",         show=True),
    ]

    search_query:      reactive[str]  = reactive("")
    sort_by_normalized: reactive[bool] = reactive(True)

    def __init__(
        self,
        entries: list[InspectEntry],
        cache: dict,
        cache_path: Path,
    ) -> None:
        super().__init__()
        self.entries = entries
        self.cache = cache
        self.cache_path = cache_path
        self._unsaved = False
        self._active_probes = 0

    # ── Compose / mount ──────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="Search raw or normalized name…", id="search-bar")
        yield DataTable(id="channel-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._build_table()
        self._recompute_ranks()
        self._refresh_table()
        self._update_status()
        self.query_one("#channel-table").focus()

    # ── Table ────────────────────────────────

    def _build_table(self) -> None:
        t = self.query_one("#channel-table", DataTable)
        t.clear(columns=True)
        t.add_column("*",            width=2,  key="mark")
        t.add_column("Rank",         width=5,  key="rank")
        t.add_column("Source Group", width=20, key="grp")
        t.add_column("Raw Name",     width=42, key="raw")
        t.add_column("Normalized",   width=30, key="norm")
        t.add_column("Qual",         width=5,  key="qual")
        t.add_column("Codec",        width=7,  key="codec")
        t.add_column("Probe",        width=22, key="probe")

    def _visible(self) -> list[InspectEntry]:
        entries = self.entries
        if self.search_query:
            q = self.search_query.lower()
            entries = [
                e for e in entries
                if q in e.raw_name.lower() or q in e.normalized_name.lower()
            ]
        if self.sort_by_normalized:
            entries = sorted(entries, key=lambda e: (e.normalized_name.lower(), e.target_group))
        return entries

    def _refresh_table(self) -> None:
        t = self.query_one("#channel-table", DataTable)
        t.clear()
        for e in self._visible():
            t.add_row(
                _mark_cell(e),
                _rank_cell(e),
                Text(e.source_group, style="dim"),
                Text(e.raw_name),
                Text(e.normalized_name, style="cyan"),
                _qual_cell(e),
                _codec_cell(e),
                _probe_cell(e),
                key=str(id(e)),
            )

    def _refresh_row(self, e: InspectEntry) -> None:
        """Update mutable cells for one entry. Silently skips if not currently visible."""
        t = self.query_one("#channel-table", DataTable)
        row_key = str(id(e))
        try:
            t.update_cell(row_key, "mark",  _mark_cell(e))
            t.update_cell(row_key, "probe", _probe_cell(e))
            t.update_cell(row_key, "rank",  _rank_cell(e))
        except Exception:
            pass  # Row filtered out of current view

    def _entry_at_cursor(self) -> InspectEntry | None:
        t = self.query_one("#channel-table", DataTable)
        visible = self._visible()
        if not visible or t.row_count == 0:
            return None
        cur = t.cursor_row
        return visible[cur] if 0 <= cur < len(visible) else None

    # ── Ranking ──────────────────────────────

    def _recompute_ranks(self) -> None:
        """
        After probing, rank variants within each (normalized_name, target_group) group,
        split into two tiers based on actual probe quality:
          Tier 1 (high-BW): 4K, FHD  →  ★ marks the winner
          Tier 2 (low-BW):  HD, SD, Unk  →  ◆ marks the winner
        Only successful probes are ranked. Updates probe_rank/probe_tier/probe_group_size.
        """
        from streamwrangler.normalizer import quality_score

        _TIER1 = {"4K", "FHD"}
        _TIER2 = {"HD", "SD", ""}

        # Reset all ranks first (handles re-runs after additional probes)
        for e in self.entries:
            e.probe_rank = 0
            e.probe_tier = 0
            e.probe_group_size = 0

        # Group successful probes by (normalized_name, target_group)
        groups: dict[tuple[str, str], list[InspectEntry]] = {}
        for e in self.entries:
            if e.probed and not e.probe_failed:
                key = (e.normalized_name, e.target_group)
                groups.setdefault(key, []).append(e)

        for group_entries in groups.values():
            tier1 = [e for e in group_entries if e.probe_quality in _TIER1]
            tier2 = [e for e in group_entries if e.probe_quality in _TIER2]

            for tier_num, tier_entries in ((1, tier1), (2, tier2)):
                ranked = sorted(
                    tier_entries,
                    key=lambda e: quality_score(e.probe_quality, e.raw_name, e.probe_codec),
                    reverse=True,
                )
                for rank, e in enumerate(ranked, 1):
                    e.probe_rank = rank
                    e.probe_tier = tier_num
                    e.probe_group_size = len(ranked)

    # ── Search ───────────────────────────────

    @on(Input.Changed, "#search-bar")
    def search_changed(self, event: Input.Changed) -> None:
        self.search_query = event.value
        self._refresh_table()
        self._update_status()

    @on(Input.Submitted, "#search-bar")
    def search_submitted(self, _: Input.Submitted) -> None:
        self.query_one("#channel-table").focus()

    def action_clear_search(self) -> None:
        inp = self.query_one("#search-bar", Input)
        if inp.value:
            inp.value = ""
            self.search_query = ""
            self._refresh_table()
            self._update_status()
        self.query_one("#channel-table").focus()

    # ── Sort ─────────────────────────────────

    def action_toggle_sort(self) -> None:
        self.sort_by_normalized = not self.sort_by_normalized
        self._refresh_table()
        self._update_status()

    # ── Mark ─────────────────────────────────

    def action_toggle_mark(self) -> None:
        e = self._entry_at_cursor()
        if e is None:
            return
        e.marked = not e.marked
        self._refresh_row(e)
        self._update_status()
        t = self.query_one("#channel-table", DataTable)
        nxt = t.cursor_row + 1
        if nxt < t.row_count:
            t.move_cursor(row=nxt)

    # ── Probe ────────────────────────────────

    async def action_probe_single(self) -> None:
        e = self._entry_at_cursor()
        if e:
            await self._probe_many([e])

    async def action_probe_marked(self) -> None:
        targets = [e for e in self._visible() if e.marked]
        if not targets:
            self.notify("No channels marked — press Space to mark", severity="warning")
            return
        await self._probe_many(targets)

    async def action_probe_visible(self) -> None:
        targets = self._visible()
        if not targets:
            self.notify("Nothing visible to probe", severity="warning")
            return
        await self._probe_many(targets)

    async def _probe_many(self, targets: list[InspectEntry]) -> None:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            self.notify("ffprobe not found — install ffmpeg", severity="error")
            return

        sem = asyncio.Semaphore(4)

        async def one(e: InspectEntry) -> None:
            async with sem:
                e.probing = True
                self._active_probes += 1
                self._refresh_row(e)
                self._update_status()
                try:
                    await self._run_ffprobe(e, ffprobe)
                finally:
                    e.probing = False
                    self._active_probes -= 1
                    self._refresh_row(e)
                    self._update_status()

        await asyncio.gather(*[one(e) for e in targets])

        # Recompute rankings across all entries now that new probes are in
        self._recompute_ranks()
        # Refresh all visible rows so rank column updates
        t = self.query_one("#channel-table", DataTable)
        for e in self._visible():
            row_key = str(id(e))
            try:
                t.update_cell(row_key, "rank", _rank_cell(e))
            except Exception:
                pass

        self._unsaved = True
        self._update_status()

    async def _run_ffprobe(self, e: InspectEntry, ffprobe_path: str) -> None:
        cmd = [
            ffprobe_path, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,bit_rate",
            "-show_entries", "format=bit_rate",
            "-of", "default=noprint_wrappers=1",
            "-timeout", "15000000",
            e.url,
        ]
        loop = asyncio.get_event_loop()
        success = False
        try:
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=15),
            )
            raw = proc.stdout.strip()
            if raw:
                pairs: dict[str, str] = {}
                for line in raw.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() not in pairs:
                            pairs[k.strip()] = v.strip()

                if "width" in pairs and "height" in pairs:
                    try:
                        w, h = int(pairs["width"]), int(pairs["height"])
                        e.probe_width = w
                        e.probe_height = h
                        e.probe_quality = _quality_from_resolution(w, h)
                    except ValueError:
                        pass

                if "codec_name" in pairs:
                    e.probe_codec = pairs["codec_name"].lower()

                if "bit_rate" in pairs:
                    try:
                        e.probe_bitrate_kbps = int(pairs["bit_rate"]) // 1000
                    except (ValueError, ZeroDivisionError):
                        pass

                success = True
                record_probe(
                    e.url, e.probe_quality, e.probe_codec,
                    e.probe_width or None, e.probe_height or None,
                    e.probe_bitrate_kbps or None,
                    self.cache,
                )
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
        finally:
            e.probed = True
            if not success:
                e.probe_failed = True

    # ── URL ──────────────────────────────────

    def action_show_url(self) -> None:
        from tui.app import UrlModal
        e = self._entry_at_cursor()
        if e:
            self.push_screen(UrlModal(e.raw_name, e.url))

    # ── Save / Quit ──────────────────────────

    def action_save_cache(self) -> None:
        save_probe_cache(self.cache, self.cache_path)
        self._unsaved = False
        self._update_status()
        self.notify("Probe cache saved", severity="information", timeout=2)

    def action_quit_app(self) -> None:
        if self._unsaved:
            save_probe_cache(self.cache, self.cache_path)
        self.exit()

    # ── Status bar ───────────────────────────

    def _update_status(self) -> None:
        visible = self._visible()
        marked = sum(1 for e in visible if e.marked)
        probed_ok = sum(1 for e in self.entries if e.probed and not e.probe_failed)
        probed_dead = sum(1 for e in self.entries if e.probe_failed)
        sort_ind = "  [cyan]⇅ norm[/cyan]" if self.sort_by_normalized else ""
        dirty = "  [yellow]● unsaved[/yellow]" if self._unsaved else ""
        probing_str = (
            f"  [yellow]⟳ {self._active_probes} probing[/yellow]"
            if self._active_probes > 0 else ""
        )
        dead_str = f"  [red]✗ {probed_dead} offline[/red]" if probed_dead else ""
        self.query_one("#status-bar", Static).update(
            f"Showing [cyan]{len(visible):,}[/cyan] / {len(self.entries):,}"
            f"  [yellow]● {marked} marked[/yellow]"
            f"  [green]✓ {probed_ok} probed[/green]"
            f"{dead_str}{probing_str}{sort_ind}{dirty}"
        )


def run_inspect_tui(
    entries: list[InspectEntry],
    cache: dict,
    cache_path: Path,
) -> None:
    InspectTUI(entries=entries, cache=cache, cache_path=cache_path).run()
