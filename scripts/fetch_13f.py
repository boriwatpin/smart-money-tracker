"""
Pulls each fund's latest 13F-HR filing from SEC EDGAR and upserts a
snapshot (portfolio value + top holdings) into a Supabase table.

Meant to run on a schedule via GitHub Actions (see .github/workflows/refresh.yml).
Safe to re-run: it upserts on (cik, period_end), so running it daily just
re-confirms "no new filing yet" until a fund actually files.

Required environment variables:
  SUPABASE_URL           e.g. https://xxxxx.supabase.co
  SUPABASE_SERVICE_KEY   the service_role key (NOT the anon key) — needed
                          to write, since RLS blocks anon writes by design
  SEC_USER_AGENT          e.g. "YourName your-email@example.com"
                          (SEC requires a real contact string on every
                          request or it may start rejecting them)
"""

import os
import re
import time
import json
import requests
import yfinance as yf
import xml.etree.ElementTree as ET
from datetime import datetime, date, timezone
from urllib.parse import quote

SEC_HEADERS = {
    "User-Agent": os.environ.get("SEC_USER_AGENT", "SmartMoneyTracker contact@example.com").strip(),
    "Accept-Encoding": "gzip, deflate",
}

SUPABASE_URL = os.environ["SUPABASE_URL"].strip().rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"].strip()

# CIKs are stable SEC identifiers for each filer. If a fund renames or
# reorganizes its filing entity, update the CIK here.
FUNDS = [
    {"name": "Berkshire Hathaway", "person": "Warren Buffett / Greg Abel", "cik": "0001067983"},
    {"name": "Bridgewater Associates", "person": "Ray Dalio", "cik": "0001350694"},
    {"name": "Renaissance Technologies", "person": "Jim Simons legacy / quant", "cik": "0001037389"},
    {"name": "Citadel Advisors", "person": "Ken Griffin", "cik": "0001423053"},
    {"name": "Pershing Square Inc.", "person": "Bill Ackman", "cik": "0002026053"},
    {"name": "Tiger Global Management", "person": "Chase Coleman", "cik": "0001167483"},
    {"name": "Third Point", "person": "Dan Loeb", "cik": "0001040273"},
    {"name": "Soros Fund Management", "person": "Dawn Fitzpatrick", "cik": "0001029160"},
    {"name": "Point72 Asset Management", "person": "Steven Cohen", "cik": "0001603466"},
    {"name": "Millennium Management", "person": "Israel Englander", "cik": "0001273087"},
    {"name": "Duquesne Family Office", "person": "Stanley Druckenmiller", "cik": "0001536411"},
    {"name": "Appaloosa Management", "person": "David Tepper", "cik": "0001656456"},
]

TOP_N_HOLDINGS = 25


