"""Canonical listing schema shared by every site adapter.

One row per listing. Adapters produce ``Listing`` objects; storage writes them to
per-transaction-type CSVs and tracks lifecycle (first_seen / last_seen / delisted_on).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# Canonical transaction types
SALE = "sale"
LEASE = "lease"

# Column order for the CSV files. Keep stable — storage relies on it.
COLUMNS = [
    "source_site",        # short site key, e.g. "crexi"
    "source_listing_id",  # site's own id for the listing (string)
    "source_url",         # canonical public URL of the listing
    "transaction_type",   # "sale" | "lease"
    "property_type",      # canonical bucket, always "retail" for this project
    "property_subtype",   # site's original type label(s), preserved
    "name",
    "address",
    "city",
    "county",
    "state",
    "zip",
    "lat",
    "lng",
    "price",              # asking price (sale) or rent (lease); numeric or ""
    "price_basis",        # "sale_price" | "rent_per_sqft_yr" | "rent_monthly" | ...
    "sqft",
    "lot_size_acres",
    "cap_rate",
    "year_built",
    "tenancy",            # "single" | "multi" | ""
    "broker_name",
    "brokerage",
    "listed_on",          # source's listing/activation date (ISO)
    "updated_on",         # source's last-updated date (ISO)
    "source_status",      # raw status string from the source
    # --- lifecycle fields, computed by our storage layer, not the source ---
    "first_seen",         # first run (UTC ISO) we saw this listing
    "last_seen",          # most recent run we saw it active
    "delisted_on",        # run at which it disappeared (=> went off-market); "" if active
    "raw_json",           # compact JSON of the source record, for reprocessing
]

# Fields the adapter is responsible for. Lifecycle fields are storage-owned.
LIFECYCLE_FIELDS = {"first_seen", "last_seen", "delisted_on"}
ADAPTER_FIELDS = [c for c in COLUMNS if c not in LIFECYCLE_FIELDS]


@dataclass
class Listing:
    source_site: str
    source_listing_id: str
    transaction_type: str
    source_url: str = ""
    property_type: str = "retail"
    property_subtype: str = ""
    name: str = ""
    address: str = ""
    city: str = ""
    county: str = ""
    state: str = ""
    zip: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    price: Optional[float] = None
    price_basis: str = ""
    sqft: Optional[float] = None
    lot_size_acres: Optional[float] = None
    cap_rate: Optional[float] = None
    year_built: Optional[str] = None
    tenancy: str = ""
    broker_name: str = ""
    brokerage: str = ""
    listed_on: str = ""
    updated_on: str = ""
    source_status: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source_site, str(self.source_listing_id))

    def to_row(self) -> dict[str, Any]:
        """Flat dict for the CSV (adapter-owned fields only; no lifecycle)."""
        d = asdict(self)
        d.pop("raw", None)
        row = {c: _cell(d.get(c)) for c in ADAPTER_FIELDS}
        row["raw_json"] = json.dumps(self.raw, separators=(",", ":"), default=str)
        return row


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    return v
