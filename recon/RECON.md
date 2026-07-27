# On-Market Scraper — Site Recon Coverage Map

**Goal:** extract **retail** on-market listings (for-sale + for-lease, kept separate) from ~84 CRE sources, preferring each site's underlying JSON/API over rendered HTML. Detect new listings daily and detect delisting (available → gone = went off-market).

**Method used:** (1) automated fingerprint of all 84 sites — HTTP status, anti-bot headers, platform signals; (2) deep browser+API probing of the highest-leverage sites/clusters. Raw fingerprint data: [`fingerprint.tsv`](recon/fingerprint.tsv). No production scraping yet.

Status: recon complete for classification; two reusable APIs proven end-to-end (Crexi, Buildout).

---

## TL;DR — the big findings

1. **Crexi alone = 55,918 retail listings** through one clean, unauthenticated JSON API. Start here. ✅ proven.
2. **A single platform, Buildout, powers ~12 of these brokerage sites.** Crack its `/plugins/{hash}/inventory` JSON endpoint once and you get Newmark, Lee & Associates, NAI Global, SVN, Mid-America, eXp, BHHS, DAUM, TSCG, Fortis, Franklin Street (+ KLNB via sister platform PropertyCapsule). ✅ endpoint proven.
3. **Two big owners = two shared walls, not many.** CoStar owns LoopNet + Showcase + CityFeet (all hard **Akamai** 403). Yardi owns CommercialCafe + CommercialSearch + PropertyShark (all **Cloudflare** challenge). Solve each once → 3 sites each.
4. Roughly **half the list is Easy** (clean HTML or embedded JSON, no serious bot wall). The **Hard tier is small and concentrated** in CoStar/Akamai + Auction.com (Imperva).
5. **Retail-type taxonomy is per-site UI work**, not a blocker — each site has its own retail sub-type list (unanchored strip, street retail, general retail, anchored strip, etc.). Plan is one small `type_map.json` per site mapping that site's vocabulary → our canonical "retail".

---

## Platform clusters (build once, reuse many)

| Cluster | Sites | Access | Difficulty | Notes |
|---|---|---|---|---|
| **Crexi API** | Crexi | `POST api.crexi.com/assets/search` (sale); lease endpoint TBD | 🟢 Easy | Proven. 55,918 retail. `{types:['Retail'],includeUnpriced:true,pageSize,offset}` → `{data[],totalCount}` |
| **Buildout** | Newmark, Lee&Assoc, NAI Global, SVN, Mid-America, eXp, BHHS, DAUM, TSCG, Fortis, Franklin St (Brevitas partial) | `GET buildout.com/plugins/{hash}/inventory?page=N` → JSON `{inventory:[...]}` | 🟢🟡 Easy–Med | Proven on TSCG. Per-site `{hash}` sits in the site's iframe `src`. One adapter serves all. |
| **PropertyCapsule** | KLNB (klnb.propertycapsule.com) | Buildout's sister platform; similar embed/API | 🟡 Med | Same vendor family as Buildout. |
| **CoStar / Akamai wall** | LoopNet, Showcase, CityFeet | Akamai bot mgr, all 403 to plain client | 🔴 Hard | Same owner (CoStar). Needs residential proxies + real headless + TLS/JA3. Solve once. |
| **Yardi / CommercialEdge** | CommercialCafe, CommercialSearch, PropertyShark | Cloudflare challenge (403 plain) | 🟡🔴 Med–Hard | Same owner (Yardi). Shared backend API (commercialedge). Solve once. |
| **Land.com / Akamai** | LandWatch, LandAndFarm | Akamai 403 | 🔴 Hard | Same family. |
| **Algolia search** | Newmark, Horvath&Tremblay, RIPCO | Hosted Algolia index; query directly with appID+searchKey pulled from page JS | 🟢 Easy | Very scrapable once keys extracted. |
| **Sucuri proxy** | B+E/TradeNetLease, Boulder Group | Sucuri Cloudproxy (light) | 🟢 Easy | Boulder returns 200 plainly; B+E 307-redirects. |

---

## Anti-bot posture summary (from fingerprint)

