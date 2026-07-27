#!/usr/bin/env python
"""Build the front-end's SQLite index from the listing CSVs.

Reads the main store *and* the staging dir (so listings still waiting to be merged
because the CSV was locked still show up), de-dupes by (site, id, txn), and writes a
single ``webapp/listings.db`` with indexes for fast map-bounds + filter queries.

Run whenever the CSVs change:  python webapp/build_index.py
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

csv.field_size_limit(10_000_000)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = Path(__file__).resolve().parent / "listings.db"

# CSV files to ingest, in priority order (main store wins over staging on conflict).
SOURCES = [
    (DATA / "listings_for_sale.csv", "sale"),
    (DATA / "listings_for_lease.csv", "lease"),
    (DATA / "_stage" / "listings_for_sale.csv", "sale"),
    (DATA / "_stage" / "listings_for_lease.csv", "lease"),
]

# Columns copied straight through (subset of the 30-col schema the UI needs).
COLS = [
    "source_site", "source_listing_id", "source_url", "transaction_type",
    "property_subtype", "name", "address", "city", "county", "state", "zip",
    "lat", "lng", "price", "price_basis", "sqft", "lot_size_acres", "cap_rate",
    "year_built", "brokerage", "broker_name", "listed_on", "updated_on",
    "source_status", "first_seen", "last_seen",
    # Delisted listings are indexed too, not skipped: a listing disappearing from a
    # marketplace is the off-market signal this whole project exists to detect, so the
    # viewer needs to be able to show it. Queries default to active-only.
    "delisted_on",
]

# One lease listing often advertises several separate spaces, each with its own size and
# rent. Those are distinct opportunities, so the index carries one row per space rather
# than collapsing them into the parent listing.
SPACE_COLS = ["space_id", "space_label", "space_count"]

# Every source stashes its property photo under a different key inside raw_json.
# Pulling it out gives the UI real photo cards instead of grey placeholders.
# (commercialcafe / commercialsearch expose no image in their card payload.)
IMAGE_PATHS = [
    "thumbnailUrl",            # crexi
    "large_thumbnail_url",     # buildout family (svn, nai, lee, tscg, bhhs, ...)
    "photo_url",               # buildout fallback
    "primaryPhoto",            # showcase
    "image",                   # cityfeet
    "card_image",              # ripco
    "cover_image.url",         # nnnpro
    "thumbnails.0.url",        # newmark
]


def _dig(obj, path: str):
    """Resolve a dotted path; numeric segments index into lists."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _image(raw_json: str):
    if not raw_json:
        return None
    try:
        d = json.loads(raw_json)
    except ValueError:
        return None
    for p in IMAGE_PATHS:
        v = _dig(d, p)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


# Display-only price sanity. Source feeds carry data-entry errors (a Ypsilanti, MI
# listing priced at $83B; a 7,348 SF lot at $1.04B). We null those for the map/sort/
# filter so they can't dominate the view — the raw value stays untouched in the CSV.
PRICE_ABS_MAX = 2_000_000_000          # no single retail asset is > $2B
PRICE_PER_SF_MAX = 20_000              # generous ceiling (prime Manhattan ~ $5k/SF)


def _sane_price(price, sqft):
    if price is None:
        return None
    if price > PRICE_ABS_MAX:
        return None
    if sqft and sqft > 0 and price / sqft > PRICE_PER_SF_MAX:
        return None
    return price


def build() -> None:
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute(
        "CREATE TABLE listings ("
        "pk INTEGER PRIMARY KEY, " + ", ".join(f"{c} TEXT" for c in COLS) + ", "
        + ", ".join(f"{c} TEXT" for c in SPACE_COLS) + ", "
        "lat_r REAL, lng_r REAL, price_n REAL, sqft_n REAL, img TEXT)"
    )

    seen: set[tuple[str, str, str]] = set()
    total = 0
    for path, txn in SOURCES:
        if not path.exists():
            continue
        n = 0
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            batch = []
            for row in reader:
                key = (row.get("source_site", ""), row.get("source_listing_id", ""),
                       row.get("transaction_type", txn))
                if key in seen:
                    continue
                seen.add(key)
                vals = [row.get(c) or None for c in COLS]
                sqft_n = _f(row.get("sqft"))
                vals += [_f(row.get("lat")), _f(row.get("lng")),
                         _sane_price(_f(row.get("price")), sqft_n), sqft_n,
                         _image(row.get("raw_json"))]
                batch.append(vals)
                n += 1
                if len(batch) >= 5000:
                    _flush(con, batch)
                    batch = []
            if batch:
                _flush(con, batch)
        total += n
        print(f"  {path.relative_to(ROOT)}: {n:,}")

    _fix_relative_urls(con)
    _backfill_from_details(con)
    _expand_spaces(con)

    print("  building indexes...")
    con.execute("CREATE INDEX ix_geo ON listings(lat_r, lng_r)")
    con.execute("CREATE INDEX ix_txn ON listings(transaction_type)")
    con.execute("CREATE INDEX ix_state ON listings(state)")
    con.execute("CREATE INDEX ix_price ON listings(price_n)")
    # Every query filters on listing status (active by default), and the off-market view
    # sorts by when it went away.
    con.execute("CREATE INDEX ix_delisted ON listings(delisted_on)")
    con.execute("CREATE INDEX ix_first_seen ON listings(first_seen)")
    con.commit()
    con.close()
    print(f"Done: {total:,} active listings -> {DB.relative_to(ROOT)}")


