"""Adapter registry — maps a site key to a factory that builds its adapter."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .adapters.base import Adapter
from .adapters.algolia import AlgoliaAdapter
from .adapters.buildout import BuildoutAdapter
from .adapters.cityfeet import CityFeetAdapter
from .adapters.commercialedge import CommercialEdgeAdapter
from .adapters.crexi import CrexiAdapter
from .adapters.nnnpro import NnnProAdapter
from .adapters.showcase import ShowcaseAdapter
from .common.http_client import HttpClient

_CONFIG = Path(__file__).resolve().parent.parent / "config"

# site_key -> factory(http) -> Adapter
_FACTORIES: dict[str, Callable[[HttpClient | None], Adapter]] = {}


def _register(site_key: str, factory: Callable[[HttpClient | None], Adapter]) -> None:
    _FACTORIES[site_key] = factory


# --- single-site adapters ---
_register(CrexiAdapter.site_key, lambda http: CrexiAdapter(http=http))
_register(NnnProAdapter.site_key, lambda http: NnnProAdapter(http=http))
_register(ShowcaseAdapter.site_key, lambda http: ShowcaseAdapter(http=http))
_register(CityFeetAdapter.site_key, lambda http: CityFeetAdapter(http=http))


# --- Buildout family: one adapter per configured site ---
def _load_buildout() -> None:
    cfg = _CONFIG / "buildout_sites.json"
    if not cfg.exists():
        return
    data = json.loads(cfg.read_text(encoding="utf-8"))
    for site in data.get("sites", []):
        key = site["site_key"]

        def factory(http, s=site):
            return BuildoutAdapter(
                site_key=s["site_key"],
                hash=s["hash"],
                domain=s["domain"],
                extra_retail=set(s.get("extra_retail", [])) or None,
                exclude_retail=set(s.get("exclude_retail", [])) or None,
                http=http,
            )

        _register(key, factory)


_load_buildout()


# --- Algolia family: one adapter per configured site ---
def _load_algolia() -> None:
    cfg = _CONFIG / "algolia_sites.json"
    if not cfg.exists():
        return
    data = json.loads(cfg.read_text(encoding="utf-8"))
    for site in data.get("sites", []):
        key = site["site_key"]

        def factory(http, s=site):
            return AlgoliaAdapter(
                site_key=s["site_key"],
                app_id=s["app_id"],
                api_key=s["api_key"],
                index=s["index"],
                base_filters=s.get("base_filters"),
                txn=s["txn"],
                ptype=s["ptype"],
                field_map=s["map"],
                base_url=s.get("base_url", ""),
                http=http,
            )

        _register(key, factory)


_load_algolia()


# --- CommercialEdge family (Yardi): one adapter per configured site ---
def _load_commercialedge() -> None:
    cfg = _CONFIG / "commercialedge_sites.json"
    if not cfg.exists():
        return
    data = json.loads(cfg.read_text(encoding="utf-8"))
    for site in data.get("sites", []):
        key = site["site_key"]

        def factory(http, s=site):
            return CommercialEdgeAdapter(site_key=s["site_key"], domain=s["domain"], http=http)

        _register(key, factory)


_load_commercialedge()


def available_sites() -> list[str]:
    return sorted(_FACTORIES)


def get_adapter(site_key: str, http: HttpClient | None = None) -> Adapter:
    try:
        factory = _FACTORIES[site_key]
    except KeyError:
        raise KeyError(f"no adapter for site {site_key!r}. Known: {available_sites()}")
    return factory(http)
