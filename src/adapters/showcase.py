"""Showcase adapter (showcase.com) — a CoStar-family marketplace.

Showcase fronts its listing pages with Akamai bot management, which rejects plain
clients outright. A Chrome TLS-impersonating client (``impersonate="chrome"``) passes,
so no headless browser is needed here.

Each search page embeds ``window.__PRELOADED_STATE__``, a JSON blob containing:
  * ``searchResults.placards.items`` — up to 30 listing records for the page
  * ``searchResults.marketingCategories`` — a directory of cities for the state with an
    exact retail ``listingCount`` per city (used to plan the crawl)
  * ``searchResults.totalResults`` — capped at 500 per search

Two facts shape the crawl:
  * **500-result cap per search**, so a nationwide query is impossible — we crawl per
    city. The state page's ``marketingCategories`` gives us the city list plus counts.
  * **Search results are padded with sponsored listings of other property types**
    (Industrial/Office show up in a retail search), so every record is filtered on
    ``propType == "Retail"``. Without this ~1/3 of rows would be wrong.

URLs:
    state: /{st}/retail-space/{for-sale|for-rent}/
    city:  /{st}/{city-slug}/retail-space/{for-sale|for-rent}/{page}/
    detail: /{street}-{city}-{st}-{zip}/{id}/
"""
from __future__ import annotations

import json
import re
import sys
from typing import Iterator, Optional

from .base import Adapter
from ..common import costar_cities as cc
from ..common.http_client import HttpClient
from ..common.next_rsc import _match_object
from ..common.schema import Listing, SALE, LEASE

BASE = "https://www.showcase.com"
STATE_URL = BASE + "/{st}/retail-space/{txn}/"
CITY_URL = BASE + "/{st}/{city}/retail-space/{txn}/{page}/"
DETAIL_URL = BASE + "/{slug}/{id}/"

TXN_PATH = {SALE: "for-sale", LEASE: "for-rent"}
PER_PAGE = 30
RESULT_CAP = 500              # server caps any single search at 500 results
MAX_PAGES = RESULT_CAP // PER_PAGE + 2
RETAIL = "Retail"

STATES = [
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo",
    "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa",
    "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
]


class ShowcaseAdapter(Adapter):
    site_key = "showcase"
    supports = (SALE, LEASE)

    def __init__(self, http: Optional[HttpClient] = None):
        # Akamai fingerprints the TLS handshake — impersonate Chrome.
        super().__init__(http or HttpClient(min_interval=0.6, impersonate="chrome"))

    # -- fetch -------------------------------------------------------------
    def fetch(self, transaction_type: str, *, limit: Optional[int] = None) -> Iterator[Listing]:
        if transaction_type not in (SALE, LEASE):
            raise ValueError(transaction_type)
        txn = TXN_PATH[transaction_type]
        seen: set[str] = set()

        for st in STATES:
            for city_slug, expected in self._cities_for(st, txn, transaction_type):
                got_in_city = 0
                for page in range(1, MAX_PAGES + 1):
                    items = self._placards(
                        CITY_URL.format(st=st, city=city_slug, txn=txn, page=page)
                    )
                    if not items:
                        break
                    new_here = 0
                    for p in items:
                        if p.get("propType") != RETAIL:
                            continue  # sponsored cross-type ad padding the results
                        lid = str(p.get("id"))
                        if lid in seen:
                            continue
                        seen.add(lid)
                        new_here += 1
                        got_in_city += 1
                        yield self._to_listing(p, transaction_type)
                        if limit and len(seen) >= limit:
                            return
                    if new_here == 0 and page > 1:
                        break
                    if expected and got_in_city >= min(expected, RESULT_CAP):
                        break

    def _state_json(self, url: str) -> Optional[dict]:
        try:
            resp = self.http.get(url)
            if resp.status_code != 200:
                return None
            text = resp.text
        except Exception as exc:  # noqa: BLE001
            print(f"  [showcase][warn] {url}: {type(exc).__name__}", file=sys.stderr)
            return None
        i = text.find("window.__PRELOADED_STATE__")
        if i < 0:
            return None
        frag = _match_object(text, text.find("{", i))
        if not frag:
            return None
        try:
            return json.loads(frag)
        except ValueError:
            return None

    def _cities_for(self, st: str, txn: str, tt: str) -> list[tuple[str, int]]:
        """City slugs (+ expected retail count) for a state. The directory itself lives
        in ``common.costar_cities`` because CityFeet plans its crawl from the same data."""
        return [(_slug(city), count) for city, count in cc.retail_cities(self.http, st, tt)]

    def _placards(self, url: str) -> list[dict]:
        state = self._state_json(url)
        if not state:
            return []
        sr = state.get("searchResults") or {}
        return (sr.get("placards") or {}).get("items") or []

    # -- mapping -----------------------------------------------------------
    def _to_listing(self, p: dict, tt: str) -> Listing:
        addr = p.get("address") or {}
        loc = p.get("location") or [None, None]   # [lng, lat]
        street = addr.get("street") or ""
        city = addr.get("city") or ""
        state = addr.get("sc") or ""
        zipc = addr.get("post") or ""
        slug = _slug(f"{street} {city} {state} {zipc}")
        sqft = p.get("buildingArea") if tt == SALE else p.get("maxSpace")
        return Listing(
            source_site=self.site_key,
            source_listing_id=str(p.get("id")),
            transaction_type=tt,
            source_url=DETAIL_URL.format(slug=slug, id=p.get("id")),
            property_type="retail",
            property_subtype=p.get("subType") or RETAIL,
            name=street,
            address=street,
            city=city,
            state=state,
            zip=zipc,
            lat=_num(loc[1] if len(loc) > 1 else None),
            lng=_num(loc[0] if loc else None),
            price=_num(p.get("salePrice")) if tt == SALE else None,
            price_basis="sale_price" if tt == SALE else "lease_rate",
            sqft=_num(sqft),
            cap_rate=_cap_pct(p.get("capRate")),
            source_status=p.get("type") or "",
            raw=p,
        )


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").lower())
    return s.strip("-")


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cap_pct(v) -> Optional[float]:
    """Showcase reports cap rate as a fraction (0.06); the rest of the project stores
    percent (6.0). Values already >1 are assumed to be percent already."""
    n = _num(v)
    if n is None or n <= 0:
        return None
    return round(n * 100, 4) if n < 1 else n
