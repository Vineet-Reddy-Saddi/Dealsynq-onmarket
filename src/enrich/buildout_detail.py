"""Buildout per-property detail fetcher — shared across all 8 Buildout-powered sites
(Lee & Associates, NAI Global, BHHS, TSCG, Mid-America, Franklin Street, SVN, Fortis;
see config/buildout_sites.json).

The list feed (``src/adapters/buildout.py``) gives ~12 fields. Each brokerage's site
embeds a Buildout JS widget that, per listing, injects an iframe pointing at

    https://buildout.com/plugins/{hash}/inventory/{slug}?embedded=true&iframe=true

which server-renders the full detail: description, location description, highlights,
year built / property type / building size (plus whatever other facts that property
has), the broker roster, a photo gallery (usually 10-70 images), and a brochure PDF.

``{slug}`` is NOT the item's internal numeric id — that id is what the list feed calls
``id`` and is what we key enrichment rows on (it has to match ``listings.db``), but
passing it to buildout.com's endpoints 404s. The slug has to be recovered from
``source_url`` (the adapter's resolved ``show_link``), which comes in three different
shapes depending on how each brokerage wired up its embed (confirmed by testing one
listing from each of the 8 sites):
  * query-string routing — ``...?propertyId=<slug>``      (lee, naiglobal, svn, fortis,
    franklinst)
  * path routing — ``.../<rootPath>/<slug>/``              (tscg, midamerica)
  * already a buildout.com URL — ``/plugins/{hash}/inventory/<slug>?...``  (bhhs)
``_slug_from_url`` tries all three, so one fetcher covers the whole family.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from html import unescape as html_unescape
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

from ..common.http_client import HttpClient

_TABLE_ROW = re.compile(r"<tr><td><strong>([^<]*)</strong></td><td>(.*?)</td></tr>", re.S)
_TAGS = re.compile(r"<[^>]+>")

# Property-details table labels -> our promoted column. Values are parsed as numbers
# except zoning/apn/occupancy/tenancy, which stay text. Iteration is first-label-wins
# (dict insertion order == document order), which is what makes "Available SF" beat
# "Building Size" on lease pages without any lease/sale branching: it's simply listed
# first in the table for those.
FACT_KEYS = {
    "year built": ("year_built", "text"),
    "year renovated": ("year_renovated", "text"),
    "available sf": ("sqft", "num"),
    "building size": ("sqft", "num"),
    "size summary": ("sqft", "num"),          # Newmark's label for the same figure
    "total space available": ("sqft", "num"),
    "lot size": ("lot_size_acres", "num"),
    "total lot size": ("lot_size_acres", "num"),
    "cap rate": ("cap_rate", "num"),
    "zoning": ("zoning", "text"),
    "apn": ("apn", "text"),
    "no. stories": ("stories", "num"),
    "number of stories": ("stories", "num"),
    "parking spaces": ("parking_spaces", "num"),
    "units": ("units", "num"),
    "occupancy": ("occupancy", "text"),
    "tenancy": ("tenancy", "text"),
    "sale price": ("price", "num"),   # sale-only: "Lease Rate" is a $/SF/(mo|yr) string,
    "price": ("price", "num"),         # not a bare price, so it's deliberately not mapped.
}


class BuildoutDetailFetcher:
    def __init__(self, site_key: str, hash: str, domain: str, http: Optional[HttpClient] = None):
        self.site_key = site_key
        self.hash = hash
        self.domain = domain
        # Buildout soft-rate-limits rapid repeats (see src/adapters/buildout.py's own
        # warning: "validate one at a time"). A 6-worker x 0.7s run tripped a block
        # after ~325 requests (bare 403s afterward). Match the list adapter's proven
        # pace instead of guessing at a faster one.
        self.http = http or HttpClient(min_interval=1.2, impersonate="chrome")

    # -- fetch -------------------------------------------------------------
    def fetch(self, listing_id: str, transaction_type: str,
              url_hint: Optional[str] = None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "source_site": self.site_key,
            "source_listing_id": str(listing_id),
            "transaction_type": transaction_type,
            "fetched_on": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "status": "ok",
        }
        slug = _slug_from_url(url_hint)
        if not slug:
            row["status"] = "error"  # no source_url on record: can't resolve a slug
            return row

        url = f"https://buildout.com/plugins/{self.hash}/inventory/{slug}?embedded=true&iframe=true"
        page = self._get(url)
        if page in ("notfound", None):
            row["status"] = page or "error"
            return row

        facts = _facts(page)
        desc = _section_value(page, "description")
        loc = _section_value(page, "location_description")
        highlights = _highlights(page)

        parts = [p for p in (desc, loc) if p]
        if highlights:
            parts.append("Highlights:\n- " + "\n- ".join(highlights))
        row["description"] = "\n\n".join(parts)

        for label, value in facts.items():
            mapped = FACT_KEYS.get(label.strip().lower())
            if not mapped or row.get(mapped[0]):
                continue
            key, kind = mapped
            if kind == "num":
                n = _num(value)
                if n is None:
                    continue
                if key == "lot_size_acres" and not (0.001 <= n <= 10000):
                    continue  # same sanity clamp as commercialedge_detail.py
                row[key] = n
            else:
                row[key] = value

        brokers = _brokers(page)
        if brokers:
            row["broker_names"] = ", ".join(b["name"] for b in brokers[:6])
            row["brokerage"] = self.domain

        imgs = _gallery(page)
        if imgs:
            row["num_images"] = len(imgs)
            row["image_urls"] = imgs[:40]

        row["raw_json"] = {
            "url": url, "facts": facts, "highlights": highlights,
            "location_description": loc, "brokers": brokers,
        }
        return row

    def _get(self, url: str, tries: int = 3):
        for attempt in range(tries):
            try:
                resp = self.http.get(url)
            except Exception:  # noqa: BLE001
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 404:
                return "notfound"
            if resp.status_code == 200 and len(resp.text) > 3000:
                return resp.text
            time.sleep(1.5 * (attempt + 1))
        return None


# -- slug recovery -----------------------------------------------------------
def _slug_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if qs.get("propertyId"):
        return qs["propertyId"][0]
    m = re.search(r"/inventory/([^/?]+)", parsed.path)
    if m:
        return m.group(1)
    segs = [s for s in parsed.path.split("/") if s]
    return segs[-1] if segs else None


# -- HTML parsing --------------------------------------------------------
def _facts(page: str) -> dict[str, str]:
    m = re.search(r'slug="property_details_custom_table".*?<table[^>]*>(.*?)</table>', page, re.S)
    if not m:
        return {}
    return {_fact_label(mm.group(1)): _text(mm.group(2)) for mm in _TABLE_ROW.finditer(m.group(1))}


def _fact_label(raw: str) -> str:
    """Normalize a fact-table label to its FACT_KEYS form.

    Roughly half these brokerages render the label with a trailing colon
    ("Building Size:" vs "Building Size") -- an audit of 9,457 enriched rows found
    1,696 colon-suffixed "building size" against 1,449 bare ones. Matching the raw
    string silently dropped every colon variant, losing sqft on 5,055 rows,
    year_built on 2,929, price on 2,822 and lot size on 1,164.
    """
    return raw.strip().rstrip(":").strip().lower()


def _section_value(page: str, name: str) -> str:
    """``description`` and ``location_description`` sections both render as
    ``slug="{name}_{name}_value">...</p>`` — one pattern covers both."""
    m = re.search(rf'slug="{name}_{name}_value">(.*?)</p>', page, re.S)
    return _text(m.group(1)) if m else ""


def _highlights(page: str) -> list[str]:
    m = re.search(r'slug="highlights_custom_text"[^>]*>\s*<ul>(.*?)</ul>', page, re.S)
    if not m:
        return []
    return [_text(li) for li in re.findall(r"<li>(.*?)</li>", m.group(1), re.S)]


def _brokers(page: str) -> list[dict]:
    out = []
    for block in page.split('class="pdt-broker py-1')[1:]:
        block = block[:2000]  # bounded window: one broker card per split chunk
        name = re.search(r'class="pdt-broker-name text-dark[^"]*">([^<]*)</strong>', block)
        if not name:
            continue
        title = re.search(r'class="pdt-broker-title[^"]*">([^<]*)</div>', block)
        phone = re.search(r'href="tel:([^"]+)"', block)
        email = re.search(r'href="mailto:([^"]+)"', block)
        out.append({
            "name": _text(name.group(1)),
            "title": _text(title.group(1)) if title else "",
            "phone": phone.group(1) if phone else "",
            "email": email.group(1) if email else "",
        })
    return out


def _gallery(page: str) -> list[str]:
    """The Photos tab carries a clean JSON array of {id, description, url} as an HTML
    attribute — far more reliable than regexing every cloudfront <img> on the page
    (which also matches broker headshots)."""
    m = re.search(r'slug="image-gallery-tab" images="(\[.*?\])"', page)
    if not m:
        return []
    try:
        items = json.loads(html_unescape(m.group(1)))
    except ValueError:
        return []
    return [it["url"] for it in items if isinstance(it, dict) and it.get("url")]


def _text(s: str) -> str:
    return html_unescape(_TAGS.sub("", s)).strip()


def _num(v) -> Optional[float]:
    """Sign is deliberately not parsed -- brokers write approximate sizes as "+/-1,440
    SF", and reading that leading "-" as a minus yields a negative building. Every
    field this feeds (sqft, price, acres, cap rate, counts) is non-negative by nature."""
    if not v:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", str(v))
    if not m:
        return None
    try:
        n = float(m.group(0).replace(",", ""))
        return n or None
    except ValueError:
        return None
