"""
ONE-TIME (or occasional manual) backfill script.

Pulls up to 12 quarterly 13F-HR filings per fund (~3 years of history) and
stores them all in Supabase, using the exact same parsing/enrichment logic
as the daily fetch_13f.py script.

This is NOT meant to run on a schedule -- run it once to build up history,
then the regular daily fetch_13f.py workflow naturally extends that history
by one more quarter every time a fund files something new. There's no need
to ever re-run this unless you want to push the history window back further
than 3 years.

Requires the same environment variables as fetch_13f.py (SUPABASE_URL,
SUPABASE_SERVICE_KEY, SEC_USER_AGENT) -- run from the same directory so the
import below finds fetch_13f.py.

Runtime note: with 10 funds x up to 12 quarters x price-lookup pacing, this
can take 30-40+ minutes. That's expected for a one-time job, not a hang.
"""

import time
from fetch_13f import FUNDS, recent_13f_filings, process_filing

QUARTERS_OF_HISTORY = 12  # ~3 years


def main():
    figi_cache = {}

    for fund in FUNDS:
        try:
            filings = recent_13f_filings(fund["cik"], n=QUARTERS_OF_HISTORY)
            if not filings:
                print(f"[skip] no 13F-HR found for {fund['name']}")
                continue

            print(f"[backfill] {fund['name']}: found {len(filings)} filings to process")

            # Oldest first, so each quarter's "prior" comparison is already
            # stored by the time we process the next one -- same pattern as
            # the daily script, just over a longer window.
            for filing in reversed(filings):
                process_filing(fund, filing, figi_cache)
                time.sleep(0.3)
        except Exception as e:
            print(f"[error] {fund['name']}: {e}")


if __name__ == "__main__":
    main()
