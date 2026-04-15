# Stream Wrangle Broadcast — Planning Document

A personal broadcast network layer on top of StreamWrangler. You define a schedule;
the system runs FFmpeg to stitch sources together and serves the result as an M3U that
Dispatcharr can ingest (keeping all streams routed through the VPN proxy).

---

## Concept

Take live IPTV streams (already normalized and proxied by Dispatcharr) and
re-broadcast them as one or more custom channels with a fixed schedule. Tune in at 6pm
and Jeopardy is there. Tune in Saturday at 3pm and the Liverpool match is there.

The key property: **broadcast model** — the channel is always advancing. You tune in
mid-stream; you don't start from the beginning.

---

## Architecture (sketch)

```
Provider feed
     │
     ▼
Dispatcharr (VPN proxy)
     │  stream URLs
     ▼
Wrangler Broadcast
  ├── Schedule store (YAML)
  ├── FFmpeg supervisor (one process per channel)
  ├── HLS / HTTP stream server
  └── EPG generator (XMLTV from schedule)
     │  M3U + EPG
     ▼
Dispatcharr (re-ingests broadcast channels)
     │
     ▼
Clients (TV, phone, etc.)
```

The output M3U points back to the local Wrangler Broadcast server. Dispatcharr proxies
those too, so all traffic stays on the VPN.

---

## Source Material

| Type | Status |
|---|---|
| Live IPTV streams (from existing provider feed) | Primary, in scope |
| Local files (recordings, downloads) | Nice to have, future |

---

## Scale

- **1–2 channels** to start (e.g. kids channel, sports channel)
- A handful of themed channels is plausible (Liverpool FC, Champions League)
- Not expected to be reprogrammed constantly — mostly set-and-leave with occasional
  additions for live events

---

## Gap Behavior

**Open question — options:**

1. **Channel goes dead** — stream unavailable during unscheduled time; player shows
   an error. Simple. No FFmpeg overhead when idle.

2. **Standby slate** — FFmpeg serves a static image (logo + "Next up: [show] at [time]")
   during gaps. Always-on, looks polished. Requires FFmpeg to keep running 24/7.

3. **Fallback stream** — during gaps, drop through to a designated live source
   (e.g. kids channel falls back to Cartoon Network). Appealing but requires choosing
   a fallback for each channel; unclear what the right defaults are.

**Leaning toward:** channel goes dead (simplest, avoids 24/7 FFmpeg cost). Standby
slate is the "nice" option if the overhead is acceptable.

---

## Scheduling

### Manual programming

You explicitly define: source stream + start time + (optionally) duration or end time.

```yaml
# Example schedule entry
channels:
  - id: kids
    name: "Kids Channel"
    slots:
      - stream_uid: cartoon_network__us_kids    # from channels.json
        start: "2026-04-15T18:00:00"
        duration: 60m                           # or end: "2026-04-15T19:00:00"
      - stream_uid: nickelodeon__us_kids
        start: "2026-04-15T19:00:00"
        duration: 90m
```

A TUI (or simple YAML edit) for managing slots. Slots don't need to be contiguous —
gaps are allowed and handled per the gap behavior choice above.

### Auto-scheduling (future / rule-based)

Themed channels defined by rules, not manual slot entries. The scheduler queries
TheSportsDB (already integrated) for upcoming fixtures matching the rules, finds the
right stream from channels.json, and generates slots automatically.

```yaml
# Example rule-based channel
  - id: liverpool
    name: "Liverpool FC"
    rules:
      - type: team_fixtures
        team: "Liverpool"          # TheSportsDB team name
        competition: any           # or "Premier League", "Champions League", etc.
        prefer_stream_group: "UK Sports"
```

The auto-scheduler produces the same YAML slot entries as manual programming — so you
can review or override before it goes live.

---

## EPG

The broadcast EPG is generated from the schedule, not pulled from a provider.

- Each scheduled slot → one `<programme>` entry (title, start, stop)
- Gap slots → either omitted or a generic "Off Air" entry
- Regenerated whenever the schedule changes
- Output: XMLTV file alongside the existing `wrangle_tennis.xml` pattern

If a source channel has EPG data (e.g. Jeopardy on CBS), we could optionally copy its
programme title/description into the broadcast EPG rather than just using the stream name.

---

## FFmpeg Strategy

For live-to-live switching between sources, the cleanest approach is likely:

- One FFmpeg process per broadcast channel
- Uses a **live HLS playlist** that Wrangler Broadcast updates at each slot boundary
- FFmpeg reads from the playlist → remuxes to HLS output → served locally
- At switchover time: update the input playlist → FFmpeg picks up the new source

Alternative: a **scheduling proxy** that, at the slot boundary, redirects the stream
endpoint to the new source URL. Simpler but the "broadcast" property (mid-stream tune-in)
is harder to preserve cleanly.

Source switching will cause a brief interruption at slot boundaries — this is acceptable
for a personal setup.

---

## Open Questions

- [ ] **Gap behavior**: dead channel vs. standby slate vs. fallback — pick one
- [ ] **Standby slate overhead**: is always-on FFmpeg acceptable? (probably ~5–10% CPU per
      channel for a static image stream)
- [ ] **Fallback content**: if fallback is chosen, what's the default for each channel type?
- [ ] **Slot boundary interruption**: is a 2–5 second gap on stream switch acceptable?
- [ ] **Schedule UI**: YAML editing acceptable, or do we want a TUI for slot management?
- [ ] **Auto-scheduler scope**: first build is manual-only; auto-scheduler generates slots
      as a second phase?
- [ ] **EPG enrichment**: copy source EPG titles/descriptions, or just use stream name?
- [ ] **Integration point**: does this live in the StreamWrangler repo, or a separate
      `wrangler-broadcast` service?

---

## What This Is Not

- Not a DVR / recording system
- Not a transcoding service (remux only — preserve source quality)
- Not a replacement for Dispatcharr — it feeds into it