- **Akamai hard-block (403 to plain client):** LoopNet, JLL, Ten-X, Showcase, CityFeet, LandWatch, LandAndFarm. Also fronting: GovDeals, Treasury, IRS Auctions.
- **Cloudflare "Just-a-moment" challenge:** Crexi (but API bypasses it), Colliers, Newmark, SRS, SHOP, PropertyShark, CommercialCafe, CommercialSearch, Miami REALTORS, LandSearch, Moody's CRE, CommercialFlip, LandHub, Hilco, Paramount, KW Commercial, Berkadia, BHHS, Sands, Hanley.
- **Cloudflare (no active challenge, 200 OK):** CBRE, Northmarq, Avison Young, Lee&Assoc, Mid-America, Brevitas, eXp, Kidder, DAUM, Foundry, Franklin St, HighStreet.
- **Imperva/Incapsula (blocked):** Auction.com.
- **Sucuri:** B+E, Boulder Group.
- **None / clean 200:** Cushman & Wakefield, Marcus & Millichap, Transwestern, Eastdil, SVN, RealNex, TotalCommercial, CIMLS, RealtyZapp, DealStream, CommercialMLS/CBA, MNCAR, SC Commercial MLS, RIMarketplace, Bid4Assets, Williams Auction, Century21, Tranzon, LastBid, FRE, Coldwell Banker, RE/MAX, Ariel, TSCG(host), RIPCO, NNN.market, Kase Group, MyEListing, DisneyIG, NNN Pro, Silber, CRECo.ai, Foundry(host).

---

## Per-site classification

Legend — **Access:** `api`=clean JSON API · `buildout`=Buildout plugin JSON · `algolia` · `next`=`__NEXT_DATA__`/Next API · `html`=parse rendered HTML (often LD-JSON present) · `hard`=behind serious bot wall.
**Diff:** 🟢 Easy · 🟡 Medium · 🔴 Hard.

### Tier 1 — National brokerages & major marketplaces
| # | Site | Anti-bot | Access | Diff | Notes |
|---|------|----------|--------|------|-------|
| 1 | LoopNet | Akamai 403 | hard | 🔴 | CoStar. Biggest inventory, hardest wall. Defer; solve with CoStar cluster. |
| 2 | Crexi | CF (API bypasses) | **api** | 🟢 | ✅ Proven. 55,918 retail. **Best first target.** |
| 3 | CBRE | CF 200 | html/next | 🟡 | Large SPA; find internal listings API. |
| 4 | JLL | Akamai 403 | hard | 🔴 | `invest.jll.com`; Akamai. |
| 5 | Cushman & Wakefield | none 200 | html | 🟢 | IIS, clean 200, redirects to `/properties-for-sale`. Discussed on call — good early win. |
| 6 | Colliers | CF challenge 403 | html(hard) | 🟡 | Needs headless to pass CF. |
| 7 | Newmark | CF challenge | **buildout+algolia** | 🟡 | Buildout + Algolia both present. |
| 8 | Marcus & Millichap | none 200 | html(LD-JSON) | 🟢 | Clean. LD-JSON per listing. |
| 9 | Ten-X | Akamai 403 | hard | 🔴 | Auction platform (CoStar-adjacent). |
| 10 | Matthews | Vercel/Next | next | 🟡 | Next.js on Vercel; look for `/_next/data` or API route. |

### Tier 2 — Other national brokerages
| # | Site | Anti-bot | Access | Diff | Notes |
|---|------|----------|--------|------|-------|
| 11 | Northmarq | CF 200 | html(LD-JSON) | 🟢 | |
| 12 | Avison Young | CF 200 | html | 🟡 | |
| 13 | NAI Global | (404 on /properties) | **buildout** | 🟢 | Real path `/investment-listings/`. Buildout. |
| 14 | SVN | none 200 | **buildout** | 🟢 | Buildout. |
| 15 | Lee & Associates | CF 200 | **buildout** | 🟢 | Buildout. |
| 16 | Transwestern | none 200 | html(LD-JSON) | 🟢 | IIS clean. |
| 17 | Eastdil Secured | none 200 | html(LD-JSON) | 🟡 | Redirects to home; find listings path. |
| 18 | Mid-America | CF 200 | **buildout** | 🟢 | Buildout. |
| 19 | Horvath & Tremblay | CF challenge | **algolia** | 🟡 | Algolia + Salesforce. Huge page (1.2MB). |
| 20 | SRS | CF challenge 404 | html(hard) | 🟡 | `/listings` 404'd; find real path. |
| 21 | SHOP Companies | CF challenge 404 | html(hard) | 🟡 | Path check needed. |

