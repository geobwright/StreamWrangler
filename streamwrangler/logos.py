"""
Logo manager — downloads TV channel logos from tv-logo/tv-logos GitHub repo.

Matches included channels by normalizing display names to the repo's
hyphenated filename convention, downloads, and normalizes to 512×512 PNG.

Logos stored in:  /home/geoffrey/infra/compose/dispatcharr/data/epgs/logos/
Served via Caddy: http://172.24.0.1:8765/logos/<filename>

channels.json tvg_logo field is updated in-place so M3U output picks them up.
"""

import io
import re
from pathlib import Path

import httpx
from PIL import Image

from .store import ChannelRecord, load_store, save_store

LOGO_DIR = Path("/home/geoffrey/infra/compose/dispatcharr/data/epgs/logos")
LOGO_BASE_URL = "http://172.24.0.1:8765/logos"
LOGO_SIZE = 512

RAW_BASE = "https://raw.githubusercontent.com/tv-logo/tv-logos/main"
GITHUB_CONTENTS = "https://api.github.com/repos/tv-logo/tv-logos/contents/countries"

# Source group prefix → (country folder, suffix used in filenames)
COUNTRY_MAP: dict[str, tuple[str, str]] = {
    "UK": ("united-kingdom", "uk"),
    "US": ("united-states", "us"),
    "FR": ("france",         "fr"),
    "DE": ("germany",        "de"),
    "NL": ("netherlands",    "nl"),
    "IT": ("italy",          "it"),
    "ES": ("spain",          "es"),
    "PT": ("portugal",       "pt"),
    "AU": ("australia",      "au"),
    "CA": ("canada",         "ca"),
    "BE": ("belgium",        "be"),
    "TR": ("turkey",         "tr"),
}

# Generic logo to use for every channel in a group (when individual matching fails)
GROUP_FALLBACK: dict[str, tuple[str, str]] = {
    # target_group → (country_folder, stem)
    "Paramount+ PPV": ("united-states", "paramount-plus-us"),
    "Tennis PPV":     ("united-states", "tennis-channel-us"),
}

# Explicit overrides: normalized slug → exact repo stem to use
# Use when the channel name doesn't map cleanly to the filename convention.
SLUG_OVERRIDES: dict[str, str] = {
    "msnbc":                  "msnbc-alt-us",
    "willow-cricket":         "willow-us",
    "willow-cricket-extra":   "willow-xtra-us",
    "willow-cricket-hd":      "willow-us",
    "usa-network":            "usa-us",
    "epl-bein-english-1-stable": "bein-sports-us",    # no UK variant; use generic US
    "epl-bein-english-2-stable": "bein-sports-2-us",
}

# US local affiliate fallback: first word of display name → generic network stem
US_NETWORK_FALLBACK: dict[str, str] = {
    "fox": "fox-us",
    "cbs": "cbs-logo-white-us",
    "nbc": "nbc-us",
    "abc": "abc-us",
    "cw":  "the-cw-us",
}

# Suffixes to strip from display names before matching
_QUALITY_RE  = re.compile(r"\b(4K|UHD|FHD|HD|SD|HEVC|H\.265|H\.264)\b", re.IGNORECASE)
_LANG_TAG_RE = re.compile(r"\s*\[(?:FR|DE|ES|IT|PT|NL|AR|TR|PL|HU|RO|CZ|SK)\]\s*", re.IGNORECASE)

# UK location suffixes that appear in feed names but not logo filenames
_LOCATION_RE = re.compile(
    r"\b(LONDON|ENGLAND|SCOTLAND|WALES|NORTHERN IRELAND|IRELAND|EAST|WEST|NORTH|SOUTH"
    r"|MIDLANDS|YORKSHIRE|REGIONAL)\b",
    re.IGNORECASE,
)

# Digit → word for channel number suffixes (BBC 1 → BBC One, etc.)
_DIGIT_WORD = {
    "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten",
}


def _normalize(display_name: str) -> str:
    """
    Strip quality/language tags, lowercase, collapse to hyphen-separated slug.
    'Sky Sports 1 FHD' → 'sky-sports-1'
    'beIN Sports 1 [FR]' → 'bein-sports-1'
    """
    name = _QUALITY_RE.sub("", display_name)
    name = _LANG_TAG_RE.sub("", name)
    name = _LOCATION_RE.sub("", name)
    name = name.strip()
    name = name.lower()
    name = name.replace("+", "-plus")        # Canal+ → canal-plus
    name = re.sub(r"\s*&\s*", "-and-", name) # Faith & Living → faith-and-living
    name = re.sub(r"[^\w\s-]", "", name)    # drop punctuation except hyphens
    name = re.sub(r"[\s_]+", "-", name)      # spaces/underscores → hyphens
    name = re.sub(r"-{2,}", "-", name)       # collapse multiple hyphens
    return name.strip("-")


