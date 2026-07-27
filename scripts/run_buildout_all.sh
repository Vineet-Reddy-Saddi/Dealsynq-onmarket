#!/usr/bin/env bash
# Re-run the whole Buildout cluster, both transaction types, sequentially.
# Sites hit buildout.com from one IP (rate-limited), so we pace between runs.
cd "$(dirname "$0")/.."
SITES="tscg midamerica franklinst svn fortis lee-associates naiglobal bhhs"
for tt in sale lease; do
  for s in $SITES; do
    echo "===== $s ($tt) ====="
    python run.py --site "$s" --type "$tt" 2>&1 | grep -vE "unmapped subtypes"
    sleep 5
  done
done
echo "BUILDOUT CLUSTER DONE"
