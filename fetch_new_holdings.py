#!/usr/bin/env python3
"""
fetch_new_holdings.py
=====================
Downloads the latest monthly portfolio disclosures from AMCs that have
automated downloaders, then parses them into holdings/{YYYY-MM}.parquet.

Supported AMCs (with direct file discovery):
  - Mirae Asset  (AjaxService API → xlsx)
  - HDFC         (S3 URL pattern  → xlsx)

For other AMCs (DSP, Nippon, ABSL, Kotak, etc.) holdings are already
backfilled via ingest_absl_kotak.py / backfill_holdings.py and updated
manually from AMC zip archives when new data is available.

Usage:
    python fetch_new_holdings.py            # download + parse all
    python fetch_new_holdings.py --amc mirae
    python fetch_new_holdings.py --amc hdfc
    python fetch_new_holdings.py --parse-only   # skip download, re-parse existing raw files
    python fetch_new_holdings.py --data-dir /path/to/mf_data
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import openpyxl
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("fundlens.holdings")

ISIN_RE = re.compile(r'^IN[A-Z0-9]{10}$')


# ── MIRAE ─────────────────────────────────────────────────────────────────────

MIRAE_FUND_MAP = {
    "mirae_large_cap":           (118825, "Mirae Asset Large Cap Fund - Direct Plan - Growth",         "Mirae"),
    "mirae_large_midcap":        (118834, "Mirae Asset Large & Midcap Fund - Direct Plan - Growth",    "Mirae"),
    "mirae_elss":                (135781, "Mirae Asset ELSS Tax Saver Fund - Direct Plan - Growth",    "Mirae"),
    "mirae_equity_savings":      (145693, "Mirae Asset Equity Savings Fund- Direct Plan- Growth",      "Mirae"),
    "mirae_focused":             (147206, "Mirae Asset Focused Fund Direct Plan Growth",               "Mirae"),
    "mirae_mid_cap":             (147445, "Mirae Asset Midcap Fund- Direct Growth Option",             "Mirae"),
    "mirae_balanced_advantage":  (150470, "Mirae Asset Balanced Advantage Fund Direct Plan- Growth",   "Mirae"),
    "mirae_flexi_cap":           (151412, "Mirae Asset Flexi Cap Fund - Direct Plan - Growth",         "Mirae"),
    "mirae_multicap":            (151810, "Mirae Asset Multicap Fund - Direct Plan - Growth",          "Mirae"),
    "mirae_small_cap":           (153196, "Mirae Asset Small Cap Fund - Direct Plan - Growth",         "Mirae"),
    "mirae_aggressive_hybrid":   (None,   "Mirae Asset Aggressive Hybrid Fund Direct Plan Growth",    "Mirae"),
    "mirae_banking_fin":         (None,   "Mirae Asset Banking and Financial Services Fund Direct",    "Mirae"),
    "mirae_consumer":            (None,   "Mirae Asset Great Consumer Fund Direct Plan Growth",        "Mirae"),
    "mirae_healthcare":          (None,   "Mirae Asset Healthcare Fund Direct Plan Growth",            "Mirae"),
    "mirae_infrastructure":      (None,   "Mirae Asset Infrastructure Fund Direct Plan Growth",        "Mirae"),
    "mirae_multi_asset":         (None,   "Mirae Asset Multi Asset Allocation Fund Direct Plan",       "Mirae"),
}

MIRAE_EQUITY_FUNDS = {
    "Large Cap Fund":                      "mirae_large_cap",
    "Large & Midcap Fund":                 "mirae_large_midcap",
    "Flexi Cap Fund":                      "mirae_flexi_cap",
    "Mid Cap Fund":                        "mirae_mid_cap",
    "Midcap Fund":                         "mirae_mid_cap",
    "Small Cap Fund":                      "mirae_small_cap",
    "ELSS Tax Saver Fund":                 "mirae_elss",
    "Focused Fund":                        "mirae_focused",
    "Multicap Fund":                       "mirae_multicap",
    "Equity Savings Fund":                 "mirae_equity_savings",
    "Balanced Advantage Fund":             "mirae_balanced_advantage",
    "Aggressive Hybrid Fund":              "mirae_aggressive_hybrid",
    "Banking and Financial Services Fund": "mirae_banking_fin",
    "Great Consumer Fund":                 "mirae_consumer",
    "Healthcare Fund":                     "mirae_healthcare",
    "Infrastructure Fund":                 "mirae_infrastructure",
    "Multi Asset Allocation Fund":         "mirae_multi_asset",
}

MONTH_MAP = {
    "january":"01","february":"02","march":"03","april":"04","may":"05",
    "june":"06","july":"07","august":"08","september":"09","october":"10",
    "november":"11","december":"12",
    "jan":"01","feb":"02","mar":"03","apr":"04","jun":"06","jul":"07",
    "aug":"08","sep":"09","oct":"10","nov":"11","dec":"12",
}

DEBT_KEYWORDS = ['DEBT','MONEY MARKET','CASH','NET ASSETS','GRAND TOTAL',
                 'MUTUAL FUND','CERTIFICATE','COMMERCIAL PAPER','TREASURY',
                 'GOVERNMENT','SECURIT','REPO ','CBLO','TOTAL ASSETS']


def mirae_parse_date(title: str) -> str | None:
    m = re.search(r'as on\s+\d+\w*\s+(\w+)\s+(\d{4})', title, re.I)
    if m:
        month = MONTH_MAP.get(m.group(1).lower()[:3])
        if month:
            return f"{m.group(2)}-{month}"
    return None


def mirae_fetch_all_urls(session: requests.Session) -> list[dict]:
    BASE = "https://www.miraeassetmf.co.in"
    URL  = f"{BASE}/AjaxService/GetDownloadsData"
    all_items, pgno, pgsize = [], 1, 200
    while True:
        r = session.post(URL, json={"request":{"modulename":"portfolio_tab1","pgno":pgno,"pgsize":pgsize}}, timeout=20)
        data = r.json()
        if data.get("ReturnCode") != "0":
            break
        items = data.get("Data", [])
        if not items:
            break
        all_items.extend(items)
        if len(all_items) >= data.get("DataCount", 0):
            break
        pgno += 1
        time.sleep(0.15)
    return all_items


def mirae_download_new(raw_dir: Path) -> list[tuple[str,str,Path]]:
    """
    Discover & download any new Mirae files not already on disk.
    Returns list of (slug, date_str, path) for files that were downloaded.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer":    "https://www.miraeassetmf.co.in/downloads/portfolio",
        "Content-Type": "application/json;charset=utf-8",
        "Accept":     "application/json",
    })

    log.info("  Mirae: fetching file catalogue from AjaxService…")
    all_items = mirae_fetch_all_urls(session)
    log.info(f"  Mirae: {len(all_items)} total entries in catalogue")

    BASE = "https://www.miraeassetmf.co.in"
    targets = {}
    for item in all_items:
        title = item.get("Title","")
        url   = item.get("URL","")
        for fund_name, slug in MIRAE_EQUITY_FUNDS.items():
            if fund_name in title:
                dt = mirae_parse_date(title)
                if dt and (slug, dt) not in targets:
                    targets[(slug, dt)] = BASE + url if url.startswith("/") else url

    new_files = []
    def _dl(slug_dt_url):
        slug, dt, url = slug_dt_url
        fund_dir = raw_dir / slug
        fund_dir.mkdir(parents=True, exist_ok=True)
        ext = url.rsplit(".",1)[-1].split("?")[0].lower()
        out  = fund_dir / f"{dt}.{ext}"
        if out.exists() and out.stat().st_size > 1000:
            return None
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                out.write_bytes(r.content)
                return (slug, dt, out)
        except Exception as e:
            log.warning(f"  Mirae download failed {slug} {dt}: {e}")
        return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(_dl, (slug, dt, url)) for (slug, dt), url in targets.items()]
        for fut in as_completed(futs):
            res = fut.result()
            if res:
                new_files.append(res)
                log.info(f"  Mirae: downloaded {res[0]} {res[1]}")

    log.info(f"  Mirae: {len(new_files)} new files downloaded ({len(targets)-len(new_files)} already cached)")
    return new_files


