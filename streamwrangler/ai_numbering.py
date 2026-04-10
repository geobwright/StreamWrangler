"""
AI-assisted channel numbering — calls Claude API to propose a logical channel lineup.

Usage:
    plan = propose_numbering(included_channels)
    save_numbering(plan)
"""

import json

import anthropic

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

    prompt = f"""You are organizing a personal IPTV channel lineup for a UK viewer.

Here are {len(channel_list)} channels that need to be assigned channel numbers:

{json.dumps(channel_list, indent=2)}

Your task:
1. Group channels into logical blocks (e.g. Entertainment, Sports, News, Movies, Kids, International)
2. Assign channel numbers with gaps to leave room for future additions
3. Place backup channels (is_backup=true) immediately after their matching primary channel
4. Use the display_name values exactly as provided — do not modify them

Numbering rules:
- Start each block at a round number (100, 200, 300, 400, 500, etc.)
- Leave ~5 numbers between channels within a block
- Leave a larger gap (20-50 numbers) between natural sub-sections within a block
- UK viewer priorities: Entertainment first (BBC, ITV, Channel 4, etc.), then Sports, News, Movies, Kids, then foreign-language channels last
- Channels with [FR], [DE], [IT], etc. in the name are foreign language — group them in an International block at the end
- Within Sports, group by type where possible (general sports, football, cricket, etc.)

Respond with ONLY valid JSON — no markdown fences, no explanation, no trailing text:
{{"blocks": [{{"name": "Entertainment", "start": 100, "channels": [{{"uid": "...", "number": 101, "display_name": "..."}}]}}]}}"""

    client = anthropic.Anthropic()
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

    blocks = []
    for b in data["blocks"]:
        block_channels = [
            NumberedChannel(uid=ch["uid"], number=ch["number"], display_name=ch["display_name"])
            for ch in b.get("channels", [])
        ]
        blocks.append(NumberingBlock(name=b["name"], start=b["start"], channels=block_channels))

    return NumberingPlan(blocks=blocks)
