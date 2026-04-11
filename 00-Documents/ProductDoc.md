# StreamWrangler — IPTV Normalization & Channel Orchestration Engine

---

## 1. Product Vision

**StreamWrangler** is a deterministic IPTV normalization engine that transforms raw provider feeds into a **stable, curated, cable-like channel lineup**.

It solves the core IPTV problem:

> Maintaining a **consistent, usable channel lineup over time** despite unstable upstream data.

---

## 2. Core Product Principle

> **StreamWrangler preserves transport, guide, and logo metadata; it owns channel presentation and structure.**

---

## 3. System Architecture

```
IPTV Provider
   ↓
IPTVEditor (Ingest + EPG + Logos)
   ↓
Raw M3U Export
   ↓
StreamWrangler
   ├── AI Classification Layer (assistive)
   ├── Deterministic Normalization Engine (authoritative)
   ├── Identity Engine (channel_uid)
   ├── Stream Selection Engine (HQ/LQ)
   └── Channel Structure Engine (groups + numbering + filtering)
   ↓
Clean M3U Output
   ↓
Dispatcharr
   ↓
Plex / Emby
```

---

## 4. Product Scope

### StreamWrangler DOES:

* Normalize channel names deterministically
* Deduplicate channels into canonical entities
* Assign stable channel identities (`channel_uid`)
* Select HQ and LQ stream variants
* Assign logical channel numbers (`tvg-chno`)
* Assign consistent groups (`group-title`)
* Limit total channels (<300 target)
* Output stable M3U for downstream systems

---

### StreamWrangler DOES NOT:

* Handle authentication or provider ingestion
* Replace IPTVEditor
* Act as a streaming proxy
* Handle playback or buffering

---

## 5. Metadata Preservation Contract

### Immutable (Pass-Through)

These fields must remain unchanged from IPTVEditor:

* Stream URL
* EPG metadata (`tvg-id`)
* Logo (`tvg-logo`)

---

### Mutable (Owned by StreamWrangler)

* Channel display name
* Group (`group-title`)
* Channel number (`tvg-chno`)
* Channel ordering
* Channel inclusion/exclusion

---

## 6. Data Model

### 6.1 Canonical Channel

```json
{
  "channel_uid": "espn",
  "display_name": "ESPN",
  "group": "Sports",
  "channel_number": 301,
  "tvg_id": "espn.us",
  "tvg_logo": "https://logo.png",
  "selected_stream": "hq"
}
```

---

### 6.2 Stream Variants

```json
{
  "channel_uid": "espn",
  "streams": {
    "hq": "http://provider/stream1",
    "lq": "http://provider/stream2"
  }
}
```

---

### 6.3 PPV/Event Channel

```json
{
  "channel_uid": "ppv_01",
  "display_name": "PPV 01 | UFC 314 Pereira vs Ankalaev",
  "group": "PPV",
  "channel_number": 951
}
```

---

## 7. Stream Selection Engine

### 7.1 Initial Heuristic (v1)

Select HQ/LQ based on:

1. Resolution keywords (FHD > HD > SD)
2. URL patterns
3. Exclude “backup” streams

---

### 7.2 Future Enhancement (v2)

Use `ffprobe` to extract:

* resolution
* bitrate
* codec
* stream validity

Example:

```bash
ffprobe -v error -select_streams v:0 \
-show_entries stream=width,height,bit_rate \
-of json http://stream-url
```

---

### 7.3 Selection Strategy

* HQ stream → primary output
* LQ stream → fallback (internal only)
* Stream switching occurs only during regeneration

---

## 8. Normalization Engine

### 8.1 Deterministic Rule

> Same input → Same output every run

---

### 8.2 Naming Rules

**Linear Channels**

```
ESPN US HD → ESPN
FOX NEWS FHD → FOX News
```

---

### 8.3 Noise Removal

Remove:

* HD / FHD / UHD
* Backup / Low
* Provider prefixes

---

### 8.4 PPV Handling

```
PPV 01 | Event Name
```

Rules:

* Stable prefix (`PPV 01`)
* Dynamic suffix (event name)

---

## 9. Identity Engine

### Problem:

* URLs change
* `tvg-id` is not unique

---

### Solution:

```
channel_uid = deterministic(canonical_name + group + type)
```

Examples:

```
espn
fox_news
ppv_01
```

---

## 10. Channel Structure Engine

### 10.1 Channel Count Target

```
Target: <300 channels
```

---

### 10.2 Logical Channel Numbering

```
100–149   Local (primary)
150–199   Local (secondary)

200–249   News
250–299   Opinion/Business

300–349   Sports (core)
350–399   Sports overflow

400–449   Entertainment
450–499   Lifestyle

500–549   Movies
550–599   Premium

600–649   Kids

700–749   International

900–949   PPV
950–999   Events
```

---

### 10.3 Selection Strategy

**Keep:**

* Core networks
* Major sports
* Major news
* Curated entertainment

**Remove:**

* Duplicates
* Low-quality streams
* Low-value channels

---

## 11. AI Classification Layer

### Purpose:

Assist initial mapping

---

### Outputs:

```json
{
  "canonical_key": "espn",
  "channel_type": "linear",
  "suggested_group": "Sports",
  "confidence": 0.97
}
```

---

### Role:

* Used during initial setup
* Results are cached
* Deterministic rules take over

---

## 12. Output Specification

### M3U Example

```xml
#EXTINF:-1 tvg-id="espn.us" tvg-logo="https://logo.png" tvg-chno="301" group-title="Sports",ESPN
http://stream-url
```

---

### Required Fields:

* `tvg-id`
* `tvg-logo`
* `tvg-chno`
* `group-title`
* display name

---

## 13. Integration

### Upstream

* IPTVEditor

  * ingestion
  * EPG
  * logos

---

### Downstream

* Dispatcharr
* Plex
* Emby

---

## 14. First-Run Workflow

1. Ingest full provider feed
2. AI classify channels
3. Apply normalization rules
4. Review top channels manually
5. Lock rules
6. Automate

---

## 15. Success Criteria

* No duplicate channels
* Stable channel numbers across refreshes
* No Dispatcharr remapping
* EPG remains intact
* Logos persist
* Channel count <300
* Plex/Emby guide performs smoothly

---

## 16. Future Enhancements

* Stream health scoring
* Automatic HQ/LQ switching
* Channel diff engine
* Rule management UI
* Multi-provider blending

---

## Final Positioning

> **StreamWrangler is the control plane for IPTV channel identity, structure, and stability.**

It converts:

```
Unstable, inconsistent IPTV feeds
```

into:

```
Deterministic, curated, cable-grade channel lineups
```

---

## Key Insight

You are not building:

* an IPTV player
* a proxy
* a streaming service

You are building:

> **The system that makes IPTV usable at scale**
