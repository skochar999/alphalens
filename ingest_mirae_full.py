"""
ingest_mirae_full.py
────────────────────
Back-fill Mirae Asset monthly portfolio holdings using the miraeassetmf.co.in
AjaxService API (the same source used by fetch_new_holdings.py).

Currently holdings have May 2024–Apr 2026 (24 months).  This script adds
Jan 2023–Apr 2024 (16 months) to reach 40 months total.

pct_nav scale: Mirae xlsx files store values as decimals (0.0998 = 9.98%).
               Existing parquets store them as-is (decimal).  We keep that
               convention here for consistency.

Usage:
    python ingest_mirae_full.py              # download + ingest
    python ingest_mirae_full.py --dry-run    # show what would be done
    python ingest_mirae_full.py --start 2023-01 --end 2024-04
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections import defaultdict

import requests
import openpyxl
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("ingest_mirae")

# ── Paths ──────────────────────────────────────────────────────────────────────
HOLD_DIR = Path("/sessions/admiring-nifty-dijkstra/mnt/outputs/mf_data/holdings")
RAW_DIR  = Path("/sessions/admiring-nifty-dijkstra/mnt/outputs/mf_data/holdings_raw/Mirae")

# ── Constants ──────────────────────────────────────────────────────────────────
BASE_URL  = "https://www.miraeassetmf.co.in"
AJAX_URL  = f"{BASE_URL}/AjaxService/GetDownloadsData"
ISIN_RE   = re.compile(r'^IN[A-Z0-9]{10}$')

MONTH_MAP = {
    "january": "01", "february": "02", "march":    "03", "april":    "04",
    "may":     "05", "june":     "06", "july":     "07", "august":   "08",
    "september":"09","october":  "10", "november": "11", "december": "12",
}

DEBT_KEYWORDS = [
    'DEBT', 'MONEY MARKET', 'CASH', 'NET ASSETS', 'GRAND TOTAL',
    'MUTUAL FUND', 'CERTIFICATE', 'COMMERCIAL PAPER', 'TREASURY',
    'GOVERNMENT', 'SECURIT', 'REPO ', 'CBLO', 'TOTAL ASSETS',
]

# slug → (scheme_code, scheme_name, amc)
FUND_MAP = {
    "mirae_large_cap":          (118825, "Mirae Asset Large Cap Fund - Direct Plan - Growth",       "Mirae"),
    "mirae_large_midcap":       (118834, "Mirae Asset Large & Midcap Fund - Direct Plan - Growth",  "Mirae"),
    "mirae_elss":               (135781, "Mirae Asset ELSS Tax Saver Fund - Direct Plan - Growth",  "Mirae"),
    "mirae_equity_savings":     (145693, "Mirae Asset Equity Savings Fund- Direct Plan- Growth",    "Mirae"),
    "mirae_focused":            (147206, "Mirae Asset Focused Fund Direct Plan Growth",             "Mirae"),
    "mirae_mid_cap":            (147445, "Mirae Asset Midcap Fund- Direct Growth Option",           "Mirae"),
    "mirae_balanced_advantage": (150470, "Mirae Asset Balanced Advantage Fund Direct Plan- Growth", "Mirae"),
    "mirae_flexi_cap":          (151412, "Mirae Asset Flexi Cap Fund - Direct Plan - Growth",       "Mirae"),
    "mirae_multicap":           (151810, "Mirae Asset Multicap Fund - Direct Plan - Growth",        "Mirae"),
    "mirae_small_cap":          (153196, "Mirae Asset Small Cap Fund - Direct Plan - Growth",       "Mirae"),
    "mirae_aggressive_hybrid":  (None,   "Mirae Asset Aggressive Hybrid Fund Direct Plan Growth",  "Mirae"),
}

MANAGED_CODES = {v[0] for v in FUND_MAP.values() if v[0] is not None}

# Fund title keyword → slug
# Order matters: check "Large & Midcap" before "Large Cap" to avoid false match
KEYWORD_SLUG = [
    ("Large & Midcap Fund",      "mirae_large_midcap"),
    ("Large Cap Fund",           "mirae_large_cap"),
    ("Flexi Cap Fund",           "mirae_flexi_cap"),
    ("Mid Cap Fund",             "mirae_mid_cap"),    # old name
    ("Midcap Fund",              "mirae_mid_cap"),    # new name (same fund)
    ("Small Cap Fund",           "mirae_small_cap"),
    ("ELSS Tax Saver Fund",      "mirae_elss"),
    ("Focused Fund",             "mirae_focused"),
    ("Multicap Fund",            "mirae_multicap"),
    ("Equity Savings Fund",      "mirae_equity_savings"),
    ("Balanced Advantage Fund",  "mirae_balanced_advantage"),
    ("Aggressive Hybrid Fund",   "mirae_aggressive_hybrid"),
]


# ── API helpers ────────────────────────────────────────────────────────────────

def fetch_all_api_items(session: requests.Session) -> list[dict]:
    """Return all items from the Mirae AjaxService portfolio API."""
    all_items: list[dict] = []
    pgno, pgsize = 1, 200
    while True:
        r = session.post(AJAX_URL,
                         json={"request": {"modulename": "portfolio_tab1",
                                           "pgno": pgno, "pgsize": pgsize}},
                         timeout=25)
        data = r.json()
        if data.get("ReturnCode") != "0":
            log.warning("API returned non-zero: %s", data)
            break
        items = data.get("Data", [])
        if not items:
            break
        all_items.extend(items)
        total = data.get("DataCount", 0)
        if len(all_items) >= total:
            break
        pgno += 1
        time.sleep(0.15)
    log.info("API: fetched %d total items across %d pages", len(all_items), pgno)
    return all_items


def parse_date_from_title(title: str) -> str | None:
    """Extract 'YYYY-MM' from titles like 'Portfolio Details as on 28th February, 2023 for ...'"""
    m = re.search(r'as on\s+\d+\w*\s+(\w+)[,\s]+(\d{4})', title, re.I)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower())
        if mon:
            return f"{m.group(2)}-{mon}"
    return None


def build_url_map(items: list[dict], start_ym: str, end_ym: str) -> dict[tuple, str]:
    """
    Return {(slug, 'YYYY-MM'): full_url} for equity funds within [start_ym, end_ym].
    Excludes Nifty/ETF/Index/FoF variants.
    """
    url_map: dict[tuple, str] = {}
    SKIP = ('nifty', 'etf', 'index', 'fof', 'fund of fund')

    for item in items:
        title = item.get("Title", "")
        url   = item.get("URL", "")
        if not url:
            continue
        tl = title.lower()
        if any(s in tl for s in SKIP):
            continue

        dt = parse_date_from_title(title)
        if not dt or not (start_ym <= dt <= end_ym):
            continue

        for kw, slug in KEYWORD_SLUG:
            if kw in title:
                key = (slug, dt)
                if key not in url_map:          # keep first match (most recent listing)
                    full = BASE_URL + url if url.startswith("/") else url
                    url_map[key] = full
                break

    return url_map


# ── Download ───────────────────────────────────────────────────────────────────

def download_file(session: requests.Session, url: str, dest: Path) -> bool:
    """Download url → dest; return True on success."""
    try:
        r = session.get(url, timeout=35)
        if r.status_code == 200 and len(r.content) > 500:
            dest.write_bytes(r.content)
            return True
        log.warning("  HTTP %d for %s", r.status_code, url)
    except Exception as e:
        log.warning("  Download error %s: %s", url, e)
    return False


def download_missing(session: requests.Session,
                     url_map: dict[tuple, str],
                     dry_run: bool = False) -> list[tuple]:
    """Download xlsx files that don't already exist.  Returns list of (slug, dt, path)."""
    tasks = []
    for (slug, dt), url in url_map.items():
        fund_dir = RAW_DIR / slug
        ext = url.rsplit(".", 1)[-1].split("?")[0].lower() or "xlsx"
        dest = fund_dir / f"{dt}.{ext}"
        if dest.exists() and dest.stat().st_size > 500:
            continue                            # already have it
        tasks.append((slug, dt, url, dest))

    log.info("Files to download: %d", len(tasks))
    if dry_run or not tasks:
        return [(s, d, None) for s, d, _, _ in tasks]

    results = []
    def _dl(args):
        slug, dt, url, dest = args
        dest.parent.mkdir(parents=True, exist_ok=True)
        ok = download_file(session, url, dest)
        return (slug, dt, dest) if ok else None

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(_dl, t) for t in tasks]
        for fut in as_completed(futs):
            res = fut.result()
            if res:
                results.append(res)
                log.info("  ✓ %s  %s", res[0], res[1])
            else:
                log.warning("  ✗ download failed")

    return results


