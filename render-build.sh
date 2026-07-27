#!/usr/bin/env bash
# Render build step: install deps and make sure both data files are present.
#
# Two separate artefacts, both hosted as GitHub Release assets and both fetched here:
#   webapp/listings.db  (DB_URL)          -- the card/map index server.py always needs
#   data/details.db     (DETAILS_DB_URL)  -- per-property detail; OPTIONAL, enriches
#                                             the drawer but the app runs fine without it
#                                             (listings just show "card data only").
# details.db here is a *slimmed* deploy copy (scripts/build_deploy_details.py drops the
# raw_json column and any non-'ok' rows) -- ~590MB instead of the 2.2GB local original.
set -euo pipefail

pip install -r requirements.txt

DB="webapp/listings.db"
DETAILS="data/details.db"

if [ -f "$DB" ]; then
  echo "listings.db already in repo ($(du -h "$DB" | cut -f1))"
elif [ -n "${DB_URL:-}" ]; then
  echo "downloading listings index..."
  curl -fSL "$DB_URL" -o "$DB"
  echo "downloaded $(du -h "$DB" | cut -f1)"
else
  # Fail loudly rather than booting a viewer with no data -- a site that loads and shows
  # "0 listings" is harder to diagnose than a build that says why.
  echo "ERROR: no $DB and DB_URL is not set." >&2
  echo "Set DB_URL in the Render dashboard to a direct-download link for listings.db." >&2
  exit 1
fi

mkdir -p data
if [ -f "$DETAILS" ]; then
  echo "details.db already present ($(du -h "$DETAILS" | cut -f1))"
elif [ -n "${DETAILS_DB_URL:-}" ]; then
  echo "downloading property-detail index..."
  curl -fSL "$DETAILS_DB_URL" -o "$DETAILS"
  echo "downloaded $(du -h "$DETAILS" | cut -f1)"
else
  # Not fatal: server.py's ATTACH is a no-op if this file is absent, and the drawer
  # falls back to "card data only". Enrichment is a nice-to-have, not a hard dependency.
  echo "NOTE: DETAILS_DB_URL not set -- deploying without per-property enrichment."
fi

python - <<'PY'
import sqlite3
n = sqlite3.connect("webapp/listings.db").execute("SELECT COUNT(*) FROM listings").fetchone()[0]
print(f"listings index OK: {n:,} rows")
import os
if os.path.exists("data/details.db"):
    m = sqlite3.connect("data/details.db").execute("SELECT COUNT(*) FROM details").fetchone()[0]
    print(f"details index OK: {m:,} rows")
PY
