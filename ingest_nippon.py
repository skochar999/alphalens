#!/usr/bin/env python3
"""
ingest_nippon.py — Parse Nippon monthly portfolio .xls files from Archive.zip
and merge them into the holdings parquet cache.

Nippon files come in two formats:
  • Newer months  → ZIP/XLSX renamed as .xls  (use openpyxl)
  • Older months  → OLE2/BIFF true .xls       (use xlrd 2.x)

Both share the same sheet layout:
  - "Index" sheet: maps 2-letter code → full scheme name
  - One sheet per fund:
      row 0 : fund code (col 0), fund name (col 1)
      row 1 : "Monthly Portfolio Statement as on {Month} {DD},{YYYY}"
      row 3 : headers (ISIN, Name, Industry, Quantity, MktVal, % to NAV, YIELD)
      row 6+: holdings rows — ISIN in col 1, pct_nav (decimal) in col 6

pct_nav in the file is stored as a decimal (0.0334 = 3.34%).
Existing parquets store pct_nav as percentage (3.34).  We multiply by 100.
"""

import io
import logging
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest_nippon")

# ── Constants ────────────────────────────────────────────────────────────────

MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}

ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}$")
DATE_RE = re.compile(
    r"as on\s+([A-Za-z]+)\s+\d+[,\s]+(\d{4})", re.IGNORECASE
)


# ── File parsing ─────────────────────────────────────────────────────────────

def _read_all_sheets(raw: bytes) -> dict:
    """Read all sheets from a Nippon file, auto-detecting format."""
    buf = io.BytesIO(raw)
    if raw[:2] == b"PK":
        return pd.read_excel(buf, engine="openpyxl", sheet_name=None, header=None)
    else:
        return pd.read_excel(buf, engine="xlrd", sheet_name=None, header=None)


def _extract_ym(date_text: str) -> str | None:
    """'Monthly Portfolio Statement as on September 30,2025' → '2025-09'."""
    m = DATE_RE.search(str(date_text))
    if m:
        mon = MONTH_MAP.get(m.group(1).lower())
        yr = m.group(2)
        if mon:
            return f"{yr}-{mon}"
    return None


def parse_nippon_file(raw: bytes) -> tuple[pd.DataFrame | None, str | None]:
    """
    Parse one Nippon monthly portfolio file.

    Returns
    -------
    df   : DataFrame with columns [isin, stock_name, pct_nav, scheme_name, _sheet]
           pct_nav is in percentage scale (0-100).
    ym   : 'YYYY-MM' string, or None if date could not be extracted.
    """
    try:
        sheets = _read_all_sheets(raw)
    except Exception as e:
        log.error(f"  Failed to open workbook: {e}")
        return None, None

    # Build code → full name from Index sheet
    code_to_name: dict[str, str] = {}
    idx_key = next((k for k in sheets if k.lower() == "index"), None)
    if idx_key:
        idx_df = sheets[idx_key]
        for _, row in idx_df.iterrows():
            code = str(row[0]).strip() if pd.notna(row[0]) else ""
            name = str(row[1]).strip() if pd.notna(row[1]) else ""
            if code and code.upper() != "INDEX" and name and name != "nan":
                code_to_name[code] = name

    records = []
    ym: str | None = None

    for sheet_code, sheet_df in sheets.items():
        if sheet_code.lower() == "index":
            continue
        if sheet_df is None or len(sheet_df) < 6:
            continue

        # Try to extract date from row 1 col 1 (only needs to succeed once)
        if ym is None:
            raw_date = sheet_df.iloc[1, 1] if sheet_df.shape[1] > 1 else None
            if pd.notna(raw_date):
                ym = _extract_ym(str(raw_date))

        # Resolve fund name: prefer Index sheet mapping
        fund_name = code_to_name.get(sheet_code, "")
        if not fund_name:
            cell = sheet_df.iloc[0, 1] if sheet_df.shape[1] > 1 else None
            fund_name = str(cell).strip() if pd.notna(cell) else ""
        if fund_name == "nan":
            fund_name = ""

        # Collect holding rows: ISIN in col 1, pct_nav (decimal) in col 6
        for _, row in sheet_df.iterrows():
            isin = str(row[1]).strip() if pd.notna(row.get(1)) else ""
            if not ISIN_RE.match(isin):
                continue
            try:
                pct_decimal = float(row[6])
            except (TypeError, ValueError, KeyError):
                continue
            if not (0 < pct_decimal <= 1.05):
                # Values in this file are decimals like 0.0334; skip outliers
                # (allow up to 1.05 to catch ~105% in leveraged funds)
                continue
            pct_val = round(pct_decimal * 100, 4)
            stock_name = str(row[2]).strip() if pd.notna(row.get(2)) else ""
            records.append({
                "isin": isin,
                "stock_name": stock_name,
                "pct_nav": pct_val,
                "scheme_name": fund_name,
                "_sheet": sheet_code,
            })

    if not records:
        return None, ym
    return pd.DataFrame(records), ym


