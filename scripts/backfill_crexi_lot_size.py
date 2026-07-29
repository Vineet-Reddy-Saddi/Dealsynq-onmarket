#!/usr/bin/env python
"""Fill missing Crexi lot size from the square-foot field already stored.

``_map_sale`` only read ``Lot Size (acres)``, but brokers fill in one unit or the other
and roughly a fifth of Crexi sale listings state the lot only as ``Lot Size (SqFt)``.
Those rows were written with ``lot_size_acres`` empty even though the page carried it.

The fix in ``crexi_detail._acres`` only helps future fetches; the attribute tables were
stored verbatim in ``raw_json``, so the already-enriched rows are recovered here without
re-hitting the API -- a refetch of 55k listings would be hours of paced requests against
a Cloudflare-fronted host, versus seconds of local SQL.

Only blanks are filled, so re-running is idempotent.

    python scripts/backfill_crexi_lot_size.py --dry-run
    python scripts/backfill_crexi_lot_size.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.enrich.crexi_detail import _acres  # noqa: E402

DB = ROOT / "data" / "details.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    updates: list[tuple] = []
    scanned = 0
    for lid, raw in con.execute(
            "SELECT source_listing_id, raw_json FROM details "
            "WHERE source_site='crexi' AND status='ok' "
            "AND (lot_size_acres IS NULL OR lot_size_acres='') AND raw_json IS NOT NULL"):
        scanned += 1
        try:
            core = (json.loads(raw) or {}).get("core") or {}
        except ValueError:
            continue
        attrs: dict = dict(core.get("details") or {})
        for s in core.get("summaryDetails") or []:
            if s.get("label") is not None:
                attrs[s["label"]] = s.get("value")
        acres = _acres(attrs.get("Lot Size (acres)"), attrs.get("Lot Size (SqFt)"))
        if acres is not None:
            updates.append((acres, lid))

    print(f"scanned {scanned:,} crexi rows with no lot size")
    print(f"recoverable: {len(updates):,}")
    if args.dry_run:
        print("(dry run -- nothing written)")
        return 0

    con.executemany(
        "UPDATE details SET lot_size_acres = ? "
        "WHERE source_site='crexi' AND source_listing_id = ? "
        "AND (lot_size_acres IS NULL OR lot_size_acres='')", updates)
    con.commit()
    print(f"committed -> {DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
