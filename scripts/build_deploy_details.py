#!/usr/bin/env python
"""Build a slimmed, deploy-only copy of data/details.db.

The live server's ``_enriched()`` query only ever selects ``status='ok'`` rows and then
immediately discards ``raw_json`` before sending anything to the browser (it's kept
locally so enrichment can be reprocessed without a re-fetch, but a deployed server never
needs it). Since raw_json is ~69% of the file, shipping the full 2.2 GB database to a
free-tier host was unnecessary weight — this script drops it plus the gated/notfound/
error rows and writes ``webapp/details_deploy.db``, the file that actually gets
uploaded as a release asset for Render.

    python scripts/build_deploy_details.py
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "details.db"
DST = ROOT / "webapp" / "details_deploy.db"


def build() -> None:
    if not SRC.exists():
        print(f"no {SRC} -- nothing to slim")
        return
    if DST.exists():
        DST.unlink()

    src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    cols = [r[1] for r in src.execute("PRAGMA table_info(details)") if r[1] != "raw_json"]

    dst = sqlite3.connect(DST)
    dst.execute("PRAGMA journal_mode=OFF")
    dst.execute("PRAGMA synchronous=OFF")
    dst.execute(f"CREATE TABLE details ({', '.join(cols)})")

    sel = ", ".join(cols)
    batch = []
    n = 0
    for row in src.execute(f"SELECT {sel} FROM details WHERE status='ok'"):
        batch.append(row)
        n += 1
        if len(batch) >= 5000:
            dst.executemany(f"INSERT INTO details VALUES ({', '.join('?' * len(cols))})", batch)
            batch = []
    if batch:
        dst.executemany(f"INSERT INTO details VALUES ({', '.join('?' * len(cols))})", batch)

    dst.execute("CREATE INDEX ix_srcid ON details(source_site, source_listing_id)")
    dst.commit()
    dst.close()
    src.close()

    print(f"wrote {n:,} rows -> {DST.relative_to(ROOT)}")
    print(f"  full details.db: {SRC.stat().st_size/1e9:.2f} GB")
    print(f"  deploy copy:      {DST.stat().st_size/1e9:.2f} GB "
          f"({100*DST.stat().st_size/SRC.stat().st_size:.0f}% of full size)")


if __name__ == "__main__":
    t0 = time.time()
    build()
    print(f"({time.time()-t0:.1f}s)")
