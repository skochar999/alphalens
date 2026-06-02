#!/usr/bin/env python3
"""
backfill_holdings.py
====================
Downloads ALL available historical monthly portfolio disclosures for each AMC
and builds a per-month holdings cache at mf_data/holdings/YYYY-MM.parquet.

Run on your Mac (needs network access to AMC/AdvisorKhoj sites).

Coverage by source:
  SBI        : ~30 months (Oct 2023-) via direct Sitefinity URL
  DSP        : ~46 months (Jun 2022-) via DSP disclosure page
  Franklin   : ~40 months (Jan 2023-) via AdvisorKhoj
  ABSL       : ~40 months (Jan 2023-) via AdvisorKhoj
  Axis       : ~40 months (Jan 2023-) via AdvisorKhoj
  Nippon     : ~40 months (varies)   via AdvisorKhoj
  ICICI_Pru  : ~16 months (Jan 2025-) via AdvisorKhoj
  HDFC       :  1 month (Apr 2026)   via HDFC page (S3 gated)

Usage:
    python backfill_holdings.py
    python backfill_holdings.py --mf-data ./mf_data --lookback-months 36
    python backfill_holdings.py --amc SBI --force
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import io
import logging
import re
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path
from typing import Optional

import pandas as pd

# Reuse parsers and scheme matcher from the main scraper
sys.path.insert(0, str(Path(__file__).parent))
from amc_holdings_scraper import (
    _curl, _load_workbook_bytes, _parse_workbook, _parse_zip,
    _parse_absl_xls, attach_scheme_codes,
    _month_parts, _ordinal,
    ISIN_RE,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("backfill")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

# ─────────────────────────────────────────────────────────────────────────────
# URL map builders — return {month_str: download_url} for each AMC
# ─────────────────────────────────────────────────────────────────────────────

def _extract_ym_from_text(text: str) -> Optional[tuple[int, int]]:
    """
    Try multiple date patterns to extract (year, month) from a filename or URL fragment.
    Returns None if no date found.
    """
    text = urllib.parse.unquote(text).lower()

    # Pattern: DD MM YY  e.g. "30 04 26" or "30-04-26"
    m = re.search(r'\b(\d{1,2})[\s\-_](\d{2})[\s\-_](\d{2})\b', text)
    if m:
        d, mo, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            yr = 2000 + y2
            return (yr, mo)

    # Pattern: DD.MM.YYYY  e.g. "30.09.2023"
    m = re.search(r'\b(\d{1,2})\.(\d{2})\.(\d{4})\b', text)
    if m:
        d, mo, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12:
            return (yr, mo)

    # Pattern: month_name DD YYYY  e.g. "april 30 2026" or "apr-30-2026"
    m = re.search(r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
                  r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
                  r'[\s\-_](\d{1,2})[\s\-_,]*(\d{4})\b', text)
    if m:
        mo = MONTH_MAP.get(m.group(1)[:3], 0)
        yr = int(m.group(3))
        if mo and 2020 <= yr <= 2030:
            return (yr, mo)

    # Pattern: DD month_name YYYY  e.g. "31 march 2026" or "31-march-2026"
    m = re.search(r'\b(\d{1,2})[\s\-_](jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|'
                  r'jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|'
                  r'dec(?:ember)?)[\s\-_](\d{4})\b', text)
    if m:
        mo = MONTH_MAP.get(m.group(2)[:3], 0)
        yr = int(m.group(3))
        if mo and 2020 <= yr <= 2030:
            return (yr, mo)

    # Pattern: month_name YYYY  e.g. "november-2025" (no day)
    m = re.search(r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
                  r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
                  r'[\s\-_](\d{4})\b', text)
    if m:
        mo = MONTH_MAP.get(m.group(1)[:3], 0)
        yr = int(m.group(2))
        if mo and 2020 <= yr <= 2030:
            return (yr, mo)

    # Pattern: DD month_name YY  e.g. "31-mar-26" or "30-april-26"
    m = re.search(r'\b(\d{1,2})[\s\-_](jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|'
                  r'jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|'
                  r'dec(?:ember)?)[\s\-_](\d{2})\b', text)
    if m:
        mo = MONTH_MAP.get(m.group(2)[:3], 0)
        y2 = int(m.group(3))
        yr = 2000 + y2
        if mo and 2020 <= yr <= 2030:
            return (yr, mo)

    # Pattern: month_name YY  e.g. "nov-25"
    m = re.search(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[\s\-_](\d{2})\b', text)
    if m:
        mo = MONTH_MAP.get(m.group(1), 0)
        yr = 2000 + int(m.group(2))
        if mo and 2020 <= yr <= 2030:
            return (yr, mo)

    return None


def _ak_url_map(ak_slug: str, file_extensions=(".xlsx", ".xls", ".zip")) -> dict[str, str]:
    """
    Scrape an AdvisorKhoj AMC page and return {month_str: url} for all download links.
    ak_slug e.g. "Franklin-Templeton-Mutual-Fund"
    """
    url = (f"https://www.advisorkhoj.com/form-download-centre/Mutual/"
           f"{ak_slug}/Monthly-Portfolio-Disclosures")
    log.info(f"  Scraping AdvisorKhoj: {ak_slug}")
    html = _curl(url, timeout=30)
    if not html:
        log.warning(f"  AdvisorKhoj fetch failed for {ak_slug}")
        return {}

    result: dict[str, str] = {}
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
    for href in hrefs:
        lower = href.lower()
        if not any(lower.endswith(ext) for ext in file_extensions):
            # Also accept links without extension (Nippon sometimes omits .xls)
            if not re.search(r'\bNIMF\b', href, re.I):
                continue
        ym = _extract_ym_from_text(href)
        if ym:
            month_str = f"{ym[0]}-{ym[1]:02d}"
            if month_str not in result:  # keep first (most recent) match
                result[month_str] = href
    log.info(f"  → {len(result)} months found: "
             f"{min(result) if result else '—'} to {max(result) if result else '—'}")
    return result


def _sbi_url_map(lookback_months: int = 40) -> dict[str, str]:
    """Generate SBI URL map by probing the Sitefinity CDN directly."""
    result: dict[str, str] = {}
    today = datetime.date.today()
    for i in range(1, lookback_months + 1):
        # go back i months
        d = (today.replace(day=1) - datetime.timedelta(days=1))
        for _ in range(i - 1):
            d = (d.replace(day=1) - datetime.timedelta(days=1))
        y, m = d.year, d.month
        last = calendar.monthrange(y, m)[1]
        mname = d.strftime("%B").lower()
        ord_day = _ordinal(last)
        fname = f"all-schemes-monthly-portfolio---as-on-{ord_day}-{mname}-{y}.xlsx"
        url = f"https://www.sbimf.com/docs/default-source/scheme-portfolios/{fname}"
        month_str = f"{y}-{m:02d}"
        result[month_str] = url  # will 404 if not published; handled at download time
    return result


def _dsp_url_map() -> dict[str, str]:
    """Scrape DSP's disclosure page — they keep full history there."""
    page_url = "https://www.dspim.com/mandatory-disclosures/portfolio-disclosures"
    log.info("  Scraping DSP disclosure page")
    html = _curl(page_url, timeout=30)
    if not html:
        return {}
    zips = list(dict.fromkeys(re.findall(
        r'https://www\.dspim\.com/media/pages/mandatory-disclosures/portfolio-disclosures/'
        r'[^\s"\'<>\\]+/monthend-portfolio[^\s"\'<>\\]+\.zip',
        html, re.I
    )))
    result: dict[str, str] = {}
    for url in zips:
        fname = url.split("/")[-1]
        ym = _extract_ym_from_text(fname)
        if ym:
            month_str = f"{ym[0]}-{ym[1]:02d}"
            if month_str not in result:
                result[month_str] = url
    log.info(f"  → {len(result)} months: {min(result) if result else '—'} to {max(result) if result else '—'}")
    return result