def parse_mirae_xlsx(path: Path, scheme_code, scheme_name: str, amc: str) -> list[dict]:
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        rows = list(wb.active.iter_rows(values_only=True))
        wb.close()
    except Exception as e:
        log.warning(f"  ERR loading {path.name}: {e}")
        return []

    holdings, in_equity = [], False
    for row in rows:
        if not row or len(row) < 3:
            continue
        cell1 = str(row[1]).strip() if row[1] else ""
        if 'EQUITY' in cell1.upper() and 'RELATED' in cell1.upper():
            in_equity = True; continue
        if 'LISTED' in cell1.upper() and 'AWAITING' in cell1.upper():
            continue
        if in_equity and cell1:
            upper = cell1.upper()
            if any(k in upper for k in DEBT_KEYWORDS):
                in_equity = False; continue
            if re.match(r'^\([B-Z]\)', upper):
                in_equity = False; continue
        if not in_equity or len(row) < 7:
            continue
        isin = str(row[2]).strip() if row[2] else ""
        if not ISIN_RE.match(isin):
            continue
        try:
            pct_nav = float(row[6])
        except (ValueError, TypeError):
            continue
        if pct_nav <= 0:
            continue
        holdings.append({
            "isin": isin,
            "stock_name": str(row[1]).strip() if row[1] else "",
            "pct_nav": pct_nav,
            "sector": str(row[3]).strip() if row[3] else "",
            "scheme_code": scheme_code,
            "scheme_name": scheme_name,
            "amc": amc,
        })
    return holdings


