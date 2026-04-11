"""
Channel store — manages channels.json, the canonical channel state file.
Handles initial population from normalized channels and persistence of
include/exclude/number decisions across sessions.
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal

from .normalizer import NormalizedChannel
from .probe_cache import get_cached_probe

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
    quality: str = ""              # actual quality — updated by probe
    advertised_quality: str = ""  # quality detected from channel name
    quality_verified: bool = False
    codec: str = ""               # actual codec from probe (e.g. h264, hevc)
    advertised_codec: str = ""    # codec detected from channel name (e.g. hevc)
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
    records = []
    for ch in data:
        # Migration: HEVC was previously stored as a quality tier; move it to codec
        is_hevc = ch.get("advertised_quality") == "HEVC" or ch.get("quality") == "HEVC"
        if is_hevc and not ch.get("advertised_codec"):
            ch["advertised_codec"] = "hevc"
        if ch.get("advertised_quality") == "HEVC":
            ch["advertised_quality"] = "HD"
        if ch.get("quality") == "HEVC":
            ch["quality"] = "HD"
        records.append(ChannelRecord(**ch))
    return records


def save_store(channels: list[ChannelRecord], path: Path = STORE_PATH) -> None:
    """Write channels to channels.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(c) for c in channels], indent=2))


def build_store(
    normalized: list[NormalizedChannel],
    existing: list[ChannelRecord] | None = None,
    probe_cache: dict[str, Any] | None = None,
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
            # Apply probe cache to records not yet verified (e.g. probed in inspect)
            if probe_cache and not old.quality_verified:
                cached = get_cached_probe(ch.url, probe_cache)
            else:
                cached = None
            # PPV channels: always use the fresh name from the feed — provider
            # updates these to reflect the current fixture/event.
            is_ppv = "PPV" in ch.target_group
            records.append(ChannelRecord(
                channel_uid=ch.channel_uid,
                display_name=ch.display_name if is_ppv else old.display_name,
                raw_display_name=ch.raw_display_name,
                target_group=ch.target_group,
                source_group=ch.source_group,
                tvg_id=ch.tvg_id,
                tvg_logo=ch.tvg_logo,
                url=ch.url,
                quality=cached["quality"] if cached else old.quality,
                advertised_quality=ch.quality,     # always re-derive from name detection
                quality_verified=True if cached else old.quality_verified,
                codec=cached.get("codec", "") if cached else old.codec,
                advertised_codec=ch.codec_hint,    # always re-derive from name
                status=old.status,
                channel_number=old.channel_number,
                cuid=ch.cuid,
            ))
        else:
            # New channel — apply probe cache if available
            cached = get_cached_probe(ch.url, probe_cache) if probe_cache else None
            records.append(ChannelRecord(
                channel_uid=ch.channel_uid,
                display_name=ch.display_name,
                raw_display_name=ch.raw_display_name,
                target_group=ch.target_group,
                source_group=ch.source_group,
                tvg_id=ch.tvg_id,
                tvg_logo=ch.tvg_logo,
                url=ch.url,
                quality=cached["quality"] if cached else ch.quality,
                advertised_quality=ch.quality,
                quality_verified=bool(cached),
                codec=cached.get("codec", "") if cached else "",
                advertised_codec=ch.codec_hint,
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