def _hdfc_url_map() -> dict[str, str]:
    """HDFC: scrape current page — only exposes the current month's S3 folder."""
    page_url = "https://www.hdfcfund.com/statutory-disclosure/portfolio/monthly-portfolio"
    log.info("  Scraping HDFC page (limited history)")
    html = _curl(page_url, timeout=30)
    if not html:
        return {}
    urls = list(dict.fromkeys(re.findall(
        r'https://files\.hdfcfund\.com/s3fs-public/\d{4}-\d{2}/[^\s"\'<>]+\.xlsx',
        html, re.I
    )))
    result: dict[str, str] = {}
    for url in urls:
        fname = urllib.parse.unquote(url.split("/")[-1])
        ym = _extract_ym_from_text(fname)
        if ym:
            month_str = f"{ym[0]}-{ym[1]:02d}"
            result[month_str] = url   # multiple files per month (one per scheme)
    # HDFC has one file per scheme; we need all of them — return a sentinel
    # We'll handle multi-file download specially below
    if result:
        log.info(f"  → {len(result)} distinct months, {len(urls)} scheme files")
    return {"_all_urls": urls, **result}  # type: ignore[dict-item]


# AdvisorKhoj slugs for each AMC
AK_SLUGS = {
    "ICICI_Pru":    "ICICI-Prudential-Mutual-Fund",
    "Aditya_Birla": "Aditya-Birla-Sun-Life-Mutual-Fund",
    "Nippon":       "Nippon-India-Mutual-Fund",
    "Axis":         "Axis-Mutual-Fund",
    "Franklin":     "Franklin-Templeton-Mutual-Fund",
    "Kotak":        "Kotak-Mutual-Fund",
    "Mirae":        "Mirae-Asset-Mutual-Fund",
}


