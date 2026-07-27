"""Crexi adapter.

Two endpoints, same request/response framework:
  * SALE:  ``POST https://api.crexi.com/assets/search``       (base param ``includeUnpriced``)
  * LEASE: ``POST https://api-lease.crexi.com/assets/search`` (base param ``includeUndisclosedRate``)

Body base: ``{"types":["Retail"], <priced-flag>:true, "pageSize":50, "offset":M}``
Response: ``{"data":[...50 max...], "totalCount": N}``. The public site is
Cloudflare-challenged; both api hosts are not, so plain requests work.

Two server limits shape the crawl (identical on both hosts):
  * page size is capped at 50 regardless of requested ``pageSize``;
  * ``offset + count`` must be < 1500, so a single query can reach only ~1450 rows.

To pull every retail listing we crawl a **quadtree** over a geographic bounding box
(``latitudeMin/Max`` + ``longitudeMin/Max``): split any tile with > 1400 results into
four quadrants, recurse, and page tiles that fit. This covers priced AND unpriced (or
undisclosed-rate) listings and every territory, with no county/state list needed. Tiles
overlap by a tiny epsilon and results de-duplicate by id so boundary listings are never
dropped. A second state-code sweep catches the ~1% of coordinate-less listings.

The two sides use different item shapes (sale: ``locations[]``, ``askingPrice``,
``squareFootage``; lease: singular ``location``, ``rentableSqftMin/Max``, ``rateType``,
no numeric rate in the list payload), handled by ``_to_listing``.
"""
from __future__ import annotations

import sys
import time
from typing import Iterator, Optional

from .base import Adapter
from ..common.http_client import HttpClient
from ..common.schema import Listing, SALE, LEASE

SALE_API = "https://api.crexi.com/assets/search"
LEASE_API = "https://api-lease.crexi.com/assets/search"
SALE_URL = "https://www.crexi.com/properties/{id}/{slug}"
LEASE_URL = "https://www.crexi.com/lease/properties/{id}/{slug}"
# Per-side base body: which "include everything" flag the endpoint requires.
PRICED_FLAG = {SALE: "includeUnpriced", LEASE: "includeUndisclosedRate"}
API_FOR = {SALE: SALE_API, LEASE: LEASE_API}
RETAIL_TAG = "Retail"

PAGE = 50                 # server hard cap on returned rows
SPLIT_THRESHOLD = 1400    # tiles above this are subdivided (stay under the 1500 window)
MAX_OFFSET = 1400         # offset + PAGE must stay < 1500 (1400 + 50 = 1450)
MIN_SPAN = 0.02           # deg; stop subdividing below this (degenerate dense point)

# Root box covers the continental US + AK, HI, PR. The quadtree prunes empty ocean
# tiles quickly (they return totalCount 0), so a generous root is cheap and complete.
ROOT_BOX = {"latMin": 15.0, "latMax": 72.0, "lngMin": -180.0, "lngMax": -64.0}
EPS = 1e-6  # child-tile overlap so boundary listings are always covered

# ~0.85% of listings carry no coordinates and no geo tile can reach them. A second
# pass sweeps by state code (no geo filter) and yields only ids the geo pass missed.
# States over the 1450-reachable window may leave a few coordinate-less stragglers
# beyond that offset; those are a documented, tiny residual.
STATE_CODES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "PR", "VI", "GU",
]


