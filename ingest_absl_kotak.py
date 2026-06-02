#!/usr/bin/env python3
"""
ingest_absl_kotak.py — Parse ABSL and Kotak monthly portfolio files
from Archive 2.zip and merge them into the holdings parquet cache.

───────────────────────────────────────────────────────────────────
ABSL format (nested ZIPs inside Archive 2.zip):
  Archive 2.zip → ABSL/{month}.zip → {month}.xls  (OLE2/BIFF)

  Sheet layout (same structure as Nippon/SBI multi-sheet format):
    Index sheet:  col0=NaN, col1=fund_code, col2=fund_name
    Per-fund sheet:
      row 0:  fund_code (col0), fund_name (col1)
      row 1:  description or NaN
      row 2:  "... Portfolio Statement as on {Month} {DD}, {YYYY}"
      row 3:  headers (col1=Name, col2=ISIN, col3=Industry, ...)
      row 4+: equity section + holding rows
    Holdings: ISIN in col 2, stock_name in col 1, pct_nav (decimal) in col 6
    pct_nav stored as decimal (0.094959 = 9.5%) → multiply by 100

───────────────────────────────────────────────────────────────────
Kotak format (direct .xls / .xlsx files inside Archive 2.zip):
  Archive 2.zip → Kotak/Consolidated{Month}{Year}.xls[x]

  No Index sheet — sheet name IS the fund code (e.g. TIF, TCH).
  Per-fund sheet:
    row 0:  NaN (col0), NaN (col1), "Portfolio of Kotak {Name} as on {DD}-{MMM}-{YYYY}"
    row 1:  headers (Name@col0, ISIN@col3, Industry@col4, Qty@col6, MktVal@col7, %NAV@col8)
    row 2+: section headers / holding rows
  Holdings: ISIN in col 3, stock_name in col 2, pct_nav (percentage) in col 8
  pct_nav already in percentage scale (20.07 = 20.07%) — NO multiplication needed

───────────────────────────────────────────────────────────────────
Usage:
  python ingest_absl_kotak.py [--zip ./uploads/Archive\ 2.zip]
                               [--mf-data ./mf_data]
                               [--amc ABSL|Kotak|both]
                               [--force]
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
log = logging.getLogger("ingest_absl_kotak")

# ── Constants ────────────────────────────────────────────────────────────────

ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}$")

# Matches "as on January 31, 2025" OR "as on April 30, 2026"  (ABSL / older)
DATE_RE_ABSL = re.compile(
    r"as on\s+([A-Za-z]+)\s+\d+[,\s]+(\d{4})", re.IGNORECASE
)

# Matches "as on 31-Jan-2025" OR "as on 30-Apr-2026"  (Kotak)
DATE_RE_KOTAK = re.compile(
    r"as on\s+(\d{1,2})[- ]([A-Za-z]{3,9})[- ](\d{4})", re.IGNORECASE
)

MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

COLS = ["isin", "stock_name", "pct_nav", "scheme_name", "_sheet",
        "amc", "scheme_code", "as_of_date"]


# ── Shared helpers ────────────────────────────────────────────────────────────

def _read_all_sheets(raw: bytes) -> dict:
    """Auto-detect OLE2 vs XLSX and read all sheets."""
    buf = io.BytesIO(raw)
    if raw[:2] == b"PK":
        return pd.read_excel(buf, engine="openpyxl", sheet_name=None, header=None)
    else:
        return pd.read_excel(buf, engine="xlrd", sheet_name=None, header=None)


def _safe_str(val) -> str:
    if pd.isna(val):
        return ""
    return str(val).strip()


# ── Scheme-code fuzzy matching ────────────────────────────────────────────────

import difflib

_PAREN_RE  = re.compile(r"\s*\([^)]*\)")
_SUFFIX_RE = re.compile(
    r"\s*-\s*(Direct|Regular|Growth|Dividend|IDCW|Plan|Option)\b.*",
    re.IGNORECASE,
)


def _clean_name(s: str) -> str:
    s = _PAREN_RE.sub("", s)
    s = _SUFFIX_RE.sub("", s)
    return s.strip().lower()


def _build_matcher(scheme_df: pd.DataFrame, amc_keyword: str):
    """Build fuzzy name→scheme_code matcher filtered to one AMC."""
    rows = scheme_df[
        scheme_df.get("amc", pd.Series(dtype=str))
        .str.contains(amc_keyword, na=False, case=False)
    ]
    clean_to_code: dict[str, str] = {}
    clean_names: list[str] = []
    codes: list[str] = []
    for _, row in rows.iterrows():
        raw_name = str(row.get("scheme_name", ""))
        code     = str(row.get("scheme_code", ""))
        cleaned  = _clean_name(raw_name)
        if cleaned not in clean_to_code:
            clean_to_code[cleaned] = code
            clean_names.append(cleaned)
            codes.append(code)

    def match(scheme_name: str) -> str | None:
        query = _clean_name(scheme_name)
        if query in clean_to_code:
            return clean_to_code[query]
        best = difflib.get_close_matches(query, clean_names, n=1, cutoff=0.50)
        if best:
            return clean_to_code[best[0]]
        return None

    return match


def attach_scheme_codes(df: pd.DataFrame, scheme_df: pd.DataFrame,
                        amc_keyword: str, amc_label: str) -> pd.DataFrame:
    matcher = _build_matcher(scheme_df, amc_keyword)
    cache: dict[str, str | None] = {}

    def _get(row):
        key = row["scheme_name"]
        if key not in cache:
            cache[key] = matcher(key)
        return cache[key]

    df = df.copy()
    df["scheme_code"] = df.apply(_get, axis=1)
    n_matched = df["scheme_code"].notna().sum()
    total     = len(df)
    log.info(f"  Scheme matching: {n_matched}/{total} rows matched "
             f"({df.loc[df['scheme_code'].notna(), 'scheme_code'].nunique()} unique schemes)")
    unmatched = (
        df[df["scheme_code"].isna()][["scheme_name"]]
        .drop_duplicates().head(8)
    )
    if not unmatched.empty:
        log.warning(f"  Unmatched:\n{unmatched.to_string(index=False)}")
    return df


# ── ABSL parser ───────────────────────────────────────────────────────────────

def _extract_ym_absl(text: str) -> str | None:
    m = DATE_RE_ABSL.search(text)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower())
        yr  = m.group(2)
        if mon:
            return f"{yr}-{mon}"
    return None


def _build_absl_index(sheets: dict) -> dict[str, str]:
    """Build code→name from ABSL Index sheet (col1=code, col2=name)."""
    code_to_name: dict[str, str] = {}
    idx_key = next((k for k in sheets if k.lower() == "index"), None)
    if not idx_key:
        return code_to_name
    idx_df = sheets[idx_key]
    sheet_set = set(sheets.keys()) - {"index", "Index"}
    for _, row in idx_df.iterrows():
        if len(row) < 3:
            continue
        c1 = _safe_str(row[1])
        c2 = _safe_str(row[2])
        if c1 and c1 in sheet_set and c2 and c2.lower() not in ("fund name", "nan"):
            code_to_name[c1] = c2[:120]
    return code_to_name


def parse_absl_file(raw: bytes) -> tuple[pd.DataFrame | None, str | None]:
    """
    Parse one ABSL monthly portfolio file.

    Returns (df, ym) where df has columns:
      [isin, stock_name, pct_nav, scheme_name, _sheet]
    pct_nav is in percentage scale (0-100).
    """
    try:
        sheets = _read_all_sheets(raw)
    except Exception as e:
        log.error(f"  Cannot open workbook: {e}")
        return None, None

    code_to_name = _build_absl_index(sheets)
    records = []
    ym: str | None = None

    for sheet_code, sheet_df in sheets.items():
        if sheet_code.lower() == "index":
            continue
        if sheet_df is None or len(sheet_df) < 6:
            continue

        # Extract date from row 2, col 1  (ABSL standard placement)
        if ym is None:
            for row_idx in [2, 1, 0]:
                if sheet_df.shape[0] > row_idx and sheet_df.shape[1] > 1:
                    raw_date = sheet_df.iloc[row_idx, 1]
                    if pd.notna(raw_date):
                        ym = _extract_ym_absl(str(raw_date))
                        if ym:
                            break

        # Resolve fund name
        fund_name = code_to_name.get(sheet_code, "")
        if not fund_name:
            cell = sheet_df.iloc[0, 1] if sheet_df.shape[1] > 1 else None
            fund_name = _safe_str(cell) if pd.notna(cell) else ""
        if fund_name.lower() == "nan":
            fund_name = ""

        # Collect holdings: ISIN in col 2, stock_name in col 1, pct_nav decimal in col 6
        for _, row in sheet_df.iterrows():
            isin = _safe_str(row.get(2, ""))
            if not ISIN_RE.match(isin):
                continue
            try:
                pct_decimal = float(row[6])
            except (TypeError, ValueError, KeyError):
                continue
            if not (0 < pct_decimal <= 1.05):
                continue  # must be decimal 0–1
            pct_val     = round(pct_decimal * 100, 4)
            stock_name  = _safe_str(row.get(1, ""))
            records.append({
                "isin":        isin,
                "stock_name":  stock_name,
                "pct_nav":     pct_val,
                "scheme_name": fund_name,
                "_sheet":      sheet_code,
            })

    if not records:
        return None, ym
    return pd.DataFrame(records), ym


# ── Kotak parser ──────────────────────────────────────────────────────────────

def _extract_ym_kotak(text: str) -> str | None:
    """'as on 31-Jan-2025' or 'as on 30-Apr-2026' → '2025-01'."""
    m = DATE_RE_KOTAK.search(text)
    if m:
        mon = MONTH_MAP.get(m.group(2).lower())
        yr  = m.group(3)
        if mon:
            return f"{yr}-{mon}"
    return None


def _extract_fund_name_kotak(header_text: str) -> str:
    """'Portfolio of Kotak XYZ Fund as on 31-Jan-2025' → 'Kotak XYZ Fund'."""
    # Strip leading "Portfolio of " and trailing " as on ..."
    text = re.sub(r"^Portfolio of\s+", "", header_text, flags=re.IGNORECASE)
    text = re.sub(r"\s+as on\s+.*$", "", text, flags=re.IGNORECASE)
    return text.strip()


def parse_kotak_file(raw: bytes, filename: str = "") -> tuple[pd.DataFrame | None, str | None]:
    """
    Parse one Kotak monthly portfolio file.

    Returns (df, ym) where df has columns:
      [isin, stock_name, pct_nav, scheme_name, _sheet]
    pct_nav is already in percentage scale (0-100) — no multiplication.
    """
    try:
        sheets = _read_all_sheets(raw)
    except Exception as e:
        log.error(f"  Cannot open workbook: {e}")
        return None, None

    records = []
    ym: str | None = None

    for sheet_code, sheet_df in sheets.items():
        if sheet_df is None or len(sheet_df) < 4:
            continue

        # Extract date + fund name from row 0, col 2
        header_cell = ""
        if sheet_df.shape[1] > 2:
            header_cell = _safe_str(sheet_df.iloc[0, 2])

        if ym is None and header_cell:
            ym = _extract_ym_kotak(header_cell)

        fund_name = _extract_fund_name_kotak(header_cell) if header_cell else ""

        # Holdings: ISIN in col 3, stock_name in col 2, pct_nav (%) in col 8
        for _, row in sheet_df.iterrows():
            if len(row) < 9:
                continue
            isin = _safe_str(row.get(3, ""))
            if not ISIN_RE.match(isin):
                continue
            try:
                pct_val = float(row[8])
            except (TypeError, ValueError, KeyError):
                continue
            # Kotak stores as percentage (e.g. 20.07); valid range 0–100
            if not (0 < pct_val <= 110):
                continue
            stock_name = _safe_str(row.get(2, ""))
            records.append({
                "isin":        isin,
                "stock_name":  stock_name,
                "pct_nav":     round(pct_val, 4),
                "scheme_name": fund_name,
                "_sheet":      sheet_code,
            })

    if not records:
        return None, ym
    return pd.DataFrame(records), ym


# ── Holdings-cache merge ──────────────────────────────────────────────────────

def _merge_df_into_cache(
    df: pd.DataFrame,
    ym: str,
    amc_label: str,
    hold_dir: Path,
    scheme_df: pd.DataFrame | None,
    amc_keyword: str,
    force: bool,
) -> bool:
    """Merge one month's data into the holdings parquet cache. Returns True if saved."""
    df["amc"]        = amc_label
    df["as_of_date"] = ym

    # Fuzzy scheme matching
    if scheme_df is not None:
        df = attach_scheme_codes(df, scheme_df, amc_keyword, amc_label)
        df["scheme_code"] = pd.to_numeric(
            df["scheme_code"], errors="coerce"
        ).astype("Int64")
    else:
        df["scheme_code"] = pd.NA

    # Ensure all COLS present
    for c in COLS:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[COLS]

    # Merge into existing parquet
    out_path = hold_dir / f"{ym}.parquet"
    if out_path.exists():
        existing = pd.read_parquet(out_path)
        if amc_label in existing["amc"].values:
            if not force:
                n_rows = (existing["amc"] == amc_label).sum()
                log.info(f"  {ym}: {amc_label} already present ({n_rows} rows) — skip (use --force)")
                return False
            else:
                existing = existing[existing["amc"] != amc_label]
                log.info(f"  {ym}: Replacing existing {amc_label} rows (--force)")
        # Align columns
        for c in COLS:
            if c not in existing.columns:
                existing[c] = pd.NA
        existing  = existing[COLS]
        combined  = pd.concat([existing, df], ignore_index=True)
    else:
        combined = df

    combined["as_of_date"] = pd.to_datetime(combined["as_of_date"], errors="coerce")
    combined.to_parquet(out_path, index=False)
    n_matched = combined["scheme_code"].notna().sum()
    log.info(f"  Saved {out_path.name}: {len(combined):,} rows total, "
             f"{n_matched:,} matched ({n_matched/len(combined)*100:.0f}%)")
    return True