def _country_prefix(source_group: str) -> str | None:
    """'UK| SPORT' → 'UK';  'FR| CANAL+' → 'FR';  'Tennis PPV' → None"""
    m = re.match(r"^([A-Z]{2})\|", source_group or "")
    return m.group(1) if m else None


# Fallback: infer country from target group when source group has no prefix (e.g. 4K feeds)
_TARGET_GROUP_PREFIX: dict[str, str] = {
    "UK":       "UK",
    "US":       "US",
    "FR":       "FR",
    "DE":       "DE",
    "NL":       "NL",
    "IT":       "IT",
    "ES":       "ES",
    "PT":       "PT",
    "AU":       "AU",
    "CA":       "CA",
    "BE":       "BE",
    "TR":       "TR",
}

def _infer_prefix(source_group: str, target_group: str) -> str | None:
    """Return country prefix, falling back to target group prefix if source has none."""
    prefix = _country_prefix(source_group)
    if prefix:
        return prefix
    # Try first word of target group: 'UK Sports' → 'UK', 'US News' → 'US'
    first_word = (target_group or "").split()[0].upper() if target_group else ""
    return _TARGET_GROUP_PREFIX.get(first_word)


def fetch_country_index(country_folder: str) -> dict[str, str]:
    """
    Fetch file listing for one country and return {stem: raw_url}.
    stem = filename without .png extension, e.g. 'bbc-one-uk'
    """
    url = f"{GITHUB_CONTENTS}/{country_folder}"
    resp = httpx.get(url, timeout=20, headers={"Accept": "application/vnd.github.v3+json"})
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    items = resp.json()
    index: dict[str, str] = {}
    for item in items:
        name: str = item.get("name", "")
        if name.endswith(".png"):
            stem = name[:-4]
            raw_url = f"{RAW_BASE}/countries/{country_folder}/{name}"
            index[stem] = raw_url
    return index


def build_index(prefixes: set[str]) -> dict[str, str]:
    """Fetch and merge country indexes for all needed prefixes."""
    index: dict[str, str] = {}
    seen_folders: set[str] = set()
    for prefix in prefixes:
        entry = COUNTRY_MAP.get(prefix)
        if not entry:
            continue
        folder, _ = entry
        if folder in seen_folders:
            continue
        seen_folders.add(folder)
        country_index = fetch_country_index(folder)
        index.update(country_index)
    return index


def _needs_dark_background(img: Image.Image) -> bool:
    """
    Return True if the logo is predominantly light-coloured on a transparent
    background — i.e. it will be nearly invisible on a white/light display.

    Heuristic: among pixels with meaningful opacity (alpha > 64), if more than
    60% are near-white (R, G, B all > 200), the logo needs a dark backing.
    """
    pixels = img.getdata()
    opaque = [(r, g, b, a) for r, g, b, a in pixels if a > 64]
    if not opaque:
        return False
    light = sum(1 for r, g, b, a in opaque if r > 200 and g > 200 and b > 200)
    return light / len(opaque) > 0.60


