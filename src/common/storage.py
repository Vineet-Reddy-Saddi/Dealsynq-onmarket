"""CSV-backed store with lifecycle tracking (new / active / delisted).

Two files, one per transaction type:
    data/listings_for_sale.csv
    data/listings_for_lease.csv

On each run an adapter yields the *currently active* listings for one or more
``source_site``s. ``apply_run`` merges them:

* new key                -> first_seen = last_seen = now, delisted_on = ""
* seen again             -> fields refreshed, last_seen = now, delisted_on cleared
                            (a listing that reappears is treated as re-listed)
* previously active, now
  absent, same site ran  -> delisted_on = now  (it went off-market)

Delisting is scoped to the ``source_site``s actually present in the run, so running
only Crexi never marks Buildout rows as delisted.
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Iterable

from .schema import COLUMNS, Listing, SALE, LEASE

# csv field size: raw_json can be large
csv.field_size_limit(10_000_000)

_FILENAMES = {
    SALE: "listings_for_sale.csv",
    LEASE: "listings_for_lease.csv",
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class CsvStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, transaction_type: str) -> Path:
        try:
            return self.data_dir / _FILENAMES[transaction_type]
        except KeyError:
            raise ValueError(f"unknown transaction_type: {transaction_type!r}")

    def _load(self, transaction_type: str) -> dict[tuple[str, str], dict]:
        path = self.path_for(transaction_type)
        rows: dict[tuple[str, str], dict] = {}
        if not path.exists():
            return rows
        with path.open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                key = (row["source_site"], row["source_listing_id"])
                rows[key] = row
        return rows

    def _write(self, transaction_type: str, rows: Iterable[dict]) -> None:
        path = self.path_for(transaction_type)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        tmp.replace(path)  # atomic on same volume

    def apply_run(
        self,
        transaction_type: str,
        listings: Iterable[Listing],
        *,
        sites_in_run: set[str],
        mark_delisted: bool = True,
    ) -> dict[str, int]:
        """Merge one run's active listings; return a stats summary.

        ``mark_delisted=False`` merges new/updated rows but leaves absent rows alone.
        Pass it when a crawl was incomplete (a page fetch failed, a city directory was
        unavailable): absence then means "we didn't look", not "it went off-market", and
        recording it as delisted would corrupt the off-market signal.
        """
        now = _now()
        existing = self._load(transaction_type)
        seen_keys: set[tuple[str, str]] = set()
        stats = {"new": 0, "updated": 0, "relisted": 0, "delisted": 0, "active_total": 0}

        for listing in listings:
            if listing.transaction_type != transaction_type:
                continue
            key = listing.key
            seen_keys.add(key)
            row = listing.to_row()
            prev = existing.get(key)
            if prev is None:
                row["first_seen"] = now
                row["last_seen"] = now
                row["delisted_on"] = ""
                stats["new"] += 1
            else:
                row["first_seen"] = prev.get("first_seen") or now
                row["last_seen"] = now
                if prev.get("delisted_on"):
                    stats["relisted"] += 1
                else:
                    stats["updated"] += 1
                row["delisted_on"] = ""
            existing[key] = row

        # Delisting: any active row from a site that ran this batch but was not seen.
        for key, row in existing.items() if mark_delisted else []:
            site = row["source_site"]
            if site not in sites_in_run:
                continue
            if key in seen_keys:
                continue
            if not row.get("delisted_on"):
                row["delisted_on"] = now
                stats["delisted"] += 1

        stats["active_total"] = sum(
            1 for r in existing.values() if not r.get("delisted_on")
        )
        self._write(transaction_type, existing.values())
        return stats
