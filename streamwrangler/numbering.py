"""
Channel numbering — YAML-backed number assignment and display name generation.

Workflow:
  wrangle number           → AI proposes numbering.yaml, open TUI to review
  wrangle number --apply   → write numbers + display names to channels.json
"""

from dataclasses import dataclass, field
from pathlib import Path

import re

import yaml

from .store import ChannelRecord

NUMBERING_PATH = Path("config/numbering.yaml")

# Source group prefixes that are NOT English → get a [LANG] tag appended to display name.
# UK, US, AU, CA, IE are English — no tag.
_LANG_PREFIXES: dict[str, str] = {
    "FR|": "FR",
    "DE|": "DE",
    "ES|": "ES",
    "IT|": "IT",
    "PT|": "PT",
    "NL|": "NL",
    "AR|": "AR",
    "TR|": "TR",
    "PL|": "PL",
    "RU|": "RU",
    "GR|": "GR",
    "SE|": "SE",
    "NO|": "NO",
    "DK|": "DK",
    "FI|": "FI",
    "HU|": "HU",
    "CZ|": "CZ",
    "SK|": "SK",
    "HR|": "HR",
    "RO|": "RO",
    "BG|": "BG",
    "AL|": "AL",
    "RS|": "RS",
}


@dataclass
class NumberedChannel:
    uid: str
    number: int
    display_name: str


@dataclass
class NumberingBlock:
    name: str
    start: int
    channels: list[NumberedChannel] = field(default_factory=list)


@dataclass
class NumberingPlan:
    blocks: list[NumberingBlock] = field(default_factory=list)

    def all_channels(self) -> list[tuple[NumberingBlock, NumberedChannel]]:
        """Flat list of (block, channel) pairs across all blocks."""
        return [(block, ch) for block in self.blocks for ch in block.channels]

    def find_channel(self, uid: str) -> tuple[NumberingBlock, NumberedChannel] | None:
        for block in self.blocks:
            for ch in block.channels:
                if ch.uid == uid:
                    return block, ch
        return None


def detect_language_tag(source_group: str) -> str:
    """Return a language tag (e.g. 'FR') if source_group is non-English, else ''."""
    for prefix, lang in _LANG_PREFIXES.items():
        if source_group.startswith(prefix):
            return lang
    return ""


def build_output_display_name(ch: ChannelRecord) -> str:
    """
    Build the final output display name for a channel.

    Format: '<base> <quality> [LANG]'
    Examples: 'Eurosport 1 FHD', 'Eurosport 1 HD [FR]', 'BBC One 4K'

    Quality: probe-verified if available, else advertised. Omitted if empty.
    Language tag: appended for non-English source groups only.
    """
    base = ch.display_name
    quality = ch.quality or ch.advertised_quality
    lang = detect_language_tag(ch.source_group)

    parts = [base]
    if quality:
        parts.append(quality)
    if lang:
        parts.append(f"[{lang}]")

    return " ".join(parts)


def load_numbering(path: Path = NUMBERING_PATH) -> NumberingPlan | None:
    """Load numbering.yaml. Returns None if file doesn't exist."""
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text())
    if not data or "blocks" not in data:
        return None

    blocks = []
    for b in data["blocks"]:
        channels = [
            NumberedChannel(uid=ch["uid"], number=ch["number"], display_name=ch["display_name"])
            for ch in b.get("channels", [])
        ]
        blocks.append(NumberingBlock(name=b["name"], start=b["start"], channels=channels))
    return NumberingPlan(blocks=blocks)


