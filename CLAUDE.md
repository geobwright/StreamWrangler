# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**StreamWrangler** — IPTV normalization and channel orchestration engine. Ingests a large provider M3U feed (~35k channels), filters to a curated target set, normalizes names and quality metadata, and outputs a clean M3U for Dispatcharr.

## Commands

```bash
# Install dependencies (editable install from repo root)
pip install -e .

# Run the full ingest pipeline (parse → filter → normalize → write channels.json)
wrangle ingest                  # first run
wrangle ingest --force          # re-ingest, preserving include/exclude decisions

# Browse raw feed variants before normalization — probe and rank quality
wrangle inspect                 # loads full filtered feed, no normalization/dedup

# Open curation TUI
wrangle curate

# Assign channel numbers — AI proposes, TUI to review/adjust
wrangle number                  # generate AI proposal (if no numbering.yaml) then open TUI
wrangle number --generate       # force-regenerate AI proposal, overwrite numbering.yaml
wrangle number --apply          # apply numbering.yaml to channels.json headlessly

# Generate EPG from PPV channel names + SportsDB team channels
wrangle epg                     # build all four XMLTV EPGs → Dispatcharr epgs/

# Download and match channel logos
wrangle logos                   # match to tv-logo/tv-logos repo, download 512×512 PNGs
wrangle logos --push            # also push to Dispatcharr via REST API
wrangle logos --dry-run         # report matches without downloading

# Write final M3U to Dispatcharr
wrangle output

# Inspect feed without ingesting
wrangle analyze                 # group/channel counts from provider URL
wrangle filter-report           # what passes the group filter

# Current curation progress
wrangle status
```

Provider URL is stored in `config/config.local.yaml` (gitignored — never commit).

## Architecture

```
streamwrangler/
  parser.py         — M3U → RawChannel list
  filter.py         — groups.yaml → keeps only mapped, enabled groups; URL filters (pro.* blocked)
  normalizer.py     — name cleaning, quality/codec detection, variant deduplication
  store.py          — channels.json read/write, ChannelRecord dataclass
  probe_cache.py    — probe result cache keyed by stable channel ID (last URL path segment)
  numbering.py      — YAML-backed channel numbering; language tag detection; display name builder
  ai_numbering.py   — Claude API (opus-4-6) proposal for block grouping and number assignment
  epg.py            — Tennis/Paramount+/Logos XMLTV generators + SportsDB XMLTV builder
  sportsdb.py       — TheSportsDB API client (free tier, rate limiter, team/venue/table lookups)
  logos.py          — Logo matcher/downloader (tv-logo/tv-logos) + Dispatcharr REST push
  tennis_rankings.py — ATP/WTA player rank lookup + 7-day cache (TheSportsDB)
  output.py         — M3U generator → Dispatcharr path
cli/
  commands.py       — Typer CLI: ingest, inspect, curate, number, epg, logos, output, analyze, status
tui/
  app.py            — Textual TUI for include/exclude curation (wrangle curate)
  inspect.py        — Textual TUI for raw feed browsing and pre-ingest probing (wrangle inspect)
  number.py         — Textual TUI for reviewing/adjusting AI-proposed channel numbering
config/
  groups.yaml           — source group → target group mapping (prefix or exact)
  normalization.yaml    — strip/replace rules for name cleaning
  numbering.yaml        — AI-proposed + user-adjusted channel numbering plan (gitignored)
  sportsdb.yaml         — TheSportsDB team configs for sports EPG (gitignored)
  config.local.yaml     — provider URL (GITIGNORED)
data/
  channels.json         — canonical channel store (decisions persist here)
  probe_cache.json      — ffprobe results keyed by channel ID (persists across ingests)
```

## Intended Workflow

```
wrangle ingest          → build initial channels.json
wrangle inspect         → search channel families (e.g. "eurosport"), probe variants,
                          see ★ (Tier1 winner) and ◆ (Tier2 winner) rankings
wrangle ingest --force  → re-run dedup using probe cache for accurate scoring
wrangle curate          → include/exclude with quality already verified
wrangle number          → AI proposes block/number layout → review in TUI → apply
wrangle output          → write M3U to Dispatcharr path
wrangle epg             → generate all four XMLTV EPGs (sports, tennis, paramount+, logos)
```

