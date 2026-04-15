"""
PPV EPG generator — builds XMLTV from PPV channel schedule names.

Currently supports Tennis PPV. Channel names encode event info:
  "Player @ Apr 12 15:00 PM - ATP Monte Carlo :Tennis  03"

Times are in Europe/Paris timezone (the provider's local time for
European tournaments). Converts to UTC for XMLTV output.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring, indent as xml_indent
from zoneinfo import ZoneInfo

from .store import ChannelRecord, load_store

EPG_PATH = Path("/home/geoffrey/infra/compose/dispatcharr/data/epgs/wrangle_tennis.xml")

# Tennis provider encodes times in Europe/Paris (CEST = UTC+2 in summer, CET = UTC+1 in winter)
SOURCE_TZ = ZoneInfo("Europe/Paris")
# Paramount+ provider encodes times in US Eastern (EDT = UTC-4 in summer, EST = UTC-5 in winter)
PARAMOUNT_SOURCE_TZ = ZoneInfo("America/New_York")
# Display timezone for programme descriptions
LOCAL_TZ = ZoneInfo("America/Chicago")

# Tennis name pattern:
#   "Players @ Apr 12 15:00 PM - ATP Monte Carlo :Tennis  03"
# Times use 24h format; the AM/PM suffix is redundant but present.
_TENNIS_RE = re.compile(
    r"^(.+?)\s*@\s*(\w{3}\s+\d{1,2})\s+(\d{1,2}:\d{2})(?:\s*[AP]M)?\s*-\s*(.+?)\s*:Tennis\s+(\d+)\s*$",
    re.IGNORECASE,
)
# No-time format: "Boulter, Katie vs Cristian, Jaqueline - WTA Rouen :Tennis 03"
# Greedy group 1 so hyphens in names/tournaments don't cause a false split.
_TENNIS_NOTIMES_RE = re.compile(
    r"^(.+)\s+-\s+(.+?)\s*:Tennis\s+(\d+)\s*$",
    re.IGNORECASE,
)
_SLOT_RE = re.compile(r":Tennis\s+(\d+)")

MATCH_HOURS = 3    # live event block length
BLOCK_HOURS = 2    # filler block length
WINDOW_HOURS = 36  # EPG coverage window

XMLTV_FMT = "%Y%m%d%H%M%S +0000"


def _floor_block(dt: datetime) -> datetime:
    """Floor a datetime to the nearest 2-hour UTC boundary."""
    utc = dt.astimezone(timezone.utc)
    return utc.replace(minute=0, second=0, microsecond=0,
                       hour=(utc.hour // BLOCK_HOURS) * BLOCK_HOURS)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(XMLTV_FMT)


def _slot(raw: str) -> int | None:
    """Extract :Tennis  NN slot number from a raw channel name."""
    m = _SLOT_RE.search(raw or "")
    return int(m.group(1)) if m else None


def parse_tennis_name(raw: str, year: int) -> dict | None:
    """
    Parse a Tennis PPV channel name into event metadata.
    Returns None for blank channels (no event info).

    Times are parsed as Europe/Paris and returned as UTC datetimes.
    Handles year rollover: if current year gives a date >14 days ago, tries year+1.
    """
    m = _TENNIS_RE.match(raw)
    if not m:
        return None

    players = m.group(1).strip()
    date_str = m.group(2).strip()   # "Apr 12"
    time_str = m.group(3).strip()   # "15:00" (24h)
    tournament = m.group(4).strip()
    slot = int(m.group(5))

    now = datetime.now(timezone.utc)
    for y in (year, year + 1):
        try:
            local_dt = datetime.strptime(f"{y} {date_str} {time_str}", "%Y %b %d %H:%M")
            local_dt = local_dt.replace(tzinfo=SOURCE_TZ)
            utc_dt = local_dt.astimezone(timezone.utc)
            # Accept if date is within the last 14 days or any time in the future
            if utc_dt >= now - timedelta(days=14):
                break
        except ValueError:
            return None

    return {
        "players": players,
        "tournament": tournament,
        "slot": slot,
        "start": utc_dt,
    }


def parse_tennis_name_notimes(raw: str) -> dict | None:
    """
    Parse a Tennis PPV channel name that has players and tournament but no time.
    e.g. "Boulter, Katie vs Cristian, Jaqueline - WTA Rouen :Tennis 03"
    Returns {players, tournament, slot} or None if it doesn't match.
    """
    m = _TENNIS_NOTIMES_RE.match(raw or "")
    if not m:
        return None
    return {
        "players": m.group(1).strip(),
        "tournament": m.group(2).strip(),
        "slot": int(m.group(3)),
    }


def _add_prog(tv: Element, cid: str, start: datetime, stop: datetime,
              title: str, desc: str, live: bool = False) -> None:
    prog = SubElement(tv, "programme",
                      start=_fmt(start), stop=_fmt(stop), channel=cid)
    SubElement(prog, "title").text = title
    SubElement(prog, "desc").text = desc
    SubElement(prog, "category").text = "Sports"
    if live:
        SubElement(prog, "live")


def _fill(tv: Element, cid: str, start: datetime, end: datetime,
          title: str, desc: str) -> None:
    """Tile a time range with BLOCK_HOURS-wide programme entries."""
    t = start
    while t < end:
        t2 = min(t + timedelta(hours=BLOCK_HOURS), end)
        _add_prog(tv, cid, t, t2, title, desc)
        t = t2


def _countdown(t: datetime, ev_start: datetime) -> str:
    """Human-readable time remaining from t until ev_start."""
    total_min = int((ev_start - t).total_seconds() / 60)
    if total_min >= 60:
        return f"{total_min // 60}h to start"
    return f"{total_min}m to start"


def _fill_countdown(tv: Element, cid: str, start: datetime, end: datetime,
                    ev_start: datetime, event_label: str, desc: str = "") -> None:
    """Tile pre-event range with per-block countdown titles.

    title — countdown text e.g. "2h to start — Liverpool vs PSG · UCL"
    desc  — richer match detail (venue, time, date); falls back to title if omitted
    """
    t = start
    while t < end:
        t2 = min(t + timedelta(hours=BLOCK_HOURS), end)
        countdown = _countdown(t, ev_start)
        title = f"{countdown} — {event_label}"
        _add_prog(tv, cid, t, t2, title, desc or title)
        t = t2


def _reorder_players(players_str: str) -> str:
    """Convert "Last, First vs Last, First" → "First Last vs First Last" (no API calls)."""
    parts = [p.strip() for p in players_str.split(" vs ")]
    out = []
    for p in parts:
        if "," in p:
            last, first = p.split(",", 1)
            out.append(f"{first.strip()} {last.strip()}")
        else:
            out.append(p)
    return " vs ".join(out)


def build_tennis_epg(channels: list[ChannelRecord]) -> str:
    """Build XMLTV XML string for all Tennis PPV channels."""
    now = datetime.now(timezone.utc)
    year = now.year
    win_start = _floor_block(now)
    win_end = win_start + timedelta(hours=WINDOW_HOURS)

    tennis = sorted(
        [c for c in channels if c.target_group == "Tennis PPV"],
        key=lambda c: _slot(c.raw_display_name) or 999,
    )

    tv = Element("tv")
    tv.set("source-info-name", "StreamWrangler EPG")

    # --- Channel declarations ---
    for ch in tennis:
        s = _slot(ch.raw_display_name)
        cid = f"WrangleTennis{s:02d}" if s else ch.tvg_id
        dname = f"Tennis  {s:02d}" if s else ch.display_name
        chan = SubElement(tv, "channel", id=cid)
        SubElement(chan, "display-name").text = dname
        if ch.tvg_logo:
            SubElement(chan, "icon", src=ch.tvg_logo)

    # --- Programme entries ---
    for ch in tennis:
        raw = ch.raw_display_name or ""
        s = _slot(raw)
        cid = f"WrangleTennis{s:02d}" if s else ch.tvg_id
        ev = parse_tennis_name(raw, year)

        if ev:
            ev_start = ev["start"]
            ev_end = ev_start + timedelta(hours=MATCH_HOURS)
            tourn = ev["tournament"]

            # Reorder "Last, First" → "First Last" (no API calls)
            players = _reorder_players(ev["players"])

            # Time label in local (Chicago) timezone for description text
            local_t = ev_start.astimezone(LOCAL_TZ)
            time_label = local_t.strftime("%-I:%M %p")  # "6:30 AM"
            tz_abbr = local_t.strftime("%Z")             # "CDT" / "CST"
            date_label = ev_start.strftime("%b %-d %Y")

            event_label = f"{players} · {tourn}"
            live_title = f"{tourn} | {players}"
            live_desc = f"{players} · {time_label} {tz_abbr} on {date_label}"
            post_title = "Signing Off"

            if ev_start >= win_end:
                # Event entirely beyond coverage window — full countdown
                _fill_countdown(tv, cid, win_start, win_end, ev_start, event_label, live_desc)
            elif ev_end <= win_start:
                # Event already over before coverage window
                _fill(tv, cid, win_start, win_end, post_title, post_title)
            else:
                # Pre-event phase with countdown
                if ev_start > win_start:
                    _fill_countdown(tv, cid, win_start, ev_start, ev_start, event_label, live_desc)
                # Live block (clipped to window if partially outside)
                live_s = max(ev_start, win_start)
                live_e = min(ev_end, win_end)
                _add_prog(tv, cid, live_s, live_e, live_title, live_desc, live=True)
                # Post-event phase
                if ev_end < win_end:
                    _fill(tv, cid, ev_end, win_end, post_title, post_title)
        else:
            notimes = parse_tennis_name_notimes(raw)
            if notimes:
                # Name has players + tournament but no time — fill today (local midnight-to-midnight)
                players = _reorder_players(notimes["players"])
                tourn = notimes["tournament"]
                tbd_title = f"TBD: {players} · {tourn}"
                local_today = datetime.now(LOCAL_TZ)
                day_start = local_today.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
                day_end = day_start + timedelta(hours=24)
                _fill(tv, cid, day_start, day_end, tbd_title, tbd_title)
            else:
                # Truly blank channel — no event info at all
                _fill(tv, cid, win_start, win_end, "No Event Today", "No Event Today")

    xml_indent(tv, space="  ")
    body = tostring(tv, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


def write_epg(path: Path = EPG_PATH) -> int:
    """Load channels.json, generate Tennis PPV EPG, write to path. Returns channel count."""
    channels = load_store()
    content = build_tennis_epg(channels)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return sum(1 for c in channels if c.target_group == "Tennis PPV")


PARAMOUNT_EPG_PATH = Path("/home/geoffrey/infra/compose/dispatcharr/data/epgs/wrangle_paramount.xml")

# Slot pattern: ":Paramount+  18"
_PARAMOUNT_SLOT_RE = re.compile(r":Paramount\+\s+(\d+)", re.IGNORECASE)

# Actual provider format (no event-type section):
#   "Concacaf W Qualifiers: Dominica vs Antigua & Barbuda @ Apr 14 2:50 PM :Paramount+  07"
# The optional "- Event Type" seen in Tennis is absent here.
_PARAMOUNT_RE = re.compile(
    r"^(.+?)\s*@\s*(\w{3}\s+\d{1,2})\s+(\d{1,2}:\d{2})(?:\s*[AP]M)?(?:\s*-\s*(.+?))?\s*:Paramount\+\s+(\d+)\s*$",
    re.IGNORECASE,
)

# Keywords that indicate a live sports event (longer 3h block + Sports category)
_SPORTS_KEYWORDS = re.compile(
    r"\b(NFL|NBA|NHL|MLB|MLS|UFC|soccer|football|basketball|hockey|baseball|"
    r"tennis|golf|rugby|cricket|f1|formula|boxing|wrestling|NASCAR|"
    r"NCAA|college|league|cup|championship|playoff|series|"
    r"qualifier|qualifiers|concacaf|conmebol|uefa|fifa|"
    r"match|game|final|semifinal|tournament|open|grand.?prix)\b",
    re.IGNORECASE,
)


def _paramount_slot(raw: str) -> int | None:
    m = _PARAMOUNT_SLOT_RE.search(raw or "")
    return int(m.group(1)) if m else None


def _is_sport(event_type: str) -> bool:
    return bool(_SPORTS_KEYWORDS.search(event_type))


def parse_paramount_name(raw: str, year: int) -> dict | None:
    """
    Parse a Paramount+ PPV channel name into event metadata.
    Returns None for blank channels (no event info).
    Times are parsed as Europe/Paris and returned as UTC datetimes.
    """
    m = _PARAMOUNT_RE.match(raw)
    if not m:
        return None

    title = m.group(1).strip()
    date_str = m.group(2).strip()
    time_str = m.group(3).strip()
    event_type = (m.group(4) or "").strip()
    slot = int(m.group(5))

    # Provider uses 12h format (e.g. "2:50 PM"), unlike Tennis which uses 24h.
    # Adjust hour manually since strptime %H won't handle AM/PM.
    ampm = m.group(0)  # full match — scan for AM/PM in original string
    ampm_m = re.search(r'\b(AM|PM)\b', raw, re.IGNORECASE)
    hour, minute = (int(x) for x in time_str.split(":"))
    if ampm_m:
        suffix = ampm_m.group(1).upper()
        if suffix == "PM" and hour != 12:
            hour += 12
        elif suffix == "AM" and hour == 12:
            hour = 0

    now = datetime.now(timezone.utc)
    for y in (year, year + 1):
        try:
            local_dt = datetime(y, 1, 1)  # placeholder — set below
            from datetime import date as _date
            parsed_date = datetime.strptime(f"{y} {date_str}", "%Y %b %d")
            local_dt = parsed_date.replace(hour=hour, minute=minute, tzinfo=PARAMOUNT_SOURCE_TZ)
            utc_dt = local_dt.astimezone(timezone.utc)
            if utc_dt >= now - timedelta(days=14):
                break
        except ValueError:
            return None

    return {
        "title": title,
        "event_type": event_type,
        "slot": slot,
        "start": utc_dt,
        "is_sport": _is_sport(event_type) or _is_sport(title),
    }


def _lookup_match_venue(title: str, ev_start: datetime, api_key: str,
                        team_cache: dict, event_cache: dict, venue_cache: dict) -> str:
    """
    For a 'League: Home vs Away' soccer title, look up the venue via TheSportsDB.
    Returns venue name string or "" if not found / API error.
    """
    from .sportsdb import fetch_team_by_name, fetch_next_events, fetch_venue, event_utc

    # Extract home team: "League: Home vs Away" → "Home"
    if " vs " not in title:
        return ""
    after_colon = title.split(": ", 1)[-1] if ": " in title else title
    home_team = after_colon.split(" vs ")[0].strip()
    if not home_team:
        return ""

    try:
        team = fetch_team_by_name(home_team, api_key, team_cache)
        if not team:
            return ""
        team_id = str(team.get("idTeam") or "")
        if not team_id:
            return ""

        if team_id not in event_cache:
            event_cache[team_id] = fetch_next_events(team_id, api_key)

        ev_date = ev_start.date()
        for ev in event_cache[team_id]:
            ev_utc = event_utc(ev)
            if ev_utc.date() == ev_date:
                venue_id = str(ev.get("idVenue") or "").strip()
                if venue_id and venue_id != "0":
                    venue = fetch_venue(venue_id, api_key, venue_cache)
                    return (venue.get("strVenue") or "").strip() if venue else ""
    except Exception:
        pass
    return ""


def build_paramount_epg(channels: list[ChannelRecord]) -> str:
    """Build XMLTV XML string for all Paramount+ PPV channels."""
    now = datetime.now(timezone.utc)
    year = now.year
    win_start = _floor_block(now)
    win_end = win_start + timedelta(hours=WINDOW_HOURS)

    paramount = sorted(
        [c for c in channels if c.target_group == "Paramount+ PPV"],
        key=lambda c: _paramount_slot(c.raw_display_name) or 999,
    )

    # Venue lookup caches — shared across all channels in this run
    try:
        from .sportsdb import load_sportsdb_config
        sportsdb_cfg = load_sportsdb_config()
        sdb_api_key = (sportsdb_cfg or {}).get("api_key", "123")
    except Exception:
        sdb_api_key = "123"
    _team_cache: dict = {}
    _event_cache: dict = {}
    _venue_cache: dict = {}

    tv = Element("tv")
    tv.set("source-info-name", "StreamWrangler EPG")

    for ch in paramount:
        s = _paramount_slot(ch.raw_display_name)
        cid = f"WrangleParamount{s:02d}" if s else ch.tvg_id
        dname = f"Paramount+  {s:02d}" if s else ch.display_name
        chan = SubElement(tv, "channel", id=cid)
        SubElement(chan, "display-name").text = dname
        if ch.tvg_logo:
            SubElement(chan, "icon", src=ch.tvg_logo)

    for ch in paramount:
        raw = ch.raw_display_name or ""
        s = _paramount_slot(raw)
        cid = f"WrangleParamount{s:02d}" if s else ch.tvg_id
        ev = parse_paramount_name(raw, year)

        if ev:
            ev_duration = MATCH_HOURS if ev["is_sport"] else BLOCK_HOURS
            ev_start = ev["start"]
            ev_end = ev_start + timedelta(hours=ev_duration)
            category = "Sports" if ev["is_sport"] else "Entertainment"

            local_t = ev_start.astimezone(LOCAL_TZ)
            time_label = local_t.strftime("%-I:%M %p")
            tz_abbr = local_t.strftime("%Z")
            date_label = ev_start.strftime("%b %-d %Y")

            # Venue lookup for soccer matches
            venue_name = ""
            if ev["is_sport"] and " vs " in ev["title"]:
                venue_name = _lookup_match_venue(
                    ev["title"], ev_start, sdb_api_key,
                    _team_cache, _event_cache, _venue_cache,
                )

            event_label = f"{ev['title']} · {ev['event_type']}" if ev["event_type"] else ev["title"]
            live_title = f"{ev['event_type']} | {ev['title']}" if ev["event_type"] else ev["title"]
            if venue_name:
                live_desc = f"{ev['title']} · {time_label} {tz_abbr} on {date_label} at {venue_name}"
            else:
                live_desc = f"{ev['title']} · {time_label} {tz_abbr} on {date_label}"
            post_title = "Signing Off"

            # Attach category to live programme
            def _add_prog_cat(tv, cid, start, stop, title, desc, live=False):
                prog = SubElement(tv, "programme",
                                  start=_fmt(start), stop=_fmt(stop), channel=cid)
                SubElement(prog, "title").text = title
                SubElement(prog, "desc").text = desc
                SubElement(prog, "category").text = category
                if live:
                    SubElement(prog, "live")

            if ev_start >= win_end:
                _fill_countdown(tv, cid, win_start, win_end, ev_start, event_label, live_desc)
            elif ev_end <= win_start:
                _fill(tv, cid, win_start, win_end, post_title, post_title)
            else:
                if ev_start > win_start:
                    _fill_countdown(tv, cid, win_start, ev_start, ev_start, event_label, live_desc)
                live_s = max(ev_start, win_start)
                live_e = min(ev_end, win_end)
                _add_prog_cat(tv, cid, live_s, live_e, live_title, live_desc, live=True)
                if ev_end < win_end:
                    _fill(tv, cid, ev_end, win_end, post_title, post_title)
        else:
            _fill(tv, cid, win_start, win_end, "No Event Today", "No Event Today")

    xml_indent(tv, space="  ")
    body = tostring(tv, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


def write_paramount_epg(path: Path = PARAMOUNT_EPG_PATH) -> int:
    """Load channels.json, generate Paramount+ PPV EPG, write to path. Returns channel count."""
    channels = load_store()
    included = [c for c in channels if c.target_group == "Paramount+ PPV" and c.status == "included"]
    if not included:
        return 0
    content = build_paramount_epg(channels)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return len(included)


SPORTS_EPG_PATH = Path("/home/geoffrey/infra/compose/dispatcharr/data/epgs/wrangle_sports.xml")
LOGOS_EPG_PATH  = Path("/home/geoffrey/infra/compose/dispatcharr/data/epgs/wrangle_logos.xml")


def build_sportsdb_epg(config: dict) -> str:
    """Build XMLTV XML string for all SportsDB team channels."""
    from .sportsdb import (
        channel_epg_map, fetch_next_events, fetch_venue, fetch_league_table,
        venue_display_tz, event_utc, team_standing, soccer_season,
    )

    api_key = config.get("api_key", "123")
    teams = config.get("teams") or []

    now = datetime.now(timezone.utc)
    year = now.year
    month = now.month
    win_start = _floor_block(now)
    win_end = win_start + timedelta(hours=WINDOW_HOURS)

    venue_cache: dict = {}
    table_cache: dict = {}

    tv = Element("tv")
    tv.set("source-info-name", "StreamWrangler EPG")

    logo_base = (config.get("logo_base_url") or "").rstrip("/")

    # Channel declarations
    for team in teams:
        cid = team["epg_id"]
        chan = SubElement(tv, "channel", id=cid)
        SubElement(chan, "display-name").text = team["name"]
        if logo_base and team.get("logo"):
            SubElement(chan, "icon", src=f"{logo_base}/{team['logo']}")

    # Programme entries
    for team in teams:
        cid = team["epg_id"]
        team_id = str(team["team_id"])
        team_name = team["name"]
        sport = team.get("sport", "soccer")
        league_id = str(team.get("league_id") or "")

        try:
            events = fetch_next_events(team_id, api_key)
        except Exception as e:
            import sys
            print(f"[sports EPG] WARNING: API call failed for {team_name}: {e}", file=sys.stderr)
            events = []

        # Find the most relevant event: in-progress or the next upcoming one
        next_event = None
        for ev in events:
            ev_utc_dt = event_utc(ev)
            # Include events that started within the last MATCH_HOURS (may be in progress)
            if ev_utc_dt >= now - timedelta(hours=MATCH_HOURS):
                next_event = ev
                break

        if not next_event:
            _fill(tv, cid, win_start, win_end, "No Match Scheduled", "No Match Scheduled")
            continue

        ev_start = event_utc(next_event)
        ev_end = ev_start + timedelta(hours=MATCH_HOURS)

        home = (next_event.get("strHomeTeam") or "").strip()
        away = (next_event.get("strAwayTeam") or "").strip()
        league = (next_event.get("strLeague") or "").strip()

        match_label = f"{home} vs {away}"
        event_label = f"{home} vs {away} · {league}"

        # Venue name for description (no longer used for timezone)
        venue_id = str(next_event.get("idVenue") or "").strip()
        venue = None
        if venue_id and venue_id != "0":
            try:
                venue = fetch_venue(venue_id, api_key, venue_cache)
            except Exception:
                pass
        venue_name = (venue.get("strVenue") or "").strip() if venue else ""

        # Kickoff time in US Central — consistent display timezone regardless of venue location
        local_t = ev_start.astimezone(LOCAL_TZ)
        time_label = local_t.strftime("%-I:%M %p")    # "2:00 PM"
        tz_abbr = local_t.strftime("%Z")               # "CDT" / "CST"
        date_label = ev_start.strftime("%b %-d %Y")

        # League standing for PL / La Liga descriptions
        standing_str = ""
        if sport == "soccer" and league_id:
            season = soccer_season(year, month)
            try:
                table = fetch_league_table(league_id, season, api_key, table_cache)
                pos = team_standing(table, team_name)
                if pos:
                    standing_str = f" · {pos}"
            except Exception:
                pass

        # Build description line
        if venue_name:
            desc = f"{match_label} · {time_label} {tz_abbr} on {date_label} at {venue_name}{standing_str}"
        else:
            desc = f"{match_label} · {time_label} {tz_abbr} on {date_label}{standing_str}"

        live_title = f"{league} | {match_label}"
        post_title = f"Event Has Ended \u00b7 {match_label}"

        if ev_start >= win_end:
            # Event entirely beyond coverage window — full countdown
            _fill_countdown(tv, cid, win_start, win_end, ev_start, event_label, desc)
        elif ev_end <= win_start:
            # Event already over before coverage window
            _fill(tv, cid, win_start, win_end, post_title, post_title)
        else:
            # Pre-event countdown
            if ev_start > win_start:
                _fill_countdown(tv, cid, win_start, ev_start, ev_start, event_label, desc)
            # Live block (clipped to window)
            live_s = max(ev_start, win_start)
            live_e = min(ev_end, win_end)
            _add_prog(tv, cid, live_s, live_e, live_title, desc, live=True)
            # Post-event
            if ev_end < win_end:
                _fill(tv, cid, ev_end, win_end, post_title, post_title)

    xml_indent(tv, space="  ")
    body = tostring(tv, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


def build_logos_epg(channels: list[ChannelRecord]) -> str:
    """
    Build a channel-declarations-only XMLTV for all included channels that
    have a local logo URL. No programme entries — purely for icon delivery.

    Channels already covered by a dedicated EPG (Tennis, Paramount+, Sports)
    are included here too so Dispatcharr always has one place to look.
    """
    from .sportsdb import load_sportsdb_config, channel_epg_map
    from .output import _sports_epg_map

    sports_map = _sports_epg_map()

    tv = Element("tv")
    tv.set("source-info-name", "StreamWrangler Logos")

    seen_ids: set[str] = set()
    channel_ids: list[str] = []  # ordered, for programme entries

    for ch in channels:
        if ch.status != "included":
            continue
        if not ch.tvg_logo:
            continue

        # Use the same tvg_id logic as the M3U output so channel IDs match
        if ch.target_group == "Tennis PPV":
            s = _slot(ch.raw_display_name)
            cid = f"WrangleTennis{s:02d}" if s else ch.tvg_id
        elif ch.target_group == "Paramount+ PPV":
            s = _paramount_slot(ch.raw_display_name)
            cid = f"WrangleParamount{s:02d}" if s else ch.tvg_id
        elif ch.channel_uid in sports_map:
            cid = sports_map[ch.channel_uid]
        else:
            cid = ch.tvg_id

        if not cid or cid in seen_ids:
            continue
        seen_ids.add(cid)
        channel_ids.append(cid)

        chan = SubElement(tv, "channel", id=cid)
        SubElement(chan, "display-name").text = ch.display_name
        SubElement(chan, "icon", src=ch.tvg_logo)

    # Add a minimal 24-hour placeholder programme per channel so Dispatcharr
    # recognises the file as a valid EPG and picks up the icon declarations.
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end   = day_start + timedelta(hours=24)
    for cid in channel_ids:
        prog = SubElement(tv, "programme",
                          start=_fmt(day_start), stop=_fmt(day_end), channel=cid)
        SubElement(prog, "title").text = " "

    xml_indent(tv, space="  ")
    body = tostring(tv, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


def write_logos_epg(path: Path = LOGOS_EPG_PATH) -> int:
    """Write channel-icons XMLTV for all included channels. Returns channel count."""
    channels = load_store()
    content = build_logos_epg(channels)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return sum(1 for c in channels if c.status == "included" and c.tvg_logo)


def write_sports_epg(path: Path = SPORTS_EPG_PATH) -> int:
    """Fetch SportsDB events and write sports EPG. Returns team count (0 if no config)."""
    from .sportsdb import load_sportsdb_config
    config = load_sportsdb_config()
    if not config:
        return 0
    content = build_sportsdb_epg(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return len(config.get("teams") or [])