def build_url_maps(lookback_months: int = 40) -> dict[str, dict[str, str]]:
    """Build {amc: {month_str: url}} for all AMCs."""
    maps: dict[str, dict[str, str]] = {}

    log.info("[URL maps] SBI")
    maps["SBI"] = _sbi_url_map(lookback_months)

    log.info("[URL maps] DSP")
    maps["DSP"] = _dsp_url_map()

    log.info("[URL maps] HDFC")
    maps["HDFC"] = _hdfc_url_map()

    for amc, slug in AK_SLUGS.items():
        log.info(f"[URL maps] {amc}")
        maps[amc] = _ak_url_map(slug)

    return maps


# ─────────────────────────────────────────────────────────────────────────────
# Per-AMC historical downloaders
# ─────────────────────────────────────────────────────────────────────────────

def _download_from_url(url: str, amc: str, month_str: str) -> pd.DataFrame:
    """
    Download and parse a single file URL for an AMC.
    Handles xlsx, xls (BIFF), and zip containing either.
    Sets scheme_name from worksheet content (parser auto-detects).
    """
    # Ensure .xls extension on Nippon URLs that lack it
    if "nipponindiaim" in url.lower() and not re.search(r'\.(xlsx?|zip)$', url, re.I):
        url = url.rstrip("/") + ".xls"

    log.debug(f"  [{amc}] {url.split('/')[-1][:80]}")
    data = _curl(url, binary=True, timeout=90)
    if not data or len(data) < 200:
        return pd.DataFrame()

    # AMCs that use single consolidated xlsx with Index sheet + per-fund sheets
    # These files start with PK (xlsx = internal ZIP) but are NOT zip-of-xlsx archives
    MULTI_SHEET_AMCS = {"Nippon", "SBI", "Kotak", "Mirae"}

    # ZIP / xlsx disambiguation:
    #   - ICICI_Pru sends a ZIP archive of per-scheme xlsx files
    #   - Multi-sheet AMCs send a single xlsx workbook (also PK magic but NOT an archive)
    #   - Axis/Franklin/DSP/ABSL are handled by _parse_workbook or _parse_absl_xls
    if data[:4] == b'PK\x03\x04':
        if amc in ("ICICI_Pru",):
            # ICICI: zip archive of per-scheme xlsx files
            from amc_holdings_scraper import _parse_zip as _pz
            df = _pz(data, month_str, scheme_name_from_file=True)
            return df
        elif amc not in MULTI_SHEET_AMCS:
            # Generic zip-of-xlsx (DSP, Franklin backfill zips, etc.)
            df = _parse_zip(data, month_str)
            return df
        # else: fall through to openpyxl workbook handler below

    # OLE2 xls (BIFF) — ABSL uses this
    if data[:4] == b'\xd0\xcf\x11\xe0':
        return _parse_absl_xls(data)

    # xlsx (openpyxl) — single consolidated workbook
    wb = _load_workbook_bytes(data)
    if wb is None:
        return pd.DataFrame()

    if amc in MULTI_SHEET_AMCS:
        # Index sheet maps short sheet codes → full scheme names.
        # Format varies:
        #   Nippon/Axis: 2 cols  (sheet_code, full_name)      → key=col0, val=col1
        #   SBI:         3 cols  (num_code, sheet_code, name) → key=col1, val=col2
        # Auto-detect: check which column values appear in wb.sheetnames
        idx = next((s for s in wb.sheetnames if s.lower() == "index"), None)
        code_to_name: dict[str, str] = {}
        if idx:
            ws_i = wb[idx]
            all_rows = [row for row in ws_i.iter_rows(values_only=True)
                        if any(v is not None for v in row)]

            # Collect candidates from col0 and col1 and see which overlap sheet names
            sheet_set = set(wb.sheetnames)
            col0_hits = sum(1 for r in all_rows if r[0] and str(r[0]).strip() in sheet_set)
            col1_hits = sum(1 for r in all_rows
                           if len(r) > 1 and r[1] and str(r[1]).strip() in sheet_set)

            if col1_hits > col0_hits:
                # SBI-style: key=col1, val=col2
                for row in all_rows:
                    if len(row) >= 3 and row[1] and row[2]:
                        k = str(row[1]).strip()
                        v = str(row[2]).strip()
                        if k and v and k != "Scheme Short code":
                            code_to_name[k] = v[:120]
            else:
                # Nippon-style: key=col0, val=col1
                for row in all_rows:
                    code = row[0]
                    name = row[1] if len(row) > 1 else None
                    if code and isinstance(code, str) and 1 <= len(code) <= 10 and name:
                        code_to_name[str(code).strip()] = str(name)[:120].strip()

        dfs = []
        from amc_holdings_scraper import _parse_sheet
        for sn in wb.sheetnames:
            if sn.lower() == "index":
                continue
            ws = wb[sn]
            df = _parse_sheet(ws)
            if not df.empty:
                if sn in code_to_name and code_to_name[sn]:
                    df["scheme_name"] = code_to_name[sn]
                df["_sheet"] = sn
                dfs.append(df)
        return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

    return _parse_workbook(wb)