def _normalize_to_512(img_bytes: bytes) -> bytes:
    """
    Resize and center image onto a 512×512 canvas.

    If the logo is predominantly light-on-transparent (e.g. France 2/3/4/5),
    adds a dark rounded-rectangle background so it stays visible on any display.
    Otherwise keeps the canvas transparent.
    """
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    img.thumbnail((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)

    if _needs_dark_background(img):
        # Dark charcoal backing — matches typical dark-theme media centre UIs
        canvas = Image.new("RGBA", (LOGO_SIZE, LOGO_SIZE), (30, 30, 30, 255))
    else:
        canvas = Image.new("RGBA", (LOGO_SIZE, LOGO_SIZE), (0, 0, 0, 0))

    offset_x = (LOGO_SIZE - img.width) // 2
    offset_y = (LOGO_SIZE - img.height) // 2
    canvas.paste(img, (offset_x, offset_y), img)

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _download_and_save(url: str, dest: Path) -> bool:
    """Download logo, normalize to 512×512, write to dest. Returns True on success."""
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        dest.write_bytes(_normalize_to_512(resp.content))
        return True
    except Exception:
        return False


def _extra_slugs(norm_name: str) -> list[str]:
    """
    Generate additional slug variants to widen match coverage:
    - digit → word  ('bbc-1' → 'bbc-one')
    - drop trailing 's'  ('main-events' → 'main-event')
    - drop location tokens already stripped by _normalize (belt-and-suspenders)
    """
    extras: list[str] = []

    # digit → word at end of slug (e.g. 'bbc-1' → 'bbc-one')
    digit_word = re.sub(
        r"-(\d+)$",
        lambda m: "-" + _DIGIT_WORD[m.group(1)] if m.group(1) in _DIGIT_WORD else m.group(0),
        norm_name,
    )
    if digit_word != norm_name:
        extras.append(digit_word)

    # word → digit (e.g. 'bbc-one' already normalized, but repo might use digits)
    for digit, word in _DIGIT_WORD.items():
        if norm_name.endswith(f"-{word}"):
            extras.append(norm_name[: -len(word)] + digit)
            break

    # Drop trailing 's' on last token ('main-events' → 'main-event', 'sports' → keep)
    # Only when last token is >5 chars to avoid stripping meaningful short words
    last = norm_name.rsplit("-", 1)
    if len(last) == 2 and last[1].endswith("s") and len(last[1]) > 5:
        extras.append(f"{last[0]}-{last[1][:-1]}")

    return extras


def _candidates(norm_name: str, country_suffix: str | None) -> list[str]:
    """Return filename stems to try, most-specific first."""
    slugs = [norm_name] + _extra_slugs(norm_name)
    if country_suffix:
        result = []
        for s in slugs:
            result.append(f"{s}-{country_suffix}")
        for s in slugs:
            result.append(s)
        return result
    return slugs


def _sync_sportsdb_logos(channels: list[ChannelRecord]) -> None:
    """
    Update tvg_logo on any channel that has a configured logo in sportsdb.yaml.
    Keyed by channel_uid from the sportsdb teams list.
    """
    try:
        from .sportsdb import load_sportsdb_config
        config = load_sportsdb_config()
        if not config:
            return
        logo_base = (config.get("logo_base_url") or "").rstrip("/")
        if not logo_base:
            return
        uid_to_logo = {
            str(t["channel_uid"]): f"{logo_base}/{t['logo']}"
            for t in (config.get("teams") or [])
            if t.get("logo")
        }
        for ch in channels:
            if ch.channel_uid in uid_to_logo:
                ch.tvg_logo = uid_to_logo[ch.channel_uid]
    except Exception:
        pass


def run_logos(
    dry_run: bool = False,
    overwrite: bool = False,
) -> tuple[list[tuple[ChannelRecord, str]], list[ChannelRecord]]:
    """
    Match and download logos for all included channels.

    Returns:
      matched   — list of (channel, filename) for channels with a logo found
      unmatched — list of channels with no match
    """
    channels = load_store()
    included = [c for c in channels if c.status == "included"]

    # Pre-pass: sync TheSportsDB logos for configured teams into channels.json
    _sync_sportsdb_logos(channels)

    # Collect which country prefixes we need (source group or inferred from target group)
    prefixes = {_infer_prefix(c.source_group, c.target_group) for c in included}
    prefixes.discard(None)

    # Always include US so group fallbacks (Paramount+, Tennis) can resolve
    prefixes.add("US")

    index = build_index(prefixes)

    LOGO_DIR.mkdir(parents=True, exist_ok=True)

    matched: list[tuple[ChannelRecord, str]] = []
    unmatched: list[ChannelRecord] = []

    for ch in included:
        # Skip channels that already have a locally-hosted logo (e.g. sports teams from TheSportsDB)
        # When overwrite=True, still skip — those logos are managed separately (sportsdb, manual)
        # and should not be re-downloaded from tv-logo.
        if not overwrite and ch.tvg_logo and ch.tvg_logo.startswith(LOGO_BASE_URL):
            matched.append((ch, Path(ch.tvg_logo).name))
            continue

        prefix = _infer_prefix(ch.source_group, ch.target_group)
        suffix = COUNTRY_MAP[prefix][1] if prefix in COUNTRY_MAP else None
        norm = _normalize(ch.display_name)
        candidates = _candidates(norm, suffix)

        # Check slug overrides first
        norm_base = _normalize(ch.display_name)
        if norm_base in SLUG_OVERRIDES:
            hit = SLUG_OVERRIDES[norm_base]
        else:
            hit = next((k for k in candidates if k in index), None)

        # US local affiliate fallback: "FOX 4 (WDAF)..." → try generic network logo
        if hit is None and prefix == "US":
            first_word = ch.display_name.split()[0].lower()
            net_stem = US_NETWORK_FALLBACK.get(first_word)
            if net_stem and net_stem in index:
                hit = net_stem

        # Group-level fallback (e.g. all Paramount+ PPV slots → paramount-plus-us)
        if hit is None and ch.target_group in GROUP_FALLBACK:
            folder, stem = GROUP_FALLBACK[ch.target_group]
            if stem in index:
                hit = stem

        if hit is None:
            unmatched.append(ch)
            continue

        filename = f"{hit}.png"
        dest = LOGO_DIR / filename
        local_url = f"{LOGO_BASE_URL}/{filename}"

        if not dry_run:
            if dest.exists() and not overwrite:
                # Already downloaded — just update the record URL
                ch.tvg_logo = local_url
            elif _download_and_save(index[hit], dest):
                ch.tvg_logo = local_url
            else:
                unmatched.append(ch)
                continue

        matched.append((ch, filename))

    if not dry_run:
        save_store(channels)

    return matched, unmatched


# ---------------------------------------------------------------------------
# Dispatcharr push — sync local logos to Dispatcharr via its REST API
# ---------------------------------------------------------------------------

DISPATCHARR_URL = "http://10.0.1.39:9191"


def _dispatcharr_token(base_url: str, username: str, password: str) -> str:
    resp = httpx.post(
        f"{base_url}/api/accounts/token/",
        json={"username": username, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access"]


def _fetch_all(url: str, headers: dict) -> list[dict]:
    """Fetch all pages from a paginated Dispatcharr endpoint."""
    results: list[dict] = []
    next_url = url
    while next_url:
        resp = httpx.get(next_url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
            break
        results.extend(data.get("results", []))
        next_url = data.get("next")
    return results


def push_logos(
    base_url: str = DISPATCHARR_URL,
    username: str = "admin",
    password: str = "admin",
    dry_run: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """
    For every included channel in channels.json that has a local logo URL,
    ensure Dispatcharr has a logo object pointing to that URL and the channel
    is linked to it.

    Returns (updated, already_ok, skipped):
      updated    — channels whose logo_id was changed
      already_ok — channels already pointing to the correct local logo
      skipped    — channels with no local logo or no Dispatcharr match
    """
    channels = load_store()
    included_with_logo = [
        c for c in channels
        if c.status == "included"
        and c.tvg_logo
        and c.tvg_logo.startswith(LOGO_BASE_URL)
    ]

    token = _dispatcharr_token(base_url, username, password)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Fetch all Dispatcharr channels — build lookup by tvg_id and by name
    d_channels = _fetch_all(f"{base_url}/api/channels/channels/?page_size=500", headers)
    by_tvg_id: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for dc in d_channels:
        tid = (dc.get("tvg_id") or "").strip().lower()
        if tid:
            by_tvg_id.setdefault(tid, []).append(dc)
        by_name.setdefault(dc["name"].lower(), []).append(dc)

    # Fetch all Dispatcharr logos — build lookup by URL
    d_logos = _fetch_all(f"{base_url}/api/channels/logos/?page_size=500", headers)
    logo_by_url: dict[str, int] = {l["url"]: l["id"] for l in d_logos}

    updated: list[str] = []
    already_ok: list[str] = []
    skipped: list[str] = []

    for ch in included_with_logo:
        local_url = ch.tvg_logo

        # Find matching Dispatcharr channel(s)
        tid = (ch.tvg_id or "").strip().lower()
        d_matches = by_tvg_id.get(tid, []) if tid else []
        if not d_matches:
            d_matches = by_name.get(ch.display_name.lower(), [])
        if not d_matches:
            skipped.append(f"{ch.display_name} (no Dispatcharr match)")
            continue

        # Ensure logo object exists for this URL
        if local_url in logo_by_url:
            logo_id = logo_by_url[local_url]
        else:
            if dry_run:
                logo_id = -1  # placeholder
            else:
                stem = Path(local_url).stem
                resp = httpx.post(
                    f"{base_url}/api/channels/logos/",
                    headers=headers,
                    json={"name": stem, "url": local_url},
                    timeout=10,
                )
                resp.raise_for_status()
                logo_id = resp.json()["id"]
                logo_by_url[local_url] = logo_id

        for dc in d_matches:
            label = f"{ch.display_name} (Dispatcharr id={dc['id']})"
            if dc.get("logo_id") == logo_id and not dry_run:
                already_ok.append(label)
                continue
            if not dry_run:
                patch = httpx.patch(
                    f"{base_url}/api/channels/channels/{dc['id']}/",
                    headers=headers,
                    json={"logo_id": logo_id},
                    timeout=10,
                )
                patch.raise_for_status()
            updated.append(label)

    return updated, already_ok, skipped