def get_prior_snapshot(cik, before_period):
    """Look up the most recent snapshot stored for this fund from BEFORE
    a given period.

    Critical: must exclude the period currently being processed. Since we
    reprocess the last 2 quarters on every run, without this exclusion a
    quarter that's already stored would be found as its own "prior"
    snapshot once both quarters exist in the table -- silently disabling
    all new/increased-position detection on every run after the first.
    """
    url = f"{SUPABASE_URL}/rest/v1/fund_snapshots?cik=eq.{cik}&period_end=lt.{before_period}&order=period_end.desc&limit=1"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def cusip_to_ticker(cusip, cache):
    """Map a CUSIP to a ticker symbol via OpenFIGI's free public endpoint.

    13F filings only disclose CUSIPs, not tickers. This lookup can fail for
    thinly-traded securities, foreign issuers, or CUSIPs OpenFIGI doesn't
    recognize -- that's expected. OpenFIGI's anonymous tier also has a low
    rate limit, so a 429 gets a couple of backoff-and-retry attempts before
    giving up, rather than being treated as a permanent failure.
    """
    if cusip in cache:
        return cache[cusip]
    for attempt in range(3):
        try:
            r = requests.post(
                "https://api.openfigi.com/v3/mapping",
                json=[{"idType": "ID_CUSIP", "idValue": cusip}],
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            if r.status_code == 429:
                wait = 4 * (attempt + 1)
                print(f"[debug] OpenFIGI rate limited on CUSIP {cusip}, waiting {wait}s (attempt {attempt + 1}/3)")
                time.sleep(wait)
                continue
            r.raise_for_status()
            result = r.json()
            ticker = None
            if result and isinstance(result, list) and result[0].get("data"):
                ticker = result[0]["data"][0].get("ticker")
            if not ticker:
                print(f"[debug] OpenFIGI found no ticker for CUSIP {cusip}: {result}")
            cache[cusip] = ticker
            return ticker
        except Exception as e:
            print(f"[debug] OpenFIGI lookup exception for CUSIP {cusip}: {e}")
            cache[cusip] = None
            return None
    print(f"[debug] OpenFIGI still rate-limited after retries, giving up on CUSIP {cusip}")
    cache[cusip] = None
    return None


def quarter_start_iso(period_end_str):
    d = date.fromisoformat(period_end_str)
    q_month = ((d.month - 1) // 3) * 3 + 1
    return date(d.year, q_month, 1).isoformat()


def normalize_ticker_for_yahoo(ticker):
    """Yahoo uses a dash for share-class tickers where other sources use a
    dot (e.g. BRK.B -> BRK-B)."""
    return ticker.replace(".", "-")


def estimate_price_for_period(ticker, period_end_str):
    """Average of daily closing prices during a given quarter, compared to
    the most recent close available, using Yahoo Finance data via yfinance.
    Used both for brand-new positions (as a rough entry price) and for
    significant increases to existing positions (as a rough price for the
    shares that were added).

    This is necessarily an approximation -- SEC filings don't disclose
    which day(s) within the quarter a fund actually traded, so this
    assumes even buying across the quarter rather than a single trade.
    """
    try:
        yahoo_ticker = normalize_ticker_for_yahoo(ticker)
        q_start = quarter_start_iso(period_end_str)
        hist = yf.Ticker(yahoo_ticker).history(start=q_start, auto_adjust=True)
        if hist.empty:
            print(f"[debug] yfinance returned no data for ticker {ticker}")
            return None

        hist = hist.reset_index()
        hist["date_str"] = hist["Date"].dt.strftime("%Y-%m-%d")

        in_quarter = hist[(hist["date_str"] >= q_start) & (hist["date_str"] <= period_end_str)]["Close"]
        if in_quarter.empty:
            print(f"[debug] no {period_end_str}-quarter prices found for {ticker} (have {len(hist)} rows total)")
            return None

        avg_price = float(in_quarter.mean())
        current_price = float(hist["Close"].iloc[-1])
        current_date = hist["date_str"].iloc[-1]
        pct_change = ((current_price - avg_price) / avg_price) * 100 if avg_price else None
        return {
            "ticker": ticker,
            "avg_price_quarter": round(avg_price, 2),
            "current_price": round(current_price, 2),
            "pct_change_since_avg": round(pct_change, 2) if pct_change is not None else None,
            "price_as_of": current_date,
        }
    except Exception as e:
        print(f"[debug] yfinance exception for ticker {ticker}: {e}")
        return None


def get_json(url):
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def get_text(url):
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def recent_13f_filings(cik, n=2):
    """Return the n most recent 13F-HR filings for a CIK, newest first.

    Pulling 2 quarters (instead of just the latest) means new-position
    detection and price estimates work immediately on first run, rather
    than only after the *next* quarter's filing shows up.
    """
    data = get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    recent = data["filings"]["recent"]
    results = []
    for i, form in enumerate(recent["form"]):
        if form == "13F-HR":
            results.append({
                "accession": recent["accessionNumber"][i],
                "filed": recent["filingDate"][i],
                "period": recent["reportDate"][i],
            })
        if len(results) >= n:
            break
    return results


def find_infotable_url(cik, accession):
    """The holdings table ships as a separate XML file inside the filing folder.

    Filers name this file wildly inconsistently (infotable.xml, inftable.xml,
    table.xml, or even names with spaces in them) -- match loosely and
    URL-encode whatever we find.
    """
    acc_nodash = accession.replace("-", "")
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/index.json"
    idx = get_json(idx_url)
    items = idx["directory"]["item"]
    for item in items:
        name = item["name"].lower()
        if "table" in name and name.endswith(".xml") and "primary_doc" not in name:
            return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{quote(item['name'])}"
    # fallback: any XML file that isn't the primary doc
    for item in items:
        name = item["name"].lower()
        if name.endswith(".xml") and "primary_doc" not in name:
            return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{quote(item['name'])}"
    return None


def parse_infotable(xml_text):
    """Strip namespaces (SEC's XML uses them inconsistently) and pull each holding row.

    SEC's 13F infotable XML often declares a namespace prefix (e.g. ns1:infoTable)
    at the root and uses it throughout the body. Just removing the xmlns
    declaration isn't enough -- the prefixes on every tag also need to go,
    or ElementTree raises "unbound prefix".
    """
    xml_text = re.sub(r'\s+\w+:\w+=["\'][^"\']*["\']', "", xml_text)
    xml_text = re.sub(r'xmlns(:\w+)?=["\'][^"\']*["\']', "", xml_text)
    xml_text = re.sub(r'<(/?)\w+:', r'<\1', xml_text)
    root = ET.fromstring(xml_text)
    holdings = []
    for entry in root.iter():
        if entry.tag.split("}")[-1] == "infoTable":
            name = (entry.findtext("nameOfIssuer") or "").strip()
            cls = (entry.findtext("titleOfClass") or "").strip()
            value = entry.findtext("value") or "0"
            shares = entry.findtext(".//sshPrnamt") or "0"
            cusip = (entry.findtext("cusip") or "").strip()
            holdings.append({
                "issuer": name,
                "class": cls,
                "value_thousands": int(value),
                "shares": int(shares),
                "cusip": cusip,
            })
    return holdings


def upsert_snapshot(row):
    url = f"{SUPABASE_URL}/rest/v1/fund_snapshots?on_conflict=cik,period_end"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    r = requests.post(url, headers=headers, data=json.dumps([row]), timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase upsert failed: {r.status_code} {r.text}")


def process_filing(fund, latest, figi_cache):
    infotable_url = find_infotable_url(fund["cik"], latest["accession"])
    if not infotable_url:
        print(f"[warn] no infotable found for {fund['name']} ({latest['accession']})")
        return

    holdings = parse_infotable(get_text(infotable_url))
    if not holdings:
        print(f"[warn] infotable parsed to zero holdings for {fund['name']}")
        return

    # SEC's 13F XML <value> field is inconsistently scaled across filers --
    # some filing agents report it in whole dollars (the modern spec's
    # stated convention), others still use the legacy thousands convention.
    # We verified this empirically: Berkshire/Citadel/Third Point report
    # whole dollars, Duquesne reports thousands -- same field, different
    # units, depending on whichever filing software was used.
    #
    # Auto-detect per filing rather than assuming one global convention.
    # $50M (not $1B) is the right cutoff: SEC's legal minimum to even
    # require a 13F filing is $100M, so nothing genuinely below that should
    # exist under either convention -- but a $1B cutoff was too aggressive
    # and wrongly inflated small-but-correct transitional filings (e.g. a
    # newly-public parent's tiny initial single-holding filing, genuinely
    # worth ~$500M) by assuming they must be in thousands.
    raw_sum = sum(h["value_thousands"] for h in holdings)
    if raw_sum > 50_000_000:
        value_scale = 1
    else:
        value_scale = 1000
    total_value = raw_sum * value_scale
    total_for_pct = raw_sum  # percentages are a ratio, so the scale cancels out regardless
    if value_scale != 1:
        print(f"[debug] {fund['name']} ({latest['period']}): raw value sum ${raw_sum:,} looked like thousands, scaled x1000")

    top = sorted(holdings, key=lambda h: h["value_thousands"], reverse=True)[:TOP_N_HOLDINGS]

    # Figure out what's new or meaningfully increased vs. whatever's already stored.
    prior = get_prior_snapshot(fund["cik"], latest["period"])
    is_new_quarter = prior is not None
    prior_by_cusip = {}
    if is_new_quarter:
        prior_by_cusip = {h.get("cusip"): h for h in (prior.get("top_holdings") or []) if h.get("cusip")}

    top_out = []
    new_count = 0
    increased_count = 0
    priced_count = 0
    for h in top:
        entry = {
            "issuer": h["issuer"],
            "class": h["class"],
            "value": h["value_thousands"] * value_scale,
            "shares": h["shares"],
            "cusip": h["cusip"],
            "pct_of_portfolio": round((h["value_thousands"] / total_for_pct) * 100, 2) if total_for_pct else 0,
        }

        is_new = is_new_quarter and h["cusip"] and h["cusip"] not in prior_by_cusip
        entry["is_new"] = bool(is_new)

        is_increased = False
        if not is_new and is_new_quarter and h["cusip"] in prior_by_cusip:
            prior_shares = prior_by_cusip[h["cusip"]].get("shares") or 0
            if prior_shares > 0:
                shares_added = h["shares"] - prior_shares
                pct_share_increase = (shares_added / prior_shares) * 100
                # 15%+ more shares is the bar for "meaningfully increased" --
                # small share creep from options/DRIP-like adjustments isn't
                # worth flagging as a notable buy.
                if shares_added > 0 and pct_share_increase >= 15:
                    is_increased = True
                    entry["shares_added"] = shares_added
                    entry["pct_share_increase"] = round(pct_share_increase, 1)
        entry["is_increased"] = is_increased

        # Only spend API calls on genuinely new or meaningfully-increased
        # positions -- this is where a price estimate is actually meaningful.
        if (is_new or is_increased) and priced_count < 15:  # shared cap per fund per run
            ticker = cusip_to_ticker(h["cusip"], figi_cache)
            if ticker and re.fullmatch(r"[A-Za-z.\-]{1,6}", ticker):
                price_info = estimate_price_for_period(ticker, latest["period"])
                if price_info:
                    entry["price_estimate"] = price_info
            elif ticker:
                print(f"[debug] skipping non-equity-looking ticker '{ticker}' (likely a bond, not a stock)")
            priced_count += 1
            time.sleep(0.8)

        if is_new:
            new_count += 1
        if is_increased:
            increased_count += 1

        top_out.append(entry)

    row = {
        "fund_name": fund["name"],
        "person": fund["person"],
        "cik": fund["cik"],
        "period_end": latest["period"],
        "filed_date": latest["filed"],
        "portfolio_value": total_value,
        "num_holdings": len(holdings),
        "top_holdings": top_out,
        "source_accession": latest["accession"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    upsert_snapshot(row)
    print(f"[ok] {fund['name']}: period {latest['period']} value ${total_value:,.0f} ({len(holdings)} holdings, {new_count} new, {increased_count} increased, {priced_count} priced)")


def get_latest_snapshot_per_fund():
    """Fetch each fund's latest stored snapshot in one query."""
    url = f"{SUPABASE_URL}/rest/v1/fund_snapshots?select=cik,fund_name,period_end,portfolio_value,top_holdings&order=period_end.desc"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    latest = {}
    for row in r.json():
        if row["cik"] not in latest:
            latest[row["cik"]] = row
    return latest


def cohort_exists(period_end):
    url = f"{SUPABASE_URL}/rest/v1/locked_cohorts?period_end=eq.{period_end}&limit=1"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return len(r.json()) > 0


def upsert_locked_cohort_row(row):
    url = f"{SUPABASE_URL}/rest/v1/locked_cohorts?on_conflict=period_end,cusip"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    r = requests.post(url, headers=headers, data=json.dumps([row]), timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase upsert failed for locked_cohorts: {r.status_code} {r.text}")


def get_all_locked_cohort_rows():
    url = f"{SUPABASE_URL}/rest/v1/locked_cohorts?select=*"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def get_cohort_rows_for_period(period_end):
    url = f"{SUPABASE_URL}/rest/v1/locked_cohorts?period_end=eq.{period_end}&select=*"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_current_price(ticker):
    """Simple last-close lookup, used for locking day-0 price and for the
    daily refresh of tracked cohort stocks."""
    try:
        yahoo_ticker = normalize_ticker_for_yahoo(ticker)
        hist = yf.Ticker(yahoo_ticker).history(period="5d", auto_adjust=True)
        if hist.empty:
            return None
        return round(float(hist["Close"].iloc[-1]), 2)
    except Exception as e:
        print(f"[debug] fetch_current_price exception for {ticker}: {e}")
        return None


COHORT_LOCK_THRESHOLD = 9  # fixed count -- deliberately NOT proportional to
# len(FUNDS), so the bar stays exactly this strict even as more funds are
# added later, rather than getting looser as the fund count grows.


def gather_new_positions_by_cusip(rows):
    """Combine NEW positions by CUSIP across a set of fund snapshot rows."""
    by_cusip = {}
    for row in rows:
        for h in (row.get("top_holdings") or []):
            if not h.get("is_new") or not h.get("cusip"):
                continue
            price_est = h.get("price_estimate")
            if not price_est or not price_est.get("ticker"):
                continue
            ticker = price_est["ticker"]
            if not re.fullmatch(r"[A-Za-z.\-]{1,6}", ticker):
                continue  # skip bond-like non-equity tickers
            cusip = h["cusip"]
            if cusip not in by_cusip:
                by_cusip[cusip] = {
                    "issuer": h["issuer"],
                    "ticker": ticker,
                    "funds": [],
                    "combined_value": 0,
                    "avg_price_quarter": price_est.get("avg_price_quarter"),
                }
            by_cusip[cusip]["funds"].append(row["fund_name"])
            by_cusip[cusip]["combined_value"] += h.get("value", 0)
    return by_cusip


def check_and_lock_cohort():
    """Lock in the top NEW positions (combined by CUSIP where multiple funds
    independently bought the same stock) as a fixed cohort tracked forward
    from today's price, once at least COHORT_LOCK_THRESHOLD funds agree on
    the same latest quarter.

    If a cohort for that period already exists, this instead runs a
    "late-arrival top-up": any fund that has since caught up to that period
    gets merged into matching existing rows (updating funds/combined value,
    but never touching the original lock date/price -- that stays fixed as
    the honest historical record). Any stock a late fund uniquely qualifies
    for top-10-equivalent status on gets added as a brand new row, locked
    as of today -- not backdated to the original lock date, since today is
    genuinely when it became knowable.

    Returns a status dict so callers (e.g. the AI summary trigger) can tell
    what actually happened this run: {"locked_new": period_or_None, "topped_up": bool}.
    """
    status = {"locked_new": None, "topped_up": False}

    latest_by_fund = get_latest_snapshot_per_fund()
    if not latest_by_fund:
        return status

    period_counts = {}
    for row in latest_by_fund.values():
        period_counts[row["period_end"]] = period_counts.get(row["period_end"], 0) + 1
    target_period, count = max(period_counts.items(), key=lambda kv: kv[1])

    if count < COHORT_LOCK_THRESHOLD:
        print(f"[cohort] only {count} funds aligned on {target_period} (need {COHORT_LOCK_THRESHOLD}), skipping")
        return status

    contributing_rows = [row for row in latest_by_fund.values() if row["period_end"] == target_period]
    by_cusip = gather_new_positions_by_cusip(contributing_rows)

    if not cohort_exists(target_period):
        if not by_cusip:
            print(f"[cohort] no qualifying NEW positions with resolved tickers for {target_period}")
            return status
        top10 = sorted(by_cusip.items(), key=lambda kv: kv[1]["combined_value"], reverse=True)[:10]
        today = date.today().isoformat()
        locked_count = 0
        for cusip, info in top10:
            day0_price = fetch_current_price(info["ticker"])
            if day0_price is None:
                print(f"[cohort] could not get a lock price for {info['ticker']}, skipping")
                continue
            upsert_locked_cohort_row({
                "period_end": target_period,
                "cusip": cusip,
                "issuer": info["issuer"],
                "ticker": info["ticker"],
                "funds": info["funds"],
                "combined_value": info["combined_value"],
                "avg_price_quarter": info["avg_price_quarter"],
                "locked_date": today,
                "day0_price": day0_price,
                "current_price": day0_price,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            locked_count += 1
            time.sleep(0.5)
        total_known = len(latest_by_fund)
        print(f"[cohort] locked {locked_count} stocks for the {target_period} cohort ({count}/{total_known} funds reporting)")
        if locked_count > 0:
            status["locked_new"] = target_period
        return status

    # Cohort already exists for this period -- check for late-arrival top-up.
    existing_rows = get_cohort_rows_for_period(target_period)
    existing_by_cusip = {r["cusip"]: r for r in existing_rows}
    min_locked_value = min((r["combined_value"] for r in existing_rows), default=0)
    today = date.today().isoformat()
    updated = 0
    added = 0

    for cusip, info in by_cusip.items():
        if cusip in existing_by_cusip:
            row = existing_by_cusip[cusip]
            merged_funds = sorted(set(row["funds"]) | set(info["funds"]))
            if merged_funds != sorted(row["funds"]) or info["combined_value"] != row["combined_value"]:
                upsert_locked_cohort_row({
                    "period_end": row["period_end"],
                    "cusip": row["cusip"],
                    "issuer": row["issuer"],
                    "ticker": row["ticker"],
                    "funds": merged_funds,
                    "combined_value": info["combined_value"],
                    "avg_price_quarter": row["avg_price_quarter"],
                    "locked_date": row["locked_date"],
                    "day0_price": row["day0_price"],
                    "current_price": row["current_price"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                updated += 1
        elif info["combined_value"] > min_locked_value:
            day0_price = fetch_current_price(info["ticker"])
            if day0_price is None:
                continue
            upsert_locked_cohort_row({
                "period_end": target_period,
                "cusip": cusip,
                "issuer": info["issuer"],
                "ticker": info["ticker"],
                "funds": info["funds"],
                "combined_value": info["combined_value"],
                "avg_price_quarter": info["avg_price_quarter"],
                "locked_date": today,
                "day0_price": day0_price,
                "current_price": day0_price,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            added += 1
            time.sleep(0.5)

    if updated or added:
        print(f"[cohort] top-up for {target_period}: updated {updated} existing rows, added {added} late-qualifying rows (locked today, not backdated)")
        status["topped_up"] = True

    return status


def refresh_cohort_prices():
    """Every day, regardless of quarter, refresh current_price for every
    already-locked cohort stock across all quarters tracked so far."""
    rows = get_all_locked_cohort_rows()
    if not rows:
        return
    refreshed = 0
    for row in rows:
        new_price = fetch_current_price(row["ticker"])
        if new_price is None:
            continue
        upsert_locked_cohort_row({
            "period_end": row["period_end"],
            "cusip": row["cusip"],
            "issuer": row["issuer"],
            "ticker": row["ticker"],
            "funds": row["funds"],
            "combined_value": row["combined_value"],
            "avg_price_quarter": row["avg_price_quarter"],
            "locked_date": row["locked_date"],
            "day0_price": row["day0_price"],
            "current_price": new_price,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        refreshed += 1
        time.sleep(0.5)
    print(f"[cohort] refreshed prices for {refreshed}/{len(rows)} tracked cohort stocks")


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-3.5-flash"  # current GA model, free tier -- gemini-2.5-*
# models are now considered legacy/deprecated even though the pricing page
# doesn't show a hard shutdown banner yet; new API keys may not have access
# to the older generation, which is what caused repeated 404s here.
# Check ai.google.dev/gemini-api/docs/pricing if this model is ever renamed/deprecated.

BIG_MOVE_THRESHOLD = 15  # percentage points of change in the max cohort move
# since the last summary, before we bother regenerating just for a price move


def call_gemini(prompt, _retry=True):
    """A single free-tier-eligible Gemini call. Returns None (rather than
    raising) on any failure, since a missing AI summary should never break
    the rest of the daily pipeline.

    maxOutputTokens is generous (4096) because gemini-3.5-flash spends part
    of its token budget on internal "thinking" before writing the visible
    answer -- a small budget can cause the model to run out of tokens
    mid-sentence. We also explicitly check finishReason and reject anything
    that didn't finish cleanly, rather than silently saving truncated text.

    Retries once after a short pause on a transient server error (5xx) --
    those are Google-side hiccups, not something a bigger token budget or
    a different prompt would fix.
    """
    if not GEMINI_API_KEY:
        print("[ai] GEMINI_API_KEY not set, skipping AI summary")
        return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096},
        }
        r = requests.post(url, json=body, timeout=30)
        if r.status_code >= 500 and _retry:
            print(f"[ai] Gemini returned {r.status_code} (likely transient), retrying once after a short pause")
            time.sleep(5)
            return call_gemini(prompt, _retry=False)
        r.raise_for_status()
        data = r.json()
        candidate = data["candidates"][0]
        finish_reason = candidate.get("finishReason")
        if finish_reason not in (None, "STOP"):
            print(f"[ai] Gemini response did not finish cleanly (finishReason={finish_reason}), discarding")
            return None
        text = candidate.get("content", {}).get("parts", [{}])[0].get("text", "").strip()
        if not text:
            print("[ai] Gemini returned an empty summary, discarding")
            return None
        return text
    except Exception as e:
        print(f"[ai] Gemini call failed: {e}")
        return None


def get_last_summary(subject):
    url = f"{SUPABASE_URL}/rest/v1/ai_summaries?subject=eq.{subject}&order=generated_at.desc&limit=1"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def save_ai_summary(subject, summary_text, trigger_reason, max_move):
    url = f"{SUPABASE_URL}/rest/v1/ai_summaries"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    row = {
        "subject": subject,
        "summary_text": summary_text,
        "trigger_reason": trigger_reason,
        "max_move_at_generation": max_move,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    r = requests.post(url, headers=headers, data=json.dumps([row]), timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase insert failed for ai_summaries: {r.status_code} {r.text}")


def get_max_cohort_move():
    rows = get_all_locked_cohort_rows()
    moves = [
        abs((r["current_price"] - r["day0_price"]) / r["day0_price"] * 100)
        for r in rows if r.get("day0_price")
    ]
    return max(moves) if moves else 0.0


DASHBOARD_PROMPT = """You are summarizing institutional 13F stock-holding disclosures for a public dashboard. Below is a data digest of this quarter's portfolio changes across several hedge funds. Write a short, factual summary (4-6 sentences, plain prose, no bullet points, no markdown) that:
- Describes what funds did (bought, added to, or exited positions) using the specific fund names and figures given
- Points out any notable cross-fund patterns, e.g. multiple funds acting on the same stock in the same direction, or in opposite directions
- Never recommends any action, never says "consider buying/selling", never predicts future price performance
- Stays strictly within the data provided -- do not invent any figures, holdings, or funds not listed below
- Ends with a brief plain-language reminder that this reflects historical, lagged disclosures, not investment advice
- HARD LIMIT: your entire response must be under 120 words. Prioritize the single most notable pattern over covering every fund if you have to choose.

DATA DIGEST:
{digest}
"""

RESEARCH_PROMPT = """You are summarizing a hedge-fund research tracking page for a public dashboard. Below is a digest of a "locked cohort" of stocks tracked since they were first flagged as new institutional buys, along with their price performance since tracking began, plus any newly-emerging multi-fund consensus picks. Write a short, factual summary (4-6 sentences, plain prose, no bullet points, no markdown) that:
- Describes how the tracked cohort has performed using the specific tickers and percentages given
- Highlights standout movers (best and/or worst performers) factually, by name and number
- Notes any new multi-fund consensus patterns from the digest
- Never recommends any action, never says "consider buying/selling", never predicts future price performance
- Stays strictly within the data provided -- do not invent any figures or tickers not listed below
- Ends with a brief plain-language reminder that this reflects historical tracking, not investment advice
- HARD LIMIT: your entire response must be under 120 words. Prioritize the single best and single worst performer over covering every ticker if you have to choose.

DATA DIGEST:
{digest}
"""


def build_dashboard_digest(latest_by_fund):
    """Raw per-fund NEW/INCREASED/EXIT summary -- deliberately left fairly
    raw (not pre-categorized) so the AI does its own cross-fund pattern
    spotting rather than just restating a category we already computed."""
    lines = []
    for row in latest_by_fund.values():
        holdings = row.get("top_holdings") or []
        new = [h for h in holdings if h.get("is_new")][:5]
        increased = [h for h in holdings if h.get("is_increased")][:5]
        lines.append(f"\n{row['fund_name']} (AUM ${row['portfolio_value']:,.0f}, period {row['period_end']}):")
        if new:
            lines.append("  NEW: " + "; ".join(f"{h['issuer']} ({h['pct_of_portfolio']}% of book)" for h in new))
        if increased:
            lines.append("  INCREASED: " + "; ".join(f"{h['issuer']} (+{h.get('pct_share_increase', '?')}% shares)" for h in increased))
        if not new and not increased:
            lines.append("  No new or increased positions this quarter.")
    return "\n".join(lines)


def build_research_digest():
    """Locked cohort performance plus a lightweight from-scratch consensus
    scan (funds independently buying the same stock this quarter)."""
    cohort_rows = get_all_locked_cohort_rows()
    lines = ["LOCKED COHORT PERFORMANCE:"]
    for r in sorted(cohort_rows, key=lambda x: -abs((x["current_price"] - x["day0_price"]) / x["day0_price"])) if cohort_rows else []:
        pct = (r["current_price"] - r["day0_price"]) / r["day0_price"] * 100
        lines.append(f"  {r['issuer']} ({r['ticker']}), funds: {', '.join(r['funds'])}: locked ${r['day0_price']} on {r['locked_date']} -> now ${r['current_price']} ({pct:+.1f}%)")
    if not cohort_rows:
        lines.append("  No cohort locked yet.")

    latest_by_fund = get_latest_snapshot_per_fund()
    by_cusip = {}
    for row in latest_by_fund.values():
        for h in (row.get("top_holdings") or []):
            if not h.get("cusip") or not (h.get("is_new") or h.get("is_increased")):
                continue
            if h["cusip"] not in by_cusip:
                by_cusip[h["cusip"]] = {"issuer": h["issuer"], "funds": []}
            by_cusip[h["cusip"]]["funds"].append(row["fund_name"])
    consensus = [v for v in by_cusip.values() if len(v["funds"]) >= 2]
    lines.append("\nEMERGING MULTI-FUND CONSENSUS THIS QUARTER:")
    if consensus:
        for c in consensus[:10]:
            lines.append(f"  {c['issuer']}: {', '.join(c['funds'])}")
    else:
        lines.append("  None this quarter.")

    return "\n".join(lines)


def maybe_generate_ai_summaries(latest_by_fund, cohort_status):
    """Only regenerate AI summaries when something meaningful actually
    happened -- a new quarter locking, a late-arrival top-up, or the
    tracked cohort's price move shifting significantly -- not on every
    single daily run, which would burn API calls for no new information."""
    current_max_move = get_max_cohort_move()

    last_dashboard = get_last_summary("dashboard")
    last_research = get_last_summary("research")

    dashboard_trigger = None
    if cohort_status["locked_new"]:
        dashboard_trigger = f"new quarter locked: {cohort_status['locked_new']}"
    elif not last_dashboard:
        dashboard_trigger = "initial summary"
    elif abs(current_max_move - (last_dashboard.get("max_move_at_generation") or 0)) >= BIG_MOVE_THRESHOLD:
        dashboard_trigger = f"big move: max cohort move shifted to {current_max_move:.1f}%"

    research_trigger = None
    if cohort_status["locked_new"]:
        research_trigger = f"new quarter locked: {cohort_status['locked_new']}"
    elif cohort_status["topped_up"]:
        research_trigger = "cohort top-up occurred"
    elif not last_research:
        research_trigger = "initial summary"
    elif abs(current_max_move - (last_research.get("max_move_at_generation") or 0)) >= BIG_MOVE_THRESHOLD:
        research_trigger = f"big move: max cohort move shifted to {current_max_move:.1f}%"

    if dashboard_trigger:
        digest = build_dashboard_digest(latest_by_fund)
        summary = call_gemini(DASHBOARD_PROMPT.format(digest=digest))
        if summary:
            save_ai_summary("dashboard", summary, dashboard_trigger, current_max_move)
            print(f"[ai] generated dashboard summary ({dashboard_trigger})")

    if research_trigger:
        digest = build_research_digest()
        summary = call_gemini(RESEARCH_PROMPT.format(digest=digest))
        if summary:
            save_ai_summary("research", summary, research_trigger, current_max_move)
            print(f"[ai] generated research summary ({research_trigger})")


def main():
    figi_cache = {}

    for fund in FUNDS:
        try:
            filings = recent_13f_filings(fund["cik"], n=2)
            if not filings:
                print(f"[skip] no 13F-HR found for {fund['name']}")
                continue

            # Process oldest first so it's already stored as the "prior"
            # snapshot by the time we process the newer one.
            for filing in reversed(filings):
                process_filing(fund, filing, figi_cache)
                time.sleep(0.3)  # be polite to SEC's rate limits
        except Exception as e:
            print(f"[error] {fund['name']}: {e}")

    try:
        cohort_status = check_and_lock_cohort()
    except Exception as e:
        print(f"[error] cohort lock check failed: {e}")
        cohort_status = {"locked_new": None, "topped_up": False}

    try:
        refresh_cohort_prices()
    except Exception as e:
        print(f"[error] cohort price refresh failed: {e}")

    try:
        maybe_generate_ai_summaries(get_latest_snapshot_per_fund(), cohort_status)
    except Exception as e:
        print(f"[error] AI summary generation failed: {e}")


if __name__ == "__main__":
    main()
