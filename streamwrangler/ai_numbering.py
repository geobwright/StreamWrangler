"""
AI-assisted channel numbering — calls Claude API to propose a logical channel lineup.

Usage:
    plan = propose_numbering(included_channels)
    save_numbering(plan)
"""

import json
import os
from pathlib import Path

import anthropic
import yaml

from .store import ChannelRecord
from .numbering import NumberingPlan, NumberingBlock, NumberedChannel, build_output_display_name


def propose_numbering(channels: list[ChannelRecord]) -> NumberingPlan:
    """
    Call Claude API to propose a numbered channel lineup.
    Only included channels are sent. Backup channels (uid ending __bk) are flagged.

    The AI proposes logical blocks, assigns numbers with gaps, and uses the
    pre-built display names (base + quality + [LANG]) verbatim.
    """
    included = [ch for ch in channels if ch.status == "included"]
    if not included:
        return NumberingPlan()

    channel_list = []
    for ch in included:
        channel_list.append({
            "uid": ch.channel_uid,
            "display_name": build_output_display_name(ch),
            "quality": ch.quality or ch.advertised_quality or "Unk",
            "target_group": ch.target_group,
            "is_backup": ch.channel_uid.endswith("__bk"),
        })

    prompt = f"""You are organizing a personal IPTV channel lineup for a US viewer based in Kansas City, MO.
Primary interests: US broadcast/news, UK sports (EPL, Champions League), tennis, soccer, cricket, rugby, F1.
Secondary: UK general, France general and sports.

Here are {len(channel_list)} channels that need to be assigned channel numbers:

{json.dumps(channel_list, indent=2)}

Your task:
1. Assign channels to the blocks below — use EXACTLY these block names, start numbers, and order
2. Assign channel numbers with gaps (~5 numbers between channels) to leave room for future additions
3. Place backup channels (is_backup=true) immediately after their matching primary channel, numbered consecutively
4. Use the display_name values exactly as provided — do not modify them
5. Every channel must appear in exactly one block

Required blocks in this exact order:
  US Broadcast       start: 100   (US local/network broadcast: ABC, CBS, NBC, FOX, CW + KC market)
  US News            start: 150   (CNN, Fox News, MSNBC, Bloomberg, etc.)
  US Sports          start: 200   (ESPN, FS1, NFL, MLB, NHL, Golf, Tennis Channel, etc.)
  UK Sports          start: 250   (Sky Sports, TNT Sports, Eurosport, BeIN English, Viaplay, etc.)
  US Entertainment   start: 300   (Hallmark, Great American Family, A&E, AMC, FX, History, Discovery, etc.)
  France             start: 350   (all French channels — TF1, France 2, Canal+, BeIN FR, L'Equipe, etc.)
  UK General         start: 400   (BBC, ITV, Channel 4, Channel 5, Sky Atlantic, Sky News, etc.)
  US PPV             start: 900   (US PPV events — NFL, MLB, NHL, UFC, Golf, MLS, DAZN US)
  UK Football PPV    start: 930   (EPL team feeds, Championship, La Liga PPV, UEFA PPV, live football)
  UK Events PPV      start: 960   (TNT Sport Events, Formula 1, DAZN UK, Rugby PPV, UFC UK)
  Paramount+ PPV     start: 1100  (Paramount+ originals and on-demand content)
  Tennis PPV         start: 990   (all tennis feeds — ATP, WTA, Grand Slams, court-by-court)

Rules:
- Every channel must go into one of the blocks above — no extra blocks
- Channels with target_group matching a block should generally go in that block
- US PPV, UK Football PPV, UK Events PPV, Tennis PPV channels have live match names that change daily — number them sequentially with no gaps (901, 902, 903...)
- Within non-PPV blocks, leave ~5 numbers between channels

Respond with ONLY valid JSON — no markdown fences, no explanation, no trailing text:
{{"blocks": [{{"name": "US Broadcast", "start": 100, "channels": [{{"uid": "...", "number": 101, "display_name": "..."}}]}}]}}"""

    cfg = yaml.safe_load(Path("config/config.local.yaml").read_text())
    # Temporarily unset ANTHROPIC_API_KEY to avoid SDK auth conflict warning when
    # both a claude.ai session token and the env var are present simultaneously.
    _env_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    finally:
        if _env_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = _env_key
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown fences if model included them despite instructions
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    data = json.loads(raw)

    # Build fallback lookup — if AI omits display_name in response, use what we sent it
    display_name_lookup = {ch["uid"]: ch["display_name"] for ch in channel_list}

    blocks = []
    for b in data["blocks"]:
        block_channels = [
            NumberedChannel(
                uid=ch["uid"],
                number=ch["number"],
                display_name=ch.get("display_name") or display_name_lookup.get(ch["uid"], ch["uid"]),
            )
            for ch in b.get("channels", [])
        ]
        blocks.append(NumberingBlock(name=b["name"], start=b["start"], channels=block_channels))

    return NumberingPlan(blocks=blocks)