The probe cache (`data/probe_cache.json`) persists across all commands and is never
overwritten by ingest — it only grows. Rankings in inspect are recomputed from the
cache on every startup.

## Pipeline

1. **Parse** — `parser.py` reads raw M3U into `RawChannel` objects
2. **Filter** — `filter.py` applies `groups.yaml`; discards unmapped/disabled groups.
   `_excluded` target group rules are respected — exact rules block channels that would
   otherwise match a broader prefix rule.
3. **Normalize** — `normalizer.py`:
   - Detects quality tier from name: `4K`, `FHD`, `HD`, `SD`, or `""` (unknown → shown as `Unk`)
   - Detects codec hint from name: `hevc` if name contains HEVC/H.265 (separate from quality)
   - **Probe-aware scoring**: if `probe_cache.json` has a result for a variant's URL, uses
     actual probe quality for dedup scoring (not just advertised name quality)
   - Picks best Tier 1 variant as primary; best Tier 2 variant as low-BW backup
4. **Store** — `store.py` merges normalized channels into `channels.json`, preserving
   curation decisions. Applies probe cache to set `quality_verified=True` on winners.

## Group Filter — groups.yaml

Two rule types supported:

```yaml
# Prefix match — catches all quality/tier variants automatically
- source_group_prefix: "UK| SPORT"
  target_group: "UK Sports"
  enabled: true

# Exact match — use where prefix would be too broad, or to block a sub-group
- source_group: "FR| CANAL+ AFRICA"
  target_group: "_excluded"
  enabled: true
```

**Always prefer prefix rules** — they catch `ᴴᴰ`, `ʰᵉᵛᶜ`, `ᴿᴬᵂ`, `ⱽᴵᴾ`, `ᵁᴴᴰ` variants without separate entries. Exact match takes priority when both could match the same group.

**`_excluded` rules work** — an exact `_excluded` rule blocks channels that would
otherwise match a prefix rule. Use this to suppress sub-groups (e.g. `FR| CANAL+ AFRICA`
inside the `FR| CANAL+` family).

## Probe Cache — probe_cache.json

Keyed by **channel ID** — the last path segment of the provider URL:
```
http://provider.com/username/password/1537488
                                       ↑ cache key (stable across credential/domain rotation)
```

Format:
```json
{
  "1537488": { "quality": "FHD", "codec": "h264", "width": 1920, "height": 1080,
               "bitrate_kbps": 4500, "probed_at": "2026-04-10T..." }
}
```

Cache is read-only during `ingest` — never overwritten, only grows via `inspect`.

## Quality Tiers and Variant Deduplication

Variants are split into two tiers. The normalizer picks the **best Tier 1 variant** as
the primary channel, and the **best Tier 2 variant** as the low-BW backup (only when a
Tier 1 primary exists). Channels with only Tier 2 variants get a single entry, no backup.

| Tier | Quality | Base score |
|---|---|---|
| Tier 1 (high-BW) | 4K | 40 |
| Tier 1 (high-BW) | FHD | 30 |
| Tier 2 (low-BW)  | HD | 20 |
| Tier 2 (low-BW)  | "" (Unk) | 15 |
| Tier 2 (low-BW)  | SD | 10 |

Score modifiers (applied on top of base — never cross tier boundaries):
- HEVC codec hint: **+3** (prefer H.265 over H.264 at same resolution)
- VIP prefix in name: **+2** (tiebreaker within same quality level)
- RAW/BACKUP/LOW in name: **−15** penalty

Effective score ladder (high to low):
`4K HEVC VIP (45) > 4K HEVC (43) > 4K VIP (42) > 4K (40) > FHD HEVC VIP (35) > FHD HEVC (33) > FHD VIP (32) > FHD (30) > HD HEVC VIP (25) > HD HEVC (23) > HD VIP (22) > HD (20) > ...`

**⚠ VIP behavior to revisit:** VIP is currently a tiebreaker only and does not override
quality tier. If provider VIP streams prove more reliable than non-VIP higher-resolution
streams, the VIP bonus may need to be raised to cross tier boundaries.

## ChannelRecord fields (channels.json)

