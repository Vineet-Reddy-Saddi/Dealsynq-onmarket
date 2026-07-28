#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
for site in lee-associates naiglobal bhhs tscg midamerica franklinst svn fortis; do
  for ttype in sale lease; do
    echo "=== $site / $ttype ==="
    python scripts/enrich.py --site "$site" --type "$ttype" --workers 1
  done
done
echo "ALL BUILDOUT ENRICHMENT DONE"
