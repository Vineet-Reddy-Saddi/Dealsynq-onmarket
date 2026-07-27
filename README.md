# On-Market Scraper

Scrapes **retail** on-market commercial listings (for-sale + for-lease, tracked
separately) from ~84 CRE marketplace/brokerage/auction/net-lease sources, preferring
each site's underlying JSON/API over rendered HTML. Detects new listings and
delistings (available → gone = went off-market) on each run.

Sibling to the off-market properties project. Site research lives in
[`recon/RECON.md`](recon/RECON.md).

## Status

| Piece | State |
|---|---|
| Pipeline (fetch → normalize → CSV → lifecycle diff) | ✅ working, tested |
| **Crexi** adapter (sale) | ✅ full quadtree crawl, ~56k retail listings |
| Crexi lease | ⛔ endpoint not yet captured (`/assets/search` is sale-only) |
| **Buildout** adapter (reusable) | ✅ built; sale + lease; retail-filtered |
| Buildout sites registered | ✅ 5 (tscg, midamerica, franklinst, svn, fortis); ~6 more need JS-DOM token extraction |
| Everything else | 🔜 per recon build order |

## Quick start

```bash
pip install -r requirements.txt

python run.py --list                          # registered sites
python run.py --site crexi --type sale --limit 100   # smoke test (100 rows)
python run.py --site crexi --type sale        # full run
python run.py --all --type sale               # every registered site
```

Output: `data/listings_for_sale.csv` and `data/listings_for_lease.csv`.
Re-running updates the same files and prints a `new / updated / relisted / delisted`
summary. Schedule it (e.g. daily) to build the availability history.

## Architecture

```
run.py                     CLI orchestrator
src/
  registry.py              site_key -> adapter
  common/
    schema.py              canonical Listing + CSV columns
    http_client.py         requests session: browser headers, rate limit, retry/backoff
    storage.py             CSV store + lifecycle (first_seen/last_seen/delisted_on)
  adapters/
    base.py                Adapter interface: fetch(transaction_type) -> Iterator[Listing]
    crexi.py               Crexi (geo quadtree crawl)
    buildout.py            Buildout platform (one class, many sites)
config/
  buildout_sites.json      Buildout sites: {site_key, domain, hash}
data/
  listings_for_sale.csv    one row per sale listing (all sites)
  listings_for_lease.csv   one row per lease listing (all sites)
```

Adding a site = write one `Adapter` subclass that yields `Listing` objects and
register it in `registry.py`. Storage, CSV, and lifecycle tracking are shared.

**Adding a Buildout site** is even cheaper — no code: find the site's plugin token
(view its listings page, grep the DOM for `token:"<40 hex>"` or the
`buildout.com/plugins/<hash>/` iframe), add `{site_key, name, domain, hash}` to
`config/buildout_sites.json`, done. Run reports any property subtypes it couldn't
classify as retail so you can tune `extra_retail`/`exclude_retail` per site.

### Lifecycle / delisting model

On each run an adapter yields the *currently active* listings for its site.
`storage.apply_run` merges them into the CSV:

* **new** key → `first_seen = last_seen = now`
* **seen again** → fields refreshed, `last_seen = now`
* **previously active, now absent** (same site ran) → `delisted_on = now` — went off-market
* **reappears after delisting** → `delisted_on` cleared (relisted)

Delisting is **scoped to the sites in the run**, so running only Crexi never marks
another site's rows as delisted. Lifecycle logic is unit-tested (new → delist →
site-isolation → relist).

## Notes on Crexi (the reference implementation)

* API: `POST https://api.crexi.com/assets/search`. The website is Cloudflare-
  challenged but the API is not — plain `requests` with browser-ish headers works.
* Two server limits: page size is capped at **50**, and `offset + count < 1500`, so a
  single filter can only reach ~1,450 of the ~56k retail rows.
* **Solution: quadtree crawl** over a lat/lng bounding box — split any tile with
  >1,400 results into quadrants, recurse, page tiles that fit. Covers priced +
  unpriced + all territories, de-duplicated by id (tiles overlap by an epsilon so
  boundary rows are never dropped).
* ~0.85% of listings have no coordinates; a **state-code sweep** second pass catches
  those. Residual: a handful of listings with neither coordinates nor state.
* **Lease** is a separate, not-yet-captured endpoint; `LEASE` raises
  `NotImplementedError` with a clear TODO.

## Storage roadmap

CSV now; migrate to Postgres when volume and cross-site overlap queries demand it. The
`Listing` schema and `raw_json` column are designed to port directly to a
`listings` + `availability_history` schema.
