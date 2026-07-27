"""Buildout adapter — one implementation, reused across every Buildout-powered site.

Buildout hosts a brokerage's inventory and exposes it as JSON:

    GET https://buildout.com/plugins/{hash}/inventory?pluginId=0&page={N}
    -> {"inventory":[... up to 40 ...], "meta":{"total":T,"page":N,"limit":40}}

``{hash}`` is the per-site plugin token found in that site's Buildout iframe ``src``
(e.g. TSCG = ba668fad90e207a3d5cfc0037b6206bf0f5d32da). Register each site's hash in
``config/buildout_sites.json``; this one class serves them all.

Per item:
  * ``sale`` (bool) is the transaction discriminator: True → for sale, False → lease
    (``sublease`` also counts as lease).
  * ``property_sub_type_name`` is the retail subtype ("Street Retail", "Strip
    Center", ...). Even retail brokerages list some Office, so we filter to a retail
    allowlist and log any unmapped subtypes (that is the per-site "which types are
    retail?" review step).
  * ``index_attributes`` is a list of ``[label, value]`` display pairs holding price /
    size / cap rate ("Price", "Lease Rate", "Building Size", "Lot Size", "Cap Rate").

Buildout soft-rate-limits rapid repeats (returns an HTML shell), so requests are paced
and non-JSON responses are retried.
"""
from __future__ import annotations

import re
import sys
import time
from typing import Iterator, Optional

from .base import Adapter
from ..common.http_client import HttpClient
from ..common.schema import Listing, SALE, LEASE

INVENTORY = "https://buildout.com/plugins/{hash}/inventory"
PAGE_LIMIT = 40

# Canonical retail subtypes seen across Buildout brokerages. Case-insensitive match on
# ``property_sub_type_name``. Per-site config may extend/override via extra_retail.
RETAIL_SUBTYPES = {
    "retail", "street retail", "general retail", "strip center",
    "anchored strip center", "unanchored strip center", "neighborhood center",
    "community center", "power center", "regional center", "lifestyle center",
    "shopping center", "outlet center", "regional mall", "specialty center",
    "anchor space at regional mall", "retail-pad", "retail pad", "pad site",
    "free standing building", "freestanding", "single tenant", "single-tenant",
    "net lease", "nnn", "retail/restaurant", "restaurant", "fast food",
    "convenience store", "gas station", "c-store", "big box", "bank",
    "car wash", "auto", "vehicle related", "day care", "health club", "grocery",
    "mixed use", "mixed-use", "commercial",
}


