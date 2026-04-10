"""
Normalization engine — cleans raw channel names, generates channel_uid,
and applies allow-list filtering to deduplicate high-volume groups.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .parser import RawChannel


@dataclass
class NormalizedChannel:
    """A channel after normalization — ready for curation and output."""
    channel_uid: str
    display_name: str          # Cleaned display name
    raw_display_name: str      # Original from provider
    target_group: str          # Our group (e.g. "US Sports")
    source_group: str          # Provider group (e.g. "US| SPORT ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ")
    tvg_id: str
    tvg_logo: str
    url: str
    cuid: str = ""             # Provider's internal ID


@dataclass
class NormalizationRules:
    strip_prefixes: list[str] = field(default_factory=list)
    strip_suffixes: list[str] = field(default_factory=list)
    strip_inline: list[str] = field(default_factory=list)
    replacements: list[tuple[str, str]] = field(default_factory=list)
    allow_lists: dict[str, list[str]] = field(default_factory=dict)


def load_normalization_rules(
    config_path: Path | str = "config/normalization.yaml",
) -> NormalizationRules:
    data = yaml.safe_load(Path(config_path).read_text())
    rules = NormalizationRules(
        strip_prefixes=data.get("strip_prefixes", []),
        strip_suffixes=data.get("strip_suffixes", []),
        strip_inline=data.get("strip_inline", []),
        replacements=[tuple(r) for r in data.get("replacements", [])],
        allow_lists=data.get("allow_lists", {}),
    )
    return rules


# Superscript/subscript unicode ranges to strip
_UNICODE_NOISE_RE = re.compile(
    r'[\u00b2-\u00b3\u00b9\u2070-\u209f\u1d00-\u1d7f\u1d80-\u1dbf'
    r'\u2c60-\u2c7f\ua720-\ua7ff\u24b6-\u24e9]+'
)
# Header/separator lines (lines that are all symbols)
_SEPARATOR_RE = re.compile(r'^[\s#=|*\-_]+$')


def _strip_unicode_noise(name: str) -> str:
    """Remove superscript, subscript, and other decorative unicode."""
    return _UNICODE_NOISE_RE.sub("", name).strip()


def clean_name(name: str, rules: NormalizationRules) -> str:
    """Apply all normalization rules to produce a clean channel name."""
    result = name.strip()

    # Strip prefixes
    for prefix in rules.strip_prefixes:
        if result.upper().startswith(prefix.upper()):
            result = result[len(prefix):].strip()
            break  # Only strip one prefix

    # Strip suffixes (loop — may need multiple passes for combos like "HD ◉")
    changed = True
    while changed:
        changed = False
        for suffix in rules.strip_suffixes:
            if result.upper().endswith(suffix.upper()):
                result = result[: -len(suffix)].strip()
                changed = True

    # Strip inline noise
    for noise in rules.strip_inline:
        result = result.replace(noise, "").strip()

    # Strip remaining unicode noise
    result = _strip_unicode_noise(result)

    # Apply word replacements
    for from_str, to_str in rules.replacements:
        result = result.replace(from_str, to_str)

    return result.strip()


def make_channel_uid(display_name: str, target_group: str) -> str:
    """
    Generate a stable, deterministic channel_uid from the canonical name + group.
    e.g. "ESPN" + "US Sports" -> "espn"
         "SKY SPORTS FOOTBALL" + "UK Sports" -> "sky_sports_football"
    """
    # Normalize unicode to ASCII equivalents where possible
    normalized = unicodedata.normalize("NFKD", display_name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")

    # Lowercase, replace non-alphanumeric with underscore, collapse runs
    uid = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower())
    uid = uid.strip("_")

    # For PPV/event channels that share a name prefix, append group slug
    group_slug = re.sub(r"[^a-z0-9]+", "_", target_group.lower()).strip("_")

    # Only append group for disambiguation if uid would collide across groups
    # (e.g. "eurosport_1" appears in both UK Sports and France Sports)
    ambiguous_names = {
        "eurosport_1", "eurosport_2", "bein_sports_1", "bein_sports_2",
        "canal_sport", "sky_news", "dazn_1",
    }
    if uid in ambiguous_names:
        uid = f"{uid}_{group_slug}"

    return uid


def is_separator(name: str) -> bool:
    """True if the display name is a section header/separator, not a real channel."""
    if _SEPARATOR_RE.match(name):
        return True
    # Names that are all caps symbols like "#### SPORT HD ####"
    stripped = re.sub(r"[#=|\-_*\s]", "", name)
    if not stripped:
        return True
    return False


def passes_allow_list(clean: str, target_group: str, allow_lists: dict) -> bool:
    """
    For groups with an allow list, check if the cleaned name matches any entry.
    Groups without an allow list pass everything through.
    """
    if target_group not in allow_lists:
        return True  # No allow list = pass all
    allowed = allow_lists[target_group]
    clean_upper = clean.upper()
    return any(pattern.upper() in clean_upper for pattern in allowed)


def normalize_channels(
    filtered: list[tuple[RawChannel, str]],
    rules: NormalizationRules,
) -> list[NormalizedChannel]:
    """
    Normalize a filtered list of (RawChannel, target_group) tuples.
    Returns deduplicated NormalizedChannel list.
    """
    seen_uids: set[str] = set()
    result: list[NormalizedChannel] = []

    for raw, target_group in filtered:
        # Skip section header/separator lines
        if is_separator(raw.display_name):
            continue

        clean = clean_name(raw.display_name, rules)

        if not clean:
            continue

        # Apply allow list filtering
        if not passes_allow_list(clean, target_group, rules.allow_lists):
            continue

        uid = make_channel_uid(clean, target_group)

        # Deduplicate — first occurrence wins (provider order = quality order
        # since better streams tend to appear first in each group)
        if uid in seen_uids:
            continue
        seen_uids.add(uid)

        result.append(NormalizedChannel(
            channel_uid=uid,
            display_name=clean,
            raw_display_name=raw.display_name,
            target_group=target_group,
            source_group=raw.group_title,
            tvg_id=raw.tvg_id,
            tvg_logo=raw.tvg_logo,
            url=raw.url,
            cuid=raw.cuid,
        ))

    return result
