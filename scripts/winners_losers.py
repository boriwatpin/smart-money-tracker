"""
Winners & Losers: estimated cost basis vs. today's price for every position
in each fund's latest stored 13F snapshot (top 25 holdings per fund).

Runs as its own GitHub Actions workflow (.github/workflows/winners_losers.yml)
after the US market close. It only READS fund_snapshots -- it never touches
the 13F fetch, locked cohorts, or any existing table except appending a row
to ai_summaries (subject = "winners_losers").

How the cost basis is estimated (13F filings never disclose entry prices):
  - Walk each fund's stored quarterly history oldest -> newest.
  - Every quarter a position's share count goes UP, the added shares are
    priced at that quarter's average daily close (Yahoo, split-adjusted).
  - Share count going DOWN reduces the position but keeps the average cost.
  - Share counts are split-adjusted too. Without this, a 10:1 split looks
    like the fund bought 9x more shares, at the post-split price.
  - A position missing from the top 25 for 2+ consecutive quarters is
    treated as a fresh position if it comes back.
  - Positions already held in the very first stored quarter are flagged
    opened_in_window = false: their real entry was before our history
    starts, so their "cost" is only the price at the start of tracking.

Writes:
  ticker_map    -- CUSIP -> ticker cache (OpenFIGI), so each CUSIP is only
                   looked up once instead of on every run
  position_pnl  -- one row per (fund, CUSIP) currently held
  ai_summaries  -- one new row per run, subject "winners_losers"

Required env vars: SUPABASE_URL, SUPABASE_SERVICE_KEY, SEC_USER_AGENT
(imported module reads it), and optionally GEMINI_API_KEY.
"""

import re
import time
import json
import statistics
from datetime import date, datetime, timedelta, timezone

import requests
import yfinance as yf

from fetch_13f import (
    FUNDS,
    SUPABASE_URL,
    SUPABASE_KEY,
    quarter_start_iso,
    call_gemini,
    save_ai_summary,
)

# When a fund's 13F filer entity changes, list the OLD CIK(s) here so the
# cost basis history carries across the change instead of every position
# looking brand-new at the first filing under the new CIK.
CIK_ALIASES = {
    "0002026053": ["0001336528"],  # Pershing Square Inc. (was Pershing Square Capital Management)
}

EQUITY_TICKER_RE = re.compile(r"[A-Za-z.\-]{1,6}")
GAP_RESET_QUARTERS = 2       # missing this many stored quarters in a row -> treat a return as a new position
UNRESOLVED_RETRY_DAYS = 30   # retry OpenFIGI for CUSIPs that had no ticker after this long
OPENFIGI_BATCH = 10          # anonymous OpenFIGI tier allows 10 jobs per request
OPENFIGI_PAUSE = 2.6         # ~25 requests/minute anonymous limit

HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
WRITE_HEADERS = {**HEADERS, "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}


# ---------------------------------------------------------------- Supabase

def sb_get_all(path):
    """GET with pagination -- PostgREST caps responses (often 1000 rows)."""
    rows, offset, page = [], 0, 500
    while True:
        sep = "&" if "?" in path else "?"
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}{sep}limit={page}&offset={offset}", headers=HEADERS, timeout=60)
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page:
            return rows
        offset += page


def sb_upsert(table, rows, conflict):
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={conflict}",
            headers=WRITE_HEADERS, data=json.dumps(chunk), timeout=60,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase upsert into {table} failed: {r.status_code} {r.text}")


def sb_delete_older_than(table, iso_ts):
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}?updated_at=lt.{iso_ts}", headers=HEADERS, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase delete on {table} failed: {r.status_code} {r.text}")


# ---------------------------------------------------------------- Snapshots