# ── Parse ──────────────────────────────────────────────────────────────────────

def parse_mirae_xlsx(path: Path, scheme_code, scheme_name: str, amc: str) -> list[dict]:
    """
    Parse one Mirae portfolio xlsx.
    pct_nav stored as decimal (0.0998 = 9.98%) — consistent with existing parquets.
    """
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        rows = list(wb.active.iter_rows(values_only=True))
        wb.close()
    except Exception as e:
        log.warning("  ERR loading %s: %s", path.name, e)
        return []

    holdings: list[dict] = []
    in_equity = False

    for row in rows:
        if not row or len(row) < 3:
            continue
        cell1 = str(row[1]).strip() if row[1] is not None else ""

        # Section start markers
        if "EQUITY" in cell1.upper() and "RELATED" in cell1.upper():
            in_equity = True
            continue
        if "LISTED" in cell1.upper() and "AWAITING" in cell1.upper():
            continue     # sub-header within equity, still equity

        # Section end markers
        if in_equity and cell1:
            upper = cell1.upper()
            if any(kw in upper for kw in DEBT_KEYWORDS):
                in_equity = False
                continue
            if re.match(r'^\([B-Z]\)', upper):
                in_equity = False
                continue

        if not in_equity:
            continue
        if len(row) < 7:
            continue

        isin = str(row[2]).strip() if row[2] is not None else ""
        if not ISIN_RE.match(isin):
            continue

        try:
            pct_nav = float(row[6])
        except (ValueError, TypeError):
            continue
        if pct_nav <= 0:
            continue

        holdings.append({
            "isin":        isin,
            "stock_name":  str(row[1]).strip() if row[1] else "",
            "pct_nav":     pct_nav,
            "scheme_code": scheme_code,
            "scheme_name": scheme_name,
            "amc":         amc,
        })

    return holdings