| Field | Description |
|---|---|
| `quality` | Actual quality — updated by ffprobe on probe |
| `advertised_quality` | Quality detected from channel name — re-derived on every ingest |
| `quality_verified` | True once ffprobe has confirmed the quality |
| `codec` | Actual codec from ffprobe (e.g. `h264`, `hevc`) |
| `advertised_codec` | Codec hint from channel name (e.g. `hevc` if name had HEVC/H.265) |

## Inspect TUI Keybindings (wrangle inspect)

| Key | Action |
|---|---|
| p | Probe channel at cursor |
| Space | Mark / unmark for bulk probe |
| b | Probe all marked channels (concurrent, max 4) |
| a | Probe all visible channels (concurrent, max 4) |
| n | Toggle sort by normalized name (clusters variants) |
| u | Show stream URL |
| s | Save probe cache |
| q | Quit (auto-saves) |
| Escape | Clear search / focus table |

### Inspect Rank Column

| Symbol | Meaning |
|---|---|
| `★` green | Tier 1 winner (best 4K/FHD in this normalized-name + target-group) |
| `◆` cyan | Tier 2 winner (best HD/SD in this normalized-name + target-group) |
| `#2`, `#3` | Ranked within tier, not the winner |
| `✗ offline` red | Probed but no response (stream down or geo-blocked) |
| blank | Not yet probed |

Ranking is grouped by `(normalized_name, target_group)` — UK Sports Eurosport 1 and
France Sports Eurosport 1 are ranked independently (different languages/feeds).

## Curate TUI Keybindings (wrangle curate)

| Key | Action |
|---|---|
| Tab / Shift+Tab | Switch focus group list ↔ channel table |
| Space | Toggle include/exclude |
| i / x | Include / Exclude |
| A / X | Include / Exclude all pending in group |
| e | Edit display name |
| p | Probe with ffprobe (async, non-blocking) |
| u | Show stream URL |
| n | Toggle A-Z sort |
| / | Search/filter |
| Escape | Clear search |
| s | Save |
| o | Save, write M3U output, and exit |
| q | Quit (auto-saves) |

## TUI Qual Column Behaviour (curate)

- **Before probe:** shows quality from name (`HD`, `FHD`, `4K`, `SD`, `Unk`)
- **After probe:** always `advertised/actual✓` — e.g. `HD/FHD✓`, `HD/HD✓`, `Unk/HD✓`
- **h265 pill on channel name:**
  - Yellow = name advertises HEVC, not yet probed
  - Green = probe confirmed HEVC (regardless of whether name advertised it)
  - No pill = probed and codec is NOT HEVC, or neither advertised nor confirmed

## Key Design Decisions

- **HEVC is a codec, not a quality tier.** A channel named "BBC One HEVC" gets `quality="HD"` (default), not `quality="HEVC"`. The HEVC hint is stored in `advertised_codec`.
- **Unknown quality shown as `Unk`.** Channels with no quality indicator in name get `quality=""`.
- **`advertised_quality` always re-derived on ingest.** Reflects current detection logic, not a historical value.
- **Probe-verified fields preserved across re-ingest** (`quality`, `quality_verified`, `codec`). Advertised fields always refreshed.
- **Probe-aware dedup** — `normalize_channels()` uses probe cache quality for scoring when available, so the correct variant wins even if the name mis-advertises the resolution.
- **Probe cache keyed by channel ID** (last URL path segment), not full URL — survives credential and domain rotation.
- **`channels.json` migration** in `load_store()` handles legacy records where `quality="HEVC"` was stored under the old scheme.
- **Prefix group matching** — always use `source_group_prefix` in groups.yaml. Exact string matching caused persistent blind spots.
- **`_excluded` exact rules** block sub-groups inside a broader prefix family (e.g. `FR| CANAL+ AFRICA` inside `FR| CANAL+`).
- **Provider URL** contains credentials — stored only in `config/config.local.yaml`, gitignored.
- **Output M3U** written to `/home/geoffrey/infra/compose/dispatcharr/data/m3us/` (separate repo).

## Channel Numbering — wrangle number

Numbering plan lives in `config/numbering.yaml` (gitignored — contains personal lineup decisions).

