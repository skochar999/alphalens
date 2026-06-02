"""
ingest_icici_full.py
────────────────────
Ingest ICICI Prudential monthly portfolio holdings from the user-supplied
ICICI.zip (which contains one inner ZIP per month).

The outer ZIP contains entries like:
  ICICI/monthly-portfolio-disclosure-january-2023.zip
  ICICI/Monthly-Portfolio-Disclosure-April-2024.zip
  ICICI/Monthly-Portfolio-Disclosure-October-2024 (2).zip   ← note "(2)"

Each inner ZIP contains one xlsx per scheme (≈120 files), named exactly
after the fund: "ICICI Prudential Bluechip Fund.xlsx", etc.

Parsing is delegated to the existing amc_holdings_scraper._parse_sheet(),
which auto-detects the header row and auto-scales pct_nav to 0–100.

Usage:
    python ingest_icici_full.py                              # ingest all months
    python ingest_icici_full.py --dry-run                    # preview only
    python ingest_icici_full.py --zip /path/to/ICICI.zip
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import logging
import zipfile
from pathlib import Path

import openpyxl
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("ingest_icici")

# ── Paths ──────────────────────────────────────────────────────────────────────
HOLD_DIR    = Path("/sessions/admiring-nifty-dijkstra/mnt/outputs/mf_data/holdings")
DEFAULT_ZIP = Path("/sessions/admiring-nifty-dijkstra/mnt/uploads/ICICI.zip")

# ── Constants ──────────────────────────────────────────────────────────────────
ISIN_RE = re.compile(r'^IN[A-Z0-9]{10}$')

MONTH_MAP = {
    "january": "01", "february": "02", "march":    "03", "april":    "04",
    "may":     "05", "june":     "06", "july":     "07", "august":   "08",
    "september":"09","october":  "10", "november": "11", "december": "12",
}

# xlsx filename stem → (scheme_code, stored_scheme_name)
# scheme_name matches what the existing 15 months use (from amc_holdings_scraper)
FUND_MAP: dict[str, tuple] = {
    # Core equity
    "ICICI Prudential Bluechip Fund":
        (120586, "ICICI Prudential Bluechip Fund",           "ICICI"),
    "ICICI Prudential Large Cap Fund":
        (120586, "ICICI Prudential Large Cap Fund",          "ICICI"),
    "ICICI Prudential Flexicap Fund":
        (148990, "ICICI Prudential Flexicap Fund",           "ICICI"),
    "ICICI Prudential Large & Mid Cap Fund":
        (120596, "ICICI Prudential Large & Mid Cap Fund",    "ICICI"),
    "ICICI Prudential Midcap Fund":
        (120381, "ICICI Prudential Midcap Fund",             "ICICI"),
    "ICICI Prudential Multicap Fund":
        (120599, "ICICI Prudential Multicap Fund",           "ICICI"),
    "ICICI Prudential Focused Equity Fund":
        (120722, "ICICI Prudential Focused Equity Fund",     "ICICI"),
    "ICICI Prudential Long Term Equity Fund (Tax Saving)":
        (120592, "ICICI Prudential ELSS Tax Saver Fund",     "ICICI"),
    "ICICI Prudential Value Discovery Fund":
        (120323, "ICICI Prudential Value Discovery Fund",    "ICICI"),
    "ICICI Prudential Smallcap Fund":
        (120591, "ICICI Prudential Smallcap Fund",           "ICICI"),
    # Thematic / sector equity (no scheme_code → stored as None, still ingested)
    "ICICI Prudential India Opportunities Fund":
        (145897, "ICICI Prudential India Opportunities Fund","ICICI"),
    "ICICI Prudential Business Cycle Fund":
        (None,   "ICICI Prudential Business Cycle Fund",     "ICICI"),
    "ICICI Prudential Dividend Yield Equity Fund":
        (None,   "ICICI Prudential Dividend Yield Equity Fund","ICICI"),
    "ICICI Prudential Technology Fund":
        (None,   "ICICI Prudential Technology Fund",         "ICICI"),
    "ICICI Prudential MNC Fund":
        (None,   "ICICI Prudential MNC Fund",                "ICICI"),
    "ICICI Prudential Banking & Financial Services Fund":
        (None,   "ICICI Prudential Banking & Financial Services Fund", "ICICI"),
    "ICICI Prudential Infrastructure Fund":
        (None,   "ICICI Prudential Infrastructure Fund",     "ICICI"),
    "ICICI Prudential Manufacturing Fund":
        (None,   "ICICI Prudential Manufacturing Fund",      "ICICI"),
    "ICICI Prudential Exports and Services Fund":
        (None,   "ICICI Prudential Exports and Services Fund","ICICI"),
    "ICICI Prudential FMCG Fund":
        (None,   "ICICI Prudential FMCG Fund",               "ICICI"),
    "ICICI Prudential Commodities Fund":
        (None,   "ICICI Prudential Commodities Fund",        "ICICI"),
    "ICICI Prudential Retirement Fund - Pure Equity Plan":
        (146349, "ICICI Prudential Retirement Fund - Pure Equity Plan", "ICICI"),
    "ICICI PRUDENTIAL HOUSING OPPORTUNITIES FUND":
        (150310, "ICICI PRUDENTIAL HOUSING OPPORTUNITIES FUND", "ICICI"),
    "ICICI PRUDENTIAL PSU EQUITY FUND":
        (150539, "ICICI PRUDENTIAL PSU EQUITY FUND",         "ICICI"),
}

MANAGED_CODES = {v[0] for v in FUND_MAP.values() if v[0] is not None}
MANAGED_NAMES = {v[1] for v in FUND_MAP.values()}


# ── Parsing ────────────────────────────────────────────────────────────────────

NAV_KEYWORDS = ("% to nav", "% to aum", "% to net assets", "% net assets",
                "%tonav", "%toaum", "% of nav", "weightage", "% nav")

def _norm(v) -> str:
    if v is None: return ""
    return re.sub(r"\s+", " ", str(v).lower()).strip()

def parse_icici_xlsx(data: bytes, stem: str) -> list[dict]:
    """Parse one ICICI fund xlsx (as raw bytes). Returns list of holding dicts."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as e:
        log.warning("  ERR loading %s: %s", stem, e)
        return []

    if not rows:
        return []

    # Find header row
    header_idx = None
    for i, row in enumerate(rows):
        cells = [_norm(v) for v in row]
        has_isin = any("isin" in c for c in cells)
        has_nav  = any(any(kw in c for kw in NAV_KEYWORDS) for c in cells)
        if has_isin and has_nav:
            header_idx = i
            break

    if header_idx is None:
        return []

    headers  = [_norm(v) for v in rows[header_idx]]
    isin_col = next((i for i, h in enumerate(headers) if h == "isin"), None)
    if isin_col is None:
        isin_col = next((i for i, h in enumerate(headers) if "isin" in h), None)
    nav_col  = next((i for i, h in enumerate(headers)
                     if any(kw in h for kw in NAV_KEYWORDS)), None)
    name_col = next((i for i, h in enumerate(headers)
                     if "name" in h and i != isin_col), None)

    if isin_col is None or nav_col is None:
        return []

    holdings = []
    for row in rows[header_idx + 1:]:
        if not row or len(row) <= max(isin_col, nav_col):
            continue
        isin = str(row[isin_col]).strip() if row[isin_col] else ""
        if not ISIN_RE.match(isin):
            continue
        try:
            pct = float(row[nav_col])
        except (ValueError, TypeError):
            continue
        if pct <= 0 or pct > 100:
            continue
        name = str(row[name_col] or "").strip() if name_col is not None and name_col < len(row) else ""
        holdings.append({"isin": isin, "stock_name": name, "pct_nav": pct})

    # Auto-scale: ICICI stores decimal (0.089 = 8.9%) — scale to 0–100
    if holdings:
        max_pct = max(h["pct_nav"] for h in holdings)
        if max_pct <= 1.5:
            for h in holdings:
                h["pct_nav"] = round(h["pct_nav"] * 100, 4)

    return holdings


