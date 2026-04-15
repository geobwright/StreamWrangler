"""StreamWrangler CLI — entry point: `wrangle`"""

from collections import Counter
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import box

from streamwrangler.parser import parse_m3u_list
from streamwrangler.filter import load_group_rules, build_group_map, filter_channels, filter_summary
from streamwrangler.normalizer import load_normalization_rules, normalize_channels
from streamwrangler.store import load_store, save_store, build_store, store_summary, STORE_PATH
from streamwrangler.probe_cache import load_probe_cache, CACHE_PATH
from streamwrangler.numbering import load_numbering, save_numbering, apply_numbering, merge_new_channels, fix_block_starts, NUMBERING_PATH

app = typer.Typer(
    name="wrangle",
    help="StreamWrangler — IPTV normalization and channel orchestration engine",
    no_args_is_help=True,
    invoke_without_command=True,
)
console = Console()


@app.command()
def analyze(
    source: Annotated[
        Optional[Path],
        typer.Argument(help="M3U file to analyze. Uses provider URL if omitted."),
    ] = None,
    top: Annotated[int, typer.Option("--top", "-n", help="Show top N groups")] = 50,
    country: Annotated[
        Optional[str],
        typer.Option("--country", "-c", help="Filter to a country prefix e.g. US, UK, FR"),
    ] = None,
    samples: Annotated[int, typer.Option("--samples", "-s", help="Sample channel names per group")] = 5,
):
    """Analyze an M3U feed — show groups, channel counts, and sample names."""

    if source is None:
        # Fall back to provider URL via config
        try:
            import yaml
            cfg = yaml.safe_load(Path("config/config.local.yaml").read_text())
            url = cfg["provider"]["url"]
        except Exception:
            console.print("[red]No source file provided and config/config.local.yaml not found.[/red]")
            raise typer.Exit(1)

        console.print(f"[dim]Fetching feed from provider URL...[/dim]")
        import httpx
        response = httpx.get(url, timeout=60, follow_redirects=True)
        response.raise_for_status()
        channels = parse_m3u_list(response.text)
    else:
        console.print(f"[dim]Parsing {source}...[/dim]")
        channels = parse_m3u_list(source)

    if country:
        channels = [c for c in channels if c.country_prefix.upper() == country.upper()]

    total = len(channels)
    console.print(f"\n[bold green]Total channels:[/bold green] {total:,}\n")

    # Group counts
    group_counts: Counter = Counter(c.group_title for c in channels)
    # Sample names per group
    group_samples: dict[str, list[str]] = {}
    for c in channels:
        group_samples.setdefault(c.group_title, [])
        if len(group_samples[c.group_title]) < samples:
            name = c.display_name
            # Skip header/separator entries (lines full of #, =, etc.)
            if not all(ch in "#=-| \t" for ch in name):
                group_samples[c.group_title].append(name)

    table = Table(
        title=f"Top {top} Groups" + (f" ({country})" if country else ""),
        box=box.SIMPLE_HEAVY,
        show_lines=False,
    )
    table.add_column("Group", style="cyan", no_wrap=True, max_width=50)
    table.add_column("Count", style="yellow", justify="right")
    table.add_column(f"Sample channels (up to {samples})", style="white", max_width=80)

    for group, count in group_counts.most_common(top):
        sample_str = "  |  ".join(group_samples.get(group, []))
        table.add_row(group, str(count), sample_str)

    console.print(table)

    # Country summary
    country_counts: Counter = Counter(c.country_prefix for c in channels)
    ctable = Table(title="By Country Prefix", box=box.SIMPLE_HEAVY)
    ctable.add_column("Prefix", style="cyan")
    ctable.add_column("Channels", style="yellow", justify="right")
    ctable.add_column("Groups", style="green", justify="right")

    prefix_groups: dict[str, set] = {}
    for c in channels:
        prefix_groups.setdefault(c.country_prefix, set()).add(c.group_title)

    for prefix, count in country_counts.most_common():
        ctable.add_row(prefix, str(count), str(len(prefix_groups[prefix])))

    console.print(ctable)


