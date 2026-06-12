#!/usr/bin/env python3
"""
ingest_phase1.py
================
Backfill + monthly pull of monthly portfolio holdings for the 8 "Phase 1"
fund houses that currently have ZERO holdings coverage and publish a single
consolidated Excel per month at a scrapeable (non-JS) URL:

    Mahindra Manulife, Bank of India, ITI, Groww, Shriram, 360 ONE, Bajaj, NJ

Design
------
* Parsing/download reuses the proven engine in amc_holdings_scraper.py
  (handles consolidated multi-scheme workbooks + dynamic header detection).
* Each house has a `discover()` that scrapes its disclosure page for the
  month's file URL(s). Discovery is best-effort and MUST be validated live
  with --dry-run on a machine with network access (the build sandbox can't
  reach these domains).
* Parsed scheme names are matched to REGULAR-plan scheme codes from
  fund_meta.parquet (these houses were never in the old direct universe).
  The step-2.5 remap passes regular codes through unchanged.
* Output: appends to mf_data/holdings/{YYYY-MM}.parquet (same schema as
  fetch_new_holdings.py), so holdings_attribution.py picks it up automatically.

Usage
-----
    python3 ingest_phase1.py --amc all --dry-run          # discover + match report, NO download/write
    python3 ingest_phase1.py --amc shriram --months 36    # backfill 36 months for one house
    python3 ingest_phase1.py --amc all --months 36        # backfill everything
    python3 ingest_phase1.py --amc mahindra --month 2026-05
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import amc_holdings_scraper as ahs            # reuse _curl / _load_workbook_bytes / _parse_workbook / _parse_zip
from rapidfuzz import fuzz, process

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("phase1")

MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,
          "oct":10,"nov":11,"dec":12,
          "january":1,"february":2,"march":3,"april":4,"june":6,"july":7,
          "august":8,"september":9,"october":10,"november":11,"december":12}


# ─── month helpers ──────────────────────────────────────────────────────────
def recent_months(n: int) -> list[str]:
    out, today = [], date.today()
    y, m = today.year, today.month
    for _ in range(n):
        m -= 1
        if m == 0:
            m = 12; y -= 1
        out.append(f"{y}-{m:02d}")
    return out


def guess_month(*texts: str) -> str | None:
    """Find a YYYY-MM from a filename/anchor text. Handles 'April 2026',
    'apr2026', '30042026', '30-04-2026', '2026-04', 'May-2026', etc."""
    blob = " ".join(t for t in texts if t)
    low = blob.lower()
    # explicit YYYY-MM or YYYY_MM
    m = re.search(r"(20\d{2})[-_/](0[1-9]|1[0-2])\b", low)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    # DDMMYYYY (e.g. 30042026) or DD-MM-YYYY
    m = re.search(r"\b(\d{2})[-_/]?(0[1-9]|1[0-2])[-_/]?(20\d{2})\b", low)
    if m:
        return f"{m.group(3)}-{m.group(2)}"
    # month-name + year
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-_,]*(20\d{2})", low)
    if m:
        return f"{m.group(2)}-{MONTHS[m.group(1)]:02d}"
    return None


# ─── regular scheme-code matcher ────────────────────────────────────────────
_STRIP = re.compile(r"\b(regular|direct|plan|growth|option|idcw|dividend|reinvest\w*|payout)\b|[-–—()]", re.I)

def _norm(s: str) -> str:
    s = re.sub(r"\([^)]*\)", " ", str(s))   # drop "(formerly known as …)" etc.
    return re.sub(r"\s+", " ", _STRIP.sub(" ", s)).strip().lower()

class CodeMatcher:
    """Match a parsed scheme name -> regular scheme_code for a given AMC."""
    def __init__(self, fund_meta: pd.DataFrame, amc_substrs: list[str]):
        amc = fund_meta.copy()
        amc["_amc"] = amc["amc"].astype(str).str.replace("_", " ")
        mask = False
        for s in amc_substrs:
            mask = mask | amc["_amc"].str.contains(s, case=False, na=False)
        amc = amc[mask]
        self.choices = {int(r.scheme_code): _norm(r.scheme_name) for _, r in amc.iterrows()}
        self.by_code = {int(r.scheme_code): r.scheme_name for _, r in amc.iterrows()}
        self.n = len(self.choices)

    def match(self, scheme_name: str, threshold: int = 80):
        q = _norm(scheme_name)
        if not q or not self.choices:
            return None, 0
        best_code, best_score = None, 0
        for code, name in self.choices.items():
            sc = fuzz.token_sort_ratio(q, name)
            if sc > best_score:
                best_code, best_score = code, sc
        return (best_code, best_score) if best_score >= threshold else (None, best_score)


_NAV_KW = ("% to nav", "% to aum", "% to net assets", "% net assets",
           "%tonav", "%toaum", "% of nav", "weightage", "% to net asset")
_ASON_RE = re.compile(r"as on\s+([a-z]+)\.?\s+\d{1,2}\s*,?\s*(20\d{2})", re.I)
# day-first variant: "as on 30-APR-2024", "as on 30 April 2024"
_ASON_RE2 = re.compile(r"as on\s+\d{1,2}[-/ ]([a-z]+)[-/ ,]+(20\d{2})", re.I)

def _nh(v) -> str:
    return re.sub(r"\s+", " ", str(v).lower()).strip() if v is not None else ""


def load_sheets(data: bytes):
    """Return [(sheet_name, rows)] for BOTH .xlsx and old-format .xls (OLE),
    using pandas (openpyxl for xlsx, xlrd for xls). rows = list of tuples."""
    import io
    try:
        sh = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=object)
    except Exception:
        return []
    return [(n, [tuple(r) for r in d.itertuples(index=False, name=None)]) for n, d in sh.items()]


def month_from_rows(sheets) -> str | None:
    """Authoritative month from the file's own header ('... as on April 30, 2026')."""
    for _, rows in sheets:
        for i, row in enumerate(rows[:40]):
            for v in row:
                if isinstance(v, str):
                    m = _ASON_RE.search(v) or _ASON_RE2.search(v)
                    if m:
                        mo = MONTHS.get(m.group(1).lower()[:3])
                        if mo:
                            return f"{m.group(2)}-{mo:02d}"
    return None


