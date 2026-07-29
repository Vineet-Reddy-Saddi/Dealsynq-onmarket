#!/usr/bin/env python
"""Fill missing building size on CommercialCafe listings from their FAQ schema block.

These sites answer "how big is it / when was it built / how much parking" in a JSON-LD
``FAQPage`` aimed at search engines, and for many listings that block is the only place
the number appears -- the rendered fact table the detail fetcher reads omits it. The
result was ``sqft`` empty on all ~31k CommercialCafe rows.

Deliberately **not** a ``enrich.py --refetch`` run. ``DetailsStore.upsert_many`` is
``INSERT OR REPLACE``, so it rewrites the whole row: a listing that 403s or times out on
the second pass would come back as a bare ``status='error'`` record and destroy the
description, images and everything else already collected for it. This only ever issues
UPDATEs for the specific columns it recovered, and only when the fetch actually
succeeded, so a failure costs nothing and a re-run is idempotent.

CommercialSearch is excluded: its pages carry no FAQ block at all (verified on sampled
listings), so there is nothing to recover there.

    python scripts/backfill_commercialedge_faq.py --limit 200   # try a slice
    python scripts/backfill_commercialedge_faq.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common.http_client import HttpClient                       # noqa: E402
from src.enrich.commercialedge_detail import CommercialEdgeDetailFetcher  # noqa: E402

DB = ROOT / "data" / "details.db"
SITE = "commercialcafe"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    ids = [r[0] for r in con.execute(
        "SELECT source_listing_id FROM details "
        "WHERE source_site=? AND status='ok' AND (sqft IS NULL OR sqft='')", (SITE,))]
    if args.limit:
        ids = ids[: args.limit]
    print(f"{SITE}: {len(ids):,} rows missing sqft")
    if not ids:
        return 0

    workers = max(1, args.workers)
    pool = [CommercialEdgeDetailFetcher(SITE, http=HttpClient(min_interval=0.5, impersonate="chrome"))
            for _ in range(workers)]
    lock = threading.Lock()
    t0 = time.time()
    n = hit = 0
    pending: list[tuple] = []

    def work(item):
        i, lid = item
        f = pool[i % workers]
        try:
            page = f._get(f._url(lid))
        except Exception:  # noqa: BLE001
            return lid, None
        if not isinstance(page, str) or page in ("notfound", "gated"):
            return lid, None
        return lid, f._faq(page)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for lid, faq in ex.map(work, enumerate(ids)):
            with lock:
                n += 1
                if faq and faq.get("sqft"):
                    hit += 1
                    pending.append((faq["sqft"], faq.get("parking"),
                                    faq.get("year_built"), SITE, lid))
                if len(pending) >= 200:
                    _flush(con, pending)
                if n % 500 == 0:
                    rate = n / max(time.time() - t0, 1e-9)
                    print(f"  ...{n:,}/{len(ids):,} recovered={hit:,} "
                          f"({rate:.1f}/s, eta {(len(ids)-n)/max(rate,1e-9)/60:.0f}m)", flush=True)
    _flush(con, pending)
    print(f"done: recovered sqft on {hit:,}/{n:,} rows in {(time.time()-t0)/60:.1f}m")
    return 0


def _flush(con, pending: list[tuple]) -> None:
    if not pending:
        return
    # COALESCE so a recovered value never overwrites one already stored.
    con.executemany(
        "UPDATE details SET sqft = COALESCE(sqft, ?), "
        "  parking_spaces = COALESCE(parking_spaces, ?), "
        "  year_built = COALESCE(NULLIF(year_built,''), ?) "
        "WHERE source_site = ? AND source_listing_id = ?", pending)
    con.commit()
    pending.clear()


if __name__ == "__main__":
    raise SystemExit(main())
