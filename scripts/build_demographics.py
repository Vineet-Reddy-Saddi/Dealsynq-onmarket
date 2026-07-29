#!/usr/bin/env python
"""Precompute 1/3/5-mile trade-area demographics for every mapped listing.

Retail siting is a demographics question -- who lives within a short drive, what they
earn, how many work nearby. The marketplaces sell this as a premium add-on behind a
login, but the underlying numbers are US Census products in the public domain, so this
builds the same thing from source.

Two free, key-less Census inputs:

  * Centers of Population (2020 Decennial) -- one row per block group with a
    **population-weighted** centroid. Population-weighted matters: a geographic centroid
    of a large rural block group can sit in empty land miles from the town it contains.
  * ACS 5-year Detailed Tables (block-group level) -- households, aggregate and median
    household income, median age, employment.

Block groups (242k, ~600-3,000 people) rather than tracts (85k) because tracts are too
coarse for a 1-mile ring: a tract-centroid build of this reported **0 population within
1 mile of Millinocket, ME**, a town of ~4,100. Block groups return 4,114.

Aggregation notes, since a ring is a sum over block groups:
  * population / households / employees are plain sums -- exact.
  * household income is aggregate income divided by households, which is the true *mean*
    for the ring. Averaging block-group *medians* would be statistically meaningless, so
    the output column is named ``avg_household_income`` and the UI must not label it
    "median".
  * median age is a population-weighted mean of block-group medians -- an approximation,
    named ``approx_median_age`` to keep that visible.

A block group is counted when its population-weighted centroid falls inside the ring.
That is the standard approximation and is accurate at 3-5 miles; at 1 mile in dense
urban cores it can bin a whole block group in or out.

    python scripts/build_demographics.py            # all mapped listings
    python scripts/build_demographics.py --limit 500
"""
from __future__ import annotations

import argparse
import io
import math
import sqlite3
import sys
import time
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "_census_cache"
OUT = ROOT / "data" / "demographics.db"
LISTINGS = ROOT / "webapp" / "listings.db"

CENPOP_BG = "https://www2.census.gov/geo/docs/reference/cenpop2020/blkgrp/CenPop2020_Mean_BG.txt"
ACS = ("https://www2.census.gov/programs-surveys/acs/summary_file/2023/"
       "table-based-SF/data/5YRData/acsdt5y2023-{}.dat")

RINGS = (1.0, 3.0, 5.0)
MI_PER_DEG_LAT = 69.0
# Listings at the same corner share a trade area; rounding to ~11m collapses the 347k
# rows to far fewer distinct ring computations.
COORD_DP = 4


def _fetch(url: str, name: str) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    if path.exists():
        return path.read_bytes()
    print(f"  downloading {name} ...", flush=True)
    data = urllib.request.urlopen(url, timeout=600).read()
    path.write_bytes(data)
    return data


def load_block_groups() -> tuple[list, dict]:
    """-> ([(lat, lng, geoid)], {geoid: population}) from the 2020 centers of population."""
    raw = _fetch(CENPOP_BG, "CenPop2020_Mean_BG.txt").decode("utf-8-sig")
    pts, pop = [], {}
    for line in raw.splitlines()[1:]:
        p = line.split(",")
        if len(p) < 7:
            continue
        try:
            geoid = p[0] + p[1] + p[2] + p[3]
            pts.append((float(p[5]), float(p[6]), geoid))
            pop[geoid] = int(p[4])
        except ValueError:
            continue
    return pts, pop


