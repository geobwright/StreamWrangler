"""
Probe result cache — persists ffprobe results keyed by stable channel ID.

The channel ID is the last path segment of the provider URL, which remains
stable even when the domain, port, or credentials rotate.

  http://provider.com/username/password/1537488
                                         ↑ channel ID (stable)
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CACHE_PATH = Path("data/probe_cache.json")


def extract_channel_id(url: str) -> str:
    """Return the stable channel ID from a provider URL (last non-empty path segment)."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def load_probe_cache(path: Path = CACHE_PATH) -> dict[str, Any]:
    """Load probe cache from disk. Returns empty dict if file doesn't exist."""
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_probe_cache(cache: dict[str, Any], path: Path = CACHE_PATH) -> None:
    """Write probe cache to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2))


def record_probe(
    url: str,
    quality: str,
    codec: str,
    width: int | None,
    height: int | None,
    bitrate_kbps: int | None,
    cache: dict[str, Any],
) -> str:
    """Store a probe result in the cache dict (mutates in-place). Returns the key used."""
    channel_id = extract_channel_id(url)
    entry: dict[str, Any] = {
        "quality": quality,
        "codec": codec,
        "probed_at": datetime.now(timezone.utc).isoformat(),
    }
    if width is not None:
        entry["width"] = width
    if height is not None:
        entry["height"] = height
    if bitrate_kbps is not None:
        entry["bitrate_kbps"] = bitrate_kbps
    cache[channel_id] = entry
    return channel_id


def get_cached_probe(url: str, cache: dict[str, Any]) -> dict[str, Any] | None:
    """Return cached probe result for a URL, or None if not found."""
    return cache.get(extract_channel_id(url))
