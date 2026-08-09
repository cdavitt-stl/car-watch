#!/usr/bin/env python3
"""
Car Watch — twice-daily used-car monitor (GitHub Actions edition).

fetch listings (MarketCheck) -> dedupe by VIN (SQLite) -> deal-score ->
write a Google Sheet: "Current" (full rewrite) + "Log" (append-only).

Secrets come from env vars only — never from config.yaml:
  MARKETCHECK_API_KEY          MarketCheck API key
  GOOGLE_SERVICE_ACCOUNT_JSON  the whole service-account JSON file, as a string
  SHEET_ID                     target spreadsheet key

MarketCheck request params and response fields were verified against
https://docs.marketcheck.com/docs/api/cars/inventory/inventory-search
See CLAUDE.md -> "MarketCheck schema verification" for the field-by-field notes.
"""

import json
import os
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

import gspread
import requests
import yaml

ROOT = Path(__file__).parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
DB_PATH = ROOT / "seen.db"

# Host per docs: GET https://api.marketcheck.com/v2/search/car/active
API_URL = "https://api.marketcheck.com/v2/search/car/active"

# `rows` is documented with a maximum of 50 (default 10), so page to get a
# complete cohort — the deal score's medians are only meaningful if the result
# set isn't an arbitrary 50-row slice.
PAGE_ROWS = 50
MAX_ROWS_PER_VEHICLE = 200

CURRENT_TAB = "Current"
LOG_TAB = "Log"
CURRENT_HEADERS = ["Score", "Year", "Model", "Trim", "Price", "Miles",
                   "Days Listed", "Dealer", "Distance", "Status", "Link"]
LOG_HEADERS = ["Timestamp (UTC)", "Listings", "New", "Price Drops"]

CHEAP = "CHEAP — CHECK HISTORY"
STAR = "★ "

# Lowercased once at import. Blank entries are dropped — "" is a substring of
# every string, so a stray empty list item would star every row.
PREFERRED_DEALERS = [
    str(d).strip().lower()
    for d in (CONFIG.get("preferred_dealers") or [])
    if str(d).strip()
]


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS seen (
        vin TEXT PRIMARY KEY,
        first_seen TEXT,
        last_seen TEXT,
        first_price REAL,
        last_price REAL
    )""")
    return con


def _normalize(item):
    build = item.get("build") or {}
    dealer = item.get("dealer") or {}
    return {
        "vin": item.get("vin", ""),
        "year": build.get("year", ""),
        "make": build.get("make", ""),
        "model": build.get("model", ""),
        "trim": build.get("trim") or "",
        "price": float(item.get("price") or 0),
        "miles": float(item.get("miles") or 0),
        "days_listed": item.get("dom", ""),
        # `or ""` not `get(..., "")`: an explicit JSON null would sail past a
        # default and land as None, which breaks the dealer match downstream.
        "seller": dealer.get("name") or "",
        "city": dealer.get("city") or "",
        "distance": item.get("dist", ""),
        "url": item.get("vdp_url", ""),
    }


def _fetch_one(key, vehicle):
    """Page through active listings for a single make/model."""
    out, start = [], 0
    while start < MAX_ROWS_PER_VEHICLE:
        params = {
            "api_key": key,
            "make": vehicle["make"],
            "model": vehicle["model"],
            "zip": CONFIG["zip"],
            "radius": CONFIG["radius_miles"],
            "price_range": f"0-{CONFIG['max_price']}",
            "miles_range": f"0-{CONFIG['max_miles']}",
            "car_type": "used",
            "sort_by": "miles",
            "sort_order": "asc",
            "rows": PAGE_ROWS,
            "start": start,
        }
        r = requests.get(API_URL, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()
        listings = payload.get("listings") or []
        out.extend(_normalize(item) for item in listings)

        start += PAGE_ROWS
        if not listings or start >= payload.get("num_found", 0):
            break
    return out


def fetch_listings():
    """One paged search per vehicle in config. Returns normalized dicts."""
    key = os.environ["MARKETCHECK_API_KEY"]
    results = []
    for v in CONFIG["vehicles"]:
        results.extend(_fetch_one(key, v))

    # Drop anything without a VIN or price, and collapse duplicate VINs —
    # paging can overlap, and a duplicate VIN would break the seen.db insert.
    seen, clean = set(), []
    for x in results:
        if not x["vin"] or x["price"] <= 0 or x["vin"] in seen:
            continue
        seen.add(x["vin"])
        clean.append(x)
    return clean


def score_deals(listings):
    """Transparent deal score: dollars under cohort median (in $1000s)
    plus miles under cohort median (in 10,000s). Keep it explainable."""
    if not listings:
        return listings
    med_price = statistics.median(x["price"] for x in listings)
    med_miles = statistics.median(x["miles"] for x in listings)
    for x in listings:
        x["deal_score"] = round(
            (med_price - x["price"]) / 1000 + (med_miles - x["miles"]) / 10000, 1
        )
    listings.sort(key=lambda x: x["deal_score"], reverse=True)
    return listings


def diff_and_store(con, listings):
    """Return (new_listings, price_drops); update seen.db."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new, drops = [], []
    for x in listings:
        row = con.execute(
            "SELECT first_price, last_price FROM seen WHERE vin=?", (x["vin"],)
        ).fetchone()
        if row is None:
            x["change"] = "NEW"
            new.append(x)
            con.execute(
                "INSERT INTO seen VALUES (?,?,?,?,?)",
                (x["vin"], now, now, x["price"], x["price"]),
            )
        else:
            if x["price"] < row[1]:
                x["change"] = "PRICE DROP"
                x["old_price"] = row[1]
                drops.append(x)
            else:
                x["change"] = ""
            con.execute(
                "UPDATE seen SET last_seen=?, last_price=? WHERE vin=?",
                (now, x["price"], x["vin"]),
            )
    con.commit()
    return new, drops


