"""
Channel store — manages channels.json, the canonical channel state file.
Handles initial population from normalized channels and persistence of
include/exclude/number decisions across sessions.
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal

from .normalizer import NormalizedChannel

STATUS = Literal["pending", "included", "excluded"]
STORE_PATH = Path("data/channels.json")


@dataclass
class ChannelRecord:
    channel_uid: str
    display_name: str
    raw_display_name: str
    target_group: str
    source_group: str
    tvg_id: str
    tvg_logo: str
    url: str
    quality: str = ""
    quality_verified: bool = False
    status: STATUS = "pending"
    channel_number: int | None = None
    cuid: str = ""

    def is_decided(self) -> bool:
        return self.status != "pending"


def load_store(path: Path = STORE_PATH) -> list[ChannelRecord]:
    """Load channels.json. Returns empty list if file doesn't exist."""
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [ChannelRecord(**ch) for ch in data]


def save_store(channels: list[ChannelRecord], path: Path = STORE_PATH) -> None:
    """Write channels to channels.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(c) for c in channels], indent=2))


def build_store(
    normalized: list[NormalizedChannel],
    existing: list[ChannelRecord] | None = None,
) -> list[ChannelRecord]:
    """
    Build a channel store from normalized channels.
    If existing records are provided, preserves status/channel_number decisions
    for channels that are still present (matched by channel_uid).
    New channels that weren't in existing store get status='pending'.
    """
    existing_map = {r.channel_uid: r for r in (existing or [])}
    records = []

    for ch in normalized:
        if ch.channel_uid in existing_map:
            # Preserve existing decisions, but update mutable fields
            # (URL may change on provider refresh; logo/tvg_id pass through)
            old = existing_map[ch.channel_uid]
            records.append(ChannelRecord(
                channel_uid=ch.channel_uid,
                display_name=old.display_name,
                raw_display_name=ch.raw_display_name,
                target_group=ch.target_group,
                source_group=ch.source_group,
                tvg_id=ch.tvg_id,
                tvg_logo=ch.tvg_logo,
                url=ch.url,
                quality=ch.quality,
                status=old.status,
                channel_number=old.channel_number,
                cuid=ch.cuid,
            ))
        else:
            records.append(ChannelRecord(
                channel_uid=ch.channel_uid,
                display_name=ch.display_name,
                raw_display_name=ch.raw_display_name,
                target_group=ch.target_group,
                source_group=ch.source_group,
                tvg_id=ch.tvg_id,
                tvg_logo=ch.tvg_logo,
                url=ch.url,
                quality=ch.quality,
                cuid=ch.cuid,
            ))

    return records


def store_summary(channels: list[ChannelRecord]) -> dict:
    """Summary stats for display."""
    total = len(channels)
    included = sum(1 for c in channels if c.status == "included")
    excluded = sum(1 for c in channels if c.status == "excluded")
    pending = sum(1 for c in channels if c.status == "pending")
    numbered = sum(1 for c in channels if c.channel_number is not None)

    by_group: dict[str, dict] = {}
    for c in channels:
        g = by_group.setdefault(c.target_group, {"total": 0, "included": 0, "excluded": 0, "pending": 0})
        g["total"] += 1
        g[c.status] += 1

    return {
        "total": total,
        "included": included,
        "excluded": excluded,
        "pending": pending,
        "numbered": numbered,
        "by_group": dict(sorted(by_group.items())),
    }