def _extract_holdings(rows) -> list[dict]:
    """SEBI-format holdings extraction on a list-of-rows sheet (format-agnostic)."""
    hidx = None
    for i, row in enumerate(rows):
        cells = [_nh(c) for c in row]
        if any("isin" in c for c in cells) and any(any(k in c for k in _NAV_KW) for c in cells):
            hidx = i; break
    if hidx is None:
        for i, row in enumerate(rows):
            if any(_nh(c) == "isin" for c in row):
                hidx = i; break
    if hidx is None:
        return []
    headers = [_nh(c) for c in rows[hidx]]
    isin_col = next((i for i, h in enumerate(headers) if h == "isin"), None)
    if isin_col is None:
        isin_col = next((i for i, h in enumerate(headers) if "isin" in h), None)
    nav_col = next((i for i, h in enumerate(headers) if any(k in h for k in _NAV_KW)), None)
    name_col = next((i for i, h in enumerate(headers) if "name" in h and "instrument" in h), None)
    if name_col is None:
        name_col = next((i for i, h in enumerate(headers) if "name" in h and i != isin_col), None)
    if isin_col is None or nav_col is None:
        return []
    recs = []
    for row in rows[hidx + 1:]:
        if len(row) <= max(isin_col, nav_col):
            continue
        iv = row[isin_col]
        if not ahs._is_isin(iv):
            continue
        try:
            pct = float(row[nav_col])
        except (ValueError, TypeError):
            continue
        if pct <= 0 or pct > 100:
            continue
        nm = str(row[name_col] or "").strip() if (name_col is not None and name_col < len(row)) else ""
        recs.append({"isin": str(iv).strip(), "stock_name": nm, "pct_nav": pct})
    if recs and max(r["pct_nav"] for r in recs) <= 1.5:      # decimal fractions → %
        for r in recs:
            r["pct_nav"] = round(r["pct_nav"] * 100, 4)
    return recs


