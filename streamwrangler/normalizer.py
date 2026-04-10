"""
Normalization engine — cleans raw channel names, generates channel_uid,
detects stream quality, and selects the best variant per channel.
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
    source_group: str          # Provider group
    tvg_id: str
    tvg_logo: str
    url: str
    quality: str = ""          # Detected quality: 4K, FHD, HD, SD, or ""
    codec_hint: str = ""       # Codec detected from name: "hevc" or ""
    cuid: str = ""


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
    return NormalizationRules(
        strip_prefixes=data.get("strip_prefixes", []),
        strip_suffixes=data.get("strip_suffixes", []),
        strip_inline=data.get("strip_inline", []),
        replacements=[tuple(r) for r in data.get("replacements", [])],
        allow_lists=data.get("allow_lists", {}),
    )


# ─────────────────────────────────────────────
# Quality detection
# ─────────────────────────────────────────────

# Patterns checked against the RAW display name (before any stripping)
_QUALITY_PATTERNS = [
    # 4K / UHD — check before HD so "4K HD" doesn't just match HD
    (re.compile(r'\b(4K|UHD|2160|³⁸⁴⁰)', re.IGNORECASE), "4K"),
    (re.compile(r'ᵁᴴᴰ'),                                   "4K"),
    (re.compile(r'\b(FHD|1080)\b', re.IGNORECASE),         "FHD"),
    (re.compile(r'\bHD\b', re.IGNORECASE),                 "HD"),
    (re.compile(r'\b(SD|480|360)\b', re.IGNORECASE),       "SD"),
]

# Codec hints detected from the raw name — separate from resolution quality
_CODEC_PATTERNS = [
    (re.compile(r'ʰᵉᵛᶜ|HEVC|H\.?265', re.IGNORECASE), "hevc"),
]

# Streams we deprioritize in selection
_LOW_PRIORITY_RE = re.compile(
    r'\b(BACKUP|backup|LOW|RAW)\b'
    r'|ᴿᴬᵂ'
    r'|\s+\([ADHS]\)\s*$'   # trailing (A) (D) (H) (S) alternate feed markers
)

# VIP prefix — preferred within the same quality tier, but does not override quality.
# Tiebreaker only: VIP FHD beats plain FHD, but VIP HD still loses to plain FHD.
# NOTE: whether VIP should ever override quality is undecided — revisit if provider
# VIP streams are found to be consistently more reliable than non-VIP higher-res streams.
_VIP_RE = re.compile(r'(^|\|)\s*VIP\b', re.IGNORECASE)


def detect_quality(raw_name: str) -> str:
    """Detect stream quality tier from raw channel name (resolution only, not codec).
    Returns "" if no quality indicator found — displayed as 'Unk' in the TUI."""
    for pattern, label in _QUALITY_PATTERNS:
        if pattern.search(raw_name):
            return label
    return ""


def detect_codec_hint(raw_name: str) -> str:
    """Detect codec hint from raw channel name (e.g. 'hevc' from 'BBC One HEVC')."""
    for pattern, label in _CODEC_PATTERNS:
        if pattern.search(raw_name):
            return label
    return ""


def quality_score(quality: str, raw_name: str, codec_hint: str = "") -> int:
    """
    Score a stream variant for selection.
    Higher = better. Used to pick the best stream when deduplicating.

    Tier 1 (high-BW) scores: 4K HEVC=43, 4K=40, FHD HEVC=33, FHD=30
    Tier 2 (low-BW)  scores: HD HEVC=23,  HD=20, SD HEVC=13,  SD=10
    VIP bonus (+2) is a tiebreaker within the same quality level — never crosses tiers.
    """
    base = {"4K": 40, "FHD": 30, "HD": 20, "SD": 10, "": 15}
    score = base.get(quality, 20)
    # HEVC/H.265 streams are preferable over plain H.264 at same resolution
    if codec_hint == "hevc":
        score += 3
    # VIP prefix is preferred within same quality level (tiebreaker only)
    if _VIP_RE.search(raw_name):
        score += 2
    # Penalise low-priority streams
    if _LOW_PRIORITY_RE.search(raw_name):
        score -= 15
    return score


# ─────────────────────────────────────────────
# Unicode / name cleaning
# ─────────────────────────────────────────────

_UNICODE_NOISE_RE = re.compile(
    r'[\u00b2-\u00b3\u00b9\u2070-\u209f\u1d00-\u1d7f\u1d80-\u1dbf'
    r'\u2c60-\u2c7f\ua720-\ua7ff\u24b6-\u24e9]+'
)
_SEPARATOR_RE = re.compile(r'^[\s#=|*\-_]+$')


def _strip_unicode_noise(name: str) -> str:
    return _UNICODE_NOISE_RE.sub("", name).strip()


def clean_name(name: str, rules: NormalizationRules) -> str:
    """Apply all normalization rules to produce a clean channel name."""
    result = name.strip()

    for prefix in rules.strip_prefixes:
        if result.upper().startswith(prefix.upper()):
            result = result[len(prefix):].strip()
            break

    changed = True
    while changed:
        changed = False
        for suffix in rules.strip_suffixes:
            if result.upper().endswith(suffix.upper()):
                result = result[: -len(suffix)].strip()
                changed = True

    for noise in rules.strip_inline:
        result = result.replace(noise, "").strip()

    result = _strip_unicode_noise(result)

    for from_str, to_str in rules.replacements:
        result = result.replace(from_str, to_str)

    return result.strip()


def make_channel_uid(display_name: str, target_group: str) -> str:
    normalized = unicodedata.normalize("NFKD", display_name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    uid = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")

    ambiguous_names = {
        "eurosport_1", "eurosport_2", "bein_sports_1", "bein_sports_2",
        "canal_sport", "sky_news", "dazn_1",
    }
    if uid in ambiguous_names:
        group_slug = re.sub(r"[^a-z0-9]+", "_", target_group.lower()).strip("_")
        uid = f"{uid}_{group_slug}"

    return uid


def is_separator(name: str) -> bool:
    if _SEPARATOR_RE.match(name):
        return True
    stripped = re.sub(r"[#=|\-_*\s]", "", name)
    return not stripped


def passes_allow_list(clean: str, target_group: str, allow_lists: dict) -> bool:
    if target_group not in allow_lists:
        return True
    clean_upper = clean.upper()
    return any(pattern.upper() in clean_upper for pattern in allow_lists[target_group])


# ─────────────────────────────────────────────
# Main normalization pipeline
# ─────────────────────────────────────────────

def normalize_channels(
    filtered: list[tuple[RawChannel, str]],
    rules: NormalizationRules,
    probe_cache: dict | None = None,
) -> list[NormalizedChannel]:
    """
    Normalize filtered channels.
    - Detects quality for each variant
    - Groups variants by (uid, target_group)
    - Keeps the highest-quality stream per channel

    If probe_cache is provided, probe-verified quality is used for scoring
    (dedup picks the right winner) while advertised quality is still stored
    in NormalizedChannel.quality for display and re-derivation in build_store().
    """
    from .probe_cache import get_cached_probe

    # Pass 1: clean, detect quality, filter
    candidates: list[tuple[str, int, NormalizedChannel]] = []  # (uid, score, channel)

    for raw, target_group in filtered:
        if is_separator(raw.display_name):
            continue

        quality = detect_quality(raw.display_name)
        codec_hint = detect_codec_hint(raw.display_name)

        # Use probe-verified quality for scoring when available — picks the
        # correct dedup winner even if the name mis-advertises the resolution.
        # Advertised quality (quality) is still stored for display purposes.
        if probe_cache:
            cached = get_cached_probe(raw.url, probe_cache)
            score_quality = cached["quality"] if cached and cached.get("quality") else quality
            score_codec   = cached.get("codec", codec_hint) if cached else codec_hint
        else:
            score_quality = quality
            score_codec   = codec_hint

        score = quality_score(score_quality, raw.display_name, score_codec)
        clean = clean_name(raw.display_name, rules)

        if not clean:
            continue

        if not passes_allow_list(clean, target_group, rules.allow_lists):
            continue

        uid = make_channel_uid(clean, target_group)

        candidates.append((uid, score, NormalizedChannel(
            channel_uid=uid,
            display_name=clean,
            raw_display_name=raw.display_name,
            target_group=target_group,
            source_group=raw.group_title,
            tvg_id=raw.tvg_id,
            tvg_logo=raw.tvg_logo,
            url=raw.url,
            quality=quality,
            codec_hint=codec_hint,
            cuid=raw.cuid,
        )))

    # Pass 2: per uid, keep highest-scoring variant (primary)
    best: dict[str, tuple[int, NormalizedChannel]] = {}
    for uid, score, ch in candidates:
        if uid not in best or score > best[uid][0]:
            best[uid] = (score, ch)

    # Pass 3: find low-BW backup for Tier 1 primaries.
    # Tier 1 (high-BW): 4K, FHD — primary slot
    # Tier 2 (low-BW):  HD, SD, Unk — backup slot
    # Only created when primary is Tier 1; picks the best Tier 2 variant by score.
    _TIER1 = {"4K", "FHD"}
    _TIER2 = {"HD", "SD", ""}
    backup: dict[str, tuple[int, NormalizedChannel]] = {}
    for uid, score, ch in candidates:
        primary_quality = best[uid][1].quality
        if primary_quality in _TIER1 and ch.quality in _TIER2:
            if uid not in backup or score > backup[uid][0]:
                backup[uid] = (score, ch)

    # Build result in original encounter order (primaries first, backup follows its primary)
    seen: set[str] = set()
    result: list[NormalizedChannel] = []
    for uid, _score, ch in candidates:
        if uid not in seen and uid in best and best[uid][1] is ch:
            seen.add(uid)
            result.append(ch)
            # Append backup immediately after its primary
            if uid in backup:
                bk_ch = backup[uid][1]
                bk_uid = f"{uid}__bk"
                result.append(NormalizedChannel(
                    channel_uid=bk_uid,
                    display_name=bk_ch.display_name,
                    raw_display_name=bk_ch.raw_display_name,
                    target_group=bk_ch.target_group,
                    source_group=bk_ch.source_group,
                    tvg_id=bk_ch.tvg_id,
                    tvg_logo=bk_ch.tvg_logo,
                    url=bk_ch.url,
                    quality=bk_ch.quality,
                    codec_hint=bk_ch.codec_hint,
                    cuid=bk_ch.cuid,
                ))

    return result