def parse_month_zip(inner_zip_data: bytes, ym: str) -> list[dict]:
    """Parse all equity fund xlsx files from one monthly inner ZIP."""
    all_rows = []
    try:
        with zipfile.ZipFile(io.BytesIO(inner_zip_data)) as zf:
            xlsx_names = [n for n in zf.namelist()
                          if n.lower().endswith(".xlsx") and "__MACOSX" not in n]
            for name in xlsx_names:
                stem = Path(name).stem  # e.g. "ICICI Prudential Bluechip Fund"
                if stem not in FUND_MAP:
                    continue
                sc, sn, amc = FUND_MAP[stem]
                data = zf.read(name)
                rows = parse_icici_xlsx(data, stem)
                for r in rows:
                    r["scheme_code"] = sc
                    r["scheme_name"] = sn
                    r["amc"]         = amc
                    r["_sheet"]      = "equity"
                all_rows.extend(rows)
    except Exception as e:
        log.warning("  ZIP parse error for %s: %s", ym, e)
    return all_rows


# ── Date extraction ────────────────────────────────────────────────────────────

def parse_ym_from_inner_name(name: str) -> str | None:
    """
    Extract YYYY-MM from inner zip filename.
    Handles:
      monthly-portfolio-disclosure-january-2023.zip
      Monthly-Portfolio-Disclosure-October-2024 (2).zip
    """
    base = Path(name).stem.lower()
    base = re.sub(r'\s*\(\d+\)', '', base).strip()   # remove " (2)" etc
    m = re.search(r'(\w+)-(\d{4})$', base)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower())
        if mon:
            return f"{m.group(2)}-{mon}"
    return None