def parse_consolidated(sheets, matcher: "CodeMatcher", threshold: int = 82) -> pd.DataFrame:
    """Each sheet is one scheme. Identify the scheme by matching the sheet's
    header text against fund_meta; skip sheets that don't match our universe
    (debt/liquid funds, index/summary sheets)."""
    out = []
    for name, rows in sheets:
        if str(name).lower() in ("index",):
            continue
        best_code, best_score = None, 0
        for row in rows[:10]:
            for v in row:
                if not isinstance(v, str):
                    continue
                # Some files bundle the scheme-type description into the name
                # cell — after a newline, inside "(...)", or joined with " - "
                # (e.g. "360 ONE Focused Fund - An Open Ended Equity Scheme…").
                # Try progressively shorter candidates; best score wins.
                first = v.split("\n")[0].split("(")[0].strip()
                no_pfx = re.sub(r"^\s*scheme\s*[-:–]\s*", "", first, flags=re.I)
                # strip leading sheet/scheme codes: "IB01-Groww Large Cap Fund"
                no_code = re.sub(r"^\s*[A-Z]{1,4}\d{1,4}\s*[-:–]\s*", "", first)
                for cand in (first, first.split(" - ")[0].strip(), no_pfx, no_code):
                    if 8 < len(cand) < 120 and "mutual fund" not in cand.lower():
                        c, s = matcher.match(cand, threshold=0)
                        if s > best_score:
                            best_code, best_score = c, s
        if best_code is None or best_score < threshold:
            continue
        recs = _extract_holdings(rows)
        if not recs:
            continue
        df = pd.DataFrame(recs)
        df["scheme_code"] = best_code
        df["scheme_name"] = matcher.by_code[best_code]
        df["_sheet"] = str(name)
        out.append(df)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


# ─── per-AMC discovery ──────────────────────────────────────────────────────
# Each entry: meta_substrs (to find its schemes in fund_meta), and a list of
# disclosure pages to scrape for file links. Discovery grabs (href, anchor-text)
# pairs, keeps .xls/.xlsx/.zip, and assigns a month via guess_month(url, text).
AMC_CONFIG = {
    "mahindra": dict(label="Mahindra Manulife", meta=["mahindra"],
                     ak="Mahindra-Manulife-Mutual-Fund",
                     pages=["https://www.mahindramanulife.com/downloads"]),
    "boi":      dict(label="Bank of India", meta=["bank of", "bank of india", "boi"],
                     ak="Bank-of-India-Mutual-Fund",
                     pages=["https://www.boimf.in/regulatory-reports"]),
    "iti":      dict(label="ITI", meta=["iti"],
                     ak="ITI-Mutual-Fund",
                     pages=["https://www.itiamc.com/statutory-disclosure/monthly-portfolios"]),
    "groww":    dict(label="Groww", meta=["groww"],
                     ak="Groww-Mutual-Fund",
                     pages=["https://www.growwmf.in/statutory-disclosure/portfolio"]),
    "shriram":  dict(label="Shriram", meta=["shriram"],
                     ak="Shriram-Mutual-Fund",
                     pages=["https://www.shriramamc.in/investor-statutory-disclosures"]),
    "360one":   dict(label="360 ONE", meta=["360", "iifl"],
                     ak="360-ONE-Mutual-Fund",
                     pages=["https://archive.iiflmf.com/downloads/disclosures"]),
    "bajaj":    dict(label="Bajaj Finserv", meta=["bajaj"],
                     ak="Bajaj-Finserv-Mutual-Fund",
                     pages=["https://www.bajajamc.com/downloads"]),
    "nj":       dict(label="NJ", meta=["nj"],
                     ak="NJ-Mutual-Fund",
                     pages=["https://www.njmutualfund.com/Monthly-Portfolio.php"]),
    # not a Phase-1 house: label matches the EXISTING holdings tag so that
    # merge_month replaces old rows per re-ingested month (no duplicates),
    # while months not re-supplied keep their original pipeline data.
    "icici":    dict(label="ICICI_Pru", meta=["icici"],
                     ak="ICICI-Prudential-Mutual-Fund",
                     pages=[]),
    "absl":     dict(label="Aditya_Birla", meta=["aditya", "birla"],
                     ak="Aditya-Birla-Sun-Life-Mutual-Fund",
                     pages=[]),
}

