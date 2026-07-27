"""CommercialEdge adapter — Yardi's CRE marketplace platform.

One implementation for every Yardi/CommercialEdge consumer site (CommercialCafe,
CommercialSearch, …). They render identical server-side HTML: a search page filtered
by query params, listing cards with address/price/type/size, and ``?Page=N`` pagination.

Search URL:
    https://{domain}/commercial-real-estate/us/?PropertyTypes=3&ListingType={Sale|Lease}&Page={N}
``PropertyTypes=3`` == Retail (confirmed: the page's own h1 becomes "Retail Properties
for Sale"). Cloudflare fronts the sites but does not block plain requests to these
listing pages (a challenge script is embedded in otherwise-real HTML), so no headless
browser is needed — we just pace politely.

Each card (delimited by ``building-name``):
  <h4 class="building-name ..."><a href="{detail}">{name}</a></h4>
  <span title="{full addr}" class="building-address">{addr}, {city}, {ST}</span>
  <div class="building key-value"><b>Property Type</b><ul><li title="Retail">...
  <div class="price key-value"><b>For Sale</b><ul><li title="$1,389,330">...
  <div class="spaces key-value"><b>Built in 2023 / Property size 10,500 SF</b>
"""
from __future__ import annotations

import html as htmllib
import re
from typing import Iterator, Optional

from .base import Adapter
from ..common.http_client import HttpClient
from ..common.schema import Listing, SALE, LEASE

SEARCH = "https://{domain}/commercial-real-estate/us/"
RETAIL_TYPE_ID = 3
MAX_PAGES = 400  # safety cap

_CARD = re.compile(r'class="building-name.*?(?=class="building-name|class="pagination|</footer>)', re.S)
_HREF = re.compile(r'href="(https://[^"]*?/commercial-property/us/[^"]+?)"')
_NAME = re.compile(r'/commercial-property/us/[^"]+?"[^>]*>([^<]+)</a>')
_ADDR = re.compile(r'class="building-address[^"]*"[^>]*>\s*([^<]+?)\s*</span>')
_ADDR_TITLE = re.compile(r'<span title="([^"]+)"\s+class="building-address')
_TYPE = re.compile(r'class="building key-value".*?<li title="([^"]+)"', re.S)
_PRICE = re.compile(r'class="price key-value".*?<li title="([^"]+)"', re.S)
_SPACES = re.compile(r'class="spaces key-value"[^>]*>\s*<b>([^<]*)</b>', re.S)
_TOTAL = re.compile(r'([\d,]+)\s+listings', re.I)


class CommercialEdgeAdapter(Adapter):
    supports = (SALE, LEASE)

    def __init__(self, *, site_key: str, domain: str, http: Optional[HttpClient] = None):
        # Cloudflare TLS-fingerprints requests; impersonate real Chrome (curl_cffi).
        # Pace politely — many sequential page fetches.
        super().__init__(http or HttpClient(min_interval=1.0, impersonate="chrome"))
        self.site_key = site_key
        self.domain = domain

    def fetch(self, transaction_type: str, *, limit: Optional[int] = None) -> Iterator[Listing]:
        if transaction_type not in (SALE, LEASE):
            raise ValueError(transaction_type)
        listing_type = "Sale" if transaction_type == SALE else "Lease"
        seen: set[str] = set()
        page = 1
        while page <= MAX_PAGES:
            params = {"PropertyTypes": RETAIL_TYPE_ID, "ListingType": listing_type, "Page": page}
            resp = self.http.get(SEARCH.format(domain=self.domain), params=params)
            resp.raise_for_status()
            cards = _CARD.findall(resp.text)
            if not cards:
                break
            new_on_page = 0
            for card in cards:
                listing = self._parse_card(card, transaction_type)
                if listing is None or listing.source_listing_id in seen:
                    continue
                seen.add(listing.source_listing_id)
                new_on_page += 1
                yield listing
                if limit and len(seen) >= limit:
                    return
            if new_on_page == 0:
                break
            page += 1

    def _parse_card(self, card: str, tt: str) -> Optional[Listing]:
        mh = _HREF.search(card)
        if not mh:
            return None
        url = mh.group(1)
        lid = url.rstrip("/").rsplit("/commercial-property/us/", 1)[-1]  # state/city/slug
        name = _clean(_NAME.search(card))
        addr_full = _clean(_ADDR_TITLE.search(card)) or _clean(_ADDR.search(card))
        address, city, state, zipc = _split_address(addr_full, url)
        subtype = _clean(_TYPE.search(card)) or "Retail"
        price_txt = _clean(_PRICE.search(card))
        spaces = _clean(_SPACES.search(card))
        year, sqft = _parse_spaces(spaces)
        return Listing(
            source_site=self.site_key,
            source_listing_id=lid,
            transaction_type=tt,
            source_url=url,
            property_type="retail",
            property_subtype=subtype,
            name=name,
            address=address,
            city=city,
            state=state,
            zip=zipc,
            price=_money(price_txt) if tt == SALE else None,
            price_basis="sale_price" if tt == SALE else (price_txt or "lease_rate"),
            sqft=sqft,
            year_built=year,
            source_status="For Sale" if tt == SALE else "For Lease",
            raw={"card_url": url, "address": addr_full, "price": price_txt, "spaces": spaces},
        )


def _clean(m) -> str:
    if not m:
        return ""
    return htmllib.unescape(m.group(1)).strip()


def _split_address(full: str, url: str):
    """"2578 TX-103, Etoile, TX 75644" -> (addr, city, ST, zip). Falls back to the
    ``/us/{state}/{city}/`` URL path for city/state when the text is sparse."""
    address = city = state = zipc = ""
    if full:
        parts = [p.strip() for p in full.split(",") if p.strip()]
        if parts:
            tail = parts[-1]
            mz = re.match(r"([A-Z]{2})\s*(\d{5})?", tail)
            if mz:
                state = mz.group(1)
                zipc = mz.group(2) or ""
                if len(parts) >= 2:
                    city = parts[-2]
                address = ", ".join(parts[:-2]) if len(parts) > 2 else parts[0]
            else:
                address = ", ".join(parts)
    # URL fallback: /commercial-property/us/{state}/{city}/{slug}/
    m = re.search(r"/commercial-property/us/([a-z]{2})/([a-z0-9\-]+)/", url)
    if m:
        state = state or m.group(1).upper()
        city = city or m.group(2).replace("-", " ").title()
    return address, city, state, zipc


def _parse_spaces(s: str):
    """'Built in 2023 / Property size 10,500 SF' -> ('2023', 10500.0)."""
    year = sqft = None
    if s:
        my = re.search(r"Built in (\d{4})", s)
        if my:
            year = my.group(1)
        ms = re.search(r"([\d,]+)\s*SF", s)
        if ms:
            try:
                sqft = float(ms.group(1).replace(",", ""))
            except ValueError:
                sqft = None
    return year, sqft


def _money(v: str) -> Optional[float]:
    if not v:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", v)
    if not m:
        return None
    try:
        val = float(m.group(0).replace(",", ""))
        return val if val > 0 else None
    except ValueError:
        return None