def load_acs(table: str, col: int = 1) -> dict:
    """-> {block_group_geoid: value} for one ACS estimate column."""
    raw = _fetch(ACS.format(table), f"{table}.dat").decode("utf-8", "replace")
    out = {}
    for line in raw.splitlines()[1:]:
        parts = line.split("|")
        if not parts[0].startswith("1500000US"):   # block groups only
            continue
        try:
            v = float(parts[col])
        except (ValueError, IndexError):
            continue
        # ACS uses large negative sentinels (-555555555 etc.) for suppressed cells.
        if v < 0:
            continue
        out[parts[0][9:]] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    t0 = time.time()
    print("loading census inputs...")
    pts, pop2020 = load_block_groups()
    households = load_acs("b11001")
    agg_income = load_acs("b19025")
    med_age = load_acs("b01002")
    # B23025_E004 = civilian labor force, employed. Column index 7 (1-based estimate 4).
    employed = load_acs("b23025", col=7)
    print(f"  block groups: {len(pts):,} | households: {len(households):,} | "
          f"income: {len(agg_income):,} | employed: {len(employed):,}")

    # Grid index: 1-degree cells keyed (int(lat), int(lng)). A 5-mile ring touches at
    # most the 9 neighbouring cells, so lookups scan a tiny slice of the 242k points.
    grid = defaultdict(list)
    for lat, lng, geoid in pts:
        grid[(int(math.floor(lat)), int(math.floor(lng)))].append((lat, lng, geoid))

    con = sqlite3.connect(LISTINGS)
    rows = con.execute(
        "SELECT DISTINCT ROUND(lat_r, ?), ROUND(lng_r, ?) FROM listings "
        "WHERE lat_r IS NOT NULL AND lng_r IS NOT NULL", (COORD_DP, COORD_DP)).fetchall()
    con.close()
    if args.limit:
        rows = rows[: args.limit]
    print(f"distinct coordinates to compute: {len(rows):,}")

    out = sqlite3.connect(OUT)
    out.execute("DROP TABLE IF EXISTS demographics")
    cols = ", ".join(
        f"pop_{int(r)}mi INTEGER, households_{int(r)}mi INTEGER, "
        f"avg_household_income_{int(r)}mi INTEGER, employees_{int(r)}mi INTEGER, "
        f"approx_median_age_{int(r)}mi REAL" for r in RINGS)
    out.execute(f"CREATE TABLE demographics (lat REAL, lng REAL, {cols}, PRIMARY KEY (lat, lng))")

    batch, done = [], 0
    for lat, lng in rows:
        cos_lat = max(math.cos(math.radians(lat)), 0.01)
        mi_per_deg_lng = MI_PER_DEG_LAT * cos_lat
        rec = [lat, lng]
        biggest = max(RINGS)
        dlat, dlng = biggest / MI_PER_DEG_LAT, biggest / mi_per_deg_lng

        # Collect candidates once for the widest ring, then filter down per ring.
        cands = []
        for gi in range(int(math.floor(lat - dlat)), int(math.floor(lat + dlat)) + 1):
            for gj in range(int(math.floor(lng - dlng)), int(math.floor(lng + dlng)) + 1):
                for blat, blng, geoid in grid.get((gi, gj), ()):
                    d = math.hypot((blat - lat) * MI_PER_DEG_LAT,
                                   (blng - lng) * mi_per_deg_lng)
                    if d <= biggest:
                        cands.append((d, geoid))

        for radius in RINGS:
            p = h = inc = emp = 0.0
            age_num = 0.0
            for d, geoid in cands:
                if d > radius:
                    continue
                bp = pop2020.get(geoid, 0)
                p += bp
                h += households.get(geoid, 0)
                inc += agg_income.get(geoid, 0)
                emp += employed.get(geoid, 0)
                if geoid in med_age:
                    age_num += med_age[geoid] * bp
            rec += [
                int(p),
                int(h),
                int(inc / h) if h else None,      # mean, not median -- see module docstring
                int(emp),
                round(age_num / p, 1) if p else None,
            ]
        batch.append(rec)
        done += 1
        if len(batch) >= 2000:
            out.executemany(
                f"INSERT OR REPLACE INTO demographics VALUES ({','.join('?' * len(batch[0]))})", batch)
            out.commit()
            batch.clear()
            rate = done / max(time.time() - t0, 1e-9)
            print(f"  ...{done:,}/{len(rows):,} ({rate:.0f}/s, "
                  f"eta {(len(rows)-done)/max(rate,1e-9)/60:.1f}m)", flush=True)
    if batch:
        out.executemany(
            f"INSERT OR REPLACE INTO demographics VALUES ({','.join('?' * len(batch[0]))})", batch)
    out.commit()
    n = out.execute("SELECT COUNT(*) FROM demographics").fetchone()[0]
    out.close()
    print(f"done: {n:,} coordinates -> {OUT} ({(time.time()-t0)/60:.1f}m)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