def save_numbering(plan: NumberingPlan, path: Path = NUMBERING_PATH) -> None:
    """Write numbering plan to YAML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "blocks": [
            {
                "name": block.name,
                "start": block.start,
                "channels": [
                    {"uid": ch.uid, "number": ch.number, "display_name": ch.display_name}
                    for ch in block.channels
                ],
            }
            for block in plan.blocks
        ]
    }
    path.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False))


# Start numbers for target groups not in the standard AI block list.
# Used when merge_new_channels needs to create a block from scratch.
_FALLBACK_BLOCK_STARTS: dict[str, int] = {
    "Paramount+ PPV": 1100,
}


def rebase_block(block: NumberingBlock, new_start: int) -> None:
    """
    Renumber all channels in a block sequentially from new_start.
    PPV blocks (no gaps): new_start, new_start+1, new_start+2, ...
    Non-PPV blocks (5-number gaps): new_start+5, new_start+10, ...
    """
    is_ppv = "PPV" in block.name
    step = 1 if is_ppv else 5
    for i, ch in enumerate(block.channels):
        ch.number = new_start + i * step
    block.start = new_start


def fix_block_starts(plan: NumberingPlan) -> list[str]:
    """
    Detect blocks whose channel numbers don't match their registered start in
    _FALLBACK_BLOCK_STARTS and rebase them. Returns list of block names that were fixed.

    This corrects numbering.yaml entries created before a start-number change.
    """
    fixed = []
    for block in plan.blocks:
        expected_start = _FALLBACK_BLOCK_STARTS.get(block.name)
        if expected_start is None or not block.channels:
            continue
        current_min = min(ch.number for ch in block.channels)
        if current_min != expected_start:
            rebase_block(block, expected_start)
            fixed.append(block.name)
    return fixed


def merge_new_channels(plan: NumberingPlan, channels: list[ChannelRecord]) -> int:
    """
    Find included channels not yet in the plan and append them to the end of
    their matching block (matched by target_group == block.name).

    If no matching block exists, creates one using _FALLBACK_BLOCK_STARTS (or 800
    as a catch-all) so truly new groups still appear in the TUI.

    Returns the count of newly added channels.
    """
    planned_uids = {ch.uid for _, ch in plan.all_channels()}
    block_by_name = {b.name: b for b in plan.blocks}
    added = 0

    for ch in channels:
        if ch.status != "included":
            continue
        if ch.channel_uid in planned_uids:
            continue

        block = block_by_name.get(ch.target_group)
        if block is None:
            start = _FALLBACK_BLOCK_STARTS.get(ch.target_group, 800)
            block = NumberingBlock(name=ch.target_group, start=start)
            plan.blocks.append(block)
            block_by_name[ch.target_group] = block

        is_ppv = "PPV" in block.name
        if block.channels:
            last_num = max(c.number for c in block.channels)
            next_num = last_num + (1 if is_ppv else 5)
        else:
            next_num = block.start + (0 if is_ppv else 5)

        block.channels.append(NumberedChannel(
            uid=ch.channel_uid,
            number=next_num,
            display_name=build_output_display_name(ch),
        ))
        planned_uids.add(ch.channel_uid)
        added += 1

    return added


def apply_numbering(plan: NumberingPlan, channels: list[ChannelRecord]) -> int:
    """
    Apply numbering plan to channel records in place.
    Sets channel_number and display_name on matched records.
    Returns count of channels updated.

    PPV blocks (block name contains "PPV") are handled specially: when no UIDs in the
    plan match current channels (daily schedules replace all channels), falls back to
    sequentially numbering all included channels in that target_group starting from
    block.start. UIDs in the feed are numeric and descend in schedule order, so sorting
    by int(uid) descending preserves the provider's schedule ordering.
    """
    uid_map = {ch.channel_uid: ch for ch in channels}

    # Build target_group → included channels map for PPV fallback
    group_channels: dict[str, list[ChannelRecord]] = {}
    for ch in channels:
        if ch.status == "included":
            group_channels.setdefault(ch.target_group, []).append(ch)

    updated = 0
    for block in plan.blocks:
        matching = [entry for entry in block.channels if entry.uid in uid_map]

        if "PPV" in block.name and not matching:
            # Stale PPV plan — schedule changed since numbering.yaml was generated.
            # Auto-assign sequential numbers to current included channels in this group.
            candidates = group_channels.get(block.name, [])
            # Sort by the trailing ordinal number in the display name — the provider
            # embeds a sequence number (e.g. ":Tennis  01") that is the canonical order.
            # UID ordering is unreliable because streams are added in batches.
            def _ppv_sort_key(ch: ChannelRecord) -> int:
                m = re.search(r"(\d+)\s*$", ch.display_name)
                return int(m.group(1)) if m else 0
            candidates = sorted(candidates, key=_ppv_sort_key)
            for i, ch in enumerate(candidates):
                ch.channel_number = block.start + i
                updated += 1
        else:
            for entry in block.channels:
                if entry.uid in uid_map:
                    uid_map[entry.uid].channel_number = entry.number
                    uid_map[entry.uid].display_name = entry.display_name
                    updated += 1
    return updated
