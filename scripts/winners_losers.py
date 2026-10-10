"""
Winners & Losers, v2: open positions, closed trades and daily fund history,
built from the holdings archive (holdings + filing_index tables).

Runs nightly via .github/workflows/winners_losers.yml, after the US close.

Cost basis (13F filings never show trade prices, so it is estimated):
  - Each fund's archived quarters are walked oldest -> newest.
  - Shares added in a quarter are priced at that quarter's average daily
    close; sells reduce the position but keep the average cost.
  - Share counts are split-adjusted to today's share basis.
  - A position missing from the archived top 100 but still listed in the
    filing's full stock list (filing_index.share_cusips) is still held, just
    smaller -- it is NOT treated as sold.
  - A position missing from the full stock list was sold during that
    quarter. It becomes a closed trade, exited at that quarter's average
    close (filings don't reveal the sell date).
  - Positions already held in a fund's first archived quarter are flagged
    opened_in_window = false: their real entry predates our history.

Transfer-saving design (Supabase free plan counts every byte read):
  - Daily closes are written to price_history but never read back. Each
    night only new days are downloaded from Yahoo; what the cost basis
    needs is kept as one average per ticker per quarter (quarter_prices).
  - The website only reads the top-25 rows it displays, plus small
    per-fund summaries in fund_pnl_daily.

Tables written: ticker_map, ticker_meta, quarter_prices, price_history,
position_pnl, closed_positions, fund_pnl_daily, ai_summaries.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, SEC_USER_AGENT, optional
GEMINI_API_KEY, optional REBUILD_HISTORY=true (re-download all price
history and rebuild the full daily fund history).
"""

import os
import re
import sys
import time
import json
import statistics
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests
import yfinance as yf

from fetch_13f import (
    FUNDS,
    LEGACY_FILERS,
    SUPABASE_URL,
    SUPABASE_KEY,
    quarter_start_iso,
    call_gemini,
    save_ai_summary,
)

DISPLAY_TOP_N = 25          # what the site shows; the archive holds 100
EQUITY_TICKER_RE = re.compile(r"[A-Za-z.\-]{1,6}")
UNRESOLVED_RETRY_DAYS = 30  # retry OpenFIGI for CUSIPs that had no ticker
PRICE_RETRY_DAYS = 7        # retry Yahoo for tickers that returned nothing
OPENFIGI_BATCH = 10
OPENFIGI_PAUSE = 2.6
WRITE_BATCH = 1000
REBUILD = os.environ.get("REBUILD_HISTORY", "").strip().lower() == "true"

HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
WRITE_HEADERS = {**HEADERS, "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal"}


# ======================================================================
# Supabase helpers
# ======================================================================

def sb_get_all(path, page=1000):
    """GET every row; callers must include an order= so paging is stable."""
    rows, offset = [], 0
    while True:
        sep = "&" if "?" in path else "?"
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}{sep}limit={page}&offset={offset}",
                         headers=HEADERS, timeout=60)
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page:
            return rows
        offset += page


def sb_upsert(table, rows, conflict, batch=500):
    if not rows:
        return
    keys = set().union(*(r.keys() for r in rows))  # PostgREST needs uniform keys
    rows = [{k: r.get(k) for k in keys} for r in rows]
    for i in range(0, len(rows), batch):
        for attempt in range(3):
            r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={conflict}",
                              headers=WRITE_HEADERS, data=json.dumps(rows[i:i + batch]), timeout=120)
            if r.status_code < 300:
                break
            if attempt == 2:
                raise RuntimeError(f"Supabase upsert into {table} failed: {r.status_code} {r.text[:300]}")
            time.sleep(3)


def sb_delete_older_than(table, iso_ts):
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}?updated_at=lt.{quote(iso_ts)}",
                        headers=HEADERS, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase delete on {table} failed: {r.status_code} {r.text}")


def sb_has_rows(table):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?select=cik&limit=1", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return bool(r.json())


