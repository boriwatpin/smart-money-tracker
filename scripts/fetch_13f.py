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
from datetime import datetime, timezone

SEC_HEADERS = {
    "User-Agent": os.environ.get("SEC_USER_AGENT", "SmartMoneyTracker contact@example.com"),
    "Accept-Encoding": "gzip, deflate",
}

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

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

TOP_N_HOLDINGS = 10


def get_json(url):
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def get_text(url):
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def latest_13f(cik):
    """Find the most recent 13F-HR filing for a CIK from the submissions feed."""
    data = get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    recent = data["filings"]["recent"]
    for i, form in enumerate(recent["form"]):
        if form == "13F-HR":
            return {
                "accession": recent["accessionNumber"][i],
                "filed": recent["filingDate"][i],
                "period": recent["reportDate"][i],
            }
    return None


def find_infotable_url(cik, accession):
    """The holdings table ships as a separate XML file inside the filing folder."""
    acc_nodash = accession.replace("-", "")
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/index.json"
    idx = get_json(idx_url)
    items = idx["directory"]["item"]
    for item in items:
        if "infotable" in item["name"].lower() and item["name"].lower().endswith(".xml"):
            return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{item['name']}"
    # fallback: some older filings name it differently
    for item in items:
        name = item["name"].lower()
        if name.endswith(".xml") and "primary_doc" not in name:
            return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{item['name']}"
    return None


def parse_infotable(xml_text):
    """Strip namespaces (SEC's XML uses them inconsistently) and pull each holding row.

    SEC's 13F infotable XML often declares a namespace prefix (e.g. ns1:infoTable)
    at the root and uses it throughout the body. Just removing
