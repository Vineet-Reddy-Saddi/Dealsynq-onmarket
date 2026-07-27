"""Helpers for Next.js App Router pages that stream data as RSC flight payloads.

App Router pages don't expose a clean JSON API or ``__NEXT_DATA__``; the rendered
data is embedded in ``self.__next_f.push([...])`` script chunks. These helpers
reassemble that flight payload and pull balanced-brace JSON objects out of it.
"""
from __future__ import annotations

import json
import re
from typing import Iterator

_PUSH = re.compile(r'self\.__next_f\.push\(\[\d+,\s*("(?:[^"\\]|\\.)*")\]\)', re.S)


def flight_blob(html: str) -> str:
    """Concatenate every RSC flight string chunk in the page into one blob."""
    parts = []
    for m in _PUSH.finditer(html):
        try:
            parts.append(json.loads(m.group(1)))
        except ValueError:
            pass
    return "".join(parts)


def _match_object(blob: str, start: int) -> str | None:
    """Return the balanced ``{...}`` beginning at ``start`` (string-aware)."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(blob)):
        ch = blob[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return blob[start : i + 1]
    return None


def objects_starting_with(blob: str, first_key: str) -> Iterator[dict]:
    """Yield every JSON object in ``blob`` that begins with ``{"<first_key>":``.

    Relies on RSC preserving server key order, so a record type can be anchored by
    its first field (e.g. NNN Pro listings start with ``concept_text``).
    """
    anchor = '{"' + first_key + '":'
    idx = 0
    while True:
        pos = blob.find(anchor, idx)
        if pos == -1:
            return
        frag = _match_object(blob, pos)
        idx = pos + len(anchor)
        if not frag:
            continue
        try:
            yield json.loads(frag)
        except ValueError:
            pass


def strip_rsc_date(v):
    """RSC encodes dates as ``$D2026-07-23T...``; return the ISO part or the value."""
    if isinstance(v, str) and v.startswith("$D"):
        return v[2:]
    return v
