#!/usr/bin/env python
"""Re-derive promoted detail columns from facts already stored in details.db.

``BuildoutDetailFetcher`` matched fact-table labels literally, but roughly half the
Buildout brokerages render them with a trailing colon ("Building Size:"), so those
rows were written with sqft / year_built / price / lot_size_acres left empty even
though the page carried them.

The fix in ``_fact_label`` only helps future fetches. This script recovers the
already-enriched rows without re-hitting the network: the raw fact table was stored
verbatim in ``raw_json.facts``, so the promotion can simply be re-run over it. That
matters because a refetch of the affected sites is ~3 hours of paced requests against
an API that rate-limits, versus a few seconds of local SQL here.

    python scripts/backfill_buildout_facts.py --dry-run
    python scripts/backfill_buildout_facts.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.enrich.buildout_detail import FACT_KEYS, _fact_label, _num  # noqa: E402

DB = ROOT / "data" / "details.db"
SITES = ("lee-associates", "naiglobal", "bhhs", "tscg",
         "midamerica", "franklinst", "svn", "fortis", "newmark")


def promote(facts: dict) -> dict:
    """Same mapping fetch() applies, over an already-stored fact table."""
    out: dict = {}
    for raw_label, value in facts.items():
        mapped = FACT_KEYS.get(_fact_label(raw_label))
        if not mapped or out.get(mapped[0]):
            continue
        key, kind = mapped
        if kind == "num":
            n = _num(value)
            if n is None:
                continue
            if key == "lot_size_acres" and not (0.001 <= n <= 10000):
                continue
            out[key] = n
        else:
            out[key] = value
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cols = ("sqft", "year_built", "lot_size_acres", "price", "cap_rate",
            "zoning", "apn", "stories", "parking_spaces", "units",
            "occupancy", "tenancy", "year_renovated")

    updates: list[tuple] = []
    gained = {c: 0 for c in cols}
    placeholders = ",".join("?" * len(SITES))
    rows = con.execute(
        f"SELECT source_site, source_listing_id, raw_json, {', '.join(cols)} "
        f"FROM details WHERE source_site IN ({placeholders}) "
        f"AND status='ok' AND raw_json IS NOT NULL", SITES)

    for row in rows:
        try:
            facts = (json.loads(row["raw_json"]) or {}).get("facts") or {}
        except ValueError:
            continue
        if not facts:
            continue
        promoted = promote(facts)
        # Only fill blanks -- never overwrite a value already stored, so re-running is
        # idempotent and a correct existing value is never clobbered by a stale table.
        fill = {k: v for k, v in promoted.items()
                if k in cols and not row[k] and v not in (None, "")}
        if not fill:
            continue
        for k in fill:
            gained[k] += 1
        sets = ", ".join(f"{k}=?" for k in fill)
        updates.append((sets, list(fill.values()) +
                        [row["source_site"], row["source_listing_id"]]))

    print(f"rows to update: {len(updates):,}")
    for c in cols:
        if gained[c]:
            print(f"  +{gained[c]:>6,}  {c}")

    if args.dry_run:
        print("(dry run -- nothing written)")
        return 0

    for sets, params in updates:
        con.execute(
            f"UPDATE details SET {sets} WHERE source_site=? AND source_listing_id=?",
            params)
    con.commit()
    print(f"committed -> {DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
