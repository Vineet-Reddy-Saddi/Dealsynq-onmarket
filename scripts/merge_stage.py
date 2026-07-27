#!/usr/bin/env python
"""Merge staged rows (data/_stage/) into the main store.

Used when the main CSV was locked during a run (e.g. open in Excel), so the crawl
was written to a staging dir instead. Replays the staged rows through the normal
storage layer, so lifecycle handling (first_seen / last_seen / delisted_on) and
per-site delisting scope behave exactly as they would have in a live run.

    python scripts/merge_stage.py            # merge everything staged
    python scripts/merge_stage.py --type sale
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

csv.field_size_limit(10_000_000)

from src.common.schema import SALE, LEASE, Listing   # noqa: E402
from src.common.storage import CsvStore              # noqa: E402

DATA = ROOT / "data"
STAGE = DATA / "_stage"
_FILES = {SALE: "listings_for_sale.csv", LEASE: "listings_for_lease.csv"}

_NUM = {"lat", "lng", "price", "sqft", "lot_size_acres", "cap_rate"}


def _row_to_listing(r: dict) -> Listing:
    kw = {}
    for k, v in r.items():
        if k in ("first_seen", "last_seen", "delisted_on", "raw_json"):
            continue
        kw[k] = (float(v) if v not in ("", None) else None) if k in _NUM else v
    try:
        kw["raw"] = json.loads(r.get("raw_json") or "{}")
    except ValueError:
        kw["raw"] = {}
    return Listing(**kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", dest="ttype", choices=[SALE, LEASE], default=None)
    args = ap.parse_args()

    types = [args.ttype] if args.ttype else [SALE, LEASE]
    store = CsvStore(DATA)
    for tt in types:
        src = STAGE / _FILES[tt]
        if not src.exists():
            print(f"[merge] no staged {tt} file, skipping")
            continue
        rows = list(csv.DictReader(src.open(encoding="utf-8")))
        if not rows:
            print(f"[merge] staged {tt} file empty, skipping")
            continue
        listings = [_row_to_listing(r) for r in rows]
        sites = {l.source_site for l in listings}
        stats = store.apply_run(tt, listings, sites_in_run=sites)
        print(f"[merge] {tt}: {len(listings)} staged rows from {sorted(sites)} -> {stats}")
        src.rename(src.with_suffix(".csv.merged"))
        print(f"[merge] staged file archived as {src.name}.merged")
    print(f"[merge] main store: {DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