# ── Ingest ─────────────────────────────────────────────────────────────────────

def ingest_month(ym: str, new_rows: list[dict], dry_run: bool = False) -> dict:
    """Merge ICICI holdings for `ym` into the holdings parquet."""
    if not new_rows:
        return {"ym": ym, "status": "skip", "rows": 0}

    as_of = pd.to_datetime(f"{ym}-01")
    for r in new_rows:
        r["as_of_date"] = as_of
        r["year"]       = int(ym[:4])
        r["month"]      = int(ym[5:7])

    new_df = pd.DataFrame(new_rows)
    new_df["as_of_date"] = pd.to_datetime(new_df["as_of_date"])

    if dry_run:
        return {"ym": ym, "status": "ok(dry)", "rows": len(new_df)}

    parq = HOLD_DIR / f"{ym}.parquet"
    if parq.exists():
        existing = pd.read_parquet(parq)
        # Remove stale ICICI rows for this month
        mask_keep = ~(
            (existing["amc"] == "ICICI") &
            (
                existing["scheme_name"].isin(MANAGED_NAMES) |
                existing["scheme_code"].isin(MANAGED_CODES)
            )
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
    ap.add_argument("--zip",     default=str(DEFAULT_ZIP), help="Path to ICICI.zip")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    outer_zip = Path(args.zip)
    if not outer_zip.exists():
        log.error("ZIP not found: %s", outer_zip)
        sys.exit(1)

    log.info("ICICI ingest from %s  (dry_run=%s)", outer_zip.name, args.dry_run)

    results = []
    with zipfile.ZipFile(outer_zip) as oz:
        inner_names = sorted([
            n for n in oz.namelist()
            if n.lower().endswith(".zip") and "__MACOSX" not in n
            and re.search(r'portfolio.disclosure', n, re.I)
        ])
        log.info("Found %d inner month ZIPs", len(inner_names))

        for inner_name in inner_names:
            ym = parse_ym_from_inner_name(inner_name)
            if not ym:
                log.warning("  Could not parse date from: %s", inner_name)
                continue

            inner_data = oz.read(inner_name)
            rows = parse_month_zip(inner_data, ym)

            funds_found = len({r["scheme_name"] for r in rows})
            log.info("  %s → %d rows across %d funds", ym, len(rows), funds_found)

            res = ingest_month(ym, rows, dry_run=args.dry_run)
            results.append(res)

            icon = "✓" if res["status"].startswith("ok") else "○"
            log.info("    %s [%s]  %d rows written", icon, res["status"], res["rows"])

    ok  = sum(1 for r in results if r["status"] == "ok")
    dry = sum(1 for r in results if r["status"] == "ok(dry)")
    skp = sum(1 for r in results if r["status"] == "skip")
    total_rows = sum(r["rows"] for r in results)

    log.info("Done: %d written, %d dry, %d skipped — %d total rows",
             ok, dry, skp, total_rows)


if __name__ == "__main__":
    main()