def load_histories():
    """Return {active_cik: {"fund_name": str, "snapshots": [oldest..newest]}}.

    Each snapshot's holdings are aggregated by CUSIP. The 13F parser stores
    one row per infotable entry, so a stock and an option on the same stock
    can share a CUSIP -- summing keeps quarter-to-quarter deltas consistent.
    """
    out = {}
    for fund in FUNDS:
        ciks = [fund["cik"]] + CIK_ALIASES.get(fund["cik"], [])
        cik_filter = ",".join(ciks)
        rows = sb_get_all(
            f"fund_snapshots?select=cik,period_end,top_holdings&cik=in.({cik_filter})&order=period_end.asc"
        )
        by_period = {}
        for row in rows:
            agg = {}
            for h in row.get("top_holdings") or []:
                cusip = h.get("cusip")
                if not cusip:
                    continue
                a = agg.setdefault(cusip, {"issuer": h.get("issuer"), "shares": 0, "value": 0, "ticker_hint": None})
                a["shares"] += h.get("shares") or 0
                a["value"] += h.get("value") or 0
                pe = h.get("price_estimate") or {}
                if pe.get("ticker"):
                    a["ticker_hint"] = pe["ticker"]
            # If an old and new CIK both filed the same period, keep the newer entity's data.
            if row["period_end"] not in by_period or row["cik"] == fund["cik"]:
                by_period[row["period_end"]] = agg
        snaps = [{"period_end": p, "holdings": by_period[p]} for p in sorted(by_period)]
        if snaps:
            out[fund["cik"]] = {"fund_name": fund["name"], "snapshots": snaps}
    return out


# ---------------------------------------------------------------- Tickers

def to_yahoo(ticker):
    return ticker.replace("/", "-").replace(".", "-")


def display_ticker(ticker):
    return ticker.replace("/", ".")


def is_equity_ticker(ticker):
    return bool(ticker) and bool(EQUITY_TICKER_RE.fullmatch(display_ticker(ticker)))


