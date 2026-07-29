"""NNN Pro per-property detail fetcher (nnnpro.com, Surmount platform).

The list feed (``src/adapters/nnnpro.py``) carries about a dozen fields. The detail page
carries the rest of what actually prices a net-lease deal: building size, lot size, year
built, the lease itself (type, commencement, expiration, term, rental increases,
renewal options remaining), NOI, ownership type, and the broker roster.

Like the list page this is a Next.js App Router route with no JSON API -- the record is
streamed as RSC flight data. Flight payloads are deduplicated, so the property object
arrives full of ``"$3e"``-style pointers into other numbered rows; ``chunk_table`` +
``resolve_refs`` in ``src/common/next_rsc.py`` rebuild it into a plain dict.

**Images are deliberately not stored.** Every asset URL here is an S3 presigned link
with ``X-Amz-Expires=3600`` -- one hour -- and the bucket refuses unsigned reads (403),
so there is no stable URL to derive. Persisting them would guarantee dead links; the
376 card images captured by the list adapter are already 403 for exactly this reason.
``num_images`` is likewise left unset rather than advertising photos the viewer cannot
show.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional

from ..common.http_client import HttpClient
from ..common.next_rsc import chunk_table, flight_blob, resolve_refs

DETAIL_URL = "https://www.nnnpro.com/properties/{id}"


class NnnProDetailFetcher:
    site_key = "nnnpro"

    def __init__(self, http: Optional[HttpClient] = None):
        self.http = http or HttpClient(min_interval=0.6, impersonate="chrome")

    def fetch(self, listing_id: str, transaction_type: str,
              url_hint: Optional[str] = None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "source_site": self.site_key,
            "source_listing_id": str(listing_id),
            "transaction_type": transaction_type,
            "fetched_on": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "status": "ok",
        }
        url = url_hint if (url_hint or "").startswith("http") else DETAIL_URL.format(id=listing_id)
        page = self._get(url)
        if page in ("notfound", None):
            row["status"] = page or "error"
            return row

        table = chunk_table(flight_blob(page))
        # The property record is the one carrying NOI -- anchoring on a field unique to
        # it is more robust than assuming a chunk id, which varies per page.
        raw = next((v for v in table.values()
                    if isinstance(v, dict) and "net_operating_income" in v), None)
        if raw is None:
            row["status"] = "error"
            return row
        p = resolve_refs(raw, table)

        row["description"] = _highlights_text(p)
        row["sqft"] = _num(p.get("building_size"))
        row["lot_size_acres"] = _acres(p.get("lot_size"))
        row["year_built"] = _year(p.get("year_built"))
        row["year_renovated"] = _year(p.get("year_renovated"))
        row["price"] = _num(p.get("price"))
        row["cap_rate"] = _num(p.get("cap_rate"))
        row["lease_type"] = _text(p.get("lease_type"))
        row["ownership"] = _text(p.get("ownership_type"))
        row["sale_condition"] = _text(p.get("status"))
        # Every NNN Pro asset is single-tenant net lease -- that is the whole inventory.
        row["tenancy"] = "Single tenant"
        row["brokerage"] = "NNN Pro Group"

        names = []
        for c in (p.get("contacts") or []):
            if not isinstance(c, dict):
                continue
            nm = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x).strip()
            if nm:
                names.append(nm)
        bor = p.get("broker_of_record")
        if isinstance(bor, dict):
            nm = " ".join(x for x in (bor.get("first_name"), bor.get("last_name")) if x).strip()
            if nm and nm not in names:
                names.append(nm)
        if names:
            row["broker_names"] = ", ".join(names[:6])

        row["raw_json"] = {
            "url": url,
            "net_operating_income": _num(p.get("net_operating_income")),
            "lease_commencement_date": p.get("lease_commencement_date"),
            "lease_expiration_date": p.get("lease_expiration_date"),
            "lease_term_years": _num(p.get("lease_term")),
            "rental_increase": _text(p.get("rental_increase")),
            "renewal_options_remaining": _text(p.get("renewal_options_remaining")),
            "sub_type": _text(p.get("sub_type")),
            "property_type": _text(p.get("property_type")),
            "concept": _text(p.get("concept")),
            "is_sale_leaseback": p.get("is_sale_leaseback"),
            "is_bonus_eligible": p.get("is_bonus_eligible"),
            "highlights": [h.get("text") for h in (p.get("highlights") or [])
                           if isinstance(h, dict) and h.get("text")],
            "address": p.get("address"),
        }
        return row

    def _get(self, url: str, tries: int = 3):
        for _ in range(tries):
            try:
                resp = self.http.get(url)
            except Exception:  # noqa: BLE001
                continue
            if resp.status_code == 404:
                return "notfound"
            if resp.status_code == 200 and len(resp.text) > 3000:
                return resp.text
        return None


def _highlights_text(p: dict) -> str:
    """NNN Pro leaves ``description`` null on most listings; the marketing copy lives in
    ``highlights`` as "Label | prose" bullets, which reads fine as a description."""
    parts = []
    if isinstance(p.get("description"), str) and p["description"].strip():
        parts.append(p["description"].strip())
    bullets = [h.get("text", "").strip() for h in (p.get("highlights") or [])
               if isinstance(h, dict) and h.get("text")]
    bullets = [re.sub(r"\s+", " ", b) for b in bullets if b]
    if bullets:
        parts.append("\n".join("- " + b for b in bullets))
    return "\n\n".join(parts)


def _text(v) -> str:
    if isinstance(v, dict):
        return str(v.get("text") or "").strip()
    return str(v or "").strip()


def _year(v) -> str:
    n = _num(v)
    return str(int(n)) if n and 1800 <= n <= 2100 else ""


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n or None


def _acres(v) -> Optional[float]:
    n = _num(v)
    if n is None:
        return None
    # Same sanity clamp the other fetchers use.
    return n if 0.001 <= n <= 10000 else None