DETAILS_DB = DATA / "details.db"

# A few feeds store a site-relative link, which is a dead "view original listing" button.
# The adapters now resolve these at scrape time; this repairs rows already collected.
URL_BASES = {
    "newmark": "https://www.nmrk.com",
    "ripco": "https://www.ripcony.com",
    "bhhs": "https://buildout.com",
}


def _fix_relative_urls(con) -> None:
    fixed = 0
    for site, base in URL_BASES.items():
        cur = con.execute(
            "UPDATE listings SET source_url = ? || source_url "
            "WHERE source_site = ? AND source_url LIKE '/%'", (base, site))
        fixed += cur.rowcount or 0
    if fixed:
        con.commit()
        print(f"  repaired {fixed:,} relative listing URLs")


def _backfill_from_details(con) -> None:
    """Fill gaps in the card data from the enrichment store.

    Some feeds publish no coordinates at all (CityFeet, CommercialCafe and
    CommercialSearch were 0% — 108k listings that could never appear on the map). The
    per-property detail we already fetched *does* carry a lat/lng, so recover it here
    rather than re-scraping. Cap rate and year built are filled the same way, since the
    card feeds usually omit them and the sidebar shows them.
    """
    if not DETAILS_DB.exists():
        return
    det = sqlite3.connect(f"file:{DETAILS_DB}?mode=ro", uri=True)
    try:
        rows = det.execute(
            "SELECT source_site, source_listing_id, cap_rate, year_built, raw_json "
            "FROM details WHERE status='ok'"
        )
    except sqlite3.Error:
        det.close()
        return

    geo = 0
    upd = []
    for site, lid, cap, yr, raw in rows:
        lat = lng = None
        if raw:
            try:
                loc = (json.loads(raw).get("listing") or {}).get("location") or {}
                lat, lng = loc.get("latitude"), loc.get("longitude")
            except (ValueError, AttributeError):
                pass
        if lat is None and cap is None and not yr:
            continue
        upd.append((lat, lng, cap, yr, site, lid))
    det.close()

    if not upd:
        return
    # The lookup index must exist *before* these updates. Without it each of ~200k
    # UPDATEs full-scans the 283k-row table (~55 billion comparisons): the build went
    # from 12 seconds to over an hour of CPU before this was added.
    con.execute("CREATE INDEX IF NOT EXISTS ix_srcid ON listings(source_site, source_listing_id)")
    con.executemany(
        "UPDATE listings SET "
        "  lat_r = COALESCE(lat_r, ?), lng_r = COALESCE(lng_r, ?), "
        "  cap_rate = COALESCE(NULLIF(cap_rate,''), ?), "
        "  year_built = COALESCE(NULLIF(year_built,''), ?) "
        "WHERE source_site = ? AND source_listing_id = ?",
        upd,
    )
    geo = con.execute("SELECT COUNT(*) FROM listings WHERE lat_r IS NOT NULL").fetchone()[0]
    con.commit()
    print(f"  backfilled from details.db: {len(upd):,} records -> {geo:,} now mappable")


def _flush(con, batch) -> None:
    ph = ", ".join(["?"] * (len(COLS) + 5))
    con.executemany(
        f"INSERT INTO listings ({', '.join(COLS)}, lat_r, lng_r, price_n, sqft_n, img) "
        f"VALUES ({ph})",
        batch,
    )


