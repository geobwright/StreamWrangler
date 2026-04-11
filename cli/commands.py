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
from streamwrangler.numbering import load_numbering, save_numbering, apply_numbering, NUMBERING_PATH

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
def status():
    """Show current curation progress."""
    if not STORE_PATH.exists():
        console.print("[red]No channels.json found.[/red] Run [bold]wrangle ingest[/bold] first.")
        raise typer.Exit(1)

    records = load_store()
    s = store_summary(records)

    console.print(f"\n[bold]Curation status[/bold]\n")
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
