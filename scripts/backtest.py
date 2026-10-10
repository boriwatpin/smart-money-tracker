"""
Copycat backtest: what if you had bought what the stock-picking funds
disclosed, as soon as their 13F filings were public, holding 10 stocks?

Runs nightly right after winners_losers.py (same workflow), so the
forward "paper" portfolios update every weekday. It only READS the
holdings archive (holdings + filing_index) and the ticker cache; it writes
three tables of its own: backtest_picks, backtest_daily, backtest_summary.

Rules (kept deliberately simple so a person could follow them by hand):
  - Universe: concentrated stock-pickers only (UNIVERSE below). Quant and
    multi-manager firms are left out: their 13F positions are often hedges
    or short-term trades, not ideas worth copying a quarter later.
  - Trade date: the first trading day after each 13F deadline (Feb 14,
    May 15, Aug 14, Nov 14; moved to Monday if it falls on a weekend).
    Only filings published by the deadline count, so nothing is used
    before it was public.
  - 10 stocks, 10% each, held until the next trade date. 0.1% cost on
    every dollar traded. Returns include dividends (Yahoo adjusted close),
    for the strategies and for SPY alike.
  - A stock that stops trading (takeover, delisting) is held at its last
    price -- effectively cash -- until the next trade date.

Strategies:
  consensus   Stocks held by the most universe funds (any size, from each
              filing's full stock list), ties broken by combined value.
  best_ideas  Each fund's largest position, then each fund's 2nd largest,
              and so on (ordered by % of that fund's portfolio), until 10
              different stocks.
  fresh_buys  Positions absent from the fund's previous filing, ranked by %
              of the buying fund's portfolio. Empty slots are held in SPY.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, SEC_USER_AGENT.
"""

import time
import bisect
from datetime import date, datetime, timedelta, timezone

import yfinance as yf

from winners_losers import sb_get_all, sb_upsert, sb_delete_older_than, resolve_tickers, to_yahoo, display_ticker

UNIVERSE = {
    "0002026053": "Pershing Square",
    "0001067983": "Berkshire Hathaway",
    "0001656456": "Appaloosa",
    "0001167483": "Tiger Global",
    "0001135730": "Coatue",
    "0001040273": "Third Point",
    "0001536411": "Duquesne",
}
LEGACY = {"0001336528": "0002026053"}  # old filer CIK -> current fund

N_PICKS = 10
START_VALUE = 10_000.0
COST_RATE = 0.001
BENCHMARK = "SPY"
# Live paper tracking starts at the first trade date on or after this day.
LIVE_FROM = "2026-10-11"
# Index funds and ETFs that show up in 13Fs aren't stock ideas.
NOT_IDEAS = {"SPY", "QQQ", "IWM", "DIA", "VOO", "IVV", "VTI", "GLD", "SLV", "EEM", "EFA", "XLF", "XLE", "XLK",
             "SMH", "TLT", "HYG", "LQD", "KWEB", "FXI", "ARKK", "GDX", "USO", "VEA", "VWO", "IEMG", "SPLG"}
STRATEGIES = {
    "consensus": "Consensus 10",
    "best_ideas": "Best ideas 10",
    "fresh_buys": "Fresh buys 10",
}


# ----------------------------------------------------------------------
# Dates
# ----------------------------------------------------------------------

def deadline_for(period_end):
    """13F deadline for a quarter end, moved to Monday if it's a weekend."""
    p = date.fromisoformat(period_end)
    nominal = {3: date(p.year, 5, 15), 6: date(p.year, 8, 14), 9: date(p.year, 11, 14),
               12: date(p.year + 1, 2, 14)}[p.month]
    while nominal.weekday() >= 5:
        nominal += timedelta(days=1)
    return nominal.isoformat()


def next_trading_day_after(day_iso, calendar):
    return next((d for d in calendar if d > day_iso), None)


def expected_trade_day(deadline_iso):
    """For a future deadline: the next weekday after it (holidays ignored)."""
    d = date.fromisoformat(deadline_iso) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