### Tier 3 — Marketplaces
| # | Site | Anti-bot | Access | Diff | Notes |
|---|------|----------|--------|------|-------|
| 22 | RCM1 | none 200 | html | 🟡 | RealCapitalMarkets; may need login for detail. |
| 23 | PropertyShark | CF challenge 403 | hard | 🟡 | **Yardi cluster.** |
| 24 | RealNex | none 200 | api/html | 🟡 | CRE platform; has its own marketplace API. |
| 25 | CommercialCafe | CF challenge 403 | hard | 🟡 | **Yardi cluster** (CommercialEdge API). |
| 26 | CommercialSearch | CF challenge 403 | hard | 🟡 | **Yardi cluster** (same backend as 25). |
| 27 | Showcase | Akamai 403 | hard | 🔴 | **CoStar cluster.** |
| 28 | CityFeet | Akamai 403 | hard | 🔴 | **CoStar cluster.** |
| 29 | Brevitas | CF 200 | api/buildout | 🟡 | CRE platform; Buildout signal. Some listings gated. |
| 30 | Moody's CRE | CF challenge | html(hard) | 🟡 | ex-CommercialExchange. |
| 31 | MyEListing | none 200 | html | 🟢 | Free marketplace, clean. |
| 32 | CIMLS | none 200 | html(LD-JSON) | 🟢 | |
| 33 | TotalCommercial | none 200 | html | 🟢 | Small/simple. |
| 34 | DealStream | none 200 | html(LD-JSON) | 🟢 | |
| 35 | CRECo.ai | none 200 | api | 🟡 | SPA; find API. |
| 36 | RealtyZapp | none 200 | html(LD-JSON) | 🟢 | IIS. |
| 37 | CommercialMLS/CBA | none 200 | html | 🟢 | Regional exchange, public search. |
| 38 | MNCAR | none 200 | html(CRE-platform) | 🟢 | MN/Upper-Midwest. |
| 39 | Miami REALTORS | CF challenge 403 | html(hard) | 🟡 | |
| 40 | SC Commercial MLS | none 200 | html(LD-JSON) | 🟢 | LiteSpeed/WordPress. |

### Tier 4 — Land & development
| # | Site | Anti-bot | Access | Diff | Notes |
|---|------|----------|--------|------|-------|
| 41 | LandSearch | CF challenge 403 | html(hard) | 🟡 | |
| 42 | LandWatch | Akamai 403 | hard | 🔴 | Land.com/CoStar family. |
| 43 | Land.com | (Akamai family) | hard | 🔴 | Same family as 42/44. |
| 44 | Land And Farm | Akamai 403 | hard | 🔴 | Land.com family. |
| 45 | LandHub | CF challenge 200 | **next** | 🟢 | `__NEXT_DATA__` present — easy structured pull. |
| 46 | CommercialFlip | CF challenge | html(LD-JSON) | 🟡 | |

