#!/usr/bin/env python
"""Run a site into a *staging* data dir, for when the main CSV is locked
(e.g. open in Excel). Merge into the main store later with merge_stage.py.

    python scripts/stage_run.py --site showcase --type sale
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import registry                       # noqa: E402
from src.common.schema import SALE, LEASE      # noqa: E402
from src.common.storage import CsvStore        # noqa: E402

STAGE_DIR = ROOT / "data" / "_stage"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--type", dest="ttype", choices=[SALE, LEASE], default=SALE)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    store = CsvStore(STAGE_DIR)
    adapter = registry.get_adapter(args.site, http=None)
    print(f"[stage] {args.site}/{args.ttype} -> {STAGE_DIR}", flush=True)
    t0 = time.time()
    listings = list(adapter.fetch(args.ttype, limit=args.limit))
    partial = args.limit is not None or not getattr(adapter, "complete", True)
    stats = store.apply_run(
        args.ttype, listings, sites_in_run={args.site}, mark_delisted=not partial
    )
    if partial:
        print("[stage] delisting skipped (limit run or incomplete crawl)")
    print(f"[stage] {args.site}/{args.ttype}: {len(listings)} scraped in "
          f"{time.time()-t0:.1f}s -> {stats}")
    print(f"[stage] file: {store.path_for(args.ttype)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
