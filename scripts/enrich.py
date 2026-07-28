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
from src.enrich.buildout_detail import BuildoutDetailFetcher              # noqa: E402
from src.enrich.commercialedge_detail import CommercialEdgeDetailFetcher  # noqa: E402
from src.enrich.costar_detail import CoStarDetailFetcher                  # noqa: E402
from src.enrich.crexi_detail import CrexiDetailFetcher                    # noqa: E402

DATA = ROOT / "data"
DB = DATA / "details.db"
CONFIG = ROOT / "config"
# CityFeet enriches through Showcase (shared CoStar ids), but rows stay attributed to
# the site the listing came from, so per-source coverage remains honest.
FETCHERS = {
    "crexi": CrexiDetailFetcher,
    "showcase": lambda: CoStarDetailFetcher("showcase"),
    "cityfeet": lambda: CoStarDetailFetcher("cityfeet"),
    "commercialcafe": lambda: CommercialEdgeDetailFetcher("commercialcafe"),
    "commercialsearch": lambda: CommercialEdgeDetailFetcher("commercialsearch"),
}


# Fetchers whose fetch() needs source_url to resolve a detail page (see
# BuildoutDetailFetcher docstring) rather than the bare listing id.
NEEDS_URL_HINT: set[str] = set()


def _load_buildout_fetchers() -> None:
    cfg = CONFIG / "buildout_sites.json"
    if not cfg.exists():
        return
    data = json.loads(cfg.read_text(encoding="utf-8"))
    for site in data.get("sites", []):
        FETCHERS[site["site_key"]] = (
            lambda s=site: BuildoutDetailFetcher(
                site_key=s["site_key"], hash=s["hash"], domain=s["domain"])
        )
        NEEDS_URL_HINT.add(site["site_key"])


_load_buildout_fetchers()

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


def load_url_map(site: str, ttype: str) -> dict[str, str]:
    """source_listing_id -> source_url, for fetchers that need the original listing
    URL to resolve a detail page (BuildoutDetailFetcher)."""
    out: dict[str, str] = {}
    for base in (DATA, DATA / "_stage"):
        path = base / FILES[ttype]
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["source_site"] != site or r["delisted_on"]:
                    continue
                lid = r["source_listing_id"]
                if lid not in out:
                    out[lid] = r["source_url"]
    return out


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
        # Not an error -- a brokerage can legitimately have zero active listings of one
        # transaction type (e.g. sale-only). Must stay exit 0: the wrapper script now
        # runs under `set -e` for the circuit-breaker fix, and a nonzero exit here
        # would abort the whole multi-site run over an empty, expected result set.
        print(f"no active {args.site}/{args.ttype} listings found")
        return 0
    done = set() if args.refetch else store.have(args.site)
    todo = [i for i in ids if i not in done]
    if args.sample:
        random.shuffle(todo)
        todo = todo[: args.sample]
        args.verbose = True
    elif args.limit:
        todo = todo[: args.limit]

    url_map = load_url_map(args.site, args.ttype) if args.site in NEEDS_URL_HINT else {}

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
        if url_map:
            return pool[idx % workers].fetch(lid, args.ttype, url_hint=url_map.get(lid))
        return pool[idx % workers].fetch(lid, args.ttype)

    # Circuit breaker: a source that starts soft-rate-limiting us returns clean "error"
    # rows for every request, not a crash -- so nothing here raises on its own, and
    # Executor.map submits every task up front, so merely breaking out of the results
    # loop would NOT stop already-queued work (they'd keep running until the `with`
    # block's shutdown(wait=True) finished them anyway). 15 straight errors is treated
    # as a block, not bad luck: cancel whatever's still queued and exit 1 immediately
    # rather than burning the rest of the batch against a wall.
    CONSECUTIVE_ERROR_LIMIT = 15
    consecutive_errors = 0
    blocked = False

    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        for row in ex.map(work, enumerate(todo)):
            with lock:
                n += 1
                batch.append(row)
                if row.get("status") == "ok":
                    ok += 1
                    consecutive_errors = 0
                else:
                    bad += 1
                    if row.get("status") == "error":
                        consecutive_errors += 1
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
                if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                    blocked = True
                    print(f"  !! {consecutive_errors} consecutive errors -- looks like a "
                          f"block, not bad luck. Stopping this run early.", flush=True)
                    break
    finally:
        ex.shutdown(wait=not blocked, cancel_futures=blocked)
    store.upsert_many(batch)
    if blocked:
        store.close()
        return 1
    dt = time.time() - t0
    print(f"done: {ok:,} ok, {bad:,} failed in {dt/60:.1f}m -> {DB}")
    print("store:", store.counts())
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
