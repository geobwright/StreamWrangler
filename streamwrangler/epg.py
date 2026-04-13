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

# Provider encodes times in Europe/Paris (CEST = UTC+2 in summer, CET = UTC+1 in winter)
SOURCE_TZ = ZoneInfo("Europe/Paris")
# Display timezone for programme descriptions
LOCAL_TZ = ZoneInfo("America/Chicago")

# Tennis name pattern:
#   "Players @ Apr 12 15:00 PM - ATP Monte Carlo :Tennis  03"
# Times use 24h format; the AM/PM suffix is redundant but present.
_TENNIS_RE = re.compile(
    r"^(.+?)\s*@\s*(\w{3}\s+\d{1,2})\s+(\d{1,2}:\d{2})(?:\s*[AP]M)?\s*-\s*(.+?)\s*:Tennis\s+(\d+)\s*$",
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
                    ev_start: datetime, event_label: str) -> None:
    """Tile pre-event range with per-block countdown titles."""
    t = start
    while t < end:
        t2 = min(t + timedelta(hours=BLOCK_HOURS), end)
        countdown = _countdown(t, ev_start)
        title = f"{countdown} — {event_label}"
        _add_prog(tv, cid, t, t2, title, title)
        t = t2


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
            players = ev["players"]
            tourn = ev["tournament"]

            # Time label in local (Chicago) timezone for description text
            local_t = ev_start.astimezone(LOCAL_TZ)
            time_label = local_t.strftime("%I:%M%p")   # "08:00AM"
            date_label = ev_start.strftime("%b %-d %Y")

            event_label = f"{players} · {tourn}"
            live_title = f"\u1d4f\u1d49\u02b7{tourn} | {players}"   # ᴺᵉʷTournament | Players
            live_desc = f"{players} at {time_label} on {date_label}"
            post_title = "Signing Off"

            if ev_start >= win_end:
                # Event entirely beyond coverage window — full countdown
                _fill_countdown(tv, cid, win_start, win_end, ev_start, event_label)
            elif ev_end <= win_start:
                # Event already over before coverage window
                _fill(tv, cid, win_start, win_end, post_title, post_title)
            else:
                # Pre-event phase with countdown
                if ev_start > win_start:
                    _fill_countdown(tv, cid, win_start, ev_start, ev_start, event_label)
                # Live block (clipped to window if partially outside)
                live_s = max(ev_start, win_start)
                live_e = min(ev_end, win_end)
                _add_prog(tv, cid, live_s, live_e, live_title, live_desc, live=True)
                # Post-event phase
                if ev_end < win_end:
                    _fill(tv, cid, ev_end, win_end, post_title, post_title)
        else:
            # Blank channel — no event scheduled
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
