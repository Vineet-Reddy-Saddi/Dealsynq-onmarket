"""Canonical "is this retail?" classifier for site subtype labels.

Every source names retail differently. Some say "Retail"; others give only the specific
use ("Fast Food", "Bank", "Freestanding", "Storefront") with no mention of the word
retail at all — so a substring test on "retail" silently drops most real retail rows.
This module holds one allowlist plus a matcher used by adapters whose source mixes
retail and non-retail results in the same feed.

``is_retail`` accepts a label that may be a comma-separated list of uses (CityFeet emits
"Office, Retail, Industrial"); the row counts as retail if *any* use is retail.

Note: ``adapters/buildout.py`` keeps its own equivalent list. It is deliberately not
switched over — its data is already collected and stable, and changing its filter would
churn the lifecycle columns (rows flapping between active and delisted) for no new data.
"""
from __future__ import annotations

# Specific retail uses / formats, lowercase. Matched exactly against one label.
RETAIL_SUBTYPES = {
    # generic
    "retail", "general retail", "street retail", "storefront", "freestanding",
    "free standing building", "freestanding building", "commercial",
    # centers
    "strip center", "anchored strip center", "unanchored strip center",
    "neighborhood center", "community center", "power center", "regional center",
    "lifestyle center", "shopping center", "outlet center", "regional mall",
    "specialty center", "anchor space at regional mall", "mall",
    # mixed retail formats
    "storefront retail office", "storefront retail residential",
    "storefront retail/office", "storefront retail/residential",
    "retail office", "mixed use", "mixed-use", "retail pad", "retail-pad", "pad site",
    # food & beverage
    "restaurant", "fast food", "bar", "coffee", "qsr", "cafe",
    "restaurant permitted", "restaurant permitted use", "restaurant former space",
    # single-tenant / net lease
    "single tenant", "single-tenant", "net lease", "nnn", "big box",
    "department store", "showroom",
    # specific retail tenants / uses
    "bank", "drugstore", "pharmacy", "convenience store", "c-store",
    "gas station", "service station", "truck stop",
    "car wash", "auto", "automotive", "auto repair", "auto service", "auto dealership",
    "vehicle related", "tire", "day care", "daycare", "day care center",
    "daycare center", "child care", "veterinarian", "veterinarian kennel",
    "veterinarian/kennel", "grocery", "supermarket", "health club", "fitness",
    "gym", "salon", "spa", "garden center", "liquor store", "funeral home",
    "movie theater", "theater", "bowling", "self serve storage retail",
}

# Substrings that make a label retail regardless of the rest ("Retail Condo", "Shops at…").
RETAIL_TOKENS = ("retail", "restaurant", "shop", "mall", "storefront")

# Retail uses whose labels vary in spelling or word order across sites, so an exact
# match on the allowlist misses them: "Carwash" vs "Car Wash", "Movie Theatre" vs
# "Movie Theater", "Bowling Alley" vs "Bowling". Checked as substrings, but only after
# the NON_RETAIL denylist, so "Food Processing" is never caught by a food-ish term.
RETAIL_SUBSTRINGS = (
    "car wash", "carwash", "bowling", "theater", "theatre", "cinema",
    "drive thru", "drive-thru", "convenience", "pharmac", "drugstore",
    "grocer", "supermarket", "salon", "barber", "day care", "daycare",
    "child care", "veterinar", "kennel", "funeral", "garden center",
    "liquor", "fitness", "health club", "gas station", "service station",
    "auto repair", "auto service", "tire", "dealership",
)

# Labels that are explicitly NOT retail. Only consulted for whole-label matches, so a
# mixed label like "Office, Retail" still counts as retail via the allowlist above.
NON_RETAIL = {
    "office", "office building", "medical", "medical office", "serviced offices",
    "office residential", "loft creative space", "loft/creative space", "creative/loft",
    "industrial", "warehouse", "warehouse/distribution", "manufacturing", "flex",
    "flex space", "distribution", "truck terminal", "cold storage",
    "apartments", "multifamily", "multi-family", "residential", "land",
    "self storage", "self-storage", "storage", "hotel", "motel", "hospitality",
    "religious facility", "church", "school", "data center", "parking",
    "mobile home park", "rv park", "marina", "golf course", "agricultural",
    "senior living", "assisted living", "skilled nursing", "other", "service",
}


def is_retail(label: str | None) -> bool:
    """True if any use in ``label`` is a retail use.

    ``label`` may be a single use ("Fast Food") or a comma list ("Office, Retail").
    Unknown labels return False so junk never silently enters the retail dataset;
    adapters should log unmapped labels so the list can be extended deliberately.
    """
    if not label:
        return False
    for part in str(label).split(","):
        use = part.strip().lower()
        if not use:
            continue
        if use in RETAIL_SUBTYPES:
            return True
        if use in NON_RETAIL:
            continue
        if any(tok in use for tok in RETAIL_TOKENS):
            return True
        if any(sub in use for sub in RETAIL_SUBSTRINGS):
            return True
    return False


def unmapped(label: str | None) -> list[str]:
    """Uses in ``label`` that are in neither list — candidates to classify."""
    out = []
    for part in str(label or "").split(","):
        use = part.strip().lower()
        if not use or use in RETAIL_SUBTYPES or use in NON_RETAIL:
            continue
        if not any(tok in use for tok in RETAIL_TOKENS) and \
           not any(sub in use for sub in RETAIL_SUBSTRINGS):
            out.append(part.strip())
    return out