@app.command()
def filter_report(
    source: Annotated[
        Optional[Path],
        typer.Argument(help="M3U file to filter. Uses provider URL if omitted."),
    ] = None,
    seasonal: Annotated[bool, typer.Option("--seasonal", help="Include seasonal groups (AU, etc.)")] = False,
):
    """Show what passes through the group filter — counts by target group."""

    if source is None:
        try:
            import yaml
            cfg = yaml.safe_load(Path("config/config.local.yaml").read_text())
            url = cfg["provider"]["url"]
        except Exception:
            console.print("[red]No source file provided and config/config.local.yaml not found.[/red]")
            raise typer.Exit(1)
        console.print("[dim]Fetching from provider...[/dim]")
        import httpx
        response = httpx.get(url, timeout=60, follow_redirects=True)
        response.raise_for_status()
        channels = parse_m3u_list(response.text)
    else:
        console.print(f"[dim]Parsing {source}...[/dim]")
        channels = parse_m3u_list(source)

    rules = load_group_rules()
    group_map = build_group_map(rules, include_seasonal=seasonal)
    summary = filter_summary(channels, group_map)

    console.print(f"\n[bold]Input:[/bold]  {summary['total_input']:,} channels")
    console.print(f"[bold green]Output:[/bold green] {summary['total_output']:,} channels\n")

    table = Table(title="Channels by Target Group", box=box.SIMPLE_HEAVY)
    table.add_column("Target Group", style="cyan")
    table.add_column("Channels", style="yellow", justify="right")

    for group, count in sorted(summary["by_target_group"].items()):
        table.add_row(group, str(count))
    table.add_row("[dim]TOTAL[/dim]", f"[bold]{summary['total_output']:,}[/bold]")

    console.print(table)

    if summary["top_unmapped"]:
        console.print(f"\n[dim]Top unmapped groups (excluded — {summary['unmapped_group_count']} total):[/dim]")
        for group, count in summary["top_unmapped"][:10]:
            console.print(f"  [dim]{count:>5}  {group}[/dim]")


def _run_pipeline(source: Path | None) -> list:
    """Shared pipeline: parse → filter → normalize (probe-cache-aware)."""
    if source is None:
        import yaml, httpx
        cfg = yaml.safe_load(Path("config/config.local.yaml").read_text())
        url = cfg["provider"]["url"]
        console.print("[dim]Fetching from provider...[/dim]")
        response = httpx.get(url, timeout=60, follow_redirects=True)
        response.raise_for_status()
        channels = parse_m3u_list(response.text)
        console.print(f"[dim]Fetched {len(channels):,} raw channels from provider.[/dim]")
    else:
        channels = parse_m3u_list(source)
        console.print(f"[dim]Parsed {len(channels):,} raw channels from file.[/dim]")

    rules = load_group_rules()
    group_map = build_group_map(rules)
    filtered = filter_channels(channels, group_map)
    console.print(f"[dim]Group filter: {len(filtered):,} channels passed → normalizing...[/dim]")
    norm_rules = load_normalization_rules()
    probe_cache = load_probe_cache()
    return normalize_channels(filtered, norm_rules, probe_cache=probe_cache)