def apply_status(listings):
    """Status column: NEW / PRICE DROP / — plus the under-min_price_flag mark.

    A cheap car can also be new or a price drop, so both signals are kept
    rather than one masking the other.
    """
    floor = CONFIG.get("min_price_flag")
    for x in listings:
        parts = [p for p in (x.get("change"),) if p]
        if floor and x["price"] < floor:
            parts.append(CHEAP)
        x["status"] = " · ".join(parts) if parts else "—"


def is_preferred(name):
    """Case-insensitive substring match against config's preferred_dealers.

    Tolerates a missing, empty, or non-string dealer name — the feed does
    occasionally omit dealer.name, and that must not take down a run.
    """
    if not name or not isinstance(name, str):
        return False
    lowered = name.lower()
    return any(p in lowered for p in PREFERRED_DEALERS)


def open_sheet():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise SystemExit("GOOGLE_SERVICE_ACCOUNT_JSON is not set.")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")

    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        raise SystemExit("SHEET_ID is not set.")

    gc = gspread.service_account_from_dict(info)
    return gc.open_by_key(sheet_id)


def _tab(sheet, title, headers, rows_hint=200):
    """Fetch or create a worksheet, guaranteeing a header row."""
    try:
        ws = sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=title, rows=rows_hint, cols=len(headers))
    if not ws.row_values(1):
        ws.update(range_name="A1", values=[headers],
                  value_input_option="USER_ENTERED")
    return ws


def write_current(sheet, listings):
    """Full rewrite, already sorted by deal score (highest first)."""
    ws = _tab(sheet, CURRENT_TAB, CURRENT_HEADERS,
              rows_hint=max(len(listings) + 20, 200))
    rows = [CURRENT_HEADERS]
    for x in listings:
        dealer = f"{x['seller']} ({x['city']})" if x["city"] else x["seller"]
        if is_preferred(x["seller"]):
            dealer = STAR + dealer
        rows.append([
            x["deal_score"], x["year"], x["model"], x["trim"],
            x["price"], x["miles"], x["days_listed"],
            dealer, x["distance"], x["status"], x["url"],
        ])
    ws.clear()
    ws.update(range_name="A1", values=rows, value_input_option="USER_ENTERED")


def append_log(sheet, listings, new, drops):
    ws = _tab(sheet, LOG_TAB, LOG_HEADERS)
    ws.append_row(
        [datetime.now(timezone.utc).isoformat(timespec="seconds"),
         len(listings), len(new), len(drops)],
        value_input_option="USER_ENTERED",
    )


def main():
    con = init_db()
    listings = score_deals(fetch_listings())
    new, drops = diff_and_store(con, listings)
    apply_status(listings)

    sheet = open_sheet()
    write_current(sheet, listings)
    append_log(sheet, listings, new, drops)

    print(f"{len(listings)} listings · {len(new)} new · {len(drops)} drops "
          f"→ sheet {os.environ['SHEET_ID']}")


if __name__ == "__main__":
    main()
