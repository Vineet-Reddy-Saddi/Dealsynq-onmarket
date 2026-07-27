#!/usr/bin/env bash
# Render build step: install deps and make sure the listings index is present.
set -euo pipefail

pip install -r requirements.txt

DB="webapp/listings.db"

if [ -f "$DB" ]; then
  echo "listings.db already in repo ($(du -h "$DB" | cut -f1))"
elif [ -n "${DB_URL:-}" ]; then
  echo "downloading listings index..."
  curl -fSL "$DB_URL" -o "$DB"
  echo "downloaded $(du -h "$DB" | cut -f1)"
else
  # Fail loudly rather than booting a viewer with no data — a site that loads and shows
  # "0 listings" is harder to diagnose than a build that says why.
  echo "ERROR: no $DB and DB_URL is not set." >&2
  echo "Set DB_URL in the Render dashboard to a direct-download link for listings.db." >&2
  exit 1
fi

python - <<'PY'
import sqlite3
n = sqlite3.connect("webapp/listings.db").execute("SELECT COUNT(*) FROM listings").fetchone()[0]
print(f"index OK: {n:,} rows")
PY