# ── Scheme-code fuzzy matching (mirrors amc_holdings_scraper.py) ─────────────

_PAREN_RE   = re.compile(r"\s*\([^)]*\)")
_SUFFIX_RE  = re.compile(
    r"\s*-\s*(Direct|Regular|Growth|Dividend|IDCW|Plan|Option)\b.*",
    re.IGNORECASE,
)


def _clean_nippon_name(s: str) -> str:
    """Strip SEBI category descriptions and plan/option suffixes for matching."""
    s = _PAREN_RE.sub("", s)       # remove "(An open ended...)" etc.
    s = _SUFFIX_RE.sub("", s)      # remove "- Direct Plan Growth..."
    return s.strip().lower()


def _build_matcher(scheme_df: pd.DataFrame):
    """Return a callable(amc, scheme_name) → scheme_code | None."""
    import difflib

    # Build cleaned index for Nippon AMC entries
    nippon_rows = scheme_df[
        scheme_df.get("amc", pd.Series(dtype=str))
        .str.contains("Nippon|nippon", na=False, case=False)
    ]

    # Also include all rows for generic fallback
    clean_to_code: dict[str, str] = {}
    clean_names: list[str] = []
    codes: list[str] = []

    for _, row in nippon_rows.iterrows():
        raw_name = str(row.get("scheme_name", ""))
        code     = str(row.get("scheme_code", ""))
        cleaned  = _clean_nippon_name(raw_name)
        if cleaned not in clean_to_code:
            clean_to_code[cleaned] = code
            clean_names.append(cleaned)
            codes.append(code)

    def match(amc: str, scheme_name: str) -> str | None:
        query = _clean_nippon_name(scheme_name)
        # Exact cleaned match
        if query in clean_to_code:
            return clean_to_code[query]
        # Fuzzy
        best = difflib.get_close_matches(query, clean_names, n=1, cutoff=0.50)
        if best:
            return clean_to_code[best[0]]
        return None

    return match


def attach_scheme_codes(df: pd.DataFrame, scheme_df: pd.DataFrame) -> pd.DataFrame:
    matcher = _build_matcher(scheme_df)
    cache: dict[str, str | None] = {}

    def _get(row):
        key = row["scheme_name"]
        if key not in cache:
            cache[key] = matcher("Nippon", key)
        return cache[key]

    df = df.copy()
    df["scheme_code"] = df.apply(_get, axis=1)
    n_matched = df["scheme_code"].notna().sum()
    log.info(f"  Scheme matching: {n_matched}/{len(df)} rows matched "
             f"({df.loc[df['scheme_code'].notna(), 'scheme_code'].nunique()} unique schemes)")

    unmatched = (
        df[df["scheme_code"].isna()][["scheme_name"]]
        .drop_duplicates().head(10)
    )
    if not unmatched.empty:
        log.warning(f"  Unmatched scheme names:\n{unmatched.to_string(index=False)}")

    return df


# ── Holdings-cache merge ─────────────────────────────────────────────────────