# ----------------------------------------------------------------------
# Archive (universe funds only)
# ----------------------------------------------------------------------

def load_filings():
    """{fund_cik: {period: {"filed", "total", "held": set, "top": [(cusip, issuer, value)] by value}}}"""
    ciks = list(UNIVERSE) + list(LEGACY)
    f = ",".join(ciks)
    index = sb_get_all(f"filing_index?select=cik,period_end,filed_date,total_value_usd,share_cusips&cik=in.({f})&order=cik,period_end")
    rows = sb_get_all(f"holdings?select=cik,period_end,cusip,put_call,share_type,issuer,value_usd&cik=in.({f})&order=cik,period_end,cusip,put_call")
    top = {}
    for r in rows:
        if r["put_call"] or r.get("share_type") == "PRN":
            continue
        top.setdefault((r["cik"], r["period_end"]), []).append((r["cusip"], r["issuer"], float(r["value_usd"] or 0)))
    out = {}
    for r in index:
        fund = LEGACY.get(r["cik"], r["cik"])
        entry = {"filed": r["filed_date"], "total": float(r["total_value_usd"] or 0),
                 "held": set(r.get("share_cusips") or []),
                 "top": sorted(top.get((r["cik"], r["period_end"]), []), key=lambda x: -x[2])}
        prev = out.setdefault(fund, {}).get(r["period_end"])
        # If an old and a new filer entity both filed, use the bigger filing.
        if prev is None or entry["total"] > prev["total"]:
            out[fund][r["period_end"]] = entry
    return out


def visible_filings(filings, period, cutoff):
    """Each universe fund's filing for `period`, if it was published by `cutoff`."""
    return {fund: q[period] for fund, q in filings.items()
            if period in q and q[period]["filed"] and q[period]["filed"] <= cutoff}


def previous_filing(filings, fund, period):
    earlier = [p for p in filings.get(fund, {}) if p < period]
    return filings[fund][max(earlier)] if earlier else None


# ----------------------------------------------------------------------
# Candidate lists (best first); the picker skips names it can't trade
# ----------------------------------------------------------------------

def ordinal(n):
    return {1: "largest", 2: "2nd largest", 3: "3rd largest"}.get(n, f"{n}th largest")


def candidates_consensus(vis):
    holders, value, issuer = {}, {}, {}
    for fund, q in vis.items():
        for c in q["held"]:
            holders.setdefault(c, set()).add(fund)
        for c, iss, v in q["top"]:
            value[c] = value.get(c, 0) + v
            issuer.setdefault(c, iss)
    ranked = sorted(holders, key=lambda c: (-len(holders[c]), -value.get(c, 0)))
    n = len(vis)
    return [(c, issuer.get(c, ""), f"Held by {len(holders[c])} of {n} funds") for c in ranked if c in issuer]


def candidates_best_ideas(vis):
    depth_lists = []
    for fund, q in vis.items():
        for depth, (c, iss, v) in enumerate(q["top"][:N_PICKS], start=1):
            pct = v / q["total"] * 100 if q["total"] else 0
            depth_lists.append((depth, -pct, c, iss, f"{UNIVERSE[fund]}'s {ordinal(depth)} position, {pct:.1f}% of book"))
    depth_lists.sort()
    return [(c, iss, why) for _, _, c, iss, why in depth_lists]


def candidates_fresh(vis, filings, period):
    out = []
    for fund, q in vis.items():
        prev = previous_filing(filings, fund, period)
        if prev is None:
            continue  # no earlier filing to compare against
        for c, iss, v in q["top"]:
            if c not in prev["held"]:
                pct = v / q["total"] * 100 if q["total"] else 0
                out.append((-pct, c, iss, f"New {UNIVERSE[fund]} position, {pct:.1f}% of book"))
    out.sort()
    return [(c, iss, why) for _, c, iss, why in out]


# ----------------------------------------------------------------------
# Prices (Yahoo adjusted close: splits and dividends)
# ----------------------------------------------------------------------