### Tier 5 — Auctions, distressed & government
| # | Site | Anti-bot | Access | Diff | Notes |
|---|------|----------|--------|------|-------|
| 47 | RI Marketplace | none (S3) | api/html | 🟡 | Static shell on S3; data via API. |
| 48 | Hilco | CF challenge | **buildout** | 🟡 | Buildout signal. |
| 49 | Williams & Williams | none 200 | html(LD-JSON) | 🟢 | Big page; has LD-JSON. |
| 50 | Bid4Assets | none 200 | html | 🟢 | IIS clean. |
| 51 | GovDeals | Akamai 200 | api/html | 🟡 | Has JSON API for listings. |
| 52 | GSA Disposal | none 200 | **salesforce** | 🟡 | Salesforce Lightning + ArcGIS; SF Aura endpoints. |
| 53 | US Treasury | Akamai 200 | html | 🟢 | Static `.shtml` — trivial to parse. |
| 54 | IRS Auctions | Akamai 200 | html | 🟢 | Simple. |
| 55 | Auction.com | Imperva/Incapsula | hard | 🔴 | Blocked (212b). Mostly residential anyway. |
| 56 | Paramount Realty | CF challenge | html(LD-JSON) | 🟡 | WordPress. |
| 57 | Tranzon | none 200 | html | 🟢 | IIS. |
| 58 | LastBid | none 200 | html(LD-JSON) | 🟢 | |
| 59 | FRE | none 200 | html(LD-JSON) | 🟢 | |

### Tier 6 — Brokerage property-search portals
| # | Site | Anti-bot | Access | Diff | Notes |
|---|------|----------|--------|------|-------|
| 60 | Coldwell Banker Comm. | none 200 | html(CRE-platform) | 🟢 | |
| 61 | Century 21 Comm. | none 200 | html | 🟢 | Apache clean. |
| 62 | RE/MAX Commercial | none 200 | html(CRE-platform) | 🟢 | |
| 63 | eXp Commercial | CF 200 | **next+buildout** | 🟢 | `__NEXT_DATA__` + Buildout. Easy. |
| 64 | KW Commercial | CF challenge | html(CRE-platform) | 🟡 | |
| 65 | BHHS Commercial | CF challenge | **buildout** | 🟡 | Buildout behind CF. |
| 66 | Berkadia | CF challenge | **salesforce** | 🟡 | SF-backed; mostly multifamily. |
| 67 | Kidder Mathews | CF 200 | html(LD-JSON) | 🟢 | |
| 68 | DAUM | CF 200 | **buildout** | 🟢 | Buildout. |
| 69 | Ariel Property Advisors | none 200 | html | 🟢 | NYC. |
| 70 | RIPCO | none 200 | **algolia** | 🟢 | Algolia + WordPress. |
| 71 | Franklin Street | CF 200 | **buildout** | 🟢 | Buildout. |
| 72 | Foundry Commercial | CF 200 | html(LD-JSON) | 🟢 | |
| 73 | TSCG | none 200 | **buildout** | 🟢 | ✅ Buildout endpoint proven here. |
| 74 | KLNB | none 200 | **propertycapsule** | 🟡 | propertycapsule.com; huge payload (12MB). |

### Tier 7 — Retail & net-lease specialists (highest retail relevance)
| # | Site | Anti-bot | Access | Diff | Notes |
|---|------|----------|--------|------|-------|
| 75 | Sands Investment Group | CF challenge | html(LD-JSON)/salesforce | 🟡 | Pure net-lease retail — high value. |
| 76 | B+E / Trade Net Lease | Sucuri 307 | html | 🟢 | Net-lease. |
| 77 | Boulder Group | Sucuri 200 | html | 🟢 | NNN retail; small clean site. |
| 78 | Kase Group | none 200 | html(WordPress) | 🟢 | Net-lease inventory. |
| 79 | NNN.market | none 200 | api/html | 🟢 | IIS; direct net-lease marketplace. |
| 80 | Fortis Net Lease | none 200 | **buildout** | 🟢 | Buildout. |
| 81 | HighStreet Net Lease | CF 200 | html | 🟢 | |
| 82 | NNN Pro | Vercel/Next | **next** | 🟢 | Next.js; large national NNN inventory. |
| 83 | Hanley Investment Group | CF challenge | html(LD-JSON) | 🟡 | Retail specialist. |
| 84 | Disney Investment Group | none 200 | html | 🟢 | Small net-lease shop. |
| 85 | Silber Properties | none 200 | html(LD-JSON) | 🟢 | Flywheel/WordPress. |

---

## Proven API details (for the build phase)

