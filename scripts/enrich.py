#!/usr/bin/env python
"""Fetch full per-property detail for listings already in the CSVs.

The search feeds give ~12 fields per listing; the detail endpoints give year built,
stories, parking, APN, zoning, occupancy, tenancy, the broker roster, the full image
gallery, comps, and — for lease — the asking rate, which the lease search feed omits
entirely.

    python scripts/enrich.py --site crexi --type sale                # all, resumable
    python scripts/enrich.py --site crexi --type lease --limit 500   # a batch
    python scripts/enrich.py --site crexi --type sale --sample 5 --verbose

Resumable: ids already stored with status 'ok' are skipped, so a long run can be
stopped and restarted freely. Results go to data/details.db (SQLite), keyed by
(source_site, source_listing_id) — the listing CSVs are never rewritten.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

csv.field_size_limit(10_000_000)

from src.common.details_store import DetailsStore      # noqa: E402
from src.common.schema import SALE, LEASE              # noqa: E402
from src.enrich.commercialedge_detail import CommercialEdgeDetailFetcher  # noqa: E402
from src.enrich.costar_detail import CoStarDetailFetcher                  # noqa: E402
from src.enrich.crexi_detail import CrexiDetailFetcher                    # noqa: E402

DATA = ROOT / "data"
DB = DATA / "details.db"
# CityFeet enriches through Showcase (shared CoStar ids), but rows stay attributed to
# the site the listing came from, so per-source coverage remains honest.
FETCHERS = {
    "crexi": CrexiDetailFetcher,
    "showcase": lambda: CoStarDetailFetcher("showcase"),
    "cityfeet": lambda: CoStarDetailFetcher("cityfeet"),
    "commercialcafe": lambda: CommercialEdgeDetailFetcher("commercialcafe"),
    "commercialsearch": lambda: CommercialEdgeDetailFetcher("commercialsearch"),
}
FILES = {SALE: "listings_for_sale.csv", LEASE: "listings_for_lease.csv"}


def load_ids(site: str, ttype: str) -> list[str]:
    """Active listing ids for a site, from the main store and the staging store."""
    ids: list[str] = []
    seen: set[str] = set()
    for base in (DATA, DATA / "_stage"):
        path = base / FILES[ttype]
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["source_site"] != site or r["delisted_on"]:
                    continue
                lid = r["source_listing_id"]
                if lid not in seen:
                    seen.add(lid)
                    ids.append(lid)
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-property detail enrichment")
    ap.add_argument("--site", default="crexi", choices=sorted(FETCHERS))
    ap.add_argument("--type", dest="ttype", choices=[SALE, LEASE], default=SALE)
    ap.add_argument("--limit", type=int, default=None, help="max listings this run")
    ap.add_argument("--sample", type=int, default=None,
                    help="random N (validation runs); implies --verbose")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--refetch", action="store_true", help="ignore resume, refetch")
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel fetchers (each gets its own paced session)")
    args = ap.parse_args()

    store = DetailsStore(DB)

    ids = load_ids(args.site, args.ttype)
    if not ids:
        print(f"no active {args.site}/{args.ttype} listings found")
        return 1
    done = set() if args.refetch else store.have(args.site)
    todo = [i for i in ids if i not in done]
    if args.sample:
        random.shuffle(todo)
        todo = todo[: args.sample]
        args.verbose = True
    elif args.limit:
        todo = todo[: args.limit]

    workers = 1 if args.sample else max(1, args.workers)
    print(f"{args.site}/{args.ttype}: {len(ids):,} active, {len(done):,} already "
          f"enriched, fetching {len(todo):,} with {workers} worker(s)")

    # One fetcher (and therefore one paced HTTP session) per worker: a requests/curl_cffi
    # session is not thread-safe, and per-worker pacing keeps the aggregate rate honest.
    pool = [FETCHERS[args.site]() for _ in range(workers)]
    t0 = time.time()
    batch, ok, bad, n = [], 0, 0, 0
    lock = threading.Lock()

    def work(item):
        idx, lid = item
        return pool[idx % workers].fetch(lid, args.ttype)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for row in ex.map(work, enumerate(todo)):
            with lock:
                n += 1
                batch.append(row)
                if row.get("status") == "ok":
                    ok += 1
                else:
                    bad += 1
                if args.verbose:
                    print(f"  [{n}/{len(todo)}] {row['source_listing_id']} {row.get('status')} "
                          f"yr={row.get('year_built') or '-'} sqft={row.get('sqft') or '-'} "
                          f"price={row.get('price') or '-'} imgs={row.get('num_images') or 0} "
                          f"brokers={(row.get('broker_names') or '-')[:32]}")
                if len(batch) >= 50:
                    store.upsert_many(batch)
                    batch.clear()
                    rate = n / max(time.time() - t0, 1e-9)
                    print(f"  ...{n:,}/{len(todo):,} ok={ok:,} err={bad:,} "
                          f"({rate:.1f}/s, eta {(len(todo)-n)/max(rate,1e-9)/3600:.1f}h)",
                          flush=True)
    store.upsert_many(batch)
    dt = time.time() - t0
    print(f"done: {ok:,} ok, {bad:,} failed in {dt/60:.1f}m -> {DB}")
    print("store:", store.counts())
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
