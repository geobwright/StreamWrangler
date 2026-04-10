"""Filter engine — applies groups.yaml to reduce a raw feed to the curated set."""

from pathlib import Path
from dataclasses import dataclass

import yaml

from .parser import RawChannel


@dataclass
class GroupRule:
    source_group: str        # exact match (or "" if prefix rule)
    source_group_prefix: str # prefix match (or "" if exact rule)
    target_group: str
    enabled: bool
    seasonal: bool = False
    notes: str = ""

    def matches(self, group_title: str) -> bool:
        if self.source_group_prefix:
            return group_title.startswith(self.source_group_prefix)
        return group_title == self.source_group


def load_group_rules(config_path: Path | str = "config/groups.yaml") -> list[GroupRule]:
    """Load group mapping rules from YAML config."""
    data = yaml.safe_load(Path(config_path).read_text())
    rules = []
    for entry in data.get("groups", []):
        rules.append(GroupRule(
            source_group=entry.get("source_group", ""),
            source_group_prefix=entry.get("source_group_prefix", ""),
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

    '_excluded' rules ARE included so they can block channels that would otherwise
    match a broader prefix rule. Exact rules take priority over prefix rules.
    filter_channels() skips any channel whose resolved target is '_excluded'.
    """
    exact: dict[str, str] = {}
    prefix_rules: list[GroupRule] = []

    for rule in rules:
        if not rule.enabled:
            continue
        if rule.seasonal and not include_seasonal:
            continue
        if rule.source_group:
            exact[rule.source_group] = rule.target_group
        elif rule.source_group_prefix:
            prefix_rules.append(rule)

    return {"__exact__": exact, "__prefix__": prefix_rules}  # type: ignore[return-value]


def _resolve_target(group_title: str, group_map: dict) -> str | None:
    """Look up a group title against exact and prefix rules."""
    exact = group_map.get("__exact__", {})
    if group_title in exact:
        return exact[group_title]
    for rule in group_map.get("__prefix__", []):
        if rule.matches(group_title):
            return rule.target_group
    return None


def filter_channels(
    channels: list[RawChannel],
    group_map: dict[str, str],
) -> list[tuple[RawChannel, str]]:
    """
    Filter channels to only those whose source group matches a rule.
    Returns list of (RawChannel, target_group) tuples.
    Channels resolving to '_excluded' are silently dropped.
    """
    result = []
    for ch in channels:
        target = _resolve_target(ch.group_title, group_map)
        if target and target != "_excluded":
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
        if _resolve_target(ch.group_title, group_map) is None:
            unmapped_groups[ch.group_title] = unmapped_groups.get(ch.group_title, 0) + 1

    return {
        "total_input": len(channels),
        "total_output": len(filtered),
        "by_target_group": dict(sorted(by_target.items())),
        "unmapped_group_count": len(unmapped_groups),
        "top_unmapped": sorted(unmapped_groups.items(), key=lambda x: -x[1])[:20],
    }