# ── ABSL top-level ────────────────────────────────────────────────────────────

def ingest_absl(
    zip_path: Path,
    hold_dir: Path,
    scheme_df: pd.DataFrame | None,
    force: bool = False,
) -> None:
    """Ingest all ABSL months from Archive 2.zip."""
    hold_dir.mkdir(parents=True, exist_ok=True)
    saved = skipped = errors = 0

    with zipfile.ZipFile(zip_path) as outer:
        absl_names = sorted(
            n for n in outer.namelist()
            if n.startswith("ABSL/") and n.endswith(".zip") and "__MACOSX" not in n
        )
        log.info(f"Found {len(absl_names)} ABSL zip files in {zip_path.name}")

        for outer_name in absl_names:
            short = outer_name.split("/")[-1]
            log.info(f"\n── ABSL: {short}")

            outer_raw = outer.read(outer_name)
            try:
                inner_zf = zipfile.ZipFile(io.BytesIO(outer_raw))
            except zipfile.BadZipFile:
                log.warning(f"  Not a valid zip: {short}")
                errors += 1
                continue

            with inner_zf as inner:
                inner_files = [
                    i.filename for i in inner.infolist()
                    if i.filename.endswith((".xls", ".xlsx")) and "__MACOSX" not in i.filename
                ]
                if not inner_files:
                    log.warning(f"  No xls/xlsx inside {short}")
                    errors += 1
                    continue

                inner_name = inner_files[0]
                raw = inner.read(inner_name)

            if len(raw) < 1000:
                log.warning(f"  File too small ({len(raw)} bytes) — skip")
                errors += 1
                continue

            df, ym = parse_absl_file(raw)
            if df is None or df.empty:
                log.warning("  No holdings parsed")
                errors += 1
                continue
            if ym is None:
                log.warning("  Could not determine month — skip")
                errors += 1
                continue

            log.info(f"  Month={ym}  rows={len(df)}  schemes={df['_sheet'].nunique()}")
            ok = _merge_df_into_cache(df, ym, "Aditya_Birla", hold_dir,
                                      scheme_df, "Aditya", force)
            if ok:
                saved += 1
            else:
                skipped += 1

    log.info(f"\nABSL ingest done: {saved} saved, {skipped} skipped, {errors} errors")


