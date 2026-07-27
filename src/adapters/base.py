"""Adapter interface. One adapter per site (or per shared platform, e.g. Buildout)."""
from __future__ import annotations

from typing import Iterator

from ..common.http_client import HttpClient
from ..common.schema import Listing


class Adapter:
    #: short stable key stored in every row's ``source_site``
    site_key: str = ""
    #: transaction types this adapter can produce
    supports: tuple[str, ...] = ()

    def __init__(self, http: HttpClient | None = None):
        self.http = http or HttpClient()

    def fetch(self, transaction_type: str, *, limit: int | None = None) -> Iterator[Listing]:
        """Yield the currently-active retail listings for the given transaction type.

        ``limit`` caps the number of records (for smoke tests); None = all.
        """
        raise NotImplementedError
