"""RIPCO per-property detail fetcher.

RIPCO's list feed is Algolia (``src/adapters/algolia.py``), and that index is unusually
thin for detail purposes: ``content``, ``images`` and ``post_excerpt`` all come back
empty, leaving only address/sqft/coordinates. Everything a broker actually wants —
asking rent, frontage, possession, lease term, the per-space breakdown, co-tenancy and
the marketing copy — exists only on the WordPress-rendered property page.

The WP REST API (``/wp-json/wp/v2/property-listings/{id}``) is public but returns the
same empty ``content``/``acf``, so it is deliberately *not* used; the rendered page is
the only complete source. It is server-side HTML (no JS execution needed) with a
consistent shape:

    <div class="info-section ..."><h4>Key Details</h4>
      <div class="section-content ...">
        <div class="item"><p class="title">Frontage</p><p class="detail">Approx 20 FT</p></div>
        ...
    <div class="info-section ..."><h4>Neighbors</h4>
      <div class="section-content ..."><ul><li>Sweetgreen</li>...</ul>

Section headings vary by listing type (``Comments`` on a lease, ``Investment
Highlights`` on a sale, ``Neighbors`` only where co-tenancy is marketed), so sections
are read generically by heading rather than matched against a fixed list.

Brokers live in a separate ``#brokers`` sidebar with name, phone and email each.
"""
from __future__ import annotations

import datetime as dt
import re
from html import unescape as html_unescape
from typing import Any, Optional

from ..common.http_client import HttpClient

BASE = "https://www.ripcony.com"

_ITEM = re.compile(
    r'<div class="item">\s*<p class="title">(.*?)</p>\s*<p class="detail">(.*?)</p>', re.S)
_SECTION = re.compile(
    r'<h4>(.*?)</h4>.*?<div class="section-content[^"]*">(.*?)(?=<div class="info-section|'
    r'<div id="sticky-column")', re.S)
_LI = re.compile(r"<li>(.*?)</li>", re.S)
_TAGS = re.compile(r"<[^>]+>")

# Fact-table labels -> promoted column. RIPCO is a leasing-heavy shop, so "Asking Rent"
# is usually a phrase ("Upon request") rather than a number and is kept as text.
FACT_KEYS = {
    "total square feet": ("sqft", "num"),
    "square feet": ("sqft", "num"),
    "available spaces": ("num_suites", "num"),
    "asking rent": ("lease_rate_yearly", "text"),
    "asking price": ("price", "num"),
    "price": ("price", "num"),
    "possession": ("sale_condition", "text"),
    "zoning": ("zoning", "text"),
    "block & lot": ("apn", "text"),
    "block and lot": ("apn", "text"),
    "year built": ("year_built", "text"),
    "lot size": ("lot_size_acres", "num"),
    "cap rate": ("cap_rate", "num"),
}

# Sections whose bullets are marketing copy worth folding into the description, keyed by
# a lowercase substring of the heading (headings differ per listing; see module docstring).
DESC_SECTIONS = ("comment", "highlight", "description", "detail", "opportunity")


