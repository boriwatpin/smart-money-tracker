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
]

TOP_N_HOLDINGS = 25


def get_prior_snapshot(cik):
    """Look up whatever snapshot is already stored for this fund, if any.

    Used to (a) detect which holdings are new this quarter and (b) avoid
    re-doing price lookups on days where nothing changed.
    """
    url = f"{SUPABASE_URL}/rest/v1/fund_snapshots?cik=eq.{cik}&order=period_end.desc&limit=1"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def cusip_to_ticker(cusip, cache):
    """Map a CUSIP to a ticker symbol via OpenFIGI's free public endpoint.

    13F filings only disclose CUSIPs, not tickers. This lookup can fail for
    thinly-traded securities, foreign issuers, or CUSIPs OpenFIGI doesn't
    recognize -- that's expected and handled by the caller.
    """
    if cusip in cache:
        return cache[cusip]
    try:
        r = requests.post(
            "https://api.openfigi.com/v3/mapping",
            json=[{"idType": "ID_CUSIP", "idValue": cusip}],
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        result = r.json()
        ticker = None
        if result and isinstance(result, list) and result[0].get("data"):
            ticker = result[0]["data"][0].get("ticker")
        cache[cusip] = ticker
        return ticker
    except Exception:
        cache[cusip] = None
        return None


def quarter_start_iso(period_end_str):
    d = date.fromisoformat(period_end_str)
    q_month = ((d.month - 1) // 3) * 3 + 1
    return date(d.year, q_month, 1).isoformat()


def fetch_daily_closes(ticker):
    """Pull daily closing prices from Stooq (free, no API key required)."""
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    lines = r.text.strip().splitlines()
    rows = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            rows.append({"date": parts[0], "close": float(parts[4])})
        except ValueError:
            continue
    return rows


def estimate_new_position_price(ticker, period_end_str):
    """Rough entry-price estimate for a brand-new position: the average of
    daily closing prices during the quarter the fund reported buying it,
    compared to the most recent close available.

    This is necessarily an approximation -- SEC filings don't disclose
    which day(s) within the quarter a fund actually bought, so this
    assumes even buying across the quarter rather than a single trade.
    """
    try:
        series = fetch_daily_closes(ticker)
        if not series:
            return None
        q_start = quarter_start_iso(period_end_str)
        in_quarter = [row["close"] for row in series if q_start <= row["date"] <= period_end_str]
        if not in_quarter:
            return None
        avg_price = sum(in_quarter) / len(in_quarter)
        current_price = series[-1]["close"]
        pct_change = ((current_price - avg_price) / avg_price) * 100 if avg_price else None
        return {
            "ticker": ticker,
            "avg_price_quarter": round(avg_price, 2),
            "current_price": round(current_price, 2),
            "pct_change_since_avg": round(pct_change, 2) if pct_change is not None else None,
            "price_as_of": series[-1]["date"],
        }
    except Exception:
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

    # Figure out which of these are genuinely new vs. whatever's already stored.
    prior = get_prior_snapshot(fund["cik"])
    is_new_quarter = prior is not None and prior.get("period_end") != latest["period"]
    prior_cusips = set()
    if is_new_quarter:
        prior_cusips = {h.get("cusip") for h in (prior.get("top_holdings") or []) if h.get("cusip")}

    top_out = []
    new_count = 0
    for h in top:
        entry = {
            "issuer": h["issuer"],
            "class": h["class"],
            "value": h["value_thousands"] * 1000,
            "shares": h["shares"],
            "cusip": h["cusip"],
            "pct_of_portfolio": round((h["value_thousands"] / total_thousands) * 100, 2) if total_thousands else 0,
        }

        is_new = is_new_quarter and h["cusip"] and h["cusip"] not in prior_cusips
        entry["is_new"] = bool(is_new)

        # Only spend API calls on genuinely new positions -- this is
        # where a price estimate is actually meaningful (see README).
        if is_new and new_count < 15:  # cap per fund per run, be a good API citizen
            ticker = cusip_to_ticker(h["cusip"], figi_cache)
            if ticker:
                price_info = estimate_new_position_price(ticker, latest["period"])
                if price_info:
                    entry["price_estimate"] = price_info
            new_count += 1
            time.sleep(0.25)

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
    print(f"[ok] {fund['name']}: period {latest['period']} value ${total_value:,.0f} ({len(holdings)} holdings, {new_count} new positions checked)")


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


if __name__ == "__main__":
    main()
