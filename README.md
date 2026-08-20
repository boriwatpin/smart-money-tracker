# Smart Money Tracker — 13F hedge fund tracker

Tracks the latest 13F-HR filings for 8 hedge funds (Berkshire Hathaway,
Bridgewater, Renaissance Technologies, Citadel, Pershing Square, Tiger
Global, Third Point, Soros Fund Management) directly from SEC EDGAR, on a
schedule, with no manual copy-pasting.

## How it works

```
GitHub Actions (cron, daily)
        │
        ▼
scripts/fetch_13f.py  ──►  SEC EDGAR (data.sec.gov, sec.gov)
        │
        ▼
Supabase table `fund_snapshots`  (service_role key writes, RLS blocks anon writes)
        │
        ▼
public/index.html  ──►  reads with the public anon key, read-only
```

The script is idempotent: it upserts on `(cik, period_end)`, so running it
every day just re-confirms "nothing new" until a fund actually files. You
don't need to change the schedule around deadlines unless you want
same-day freshness.

## Setup (about 15 minutes)

1. **Create a Supabase project** at supabase.com (free tier is enough).
2. In the Supabase SQL editor, run everything in `schema.sql`.
3. In Supabase → Project Settings → API, copy:
   - the **Project URL**
   - the **anon / public key**
   - the **service_role key** (keep this one secret — never put it in the frontend)
4. **Push this repo to GitHub.**
5. In your GitHub repo → Settings → Secrets and variables → Actions, add:
   - `SUPABASE_URL` — your project URL
   - `SUPABASE_SERVICE_KEY` — the service_role key
   - `SEC_USER_AGENT` — SEC asks every automated requester to identify
     itself, e.g. `"Jane Doe jane@example.com"`
6. Go to the **Actions** tab in GitHub → "Refresh 13F data" → **Run workflow**
   to trigger it manually the first time (don't wait for the daily cron).
7. Edit `public/index.html` and fill in `SUPABASE_URL` and
   `SUPABASE_ANON_KEY` (the anon key from step 3, not the service key).
8. Host `public/index.html` anywhere static — GitHub Pages, Vercel,
   Netlify, or even just opening the file locally. GitHub Pages is the
   simplest option if it's already a GitHub repo: Settings → Pages → set
   source to the `public/` folder.

## Adjusting the schedule

The cron in `.github/workflows/refresh.yml` runs daily at 13:00 UTC. You
can tighten this — e.g. run every 2 hours only in the week of a deadline —
but daily is enough for a filing window that spans weeks and funds that
usually file once per quarter.

## Extending it

- Add more funds: add an entry to the `FUNDS` list in `scripts/fetch_13f.py`
  with the fund's SEC CIK number (look it up at sec.gov/cgi-bin/browse-edgar).
- Track more than the top 10 holdings per fund: change `TOP_N_HOLDINGS`.
- Add a history chart: `fund_snapshots` keeps every past quarter's row per
  fund (never overwritten, only added to), so a chart of portfolio value
  over time is just a `select * where cik = ... order by period_end` away.

## Known limitations

- 13F filings are inherently lagged (up to 45 days after quarter-end) and
  exclude cash, short positions, and most non-U.S.-listed holdings.
- "Value change quarter-over-quarter" blends real trading activity with
  market price moves — it is not a true realized gain/loss figure, which
  isn't derivable from 13F data alone (no cost basis is disclosed).
- The countdown clock adjusts for weekend deadlines but does not account
  for federal holidays falling on a Friday/Monday around the 14th/15th —
  edge case, but worth a manual double check right before a deadline.
