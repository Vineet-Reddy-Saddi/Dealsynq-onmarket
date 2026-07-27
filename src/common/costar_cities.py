"""Shared city directory for the CoStar-family sites (Showcase, CityFeet).

Both sites cap any single search at 500 results, so a nationwide query is impossible
and the crawl has to be driven city by city. Showcase's state pages conveniently embed
a per-state city directory with an exact retail listing count per city:

    window.__PRELOADED_STATE__.searchResults.marketingCategories
      -> [{propertyType, forSale, forLease, city, stateCode, listingCount}, ...]

CityFeet exposes no equivalent directory, but it serves the same CoStar market data, so
both adapters plan their crawl from this one source.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .http_client import HttpClient
from .next_rsc import _match_object
from .schema import SALE

SHOWCASE_STATE_URL = "https://www.showcase.com/{st}/retail-space/{txn}/"
RETAIL = "Retail"

# Both adapters ask for these lists on every run (51 states x 2 transaction types), and
# Showcase throttles that: a CityFeet run once lost 12 states' directories to
# rate-limiting, silently shrinking coverage. Cache to disk so each state is fetched at
# most once per TTL, and never re-fetch mid-run.
CACHE = Path(__file__).resolve().parent.parent.parent / "data" / "_cache" / "costar_cities.json"
CACHE_TTL = 7 * 24 * 3600          # city rosters change slowly; a week is plenty
_MEM: dict[str, dict] = {}


def _load_cache() -> dict:
    global _MEM
    if _MEM:
        return _MEM
    try:
        _MEM = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _MEM = {}
    return _MEM


def _save_cache() -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_MEM), encoding="utf-8")
        tmp.replace(CACHE)
    except OSError:
        pass                        # cache is an optimisation, never fatal

STATES = [
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo",
    "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa",
    "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
]


def preloaded_state(http: HttpClient, url: str) -> Optional[dict]:
    """Fetch a Showcase page and return its ``window.__PRELOADED_STATE__`` object."""
    try:
        resp = http.get(url)
        if resp.status_code != 200:
            return None
        text = resp.text
    except Exception:  # noqa: BLE001 - caller treats None as "no data"
        return None
    i = text.find("window.__PRELOADED_STATE__")
    if i < 0:
        return None
    frag = _match_object(text, text.find("{", i))
    if not frag:
        return None
    try:
        return json.loads(frag)
    except ValueError:
        return None


def retail_cities(http: HttpClient, st: str, transaction_type: str) -> list[tuple[str, int]]:
    """Retail city names (+ expected listing count) for one state.

    Returns ``[(city_name, listing_count), ...]``; city names are as CoStar spells them
    (e.g. "Los Angeles"), for the caller to slugify into its own URL shape.
    """
    txn = "for-sale" if transaction_type == SALE else "for-rent"
    key = f"{st}:{txn}"
    cache = _load_cache()
    hit = cache.get(key)
    if hit and (time.time() - hit.get("t", 0)) < CACHE_TTL:
        return [(c, n) for c, n in hit.get("cities", [])]

    state = preloaded_state(http, SHOWCASE_STATE_URL.format(st=st, txn=txn))
    if not state:
        # Throttled or transient failure. Serve a stale cache entry if we have one — a
        # week-old roster is far better than silently dropping the whole state.
        if hit:
            return [(c, n) for c, n in hit.get("cities", [])]
        return []
    cats = (state.get("searchResults") or {}).get("marketingCategories") or []
    want_sale = transaction_type == SALE
    out: list[tuple[str, int]] = []
    for c in cats:
        if c.get("propertyType") != RETAIL:
            continue
        if bool(c.get("forSale")) != want_sale:
            continue
        if (c.get("stateCode") or "").lower() != st:
            continue
        city = (c.get("city") or "").strip()
        if city:
            out.append((city, int(c.get("listingCount") or 0)))
    if out:
        cache[key] = {"t": time.time(), "cities": out}
        _save_cache()
    return out