def ingest_mirae(raw_dir: Path, hold_dir: Path) -> dict[str, list[dict]]:
    results: dict[str, list] = {}
    for slug_dir in sorted(raw_dir.iterdir()):
        slug = slug_dir.name
        if slug not in MIRAE_FUND_MAP:
            continue
        scheme_code, scheme_name, amc = MIRAE_FUND_MAP[slug]
        for path in sorted(slug_dir.glob("*.xlsx")):
            dt = path.stem
            if not re.match(r'^\d{4}-\d{2}$', dt):
                continue
            rows = parse_mirae_xlsx(path, scheme_code, scheme_name, amc)
            if rows:
                results.setdefault(dt, []).extend(rows)
    return results


# ── HDFC ──────────────────────────────────────────────────────────────────────

HDFC_FUNDS = {
    "118989": ("HDFC Large Cap Fund - Growth Option - Direct Plan",          "HDFC", "LargeCap"),
    "119026": ("HDFC Mid-Cap Opportunities Fund - Growth - Direct Plan",     "HDFC", "MidCap"),
    "119046": ("HDFC Small Cap Fund - Growth Option - Direct Plan",          "HDFC", "SmallCap"),
    "118988": ("HDFC Flexi Cap Fund - Growth Option - Direct Plan",          "HDFC", "FlexiCap"),
    "118994": ("HDFC Large and Mid Cap Fund - Growth Option - Direct Plan",  "HDFC", "LargeMidCap"),
    "120505": ("HDFC Multi Cap Fund - Growth Option - Direct Plan",          "HDFC", "MultiCap"),
    "122639": ("HDFC Focused 30 Fund - Growth Option - Direct Plan",         "HDFC", "Focused"),
    "118993": ("HDFC ELSS Tax saver - Growth Option - Direct Plan",          "HDFC", "ELSS"),
    "120847": ("HDFC Value Fund - Growth Option - Direct Plan",              "HDFC", "Value"),
    "134120": ("HDFC Housing Opportunities Fund - Growth Option - Direct",   "HDFC", "Housing"),
    "118991": ("HDFC Equity Savings Fund - Growth - Direct Plan",            "HDFC", "EquitySavings"),
    "120467": ("HDFC Balanced Advantage Fund - Growth - Direct Plan",        "HDFC", "BalAdv"),
    "118998": ("HDFC Hybrid Equity Fund - Growth - Direct Plan",             "HDFC", "HybridEq"),
    "148883": ("HDFC Retirement Savings Fund - Equity - Direct Plan",        "HDFC", "RetireEq"),
    "148884": ("HDFC Retirement Savings Fund - Hybrid-Equity - Direct",      "HDFC", "RetireHybEq"),
    "120716": ("HDFC NIFTY Midcap 150 Index Fund - Growth Option - Direct",  "HDFC", "NiftyMid150"),
    "122639": ("HDFC Focused 30 Fund - Growth Option - Direct Plan",         "HDFC", "Focused"),
    "149390": ("HDFC NIFTY Smallcap 250 Index Fund - Growth Option - Direct","HDFC", "NiftySmall250"),
}

