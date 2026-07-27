"""CoStar-family per-property detail fetcher — serves Showcase **and** CityFeet.

Showcase detail pages embed ``window.__PRELOADED_STATE__`` whose ``listing`` object is
the richest per-property record in the project. Two facts make one fetcher cover both
sites:

  * Showcase resolves a listing from its **id alone** — ``/x/{id}/`` returns the full
    state regardless of the address slug (verified), so no slug bookkeeping is needed.
  * CityFeet shares CoStar's listing ids (``cs41242082`` == Showcase ``41242082``); a
    sample of 12 CityFeet ids resolved on Showcase 12/12, so CityFeet rows enrich
    through the same endpoint.

What this adds over the search card (both beyond what Crexi exposes):
  * **broker contacts with email and phone** — Crexi's API gives names only
  * **investment highlights** — the bulleted marketing points
  * sale: capRate, tenancy, yearBuilt/Renovated, buildingClass, apnParcelId, zoning,
    landAcres, numberStoriesFloors, parkingSpaces, amenity[], propertyUseType,
    constructionStatus, saleConditions, tenants[]
  * lease: **rentMin/rentMax + rentRateStrings** (an actual asking-rent range),
    leaseTerm, leaseType, numberOfSpaces, min/maxSpace, parkingRatio, shoppingCenter
    type, spaceUses[], features[]

Akamai fronts Showcase, so the client impersonates Chrome TLS.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Optional

from ..common.http_client import HttpClient
from ..common.next_rsc import _match_object
from ..common.schema import SALE, LEASE

DETAIL_URL = "https://www.showcase.com/x/{id}/"


class CoStarDetailFetcher:
    """``site_key`` is set by the caller so rows are attributed to the originating
    site (showcase or cityfeet) even though both fetch from Showcase."""

    def __init__(self, site_key: str = "showcase", http: Optional[HttpClient] = None):
        self.site_key = site_key
        self.http = http or HttpClient(min_interval=0.4, impersonate="chrome")

    # -- fetch -------------------------------------------------------------
    def fetch(self, listing_id: str, transaction_type: str) -> dict[str, Any]:
        row: dict[str, Any] = {
            "source_site": self.site_key,
            "source_listing_id": str(listing_id),
            "transaction_type": transaction_type,
            "fetched_on": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "status": "ok",
        }
        state = self._state(listing_id)
        if isinstance(state, str):
            row["status"] = state
            return row
        if state is None:
            row["status"] = "error"
            return row

        listing = state.get("listing") or {}
        if not listing or str(listing.get("listingId")) != str(listing_id):
            row["status"] = "notfound"
            return row

        det = listing.get("detailsForSale") or listing.get("detailsForLease") or {}
        self._map_common(listing, det, row)
        if listing.get("detailsForLease"):
            self._map_lease(det, row)
        else:
            self._map_sale(det, row)

        row["raw_json"] = {"listing": listing, "gallery": state.get("gallery")}
        return row

    def _state(self, listing_id: str):
        for attempt in range(4):
            try:
                resp = self.http.get(DETAIL_URL.format(id=listing_id))
            except Exception:  # noqa: BLE001
                continue
            if resp.status_code == 404:
                return "notfound"
            if resp.status_code in (401, 403) and "Just a moment" not in resp.text:
                return "gated"
            text = resp.text
            i = text.find("window.__PRELOADED_STATE__")
            if resp.status_code == 200 and i >= 0:
                frag = _match_object(text, text.find("{", i))
                if frag:
                    try:
                        return json.loads(frag)
                    except ValueError:
                        pass
        return None

    # -- mapping -----------------------------------------------------------
    def _map_common(self, listing: dict, det: dict, row: dict) -> None:
        addr = listing.get("address") or {}
        row["description"] = det.get("note") or listing.get("description") or ""
        row["year_built"] = _txt(det.get("yearBuilt"))
        row["year_renovated"] = _txt(det.get("yearRenovated"))
        row["stories"] = _num(det.get("numberStoriesFloors"))
        row["parking_spaces"] = _num(det.get("parkingSpaces"))
        row["apn"] = _txt(det.get("apnParcelId"))
        row["zoning"] = _txt(det.get("zoning") or det.get("zoningDescription"))
        row["lot_size_acres"] = _num(det.get("landAcres") or det.get("landAreaTotal"))
        row["sqft"] = _num(det.get("bldgAreaTotal") or det.get("bldgArea"))
        row["tenancy"] = _txt(det.get("tenancy"))
        row["occupancy"] = _txt(det.get("occupancy"))
        row["num_suites"] = _num(det.get("numberOfSpaces"))

        contacts = listing.get("contacts") or ([listing["contact"]] if listing.get("contact") else [])
        names, brokerage = [], ""
        for c in contacts:
            if not isinstance(c, dict):
                continue
            nm = " ".join(x for x in [c.get("firstName"), c.get("lastName")] if x).strip()
            # Email/phone are part of the public broker card; keep them with the name so
            # the contact is actionable rather than just a label.
            extra = " / ".join(x for x in [c.get("email"), c.get("phone")] if x)
            if nm:
                names.append(f"{nm} ({extra})" if extra else nm)
            brokerage = brokerage or c.get("company") or ""
        row["broker_names"] = "; ".join(names[:6])
        row["brokerage"] = brokerage or ((listing.get("company") or {}).get("name") or "")

        att = listing.get("attachments") or {}
        photos = att.get("photos")
        if isinstance(photos, list):
            row["num_images"] = len(photos)
            row["image_urls"] = [_photo_url(p) for p in photos[:40] if _photo_url(p)]
        elif isinstance(photos, int):
            row["num_images"] = photos

        hi = listing.get("highlights")
        if isinstance(hi, list) and hi:
            # Investment highlights are the broker's own selling points; keep them
            # verbatim appended to the description rather than in a bespoke column.
            row["description"] = (row["description"] + "\n\nHighlights:\n- "
                                  + "\n- ".join(str(x) for x in hi)).strip()

    def _map_sale(self, det: dict, row: dict) -> None:
        row["price"] = _num(det.get("price") or det.get("askingPrice"))
        row["price_per_sqft"] = _num(det.get("priceSF"))
        row["cap_rate"] = _cap_pct(det.get("capRate"))
        row["units"] = _num(det.get("propCount"))
        row["ownership"] = _txt(det.get("note"))
        row["sale_condition"] = _txt(det.get("saleConditions") or det.get("propertyUseType"))
        row["net_rentable_sqft"] = _num(det.get("bldgAreaTotal"))

    def _map_lease(self, det: dict, row: dict) -> None:
        strings = det.get("rentRateStrings") or {}
        row["lease_rate_yearly"] = _txt(strings.get("yearly"))
        row["lease_rate_monthly"] = _txt(strings.get("monthly"))
        # rentMin/rentMax are $/SF/yr. A single (non-range) rate is published as
        # rentMax with rentMin null, so falling back to rentMax is required — reading
        # only rentMin silently dropped the rate on those listings.
        rent = det.get("rentMin")
        if rent in (None, "", 0):
            rent = det.get("rentMax")
        row["price"] = _num(rent)
        row["lease_type"] = _txt(det.get("leaseType"))
        row["sale_condition"] = _txt(det.get("leaseTerm"))
        row["net_rentable_sqft"] = _num(det.get("maxSpace") or det.get("areaTotal"))


def _photo_url(p) -> str:
    """Showcase photos carry the full-size link in ``uri`` (``thumbnailUri`` is the
    small one); other keys are accepted so a schema change degrades rather than breaks."""
    if isinstance(p, str):
        return p
    if isinstance(p, dict):
        for k in ("uri", "url", "imageUrl", "large", "medium", "src", "thumbnailUri"):
            if p.get(k):
                return str(p[k])
    return ""


def _txt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v).strip()


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(v))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _cap_pct(v) -> Optional[float]:
    """Showcase reports cap rate as a fraction (0.06); we store percent (6.0)."""
    n = _num(v)
    if n is None or n <= 0:
        return None
    return round(n * 100, 4) if n < 1 else n
