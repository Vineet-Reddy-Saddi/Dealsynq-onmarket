#!/usr/bin/env python
"""On-Market Scraper — orchestrator CLI.

Examples:
    python run.py --site crexi --type sale                 # full Crexi sale run
    python run.py --site crexi --type sale --limit 50      # smoke test (50 records)
    python run.py --all --type sale                        # every registered site
    python run.py --list                                   # list registered sites

Writes/updates data/listings_for_sale.csv (or _for_lease.csv) and prints a
new / updated / delisted summary for the run.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# allow "python run.py" from repo root
sys.path.insert(0, str(Path(__file__).parent))

from src.common.schema import SALE, LEASE           # noqa: E402
from src.common.storage import CsvStore             # noqa: E402
from src import registry                            # noqa: E402

DATA_DIR = Path(__file__).parent / "data"


def run_site(site: str, transaction_type: str, limit: int | None, store: CsvStore) -> None:
    # http=None lets each adapter pick its own pacing (e.g. Crexi runs briskly).
    adapter = registry.get_adapter(site, http=None)
    if transaction_type not in adapter.supports:
        print(f"  [skip] {site}: does not support '{transaction_type}' "
              f"(supports: {', '.join(adapter.supports) or 'none yet'})")
        return
    print(f"  [{site}/{transaction_type}] fetching...", flush=True)
    t0 = time.time()
    listings = list(adapter.fetch(transaction_type, limit=limit))
    dt = time.time() - t0
    # An adapter may report an incomplete crawl (failed fetch / missing directory). Then
    # a listing's absence means "not looked at", not "off-market", so skip delisting —
    # otherwise the off-market signal fills with false positives.
    complete = getattr(adapter, "complete", True)
    partial = limit is not None or not complete
    stats = store.apply_run(
        transaction_type, listings, sites_in_run={site}, mark_delisted=not partial
    )
    note = ""
    if partial:
        note = " [delisting skipped: " + ("limit run" if limit else "incomplete crawl") + "]"
    print(
        f"  [{site}/{transaction_type}] {len(listings)} scraped in {dt:.1f}s -> "
        f"new={stats['new']} updated={stats['updated']} relisted={stats['relisted']} "
        f"delisted={stats['delisted']} active_total={stats['active_total']}{note}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="On-Market Scraper")
    ap.add_argument("--site", help="site key (see --list)")
    ap.add_argument("--all", action="store_true", help="run all registered sites")
    ap.add_argument("--type", dest="ttype", choices=[SALE, LEASE], default=SALE)
    ap.add_argument("--limit", type=int, default=None, help="cap records (smoke test)")
    ap.add_argument("--list", action="store_true", help="list registered sites and exit")
    args = ap.parse_args()

    if args.list:
        print("Registered sites:", ", ".join(registry.available_sites()))
        return 0

    store = CsvStore(DATA_DIR)
    sites = registry.available_sites() if args.all else ([args.site] if args.site else [])
    if not sites:
        ap.error("specify --site <key>, --all, or --list")

    print(f"Run: type={args.ttype} sites={sites} limit={args.limit}")
    for site in sites:
        try:
            run_site(site, args.ttype, args.limit, store)
        except NotImplementedError as exc:
            print(f"  [skip] {site}: {exc}")
        except Exception as exc:  # keep going across sites
            print(f"  [ERROR] {site}: {type(exc).__name__}: {exc}")
    print(f"CSV: {store.path_for(args.ttype)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