class CrexiAdapter(Adapter):
    site_key = "crexi"
    supports = (SALE, LEASE)

    def __init__(self, http: Optional[HttpClient] = None):
        # Crexi's API tolerates a brisk pace; keep it polite but not slow.
        super().__init__(http or HttpClient(min_interval=0.35))
        self._headers = {
            "content-type": "application/json",
            "origin": "https://www.crexi.com",
            "referer": "https://www.crexi.com/",
        }

    # -- public ------------------------------------------------------------
    def fetch(self, transaction_type: str, *, limit: Optional[int] = None) -> Iterator[Listing]:
        if transaction_type not in (SALE, LEASE):
            raise ValueError(transaction_type)
        yield from self._fetch_geo(transaction_type, limit=limit)

    # -- crawl -------------------------------------------------------------
    def _post_raw(self, tt: str, body: dict) -> dict:
        """Single request to the endpoint for ``tt``; raises on HTTP error."""
        body = {"types": [RETAIL_TAG], PRICED_FLAG[tt]: True, "pageSize": PAGE, **body}
        resp = self.http.post(API_FOR[tt], json=body, headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    def _post_hard(self, tt: str, body: dict, tries: int = 6) -> dict:
        """Request with extra retries covering transient 400s (WAF throttling), which
        HttpClient does not retry. Raises only after exhausting all tries."""
        last: Exception | None = None
        for attempt in range(1, tries + 1):
            try:
                return self._post_raw(tt, body)
            except Exception as exc:  # noqa: BLE001 - want to retry any transient failure
                last = exc
                time.sleep(min(1.5 * attempt, 8) + 0.3 * attempt)
        raise last  # type: ignore[misc]

    def _post_soft(self, tt: str, body: dict) -> dict:
        """For data pages only: a lost page of 50 is a minor gap, so degrade to empty."""
        try:
            return self._post_hard(tt, body, tries=3)
        except Exception as exc:  # noqa: BLE001
            print(f"  [crexi][warn] data page failed ({type(exc).__name__}: {exc}); "
                  f"skipping offset={body.get('offset')}", file=sys.stderr)
            return {"data": [], "totalCount": 0}

    @staticmethod
    def _box_body(box: dict, offset: int) -> dict:
        # Clamp to valid ranges — the EPS overlap can push a boundary past ±180 lng /
        # ±90 lat, which the API rejects with a 400.
        return {
            "offset": offset,
            "latitudeMin": max(-90.0, box["latMin"]),
            "latitudeMax": min(90.0, box["latMax"]),
            "longitudeMin": max(-180.0, box["lngMin"]),
            "longitudeMax": min(180.0, box["lngMax"]),
        }

    @staticmethod
    def _quadrants(box: dict) -> list[dict]:
        latMid = (box["latMin"] + box["latMax"]) / 2
        lngMid = (box["lngMin"] + box["lngMax"]) / 2
        mk = lambda a, b, c, d: {"latMin": a, "latMax": b, "lngMin": c, "lngMax": d}
        return [
            mk(box["latMin"] - EPS, latMid + EPS, box["lngMin"] - EPS, lngMid + EPS),
            mk(box["latMin"] - EPS, latMid + EPS, lngMid - EPS, box["lngMax"] + EPS),
            mk(latMid - EPS, box["latMax"] + EPS, box["lngMin"] - EPS, lngMid + EPS),
            mk(latMid - EPS, box["latMax"] + EPS, lngMid - EPS, box["lngMax"] + EPS),
        ]

    def _fetch_geo(self, tt: str, *, limit: Optional[int]) -> Iterator[Listing]:
        seen: set[str] = set()

        def emit(items) -> Iterator[Listing]:
            for item in items:
                lid = str(item.get("id"))
                if lid in seen:
                    continue
                seen.add(lid)
                yield self._to_listing(item, tt)

        # Pass 1: geographic quadtree (everything with coordinates).
        # Each stack entry carries a retry counter: the offset=0 request doubles as the
        # split-probe, so a transient failure must re-queue the box, never drop its
        # subtree (dropping a box silently loses every listing under it).
        stack: list[tuple[dict, int]] = [(ROOT_BOX, 0)]
        while stack:
            box, attempts = stack.pop()
            try:
                first = self._post_hard(tt, self._box_body(box, 0))
            except Exception as exc:  # noqa: BLE001
                if attempts < 3:
                    stack.append((box, attempts + 1))
                else:
                    print(f"  [crexi][error] giving up on box {box} after retries: {exc}",
                          file=sys.stderr)
                continue
            total = first.get("totalCount") or 0
            if total == 0:
                continue
            span = max(box["latMax"] - box["latMin"], box["lngMax"] - box["lngMin"])
            if total > SPLIT_THRESHOLD and span > MIN_SPAN:
                stack.extend((q, 0) for q in self._quadrants(box))
                continue
            if total > MAX_OFFSET + PAGE:
                print(f"  [crexi][warn] dense tile {box} total={total} > reachable "
                      f"{MAX_OFFSET + PAGE}; some rows unreachable here", file=sys.stderr)
            data = first.get("data") or []
            offset = PAGE
            while offset < total and offset <= MAX_OFFSET:
                page = self._post_soft(tt, self._box_body(box, offset)).get("data") or []
                if not page:
                    break
                data += page
                offset += PAGE
            for listing in emit(data):
                yield listing
                if limit and len(seen) >= limit:
                    return

        # Pass 2: state-code sweep (catches coordinate-less listings the geo pass missed).
        for code in STATE_CODES:
            offset = 0
            while offset <= MAX_OFFSET:
                payload = self._post_soft(tt, {"offset": offset, "states": [code]})
                total = payload.get("totalCount") or 0
                data = payload.get("data") or []
                if not data:
                    break
                for listing in emit(data):
                    yield listing
                    if limit and len(seen) >= limit:
                        return
                offset += PAGE
                if offset >= total:
                    break

    # -- mapping -----------------------------------------------------------
    def _to_listing(self, item: dict, tt: str) -> Listing:
        return self._to_lease(item) if tt == LEASE else self._to_sale(item)

    def _to_sale(self, item: dict) -> Listing:
        loc = (item.get("locations") or [{}])[0]
        state = loc.get("state") or {}
        asset_id = item.get("id")
        return Listing(
            source_site=self.site_key,
            source_listing_id=str(asset_id),
            transaction_type=SALE,
            source_url=SALE_URL.format(id=asset_id, slug=item.get("urlSlug") or ""),
            property_type="retail",
            property_subtype=", ".join(item.get("types") or []),
            name=item.get("name") or "",
            address=loc.get("address") or "",
            city=loc.get("city") or "",
            county=loc.get("county") or "",
            state=state.get("code") or "",
            zip=loc.get("zip") or "",
            lat=loc.get("latitude"),
            lng=loc.get("longitude"),
            price=item.get("askingPrice"),
            price_basis="sale_price",
            sqft=item.get("squareFootage"),
            brokerage=item.get("brokerageName") or "",
            listed_on=item.get("activatedOn") or "",
            updated_on=item.get("updatedOn") or "",
            source_status=item.get("status") or "",
            raw=item,
        )

    def _to_lease(self, item: dict) -> Listing:
        # Lease items carry a singular ``location`` and rentable-SF range; the list
        # payload has no numeric rate (rates are often "undisclosed"), only a rateType.
        loc = item.get("location") or {}
        state = loc.get("state") or {}
        asset_id = item.get("id")
        sqft = item.get("rentableSqftMax") or item.get("rentableSqftMin")
        return Listing(
            source_site=self.site_key,
            source_listing_id=str(asset_id),
            transaction_type=LEASE,
            source_url=LEASE_URL.format(id=asset_id, slug=item.get("urlSlug") or ""),
            property_type="retail",
            property_subtype=", ".join(item.get("types") or []),
            name=item.get("name") or "",
            address=loc.get("address") or "",
            city=loc.get("city") or "",
            county=loc.get("county") or "",
            state=state.get("code") or "",
            zip=loc.get("zip") or "",
            lat=loc.get("latitude"),
            lng=loc.get("longitude"),
            price=None,  # numeric rate not exposed in the lease list payload
            price_basis=item.get("rateType") or "lease_rate",
            sqft=sqft,
            brokerage=item.get("brokerageName") or "",
            listed_on=item.get("activatedOn") or "",
            updated_on=item.get("updatedOn") or "",  # lease items usually omit this
            source_status=item.get("status") or "",
            raw=item,
        )
