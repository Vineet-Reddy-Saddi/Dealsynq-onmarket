#!/usr/bin/env python
"""Local front-end server for the on-market listings (stdlib only).

    python webapp/server.py            # serves http://127.0.0.1:8000

Endpoints:
    GET /api/stats                      -> totals, states, subtypes, price bounds
    GET /api/clusters?bbox&zoom&...     -> grid-aggregated counts for the map
    GET /api/listings?bbox=s,w,n,e&...  -> page of listings for the sidebar (+ total)
    GET /api/listing/<pk>               -> one listing incl. enriched detail
Static files (index.html, app.js, styles.css) are served from webapp/static/.

The map is driven by **server-side clustering**: 282k points cannot be shipped to the
browser, and client-side clustering still needs every point. Instead the server snaps
listings to a zoom-dependent grid and returns one aggregate per cell, so a nationwide
view is a few hundred rows instead of a quarter-million. Past a zoom threshold the same
endpoint returns individual listings so the user sees real pins with prices.

Per-property detail comes from data/details.db (built by scripts/enrich.py) and is
attached read-only, so the viewer shows galleries, brokers and highlights without the
enrichment run and the viewer fighting over one file.

Intentionally read-only and localhost-only: it just visualizes the derived DBs.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
DB = HERE / "listings.db"
STATIC = HERE / "static"
US_STATES = frozenset("""
AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO
MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
PR VI GU AS MP
""".split())

PAGE_SIZE = 40          # sidebar page size (infinite scroll appends further pages)
MAP_CAP = 900           # max individual points returned for one map view (keeps the
                        # marker layer light enough to stay responsive; sidebar + the
                        # "total in view" count still reflect the full match set)
LIST_COLS = [
    "pk", "source_site", "transaction_type", "property_subtype", "name", "address",
    "city", "state", "zip", "lat_r", "lng_r", "price_n", "price_basis", "sqft_n",
    "cap_rate", "source_url", "img", "delisted_on", "first_seen",
    "space_id", "space_label", "space_count",
]

# Whitelisted sort modes -> ORDER BY (never interpolate user input into SQL).
SORTS = {
    "new": "first_seen DESC",
    "price_desc": "(price_n IS NULL), price_n DESC",
    "price_asc": "(price_n IS NULL), price_n ASC",
    "sqft_desc": "(sqft_n IS NULL), sqft_n DESC",
    # cap_rate is TEXT in the CSV schema; cast so 9% doesn't sort above 10%
    "cap_desc": "(cap_rate IS NULL OR cap_rate=''), CAST(cap_rate AS REAL) DESC",
    # For the off-market view: most recently disappeared first.
    "delisted_desc": "delisted_on DESC",
}


DETAILS_DB = HERE.parent / "data" / "details.db"
DEMOG_DB = HERE.parent / "data" / "demographics.db"
#: Coordinates in demographics.db are rounded to this many places (see
#: scripts/build_demographics.py) -- listings on the same corner share a trade area.
DEMOG_DP = 4

#: Zoom at which the map stops aggregating and shows individual listings. Below this a
#: nationwide view would need a quarter-million markers; above it, cells hold so few
#: listings that a cluster bubble is less useful than a real pin.
PIN_ZOOM = 12
PIN_CAP = 1500          # individual pins returned per view once past PIN_ZOOM
CLUSTER_CAP = 600       # aggregate cells per view

#: Grid size in degrees per zoom level. Roughly one cell per ~60 screen px, so cluster
#: bubbles stay visually separated as the user zooms.
def _grid_for(zoom: int) -> float:
    z = max(0, min(int(zoom), 20))
    return 360.0 / (2 ** (z + 2))


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB, check_same_thread=False)
    con.row_factory = sqlite3.Row
    # Attach the enrichment output read-only. immutable=0 + query_only keeps the viewer
    # a pure reader while scripts/enrich.py may still be writing (details.db is WAL).
    if DETAILS_DB.exists():
        try:
            con.execute("ATTACH DATABASE ? AS det", (f"file:{DETAILS_DB}?mode=ro",))
        except sqlite3.Error:
            try:
                con.execute("ATTACH DATABASE ? AS det", (str(DETAILS_DB),))
            except sqlite3.Error:
                pass
    if DEMOG_DB.exists():
        try:
            con.execute("ATTACH DATABASE ? AS dem", (f"file:{DEMOG_DB}?mode=ro",))
        except sqlite3.Error:
            try:
                con.execute("ATTACH DATABASE ? AS dem", (str(DEMOG_DB),))
            except sqlite3.Error:
                pass
    return con


CON = _conn()
HAS_DETAILS = bool(CON.execute(
    "SELECT COUNT(*) FROM pragma_database_list WHERE name='det'").fetchone()[0])
HAS_DEMOG = bool(CON.execute(
    "SELECT COUNT(*) FROM pragma_database_list WHERE name='dem'").fetchone()[0])
_STATS_CACHE: dict | None = None


def _where(q: dict, *, require_geo: bool = True) -> tuple[str, list]:
    """Build a WHERE clause from query params shared by count + fetch.

    ``status`` selects the listing lifecycle slice and defaults to ``active`` so no
    caller accidentally mixes off-market rows into a normal search:
        active   – still on the market (default)
        delisted – disappeared from its source, i.e. likely went off-market
        new      – first seen within the last 7 days
        all      – no lifecycle filter
    """
    clauses = ["lat_r IS NOT NULL"] if require_geo else []
    args: list = []

    status = (_one(q, "status") or "active").lower()
    if status == "active":
        clauses.append("(delisted_on IS NULL OR delisted_on = '')")
    elif status == "delisted":
        clauses.append("(delisted_on IS NOT NULL AND delisted_on <> '')")
    elif status == "new":
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
        clauses.append("(delisted_on IS NULL OR delisted_on = '')")
        clauses.append("first_seen >= ?")
        args.append(cutoff)
    txn = _one(q, "txn")
    if txn in ("sale", "lease"):
        clauses.append("transaction_type = ?")
        args.append(txn)
    bbox = _one(q, "bbox")
    if bbox:
        try:
            s, w, n, e = (float(x) for x in bbox.split(","))
            clauses.append("lat_r BETWEEN ? AND ? AND lng_r BETWEEN ? AND ?")
            args += [s, n, w, e]
        except ValueError:
            pass
    for field, col in (("priceMin", "price_n >= ?"), ("priceMax", "price_n <= ?"),
                       ("sqftMin", "sqft_n >= ?"), ("sqftMax", "sqft_n <= ?"),
                       ("yearMin", "CAST(year_built AS INT) >= ?"),
                       ("capMin", "CAST(cap_rate AS REAL) >= ?")):
        v = _one(q, field)
        if v:
            try:
                args.append(float(v))
                clauses.append(col)
            except ValueError:
                pass
    state = _one(q, "state")
    if state:
        clauses.append("state = ?")
        args.append(state.upper())
    source = _one(q, "source")
    if source:
        clauses.append("source_site = ?")
        args.append(source)
    subtype = _one(q, "subtype")
    if subtype:
        clauses.append("property_subtype = ?")
        args.append(subtype)
    if _one(q, "hasCap"):
        clauses.append("cap_rate IS NOT NULL AND cap_rate <> ''")
    kw = _one(q, "q")
    if kw:
        like = f"%{kw.lower()}%"
        clauses.append("(LOWER(name) LIKE ? OR LOWER(address) LIKE ? OR "
                       "LOWER(city) LIKE ? OR LOWER(property_subtype) LIKE ?)")
        args += [like, like, like, like]
    return " AND ".join(clauses), args


def _one(q: dict, k: str):
    v = q.get(k)
    return v[0] if v else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/stats":
                self._json(self._stats())
            elif path == "/api/clusters":
                self._json(self._clusters(parse_qs(parsed.query)))
            elif path == "/api/listings":
                self._json(self._listings(parse_qs(parsed.query)))
            elif path.startswith("/api/listing/"):
                self._json(self._detail(path.rsplit("/", 1)[-1]))
            else:
                self._static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, code=500)

    # -- endpoints ---------------------------------------------------------
    def _stats(self) -> dict:
        # listings.db is a read-only deploy artifact (docstring above): it never
        # changes without a process restart, so the aggregate scans below (9 full
        # table/index scans) only need to run once per process, not once per request.
        global _STATS_CACHE
        if _STATS_CACHE is not None:
            return _STATS_CACHE
        c = CON
        total = c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        by_txn = {r[0]: r[1] for r in c.execute(
            "SELECT transaction_type, COUNT(*) FROM listings GROUP BY 1")}
        # Restrict to real US state/territory codes: the feeds also carry Canadian and
        # Mexican provinces plus malformed values (BCN, COL, ROO, "Virgin Islands").
        states = [r[0] for r in c.execute(
            "SELECT state FROM listings WHERE state<>'' GROUP BY 1 ORDER BY 1")
            if r[0] in US_STATES]
        sources = [{"site": r[0], "n": r[1]} for r in c.execute(
            "SELECT source_site, COUNT(*) FROM listings GROUP BY 1 ORDER BY 2 DESC")]
        subtypes = [r[0] for r in c.execute(
            "SELECT property_subtype FROM listings WHERE property_subtype<>'' "
            "GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 40")]
        # Lifecycle counts drive the off-market view's badges.
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
        active = c.execute(
            "SELECT COUNT(*) FROM listings WHERE delisted_on IS NULL OR delisted_on=''"
        ).fetchone()[0]
        delisted = c.execute(
            "SELECT COUNT(*) FROM listings WHERE delisted_on IS NOT NULL AND delisted_on<>''"
        ).fetchone()[0]
        fresh = c.execute(
            "SELECT COUNT(*) FROM listings WHERE (delisted_on IS NULL OR delisted_on='') "
            "AND first_seen >= ?", (cutoff,)).fetchone()[0]
        _STATS_CACHE = {"total": total, "byTxn": by_txn, "states": states,
                "sources": sources, "subtypes": subtypes, "mapCap": MAP_CAP,
                "lifecycle": {"active": active, "delisted": delisted, "new": fresh}}
        return _STATS_CACHE

    def _clusters(self, q: dict) -> dict:
        """Map layer: aggregate cells when zoomed out, individual pins when zoomed in."""
        where, args = _where(q)
        try:
            zoom = int(float(_one(q, "zoom") or 4))
        except ValueError:
            zoom = 4
        total = CON.execute(
            f"SELECT COUNT(*) FROM listings WHERE {where}", args).fetchone()[0]

        if zoom >= PIN_ZOOM:
            rows = CON.execute(
                f"SELECT pk, lat_r, lng_r, price_n, price_basis, transaction_type, name, "
                f"address, city, state, sqft_n, img FROM listings WHERE {where} "
                f"LIMIT {PIN_CAP}", args
            ).fetchall()
            return {"mode": "pins", "total": total, "zoom": zoom,
                    "capped": total > len(rows), "items": [dict(r) for r in rows]}

        g = _grid_for(zoom)
        rows = CON.execute(
            f"SELECT COUNT(*) AS n, AVG(lat_r) AS lat, AVG(lng_r) AS lng, "
            f"       MIN(pk) AS pk, AVG(price_n) AS avg_price "
            f"FROM listings WHERE {where} "
            f"GROUP BY CAST(lat_r/{g} AS INT), CAST(lng_r/{g} AS INT) "
            f"ORDER BY n DESC LIMIT {CLUSTER_CAP}", args
        ).fetchall()
        return {"mode": "clusters", "total": total, "zoom": zoom, "grid": g,
                "items": [dict(r) for r in rows]}

    def _listings(self, q: dict) -> dict:
        where, args = _where(q)
        total = CON.execute(
            f"SELECT COUNT(*) FROM listings WHERE {where}", args).fetchone()[0]
        cols = ", ".join(LIST_COLS)
        # Default "new" (most recently indexed) rather than price DESC — price-first made
        # every zoomed-out view lead with source data-entry junk.
        order = SORTS.get(_one(q, "sort") or "new", SORTS["new"])
        # Paged, so the sidebar renders ~40 cards instead of 900 DOM nodes at once.
        try:
            page = max(0, int(_one(q, "page") or 0))
            size = max(1, min(int(_one(q, "size") or PAGE_SIZE), 200))
        except ValueError:
            page, size = 0, PAGE_SIZE
        rows = CON.execute(
            f"SELECT {cols} FROM listings WHERE {where} "
            f"ORDER BY {order} LIMIT {size} OFFSET {page * size}", args
        ).fetchall()
        return {"total": total, "returned": len(rows), "page": page, "size": size,
                "hasMore": (page + 1) * size < total,
                "items": [dict(r) for r in rows]}

    def _detail(self, pk: str) -> dict:
        try:
            row = CON.execute(
                "SELECT * FROM listings WHERE pk = ?", (int(pk),)).fetchone()
        except ValueError:
            return {"error": "bad id"}
        if not row:
            return {"error": "not found"}
        out = dict(row)
        out["detail"] = self._enriched(out) if HAS_DETAILS else None
        out["demographics"] = self._demographics(out) if HAS_DEMOG else None
        return out

    @staticmethod
    def _demographics(listing: dict) -> dict | None:
        """1/3/5-mile trade-area profile for this listing's coordinates.

        Keyed on the rounded coordinate rather than the listing, so the ~347k listings
        (many sharing a corner, and every expanded space record sharing its parent's
        position) resolve to ~198k precomputed trade areas.
        """
        lat, lng = listing.get("lat_r"), listing.get("lng_r")
        if lat is None or lng is None:
            return None
        try:
            r = CON.execute(
                "SELECT * FROM dem.demographics WHERE lat=? AND lng=?",
                (round(lat, DEMOG_DP), round(lng, DEMOG_DP)),
            ).fetchone()
        except sqlite3.Error:
            return None
        if not r:
            return None
        d = dict(r)
        d.pop("lat", None)
        d.pop("lng", None)
        # Drop rings with no population -- an empty ring is missing data to a reader,
        # and showing "0 people" next to a real 5-mile figure reads as a bug.
        return d if any(v for k, v in d.items() if k.startswith("pop_")) else None

    @staticmethod
    def _enriched(listing: dict) -> dict | None:
        """Per-property record from details.db, if this listing has been enriched."""
        try:
            r = CON.execute(
                "SELECT * FROM det.details WHERE source_site=? AND source_listing_id=? "
                "AND status='ok'",
                (listing.get("source_site"), listing.get("source_listing_id")),
            ).fetchone()
        except sqlite3.Error:
            return None
        if not r:
            return None
        d = dict(r)
        d.pop("raw_json", None)          # large; the promoted columns are what the UI uses
        for k in ("image_urls", "comp_ids"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (ValueError, TypeError):
                    d[k] = None
        return d

    # -- helpers -----------------------------------------------------------
    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        fp = (STATIC / rel).resolve()
        if not str(fp).startswith(str(STATIC)) or not fp.is_file():
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html", ".js": "application/javascript",
            ".css": "text/css", ".json": "application/json",
            # Fonts need their real type — some browsers refuse a webfont served as
            # octet-stream, which would silently drop the brand typeface.
            ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
            ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        }.get(fp.suffix, "application/octet-stream")
        body = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(port: int = 8000):
    # Bind to all interfaces when running on a host like Render (which routes external
    # traffic to the container), but stay on localhost for local use so the dev server
    # is not exposed on the LAN by default.
    host = "0.0.0.0" if os.environ.get("RENDER") or os.environ.get("HOST_ALL") else "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), Handler)
    n = CON.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"On-Market viewer: {n:,} listings  ->  http://{host}:{port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