@app.command()
def ingest(
    source: Annotated[
        Optional[Path],
        typer.Argument(help="M3U file to ingest. Uses provider URL if omitted."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Re-ingest even if channels.json exists")] = False,
):
    """
    Ingest provider feed → build channels.json.
    Preserves existing include/exclude decisions on re-ingest.
    """
    if STORE_PATH.exists() and not force:
        console.print(f"[yellow]channels.json already exists.[/yellow] Use --force to re-ingest.")
        console.print(f"Run [bold]wrangle curate[/bold] to open the curation TUI.")
        raise typer.Exit()

    console.print("[dim]Running pipeline...[/dim]")
    normalized = _run_pipeline(source)

    existing = load_store() if STORE_PATH.exists() else []
    probe_cache = load_probe_cache()
    records = build_store(normalized, existing, probe_cache=probe_cache)
    save_store(records)

    s = store_summary(records)
    console.print(f"\n[bold green]Ingested {s['total']} channels → data/channels.json[/bold green]")

    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("Group", style="cyan")
    table.add_column("Channels", style="yellow", justify="right")
    for group, stats in s["by_group"].items():
        table.add_row(group, str(stats["total"]))
    console.print(table)
    console.print(f"\nRun [bold]wrangle curate[/bold] to start curation.")


@app.command()
def curate():
    """Open the TUI to include/exclude channels."""
    if not STORE_PATH.exists():
        console.print("[red]No channels.json found.[/red] Run [bold]wrangle ingest[/bold] first.")
        raise typer.Exit(1)
    from tui.app import run_tui
    run_tui(STORE_PATH)


@app.command()
def inspect(
    source: Annotated[
        Optional[Path],
        typer.Argument(help="M3U file to inspect. Uses provider URL if omitted."),
    ] = None,
):
    """
    Browse raw feed variants pre-normalization — probe quality and build probe cache.

    Type a channel name (e.g. 'eurosport') to filter all matching variants.
    Probe individual streams with p, mark with Space, bulk-probe marked with b,
    or probe everything visible with a. Results persist to data/probe_cache.json
    and are applied automatically on the next `wrangle ingest`.
    """
    if source is None:
        try:
            import yaml
            cfg = yaml.safe_load(Path("config/config.local.yaml").read_text())
            url = cfg["provider"]["url"]
        except Exception:
            console.print("[red]No source file provided and config/config.local.yaml not found.[/red]")
            raise typer.Exit(1)
        console.print("[dim]Fetching from provider...[/dim]")
        import httpx
        response = httpx.get(url, timeout=60, follow_redirects=True)
        response.raise_for_status()
        channels = parse_m3u_list(response.text)
    else:
        console.print(f"[dim]Parsing {source}...[/dim]")
        channels = parse_m3u_list(source)

    rules = load_group_rules()
    group_map = build_group_map(rules)
    filtered = filter_channels(channels, group_map)

    norm_rules = load_normalization_rules()

    from streamwrangler.normalizer import clean_name, detect_quality, detect_codec_hint, is_separator
    from streamwrangler.probe_cache import extract_channel_id, get_cached_probe
    from tui.inspect import InspectEntry, run_inspect_tui

    cache = load_probe_cache()

    entries: list[InspectEntry] = []
    for raw, target_group in filtered:
        if is_separator(raw.display_name):
            continue
        normalized_name = clean_name(raw.display_name, norm_rules)
        if not normalized_name:
            continue

        entry = InspectEntry(
            raw_name=raw.display_name,
            normalized_name=normalized_name,
            source_group=raw.group_title,
            target_group=target_group,
            url=raw.url,
            channel_id=extract_channel_id(raw.url),
            detected_quality=detect_quality(raw.display_name),
            detected_codec=detect_codec_hint(raw.display_name),
        )

        # Pre-populate from probe cache so previously-probed channels show results
        cached = get_cached_probe(raw.url, cache)
        if cached:
            entry.probe_quality = cached.get("quality", "")
            entry.probe_codec = cached.get("codec", "")
            entry.probe_width = cached.get("width", 0)
            entry.probe_height = cached.get("height", 0)
            entry.probe_bitrate_kbps = cached.get("bitrate_kbps", 0)
            entry.probed = True

        entries.append(entry)

    console.print(
        f"[bold green]{len(entries):,} channels loaded[/bold green] "
        f"([dim]{sum(1 for e in entries if e.probed)} previously probed[/dim]) "
        f"— opening inspector…"
    )
    run_inspect_tui(entries, cache, CACHE_PATH)


@app.command()
def number(
    generate: Annotated[bool, typer.Option("--generate", help="Re-generate AI proposal even if numbering.yaml exists")] = False,
    apply: Annotated[bool, typer.Option("--apply", help="Apply numbering.yaml to channels.json without opening TUI")] = False,
):
    """
    Assign channel numbers via AI proposal + interactive TUI.

    First run: calls Claude API to propose a logical numbered lineup, saves
    config/numbering.yaml, then opens the TUI to review and adjust.

    Subsequent runs: opens existing numbering.yaml directly in the TUI.

    Use --generate to force a fresh AI proposal (overwrites existing numbering.yaml).
    Use --apply to write numbers and display names to channels.json without the TUI.
    """
    if not STORE_PATH.exists():
        console.print("[red]No channels.json found.[/red] Run [bold]wrangle ingest[/bold] first.")
        raise typer.Exit(1)

    channels = load_store()
    included_count = sum(1 for c in channels if c.status == "included")
    if included_count == 0:
        console.print("[yellow]No included channels found.[/yellow] Run [bold]wrangle curate[/bold] first.")
        raise typer.Exit(1)

    plan = None

    if not generate and NUMBERING_PATH.exists():
        plan = load_numbering()
        if plan:
            console.print(
                f"[dim]Loaded existing numbering.yaml — "
                f"{sum(len(b.channels) for b in plan.blocks)} channels across "
                f"{len(plan.blocks)} blocks.[/dim]"
            )
            fixed_blocks = fix_block_starts(plan)
            if fixed_blocks:
                console.print(
                    f"[yellow]Renumbered block(s) to correct start positions: {', '.join(fixed_blocks)}[/yellow]"
                )
            new_count = merge_new_channels(plan, channels)
            if new_count:
                console.print(
                    f"[yellow]{new_count} new channel(s) added to plan (placed at end of their block) "
                    f"— reposition in TUI if needed.[/yellow]"
                )
            if fixed_blocks or new_count:
                save_numbering(plan)

    if plan is None:
        console.print(
            f"[dim]Calling Claude API to propose numbering for {included_count} included channels…[/dim]"
        )
        try:
            from streamwrangler.ai_numbering import propose_numbering
            plan = propose_numbering(channels)
        except Exception as e:
            console.print(f"[red]AI proposal failed:[/red] {e}")
            raise typer.Exit(1)

        save_numbering(plan)
        console.print(
            f"[bold green]AI proposed {sum(len(b.channels) for b in plan.blocks)} channels "
            f"across {len(plan.blocks)} blocks → config/numbering.yaml[/bold green]"
        )

    if apply:
        updated = apply_numbering(plan, channels)
        save_store(channels)
        console.print(f"[bold green]Applied {updated} channel numbers to channels.json[/bold green]")
        return

    from tui.number import run_number_tui
    run_number_tui(plan, STORE_PATH)


@app.command()
def report(
    group: Annotated[
        Optional[str],
        typer.Option("--group", "-g", help="Filter to a target group (partial match, case-insensitive)"),
    ] = None,
):
    """
    Report all included channels — normalized name, raw provider name, source group.
    Useful for cross-referencing EPG sources.
    Grouped by target group. Use --group to filter to one group.
    """
    if not STORE_PATH.exists():
        console.print("[red]No channels.json found.[/red] Run [bold]wrangle ingest[/bold] first.")
        raise typer.Exit(1)

    from streamwrangler.probe_cache import extract_channel_id

    records = load_store()
    included = [r for r in records if r.status == "included"]

    if group:
        included = [r for r in included if group.lower() in r.target_group.lower()]

    if not included:
        console.print("[yellow]No included channels found.[/yellow]")
        raise typer.Exit()

    # Group by target_group preserving order
    by_group: dict[str, list] = {}
    for r in included:
        by_group.setdefault(r.target_group, []).append(r)

    total = 0
    for target_group, channels in sorted(by_group.items()):
        table = Table(
            title=f"{target_group}  ({len(channels)})",
            box=box.SIMPLE_HEAVY,
            show_lines=False,
            title_style="bold cyan",
        )
        table.add_column("Display Name",      style="white",   max_width=32)
        table.add_column("Raw Provider Name", style="dim",    max_width=36)
        table.add_column("Source Group",      style="yellow", max_width=28)
        table.add_column("Channel ID",         style="dim",    max_width=12, no_wrap=True)

        for ch in channels:
            cid = "/" + extract_channel_id(ch.url)
            table.add_row(ch.display_name, ch.raw_display_name, ch.source_group, cid)

        console.print(table)
        total += len(channels)

    console.print(f"[dim]Total: {total} included channels[/dim]")


@app.command()
def epg(
    path: Annotated[
        Optional[Path],
        typer.Argument(help="Output path. Defaults to Dispatcharr EPG path."),
    ] = None,
):
    """Generate XMLTV EPG — Tennis PPV from channel names, sports teams from TheSportsDB."""
    if not STORE_PATH.exists():
        console.print("[red]No channels.json found.[/red] Run [bold]wrangle ingest[/bold] first.")
        raise typer.Exit(1)

    from streamwrangler.epg import (
        write_epg, EPG_PATH,
        write_sports_epg, SPORTS_EPG_PATH,
        write_paramount_epg, PARAMOUNT_EPG_PATH,
        write_logos_epg, LOGOS_EPG_PATH,
    )

    # Sports EPG first — fetch live match data before any tennis ranking API calls
    # that might exhaust the free-tier rate limit.
    sports_count = write_sports_epg(SPORTS_EPG_PATH)
    if sports_count:
        console.print(f"[bold green]Sports EPG:[/bold green] {SPORTS_EPG_PATH}  ({sports_count} teams)")
    else:
        console.print("[dim]Sports EPG: no config/sportsdb.yaml found — skipped[/dim]")

    tennis_path = path or EPG_PATH
    tennis_count = write_epg(tennis_path)
    console.print(f"[bold green]Tennis EPG:[/bold green] {tennis_path}  ({tennis_count} channels)")

    paramount_count = write_paramount_epg(PARAMOUNT_EPG_PATH)
    if paramount_count:
        console.print(f"[bold green]Paramount+ EPG:[/bold green] {PARAMOUNT_EPG_PATH}  ({paramount_count} channels)")
    else:
        console.print("[dim]Paramount+ EPG: no included Paramount+ PPV channels — skipped[/dim]")

    logos_count = write_logos_epg(LOGOS_EPG_PATH)
    console.print(f"[bold green]Logos EPG:[/bold green]     {LOGOS_EPG_PATH}  ({logos_count} channels)")


@app.command()
def logos(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Match only — do not download or update channels.json")] = False,
    overwrite: Annotated[bool, typer.Option("--overwrite", help="Re-download logos even if file already exists")] = False,
    push: Annotated[bool, typer.Option("--push", help="Push local logos to Dispatcharr via its REST API")] = False,
    dispatcharr_url: Annotated[str, typer.Option("--dispatcharr-url", help="Dispatcharr base URL")] = "http://10.0.1.39:9191",
    dispatcharr_user: Annotated[str, typer.Option("--dispatcharr-user", help="Dispatcharr username")] = "admin",
    dispatcharr_pass: Annotated[str, typer.Option("--dispatcharr-pass", help="Dispatcharr password")] = "admin",
):
    """
    Download channel logos from tv-logo/tv-logos and update channels.json.

    Matches included channels to the tv-logo repo by normalizing display names.
    Logos are resized/padded to 512×512 transparent PNG and served via Caddy.
    Prints a summary of matches and an unmatched list for manual review.

    Use --push to also update Dispatcharr via its REST API so every channel
    immediately shows the correct logo without manual configuration.
    """
    if not STORE_PATH.exists():
        console.print("[red]No channels.json found.[/red] Run [bold]wrangle ingest[/bold] first.")
        raise typer.Exit(1)

    if push:
        from streamwrangler.logos import push_logos
        console.print("[dim]Connecting to Dispatcharr and pushing logos...[/dim]")
        updated, already_ok, skipped = push_logos(
            base_url=dispatcharr_url,
            username=dispatcharr_user,
            password=dispatcharr_pass,
            dry_run=dry_run,
        )
        console.print(
            f"\n[bold green]Updated:[/bold green]    {len(updated)}\n"
            f"[bold cyan]Already OK:[/bold cyan]  {len(already_ok)}\n"
            f"[bold yellow]Skipped:[/bold yellow]    {len(skipped)}\n"
        )
        if updated:
            table = Table(title="Logo Updated in Dispatcharr", box=box.SIMPLE_HEAVY)
            table.add_column("Channel", style="green", max_width=55)
            for label in sorted(updated):
                table.add_row(label)
            console.print(table)
        if skipped:
            table = Table(title="Skipped — no Dispatcharr match or no local logo", box=box.SIMPLE_HEAVY)
            table.add_column("Channel", style="yellow", max_width=65)
            for label in sorted(skipped):
                table.add_row(label)
            console.print(table)
        return

    from streamwrangler.logos import run_logos

    console.print("[dim]Fetching logo index from tv-logo/tv-logos...[/dim]")
    matched, unmatched = run_logos(dry_run=dry_run, overwrite=overwrite)

    console.print(
        f"\n[bold green]Matched:[/bold green]   {len(matched)}\n"
        f"[bold yellow]Unmatched:[/bold yellow] {len(unmatched)}\n"
    )

    if matched:
        table = Table(title="Matched Logos", box=box.SIMPLE_HEAVY)
        table.add_column("Channel",  style="white",  max_width=36)
        table.add_column("File",     style="cyan",   max_width=32)
        table.add_column("Group",    style="dim",    max_width=24)
        for ch, filename in matched:
            table.add_row(ch.display_name, filename, ch.target_group)
        console.print(table)

    if unmatched:
        table = Table(title="Unmatched — no logo found", box=box.SIMPLE_HEAVY)
        table.add_column("Channel",  style="yellow", max_width=36)
        table.add_column("Group",    style="dim",    max_width=24)
        for ch in unmatched:
            table.add_row(ch.display_name, ch.target_group)
        console.print(table)

    if not dry_run and matched:
        console.print(f"[dim]channels.json updated — run [bold]wrangle output[/bold] to regenerate M3U.[/dim]")


@app.command()
def output(
    path: Annotated[
        Optional[Path],
        typer.Argument(help="Output path. Defaults to Dispatcharr M3U path."),
    ] = None,
):
    """Write included, numbered channels to M3U for Dispatcharr."""
    if not STORE_PATH.exists():
        console.print("[red]No channels.json found.[/red] Run [bold]wrangle ingest[/bold] first.")
        raise typer.Exit(1)

    from streamwrangler.output import write_output, OUTPUT_PATH
    out_path = path or OUTPUT_PATH
    count = write_output(out_path)
    console.print(f"[bold green]Wrote {count} channels → {out_path}[/bold green]")


@app.command()
def status():
    """Show current curation progress and pipeline timestamps."""
    if not STORE_PATH.exists():
        console.print("[red]No channels.json found.[/red] Run [bold]wrangle ingest[/bold] first.")
        raise typer.Exit(1)

    import datetime
    from zoneinfo import ZoneInfo
    from streamwrangler.output import OUTPUT_PATH
    from streamwrangler.epg import EPG_PATH, SPORTS_EPG_PATH, LOGOS_EPG_PATH
    from streamwrangler.probe_cache import CACHE_PATH

    _LOCAL_TZ = ZoneInfo("America/Chicago")

    def _age(path: Path) -> str:
        if not path.exists():
            return "[dim]not found[/dim]"
        mtime_utc = datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.timezone.utc)
        mtime_local = mtime_utc.astimezone(_LOCAL_TZ)
        age = datetime.datetime.now(datetime.timezone.utc) - mtime_utc
        total_seconds = int(age.total_seconds())
        label = mtime_local.strftime("%Y-%m-%d %I:%M %p")
        if total_seconds < 60:
            return f"[green]{total_seconds}s ago[/green]  ({label})"
        elif total_seconds < 3600:
            return f"[green]{total_seconds // 60}m ago[/green]  ({label})"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            return f"[yellow]{hours}h ago[/yellow]  ({label})"
        else:
            days = total_seconds // 86400
            return f"[red]{days}d ago[/red]  ({label})"

    console.print()

    # Pipeline timestamps
    ts_table = Table(box=box.SIMPLE_HEAVY, show_header=False)
    ts_table.add_column("Label", style="cyan", width=20)
    ts_table.add_column("Value")
    ts_table.add_row("Last ingest",    _age(STORE_PATH))
    ts_table.add_row("Last output",    _age(OUTPUT_PATH))
    ts_table.add_row("Tennis EPG",     _age(EPG_PATH))
    ts_table.add_row("Sports EPG",     _age(SPORTS_EPG_PATH))
    ts_table.add_row("Logos EPG",      _age(LOGOS_EPG_PATH))
    ts_table.add_row("Probe cache",    _age(CACHE_PATH))
    console.print(ts_table)

    # Probe cache size
    if CACHE_PATH.exists():
        import json as _json
        probe_count = len(_json.loads(CACHE_PATH.read_text()))
        console.print(f"  [dim]Probe cache: {probe_count:,} entries[/dim]\n")

    records = load_store()
    s = store_summary(records)

    # Curation summary
    console.print(f"  Total:    {s['total']}")
    console.print(f"  [green]Included: {s['included']}[/green]")
    console.print(f"  [red]Excluded: {s['excluded']}[/red]")
    console.print(f"  [dim]Pending:  {s['pending']}[/dim]")
    console.print(f"  Numbered: {s['numbered']}\n")

    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("Group", style="cyan")
    table.add_column("Total", justify="right")
    table.add_column("✓ In", style="green", justify="right")
    table.add_column("✗ Out", style="red", justify="right")
    table.add_column("· Pending", style="dim", justify="right")

    for group, stats in s["by_group"].items():
        table.add_row(
            group,
            str(stats["total"]),
            str(stats["included"]),
            str(stats["excluded"]),
            str(stats["pending"]),
        )
    console.print(table)