### Crexi ✅
- **Sale:** `POST https://api.crexi.com/assets/search`
  - Body: `{"types":["Retail"],"includeUnpriced":true,"pageSize":60,"offset":0}` (paginate via `offset`)
  - Response: `{"data":[…],"totalCount":55918}`
  - Item fields: `id, name, description, urlSlug, brokerageName, brokerTeamLogoUrl, activatedOn, updatedOn, squareFootage, askingPrice, types[], status ("On-Market"), locations[{address,city,county,state{code,name},zip,latitude,longitude,fullAddress}], isNew, isInOpportunityZone, hasOM, numberOfImages`.
  - `status:"On-Market"` + `updatedOn`/`activatedOn` → perfect for new-vs-delisted diffing.
  - **Lease endpoint:** not `/assets/search` (that's sale-only, has `askingPrice`). Separate endpoint under api.crexi.com — discover exact route from the lease-page JS bundle at build time.
  - Front is Cloudflare-challenged, but **api.crexi.com is not** — direct JSON calls work.

### Buildout ✅ (reusable for ~12 sites)
- **Endpoint:** `GET https://buildout.com/plugins/{hash}/inventory?pluginId=0&page={N}`
  - Headers: `X-Requested-With: XMLHttpRequest`. Returns JSON `{inventory:[…], …pagination…}`.
  - Item fields observed: `address, address_one_line, address_two_line[], address_three_line[], also_for_sale_or_lease, …` (full field list to be captured per adapter; includes property type + sale/lease flags).
  - `{hash}` per site = the token in the site's Buildout iframe `src` (e.g. TSCG = `ba668fad90e207a3d5cfc0037b6206bf0f5d32da`). Extract each site's hash once.
  - Note: endpoint showed light rate-limiting / occasional HTML shell on rapid repeat calls → pace requests, set proper referer, retry.

---

## Retail taxonomy plan

Each site names retail sub-types differently (unanchored strip center, street retail, general retail, anchored strip center, single-tenant NNN, etc.). Approach:
- One `type_map.json` per site: pull that site's property-type list from its filter UI/API, decide which entries are "retail," store the mapping. (This is the "paste the type list into Claude, ask which are retail" step from the call.)
- Canonical output field `property_type = retail` plus `property_subtype` preserving the site's original label.
- Net-lease specialists (Tier 7) are effectively all-retail already → minimal mapping.

---

## Recommended build order

1. **Crexi** — proven API, 55.9k retail, sale+lease. Establishes the pipeline (fetch → normalize → CSV → daily diff/delist). ⭐
2. **Buildout adapter** — one adapter, then register the ~12 hashes → ~12 sites for the price of one.
3. **Easy long-tail** (clean HTML + LD-JSON): Cushman, Marcus & Millichap, Northmarq, Transwestern, the net-lease specialists (Boulder, Kase, Fortis, NNN Pro, NNN.market, Disney IG, Silber), MyEListing, CIMLS, DealStream, RealtyZapp, Century21, RE/MAX, Coldwell Banker, Tranzon, LastBid, FRE, Bid4Assets, Williams, Treasury, IRS.
4. **Algolia trio** (Newmark, Horvath, RIPCO) — extract keys, query index directly.
5. **Next trio** (Matthews, NNN Pro, LandHub, eXp) — pull `__NEXT_DATA__` / `_next/data`.
6. **Yardi cluster** (CommercialCafe, CommercialSearch, PropertyShark) — one CF-passing headless + shared CommercialEdge API.
7. **Hard tier last** (CoStar: LoopNet/Showcase/CityFeet; JLL; Ten-X; Land.com; Auction.com) — needs residential proxies + full headless + TLS/JA3 spoofing. Highest effort, but LoopNet is the biggest prize.

---

## Common data schema (target, for CSV → Postgres later)

`source_site, source_listing_id, source_url, transaction_type (sale|lease), property_type, property_subtype, name, address, city, county, state, zip, lat, lng, price_or_rent, price_basis, sqft, lot_size, cap_rate, year_built, tenancy, broker_name, brokerage, listed_on, updated_on, status, first_seen (ours), last_seen (ours), delisted_on (ours), raw_json`

Two CSVs to start: `listings_for_sale.csv`, `listings_for_lease.csv` (+ per-site raw dumps). `first_seen`/`last_seen`/`delisted_on` are computed by our daily diff, not the source.