# ======================================================================
# Dates
# ======================================================================

def quarter_end_of(day_iso):
    d = date.fromisoformat(day_iso)
    q_end_month = ((d.month - 1) // 3) * 3 + 3
    nxt = date(d.year + (q_end_month == 12), (q_end_month % 12) + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


def days_before(day_iso, n):
    return (date.fromisoformat(day_iso) - timedelta(days=n)).isoformat()


# ======================================================================
# Archive
# ======================================================================

def fund_groups():
    """Current fund -> its CIKs (current first, then any legacy filers)."""
    groups = {f["cik"]: {"fund": f, "ciks": [f["cik"]]} for f in FUNDS}
    for lf in LEGACY_FILERS:
        if lf["successor_cik"] in groups:
            groups[lf["successor_cik"]]["ciks"].append(lf["cik"])
    return groups


def load_archive(groups):
    """Return {fund_cik: [quarter, ...]} oldest first. Each quarter:
    {"period", "filed", "held": set of every share CUSIP (or None),
     "top": {cusip: {"issuer", "shares", "value", "rank"}}}"""
    ciks = [c for g in groups.values() for c in g["ciks"]]
    cik_filter = ",".join(ciks)
    index_rows = sb_get_all(f"filing_index?select=cik,period_end,filed_date,total_value_usd,share_cusips"
                            f"&cik=in.({cik_filter})&order=cik,period_end")
    hold_rows = sb_get_all(f"holdings?select=cik,period_end,cusip,put_call,share_type,issuer,shares,value_usd"
                           f"&cik=in.({cik_filter})&order=cik,period_end,cusip,put_call")
    print(f"[archive] {len(index_rows)} filings, {len(hold_rows)} archived positions")

    by_filing = {}
    for r in hold_rows:
        # Plain shares only: options are tracked separately in holdings and
        # bonds (PRN) have no stock price to compare against.
        if r["put_call"] or r.get("share_type") == "PRN":
            continue
        by_filing.setdefault((r["cik"], r["period_end"]), []).append(r)
    index = {(r["cik"], r["period_end"]): r for r in index_rows}

    out = {}
    for fund_cik, g in groups.items():
        periods = sorted({p for (c, p) in index if c in g["ciks"]} |
                         {p for (c, p) in by_filing if c in g["ciks"]})
        quarters = []
        for p in periods:
            # When old and new filer entities both filed the same quarter, use
            # the bigger filing. Pershing Square's new entity filed small 13Fs
            # for several quarters while the old one still filed the full
            # portfolio; taking the small one would fake a wave of exits.
            candidates = [c for c in g["ciks"] if (c, p) in index or (c, p) in by_filing]
            cik = max(candidates, key=lambda c: (
                float((index.get((c, p)) or {}).get("total_value_usd") or 0),
                len(by_filing.get((c, p), [])),
            ))
            idx_row = index.get((cik, p)) or {}
            rows = sorted(by_filing.get((cik, p), []), key=lambda r: float(r["value_usd"] or 0), reverse=True)
            top = {}
            for rank, r in enumerate(rows, start=1):
                top[r["cusip"]] = {"issuer": r["issuer"], "shares": float(r["shares"] or 0),
                                   "value": float(r["value_usd"] or 0), "rank": rank}
            held = idx_row.get("share_cusips")
            quarters.append({"period": p, "filed": idx_row.get("filed_date") or p,
                             "held": set(held) if held is not None else None, "top": top})
        if quarters:
            out[fund_cik] = quarters
    return out


# ======================================================================
# Tickers (OpenFIGI, cached in ticker_map)
# ======================================================================

def to_yahoo(ticker):
    return ticker.replace("/", "-").replace(".", "-")


def display_ticker(ticker):
    return ticker.replace("/", ".")


def is_equity_ticker(ticker):
    return bool(ticker) and bool(EQUITY_TICKER_RE.fullmatch(display_ticker(ticker)))


def openfigi_batch(cusips):
    jobs = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in cusips]
    for attempt in range(4):
        try:
            r = requests.post("https://api.openfigi.com/v3/mapping", json=jobs,
                              headers={"Content-Type": "application/json"}, timeout=20)
            if r.status_code == 429:
                time.sleep(8 * (attempt + 1))
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
    return {}


def resolve_tickers(cusips):
    cached = {r["cusip"]: r for r in sb_get_all("ticker_map?select=cusip,ticker,resolved_at&order=cusip")}
    now = datetime.now(timezone.utc)
    result, to_lookup, new_rows = {}, [], []
    for c in cusips:
        row = cached.get(c)
        if row and row.get("ticker"):
            result[c] = row["ticker"]
        elif row and datetime.fromisoformat(row["resolved_at"].replace("Z", "+00:00")) > now - timedelta(days=UNRESOLVED_RETRY_DAYS):
            result[c] = None
        else:
            to_lookup.append(c)
    print(f"[figi] {len(cusips)} CUSIPs, {len(to_lookup)} need an OpenFIGI lookup")
    for i in range(0, len(to_lookup), OPENFIGI_BATCH):
        batch = to_lookup[i:i + OPENFIGI_BATCH]
        found = openfigi_batch(batch)
        for c in batch:
            if c in found:
                result[c] = found[c]
                new_rows.append({"cusip": c, "ticker": found[c], "resolved_at": now.isoformat()})
        time.sleep(OPENFIGI_PAUSE)
    sb_upsert("ticker_map", new_rows, "cusip")
    return {c: t for c, t in result.items() if is_equity_ticker(t)}


# ======================================================================
# Prices (Yahoo -> price_history / quarter_prices / ticker_meta)
# ======================================================================

def fetch_history(ticker, start):
    """Daily split-adjusted closes and split events since start, or None.
    With auto_adjust=False, Close is adjusted for splits but not dividends."""
    for attempt in range(3):
        try:
            df = yf.Ticker(to_yahoo(ticker)).history(start=start, auto_adjust=False, actions=True)
            if df is None or df.empty:
                return None
            df = df.reset_index()
            dates = df[df.columns[0]].dt.strftime("%Y-%m-%d")
            closes = [(d, float(c)) for d, c in zip(dates, df["Close"]) if c == c and c > 0]
            splits = []
            if "Stock Splits" in df.columns:
                splits = [(d, float(s)) for d, s in zip(dates, df["Stock Splits"]) if s == s and float(s) > 0]
            return (closes, splits) if closes else None
        except Exception as e:
            msg = str(e)
            time.sleep(30 if ("Too Many" in msg or "Rate" in msg) else 3)
    return None


class PriceBook:
    def __init__(self):
        self.meta = {r["ticker"]: r for r in sb_get_all(
            "ticker_meta?select=ticker,splits,history_start,first_date,last_close,last_close_date,failed,refreshed_at&order=ticker")}
        self.qavg = {}
        for r in sb_get_all("quarter_prices?select=ticker,period_end,avg_close,through_date&order=ticker,period_end"):
            self.qavg[(r["ticker"], r["period_end"])] = (float(r["avg_close"]), r["through_date"])
        self.daily = {}            # ticker -> {date: close}, only for tickers fully downloaded this run
        self.out_prices, self.out_quarters, self.out_meta = [], [], []
        self.stats = {"full": 0, "incremental": 0, "failed": 0, "skipped": 0}

    # ---- lookups used by the cost-basis walk
    def has(self, t):
        m = self.meta.get(t)
        return bool(m and m.get("last_close") is not None)

    def quarter_avg(self, t, period):
        v = self.qavg.get((t, period))
        return v[0] if v else None

    def split_factor_after(self, t, period):
        f = 1.0
        for d, ratio in (self.meta.get(t) or {}).get("splits") or []:
            if d > period:
                f *= ratio
        return f

    def last(self, t):
        m = self.meta.get(t) or {}
        return (float(m["last_close"]), m["last_close_date"]) if m.get("last_close") is not None else (None, None)

    # ---- refreshing
    def _store(self, t, closes, splits, start, full, keep_daily, since=None):
        stamp = datetime.now(timezone.utc).isoformat()
        # price_history keeps daily closes only for currently held tickers
        # (exited positions only need their quarterly averages), and a
        # nightly update only writes days it hasn't written before.
        if keep_daily:
            self.out_prices.extend({"ticker": t, "d": d, "close": round(c, 4)}
                                   for d, c in closes if since is None or d > since)
        by_q = {}
        for d, c in closes:
            by_q.setdefault(quarter_end_of(d), []).append((d, c))
        # A window always starts on a quarter's first day, so each quarter
        # seen here is complete up to the window's last day.
        for q, vals in by_q.items():
            avg = sum(c for _, c in vals) / len(vals)
            through = max(d for d, _ in vals)
            self.qavg[(t, q)] = (avg, through)
            self.out_quarters.append({"ticker": t, "period_end": q, "avg_close": round(avg, 4),
                                      "n_days": len(vals), "through_date": through})
        m = dict(self.meta.get(t) or {"ticker": t})
        if full:
            m.update({"splits": splits, "history_start": start, "first_date": closes[0][0]})
        m.update({"last_close": round(closes[-1][1], 4), "last_close_date": closes[-1][0],
                  "failed": False, "refreshed_at": stamp})
        self.meta[t] = m
        self.out_meta.append(m)

    def _mark_failed(self, t):
        m = dict(self.meta.get(t) or {"ticker": t})
        m.update({"failed": True, "refreshed_at": datetime.now(timezone.utc).isoformat()})
        self.meta[t] = m
        self.out_meta.append(m)
        self.stats["failed"] += 1

    def full_refresh(self, t, start, is_open):
        got = fetch_history(t, start)
        if not got:
            self._mark_failed(t)
            return
        closes, splits = got
        self._store(t, closes, splits, start, full=True, keep_daily=is_open)
        self.daily[t] = dict(closes)
        self.stats["full"] += 1

    def incremental(self, t):
        m = self.meta[t]
        start = quarter_start_iso(quarter_end_of(m["last_close_date"]))
        got = fetch_history(t, start)
        if not got:
            self.stats["failed"] += 1  # keep yesterday's price rather than dropping the ticker
            return
        closes, splits = got
        known = {d for d, _ in m.get("splits") or []}
        if any(d not in known for d, _ in splits):
            # A new split re-bases every past price, so start over for this ticker.
            print(f"[price] {t}: new stock split detected, re-downloading full history")
            self.full_refresh(t, m["history_start"], is_open=True)
            return
        self._store(t, closes, splits, start, full=False, keep_daily=True,
                    since=days_before(m["last_close_date"], 5))
        self.stats["incremental"] += 1

    def ensure(self, t, needed_periods, is_open, history_start):
        m = self.meta.get(t)
        if m and m.get("failed") and not REBUILD:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(m["refreshed_at"].replace("Z", "+00:00"))
            if age < timedelta(days=PRICE_RETRY_DAYS):
                self.stats["skipped"] += 1
                return
        need_full = REBUILD or not m or m.get("failed") or not m.get("history_start") \
            or m["history_start"] > history_start
        if not need_full:
            first, last_d = m.get("first_date") or "0000", m.get("last_close_date") or "0000"
            for p in needed_periods:
                if p < first or p > last_d:
                    continue  # ticker wasn't trading (yet / anymore) then
                q = self.qavg.get((t, p))
                if q is None or q[1] < days_before(p, 5):
                    need_full = True  # missing or computed before that quarter ended
                    break
        if need_full:
            self.full_refresh(t, history_start, is_open)
        elif is_open:
            self.incremental(t)
        else:
            return  # nothing to download: every quarter it needs is already stored
        time.sleep(0.25)

    def flush(self):
        sb_upsert("ticker_meta", list({m["ticker"]: m for m in self.out_meta}.values()), "ticker")
        sb_upsert("quarter_prices", list({(q["ticker"], q["period_end"]): q for q in self.out_quarters}.values()),
                  "ticker,period_end")
        sb_upsert("price_history", self.out_prices, "ticker,d", batch=WRITE_BATCH)
        print(f"[price] full downloads {self.stats['full']}, nightly updates {self.stats['incremental']}, "
              f"failed {self.stats['failed']}, waiting to retry {self.stats['skipped']}; "
              f"{len(self.out_prices)} daily closes written")


# ======================================================================
# Cost-basis walk
# ======================================================================

def walk_fund(quarters, tickers, prices):
    """Returns (open_rows_for_latest_quarter, closed_trades, per_quarter_snapshots)."""
    state, closed, snapshots = {}, [], []

    for idx, q in enumerate(quarters):
        p = q["period"]

        # Exits: open positions absent from this filing's full stock list.
        if idx > 0 and q["held"] is not None:
            for cusip in [c for c in state if c not in q["top"] and c not in q["held"]]:
                st = state.pop(cusip)
                t = tickers.get(cusip)
                exit_px = prices.quarter_avg(t, p) if t else None
                if exit_px is None or st["shares"] <= 0:
                    continue
                cost = st["cost"] / st["shares"]
                closed.append({
                    "cusip": cusip, "ticker": display_ticker(t), "issuer": st["issuer"],
                    "opened_period": st["first"], "closed_period": p, "closed_filed": q["filed"],
                    "opened_in_window": not st["pre_history"], "quarters_held": idx - st["first_idx"],
                    "best_rank": st["best_rank"], "shares": round(st["shares"], 2),
                    "est_cost": round(cost, 4), "est_exit": round(exit_px, 4),
                    "realized_return_pct": round((exit_px - cost) / cost * 100, 2),
                    "realized_gain": round(st["shares"] * (exit_px - cost), 2),
                })

        # Buys and sells among the archived top 100.
        for cusip, h in q["top"].items():
            t = tickers.get(cusip)
            if not t or not prices.has(t) or h["shares"] <= 0:
                continue
            factor = prices.split_factor_after(t, p)
            shares = h["shares"] * factor
            px = prices.quarter_avg(t, p)
            if px is None and h["value"]:
                px = (h["value"] / h["shares"]) / factor  # the filing's own quarter-end price
            if px is None:
                continue
            st = state.get(cusip)
            if st is None:
                state[cusip] = {"shares": shares, "cost": shares * px, "first": p, "first_idx": idx,
                                "pre_history": idx == 0, "best_rank": h["rank"], "issuer": h["issuer"]}
                continue
            added = shares - st["shares"]
            if added > 0:
                st["cost"] += added * px
            elif added < 0:
                st["cost"] = st["cost"] / st["shares"] * shares
            st["shares"] = shares
            st["best_rank"] = min(st["best_rank"], h["rank"])

        snapshots.append([
            {"ticker": tickers[c], "shares": state[c]["shares"], "cost": state[c]["cost"] / state[c]["shares"],
             "rank": h["rank"], "opened_in_window": not state[c]["pre_history"]}
            for c, h in q["top"].items() if c in state and state[c]["shares"] > 0
        ])

    latest = quarters[-1]
    open_rows = []
    for cusip, h in latest["top"].items():
        t = tickers.get(cusip)
        st = state.get(cusip)
        base = {"cusip": cusip, "issuer": h["issuer"], "ticker": display_ticker(t) if t else None,
                "latest_period": latest["period"], "rank": h["rank"]}
        price, price_date = prices.last(t) if t else (None, None)
        if not st or price is None or st["shares"] <= 0:
            open_rows.append({**base, "status": "unpriced"})
            continue
        cost = st["cost"] / st["shares"]
        open_rows.append({
            **base, "status": "priced", "first_seen_period": st["first"],
            "opened_in_window": not st["pre_history"], "shares": round(st["shares"], 2),
            "est_cost": round(cost, 4), "current_price": round(price, 4), "price_as_of": price_date,
            "return_pct": round((price - cost) / cost * 100, 2),
            "unrealized_gain": round(st["shares"] * (price - cost), 2),
            "value_now": round(st["shares"] * price, 2),
        })
    return open_rows, closed, snapshots


# ======================================================================
# Fund summaries (fund_pnl_daily)
# ======================================================================

# Every summary is stored twice: for positions opened within our history
# (the site's default), and with suffix _all including positions already
# held when history begins (the site's "include older positions" toggle).
# Without the _all figures, funds that rarely trade -- Berkshire, Pershing --
# would have almost nothing to show.

def summarize(items, suffix=""):
    """items: [(return_pct, unrealized_gain)]"""
    rets = [r for r, _ in items]
    if not rets:
        return {f"positions{suffix}": 0, f"median_return{suffix}": None,
                f"hit_rate{suffix}": None, f"total_unrealized{suffix}": None}
    return {f"positions{suffix}": len(rets), f"median_return{suffix}": round(statistics.median(rets), 2),
            f"hit_rate{suffix}": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
            f"total_unrealized{suffix}": round(sum(g for _, g in items), 2)}


def closed_stats(closed_rows, suffix=""):
    rets = [c["realized_return_pct"] for c in closed_rows]
    if not rets:
        return {f"closed_count{suffix}": 0, f"closed_hit_rate{suffix}": None, f"closed_median{suffix}": None}
    return {f"closed_count{suffix}": len(rets),
            f"closed_hit_rate{suffix}": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
            f"closed_median{suffix}": round(statistics.median(rets), 2)}


def in_scope_closed(closed, include_old=False):
    return [c for c in closed if c["best_rank"] <= DISPLAY_TOP_N and (include_old or c["opened_in_window"])]


def fund_summary(open_items, closed):
    """open_items: [(return_pct, gain, opened_in_window)] for top-25 priced positions."""
    return {**summarize([(r, g) for r, g, w in open_items if w]),
            **summarize([(r, g) for r, g, _ in open_items], "_all"),
            **closed_stats(in_scope_closed(closed)),
            **closed_stats(in_scope_closed(closed, include_old=True), "_all")}


def history_rows(cik, name, quarters, snapshots, closed, prices):
    """Rebuild the daily fund summary from price history downloaded this run.
    On each day, a fund's positions are those from its latest filing that had
    been PUBLISHED by then -- what an outside observer could actually know."""
    rows = []
    for i, q in enumerate(quarters):
        start = q["filed"]
        end = quarters[i + 1]["filed"] if i + 1 < len(quarters) else "9999-12-31"
        pos = [s for s in snapshots[i] if s["rank"] <= DISPLAY_TOP_N and s["ticker"] in prices.daily]
        days = sorted({d for s in pos for d in prices.daily[s["ticker"]] if start <= d < end})
        for d in days:
            items = []
            for s in pos:
                c = prices.daily[s["ticker"]].get(d)
                if c:
                    items.append(((c - s["cost"]) / s["cost"] * 100, s["shares"] * (c - s["cost"]), s["opened_in_window"]))
            if not items:
                continue
            known = [c for c in closed if c["closed_filed"] <= d]
            rows.append({"cik": cik, "fund_name": name, "d": d, **fund_summary(items, known)})
    return rows


# ======================================================================
# AI summary
# ======================================================================

WL_PROMPT = """You are summarizing estimated profit and loss on hedge funds' disclosed 13F stock positions for a public dashboard. Every number below was already computed; your job is only to explain it. Write a short, factual summary (4-6 sentences, plain prose, no bullet points, no markdown) that:
- Names the biggest open winners and losers by ticker, fund, and return percentage as given
- Mentions a notable closed trade or a fund's realized hit rate if the digest shows something striking
- Compares funds' hit rates or median returns where the difference is notable
- Never recommends any action, never says "consider buying/selling", never predicts future price performance
- Stays strictly within the data provided -- do not invent or recalculate any figures, tickers, or funds
- Ends with a brief plain-language reminder that cost and exit prices are estimated from lagged quarterly filings, not actual trade prices, and this is not investment advice
- HARD LIMIT: your entire response must be under 120 words. Prioritize the single most notable pattern over covering everything.

DATA DIGEST:
{digest}
"""


def build_digest(open_rows, closed_rows, fund_rows):
    scope = [r for r in open_rows if r["status"] == "priced" and r["opened_in_window"] and r["rank"] <= DISPLAY_TOP_N]
    if not scope:
        return None
    rets = [r["return_pct"] for r in scope]
    lines = [f"Open positions (each fund's top {DISPLAY_TOP_N}, opened within tracked history): {len(scope)}; "
             f"in profit {sum(1 for x in rets if x > 0) / len(rets) * 100:.0f}%; median {statistics.median(rets):+.1f}%"]
    lines.append("\nPER FUND (open median, open hit rate, closed trades, closed hit rate, closed median):")
    for f in sorted(fund_rows, key=lambda f: float("inf") if f["median_return"] is None else -f["median_return"]):
        if f["positions"]:
            closed_part = (f", {f['closed_count']} closed, {f['closed_hit_rate']:.0f}%, {f['closed_median']:+.1f}%"
                           if f["closed_count"] else ", no closed trades")
            lines.append(f"  {f['fund_name']}: {f['median_return']:+.1f}%, {f['hit_rate']:.0f}%{closed_part}")

    def fmt_open(r):
        return (f"  {r['ticker']} ({r['fund_name']}, {r['other_holders']} other tracked funds hold it): est. cost "
                f"${r['est_cost']:.2f} -> ${r['current_price']:.2f} ({r['return_pct']:+.1f}%), since {r['first_seen_period']}")
    ordered = sorted(scope, key=lambda r: r["return_pct"])
    lines.append("\nTOP OPEN WINNERS:")
    lines += [fmt_open(r) for r in reversed(ordered[-6:]) if r["return_pct"] > 0]
    lines.append("\nTOP OPEN LOSERS:")
    lines += [fmt_open(r) for r in ordered[:6] if r["return_pct"] < 0]

    recent = sorted([c for c in closed_rows if c["best_rank"] <= DISPLAY_TOP_N and c["opened_in_window"]],
                    key=lambda c: c["closed_period"])[-60:]
    if recent:
        recent.sort(key=lambda c: c["realized_return_pct"])
        lines.append("\nRECENT CLOSED TRADES, best and worst (estimated entry -> exit):")
        for c in recent[-3:][::-1] + recent[:3]:
            lines.append(f"  {c['ticker']} ({c['fund_name']}): ${c['est_cost']:.2f} -> ${c['est_exit']:.2f} "
                         f"({c['realized_return_pct']:+.1f}%), held {c['opened_period']} to {c['closed_period']}")
    return "\n".join(lines), statistics.median(rets)


# ======================================================================
# Main
# ======================================================================

def main():
    run_started = datetime.now(timezone.utc).isoformat()
    groups = fund_groups()
    archive = load_archive(groups)
    if not archive:
        print("[error] The holdings archive is empty. Run Actions -> Backfill 13F history "
              "with 'Archive only' ticked, then re-run this workflow.")
        sys.exit(1)

    cusips = {c for qs in archive.values() for q in qs for c in q["top"]}
    tickers = resolve_tickers(cusips)

    # Which quarters each ticker needs prices for: every quarter it was held
    # plus the following one (where an exit would be priced).
    needed, open_tickers = {}, set()
    for qs in archive.values():
        for i, q in enumerate(qs):
            for c in q["top"]:
                t = tickers.get(c)
                if t:
                    needed.setdefault(t, set()).add(q["period"])
                    if i + 1 < len(qs):
                        needed[t].add(qs[i + 1]["period"])
        open_tickers |= {tickers[c] for c in qs[-1]["top"] if c in tickers}
    history_start = quarter_start_iso(min(q["period"] for qs in archive.values() for q in qs))

    prices = PriceBook()
    print(f"[price] {len(needed)} tickers needed, {len(open_tickers)} currently held"
          f"{' (REBUILD: re-downloading everything)' if REBUILD else ''}")
    for n, t in enumerate(sorted(needed), start=1):
        prices.ensure(t, needed[t], t in open_tickers, history_start)
        if n % 100 == 0:
            print(f"[price] {n}/{len(needed)} tickers checked")
    prices.flush()

    # Walk every fund.
    open_all, closed_all, walks = [], [], {}
    stamp = datetime.now(timezone.utc).isoformat()
    for cik, qs in archive.items():
        name = groups[cik]["fund"]["name"]
        open_rows, closed, snaps = walk_fund(qs, tickers, prices)
        for r in open_rows:
            r.update({"cik": cik, "fund_name": name, "updated_at": stamp})
        for c in closed:
            c.update({"cik": cik, "fund_name": name, "updated_at": stamp})
        open_all += open_rows
        closed_all += closed
        walks[cik] = (name, qs, snaps, closed)
        priced = sum(1 for r in open_rows if r["status"] == "priced")
        print(f"[wl] {name}: {priced}/{len(open_rows)} open priced, {len(closed)} closed trades "
              f"(latest {qs[-1]['period']}, {len(qs)} quarters)")

    holders = {}
    for r in open_all:
        holders[r["cusip"]] = holders.get(r["cusip"], 0) + 1
    for r in open_all:
        r["other_holders"] = holders[r["cusip"]] - 1

    sb_upsert("position_pnl", open_all, "cik,cusip")
    sb_delete_older_than("position_pnl", run_started)
    sb_upsert("closed_positions", closed_all, "cik,cusip,opened_period")
    sb_delete_older_than("closed_positions", run_started)
    print(f"[wl] wrote {len(open_all)} open positions, {len(closed_all)} closed trades")

    # Daily fund summaries: tonight's row, plus the full history on the first
    # run (or on a REBUILD), from prices downloaded in this same run.
    fund_rows = []
    for cik, (name, qs, snaps, closed) in walks.items():
        scope = [r for r in open_all if r["cik"] == cik and r["status"] == "priced"
                 and r["rank"] <= DISPLAY_TOP_N]
        d = max((r["price_as_of"] for r in scope), default=None)
        if not d:
            continue
        items = [(r["return_pct"], r["unrealized_gain"], r["opened_in_window"]) for r in scope]
        fund_rows.append({"cik": cik, "fund_name": name, "d": d, **fund_summary(items, closed)})
    backfill = REBUILD or not sb_has_rows("fund_pnl_daily")
    hist = []
    if backfill:
        for cik, (name, qs, snaps, closed) in walks.items():
            hist += history_rows(cik, name, qs, snaps, closed, prices)
    merged = {(r["cik"], r["d"]): r for r in hist}
    merged.update({(r["cik"], r["d"]): r for r in fund_rows})  # tonight's row wins
    rows = [{**r, "updated_at": stamp} for r in merged.values()]
    sb_upsert("fund_pnl_daily", rows, "cik,d", batch=WRITE_BATCH)
    print(f"[wl] wrote {len(rows)} daily fund summary rows{' (history rebuilt)' if backfill else ''}")

    try:
        built = build_digest(open_all, closed_all, fund_rows)
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
