"""
TheSportsDB API client — fetches upcoming fixtures for configured teams.

Free tier: API key "123", 30 requests/minute.

Key timezone note: strTimestamp from the events API is UTC. Venue timezone
is fetched only for display — converting UTC kickoff to local time for
descriptions (e.g. "8:00 PM BST" at Anfield, "9:00 PM CEST" in Madrid).
"""

import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import yaml

SPORTSDB_CONFIG_PATH = Path("config/sportsdb.yaml")
BASE_URL = "https://www.thesportsdb.com/api/v1/json"

# Venue country → IANA timezone (display only, not for UTC conversion)
# NOTE: US entries default to America/New_York — city-level lookup needed for
# baseball (Chicago, LA, etc.). Revisit when adding MLB teams.
COUNTRY_TZ: dict[str, str] = {
    "England": "Europe/London",
    "Scotland": "Europe/London",
    "Wales": "Europe/London",
    "Northern Ireland": "Europe/London",
    "Republic of Ireland": "Europe/Dublin",
    "Spain": "Europe/Madrid",
    "Germany": "Europe/Berlin",
    "France": "Europe/Paris",
    "Italy": "Europe/Rome",
    "Netherlands": "Europe/Amsterdam",
    "Portugal": "Europe/Lisbon",
    "Belgium": "Europe/Brussels",
    "Turkey": "Europe/Istanbul",
    "Greece": "Europe/Athens",
    "Brazil": "America/Sao_Paulo",
    "Argentina": "America/Argentina/Buenos_Aires",
    "United States": "America/New_York",
    "USA": "America/New_York",
    "Canada": "America/Toronto",
    "Australia": "Australia/Sydney",
    "Japan": "Asia/Tokyo",
}


def load_sportsdb_config() -> dict | None:
    """Load config/sportsdb.yaml. Returns None if not present."""
    if not SPORTSDB_CONFIG_PATH.exists():
        return None
    return yaml.safe_load(SPORTSDB_CONFIG_PATH.read_text())


def channel_epg_map(config: dict) -> dict[str, str]:
    """Return {channel_uid: epg_id} for all configured teams."""
    return {str(t["channel_uid"]): t["epg_id"] for t in (config.get("teams") or [])}


# Rate limiter — free tier allows 30 requests/minute.
# Tracks timestamps of recent calls in a sliding window and sleeps if needed.
_RATE_LIMIT = 25          # stay comfortably under the 30/min ceiling
_RATE_WINDOW = 60.0       # seconds
_call_times: deque = deque()


def _get(url: str) -> dict:
    now = time.monotonic()

    # Drop timestamps outside the sliding window
    while _call_times and now - _call_times[0] > _RATE_WINDOW:
        _call_times.popleft()

    if len(_call_times) >= _RATE_LIMIT:
        sleep_for = _RATE_WINDOW - (now - _call_times[0]) + 0.1
        if sleep_for > 0:
            time.sleep(sleep_for)

    _call_times.append(time.monotonic())
    response = httpx.get(url, timeout=15, follow_redirects=True)
    response.raise_for_status()
    return response.json()


def fetch_team_by_name(team_name: str, api_key: str, cache: dict) -> dict | None:
    """Search for a team by name. Returns the first result or None. Cached by lowercased name."""
    key = team_name.lower().strip()
    if key in cache:
        return cache[key]
    try:
        data = _get(f"{BASE_URL}/{api_key}/searchteams.php?t={team_name}")
        teams = data.get("teams") or []
        result = teams[0] if teams else None
    except Exception:
        result = None
    cache[key] = result
    return result


def fetch_next_events(team_id: str, api_key: str) -> list[dict]:
    """Next upcoming events for a team (up to 5 on free tier)."""
    data = _get(f"{BASE_URL}/{api_key}/eventsnext.php?id={team_id}")
    return data.get("events") or []


def fetch_venue(venue_id: str, api_key: str, cache: dict) -> dict | None:
    """Fetch venue by ID, caching within a run to avoid duplicate calls."""
    if venue_id in cache:
        return cache[venue_id]
    data = _get(f"{BASE_URL}/{api_key}/lookupvenue.php?id={venue_id}")
    venues = data.get("venues") or []
    result = venues[0] if venues else None
    cache[venue_id] = result
    return result


def fetch_league_table(league_id: str, season: str, api_key: str, cache: dict) -> list[dict]:
    """League standings, cached per league+season within a run."""
    key = f"{league_id}:{season}"
    if key in cache:
        return cache[key]
    data = _get(f"{BASE_URL}/{api_key}/lookuptable.php?l={league_id}&s={season}")
    table = data.get("table") or []
    cache[key] = table
    return table


def venue_display_tz(venue: dict | None) -> ZoneInfo:
    """
    Timezone for displaying local kickoff time in descriptions.
    Falls back to UTC offset from strTimezone string if country unknown.
    """
    if not venue:
        return ZoneInfo("UTC")
    country = (venue.get("strCountry") or "").strip()
    if country in COUNTRY_TZ:
        return ZoneInfo(COUNTRY_TZ[country])
    tz_str = venue.get("strTimezone") or ""
    m = re.search(r"UTC\s*([+-])(\d+):(\d+)", tz_str)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        offset = timedelta(hours=int(m.group(2)) * sign, minutes=int(m.group(3)) * sign)
        return timezone(offset)
    return ZoneInfo("UTC")


def event_utc(event: dict) -> datetime:
    """
    Parse event start as UTC. strTimestamp is UTC per the TheSportsDB API.
    Falls back to dateEvent + strTime if strTimestamp is absent.
    """
    ts = (event.get("strTimestamp") or "").strip()
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    date_str = event.get("dateEvent") or ""
    time_str = event.get("strTime") or "00:00:00"
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def team_standing(table: list[dict], team_name: str) -> str | None:
    """
    Return position string like '1st 79pts' for a team in a league table.
    Returns None if team not found.
    """
    for row in table:
        if (row.get("strTeam") or "").lower() == team_name.lower():
            pos = row.get("intRank") or row.get("intPosition") or ""
            pts = row.get("intPoints") or ""
            if pos:
                suffix = {1: "st", 2: "nd", 3: "rd"}.get(int(pos), "th")
                result = f"{pos}{suffix}"
                if pts:
                    result += f" {pts}pts"
                return result
    return None


def soccer_season(year: int, month: int) -> str:
    """Current soccer season string, e.g. '2025-2026'. Seasons start in July."""
    if month < 7:
        return f"{year - 1}-{year}"
    return f"{year}-{year + 1}"


def baseball_season(year: int) -> str:
    return str(year)
