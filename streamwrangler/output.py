"""
M3U output writer — generates a clean M3U from included, numbered channels.

Writes only channels with status='included' and a channel_number assigned.
Sorted by channel_number ascending.

Output path: /home/geoffrey/infra/compose/dispatcharr/data/m3us/streamwrangler.m3u
"""

from pathlib import Path

from .store import ChannelRecord, load_store

OUTPUT_PATH = Path("/home/geoffrey/infra/compose/dispatcharr/data/m3us/streamwrangler.m3u")


def build_m3u(channels: list[ChannelRecord]) -> str:
    """Build M3U content from a list of channel records."""
    lines = ["#EXTM3U"]

    eligible = sorted(
        (c for c in channels if c.status == "included" and c.channel_number is not None),
        key=lambda c: c.channel_number,
    )

    for ch in eligible:
        attrs = f'tvg-id="{ch.tvg_id}"'
        if ch.tvg_logo:
            attrs += f' tvg-logo="{ch.tvg_logo}"'
        attrs += f' tvg-chno="{ch.channel_number}"'
        attrs += f' group-title="{ch.target_group}"'

        lines.append(f"#EXTINF:-1 {attrs},{ch.display_name}")
        lines.append(ch.url)

    return "\n".join(lines) + "\n"


def write_output(path: Path = OUTPUT_PATH) -> int:
    """Load channels.json and write M3U to path. Returns number of channels written."""
    channels = load_store()
    content = build_m3u(channels)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    return sum(1 for c in channels if c.status == "included" and c.channel_number is not None)