HDFC_FUND_NAMES = {
    "LargeCap":     "HDFC Large Cap",
    "MidCap":       "HDFC Mid-Cap Opportunities",
    "SmallCap":     "HDFC Small Cap",
    "FlexiCap":     "HDFC Flexi Cap",
    "LargeMidCap":  "HDFC Large and Mid Cap",
    "MultiCap":     "HDFC Multi Cap",
    "Focused":      "HDFC Focused 30",
    "ELSS":         "HDFC ELSS Tax saver",
    "Value":        "HDFC Value",
    "Housing":      "HDFC Housing Opportunities",
    "EquitySavings":"HDFC Equity Savings",
    "BalAdv":       "HDFC Balanced Advantage",
    "HybridEq":     "HDFC Hybrid Equity",
    "RetireEq":     "HDFC Retirement Savings",
    "RetireHybEq":  "HDFC Retirement Savings Hybrid",
    "NiftyMid150":  "HDFC NIFTY Midcap 150",
    "NiftySmall250":"HDFC NIFTY Smallcap 250",
}

HDFC_S3_BASE = "https://files.hdfcfund.com/s3fs-public"

MONTH_NAMES = ["","January","February","March","April","May","June",
               "July","August","September","October","November","December"]


def hdfc_s3_url(fund_slug: str, year: int, month: int) -> str:
    # Publication month = data month + 1
    pub_date = date(year, month, 1)
    if month == 12:
        pub_year, pub_month = year + 1, 1
    else:
        pub_year, pub_month = year, month + 1
    pub_ym = f"{pub_year}-{pub_month:02d}"
    month_name = MONTH_NAMES[month]
    # Last day of month approximation (use 31, server ignores)
    days = [0,31,28,31,30,31,30,31,31,30,31,30,31]
    dd = days[month]
    fund_name = HDFC_FUND_NAMES.get(fund_slug, fund_slug)
    fname = f"Monthly%20HDFC%20{fund_name.replace(' ','%20')}%20-%20{dd}%20{month_name}%20{year}.xlsx"
    return f"{HDFC_S3_BASE}/{pub_ym}/{fname}"