def _download_hdfc_month(month_str: str, all_urls: list[str]) -> pd.DataFrame:
    """
    HDFC: download all per-scheme xlsx files for a given month.
    Filter `all_urls` to those matching the target month name.
    """
    y, m, last, mname, mabbr, mlower, y2 = _month_parts(month_str)
    file_urls = [u for u in all_urls
                 if mname.lower() in urllib.parse.unquote(u).lower()
                 and str(y) in u]
    if not file_urls:
        return pd.DataFrame()
    dfs = []
    for url in file_urls:
        data = _curl(url, binary=True, timeout=30)
        if not data:
            continue
        wb = _load_workbook_bytes(data)
        if wb is None:
            continue
        df = _parse_workbook(wb)
        if not df.empty:
            dfs.append(df)
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# Main backfill logic
# ─────────────────────────────────────────────────────────────────────────────

def merge_amc_into_cache(
    amc: str,
    mf_data: Path,
    lookback_months: int = 40,
    force: bool = False,
    month_filter: Optional[str] = None,
):
    """
    Download one AMC's historical holdings and MERGE into existing monthly parquets.
    Unlike backfill(), this does NOT skip months that already have a parquet —
    it loads the existing parquet, removes any prior rows for this AMC, appends
    the freshly downloaded rows, and saves.  Idempotent with force=False.
    """
    hold_dir = mf_data / "holdings"
    hold_dir.mkdir(parents=True, exist_ok=True)

    scheme_path = mf_data / "scheme_list.parquet"
    scheme_df = pd.read_parquet(scheme_path) if scheme_path.exists() else None
    if scheme_df is not None and "amc" not in scheme_df.columns and "amc_name" in scheme_df.columns:
        scheme_df = scheme_df.rename(columns={"amc_name": "amc"})

    log.info(f"Building URL map for {amc} …")
    url_maps = build_url_maps(lookback_months)
    hdfc_all_urls = url_maps.get("HDFC", {}).pop("_all_urls", [])
    amc_map = url_maps.get(amc, {})

    # Determine months to process
    today = datetime.date.today()
    all_months = []
    for i in range(1, lookback_months + 1):
        d = today.replace(day=1) - datetime.timedelta(days=1)
        for _ in range(i - 1):
            d = d.replace(day=1) - datetime.timedelta(days=1)
        all_months.append(f"{d.year}-{d.month:02d}")
    if month_filter:
        all_months = [m for m in all_months if m == month_filter]

    COLS = ["isin", "stock_name", "pct_nav", "scheme_name", "_sheet",
            "amc", "scheme_code", "as_of_date"]

    ok, skipped, failed = 0, 0, 0
    for month_str in sorted(all_months, reverse=True):
        out_path = hold_dir / f"{month_str}.parquet"

        # Check if AMC already present
        if out_path.exists() and not force:
            existing = pd.read_parquet(out_path)
            if amc in existing["amc"].values:
                log.info(f"  {month_str}: {amc} already present — skipping")
                skipped += 1
                continue
        elif out_path.exists():
            existing = pd.read_parquet(out_path)
            existing = existing[existing["amc"] != amc]  # remove old rows for this AMC
        else:
            existing = pd.DataFrame(columns=COLS)

        # Download
        if amc == "HDFC":
            df = _download_hdfc_month(month_str, hdfc_all_urls)
        else:
            url = amc_map.get(month_str)
            if not url:
                log.debug(f"  {month_str}: no URL for {amc}")
                continue
            df = _download_from_url(url, amc, month_str)

        if df.empty:
            log.debug(f"  {month_str}: {amc} returned empty")
            failed += 1
            continue

        df["amc"] = amc
        log.info(f"  {month_str}: {amc} {len(df):,} rows, {df['scheme_name'].nunique()} schemes")

        # Scheme matching
        if scheme_df is not None:
            df = attach_scheme_codes(df, scheme_df)
            df["scheme_code"] = pd.to_numeric(df["scheme_code"], errors="coerce").astype("Int64")
        else:
            df["scheme_code"] = pd.NA

        df["as_of_date"] = month_str

        # Align columns and merge
        for c in COLS:
            if c not in df.columns:
                df[c] = pd.NA
            if c not in existing.columns:
                existing[c] = pd.NA
        df       = df[COLS]
        existing = existing[COLS]

        combined = pd.concat([existing, df], ignore_index=True)
        combined.to_parquet(out_path, index=False)
        n_matched = combined["scheme_code"].notna().sum()
        log.info(f"    → {out_path.name}: {len(combined):,} total rows, "
                 f"{n_matched/len(combined)*100:.0f}% matched")
        ok += 1

    log.info(f"\n{amc} merge complete: {ok} months added, {skipped} skipped, {failed} failed")


