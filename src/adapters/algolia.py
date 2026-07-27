"""Algolia adapter — config-driven, one class for every Algolia-backed site.

Each brokerage runs its own Algolia app + index with its own field names, so the
per-site schema lives in ``config/algolia_sites.json`` (app_id, api_key, index, the
sale/lease + property-type facets, and a field map). This class turns that config into
paginated queries against Algolia's search API and normalizes hits into Listings.

  * property_type is filtered **server-side** via ``facetFilters`` (retail allowlist,
    OR'd within one clause).
  * the sale/lease split is server-side when the field is an Algolia facet
    (``txn.facetable``); otherwise it is filtered **client-side** on the mapped field
    value, which the getter flattens (handles nested arrays like RIPCO's ``[["lease"]]``).

Field access supports dotted paths (``coordinates.lat``) and joins/ flattens list
values, so both a flat CMS index (Newmark) and a WordPress index (RIPCO) map cleanly.
"""
from __future__ import annotations

from typing import Iterator, Optional

from .base import Adapter
from ..common.http_client import HttpClient
from ..common.schema import Listing, SALE, LEASE

QUERY_URL = "https://{app}-dsn.algolia.net/1/indexes/{index}/query"
HITS_PER_PAGE = 1000  # Algolia's default max window (paginationLimitedTo)


class AlgoliaAdapter(Adapter):
    supports = (SALE, LEASE)

    def __init__(
        self,
        *,
        site_key: str,
        app_id: str,
        api_key: str,
        index: str,
        base_filters: Optional[list] = None,
        txn: dict,
        ptype: dict,
        field_map: dict,
        base_url: str = "",
        http: Optional[HttpClient] = None,
    ):
        super().__init__(http or HttpClient(min_interval=0.4))
        self.site_key = site_key
        self.app_id = app_id
        self.index = index
        self.base_filters = base_filters or []
        self.txn = txn
        self.ptype = ptype
        self.map = field_map
        # Some indexes store a site-relative URL (Newmark's `url` is "/properties/..."),
        # which is a dead link outside the site. Resolve it against the site's origin.
        self.base_url = base_url.rstrip("/")
        self._url = QUERY_URL.format(app=app_id.lower(), index=index)
        self._headers = {
            "X-Algolia-Application-Id": app_id,
            "X-Algolia-API-Key": api_key,
            "content-type": "application/json",
        }

    # -- fetch -------------------------------------------------------------
    def fetch(self, transaction_type: str, *, limit: Optional[int] = None) -> Iterator[Listing]:
        if transaction_type not in (SALE, LEASE):
            raise ValueError(transaction_type)
        want = self.txn["sale"] if transaction_type == SALE else self.txn["lease"]

        facet_filters: list = list(self.base_filters)
        # retail property types — OR within a single clause
        facet_filters.append([f"{self.ptype['field']}:{v}" for v in self.ptype["retail"]])
        if self.txn.get("facetable"):
            facet_filters.append([f"{self.txn['field']}:{want}"])

        yielded = 0
        page = 0
        while True:
            body = {
                "query": "",
                "hitsPerPage": HITS_PER_PAGE,
                "page": page,
                "facetFilters": facet_filters,
                "attributesToHighlight": [],
                "attributesToSnippet": [],
            }
            resp = self.http.post(self._url, json=body, headers=self._headers)
            resp.raise_for_status()
            j = resp.json()
            hits = j.get("hits") or []
            if not hits:
                break
            for h in hits:
                if not self.txn.get("facetable"):
                    # sale/lease field isn't an Algolia facet — filter here.
                    got = self._flatten(self._get(h, self.txn["field"])).lower()
                    if str(want).lower() not in got.split(", "):
                        continue
                yield self._to_listing(h, transaction_type)
                yielded += 1
                if limit and yielded >= limit:
                    return
            page += 1
            if page >= (j.get("nbPages") or 1):
                break

    # -- field access ------------------------------------------------------
    @staticmethod
    def _get(obj, path: str):
        cur = obj
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        return cur

    @staticmethod
    def _flatten(v) -> str:
        """`[["lease"]]` -> "lease"; `["a","b"]` -> "a, b"; scalar -> str; None -> ""."""
        out: list[str] = []

        def walk(x):
            if isinstance(x, (list, tuple)):
                for i in x:
                    walk(i)
            elif x is not None:
                out.append(str(x))

        walk(v)
        return ", ".join(out)

    def _val(self, hit, key):
        path = self.map.get(key)
        return self._get(hit, path) if path else None

    def _str(self, hit, key) -> str:
        return self._flatten(self._val(hit, key))

    def _abs(self, url: str) -> str:
        if url.startswith("/") and self.base_url:
            return self.base_url + url
        return url

    # -- mapping -----------------------------------------------------------
    def _to_listing(self, hit: dict, tt: str) -> Listing:
        return Listing(
            source_site=self.site_key,
            source_listing_id=self._str(hit, "id"),
            transaction_type=tt,
            source_url=self._abs(self._str(hit, "url")),
            property_type="retail",
            property_subtype=self._str(hit, "subtype"),
            name=self._str(hit, "name"),
            address=self._str(hit, "address"),
            city=self._str(hit, "city"),
            county=self._str(hit, "county"),
            state=self._str(hit, "state"),
            zip=self._str(hit, "zip"),
            lat=_num(self._val(hit, "lat")),
            lng=_num(self._val(hit, "lng")),
            price=_money(self._val(hit, "price")) if tt == SALE else None,
            price_basis="sale_price" if tt == SALE else "lease_rate",
            sqft=_num(self._val(hit, "sqft")),
            lot_size_acres=_num(self._val(hit, "lot_size_acres")),
            brokerage=self._str(hit, "brokerage"),
            listed_on=self._str(hit, "listed_on"),
            updated_on=self._str(hit, "updated_on"),
            source_status=self._str(hit, "status"),
            raw=hit,
        )


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _money(v) -> Optional[float]:
    """Parse a price that may be numeric or a formatted string ("$5,995,000").
    Non-numeric placeholders ("Subject To Offer", "Call for Price") -> None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v) or None
    import re
    m = re.search(r"[\d,]+(?:\.\d+)?", str(v))
    if not m:
        return None
    try:
        val = float(m.group(0).replace(",", ""))
        return val if val > 0 else None
    except ValueError:
        return None
