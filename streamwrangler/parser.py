"""M3U parser — reads provider M3U files into Channel objects."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class RawChannel:
    """A channel entry as parsed directly from an M3U file — no normalization applied."""
    tvg_name: str = ""
    tvg_id: str = ""
    tvg_logo: str = ""
    group_title: str = ""
    cuid: str = ""
    display_name: str = ""
    url: str = ""
    raw_extinf: str = ""

    @property
    def country_prefix(self) -> str:
        """Extract country prefix e.g. 'US', 'UK', 'FR' from group_title."""
        m = re.match(r'^([A-Z0-9]+)\|', self.group_title)
        return m.group(1) if m else ""


# Regex to extract key=value attributes from #EXTINF line
_ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')


def _parse_extinf(line: str) -> dict:
    """Extract all key="value" attributes from an #EXTINF line."""
    attrs = {}
    for key, value in _ATTR_RE.findall(line):
        attrs[key.lower().replace("-", "_")] = value
    # Display name is everything after the last comma
    comma_pos = line.rfind(",")
    attrs["display_name"] = line[comma_pos + 1:].strip() if comma_pos != -1 else ""
    return attrs


def parse_m3u(source: Path | str) -> Iterator[RawChannel]:
    """
    Parse an M3U file and yield RawChannel objects.
    Accepts a file path or raw M3U text string.
    """
    if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source):
        text = Path(source).read_text(encoding="utf-8", errors="replace")
    else:
        text = source

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            attrs = _parse_extinf(line)
            url = ""
            # Next non-empty, non-comment line is the URL
            j = i + 1
            while j < len(lines):
                candidate = lines[j].strip()
                if candidate and not candidate.startswith("#"):
                    url = candidate
                    i = j
                    break
                j += 1
            yield RawChannel(
                tvg_name=attrs.get("tvg_name", ""),
                tvg_id=attrs.get("tvg_id", ""),
                tvg_logo=attrs.get("tvg_logo", ""),
                group_title=attrs.get("group_title", ""),
                cuid=attrs.get("cuid", ""),
                display_name=attrs.get("display_name", ""),
                url=url,
                raw_extinf=line,
            )
        i += 1


def parse_m3u_list(source: Path | str) -> list[RawChannel]:
    """Parse M3U and return all channels as a list."""
    return list(parse_m3u(source))