def backfill(
    mf_data: Path,
    lookback_months: int = 40,
    force: bool = False,
    amc_filter: Optional[str] = None,
    month_filter: Optional[str] = None,
):
    """
    Download all available historical holdings, merge by month, cache to parquet.
    """
    hold_dir = mf_data / "holdings"
    hold_dir.mkdir(parents=True, exist_ok=True)

    # Load scheme list for fuzzy matching
    scheme_path = mf_data / "scheme_list.parquet"
    scheme_df = pd.read_parquet(scheme_path) if scheme_path.exists() else None
    if scheme_df is not None and "amc" not in scheme_df.columns and "amc_name" in scheme_df.columns:
        scheme_df = scheme_df.rename(columns={"amc_name": "amc"})

    # Build URL maps
    log.info("Building URL maps for all AMCs …")
    url_maps = build_url_maps(lookback_months)

    # Determine all months to process
    today = datetime.date.today()
    all_months = []
    for i in range(1, lookback_months + 1):
        d = today.replace(day=1) - datetime.timedelta(days=1)
        for _ in range(i - 1):
            d = d.replace(day=1) - datetime.timedelta(days=1)
        all_months.append(f"{d.year}-{d.month:02d}")
    if month_filter:
        all_months = [m for m in all_months if m == month_filter]

    hdfc_all_urls = url_maps.get("HDFC", {}).pop("_all_urls", [])
    amcs_to_run = [amc_filter] if amc_filter else list(url_maps.keys())

    log.info(f"Processing {len(all_months)} months × {len(amcs_to_run)} AMCs")

    for month_str in sorted(all_months, reverse=True):
        out_path = hold_dir / f"{month_str}.parquet"
        if not force and out_path.exists():
            log.info(f"  {month_str}: already cached — skipping")
            continue

        log.info(f"\n{'─'*60}")
        log.info(f"  Month: {month_str}")
        month_dfs: list[pd.DataFrame] = []

        for amc in amcs_to_run:
            amc_map = url_maps.get(amc, {})

            # HDFC special case
            if amc == "HDFC":
                df = _download_hdfc_month(month_str, hdfc_all_urls)
                if not df.empty:
                    df["amc"] = amc
                    month_dfs.append(df)
                    log.info(f"    HDFC: {len(df)} rows")
                continue

            url = amc_map.get(month_str)
            if not url:
                log.debug(f"    {amc}: no URL for {month_str}")
                continue

            df = _download_from_url(url, amc, month_str)
            if df.empty:
                log.info(f"    {amc}: empty (404 or parse failed)")
                continue

            df["amc"] = amc
            log.info(f"    {amc}: {len(df):,} rows, {df['scheme_name'].nunique()} schemes")
            month_dfs.append(df)

        if not month_dfs:
            log.warning(f"  {month_str}: no data from any AMC")
            continue

        combined = pd.concat(month_dfs, ignore_index=True)

        # Fuzzy scheme matching
        if scheme_df is not None:
            combined = attach_scheme_codes(combined, scheme_df)
            combined["scheme_code"] = pd.to_numeric(
                combined["scheme_code"], errors="coerce"
            ).astype("Int64")
        else:
            combined["scheme_code"] = pd.NA

        combined["as_of_date"] = month_str
        combined.to_parquet(out_path, index=False)

        n_matched = combined["scheme_code"].notna().sum()
        log.info(f"  {month_str}: {len(combined):,} rows total, "
                 f"{n_matched:,} matched ({n_matched/len(combined)*100:.0f}%)"
                 f" → {out_path.name}")

    # Summary
    cached = sorted(hold_dir.glob("*.parquet"))
    log.info(f"\n{'='*60}")
    log.info(f"Holdings cache: {len(cached)} months")
    if cached:
        log.info(f"  Earliest: {cached[0].stem}")
        log.info(f"  Latest  : {cached[-1].stem}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Allow direct call: python backfill_holdings.py --merge-amc SBI
    p = argparse.ArgumentParser(description="Backfill historical AMC portfolio holdings")
    p.add_argument("--mf-data",          default="./mf_data")
    p.add_argument("--lookback-months",  type=int, default=40,
                   help="How many months back to attempt (default: 40)")
    p.add_argument("--amc",             default=None,
                   help="Single AMC for full backfill (e.g. SBI, DSP, Franklin)")
    p.add_argument("--merge-amc",       default=None,
                   help="Merge one AMC into existing parquets without re-downloading others")
    p.add_argument("--month",           default=None,
                   help="Single month to process (YYYY-MM)")
    p.add_argument("--force",           action="store_true",
                   help="Re-download even if cached / overwrite existing AMC rows")
    p.add_argument("--debug",           action="store_true")
    args = p.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.merge_amc:
        merge_amc_into_cache(
            amc=args.merge_amc,
            mf_data=Path(args.mf_data),
            lookback_months=args.lookback_months,
            force=args.force,
            month_filter=args.month,
        )
    else:
        backfill(
            mf_data=Path(args.mf_data),
            lookback_months=args.lookback_months,
            force=args.force,
            amc_filter=args.amc,
            month_filter=args.month,
        )


if __name__ == "__main__":
    main()