def fetch_adjusted(ticker, start):
    for attempt in range(3):
        try:
            df = yf.Ticker(to_yahoo(ticker)).history(start=start, auto_adjust=True, actions=False)
            if df is None or df.empty:
                return {}
            df = df.reset_index()
            dates = df[df.columns[0]].dt.strftime("%Y-%m-%d")
            return {d: float(c) for d, c in zip(dates, df["Close"]) if c == c and c > 0}
        except Exception as e:
            time.sleep(30 if ("Too Many" in str(e) or "Rate" in str(e)) else 3)
    return {}


class Prices:
    def __init__(self, start):
        self.start = start
        self.data = {}
        self.days = {}

    def get(self, ticker):
        if ticker not in self.data:
            self.data[ticker] = fetch_adjusted(ticker, self.start)
            self.days[ticker] = sorted(self.data[ticker])
            time.sleep(0.25)
        return self.data[ticker]

    def on(self, ticker, day):
        return self.get(ticker).get(day)

    def last_on_or_before(self, ticker, day):
        series = self.get(ticker)
        days = self.days[ticker]
        i = bisect.bisect_right(days, day)
        return series[days[i - 1]] if i else None


# ----------------------------------------------------------------------
# Picking and simulating
# ----------------------------------------------------------------------

def pick(cands, tickers, prices, trade_day):
    chosen, seen = [], set()
    for c, iss, why in cands:
        t = tickers.get(c)
        if not t or display_ticker(t) in NOT_IDEAS or display_ticker(t) in seen:
            continue
        if prices.on(t, trade_day) is None:
            continue  # not trading that day (not listed yet, or already gone)
        seen.add(display_ticker(t))
        chosen.append({"cusip": c, "ticker": t, "issuer": iss, "reason": why})
        if len(chosen) == N_PICKS:
            break
    return chosen


def simulate(rounds, prices, calendar, end_day):
    """rounds: [(trade_day, [ticker,...] with BENCHMARK for empty slots)].
    Returns ({day: value}, [(start_value, end_value) per round])."""
    values, round_results = {}, []
    value, shares = START_VALUE, {}
    for i, (day, names) in enumerate(rounds):
        # Mark current holdings at the trade-day close, then rebalance.
        if shares:
            value = sum(n * (prices.last_on_or_before(t, day) or 0) for t, n in shares.items())
        target = {}
        for t in names:
            target[t] = target.get(t, 0) + value / len(names)
        current = {t: n * (prices.last_on_or_before(t, day) or 0) for t, n in shares.items()}
        traded = sum(abs(target.get(t, 0) - current.get(t, 0)) for t in set(target) | set(current))
        value -= traded * COST_RATE
        shares = {t: (value * target[t] / sum(target.values())) / prices.on(t, day) for t in target}
        start_value = value
        nxt = rounds[i + 1][0] if i + 1 < len(rounds) else None
        for d in calendar:
            if d < day or (nxt and d > nxt) or d > end_day:
                continue
            values[d] = sum(n * (prices.last_on_or_before(t, d) or 0) for t, n in shares.items())
        last_day = nxt or max(d for d in values)
        round_results.append((start_value, values[last_day]))
    return values, round_results


