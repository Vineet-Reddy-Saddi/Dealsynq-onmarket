"""NNN Pro adapter (nnnpro.com) — net-lease retail investment sales.

Next.js App Router site (Surmount platform). No JSON API / ``__NEXT_DATA__``; the
listing rows are streamed as RSC flight data in each page's HTML. ``?page=N`` paginates
server-side (12/page), so we fetch pages and pull listing objects out of the flight
blob. Every NNN Pro listing is a single-tenant net-lease retail asset FOR SALE, so the
adapter only supports SALE.

Listing object (anchored by first key ``concept_text``):
  concept_text (tenant/brand), price, net_operating_income, cap_rate, lease_type.text,
  status_text, address{address_1,city,zip,state_short_name,coordinates{lat,lng}}, id.
Detail URL: ``/properties/{id}``.
"""
from __future__ import annotations

from typing import Iterator, Optional

from .base import Adapter
from ..common.http_client import HttpClient
from ..common.next_rsc import flight_blob, objects_starting_with, strip_rsc_date
from ..common.schema import Listing, SALE

LIST_URL = "https://www.nnnpro.com/properties"
DETAIL_URL = "https://www.nnnpro.com/properties/{id}"
MAX_PAGES = 200  # safety cap (~32 real pages at 12/row)


class NnnProAdapter(Adapter):
    site_key = "nnnpro"
    supports = (SALE,)

    def __init__(self, http: Optional[HttpClient] = None):
        super().__init__(http or HttpClient(min_interval=0.5))

    def fetch(self, transaction_type: str, *, limit: Optional[int] = None) -> Iterator[Listing]:
        if transaction_type != SALE:
            return
        seen: set[str] = set()
        total: Optional[int] = None
        page = 1
        while page <= MAX_PAGES:
            resp = self.http.get(LIST_URL, params={"page": page})
            resp.raise_for_status()
            rows = list(objects_starting_with(flight_blob(resp.text), "concept_text"))
            if not rows:
                break
            if total is None:
                try:
                    total = int(rows[0].get("total_results"))
                except (TypeError, ValueError):
                    total = None
            new_on_page = 0
            for o in rows:
                lid = str(o.get("id"))
                if lid in seen:
                    continue
                seen.add(lid)
                new_on_page += 1
                yield self._to_listing(o)
                if limit and len(seen) >= limit:
                    return
            if new_on_page == 0:
                break
            if total is not None and len(seen) >= total:
                break
            page += 1

    def _to_listing(self, o: dict) -> Listing:
        addr = o.get("address") or {}
        coords = addr.get("coordinates") or {}
        return Listing(
            source_site=self.site_key,
            source_listing_id=str(o.get("id")),
            transaction_type=SALE,
            source_url=DETAIL_URL.format(id=o.get("id")),
            property_type="retail",
            property_subtype=(o.get("lease_type") or {}).get("text") or "",
            name=o.get("concept_text") or addr.get("address_1") or o.get("address_1") or "",
            address=addr.get("address_1") or o.get("address_1") or "",
            city=addr.get("city") or o.get("city") or "",
            state=addr.get("state_short_name") or o.get("short_name") or "",
            zip=addr.get("zip") or "",
            lat=_num(coords.get("lat")),
            lng=_num(coords.get("lng")),
            price=_num(o.get("price")),
            price_basis="sale_price",
            cap_rate=_num(o.get("cap_rate")),
            tenancy="single",  # net-lease single-tenant
            listed_on=str(strip_rsc_date(o.get("publish_date")) or ""),
            source_status=o.get("status_text") or "",
            raw=o,
        )


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
