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


_CHUNK = re.compile(r'^([0-9a-f]+):([\[{].*)$', re.M)


def chunk_table(blob: str) -> dict[str, object]:
    """Map each ``<hex-id>:<json>`` flight row to its parsed value.

    A flight payload is deduplicated: any value used more than once is hoisted into its
    own numbered row and referenced elsewhere as the string ``"$<id>"``. Without this
    table a detail record reads as ``{"lease_type": "$3e", "contacts": "$27"}`` -- the
    interesting fields are all references.
    """
    out: dict[str, object] = {}
    for m in _CHUNK.finditer(blob):
        frag = _match_object(blob, m.start(2)) if m.group(2)[0] == "{" else None
        if frag is None:
            # Arrays and anything unbalanced: fall back to a permissive line parse.
            try:
                out[m.group(1)] = json.loads(m.group(2))
            except ValueError:
                pass
            continue
        try:
            out[m.group(1)] = json.loads(frag)
        except ValueError:
            pass
    return out


def resolve_refs(value, table: dict[str, object], _depth: int = 0):
    """Recursively swap ``"$<id>"`` references for their value from ``chunk_table``.

    Depth-limited because flight graphs can be cyclic (a node referencing an ancestor),
    which would otherwise recurse forever. ``$D``-prefixed dates and the ``$`` element
    sentinel are left alone -- they are values, not references.
    """
    if _depth > 6:
        return value
    if isinstance(value, str):
        if len(value) > 1 and value[0] == "$" and value[1] not in "D$":
            ref = table.get(value[1:])
            return resolve_refs(ref, table, _depth + 1) if ref is not None else None
        return strip_rsc_date(value)
    if isinstance(value, list):
        return [resolve_refs(v, table, _depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: resolve_refs(v, table, _depth + 1) for k, v in value.items()}
    return value
