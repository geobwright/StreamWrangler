"""Filter engine — applies groups.yaml to reduce a raw feed to the curated set."""

from pathlib import Path
from dataclasses import dataclass

import yaml

from .parser import RawChannel


@dataclass
class GroupRule:
    source_group: str
    target_group: str
    enabled: bool
    seasonal: bool = False
    notes: str = ""


def load_group_rules(config_path: Path | str = "config/groups.yaml") -> list[GroupRule]:
    """Load group mapping rules from YAML config."""
    data = yaml.safe_load(Path(config_path).read_text())
    rules = []
    for entry in data.get("groups", []):
        rules.append(GroupRule(
            source_group=entry["source_group"],
            target_group=entry["target_group"],
            enabled=entry.get("enabled", True),
            seasonal=entry.get("seasonal", False),
            notes=entry.get("notes", ""),
        ))
    return rules


def build_group_map(
    rules: list[GroupRule],
    include_seasonal: bool = False,
) -> dict[str, str]:
    """
    Build a lookup dict: source_group_title -> target_group_name.
    Only includes enabled rules (and seasonal if include_seasonal=True).
    Excludes rules targeting '_excluded'.
    """
    mapping = {}
    for rule in rules:
        if not rule.enabled:
            continue
        if rule.seasonal and not include_seasonal:
            continue
        if rule.target_group == "_excluded":
            continue
        mapping[rule.source_group] = rule.target_group
    return mapping


def filter_channels(
    channels: list[RawChannel],
    group_map: dict[str, str],
) -> list[tuple[RawChannel, str]]:
    """
    Filter channels to only those whose source group is in the group_map.
    Returns list of (RawChannel, target_group) tuples.
    """
    result = []
    for ch in channels:
        target = group_map.get(ch.group_title)
        if target:
            result.append((ch, target))
    return result


def filter_summary(
    channels: list[RawChannel],
    group_map: dict[str, str],
) -> dict:
    """Return a summary dict of filter results for reporting."""
    filtered = filter_channels(channels, group_map)

    by_target: dict[str, int] = {}
    for _, tg in filtered:
        by_target[tg] = by_target.get(tg, 0) + 1

    unmapped_groups: dict[str, int] = {}
    for ch in channels:
        if ch.group_title not in group_map:
            unmapped_groups[ch.group_title] = unmapped_groups.get(ch.group_title, 0) + 1

    return {
        "total_input": len(channels),
        "total_output": len(filtered),
        "by_target_group": dict(sorted(by_target.items())),
        "unmapped_group_count": len(unmapped_groups),
        "top_unmapped": sorted(unmapped_groups.items(), key=lambda x: -x[1])[:20],
    }
