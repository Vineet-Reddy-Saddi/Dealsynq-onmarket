"""CityFeet adapter (cityfeet.com) — a CoStar-family marketplace.

Like Showcase, CityFeet is fronted by Akamai and needs a Chrome TLS-impersonating
client. Unlike Showcase, its listing data comes out of the page's **JSON-LD**: a
``SearchResultsPage`` block whose ``about[].item`` entries are ``Offer`` objects:

    {"@type":"Offer","name":"3250 Glendale Blvd",
     "description":"1,105 SF For Lease in Los Angeles, CA","category":"Retail",
     "url":".../cont/listing/3250-glendale-blvd-los-angeles-ca-90039/cs29645158",
     "availableAtOrFrom":{"address":{"streetAddress","addressLocality","addressRegion"}},
     "offeredBy":[{"@type":"Person","name":...}]}

Notes that shape the crawl:
  * URLs are ``/cont/{city}-{st}/retail-{space-for-lease|properties-for-sale}`` and
    pagination is ``?pgNum=N`` (30/page). Other spellings (``?page=``, ``/2/``) silently
    return page 1 — verified — so only ``pgNum`` is used.
  * Same **500-result cap per search** as Showcase, so we crawl city by city using the
    shared CoStar city directory (see ``common.costar_cities``).
  * ``category`` is the property's **use**, and on the sale side it is usually the
    specific use with no mention of the word retail ("Fast Food", "Bank", "Storefront").
    The retail URL also still returns genuine non-retail (Warehouse / Office / Apartments
    were all observed), so rows are classified with ``common.retail_types.is_retail``
    rather than a substring test — a substring test drops ~90% of real retail here.
  * ``description`` carries the size and, for sale, the asking price
    ("2,446 SF For Sale in Center Point, AL offered at $2,583,000").
  * Listing ids (``cs12345678``) are CoStar ids, so they intentionally overlap Showcase's
    — kept as a separate source so cross-site overlap stays visible.

The JSON-LD carries no price and no coordinates; ``description`` yields the size.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Iterator, Optional

from .base import Adapter
from ..common import costar_cities as cc
from ..common import retail_types as rt
from ..common.http_client import HttpClient
from ..common.schema import Listing, SALE, LEASE

BASE = "https://www.cityfeet.com"
PATH = {SALE: "retail-properties-for-sale", LEASE: "retail-space-for-lease"}
PER_PAGE = 30
RESULT_CAP = 500
MAX_PAGES = RESULT_CAP // PER_PAGE + 2

_LD = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)
_RESULT = re.compile(r"d\['result'\]=(\{.*?\});", re.S)
_ID = re.compile(r"/cs(\d+)\s*$", re.I)
_SF = re.compile(r"([\d,]+)\s*SF", re.I)


class CityFeetAdapter(Adapter):
    site_key = "cityfeet"
    supports = (SALE, LEASE)

    def __init__(self, http: Optional[HttpClient] = None):
        super().__init__(http or HttpClient(min_interval=0.6, impersonate="chrome"))
        #: False after a failed fetch / missing city directory, so the caller skips
        #: delisting rather than reporting "not looked at" as "went off-market".
        self.complete = True
        self._unmapped: set[str] = set()

    # -- fetch -------------------------------------------------------------
    def fetch(self, transaction_type: str, *, limit: Optional[int] = None) -> Iterator[Listing]:
        if transaction_type not in (SALE, LEASE):
            raise ValueError(transaction_type)
        seen: set[str] = set()
        self._unmapped: set[str] = set()
        self.complete = True   # cleared if any city fetch fails -> caller skips delisting
        for st in cc.STATES:
            cities = cc.retail_cities(self.http, st, transaction_type)
            if not cities:
                # No directory for this state: we cannot know what we missed.
                self.complete = False
                print(f"  [cityfeet][warn] no city directory for {st.upper()}; "
                      f"marking run incomplete", file=sys.stderr)
                continue
            for city, _expected in cities:
                slug = f"{_slug(city)}-{st}"
                total_pages = None
                for page in range(1, MAX_PAGES + 1):
                    url = f"{BASE}/cont/{slug}/{PATH[transaction_type]}"
                    params = {"pgNum": page} if page > 1 else None
                    html = self._get(url, params)
                    if html is None:
                        # A failed fetch is NOT an empty city: stopping here would make
                        # every previously-seen listing in it look delisted.
                        self.complete = False
                        print(f"  [cityfeet][warn] fetch failed {slug} p{page}; "
                              f"marking run incomplete", file=sys.stderr)
                        break
                    if total_pages is None:
                        total_pages = self._total_pages(html)
                    new_here = 0
                    for offer in self._offers(html):
                        cat = offer.get("category") or ""
                        if not rt.is_retail(cat):
                            self._unmapped.update(rt.unmapped(cat))
                            continue
                        lid = self._listing_id(offer)
                        if not lid or lid in seen:
                            continue
                        seen.add(lid)
                        new_here += 1
                        yield self._to_listing(offer, lid, transaction_type)
                        if limit and len(seen) >= limit:
                            return
                    if new_here == 0 and page > 1:
                        break
                    # Stop only on CityFeet's OWN page count. The directory's per-city
                    # count comes from Showcase, which has a different inventory — using
                    # it as the stop condition truncated big cities (Houston capped at
                    # ~91 of 391) and made the remainder look delisted.
                    if total_pages and page >= total_pages:
                        break
        if self._unmapped:
            print(f"  [cityfeet] unclassified uses (review for retail): "
                  f"{sorted(self._unmapped)[:40]}", file=sys.stderr)

    def _get(self, url: str, params: Optional[dict]) -> Optional[str]:
        try:
            resp = self.http.get(url, params=params)
        except Exception:  # noqa: BLE001
            return None
        if resp.status_code != 200:
            return None
        return resp.text

    @staticmethod
    def _total_pages(html: str) -> Optional[int]:
        m = _RESULT.search(html)
        if not m:
            return None
        try:
            return int(json.loads(m.group(1)).get("totalPages") or 0) or None
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _offers(html: str) -> list[dict]:
        for block in _LD.findall(html):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            if not isinstance(data, dict) or data.get("@type") != "SearchResultsPage":
                continue
            out = []
            for entry in data.get("about") or []:
                item = entry.get("item") if isinstance(entry, dict) else None
                if isinstance(item, dict) and item.get("@type") == "Offer":
                    out.append(item)
            return out
        return []

    @staticmethod
    def _listing_id(offer: dict) -> str:
        m = _ID.search((offer.get("url") or "").rstrip("/"))
        return m.group(1) if m else ""

    # -- mapping -----------------------------------------------------------
    def _to_listing(self, offer: dict, lid: str, tt: str) -> Listing:
        place = offer.get("availableAtOrFrom") or {}
        addr = place.get("address") or {}
        desc = offer.get("description") or ""
        brokers = [
            p.get("name") for p in (offer.get("offeredBy") or [])
            if isinstance(p, dict) and p.get("name")
        ]
        return Listing(
            source_site=self.site_key,
            source_listing_id=lid,
            transaction_type=tt,
            source_url=offer.get("url") or "",
            property_type="retail",
            property_subtype=offer.get("category") or "Retail",
            name=offer.get("name") or "",
            address=addr.get("streetAddress") or offer.get("name") or "",
            city=addr.get("addressLocality") or "",
            state=addr.get("addressRegion") or "",
            zip=_zip_from_url(offer.get("url") or ""),
            price=_price(desc) if tt == SALE else None,
            price_basis="sale_price" if tt == SALE else "lease_rate",
            sqft=_sf(desc),
            broker_name=", ".join(brokers[:3]),
            source_status=desc,
            raw=offer,
        )


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _price(desc: str) -> Optional[float]:
    """Sale descriptions end "... offered at $2,583,000"; lease ones have no price."""
    m = re.search(r"offered at\s*\$?([\d,]+(?:\.\d+)?)", desc or "", re.I)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
        return val if val > 0 else None
    except ValueError:
        return None


def _sf(desc: str) -> Optional[float]:
    m = _SF.search(desc or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _zip_from_url(url: str) -> str:
    """Detail slugs end ``...-{city}-{st}-{zip}/cs{id}`` — pull the ZIP when present."""
    m = re.search(r"-([A-Za-z]{2})-(\d{5})/cs\d+", url)
    return m.group(2) if m else ""