def merge_nippon_into_cache(
    zip_path: Path,
    hold_dir: Path,
    scheme_df: pd.DataFrame | None,
    force: bool = False,
) -> None:
    """
    For each Nippon file in Archive.zip:
      1. Parse → DataFrame + YYYY-MM
      2. Add amc='Nippon', as_of_date=YYYY-MM
      3. Attach scheme_codes
      4. Load existing parquet for that month (if any) and append/replace Nippon rows
      5. Save updated parquet
    """
    hold_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        nippon_names = sorted(
            n for n in zf.namelist()
            if "Nippon/" in n and n.endswith(".xls") and "__MACOSX" not in n
        )
        log.info(f"Found {len(nippon_names)} Nippon .xls files in {zip_path.name}")

        for zname in nippon_names:
            short = zname.split("/")[-1]
            log.info(f"\n── {short}")

            raw = zf.read(zname)
            if len(raw) < 1000:
                log.warning(f"  Skipping (too small: {len(raw)} bytes)")
                continue

            df, ym = parse_nippon_file(raw)
            if df is None or df.empty:
                log.warning(f"  No holdings parsed")
                continue
            if ym is None:
                log.warning(f"  Could not determine month — skipping")
                continue

            log.info(f"  Month={ym}  rows={len(df)}  schemes={df['_sheet'].nunique()}")

            # Attach metadata
            df["amc"] = "Nippon"
            df["as_of_date"] = ym

            # Fuzzy scheme matching
            if scheme_df is not None:
                df = attach_scheme_codes(df, scheme_df)
                df["scheme_code"] = pd.to_numeric(
                    df["scheme_code"], errors="coerce"
                ).astype("Int64")
            else:
                df["scheme_code"] = pd.NA

            # Ensure consistent column order
            COLS = ["isin", "stock_name", "pct_nav", "scheme_name", "_sheet",
                    "amc", "scheme_code", "as_of_date"]
            for c in COLS:
                if c not in df.columns:
                    df[c] = pd.NA
            df = df[COLS]

            # Merge into existing parquet
            out_path = hold_dir / f"{ym}.parquet"
            if out_path.exists():
                existing = pd.read_parquet(out_path)
                if "Nippon" in existing["amc"].values:
                    if not force:
                        nippon_rows = (existing["amc"] == "Nippon").sum()
                        log.info(f"  {ym}: Nippon already present "
                                 f"({nippon_rows} rows) — skipping (use --force to overwrite)")
                        continue
                    else:
                        existing = existing[existing["amc"] != "Nippon"]
                        log.info(f"  {ym}: Replacing existing Nippon rows (--force)")

                # Align columns
                for c in COLS:
                    if c not in existing.columns:
                        existing[c] = pd.NA
                existing = existing[COLS]
                combined = pd.concat([existing, df], ignore_index=True)
            else:
                combined = df

            combined.to_parquet(out_path, index=False)
            n_matched = combined["scheme_code"].notna().sum()
            log.info(f"  Saved {out_path.name}: {len(combined):,} rows total, "
                     f"{n_matched:,} matched "
                     f"({n_matched/len(combined)*100:.0f}%)")

    # Print summary
    cached = sorted(hold_dir.glob("*.parquet"))
    log.info(f"\n{'='*60}")
    log.info(f"Holdings cache: {len(cached)} months")
    for p in cached:
        df_c = pd.read_parquet(p)
        amcs = sorted(df_c["amc"].unique())
        log.info(f"  {p.stem}: {len(df_c):,} rows  AMCs={amcs}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Ingest Nippon files from Archive.zip into holdings cache")
    ap.add_argument("--zip",       default="./uploads/Archive.zip",
                    help="Path to Archive.zip")
    ap.add_argument("--mf-data",   default="./mf_data",
                    help="Directory containing holdings/ and scheme_list.parquet")
    ap.add_argument("--force",     action="store_true",
                    help="Overwrite existing Nippon rows in parquets")
    ap.add_argument("--debug",     action="store_true")
    args = ap.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    mf_data   = Path(args.mf_data)
    zip_path  = Path(args.zip)
    hold_dir  = mf_data / "holdings"

    if not zip_path.exists():
        log.error(f"Archive not found: {zip_path}")
        sys.exit(1)

    # Load scheme list
    scheme_path = mf_data / "scheme_list.parquet"
    scheme_df = None
    if scheme_path.exists():
        scheme_df = pd.read_parquet(scheme_path)
        if "amc" not in scheme_df.columns and "amc_name" in scheme_df.columns:
            scheme_df = scheme_df.rename(columns={"amc_name": "amc"})
        log.info(f"Loaded scheme list: {len(scheme_df)} schemes")
    else:
        log.warning(f"scheme_list.parquet not found at {scheme_path} — scheme_codes will be NA")

    merge_nippon_into_cache(zip_path, hold_dir, scheme_df, force=args.force)


if __name__ == "__main__":
    main()