def openfigi_batch(cusips):
    """Look up up to 10 CUSIPs in one request. Returns {cusip: ticker_or_None}."""
    jobs = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in cusips]
    for attempt in range(4):
        try:
            r = requests.post("https://api.openfigi.com/v3/mapping", json=jobs,
                              headers={"Content-Type": "application/json"}, timeout=20)
            if r.status_code == 429:
                wait = 8 * (attempt + 1)
                print(f"[figi] rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            result = {}
            for cusip, item in zip(cusips, r.json()):
                data = item.get("data") or []
                result[cusip] = data[0].get("ticker") if data else None
            return result
        except Exception as e:
            print(f"[figi] batch failed ({e}), attempt {attempt + 1}/4")
            time.sleep(5)
    return {}  # leave unknown rather than caching a false "no ticker"


def resolve_tickers(needed):
    """needed: {cusip: ticker_hint_or_None}. Returns {cusip: ticker_or_None}."""
    cached = {r["cusip"]: r for r in sb_get_all("ticker_map?select=cusip,ticker,resolved_at")}
    now = datetime.now(timezone.utc)
    result, new_rows, to_lookup = {}, [], []

    for cusip, hint in needed.items():
        row = cached.get(cusip)
        if row and row.get("ticker"):
            result[cusip] = row["ticker"]
        elif hint:
            # fetch_13f.py already resolved this one for a new/increased position
            result[cusip] = hint
            new_rows.append({"cusip": cusip, "ticker": hint, "resolved_at": now.isoformat()})
        elif row and datetime.fromisoformat(row["resolved_at"].replace("Z", "+00:00")) > now - timedelta(days=UNRESOLVED_RETRY_DAYS):
            result[cusip] = None  # recently tried, OpenFIGI had nothing
        else:
            to_lookup.append(cusip)

    print(f"[figi] {len(needed)} CUSIPs needed, {len(to_lookup)} need an OpenFIGI lookup")
    for i in range(0, len(to_lookup), OPENFIGI_BATCH):
        batch = to_lookup[i:i + OPENFIGI_BATCH]
        found = openfigi_batch(batch)
        for cusip in batch:
            if cusip in found:
                result[cusip] = found[cusip]
                new_rows.append({"cusip": cusip, "ticker": found[cusip], "resolved_at": now.isoformat()})
            else:
                result[cusip] = None  # request failed entirely; try again next run
        time.sleep(OPENFIGI_PAUSE)

    if new_rows:
        sb_upsert("ticker_map", new_rows, "cusip")
    return result


# ---------------------------------------------------------------- Prices

class PriceBook:
    """Daily split-adjusted closes and split events for one ticker."""

    def __init__(self, ticker, start):
        hist = yf.Ticker(to_yahoo(ticker)).history(start=start, auto_adjust=False, actions=True)
        if hist.empty:
            raise ValueError("no price history")
        hist = hist.reset_index()
        hist["d"] = hist["Date"].dt.strftime("%Y-%m-%d")
        # With auto_adjust=False, "Close" is adjusted for splits but NOT
        # dividends, so it reads like the real share price (price return).
        self.closes = list(zip(hist["d"], hist["Close"].astype(float)))
        self.splits = [(d, float(s)) for d, s in zip(hist["d"], hist["Stock Splits"]) if s and float(s) > 0]
        self.last_date, self.last_close = self.closes[-1]

    def split_factor_after(self, period_end):
        """Multiply shares reported at period_end by this to get today's share basis."""
        f = 1.0
        for d, ratio in self.splits:
            if d > period_end:
                f *= ratio
        return f

    def quarter_avg(self, period_end):
        q_start = quarter_start_iso(period_end)
        vals = [c for d, c in self.closes if q_start <= d <= period_end]
        return sum(vals) / len(vals) if vals else None


def load_prices(tickers, start):
    books, failed = {}, []
    for t in sorted(tickers):
        for attempt in range(2):
            try:
                books[t] = PriceBook(t, start)
                break
            except Exception as e:
                if attempt == 1:
                    failed.append(t)
                    print(f"[price] {t}: {e}")
                time.sleep(1.5)
        time.sleep(0.3)
    print(f"[price] priced {len(books)}/{len(tickers)} tickers ({len(failed)} failed)")
    return books


# ---------------------------------------------------------------- Cost basis

def build_positions(cik, fund_name, snapshots, tickers, books):
    latest = snapshots[-1]
    current = set(latest["holdings"])
    state = {}
    rows = []

    for idx, snap in enumerate(snapshots):
        p = snap["period_end"]
        for cusip, h in snap["holdings"].items():
            if cusip not in current:
                continue  # only positions still held matter for today's P&L
            book = books.get(tickers.get(cusip))
            if not book or h["shares"] <= 0:
                continue
            factor = book.split_factor_after(p)
            shares = h["shares"] * factor
            px = book.quarter_avg(p)
            method = "quarter_avg"
            if px is None and h["value"]:
                px = (h["value"] / h["shares"]) / factor  # filing's own quarter-end price
                method = "filing_implied"
            if px is None:
                continue

            st = state.get(cusip)
            if st and idx - st["last_idx"] > GAP_RESET_QUARTERS:
                st = None  # gone long enough that a return counts as a new position
            if st is None:
                state[cusip] = {"shares": shares, "cost": shares * px, "first": p,
                                "pre_history": idx == 0, "methods": {method}, "last_idx": idx}
                continue
            added = shares - st["shares"]
            if added > 0:
                st["cost"] += added * px
                st["methods"].add(method)
            elif added < 0:
                st["cost"] = (st["cost"] / st["shares"]) * shares
            st["shares"] = shares
            st["last_idx"] = idx

    stamp = datetime.now(timezone.utc).isoformat()
    for cusip, h in latest["holdings"].items():
        ticker = tickers.get(cusip)
        st = state.get(cusip)
        book = books.get(ticker)
        base = {"cik": cik, "fund_name": fund_name, "cusip": cusip, "issuer": h["issuer"],
                "ticker": display_ticker(ticker) if ticker else None,
                "latest_period": latest["period_end"], "updated_at": stamp}
        if not st or not book or st["shares"] <= 0:
            rows.append({**base, "status": "unpriced"})
            continue
        est_cost = st["cost"] / st["shares"]
        cur = book.last_close
        rows.append({
            **base,
            "status": "priced",
            "first_seen_period": st["first"],
            "opened_in_window": not st["pre_history"],
            "shares": round(st["shares"], 2),
            "est_cost": round(est_cost, 4),
            "current_price": round(cur, 4),
            "price_as_of": book.last_date,
            "return_pct": round((cur - est_cost) / est_cost * 100, 2),
            "unrealized_gain": round(st["shares"] * (cur - est_cost), 2),
            "value_now": round(st["shares"] * cur, 2),
            "basis_method": "mixed" if len(st["methods"]) > 1 else next(iter(st["methods"])),
        })
    return rows


# ---------------------------------------------------------------- AI summary

WL_PROMPT = """You are summarizing estimated profit and loss on hedge funds' disclosed 13F stock positions for a public dashboard. Every number below was already computed; your job is only to explain it. Write a short, factual summary (4-6 sentences, plain prose, no bullet points, no markdown) that:
- Names the biggest winners and losers by ticker, fund, and return percentage as given
- Compares funds' hit rates or median returns where the difference is notable
- Notes whether stocks held by several funds performed differently from single-fund positions, if the digest shows it
- Never recommends any action, never says "consider buying/selling", never predicts future price performance
- Stays strictly within the data provided -- do not invent or recalculate any figures, tickers, or funds
- Ends with a brief plain-language reminder that cost basis is estimated from lagged quarterly filings, not actual trade prices, and this is not investment advice
- HARD LIMIT: your entire response must be under 120 words. Prioritize the single most notable pattern over covering everything.

DATA DIGEST:
{digest}
"""


def build_digest(rows):
    priced = [r for r in rows if r["status"] == "priced" and r["opened_in_window"]]
    if not priced:
        return None
    holders = {}
    for r in rows:
        holders[r["cusip"]] = holders.get(r["cusip"], 0) + 1

    rets = [r["return_pct"] for r in priced]
    lines = [
        f"Positions opened within the tracked window and priced: {len(priced)}",
        f"In profit: {sum(1 for x in rets if x > 0) / len(rets) * 100:.0f}%; median return {statistics.median(rets):+.1f}%",
    ]
    crowded = [r["return_pct"] for r in priced if holders[r["cusip"]] >= 3]
    solo = [r["return_pct"] for r in priced if holders[r["cusip"]] == 1]
    if len(crowded) >= 3 and len(solo) >= 3:
        lines.append(f"Median return, stocks held by 3+ tracked funds: {statistics.median(crowded):+.1f}% (n={len(crowded)}); "
                     f"held by only one fund: {statistics.median(solo):+.1f}% (n={len(solo)})")

    lines.append("\nPER FUND (positions, % in profit, median return):")
    by_fund = {}
    for r in priced:
        by_fund.setdefault(r["fund_name"], []).append(r["return_pct"])
    for name, vals in sorted(by_fund.items(), key=lambda kv: -statistics.median(kv[1])):
        if len(vals) >= 3:
            lines.append(f"  {name}: {len(vals)}, {sum(1 for v in vals if v > 0) / len(vals) * 100:.0f}%, {statistics.median(vals):+.1f}%")

    def fmt(r):
        return (f"  {r['ticker']} ({r['fund_name']}, held by {holders[r['cusip']]} tracked funds): "
                f"est. cost ${r['est_cost']:.2f} -> ${r['current_price']:.2f} ({r['return_pct']:+.1f}%), first seen {r['first_seen_period']}")

    ordered = sorted(priced, key=lambda r: r["return_pct"])
    lines.append("\nTOP WINNERS:")
    lines += [fmt(r) for r in reversed(ordered[-8:]) if r["return_pct"] > 0]
    lines.append("\nTOP LOSERS:")
    lines += [fmt(r) for r in ordered[:8] if r["return_pct"] < 0]
    return "\n".join(lines), statistics.median(rets)


# ---------------------------------------------------------------- Main

def main():
    run_started = datetime.now(timezone.utc).isoformat()

    histories = load_histories()
    if not histories:
        print("[wl] no fund snapshots found, nothing to do")
        return

    needed = {}
    for h in histories.values():
        for cusip, info in h["snapshots"][-1]["holdings"].items():
            if cusip not in needed or info["ticker_hint"]:
                needed[cusip] = info["ticker_hint"]
    tickers = resolve_tickers(needed)
    tickers = {c: t for c, t in tickers.items() if is_equity_ticker(t)}

    earliest = min(h["snapshots"][0]["period_end"] for h in histories.values())
    books = load_prices(set(tickers.values()), quarter_start_iso(earliest))

    all_rows = []
    for cik, h in histories.items():
        rows = build_positions(cik, h["fund_name"], h["snapshots"], tickers, books)
        priced = sum(1 for r in rows if r["status"] == "priced")
        print(f"[wl] {h['fund_name']}: {priced}/{len(rows)} positions priced (latest {h['snapshots'][-1]['period_end']})")
        all_rows.extend(rows)

    # PostgREST bulk upserts need every row to have the same keys.
    keys = set().union(*(r.keys() for r in all_rows))
    all_rows = [{k: r.get(k) for k in keys} for r in all_rows]
    sb_upsert("position_pnl", all_rows, "cik,cusip")
    sb_delete_older_than("position_pnl", run_started)  # positions funds have since exited
    print(f"[wl] wrote {len(all_rows)} rows to position_pnl")

    try:
        built = build_digest(all_rows)
        if built:
            digest, median_ret = built
            summary = call_gemini(WL_PROMPT.format(digest=digest))
            if summary:
                save_ai_summary("winners_losers", summary, "daily price refresh", round(median_ret, 2))
                print("[ai] generated winners & losers summary")
    except Exception as e:
        print(f"[error] AI summary failed: {e}")


if __name__ == "__main__":
    main()
