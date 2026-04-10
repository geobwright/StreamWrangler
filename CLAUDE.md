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

# Open curation TUI
wrangle curate

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
  parser.py       — M3U → RawChannel list
  filter.py       — groups.yaml → keeps only mapped, enabled groups
  normalizer.py   — name cleaning, quality/codec detection, variant deduplication
  store.py        — channels.json read/write, ChannelRecord dataclass
cli/
  commands.py     — Typer CLI: ingest, curate, analyze, filter-report, status
tui/
  app.py          — Textual TUI for include/exclude curation
config/
  groups.yaml           — source group → target group mapping (prefix or exact)
  normalization.yaml    — strip/replace rules for name cleaning
  config.local.yaml     — provider URL (GITIGNORED)
data/
  channels.json         — canonical channel store (decisions persist here)
```

## Pipeline

1. **Parse** — `parser.py` reads raw M3U into `RawChannel` objects
2. **Filter** — `filter.py` applies `groups.yaml`; discards unmapped/disabled groups
3. **Normalize** — `normalizer.py`:
   - Detects quality tier from name: `4K`, `FHD`, `HD`, `SD`, or `""` (unknown → shown as `Unk`)
   - Detects codec hint from name: `hevc` if name contains HEVC/H.265 (separate from quality)
   - Scores variants; keeps best per channel uid
   - Appends HD backup (`{uid}__bk`) for channels whose primary is 4K or FHD
4. **Store** — `store.py` merges normalized channels into `channels.json`, preserving curation decisions

## Group Filter — groups.yaml

Two rule types supported:

```yaml
# Prefix match — catches all quality/tier variants automatically
- source_group_prefix: "UK| SPORT"
  target_group: "UK Sports"
  enabled: true

# Exact match — use where prefix would be too broad
- source_group: "4K| ᵁᴴᴰ ³⁸⁴⁰ᴾ"
  target_group: "UK Sports"
  enabled: true
```

**Always prefer prefix rules** — they catch `ᴴᴰ`, `ʰᵉᵛᶜ`, `ᴿᴬᵂ`, `ⱽᴵᴾ`, `ᵁᴴᴰ` variants without separate entries. Exact match takes priority when both could match the same group.

## Quality Scoring (variant deduplication)

When multiple variants of the same channel exist, the highest scorer wins:

| Quality | Base score |
|---|---|
| 4K | 40 |
| FHD | 30 |
| HD | 20 |
| "" (Unk) | 15 |
| SD | 10 |

- HEVC codec hint: +3 bonus (prefer H.265 over H.264 at same resolution)
- RAW/BACKUP/LOW in name: −15 penalty

## ChannelRecord fields (channels.json)

| Field | Description |
|---|---|
| `quality` | Actual quality — updated by ffprobe on probe |
| `advertised_quality` | Quality detected from channel name — re-derived on every ingest |
| `quality_verified` | True once ffprobe has confirmed the quality |
| `codec` | Actual codec from ffprobe (e.g. `h264`, `hevc`) |
| `advertised_codec` | Codec hint from channel name (e.g. `hevc` if name had HEVC/H.265) |

## TUI Keybindings

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
| q | Quit (auto-saves) |

## TUI Qual Column Behaviour

- **Before probe:** shows quality from name (`HD`, `FHD`, `4K`, `SD`, `Unk`)
- **After probe:** always `advertised/actual✓` — e.g. `HD/FHD✓`, `HD/HD✓`, `Unk/HD✓`
- **h265 pill on channel name:** yellow = advertised but unconfirmed; green = probe confirmed; removed if probe finds non-HEVC codec

## Key Design Decisions

- **HEVC is a codec, not a quality tier.** A channel named "BBC One HEVC" gets `quality="HD"` (default), not `quality="HEVC"`. The HEVC hint is stored in `advertised_codec`.
- **Unknown quality shown as `Unk`.** Channels with no quality indicator in name get `quality=""`.
- **`advertised_quality` always re-derived on ingest.** Reflects current detection logic, not a historical value.
- **Probe-verified fields preserved across re-ingest** (`quality`, `quality_verified`, `codec`). Advertised fields always refreshed.
- **`channels.json` migration** in `load_store()` handles legacy records where `quality="HEVC"` was stored under the old scheme.
- **Prefix group matching** — always use `source_group_prefix` in groups.yaml. Exact string matching caused persistent blind spots (entire quality tiers missing).
- **Provider URL** contains credentials — stored only in `config/config.local.yaml`, gitignored.
- **Output M3U** written to `/home/geoffrey/infra/compose/dispatcharr/data/m3us/` (separate repo).

## What's Not Built Yet

- `output.py` — clean M3U generator (writes final file to Dispatcharr path)
- `wrangle output` CLI command
- Channel numbering (`wrangle number`)
- Claude API classifier for auto-suggest on pending channels
- Scheduled cron refresh (2-hour interval)