class BuildoutAdapter(Adapter):
    supports = (SALE, LEASE)

    def __init__(
        self,
        *,
        site_key: str,
        hash: str,
        domain: str,
        extra_retail: Optional[set[str]] = None,
        exclude_retail: Optional[set[str]] = None,
        http: Optional[HttpClient] = None,
    ):
        super().__init__(http or HttpClient(min_interval=1.2))  # pace: Buildout is touchy
        self.site_key = site_key
        self.hash = hash
        self.domain = domain
        self.retail = (RETAIL_SUBTYPES | {s.lower() for s in (extra_retail or set())}) - {
            s.lower() for s in (exclude_retail or set())
        }
        self._unmapped: set[str] = set()

    # -- fetch -------------------------------------------------------------
    def fetch(self, transaction_type: str, *, limit: Optional[int] = None) -> Iterator[Listing]:
        if transaction_type not in (SALE, LEASE):
            raise ValueError(transaction_type)
        url = INVENTORY.format(hash=self.hash)
        page = 1
        yielded = 0
        seen = 0            # actual items received across pages (any page size)
        total = None
        while True:
            payload = self._get_page(url, page)
            if payload is None:
                break
            inv = payload.get("inventory") or []
            meta = payload.get("meta") or {}
            if total is None:
                total = meta.get("total")
            if not inv:
                break          # ran out of pages — definitive stop
            for item in inv:
                tt = SALE if item.get("sale") else LEASE
                if tt != transaction_type:
                    continue
                if not self._is_retail(item):
                    continue
                yield self._to_listing(item, tt)
                yielded += 1
                if limit and yielded >= limit:
                    return
            seen += len(inv)
            # Stop when we've received the full inventory. Count ACTUAL items —
            # Buildout's page size varies (30 or 40), so never derive stop from
            # an assumed PAGE_LIMIT (that stops early on large inventories).
            if total is not None and seen >= total:
                break
            page += 1
        if self._unmapped:
            print(f"  [{self.site_key}] unmapped subtypes (review for retail): "
                  f"{sorted(self._unmapped)}", file=sys.stderr)

    def _get_page(self, url: str, page: int) -> Optional[dict]:
        params = {"pluginId": 0, "page": page}
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": f"https://buildout.com/plugins/{self.hash}/{self.domain}/inventory/?pluginId=0&iframe=true",
        }
        for attempt in range(4):
            resp = self.http.get(url, params=params, headers=headers)
            text = resp.text.lstrip()
            if text.startswith("{"):
                try:
                    return resp.json()
                except ValueError:
                    pass
            time.sleep(2 * (attempt + 1))  # Buildout served the HTML shell; back off
        print(f"  [{self.site_key}][warn] page {page} never returned JSON; stopping",
              file=sys.stderr)
        return None

    # -- retail filter -----------------------------------------------------
    def _is_retail(self, item: dict) -> bool:
        sub = (item.get("property_sub_type_name") or "").strip()
        if not sub:
            return True  # untyped: keep (retail brokerage default); refine per-site
        low = sub.lower()
        if low in self.retail or any(tok in low for tok in ("retail", "restaurant", "shop", "mall")):
            return True
        self._unmapped.add(sub)
        return False

    # -- mapping -----------------------------------------------------------
    def _to_listing(self, item: dict, tt: str) -> Listing:
        attrs = {str(k).strip().lower(): v for k, v in (item.get("index_attributes") or [])}
        price, basis = self._price(attrs, tt)
        return Listing(
            source_site=self.site_key,
            source_listing_id=str(item.get("id")),
            transaction_type=tt,
            # Some sites (BHHS) return a site-relative plugin path here, which is a dead
            # link once it leaves the iframe — resolve it against buildout.com.
            source_url=_abs(item.get("show_link") or item.get("pdf_url") or ""),
            property_type="retail",
            property_subtype=item.get("property_sub_type_name") or "",
            name=item.get("display_name") or item.get("name") or "",
            address=item.get("address") or "",
            city=item.get("city") or "",
            state=item.get("state") or "",
            zip=item.get("zip") or "",
            lat=_num(item.get("latitude")),
            lng=_num(item.get("longitude")),
            price=price,
            price_basis=basis,
            sqft=_parse_sf(attrs.get("building size") or attrs.get("available")
                           or item.get("size_summary")),
            lot_size_acres=_parse_acres(attrs.get("lot size") or item.get("size_summary")),
            cap_rate=_parse_pct(attrs.get("cap rate")),
            brokerage=self.domain,
            source_status=item.get("deal_status_label_override") or (
                "Under Contract" if item.get("under_contract") else "Available"),
            raw=item,
        )

    @staticmethod
    def _price(attrs: dict, tt: str) -> tuple[Optional[float], str]:
        if tt == SALE:
            return _parse_money(attrs.get("price") or attrs.get("sale price")), "sale_price"
        rate = attrs.get("lease rate") or attrs.get("rate")
        return _parse_money(rate), "lease_rate" if rate else ""


# -- parse helpers ---------------------------------------------------------
def _abs(url: str) -> str:
    """Make a Buildout link absolute. Relative plugin paths are unusable as a public
    'view original listing' link."""
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://buildout.com" + url
    return url


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_money(v) -> Optional[float]:
    if not v:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", str(v))
    if not m:
        return None
    try:
        val = float(m.group(0).replace(",", ""))
        return val if val > 0 else None
    except ValueError:
        return None


def _parse_sf(v) -> Optional[float]:
    if not v or "acre" in str(v).lower():
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*SF", str(v), re.I)
    return float(m.group(1).replace(",", "")) if m else None


def _parse_acres(v) -> Optional[float]:
    if not v:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*acre", str(v), re.I)
    return float(m.group(1).replace(",", "")) if m else None


def _parse_pct(v) -> Optional[float]:
    if not v:
        return None
    m = re.search(r"([\d.]+)\s*%", str(v))
    return float(m.group(1)) if m else None