# ── Ingest month ───────────────────────────────────────────────────────────────

def ingest_month(ym: str,
                 rows_by_slug: dict[str, list[dict]],
                 dry_run: bool = False) -> dict:
    """
    Merge new Mirae rows for month `ym` into the holdings parquet.
    Removes stale Mirae rows (amc=="Mirae" and scheme_code in MANAGED_CODES)
    before inserting new data.
    """
    parq = HOLD_DIR / f"{ym}.parquet"
    new_rows: list[dict] = []
    for slug, rows in rows_by_slug.items():
        new_rows.extend(rows)

    if not new_rows:
        return {"ym": ym, "status": "skip", "rows": 0}

    as_of = pd.to_datetime(f"{ym}-01")

    # Add date metadata
    for r in new_rows:
        r["as_of_date"] = as_of
        r["year"]       = int(ym[:4])
        r["month"]      = int(ym[5:7])
        r["_sheet"]     = "equity"

    new_df = pd.DataFrame(new_rows)
    new_df["as_of_date"] = pd.to_datetime(new_df["as_of_date"])

    if dry_run:
        return {"ym": ym, "status": "ok(dry)", "rows": len(new_df)}

    if parq.exists():
        existing = pd.read_parquet(parq)
        # Remove stale Mirae equity rows
        mask_keep = ~(
            (existing["amc"] == "Mirae") &
            (existing["scheme_code"].isin(MANAGED_CODES))
        )
        existing = existing[mask_keep]
        merged = pd.concat([existing, new_df], ignore_index=True)
    else:
        merged = new_df

    merged["as_of_date"] = pd.to_datetime(merged["as_of_date"])
    merged.to_parquet(parq, index=False)
    return {"ym": ym, "status": "ok", "rows": len(new_df)}


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start",    default="2023-01", help="First month YYYY-MM")
    ap.add_argument("--end",      default="2024-04", help="Last month YYYY-MM")
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--all",      action="store_true",
                    help="Process all available months (overrides --start/--end)")
    args = ap.parse_args()

    if args.all:
        start_ym, end_ym = "2021-01", "2026-12"
    else:
        start_ym, end_ym = args.start, args.end

    log.info("Mirae full ingest: %s → %s  (dry_run=%s)", start_ym, end_ym, args.dry_run)

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (FundLens/1.0)"})

    # Step 1: fetch API
    log.info("Fetching Mirae AjaxService API …")
    items = fetch_all_api_items(session)

    # Step 2: build URL map
    url_map = build_url_map(items, start_ym, end_ym)
    log.info("URL map: %d (slug, month) pairs", len(url_map))

    by_slug_month: dict[str, set] = defaultdict(set)
    for slug, dt in url_map:
        by_slug_month[slug].add(dt)
    for slug in sorted(by_slug_month):
        months = sorted(by_slug_month[slug])
        log.info("  %-30s  %d months  (%s – %s)",
                 slug, len(months), months[0], months[-1])

    # Step 3: download missing files
    downloaded = download_missing(session, url_map, dry_run=args.dry_run)
    log.info("Downloaded %d new files", len(downloaded))

    if args.dry_run:
        log.info("DRY RUN — no parquets written")
        # Still show parse preview for first 3 files
        for slug, dt, path in downloaded[:3]:
            if path and path.exists():
                sc, sn, amc = FUND_MAP[slug]
                rows = parse_mirae_xlsx(path, sc, sn, amc)
                log.info("  Preview %s %s → %d rows", slug, dt, len(rows))
        return

    # Step 4: parse downloaded files + all newly available raw files in range
    # (Handles files that were already on disk but not yet in parquets)
    rows_by_month: dict[str, dict[str, list]] = defaultdict(dict)

    # Scan ALL raw files in the target date range
    for slug_dir in sorted(RAW_DIR.iterdir()):
        slug = slug_dir.name
        if slug not in FUND_MAP:
            continue
        sc, sn, amc = FUND_MAP[slug]
        for path in sorted(slug_dir.glob("*.xlsx")):
            dt = path.stem
            if not re.match(r'^\d{4}-\d{2}$', dt):
                continue
            if not (start_ym <= dt <= end_ym):
                continue
            rows = parse_mirae_xlsx(path, sc, sn, amc)
            if rows:
                rows_by_month[dt][slug] = rows

    log.info("Parsed data for %d months", len(rows_by_month))

    # Step 5: ingest each month
    results = []
    for ym in sorted(rows_by_month):
        res = ingest_month(ym, rows_by_month[ym], dry_run=False)
        results.append(res)
        icon = "✓" if res["status"].startswith("ok") else "○"
        log.info("  %s  %s  [%s]  %d rows", icon, ym, res["status"], res["rows"])

    ok  = sum(1 for r in results if r["status"] == "ok")
    skp = sum(1 for r in results if r["status"] == "skip")
    total_rows = sum(r["rows"] for r in results)
    log.info("Done: %d written, %d skipped, %d total rows ingested", ok, skp, total_rows)


if __name__ == "__main__":
    main()
