"""SQLite store for full per-listing property detail.

The search/card feeds give roughly a dozen fields. Each site's *detail* record carries
far more (year built, stories, parking, APN, zoning, occupancy, tenancy, broker roster,
image gallery, comps) and the shape differs per site, so details live here rather than
being forced into the flat listing CSVs.

One row per (source_site, source_listing_id):
  * promoted columns for the fields we query on, and
  * ``raw_json`` holding everything the source returned, so re-processing never needs
    another network fetch.

SQLite (not CSV) because enrichment is incremental and resumable: a run has to ask
"which of these 282k listings do I still need?" cheaply, and re-running must update in
place rather than rewrite a 200 MB file.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS details (
    source_site        TEXT NOT NULL,
    source_listing_id  TEXT NOT NULL,
    transaction_type   TEXT,
    fetched_on         TEXT,
    status             TEXT,          -- ok | notfound | error
    -- promoted attributes
    description        TEXT,
    year_built         TEXT,
    year_renovated     TEXT,
    stories            REAL,
    buildings          REAL,
    parking_spaces     REAL,
    units              REAL,
    apn                TEXT,
    zoning             TEXT,
    lot_size_acres     REAL,
    sqft               REAL,
    net_rentable_sqft  REAL,
    price              REAL,
    price_per_sqft     REAL,
    cap_rate           REAL,
    occupancy          TEXT,
    tenancy            TEXT,
    ownership          TEXT,
    sale_condition     TEXT,
    lease_type         TEXT,
    lease_rate_yearly  TEXT,
    lease_rate_monthly TEXT,
    num_suites         REAL,
    broker_names       TEXT,
    brokerage          TEXT,
    num_images         INTEGER,
    image_urls         TEXT,          -- JSON array (first N)
    comp_ids           TEXT,          -- JSON array of similar-asset ids
    raw_json           TEXT,
    PRIMARY KEY (source_site, source_listing_id)
);
CREATE INDEX IF NOT EXISTS idx_details_site ON details(source_site);
CREATE INDEX IF NOT EXISTS idx_details_status ON details(status);
"""

COLUMNS = [
    "source_site", "source_listing_id", "transaction_type", "fetched_on", "status",
    "description", "year_built", "year_renovated", "stories", "buildings",
    "parking_spaces", "units", "apn", "zoning", "lot_size_acres", "sqft",
    "net_rentable_sqft", "price", "price_per_sqft", "cap_rate", "occupancy", "tenancy",
    "ownership", "sale_condition", "lease_type", "lease_rate_yearly",
    "lease_rate_monthly", "num_suites", "broker_names", "brokerage", "num_images",
    "image_urls", "comp_ids", "raw_json",
]


class DetailsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        # Enrichment is a long write-heavy loop; WAL keeps it responsive and lets a
        # reader (the viewer) query while a run is in flight.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.commit()

    #: Outcomes a resumed run must not retry: success, plus the permanent failures
    #: (listing removed, or private/login-only — we never authenticate, so retrying
    #: those burns requests forever). Plain 'error' IS retried: it means transient.
    SETTLED = ("ok", "gated", "notfound")

    def have(self, site: str) -> set[str]:
        """Ids already settled for a site (for resume)."""
        marks = ",".join("?" for _ in self.SETTLED)
        cur = self.conn.execute(
            f"SELECT source_listing_id FROM details "
            f"WHERE source_site=? AND status IN ({marks})",
            (site, *self.SETTLED),
        )
        return {r[0] for r in cur}

    def upsert_many(self, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        placeholders = ",".join("?" for _ in COLUMNS)
        sql = (
            f"INSERT OR REPLACE INTO details ({','.join(COLUMNS)}) VALUES ({placeholders})"
        )
        payload = []
        for r in rows:
            vals = []
            for c in COLUMNS:
                v = r.get(c)
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, separators=(",", ":"), default=str)
                vals.append(v)
            payload.append(vals)
        self.conn.executemany(sql, payload)
        self.conn.commit()
        return len(payload)

    def counts(self) -> dict[str, int]:
        cur = self.conn.execute(
            "SELECT source_site, status, COUNT(*) FROM details GROUP BY 1,2"
        )
        out: dict[str, int] = {}
        for site, status, n in cur:
            out[f"{site}:{status}"] = n
        return out

    def close(self) -> None:
        self.conn.close()