def _sf(v):
    """'1,000 SF' -> 1000.0 ; '1,500-2,500 SF' -> 1500.0 (low end of the range)."""
    if not v:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)", str(v).replace("+", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _rate_per_sf_yr(price: dict):
    """Pull $/SF/yr from a Showcase displayPrice block, matching how the rest of the
    project stores lease rates. Falls back through the other quoted bases."""
    if not isinstance(price, dict):
        return None
    all_ = price.get("all") or {}
    for key in ("yearlyPriceSF", "monthlyPriceSF"):
        raw = all_.get(key)
        if not raw:
            continue
        n = _sf(raw)
        if n is None:
            continue
        return n * 12 if key == "monthlyPriceSF" else n
    return None


def _expand_spaces(con) -> None:
    """Give every advertised space its own row (and therefore its own map pin).

    A lease listing frequently offers several spaces at different sizes and rents; the
    property details are shared but the deal terms are not, so collapsing them hides
    most of the actual opportunities. The parent row becomes space 1 and the remaining
    spaces are inserted as clones with only the space-specific fields changed.

    Expansion happens here, in the view index — never in the CSVs. Lifecycle tracking is
    keyed on (site, listing_id) upstream, and inventing per-space ids there would break
    new/delisted detection.
    """
    if not DETAILS_DB.exists():
        return
    det = sqlite3.connect(f"file:{DETAILS_DB}?mode=ro", uri=True)
    try:
        rows = det.execute(
            "SELECT source_site, source_listing_id, raw_json FROM details "
            "WHERE transaction_type='lease' AND status='ok'"
        ).fetchall()
    except sqlite3.Error:
        det.close()
        return
    det.close()

    plans: dict[tuple[str, str], list] = {}
    for site, lid, raw in rows:
        if not raw:
            continue
        try:
            fl = ((json.loads(raw).get("listing") or {})
                  .get("detailsForLease") or {}).get("forLeases") or []
        except (ValueError, AttributeError):
            continue
        if len(fl) < 2:
            continue           # single-space listings are already correct as-is
        spaces = []
        for sp in fl:
            if not isinstance(sp, dict):
                continue
            size = (sp.get("displaySize") or {}).get("default")
            spaces.append({
                "id": str(sp.get("id") or ""),
                "label": str(size or "").strip(),
                "sqft": _sf(size),
                "rate": _rate_per_sf_yr(sp.get("displayPrice")),
            })
        if len(spaces) >= 2:
            plans[(site, lid)] = spaces
    if not plans:
        return

    cols = [r[1] for r in con.execute("PRAGMA table_info(listings)")]
    ins_cols = [c for c in cols if c != "pk"]
    sel = ", ".join(ins_cols)
    i_site, i_lid = ins_cols.index("source_site"), ins_cols.index("source_listing_id")
    i_sid, i_slab = ins_cols.index("space_id"), ins_cols.index("space_label")
    i_cnt = ins_cols.index("space_count")
    i_price, i_sqft = ins_cols.index("price_n"), ins_cols.index("sqft_n")
    i_basis = ins_cols.index("price_basis")

    new_rows, updates, expanded = [], [], 0
    cur = con.execute(
        f"SELECT pk, {sel} FROM listings WHERE transaction_type='lease'")
    for row in cur.fetchall():
        pk, vals = row[0], list(row[1:])
        spaces = plans.get((vals[i_site], vals[i_lid]))
        if not spaces:
            continue
        expanded += 1
        n = len(spaces)
        first = spaces[0]
        updates.append((first["id"], first["label"], str(n),
                        first["rate"], first["sqft"], pk))
        for sp in spaces[1:]:
            clone = list(vals)
            clone[i_sid], clone[i_slab], clone[i_cnt] = sp["id"], sp["label"], str(n)
            if sp["rate"] is not None:
                clone[i_price] = sp["rate"]
            if sp["sqft"] is not None:
                clone[i_sqft] = sp["sqft"]
            clone[i_basis] = "lease_rate"
            new_rows.append(clone)

    if updates:
        con.executemany(
            "UPDATE listings SET space_id=?, space_label=?, space_count=?, "
            "price_n=COALESCE(?, price_n), sqft_n=COALESCE(?, sqft_n) WHERE pk=?",
            updates)
    if new_rows:
        ph = ", ".join(["?"] * len(ins_cols))
        con.executemany(
            f"INSERT INTO listings ({sel}) VALUES ({ph})", new_rows)
    con.commit()
    print(f"  spaces: expanded {expanded:,} multi-space listings -> "
          f"+{len(new_rows):,} additional space records")


if __name__ == "__main__":
    t0 = time.time()
    build()
    print(f"({time.time() - t0:.1f}s)")
