"""
M3U output writer — generates a clean M3U from included, numbered channels.

Writes only channels with status='included' and a channel_number assigned.
Sorted by channel_number ascending.

Output path: /home/geoffrey/infra/compose/dispatcharr/data/m3us/streamwrangler.m3u
"""

from pathlib import Path

from .epg import _slot, _paramount_slot
from .store import ChannelRecord, load_store

OUTPUT_PATH = Path("/home/geoffrey/infra/compose/dispatcharr/data/m3us/streamwrangler.m3u")


def _sports_epg_map() -> dict[str, str]:
    """Return {channel_uid: epg_id} from sportsdb.yaml, or empty dict if not configured."""
    try:
        from .sportsdb import load_sportsdb_config, channel_epg_map
        config = load_sportsdb_config()
        return channel_epg_map(config) if config else {}
    except Exception:
        return {}


def build_m3u(channels: list[ChannelRecord]) -> str:
    """Build M3U content from a list of channel records."""
    lines = ["#EXTM3U"]

    eligible = sorted(
        (c for c in channels if c.status == "included" and c.channel_number is not None),
        key=lambda c: c.channel_number,
    )

    sports_map = _sports_epg_map()

    for ch in eligible:
        if ch.target_group == "Tennis PPV":
            s = _slot(ch.raw_display_name)
            tvg_id = f"WrangleTennis{s:02d}" if s else ch.tvg_id
        elif ch.target_group == "Paramount+ PPV":
            s = _paramount_slot(ch.raw_display_name)
            tvg_id = f"WrangleParamount{s:02d}" if s else ch.tvg_id
        elif ch.channel_uid in sports_map:
            tvg_id = sports_map[ch.channel_uid]
        else:
            tvg_id = ch.tvg_id
        attrs = f'tvg-id="{tvg_id}"'
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