# ── Kotak top-level ───────────────────────────────────────────────────────────

_KOTAK_MONTH_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"(\d{4})\.xlsx?$",
    re.IGNORECASE,
)


def _ym_from_kotak_filename(fname: str) -> str | None:
    """'ConsolidatedSEBIPortfolioApril2026.xlsx' → '2026-04'."""
    m = _KOTAK_MONTH_RE.search(fname)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower())
        yr  = m.group(2)
        if mon:
            return f"{yr}-{mon}"
    return None


def ingest_kotak(
    zip_path: Path,
    hold_dir: Path,
    scheme_df: pd.DataFrame | None,
    force: bool = False,
) -> None:
    """Ingest all Kotak months from Archive 2.zip."""
    hold_dir.mkdir(parents=True, exist_ok=True)
    saved = skipped = errors = 0

    with zipfile.ZipFile(zip_path) as outer:
        kotak_names = sorted(
            n for n in outer.namelist()
            if n.startswith("Kotak/") and (n.endswith(".xls") or n.endswith(".xlsx"))
            and "__MACOSX" not in n
        )
        log.info(f"Found {len(kotak_names)} Kotak files in {zip_path.name}")

        for zname in kotak_names:
            short = zname.split("/")[-1]
            log.info(f"\n── Kotak: {short}")

            raw = outer.read(zname)
            if len(raw) < 1000:
                log.warning(f"  Too small — skip")
                errors += 1
                continue

            df, ym = parse_kotak_file(raw, filename=short)

            # Fallback: extract YYYY-MM from filename if parser couldn't find it
            if ym is None:
                ym = _ym_from_kotak_filename(short)
                if ym:
                    log.info(f"  Date from filename: {ym}")

            if df is None or df.empty:
                log.warning("  No holdings parsed")
                errors += 1
                continue
            if ym is None:
                log.warning("  Could not determine month — skip")
                errors += 1
                continue

            log.info(f"  Month={ym}  rows={len(df)}  schemes={df['_sheet'].nunique()}")
            ok = _merge_df_into_cache(df, ym, "Kotak", hold_dir,
                                      scheme_df, "Kotak", force)
            if ok:
                saved += 1
            else:
                skipped += 1

    log.info(f"\nKotak ingest done: {saved} saved, {skipped} skipped, {errors} errors")