LINK_RE = re.compile(r'<a[^>]+href=["\']([^"\']+\.(?:xlsx|xls|zip))["\'][^>]*>(.*?)</a>',
                     re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")


CACHE_DIR = HERE / "mf_data" / "_phase1_cache"

def _scrape_page(label: str, page: str) -> dict[str, str]:
    """Fetch a disclosure page and extract {month: url}. Retries until it
    actually finds portfolio links (a JS shell can be long but linkless)."""
    base = re.match(r"(https?://[^/]+)", page).group(1)
    found: dict[str, str] = {}
    for attempt in range(5):
        try:
            html = ahs._curl(page, binary=False)
        except Exception as e:
            log.debug(f"  {label}: fetch {attempt+1} err {e}"); html = None
        if html:
            for href, text in LINK_RE.findall(html):
                url = href if href.startswith("http") else base + ("" if href.startswith("/") else "/") + href.lstrip("/")
                mon = guess_month(href, TAG_RE.sub(" ", text))
                if mon and "portfolio" in (href + text).lower():
                    found.setdefault(mon, url)
        if found:
            return found
        __import__("time").sleep(2)
    return found


def discover(amc_key: str) -> list[tuple[str, str]]:
    """Return [(month, url)] for an AMC. Caches good catalogs to disk and falls
    back to the cache when a page transiently serves a JS shell, so flaky pages
    can't wipe a previously-good catalog."""
    cfg = AMC_CONFIG[amc_key]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_f = CACHE_DIR / f"{amc_key}.json"

    found: dict[str, str] = {}
    # 1) AdvisorKhoj static catalog — reliable, clean month-encoded URLs (primary)
    if cfg.get("ak"):
        found.update(_advisorkhoj_map(cfg["ak"], cfg["label"]))
    # 2) fallback: the AMC's own page (flaky / often JS-gated)
    if not found:
        for page in cfg["pages"]:
            found.update(_scrape_page(cfg["label"], page))

    import json
    if found:                       # overwrite cache with the fresh good catalog
        cache_f.write_text(json.dumps(found, indent=0))
        log.info(f"  {cfg['label']}: catalog = {len(found)} months (cached)")
    elif cache_f.exists():
        try: found = json.loads(cache_f.read_text())
        except Exception: found = {}
        log.warning(f"  {cfg['label']}: live sources empty — using cached catalog ({len(found)})")
    else:
        log.warning(f"  {cfg['label']}: no catalog from AdvisorKhoj or AMC page")
    return sorted(found.items())


def _advisorkhoj_map(slug: str, label: str) -> dict[str, str]:
    """Scrape AdvisorKhoj's static SSR catalog for an AMC -> {month: url}.
    Clean month-encoded filenames pointing at the AMC's CDN (the reliable
    source the existing AMC backfills use)."""
    page = ("https://www.advisorkhoj.com/form-download-centre/Mutual/"
            f"{slug}/Monthly-Portfolio-Disclosures")
    res: dict[str, str] = {}
    for attempt in range(4):
        try:
            html = ahs._curl(page, binary=False)
        except Exception:
            html = None
        if html:
            for href in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
                low = href.lower()
                if not any(low.endswith(e) for e in (".xlsx", ".xls", ".zip")):
                    continue
                mon = guess_month(href)
                if mon:
                    url = href if href.startswith("http") else \
                        "https://www.advisorkhoj.com" + (href if href.startswith("/") else "/" + href)
                    res.setdefault(mon, url)
        if res:
            break
        __import__("time").sleep(2)
    log.info(f"  {label}: AdvisorKhoj → {len(res)} months "
             f"({min(res) if res else '—'}..{max(res) if res else '—'})")
    return res


# ─── ingest ─────────────────────────────────────────────────────────────────
KEEP = ["isin","stock_name","pct_nav","scheme_name","_sheet","amc","scheme_code","as_of_date","year","month"]

def merge_month(rows: pd.DataFrame, month: str, amc_tag: str, hold_dir: Path) -> None:
    rows = rows.copy()
    rows["amc"] = amc_tag
    rows["_sheet"] = amc_tag
    # match the existing files' datetime64 month-end as_of_date (attribution
    # keys off the FILENAME, so the column is cosmetic — but dtypes must agree
    # or the concat→parquet write fails with an ArrowTypeError).
    rows["as_of_date"] = pd.Timestamp(f"{month}-01") + pd.offsets.MonthEnd(0)
    rows["year"] = float(month[:4]); rows["month"] = float(month[5:7])
    rows["scheme_code"] = pd.array(pd.to_numeric(rows["scheme_code"], errors="coerce"), dtype="Float64")
    rows = rows[[c for c in KEEP if c in rows.columns]]
    parq = hold_dir / f"{month}.parquet"
    if parq.exists():
        existing = pd.read_parquet(parq)
        existing = existing[existing["amc"] != amc_tag]
        existing["as_of_date"] = pd.to_datetime(existing["as_of_date"], errors="coerce")
        rows = pd.concat([existing, rows], ignore_index=True)
    rows["as_of_date"] = pd.to_datetime(rows["as_of_date"], errors="coerce")
    rows["scheme_code"] = pd.array(pd.to_numeric(rows["scheme_code"], errors="coerce"), dtype="Float64")
    rows.to_parquet(parq, index=False)


def _fetch_url(url: str, raw: Path) -> bytes | None:
    """Download a URL to bytes, caching the raw file. Retries (host throttling)."""
    import hashlib, time
    cache_f = raw / (hashlib.md5(url.encode()).hexdigest()[:16] + ".bin")
    if cache_f.exists() and cache_f.stat().st_size > 4000:
        return cache_f.read_bytes()
    for attempt in range(3):
        try:
            data = ahs._curl(url, binary=True)
        except Exception:
            data = None
        if data and len(data) > 4000 and data[:2] in (b"PK", b"\xd0\xcf"):
            cache_f.write_bytes(data)
            return data
        time.sleep(1.2 * (attempt + 1))
    return None


def ingest_amc(amc_key: str, months: list[str], matcher: CodeMatcher,
               hold_dir: Path, dry_run: bool) -> dict:
    """Source files in priority order, then parse → match → write. The month is
    always read from the FILE CONTENT, so sources don't need to label months:
      1. LOCAL DROP  data/_phase1_drop/<amc>/*.xls[x]  — files you downloaded in
         a browser (most robust: no scraping/CDN/JS issues at all).
      2. MANUAL URLS data/_phase1_urls/<amc>.txt       — one direct file URL per
         line (copied from AdvisorKhoj's rendered page).
      3. AUTO        AdvisorKhoj / AMC page scrape      — best-effort fallback.
    """
    cfg = AMC_CONFIG[amc_key]
    label = cfg["label"]
    want = set(months)
    data_dir = hold_dir.parent
    raw = data_dir / "holdings_raw" / "phase1" / amc_key
    raw.mkdir(parents=True, exist_ok=True)
    drop = data_dir / "_phase1_drop" / amc_key
    urls_f = data_dir / "_phase1_urls" / f"{amc_key}.txt"

    sources: list[tuple[str, object]] = []     # ("file", Path) or ("url", str)
    if drop.exists() and any(drop.glob("*.xls*")):
        sources = [("file", f) for f in sorted(drop.glob("*.xls*"))]
        log.info(f"  {label}: {len(sources)} local files in _phase1_drop/{amc_key}/")
    elif urls_f.exists():
        urls = [u.strip() for u in urls_f.read_text().splitlines()
                if u.strip() and not u.strip().startswith("#")]
        sources = [("url", u) for u in urls]
        log.info(f"  {label}: {len(sources)} manual URLs from _phase1_urls/{amc_key}.txt")
    else:
        sources = [("url", u) for m, u in discover(amc_key) if not want or m in want]

    stat = {"amc": label, "files": 0, "schemes_matched": 0, "rows": 0, "months": []}
    # Some AMCs (e.g. NJ from Sep-2025) publish ONE FILE PER SCHEME instead of a
    # consolidated workbook — so accumulate every file's rows per month first,
    # then merge each month once. Within a month, duplicate scheme files (re-
    # uploads / "Final" versions) keep the first occurrence of each scheme.
    by_month: dict[str, list[pd.DataFrame]] = {}
    for kind, src in sources:
        data = src.read_bytes() if kind == "file" else _fetch_url(src, raw)
        if not data:
            log.warning(f"  {label}: could not read {str(src)[-36:]}"); continue
        sheets = load_sheets(data)
        if not sheets:
            log.warning(f"  {label}: unreadable file {str(src)[-36:]}"); continue
        mon = month_from_rows(sheets)
        if not mon:
            log.warning(f"  {label}: no 'as on' month in {str(src)[-36:]}"); continue
        if want and mon not in want:
            continue
        try:
            df = parse_consolidated(sheets, matcher)
        except Exception as e:
            log.warning(f"  {label} {mon}: parse error: {e}"); continue
        if df is None or df.empty:
            log.warning(f"  {label} {mon}: no schemes matched (top-10/summary file?)"); continue
        df["_src"] = len(by_month.get(mon, []))
        by_month.setdefault(mon, []).append(df)
    for mon in sorted(by_month):
        df = pd.concat(by_month[mon], ignore_index=True)
        # first file that carries a scheme wins (drop re-uploaded duplicates)
        first_src = df.groupby("scheme_code")["_src"].transform("min")
        df = df[df["_src"] == first_src].drop(columns="_src").reset_index(drop=True)
        n = df["scheme_code"].nunique()
        stat["schemes_matched"] = max(stat["schemes_matched"], n)
        stat["rows"] += len(df); stat["months"].append(mon)
        log.info(f"  {label} {mon}: {n} schemes, {len(df)} rows"
                 f"{' (DRY-RUN)' if dry_run else ''}")
        if not dry_run:
            merge_month(df, mon, label, hold_dir)
    covered = set(by_month)
    stat["files"] = len(covered)
    miss = sorted(want - covered) if want else []
    if miss:
        log.warning(f"  {label}: {len(miss)} target months missing: "
                    f"{miss[:8]}{'…' if len(miss) > 8 else ''}")
    return stat


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--amc", default="all",
                   choices=["all"] + list(AMC_CONFIG.keys()))
    p.add_argument("--months", type=int, default=36, help="how many recent months to backfill")
    p.add_argument("--month", help="single month YYYY-MM (overrides --months)")
    p.add_argument("--dry-run", action="store_true", help="discover + match only; no download/write")
    p.add_argument("--data-dir", default=str(HERE / "mf_data"))
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    hold_dir = data_dir / "holdings"; hold_dir.mkdir(parents=True, exist_ok=True)
    fund_meta = pd.read_parquet(data_dir / "fund_meta.parquet")
    months = [args.month] if args.month else recent_months(args.months)

    targets = list(AMC_CONFIG) if args.amc == "all" else [args.amc]
    summary = []
    for key in targets:
        cfg = AMC_CONFIG[key]
        log.info(f"\n── {cfg['label']} ──")
        matcher = CodeMatcher(fund_meta, cfg["meta"])
        log.info(f"  {matcher.n} regular schemes for this AMC in fund_meta")
        summary.append(ingest_amc(key, months, matcher, hold_dir, args.dry_run))

    log.info("\n" + "=" * 60 + "\nSUMMARY" + (" (dry-run)" if args.dry_run else ""))
    for s in summary:
        log.info(f"  {s['amc']:18s} files={s['files']:3d}  schemes_matched={s['schemes_matched']:3d}  "
                 f"rows={s['rows']:6d}  months={len(s['months'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
