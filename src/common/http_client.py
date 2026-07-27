"""Shared HTTP client: browser-like headers, polite rate limiting, retry/backoff.

Adapters get one of these per run. By default it is built on ``requests``. For sites
behind TLS-fingerprinting WAFs (Cloudflare on the Yardi/CommercialEdge sites, and later
the CoStar/Akamai tier), pass ``impersonate="chrome"`` and the client is built on
``curl_cffi`` instead, which replays a real Chrome TLS/JA3 handshake and passes those
challenges. The get/post/raise_for_status surface is identical either way.
"""
from __future__ import annotations

import random
import time
from typing import Any, Optional

import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


class HttpClient:
    def __init__(
        self,
        *,
        min_interval: float = 0.7,   # seconds between requests (rate limit)
        max_retries: int = 4,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_UA,
        default_headers: Optional[dict[str, str]] = None,
        impersonate: Optional[str] = None,   # e.g. "chrome" -> use curl_cffi TLS spoofing
    ):
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self._last_request = 0.0
        self.impersonate = impersonate
        if impersonate:
            from curl_cffi import requests as cffi_requests  # lazy: optional dependency
            self.session = cffi_requests.Session(impersonate=impersonate)
            # curl_cffi already sends a full, matching browser header set; only add extras.
            if default_headers:
                self.session.headers.update(default_headers)
        else:
            self.session = requests.Session()
            self.session.headers.update(
                {
                    "User-Agent": user_agent,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                }
            )
            if default_headers:
                self.session.headers.update(default_headers)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        wait = self.min_interval - elapsed
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.25))
        self._last_request = time.monotonic()

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.request(method, url, **kwargs)
            except Exception as exc:  # requests.RequestException or curl_cffi.CurlError
                last_exc = exc
                self._backoff(attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                self._backoff(attempt, resp)
                continue
            return resp
        if last_exc:
            raise last_exc
        raise RuntimeError(f"request failed after {self.max_retries} retries: {url}")

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def _backoff(self, attempt: int, resp: Optional[requests.Response] = None) -> None:
        retry_after = None
        if resp is not None:
            try:
                retry_after = float(resp.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                retry_after = None
        delay = retry_after if retry_after else min(2 ** attempt, 30)
        time.sleep(delay + random.uniform(0, 0.5))
