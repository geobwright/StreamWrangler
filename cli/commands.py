"""StreamWrangler CLI — entry point: `wrangle`"""

from collections import Counter
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import box

from streamwrangler.parser import parse_m3u_list
from streamwrangler.filter import load_group_rules, build_group_map, filter_summary

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
