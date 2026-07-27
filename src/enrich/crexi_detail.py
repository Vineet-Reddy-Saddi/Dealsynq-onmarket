"""Crexi per-property detail fetcher.

The search API returns ~12 fields per listing. Each asset also has a detail record with
far more, across a few endpoints (all unauthenticated):

  SALE  https://api.crexi.com/assets/{id}
        -> marketingDescription, details{}, summaryDetails[] carrying Year Built,
           Year Renovated, Stories, Buildings, Parking, Units, APN, Zoning,
           Lot Size (acres), Occupancy, Tenancy, Ownership, Sale Condition,
           Price/SqFt, Net Rentable (SqFt), Lease Type
  LEASE https://api-lease.crexi.com/assets/{id}/asset-data
        -> baseRateYearly / baseRateMonthly (the asking rent, absent from the lease
           search feed), numberOfSuites, squareFootageMin/Max
  BOTH  /assets/{id}/brokers        -> broker roster + brokerage
        /assets/{id}/gallery        -> full image gallery (often 20-70 photos)
        /assets/{id}/similar-assets -> comparable properties

Endpoints requiring a signed-in user (``/stats``, ``/specialist``, ``/suites``,
``/moderated-gallery``) return 401 and are deliberately not called — we never
authenticate or accept terms.

Cloudflare fronts these hosts and throttles bursts, so requests are paced and 403/HTML
responses are retried with backoff.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from typing import Any, Optional

from ..common.http_client import HttpClient
from ..common.schema import SALE, LEASE

SALE_API = "https://api.crexi.com"
LEASE_API = "https://api-lease.crexi.com"
MAX_IMAGES = 40          # store a generous sample; galleries can exceed 70

HEADERS = {
    "accept": "application/json",
    "origin": "https://www.crexi.com",
    "referer": "https://www.crexi.com/",
}


class CrexiDetailFetcher:
    site_key = "crexi"

    def __init__(self, http: Optional[HttpClient] = None):
        self.http = http or HttpClient(min_interval=0.45, impersonate="chrome")

    # -- low level ---------------------------------------------------------
    def _get(self, url: str, tries: int = 4):
        """Return parsed JSON, or a sentinel string.

        Distinguishes permanent outcomes from transient ones so callers don't retry
        forever: 404 -> ``"notfound"``, 400/401 -> ``"gated"`` (a private or vault-only
        listing; we never authenticate, so it will never succeed). A Cloudflare
        challenge returns HTML instead of JSON and *is* retried with backoff.
        """
        for attempt in range(tries):
            try:
                resp = self.http.get(url, headers=HEADERS)
            except Exception:  # noqa: BLE001
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 404:
                return "notfound"
            if resp.status_code in (400, 401, 403) and "Just a moment" not in resp.text:
                return "gated"       # private listing / requires sign-in: permanent
            text = resp.text.lstrip()
            if resp.status_code == 200 and text[:1] in "{[":
                try:
                    return resp.json()
                except ValueError:
                    pass
            time.sleep(2 * (attempt + 1))
        return None

    # -- public ------------------------------------------------------------
    def fetch(self, listing_id: str, transaction_type: str) -> dict[str, Any]:
        base = SALE_API if transaction_type == SALE else LEASE_API
        row: dict[str, Any] = {
            "source_site": self.site_key,
            "source_listing_id": str(listing_id),
            "transaction_type": transaction_type,
            "fetched_on": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "status": "ok",
        }
        raw: dict[str, Any] = {}

        core = self._get(f"{base}/assets/{listing_id}" if transaction_type == SALE
                         else f"{base}/assets/{listing_id}/asset-data")
        if isinstance(core, str):
            # "notfound" (removed) or "gated" (private/login-only) — both permanent, so
            # recorded distinctly from "error" and never retried by a resumed run.
            row["status"] = core
            return row
        if core is None:
            row["status"] = "error"
            return row
        raw["core"] = core

        if transaction_type == SALE:
            self._map_sale(core, row)
        else:
            self._map_lease(core, row)

        brokers = self._get(f"{base}/assets/{listing_id}/brokers", tries=2)
        if isinstance(brokers, list) and brokers:
            raw["brokers"] = brokers
            names, brokerage = [], ""
            for b in brokers:
                nm = " ".join(x for x in [b.get("firstName"), b.get("lastName")] if x).strip()
                if nm:
                    names.append(nm)
                brokerage = brokerage or ((b.get("brokerage") or {}).get("name") or "")
            row["broker_names"] = ", ".join(names[:6])
            row["brokerage"] = brokerage

        gallery = self._get(f"{base}/assets/{listing_id}/gallery", tries=2)
        if isinstance(gallery, list) and gallery:
            urls = [g.get("imageUrl") for g in gallery if g.get("imageUrl")]
            row["num_images"] = len(gallery)
            row["image_urls"] = urls[:MAX_IMAGES]

        comps = self._get(f"{base}/assets/{listing_id}/similar-assets", tries=2)
        if isinstance(comps, list) and comps:
            row["comp_ids"] = [c.get("id") for c in comps if c.get("id")][:20]

        row["raw_json"] = raw
        return row

    # -- mapping -----------------------------------------------------------
    @staticmethod
    def _attrs(core: dict) -> dict[str, Any]:
        """Merge ``details`` (display strings) and ``summaryDetails`` (typed) into one
        lookup; typed values win because they parse cleanly."""
        out: dict[str, Any] = dict(core.get("details") or {})
        for item in core.get("summaryDetails") or []:
            label = item.get("label") or item.get("key")
            if label is not None:
                out[label] = item.get("value")
        return out

    def _map_sale(self, core: dict, row: dict) -> None:
        a = self._attrs(core)
        row["description"] = core.get("marketingDescription") or core.get("description") or ""
        row["price"] = _num(core.get("askingPrice") or a.get("Asking Price"))
        row["year_built"] = _txt(a.get("Year Built"))
        row["year_renovated"] = _txt(a.get("Year Renovated"))
        row["stories"] = _num(a.get("Stories"))
        row["buildings"] = _num(a.get("Buildings"))
        row["parking_spaces"] = _num(a.get("Parking (spaces)"))
        row["units"] = _num(a.get("Units"))
        row["apn"] = _txt(a.get("APN"))
        row["zoning"] = _txt(a.get("Permitted Zoning") or a.get("Zoning"))
        row["lot_size_acres"] = _num(a.get("Lot Size (acres)"))
        row["sqft"] = _num(a.get("Square Footage"))
        row["net_rentable_sqft"] = _num(a.get("Net Rentable (SqFt)"))
        row["price_per_sqft"] = _num(a.get("Price/SqFt") or a.get("Price per SqFt"))
        row["cap_rate"] = _num(a.get("Cap Rate"))
        row["occupancy"] = _txt(a.get("Occupancy"))
        row["tenancy"] = _txt(a.get("Tenancy"))
        row["ownership"] = _txt(a.get("Ownership"))
        row["sale_condition"] = _txt(a.get("Sale Condition"))
        row["lease_type"] = _txt(a.get("Lease Type"))

    def _map_lease(self, core: dict, row: dict) -> None:
        row["description"] = core.get("description") or ""
        row["lease_rate_yearly"] = _txt(core.get("baseRateYearly"))
        row["lease_rate_monthly"] = _txt(core.get("baseRateMonthly"))
        # Promote the yearly asking rate to the numeric price column when parseable.
        row["price"] = _num(core.get("baseRateYearly"))
        row["num_suites"] = _num(core.get("numberOfSuites"))
        row["sqft"] = _num(core.get("squareFootageMax") or core.get("squareFootageMin"))
        row["brokerage"] = core.get("brokerageName") or ""


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
