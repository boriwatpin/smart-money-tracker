# Smart Money Tracker — 13F hedge fund tracker

**Live dashboard:** https://boriwatpin.github.io/smart-money-tracker/public/index.html

Tracks the latest 13F-HR filings for 10 hedge funds (Berkshire Hathaway,
Bridgewater, Renaissance Technologies, Citadel, Pershing Square, Tiger
Global, Third Point, Soros Fund Management, Point72, Millennium
Management) directly from SEC EDGAR, on a schedule, with no manual
copy-pasting.

## Features

- **Live portfolio values** per fund, updated daily
- **NEW position badges** (green) — a CUSIP the fund didn't hold last quarter
- **INCREASED position badges** (blue) — an existing position that grew 15%+ in share count
- **Price estimates** for both NEW and INCREASED positions — average daily
  closing price during the quarter vs. today's price, pulled from Yahoo
  Finance via `yfinance` (a rough approximation, not a real cost basis —
  see Known limitations)
- **3-year position history table** per fund, color-coded by quarter-over-quarter change
- **Countdown clock** to the next 13F filing deadline

## How it works

```
GitHub Actions (cron, daily)
        │
        ▼
scripts/fetch_13f.py  ──►  SEC EDGAR (data.sec.gov, sec.gov)
        │                   OpenFIGI (CUSIP → ticker)
        │                   Yahoo Finance via yfinance (price history)
        ▼
Supabase table `fund_snapshots`  (service_role key writes, RLS blocks anon writes)
        │
        ▼
public/index.html  ──►  reads with the public anon key, read-only
```

The daily script is idempotent: it upserts on `(cik, period_end)`, so
running it every day just re-confirms "nothing new" until a fund actually
files. You don't need to change the schedule around deadlines unless you
want same-day freshness.

A separate **one-time backfill script** (`scripts/backfill_13f.py`) pulls
~3 years of history (12 quarters) per fund. Run it once manually via the
"Backfill 13F history" GitHub Action; the daily script then naturally
extends that history by one more quarter every time a fund files something
new — there's no need to ever re-run the backfill unless you want to push
the history window back further.

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
   source to the `public/` folder (or root, with a redirect — see note below).
9. Optional: run **Actions → "Backfill 13F history" → Run workflow** once
   to populate ~3 years of history for the position history table.
   Takes 30-40+ minutes for 10 funds — that's expected, not a hang.

**Note on the live URL:** if GitHub Pages is set to deploy from the repo
root rather than `/public`, the root URL (`.../smart-money-tracker/`) will
just show this README rendered as a webpage (Jekyll's default behavior),
not the dashboard. The actual app is always at
`.../smart-money-tracker/public/index.html` regardless of Pages source setting.

## Adjusting the schedule

The cron in `.github/workflows/refresh.yml` runs daily at 13:00 UTC. You
can tighten this — e.g. run every 2 hours only in the week of a deadline —
but daily is enough for a filing window that spans weeks and funds that
usually file once per quarter. Since this is a public repo, GitHub Actions
minutes are unlimited and free regardless of frequency chosen.

## Extending it

- **Add more funds:** add an entry to the `FUNDS` list in
  `scripts/fetch_13f.py` with the fund's SEC CIK number (look it up at
  sec.gov/cgi-bin/browse-edgar). After adding, run "Backfill 13F history"
  again to give the new fund the same 3-year history as the rest — it will
  only backfill funds/quarters not already stored, so it's safe to re-run.
- **Track more than the top 25 holdings per fund:** change `TOP_N_HOLDINGS`
  in `scripts/fetch_13f.py`.
- **Extend history further back than 3 years:** bump `QUARTERS_OF_HISTORY`
  in `scripts/backfill_13f.py` and re-run that workflow.
- **Change the "significant increase" threshold** (currently 15% more
  shares): edit the threshold in `process_filing()` in `scripts/fetch_13f.py`.

## Known limitations

- 13F filings are inherently lagged (up to 45 days after quarter-end) and
  exclude cash, short positions, and most non-U.S.-listed holdings.
- "Value change quarter-over-quarter" blends real trading activity with
  market price moves — it is not a true realized gain/loss figure, which
  isn't derivable from 13F data alone (no cost basis is disclosed).
- Price estimates for NEW/INCREASED positions use the *average* daily
  closing price across the whole quarter as a stand-in for entry price,
  since SEC filings don't disclose the exact trade date. This is a rough
  approximation, not a real cost basis — treat it as directionally useful,
  not precise.
- The "position history" table only tracks CUSIPs that made a fund's top 25
  holdings in a given quarter — a "—" in the table means the holding wasn't
  in the top 25 that quarter, not necessarily that the fund didn't hold it
  at all.
- CUSIP → ticker mapping (via OpenFIGI) and price lookups (via Yahoo
  Finance) can occasionally fail for thinly-traded securities, bonds, or
  foreign issuers — these show as "price estimate unavailable" rather than
  blocking the rest of the data.
- The countdown clock adjusts for weekend deadlines but does not account
  for federal holidays falling on a Friday/Monday around the 14th/15th —
  edge case, but worth a manual double check right before a deadline.