def max_drawdown(values):
    peak, worst = 0, 0
    for d in sorted(values):
        peak = max(peak, values[d])
        worst = min(worst, values[d] / peak - 1)
    return worst * 100


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    run_started = datetime.now(timezone.utc).isoformat()
    filings = load_filings()
    periods = sorted({p for q in filings.values() for p in q})
    if not periods:
        print("[backtest] holdings archive is empty -- run the archive backfill first")
        return

    first_trade_guess = deadline_for(periods[0])
    prices = Prices(start=(date.fromisoformat(first_trade_guess) - timedelta(days=10)).isoformat())
    calendar = sorted(prices.get(BENCHMARK))
    if not calendar:
        raise RuntimeError("could not download SPY prices from Yahoo")
    today = calendar[-1]

    # Trade dates per period; split into completed rounds and the upcoming one.
    schedule = []
    for p in periods:
        dl = deadline_for(p)
        td = next_trading_day_after(dl, calendar)
        schedule.append((p, dl, td))
    traded = [(p, dl, td) for p, dl, td in schedule if td]
    upcoming = next(((p, dl) for p, dl, td in schedule if td is None), None)

    # Resolve tickers for every name any strategy might consider.
    cand_cache = {}
    for p, dl, td in traded + ([(upcoming[0], upcoming[1], None)] if upcoming else []):
        cutoff = dl if td else today
        vis = visible_filings(filings, p, cutoff)
        cand_cache[p] = {"vis": vis,
                         "consensus": candidates_consensus(vis)[:60],
                         "best_ideas": candidates_best_ideas(vis)[:60],
                         "fresh_buys": candidates_fresh(vis, filings, p)[:60]}
    cusips = {c for v in cand_cache.values() for k in STRATEGIES for c, _, _ in v[k]}
    tickers = resolve_tickers(cusips)

    stamp = datetime.now(timezone.utc).isoformat()
    live_since = next((td for _, _, td in traded if td >= LIVE_FROM), None)
    pick_rows, daily_rows, summary_rows = [], [], []

    # Benchmark: buy and hold SPY from the first trade date.
    if not traded:
        print("[backtest] no completed trade dates yet")
        return
    t0 = traded[0][2]
    bench_vals, bench_rounds = simulate([(td, [BENCHMARK]) for _, _, td in traded], prices, calendar, today)

    for key, label in STRATEGIES.items():
        rounds = []
        for p, dl, td in traded:
            chosen = pick(cand_cache[p][key], tickers, prices, td)
            slots = [c["ticker"] for c in chosen] + [BENCHMARK] * (N_PICKS - len(chosen)) if chosen else [BENCHMARK]
            rounds.append((td, slots))
            nxt = next((x for _, _, x in traded if x > td), None)
            exit_day = nxt or today
            for rank, c in enumerate(chosen, start=1):
                entry = prices.on(c["ticker"], td)
                exit_px = prices.last_on_or_before(c["ticker"], exit_day)
                pick_rows.append({
                    "strategy": key, "rebalance_date": td, "rank": rank, "ticker": display_ticker(c["ticker"]),
                    "cusip": c["cusip"], "issuer": c["issuer"], "reason": c["reason"], "weight": round(100 / len(slots), 2),
                    "source_period": p, "provisional": False, "entry_price": round(entry, 4),
                    "exit_price": round(exit_px, 4) if exit_px else None, "exit_date": exit_day,
                    "return_pct": round((exit_px / entry - 1) * 100, 2) if exit_px else None, "updated_at": stamp,
                })
            if len(chosen) < N_PICKS:
                pick_rows.append({
                    "strategy": key, "rebalance_date": td, "rank": len(chosen) + 1, "ticker": BENCHMARK, "cusip": "",
                    "issuer": "SPDR S&P 500 ETF", "reason": f"{N_PICKS - len(chosen)} empty slot(s) held in SPY",
                    "weight": round(100 * (N_PICKS - len(chosen)) / N_PICKS, 2), "source_period": p, "provisional": False,
                    "entry_price": round(prices.on(BENCHMARK, td), 4),
                    "exit_price": round(prices.last_on_or_before(BENCHMARK, exit_day), 4), "exit_date": exit_day,
                    "return_pct": round((prices.last_on_or_before(BENCHMARK, exit_day) / prices.on(BENCHMARK, td) - 1) * 100, 2),
                    "updated_at": stamp,
                })

        vals, round_results = simulate(rounds, prices, calendar, today)
        for d, v in vals.items():
            daily_rows.append({"strategy": key, "d": d, "value": round(v, 2),
                               "live": bool(live_since and d >= live_since), "updated_at": stamp})
        completed = len(round_results) - 1  # the last round is still running
        beat = sum(1 for (s, e), (bs, be) in zip(round_results[:completed], bench_rounds[:completed]) if e / s > be / bs)
        years = (date.fromisoformat(today) - date.fromisoformat(t0)).days / 365.25
        total = vals[today] / START_VALUE - 1
        summary_rows.append({
            "strategy": key, "label": label, "start_date": t0, "end_date": today,
            "total_return": round(total * 100, 2),
            "annual_return": round(((1 + total) ** (1 / years) - 1) * 100, 2) if years > 0.5 else None,
            "max_drawdown": round(max_drawdown(vals), 2), "quarters_beat": beat, "quarters_total": completed,
            "live_since": live_since,
            "live_return": round((vals[today] / vals[live_since] - 1) * 100, 2) if live_since and live_since in vals else None,
            "updated_at": stamp,
        })
        print(f"[backtest] {label}: {total * 100:+.1f}% since {t0}, beat SPY in {beat}/{completed} quarters")

    for d, v in bench_vals.items():
        daily_rows.append({"strategy": "spy", "d": d, "value": round(v, 2),
                           "live": bool(live_since and d >= live_since), "updated_at": stamp})
    b_total = bench_vals[today] / START_VALUE - 1
    b_years = (date.fromisoformat(today) - date.fromisoformat(t0)).days / 365.25
    summary_rows.append({
        "strategy": "spy", "label": "SPY", "start_date": t0, "end_date": today,
        "total_return": round(b_total * 100, 2),
        "annual_return": round(((1 + b_total) ** (1 / b_years) - 1) * 100, 2) if b_years > 0.5 else None,
        "max_drawdown": round(max_drawdown(bench_vals), 2), "quarters_beat": None, "quarters_total": len(bench_rounds) - 1,
        "live_since": live_since,
        "live_return": round((bench_vals[today] / bench_vals[live_since] - 1) * 100, 2) if live_since and live_since in bench_vals else None,
        "updated_at": stamp,
    })
    print(f"[backtest] SPY: {b_total * 100:+.1f}% since {t0}")

    # Upcoming round: provisional picks from the filings published so far.
    if upcoming:
        p, dl = upcoming
        vis = cand_cache[p]["vis"]
        for key in STRATEGIES:
            chosen = pick(cand_cache[p][key], tickers, prices, today)
            for rank, c in enumerate(chosen, start=1):
                pick_rows.append({
                    "strategy": key, "rebalance_date": expected_trade_day(dl),
                    "rank": rank, "ticker": display_ticker(c["ticker"]), "cusip": c["cusip"], "issuer": c["issuer"],
                    "reason": c["reason"], "weight": round(100 / N_PICKS, 2), "source_period": p, "provisional": True,
                    "funds_filed": len(vis), "funds_total": len(UNIVERSE), "updated_at": stamp,
                })
            if len(chosen) < N_PICKS:
                pick_rows.append({
                    "strategy": key, "rebalance_date": expected_trade_day(dl), "rank": len(chosen) + 1,
                    "ticker": BENCHMARK, "cusip": "", "issuer": "SPDR S&P 500 ETF",
                    "reason": f"{N_PICKS - len(chosen)} empty slot(s) held in SPY",
                    "weight": round(100 * (N_PICKS - len(chosen)) / N_PICKS, 2), "source_period": p,
                    "provisional": True, "funds_filed": len(vis), "funds_total": len(UNIVERSE), "updated_at": stamp,
                })
        print(f"[backtest] upcoming {p} round: {len(vis)}/{len(UNIVERSE)} funds filed so far")

    sb_upsert("backtest_picks", pick_rows, "strategy,rebalance_date,rank")
    sb_delete_older_than("backtest_picks", run_started)
    sb_upsert("backtest_daily", daily_rows, "strategy,d", batch=1000)
    sb_delete_older_than("backtest_daily", run_started)
    sb_upsert("backtest_summary", summary_rows, "strategy")
    print(f"[backtest] wrote {len(pick_rows)} picks, {len(daily_rows)} daily values")


if __name__ == "__main__":
    main()
