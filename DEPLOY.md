# Deploying the viewer to Render

Vercel is static-only; this app needs a live Python process for the SQLite-backed API,
so Render's free web service is the right fit (same as the previous site).

## One-time setup

1. **Push the repo to GitHub.** Do *not* commit the data files — `webapp/listings.db`
   (~180 MB), `data/details.db` (~2 GB) and the CSVs are all too large for git.
   `.gitignore` already excludes them.

2. **Host the listings index somewhere with a direct download link.** Easiest is a
   GitHub *Release asset* (up to 2 GB, free):

   ```
   gh release create data-v1 webapp/listings.db --title "listings index"
   ```

   Copy the asset's download URL.

3. **Render → New → Blueprint → select the repo.** It reads `render.yaml`.

4. **Set `DB_URL`** in the Render dashboard to that download URL. The build script
   fetches the index and fails loudly if it is missing — better than a site that loads
   and shows "0 listings".

## Refreshing the data

Re-run the scrape and index locally, upload the new `listings.db`, then trigger a
Render redeploy (or "Clear build cache & deploy").

```
python run.py --all --type sale
python run.py --all --type lease
python scripts/enrich.py --site crexi --type sale --workers 12
python webapp/build_index.py
gh release upload data-v1 webapp/listings.db --clobber
```

## Notes

* Free instances sleep after ~15 min idle and take ~30 s to wake — fine for a demo,
  worth upgrading before showing it to a client live.
* The service is read-only: it only serves the prebuilt index.
* `render-build.sh` verifies the row count at build time so a bad index fails the
  deploy instead of silently serving nothing.
