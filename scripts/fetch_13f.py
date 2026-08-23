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
    {"name": "Pershing Square Capital Management", "person": "Bill Ackman", "cik": "0001336528"},
    {"name": "Tiger Global Management", "person": "Chase Coleman", "cik": "0001167483"},
    {"name": "Third Point", "person": "Dan Loeb", "cik": "0001040273"},
    {"name": "Soros Fund Management", "person": "Dawn Fitzpatrick", "cik": "0001029160"},
    {"name": "Point72 Asset Management", "person": "Steven Cohen", "cik": "0001603466"},
    {"name": "Millennium Management", "person": "Israel Englander", "cik": "0001273087"},
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

    total_thousands = sum(h["value_thousands"] for h in holdings)
    total_value = total_thousands * 1000

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
            "value": h["value_thousands"] * 1000,
            "shares": h["shares"],
            "cusip": h["cusip"],
            "pct_of_portfolio": round((h["value_thousands"] / total_thousands) * 100, 2) if total_thousands else 0,
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
    url = f"{SUPABASE_URL}/rest/v1/fund_snapshots?select=cik,fund_name,period_end,top_holdings&order=period_end.desc"
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


def check_and_lock_cohort():
    """Once all 10 funds report the same latest quarter, lock in the top 10
    NEW positions (combined by CUSIP where multiple funds independently
    bought the same stock) as a fixed cohort tracked forward from today's
    price. Only ever locks once per quarter -- if a cohort already exists
    for a period, this does nothing.
    """
    latest_by_fund = get_latest_snapshot_per_fund()
    if len(latest_by_fund) < len(FUNDS):
        print(f"[cohort] only {len(latest_by_fund)}/{len(FUNDS)} funds have any data yet, skipping")
        return

    periods = set(row["period_end"] for row in latest_by_fund.values())
    if len(periods) > 1:
        print(f"[cohort] funds not yet aligned on the same latest quarter ({sorted(periods)}), skipping lock check")
        return

    period = periods.pop()
    if cohort_exists(period):
        return  # already locked, nothing to do

    by_cusip = {}
    for row in latest_by_fund.values():
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

    if not by_cusip:
        print(f"[cohort] no qualifying NEW positions with resolved tickers for {period}")
        return

    top10 = sorted(by_cusip.items(), key=lambda kv: kv[1]["combined_value"], reverse=True)[:10]

    today = date.today().isoformat()
    locked_count = 0
    for cusip, info in top10:
        day0_price = fetch_current_price(info["ticker"])
        if day0_price is None:
            print(f"[cohort] could not get a lock price for {info['ticker']}, skipping")
            continue
        upsert_locked_cohort_row({
            "period_end": period,
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

    print(f"[cohort] locked {locked_count} stocks for the {period} cohort")


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
        check_and_lock_cohort()
    except Exception as e:
        print(f"[error] cohort lock check failed: {e}")

    try:
        refresh_cohort_prices()
    except Exception as e:
        print(f"[error] cohort price refresh failed: {e}")


if __name__ == "__main__":
    main()