**Display name format:** `<base> <quality> [LANG]` — e.g. `Eurosport 1 FHD`, `Eurosport 1 HD [FR]`
- Quality suffix: probe-verified if available, else advertised. Omitted if empty.
- Language tag: non-English source groups only (FR, DE, ES, IT, PT, NL, …). UK/US/AU = no tag.
- Backup channels (`__bk`) get their own row and number, placed immediately after their primary.

**YAML schema:**
```yaml
blocks:
  - name: Sports
    start: 400
    channels:
      - uid: eurosport_1__uk_sports
        number: 401
        display_name: Eurosport 1 FHD
      - uid: eurosport_1__uk_sports__bk
        number: 402
        display_name: Eurosport 1 HD
```

**Number TUI keybindings:**

| Key | Action |
|---|---|
| Tab / Shift+Tab | Switch focus block list ↔ channel table |
| Shift+J / Shift+K | Move channel down / up within block |
| e | Edit display name |
| n | Edit channel number |
| m | Move channel to a different block |
| s | Save numbering.yaml |
| a | Apply to channels.json (writes numbers + display names) |
| q | Quit (auto-saves) |

## URL Filters (filter.py)

Applied in `filter_channels()` before any downstream processing:

- **`pro.*` hostnames** — dropped. Geoffrey owns this domain; channels appeared as feed
  contamination but are not valid provider streams. Remove the `://pro.` check if the
  domain returns as a legitimate source.

## EPG System

`wrangle epg` writes four XMLTV files in one run:

| File | Source | Channel IDs |
|---|---|---|
| `wrangle_sports.xml` | TheSportsDB live API | `WrangleSports_LFC`, `_RMA`, `_WXM` |
| `wrangle_tennis.xml` | Tennis PPV channel names | `WrangleTennis01`–`NN` |
| `wrangle_paramount.xml` | Paramount+ PPV channel names | `WrangleParamount01`–`NN` |
| `wrangle_logos.xml` | channels.json (icon delivery only) | same IDs as M3U |

### Tennis PPV EPG

**Timed format:** `"Last, First vs Last, First @ Apr 12 15:00 PM - ATP Monte Carlo :Tennis  03"`
- Times in Europe/Paris → converted to UTC
- Pre-event: per-block countdown titles; Live: 3h block; Post: "Signing Off" filler

**No-time format fallback:** `"Boulter, Katie vs Cristian, Jaqueline - WTA Rouen :Tennis 03"`
- Produces `"TBD: Katie Boulter vs Jaqueline Cristian · WTA Rouen"` tiled across today (Chicago midnight-to-midnight)
- SportsDB is NOT used as fallback — free tier has no player-based event lookup

Player names always reordered "Last, First" → "First Last" in titles/descriptions.
Window: 36h from now. Display timezone: America/Chicago.

### Paramount+ PPV EPG

Channel name format: `"Title @ Apr 14 2:50 PM :Paramount+  07"` (12h time, US Eastern TZ)
- Sports vs entertainment auto-detected via keyword list
- Sports: 3h block + Sports category; Entertainment: 2h + Entertainment category
- SportsDB venue lookup for soccer matches

### Sports Team EPG

Config: `config/sportsdb.yaml` (gitignored). Uses TheSportsDB free tier (`"123"`, 30 req/min).
`strTimestamp` from events API is UTC. All times displayed in America/Chicago.
Current teams: Liverpool FC (`133602`), Real Madrid (`133738`), Wrexham (`134775`).

### Logos EPG

Channel declarations only with 24h placeholder programmes. All included channels with a
`tvg_logo` set appear here. Purpose: single icon-delivery source for Dispatcharr.

## Logo Pipeline — wrangle logos

Matches included channels to the `tv-logo/tv-logos` GitHub repo by normalizing display names.
Downloads logos and resizes/pads to 512×512 transparent PNG.
`--push` updates Dispatcharr via REST API immediately.
`--dry-run` reports matches without downloading or modifying channels.json.

## What's Not Built Yet

- Direct provider Xtream Codes access for fresher PPV names (bypasses IPTVEditor 2–3h cache) — deferred
- Claude API classifier for auto-suggest on pending channels
- EPG support for UK Football PPV, UK Events PPV, US PPV groups
