"""CommercialCafe / CommercialSearch (Yardi CommercialEdge) per-property detail.

These two sites publish **no coordinates in their search feed**, so all ~54k of their
listings were invisible on the map. The detail page does carry them
(``data-latitude`` / ``data-longitude``), which makes this fetcher the fix for map
coverage as well as for the missing property attributes.

The page has no JSON API. Three things are parsed:
  * JSON-LD ``@graph`` — a ``Product`` node (name, description, image[], category,
    offers.price/availability) and a ``Place`` node (full postal address).
  * ``data-latitude`` / ``data-longitude`` attributes — the coordinates.
  * a label/value facts table rendered as plain markup — Year Built, Property Tenancy,
    Property Type, Building Size, Lot Size, Cap Rate, Property Subtype, Date Updated.

Cloudflare fronts both sites, so the client impersonates Chrome TLS.
"""
from __future__ import annotations

import datetime as dt
import html as htmllib
import json
import re
from typing import Any, Optional

from ..common.http_client import HttpClient
from ..common.schema import SALE, LEASE

_LD = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)
_LAT = re.compile(r'data-latitude="(-?[\d.]+)"')
_LNG = re.compile(r'data-longitude="(-?[\d.]+)"')
_TAGS = re.compile(r"<[^>]+>")

# Facts render as "<x>Label</x><y>Value</y>"; strip markup then read the pairs.
FACT_KEYS = {
    "year built": "year_built",
    "year renovated": "year_renovated",
    "property tenancy": "tenancy",
    "tenancy": "tenancy",
    "property type": "ptype",
    "property subtype": "subtype",
    "building size": "sqft",
    "lot size": "lot",
    "cap rate": "cap_rate",
    "no. stories": "stories",
    "number of stories": "stories",
    "parking ratio": "parking_ratio",
    "zoning": "zoning",
    "occupancy": "occupancy",
    "average space size": "avg_space",
}


