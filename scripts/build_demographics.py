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
import struct
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
TIGER_BG = "https://www2.census.gov/geo/tiger/TIGER2024/BG/tl_2024_{fips}_bg.zip"

#: State/territory FIPS codes carrying block groups (50 states + DC + PR).
STATE_FIPS = [f"{n:02d}" for n in (
    1, 2, 4, 5, 6, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
    26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 44, 45, 46,
    47, 48, 49, 50, 51, 53, 54, 55, 56, 72)]

SQMI_PER_SQM = 1.0 / 2_589_988.11

RINGS = (1.0, 3.0, 5.0)
MI_PER_DEG_LAT = 69.0
#: Candidate search is padded by this so a large block group can reach a ring from
#: outside it. Alaskan block groups are enormous; beyond this the disc model is
#: meaningless anyway and the tail contributes almost nothing.
MAX_BG_RADIUS_MI = 25.0
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


def load_areas() -> dict:
    """-> {block_group_geoid: land_area_sq_miles} from the TIGER block-group .dbf.

    Only the attribute table is read; the .shp geometry in the same archive is ignored,
    so no geometry library is needed. Census publishes these per state.
    """
    areas: dict[str, float] = {}
    for fips in STATE_FIPS:
        raw = _fetch(TIGER_BG.format(fips=fips), f"bg_{fips}.zip")
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name = next(n for n in z.namelist() if n.endswith(".dbf"))
            b = z.open(name).read()
        nrec, hlen, rlen = struct.unpack("<IHH", b[4:12])
        fields, off = [], 32
        while b[off] != 0x0D:
            fields.append((b[off:off + 11].split(b"\x00")[0].decode(), b[off + 16]))
            off += 32
        for i in range(nrec):
            pos = hlen + i * rlen + 1          # +1 skips the deletion flag
            rec = {}
            for nm, ln in fields:
                if nm in ("GEOID", "ALAND"):
                    rec[nm] = b[pos:pos + ln].decode("latin-1").strip()
                pos += ln
            try:
                areas[rec["GEOID"]] = int(rec["ALAND"]) * SQMI_PER_SQM
            except (KeyError, ValueError):
                continue
    return areas


def overlap_fraction(d: float, ring_r: float, bg_r: float) -> float:
    """Fraction of a block group's disc that falls inside the ring.

    Replaces all-or-nothing centroid binning, which put a block group's entire
    population inside the ring the moment its centroid crossed the line and contributed
    nothing otherwise. On a rural block group spanning tens of square miles that is
    badly wrong in both directions: it credited a 1-mile ring with people living twenty
    miles away, and it left 5.1% of trade areas reporting **zero** population within a
    mile -- which for a retail property is almost never true.

    Each block group is modelled as a disc of equivalent land area centred on its
    population-weighted centroid, and this returns the circle-circle intersection as a
    share of that disc. Still an approximation -- real block groups are not round -- but
    it degrades smoothly with distance instead of snapping between 0 and 1.
    """
    if bg_r <= 0:
        return 1.0 if d <= ring_r else 0.0
    if d >= ring_r + bg_r:
        return 0.0
    if d <= abs(ring_r - bg_r):
        # One disc sits entirely inside the other.
        return 1.0 if bg_r <= ring_r else (ring_r * ring_r) / (bg_r * bg_r)
    r2, R2, d2 = bg_r * bg_r, ring_r * ring_r, d * d
    a1 = math.acos(max(-1.0, min(1.0, (d2 + r2 - R2) / (2 * d * bg_r))))
    a2 = math.acos(max(-1.0, min(1.0, (d2 + R2 - r2) / (2 * d * ring_r))))
    lens = r2 * (a1 - math.sin(2 * a1) / 2) + R2 * (a2 - math.sin(2 * a2) / 2)
    return max(0.0, min(1.0, lens / (math.pi * r2)))


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
    print("  loading block-group land areas (TIGER)...", flush=True)
    areas = load_areas()
    # Equivalent-area disc radius per block group, precomputed once.
    radius = {g: math.sqrt(a / math.pi) for g, a in areas.items() if a > 0}
    print(f"  block groups: {len(pts):,} | households: {len(households):,} | "
          f"income: {len(agg_income):,} | employed: {len(employed):,} | "
          f"areas: {len(radius):,}")

    # Grid index over 1-degree cells. Each block group is inserted into every cell its
    # *disc* touches, not just the one holding its centroid -- so a query only has to
    # look at cells within the ring radius, instead of padding every search by the
    # largest block group in the country. Median radius is 0.4mi and 99% are under 8mi,
    # so almost every block group lands in exactly one cell; the handful of enormous
    # rural ones pay for their own reach.
    grid = defaultdict(list)
    for lat, lng, geoid in pts:
        r = min(radius.get(geoid, 0.0), MAX_BG_RADIUS_MI)
        dlat = r / MI_PER_DEG_LAT
        dlng = r / (MI_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01))
        for gi in range(int(math.floor(lat - dlat)), int(math.floor(lat + dlat)) + 1):
            for gj in range(int(math.floor(lng - dlng)), int(math.floor(lng + dlng)) + 1):
                grid[(gi, gj)].append((lat, lng, geoid, radius.get(geoid, 0.0)))

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

        # Cells are pre-expanded by each block group's own reach, so the query only has
        # to cover the ring itself.
        cands = []
        pad_lat, pad_lng = biggest / MI_PER_DEG_LAT, biggest / mi_per_deg_lng
        seen = set()
        for gi in range(int(math.floor(lat - pad_lat)), int(math.floor(lat + pad_lat)) + 1):
            for gj in range(int(math.floor(lng - pad_lng)), int(math.floor(lng + pad_lng)) + 1):
                for blat, blng, geoid, br in grid.get((gi, gj), ()):
                    if geoid in seen:      # a big disc can be indexed in several cells
                        continue
                    d = math.hypot((blat - lat) * MI_PER_DEG_LAT,
                                   (blng - lng) * mi_per_deg_lng)
                    if d <= biggest + br:
                        seen.add(geoid)
                        cands.append((d, geoid, br))

        for ring in RINGS:
            p = h = inc = emp = 0.0
            # ACS suppresses small cells, so some block groups carry population but no
            # income or age. Each derived average therefore needs a denominator built
            # from exactly the block groups that contributed to its numerator -- dividing
            # by the full population instead dragged the result toward zero and left
            # 2,190 trade areas reporting a median age of 0.0.
            inc_h = age_pop = age_num = 0.0
            for d, geoid, br in cands:
                w = overlap_fraction(d, ring, br)
                if w <= 0:
                    continue
                bp = pop2020.get(geoid, 0) * w
                bh = households.get(geoid, 0) * w
                p += bp
                h += bh
                emp += employed.get(geoid, 0) * w
                if geoid in agg_income:
                    inc += agg_income[geoid] * w
                    inc_h += bh
                if geoid in med_age and bp:
                    age_num += med_age[geoid] * bp
                    age_pop += bp
            rec += [
                int(round(p)),
                int(round(h)),
                # mean, not median -- see module docstring
                int(inc / inc_h) if inc_h and inc > 0 else None,
                int(round(emp)),
                round(age_num / age_pop, 1) if age_pop else None,
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