# ── Summary ───────────────────────────────────────────────────────────────────

def print_cache_summary(hold_dir: Path) -> None:
    cached = sorted(hold_dir.glob("*.parquet"))
    log.info(f"\n{'='*60}")
    log.info(f"Holdings cache: {len(cached)} months")
    for p in cached:
        df_c = pd.read_parquet(p)
        amcs = sorted(df_c["amc"].unique())
        log.info(f"  {p.stem}: {len(df_c):,} rows  AMCs={amcs}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Ingest ABSL and/or Kotak files from Archive 2.zip into holdings cache"
    )
    ap.add_argument("--zip",     default="./uploads/Archive 2.zip",
                    help="Path to 'Archive 2.zip'")
    ap.add_argument("--mf-data", default="./mf_data",
                    help="Directory containing holdings/ and scheme_list.parquet")
    ap.add_argument("--amc",     default="both",
                    choices=["ABSL", "Kotak", "both"],
                    help="Which AMC(s) to ingest (default: both)")
    ap.add_argument("--force",   action="store_true",
                    help="Overwrite existing rows in parquets")
    ap.add_argument("--debug",   action="store_true")
    args = ap.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    mf_data  = Path(args.mf_data)
    zip_path = Path(args.zip)
    hold_dir = mf_data / "holdings"

    if not zip_path.exists():
        log.error(f"Archive not found: {zip_path}")
        sys.exit(1)

    # Load scheme list
    scheme_path = mf_data / "scheme_list.parquet"
    scheme_df   = None
    if scheme_path.exists():
        scheme_df = pd.read_parquet(scheme_path)
        if "amc" not in scheme_df.columns and "amc_name" in scheme_df.columns:
            scheme_df = scheme_df.rename(columns={"amc_name": "amc"})
        log.info(f"Loaded scheme list: {len(scheme_df)} schemes")
    else:
        log.warning(f"scheme_list.parquet not found — scheme_codes will be NA")

    if args.amc in ("ABSL", "both"):
        ingest_absl(zip_path, hold_dir, scheme_df, force=args.force)

    if args.amc in ("Kotak", "both"):
        ingest_kotak(zip_path, hold_dir, scheme_df, force=args.force)

    print_cache_summary(hold_dir)


if __name__ == "__main__":
    main()