class RipcoDetailFetcher:
    site_key = "ripco"

    def __init__(self, http: Optional[HttpClient] = None):
        self.http = http or HttpClient(min_interval=0.8, impersonate="chrome")

    def fetch(self, listing_id: str, transaction_type: str,
              url_hint: Optional[str] = None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "source_site": self.site_key,
            "source_listing_id": str(listing_id),
            "transaction_type": transaction_type,
            "fetched_on": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "status": "ok",
        }
        url = self._url(url_hint)
        if not url:
            row["status"] = "error"
            return row

        page = self._get(url)
        if page in ("notfound", None):
            row["status"] = page or "error"
            return row

        facts = {_label(k): _text(v) for k, v in _ITEM.findall(page)}
        sections = {_label(h): body for h, body in _SECTION.findall(page)}

        parts: list[str] = []
        for head, body in sections.items():
            if not any(tok in head for tok in DESC_SECTIONS):
                continue
            bullets = [_text(li) for li in _LI.findall(body)]
            bullets = [b for b in bullets if b]
            if bullets:
                parts.append(f"{head.title()}:\n- " + "\n- ".join(bullets))
        # Co-tenancy: which brands trade alongside this space is a core retail signal,
        # so it is surfaced in the description rather than buried in raw_json.
        neighbors = [_text(li) for li in _LI.findall(sections.get("neighbors", ""))]
        neighbors = [n for n in neighbors if n]
        if neighbors:
            parts.append("Neighbors: " + ", ".join(neighbors))
        row["description"] = "\n\n".join(parts)

        for label, value in facts.items():
            mapped = FACT_KEYS.get(label)
            if not mapped or row.get(mapped[0]) or not value:
                continue
            key, kind = mapped
            if kind == "num":
                n = _num(value)
                if n is None:
                    continue
                if key == "lot_size_acres" and not (0.001 <= n <= 10000):
                    continue
                row[key] = n
            else:
                row[key] = value

        brokers = _brokers(page)
        if brokers:
            row["broker_names"] = ", ".join(b["name"] for b in brokers[:6])
            row["brokerage"] = "RIPCO Real Estate"

        imgs = _images(page)
        if imgs:
            row["num_images"] = len(imgs)
            row["image_urls"] = imgs[:40]

        row["raw_json"] = {
            "url": url, "facts": facts, "brokers": brokers, "neighbors": neighbors,
            "brochure": _brochure(page),
        }
        return row

    @staticmethod
    def _url(url_hint: Optional[str]) -> Optional[str]:
        if not url_hint:
            return None
        u = url_hint if url_hint.startswith("http") else BASE + "/" + url_hint.lstrip("/")
        return u if u.endswith("/") else u + "/"

    def _get(self, url: str, tries: int = 3):
        for attempt in range(tries):
            try:
                resp = self.http.get(url)
            except Exception:  # noqa: BLE001
                continue
            if resp.status_code == 404:
                return "notfound"
            if resp.status_code == 200 and len(resp.text) > 3000:
                return resp.text
        return None


def _brokers(page: str) -> list[dict]:
    """Split on the card boundary rather than matching a card as one regex: the last
    broker has no following sibling to anchor against, and an end-of-block alternative
    silently dropped it (a two-broker listing reported one)."""
    m = re.search(r'<div id="brokers"[^>]*>(.*?)</div>\s*</div>\s*</div>', page, re.S)
    if not m:
        return []
    out = []
    for card in m.group(1).split('<div class="broker')[1:]:
        name = re.search(r"<h6>(.*?)</h6>", card, re.S)
        if not name:
            continue
        phone = re.search(r'href="tel:([^"]+)"', card)
        email = re.search(r'href="mailto:([^"?]+)', card)
        out.append({
            "name": _text(name.group(1)),
            "phone": phone.group(1) if phone else "",
            "email": email.group(1) if email else "",
        })
    return [b for b in out if b["name"]]


def _images(page: str) -> list[str]:
    """Property photography only. Broker headshots live under the same uploads path, so
    they are filtered by their fixed ``_75x75`` suffix rather than by path."""
    urls = re.findall(r'(https://www\.ripcony\.com/wp-content/uploads/[^"\' >]+?'
                      r'\.(?:jpg|jpeg|png|webp))', page, re.I)
    seen, out = set(), []
    for u in urls:
        if "_75x75" in u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _brochure(page: str) -> str:
    m = re.search(r'href="([^"]+\.pdf)"', page, re.I)
    return m.group(1) if m else ""


def _label(s: str) -> str:
    return _text(s).rstrip(":").strip().lower()


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html_unescape(_TAGS.sub(" ", s))).strip()


def _num(v) -> Optional[float]:
    """Sign is deliberately not parsed. RIPCO writes approximate sizes as "+/-1,440 SF",
    and reading that leading "-" as a minus produced a -1,440 SF listing. Every field
    this feeds (sqft, price, acres, cap rate, suite count) is non-negative by nature."""
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