def hdfc_download_new(raw_dir: Path) -> list[tuple[str,str,Path]]:
    """Download any HDFC monthly files not yet cached."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer":    "https://www.hdfcfund.com/",
    })

    # Determine months to try: last 6 months up to current month
    today = date.today()
    months_to_try = []
    for offset in range(0, 7):
        m = today.month - offset
        y = today.year
        while m <= 0:
            m += 12; y -= 1
        months_to_try.append((y, m))

    new_files = []

    # scheme_code → (scheme_name, amc, slug)
    sc_map = {sc: (name, amc, slug) for sc, (name, amc, slug) in HDFC_FUNDS.items()}

    for scheme_code, (scheme_name, amc, slug) in HDFC_FUNDS.items():
        fund_dir = raw_dir / scheme_code
        fund_dir.mkdir(parents=True, exist_ok=True)
        for year, month in months_to_try:
            dt = f"{year}-{month:02d}"
            out = fund_dir / f"{dt}.xlsx"
            if out.exists() and out.stat().st_size > 5000:
                continue
            url = hdfc_s3_url(slug, year, month)
            try:
                r = session.get(url, timeout=30)
                if r.status_code == 200:
                    out.write_bytes(r.content)
                    new_files.append((scheme_code, dt, out))
                    log.info(f"  HDFC: downloaded {slug} {dt}")
            except Exception as e:
                log.debug(f"  HDFC: skip {slug} {dt}: {e}")
            time.sleep(0.1)

    log.info(f"  HDFC: {len(new_files)} new files downloaded")
    return new_files


# ── SHARED MERGE ──────────────────────────────────────────────────────────────

def merge_amc_into_holdings(new_data: dict[str,list], hold_dir: Path, amc_tag: str) -> None:
    for date_str, rows in sorted(new_data.items()):
        parq = hold_dir / f"{date_str}.parquet"
        new_df = pd.DataFrame(rows)
        new_df["as_of_date"] = date_str
        new_df["_sheet"]     = amc_tag
        new_df["year"]       = float(date_str[:4])
        new_df["month"]      = float(date_str[5:7])
        new_df["scheme_code"]= pd.array(new_df["scheme_code"], dtype="Float64")
        keep = ["isin","stock_name","pct_nav","scheme_name","_sheet","amc",
                "scheme_code","as_of_date","year","month"]
        new_df = new_df[[c for c in keep if c in new_df.columns]]

        if parq.exists():
            existing = pd.read_parquet(parq)
            existing = existing[existing["amc"] != amc_tag]
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df
        combined.to_parquet(parq, index=False)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Fetch & ingest latest AMC holdings")
    p.add_argument("--amc",        choices=["mirae","hdfc","all"], default="all")
    p.add_argument("--parse-only", action="store_true", help="Skip download, re-parse raw files")
    p.add_argument("--data-dir",   default=str(Path(__file__).parent / "mf_data"))
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    hold_dir = data_dir / "holdings"
    hold_dir.mkdir(parents=True, exist_ok=True)

    do_mirae = args.amc in ("mirae","all")
    do_hdfc  = args.amc in ("hdfc", "all")

    # ── Mirae ──────────────────────────────────────────────────────────
    if do_mirae:
        log.info("\n── Mirae Asset ─────────────────────────────────────────")
        raw_dir = data_dir / "holdings_raw" / "Mirae"
        raw_dir.mkdir(parents=True, exist_ok=True)
        if not args.parse_only:
            mirae_download_new(raw_dir)
        log.info("  Parsing all Mirae xlsx files…")
        new_data = ingest_mirae(raw_dir, hold_dir)
        total_rows = sum(len(v) for v in new_data.values())
        log.info(f"  Parsed {total_rows} rows across {len(new_data)} months")
        merge_amc_into_holdings(new_data, hold_dir, "Mirae")
        log.info("  Mirae holdings merged into parquets ✓")

    # ── HDFC ───────────────────────────────────────────────────────────
    if do_hdfc:
        log.info("\n── HDFC ────────────────────────────────────────────────")
        raw_dir = data_dir / "holdings_raw" / "HDFC"
        raw_dir.mkdir(parents=True, exist_ok=True)
        if not args.parse_only:
            hdfc_download_new(raw_dir)
        # HDFC parsing is handled by the existing ingest_hdfc.py script
        from ingest_hdfc import process_all as hdfc_process, merge_into_holdings as hdfc_merge
        new_data = hdfc_process()
        hdfc_merge(new_data)
        log.info("  HDFC holdings merged into parquets ✓")

    log.info("\nAll AMC holdings updated ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