class CommercialEdgeDetailFetcher:
    def __init__(self, site_key: str = "commercialcafe", http: Optional[HttpClient] = None):
        self.site_key = site_key
        self.http = http or HttpClient(min_interval=0.5, impersonate="chrome")

    # -- fetch -------------------------------------------------------------
    def fetch(self, listing_id: str, transaction_type: str) -> dict[str, Any]:
        """``listing_id`` for these sites is the URL path (state/city/slug), because the
        feed exposes no numeric id — see the adapter, which stores it that way."""
        row: dict[str, Any] = {
            "source_site": self.site_key,
            "source_listing_id": str(listing_id),
            "transaction_type": transaction_type,
            "fetched_on": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "status": "ok",
        }
        url = self._url(listing_id)
        page = self._get(url)
        if isinstance(page, str) and page in ("notfound", "gated"):
            row["status"] = page
            return row
        if page is None:
            row["status"] = "error"
            return row

        product, place = self._ld(page)
        facts = self._facts(page)

        row["description"] = _clean(product.get("description"))
        imgs = product.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        if isinstance(imgs, list) and imgs:
            row["num_images"] = len(imgs)
            row["image_urls"] = imgs[:40]

        offers = product.get("offers") or {}
        price = _num(offers.get("price"))
        if transaction_type == SALE:
            row["price"] = price
        row["sale_condition"] = _avail(offers.get("availability"))

        addr = (place.get("address") or {})
        row["zoning"] = facts.get("zoning", "")
        row["year_built"] = facts.get("year_built", "")
        row["year_renovated"] = facts.get("year_renovated", "")
        row["tenancy"] = facts.get("tenancy", "")
        row["occupancy"] = facts.get("occupancy", "")
        row["stories"] = _num(facts.get("stories"))
        row["sqft"] = _sf(facts.get("sqft"))
        row["lot_size_acres"] = _acres(facts.get("lot"))
        row["cap_rate"] = _pct(facts.get("cap_rate"))

        lat, lng = _LAT.search(page), _LNG.search(page)
        row["raw_json"] = {
            "url": url,
            "product": product,
            "place": place,
            "facts": facts,
            # Stored under the same shape the CoStar fetcher uses, so the index's
            # coordinate backfill reads both without special-casing.
            "listing": {"location": {
                "latitude": float(lat.group(1)) if lat else None,
                "longitude": float(lng.group(1)) if lng else None,
            }, "address": addr},
        }
        return row

    def _url(self, listing_id: str) -> str:
        """The stored id is the URL path *without* the ``commercial-property/us/``
        prefix (e.g. ``sc/columbia/2757-rosewood-dr``); a full URL is passed straight
        through so callers can hand over ``source_url`` directly."""
        lid = str(listing_id).strip("/")
        if lid.startswith("http"):
            return lid if lid.endswith("/") else lid + "/"
        host = ("www.commercialsearch.com" if self.site_key == "commercialsearch"
                else "www.commercialcafe.com")
        if not lid.startswith("commercial-property/"):
            lid = "commercial-property/us/" + lid
        return f"https://{host}/{lid}/"

    def _get(self, url: str, tries: int = 3):
        for attempt in range(tries):
            try:
                resp = self.http.get(url)
            except Exception:  # noqa: BLE001
                continue
            if resp.status_code == 404:
                return "notfound"
            if resp.status_code in (401, 403) and "Just a moment" not in resp.text:
                return "gated"
            if resp.status_code == 200 and len(resp.text) > 5000:
                return resp.text
        return None

    # -- parsing -----------------------------------------------------------
    @staticmethod
    def _ld(page: str) -> tuple[dict, dict]:
        product, place = {}, {}
        for block in _LD.findall(page):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            nodes = data.get("@graph") if isinstance(data, dict) else None
            for node in (nodes or ([data] if isinstance(data, dict) else [])):
                if not isinstance(node, dict):
                    continue
                if node.get("@type") == "Product" and not product:
                    product = node
                elif node.get("@type") == "Place" and not place:
                    place = node
        return product, place

    @staticmethod
    def _facts(page: str) -> dict[str, str]:
        """Read the label/value fact table out of the rendered markup."""
        text = _TAGS.sub("|", page)
        parts = [htmllib.unescape(p).strip() for p in text.split("|")]
        parts = [p for p in parts if p]
        out: dict[str, str] = {}
        for i, p in enumerate(parts[:-1]):
            key = FACT_KEYS.get(p.lower())
            if key and key not in out:
                val = parts[i + 1].strip()
                if val and val.lower() not in FACT_KEYS:
                    out[key] = val
        return out


def _clean(v) -> str:
    return htmllib.unescape(str(v or "")).strip()


def _avail(v) -> str:
    if not v:
        return ""
    return "Available" if "InStock" in str(v) else "Off-market/under contract"


def _num(v) -> Optional[float]:
    if v in (None, ""):
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(v))
    if not m:
        return None
    try:
        n = float(m.group(0).replace(",", ""))
        return n or None
    except ValueError:
        return None


def _sf(v) -> Optional[float]:
    if not v or "acre" in str(v).lower():
        return None
    return _num(v)


def _acres(v) -> Optional[float]:
    if not v:
        return None
    n = _num(v)
    if n is None:
        return None
    acres = n / 43560.0 if "sf" in str(v).lower() else n
    # The fact-table scrape occasionally grabs a stray number for this field (seen: a
    # value that only makes sense as "0.4 SF" divided down to ~9e-6 acres -- no real
    # retail lot is that size). Reject outside a sane range rather than store a value
    # already known to be wrong; matches the display-side clamp in webapp/static/app.js.
    if not (0.001 <= acres <= 10000):
        return None
    return acres


def _pct(v) -> Optional[float]:
    n = _num(v)
    if n is None:
        return None
    return round(n * 100, 4) if n < 1 else n
