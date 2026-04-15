"""
Tennis player ranking lookup via TheSportsDB.

Lookups are cached at data/tennis_rank_cache.json:
  - Player IDs: permanent (never expire)
  - Rankings: 7-day TTL (strNumber from lookupplayer.php)

Only "Last, First" formatted names (containing a comma) trigger API lookups.
Two-step lookup: searchplayers.php → idPlayer, then lookupplayer.php → strNumber.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

RANK_CACHE_PATH = Path("data/tennis_rank_cache.json")
BASE_URL = "https://www.thesportsdb.com/api/v1/json"

RANK_TTL_DAYS = 7    # re-fetch ranking weekly
SEARCH_TTL_DAYS = 30 # retry "not found" player searches after 30 days


def load_rank_cache() -> dict:
    if RANK_CACHE_PATH.exists():
        try:
            return json.loads(RANK_CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def save_rank_cache(cache: dict) -> None:
    RANK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RANK_CACHE_PATH.write_text(json.dumps(cache, indent=2))


from .sportsdb import _get  # shared rate-limited TheSportsDB caller


def _is_stale(timestamp_str: str | None, ttl_days: int) -> bool:
    if not timestamp_str:
        return True
    try:
        fetched = datetime.fromisoformat(timestamp_str)
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - fetched > timedelta(days=ttl_days)
    except ValueError:
        return True


def _reorder_name(raw: str) -> str:
    """Convert 'Last, First' to 'First Last'. Returns stripped raw if no comma."""
    if "," not in raw:
        return raw.strip()
    last, first = raw.split(",", 1)
    return f"{first.strip()} {last.strip()}"


def _fetch_player_id(first_last: str, api_key: str) -> str | None:
    """Search for player by 'First Last' name. Returns idPlayer string or None."""
    search_name = first_last.replace(" ", "+")
    try:
        data = _get(f"{BASE_URL}/{api_key}/searchplayers.php?p={search_name}")
        players = data.get("player") or []
        if players:
            return str(players[0]["idPlayer"])
    except Exception:
        pass
    return None


def _fetch_player_rank(player_id: str, api_key: str) -> int | None:
    """Look up player by ID. Returns strNumber as int, or None if not available."""
    try:
        data = _get(f"{BASE_URL}/{api_key}/lookupplayer.php?id={player_id}")
        players = data.get("players") or []
        if players:
            rank_str = (players[0].get("strNumber") or "").strip()
            if rank_str.isdigit():
                return int(rank_str)
    except Exception:
        pass
    return None


def player_display(raw_name: str, api_key: str = "123",
                   cache: dict | None = None) -> tuple[str, int | None]:
    """
    Convert "Last, First" to "First Last" and look up ATP/WTA ranking.

    Returns (formatted_name, rank) — rank is None if the name has no comma,
    the player is not found, or the lookup fails.

    Pass a shared cache dict to avoid redundant disk reads across multiple
    calls in the same session. Caller is responsible for saving the cache.
    """
    own_cache = cache is None
    if own_cache:
        cache = load_rank_cache()

    formatted = _reorder_name(raw_name)

    if "," not in raw_name:
        return formatted, None

    now_str = datetime.now(timezone.utc).isoformat()
    key = formatted.lower()
    entry = dict(cache.get(key) or {})

    # Determine what needs fetching.
    # Null IDs and null ranks both use a 1-day retry TTL so rate-limit errors
    # during a run don't block re-fetching for days. The longer SEARCH_TTL_DAYS
    # only applies once a valid ID has been confirmed (player genuinely missing).
    has_id = bool(entry.get("id"))
    id_ttl = SEARCH_TTL_DAYS if has_id else 1
    needs_id = not has_id and _is_stale(entry.get("id_fetched_at"), id_ttl)
    rank_ttl = RANK_TTL_DAYS if entry.get("rank") is not None else 1
    needs_rank = has_id and _is_stale(entry.get("rank_fetched_at"), rank_ttl)

    if needs_id:
        player_id = _fetch_player_id(formatted, api_key)
        entry["id"] = player_id
        entry["id_fetched_at"] = now_str
        entry["rank"] = None
        entry["rank_fetched_at"] = None
        if player_id:
            needs_rank = True
        cache[key] = entry

    if needs_rank and entry.get("id"):
        rank = _fetch_player_rank(entry["id"], api_key)
        entry["rank"] = rank
        entry["rank_fetched_at"] = now_str
        cache[key] = entry

    if own_cache:
        save_rank_cache(cache)

    return formatted, entry.get("rank")


def enrich_players(players_str: str, api_key: str = "123") -> tuple[str, str]:
    """
    Parse a "Last, First vs Last, First" player string into enriched title and
    description forms.

    Returns (title_str, desc_str):
      title_str  — "First Last vs First Last"
      desc_str   — "First Last (#62) vs First Last (#71)"
                   (rank parenthetical omitted for players whose rank is unknown)

    Works for any number of " vs "-separated players.
    """
    cache = load_rank_cache()

    parts = [p.strip() for p in players_str.split(" vs ")]
    title_parts: list[str] = []
    desc_parts: list[str] = []

    for raw in parts:
        name, rank = player_display(raw, api_key, cache)
        title_parts.append(name)
        if rank is not None:
            desc_parts.append(f"{name} (#{rank})")
        else:
            desc_parts.append(name)

    save_rank_cache(cache)

    return " vs ".join(title_parts), " vs ".join(desc_parts)
