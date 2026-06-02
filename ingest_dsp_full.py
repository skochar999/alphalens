#!/usr/bin/env python3
"""
ingest_dsp_full.py
==================
Download all 40 monthly portfolio ZIP files from dspim.com,
extract ISIN-level equity holdings, and merge into the holdings parquets.

ZIP URLs sourced from:
https://www.advisorkhoj.com/form-download-centre/Mutual/DSP-Mutual-Fund/Monthly-Portfolio-Disclosures

Format (per xlsx sheet):
  Row 0: Fund name (col 1)
  Row 1: "Portfolio as on {date}" (col 1)
  Row 3: Header → Sr.No. | Name of Instrument | ISIN | ... | % to Net Assets | ...
  Row 7+: Holdings (% to Net Assets in col 6, decimal form: 0.091 = 9.1%)

pct_nav stored as PERCENTAGE (0-100) to match rest of holdings parquets.
"""

import io, re, zipfile, sys
import requests
import pandas as pd
from pathlib import Path
from dateutil.parser import parse as dateparse

DATA_DIR = Path(__file__).parent / "mf_data" / "holdings"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── 40 monthly portfolio ZIP URLs ──────────────────────────────────────────
DSP_ZIP_URLS = [
    ("2023-01", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/aa9194f382-1720430579/monthend-portfolio-january-31-2023.zip"),
    ("2023-02", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/608000d99a-1720430579/monthend-portfolio-february-28-2023.zip"),
    ("2023-03", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/8dd2624390-1720430579/monthend-portfolio-march-31-2023.zip"),
    ("2023-04", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/b90c060d08-1720430579/monthend-portfolio-april-30-2023.zip"),
    ("2023-05", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/cd8dd02901-1720430575/dsp-monthend-portfolio-as-on-may-2023.zip"),
    ("2023-06", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/85975154b0-1720430579/monthend-portfolio-june-30-2023.zip"),
    ("2023-07", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/da3464b42a-1720430579/monthend-portfolio-july-31st-2023.zip"),
    ("2023-08", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/06b9bd536e-1720430576/month-end-portfolio-august-2023.zip"),
    ("2023-09", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/ef8b385bd2-1720430580/monthend-portfolio-september-2023.zip"),
    ("2023-10", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/c4ca90394b-1720430580/monthend-portfolio-october-2023.zip"),
    ("2023-11", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/1ee0bbaa37-1720430579/monthend-portfolio-november-2023.zip"),
    ("2023-12", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/6755b783ec-1720430579/monthend-portfolio-december-31-2023.zip"),
    ("2024-01", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/6fd07c04dc-1720430579/monthend-portfolio-january-2024.zip"),
    ("2024-02", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/2ebdbd8d27-1720430579/monthend-portfolio-february-2024.zip"),
    ("2024-03", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/1bfaab3f7d-1720430580/monthend-portfolio-march-31-2024.zip"),
    ("2024-04", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/376234d7a6-1720430579/monthend-portfolio-april-2024.zip"),
    ("2024-05", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/4a937682c8-1717857857/monthend-portfolio-may-2024.zip"),
    ("2024-06", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/901b3e3f5a-1720531013/monthend-portfolio-june-2024.zip"),
    ("2024-07", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/1352cabccd-1723212435/monthend-portfolio-july-2024.zip"),
    ("2024-08", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/0bb3a4390d-1725947858/monthend-portfolio-august-2024.zip"),
    ("2024-09", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/d22c22489a-1728496273/monthend-portfolio-september-30-2024.zip"),
    ("2024-10", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/fb740025a4-1731080131/monthend-portfolio-october-31-2024.zip"),
    ("2024-11", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/9ae79acb70-1733739994/monthend-portfolio-november-30-2024.zip"),
    ("2024-12", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/b43e0a72c2-1736446623/monthend-portfolio-december-31-2024.zip"),
    ("2025-01", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/758c5da9c1-1739170253/monthend-portfolio-january-31-2025.zip"),
    ("2025-02", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/f715dc48e9-1741597802/monthend-portfolio-february-28-2025.zip"),
    ("2025-03", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/339326760c-1744135647/monthend-portfolio-march-31-2025.zip"),
    ("2025-04", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/d68a67cdea-1746886991/monthend-portfolio-april-30-2025.zip"),
    ("2025-05", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/79859b96a0-1749486449/monthend-portfolio-may-2025.zip"),
    ("2025-06", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/8f0e90fd0c-1752166199/monthend-portfolio-june-2025.zip"),
    ("2025-07", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/b68e3ec871-1754747782/monthend-portfolio-july-2025.zip"),
    ("2025-08", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/6eda8470d5-1757771557/monthend-portfolio-august-2025.zip"),
    ("2025-09", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/754f55d76e-1760032442/monthend-portfolio-september-30-2025.zip"),
    ("2025-10", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/d155b953f0-1762611469/monthend-portfolio-october-2025.zip"),
    ("2025-11", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/0e5f7b1d70-1765381448/monthend-portfolio-november-2025.zip"),
    ("2025-12", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/b3e426eed3-1767976447/monthend-portfolio-december-31-2025.zip"),
    ("2026-01", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/fd9bf9ce01-1770662998/monthend-portfolio-january-31-2026.zip"),
    ("2026-02", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/1c747fb85f-1773156944/monthend-portfolio-february-28-2026.zip"),
    ("2026-03", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/b1bcdfd489-1775749401/monthend-portfolio-31march2026.zip"),
    ("2026-04", "https://www.dspim.com/media/pages/mandatory-disclosures/portfolio-disclosures/d80216af21-1778404078/monthend-portfolios_30-april-2026.zip"),
]

# ── Sheet name → (AMFI scheme_code, canonical scheme_name) ─────────────────
# Only active equity funds. ETF, debt, FoF, hybrid sheets are skipped.
SHEET_MAP = {
    "TOP100":        (119250, "DSP Top 100 Equity Fund"),          # Large Cap
    "EQUITYOPPOR":   (119218, "DSP Equity Opportunities Fund"),    # Large & Mid Cap
    "MIDCAP":        (119071, "DSP Midcap Fund"),
    "SMALLCAP":      (119212, "DSP Small Cap Fund"),
    "Flexi Cap":     (119076, "DSP Flexi Cap Fund"),
    "VALUE":         (148595, "DSP Value Fund"),
    "Multicap Fund": (152310, "DSP Multicap Fund"),
    "FOCUS":         (119096, "DSP Focused Fund"),
    "TAX":           (119242, "DSP ELSS Tax Saver Fund"),
}

# All scheme codes we manage (for removing stale rows before reinserting)
MANAGED_CODES = {code for code, _ in SHEET_MAP.values()}


def parse_sheet(xl: pd.ExcelFile, sheet: str) -> pd.DataFrame | None:
    """
    Parse one equity fund sheet. Returns DataFrame with columns:
      isin, stock_name, pct_nav (as percentage 0-100)
    or None if sheet is empty / parse fails.
    """
    try:
        df = xl.parse(sheet, header=None)
    except Exception as e:
        print(f"    [WARN] could not parse sheet '{sheet}': {e}")
        return None

    if df.shape[0] < 8:
        return None

    # Header is row 3 → find data rows: rows with a numeric Sr.No. in col 0
    rows = []
    for _, row in df.iloc[7:].iterrows():
        isin = str(row.iloc[2]).strip() if pd.notna(row.iloc[2]) else ""
        name = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
        pct_raw = row.iloc[6]

        # Stop at MONEY MARKET, GRAND TOTAL, or similar section markers
        if any(x in name.upper() for x in ("MONEY MARKET", "GRAND TOTAL",
                                             "TREPS", "REVERSE REPO",
                                             "CASH & CASH", "NET RECEIVABLE")):
            break
        # Only keep rows with a valid 12-char ISIN starting with IN
        if not re.match(r'^IN[A-Z0-9]{10}$', isin):
            continue
        # pct_raw must be numeric
        try:
            pct = float(pct_raw)
        except (TypeError, ValueError):
            continue
        if pct <= 0 or pct > 1.05:   # sanity: decimal form 0-1 (allow slight overrun)
            continue
        rows.append({"isin": isin, "stock_name": name, "pct_nav": round(pct * 100, 4)})

    if not rows:
        return None
    return pd.DataFrame(rows)


def parse_date_from_sheet(xl: pd.ExcelFile, sheet: str) -> pd.Timestamp | None:
    """Extract portfolio date from row 1: 'Portfolio as on January 31, 2023'"""
    try:
        df = xl.parse(sheet, header=None)
        cell = str(df.iloc[1, 1])
        # Extract date portion after "as on"
        m = re.search(r'as on\s+(.+)', cell, re.IGNORECASE)
        if m:
            return pd.Timestamp(dateparse(m.group(1), dayfirst=False))
    except Exception:
        pass
    return None


def get_equity_xlsx_from_zip(zip_bytes: bytes) -> bytes | None:
    """Return bytes of the equity FOF xlsx file from a ZIP."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        # Prefer "Equity FOF" file; fall back to first xlsx
        equity_files = [n for n in names
                        if n.endswith('.xlsx') and 'ISIN DEBT' not in n.upper()
                        and ('EQUITY' in n.upper() or 'FOF' in n.upper())]
        if not equity_files:
            equity_files = [n for n in names if n.endswith('.xlsx')]
        if not equity_files:
            return None
        return zf.read(equity_files[0])


def ingest_month(ym: str, url: str, dry_run: bool = False) -> dict:
    """Download ZIP, parse equity sheets, update holdings parquet."""
    parquet_path = DATA_DIR / f"{ym}.parquet"

    # ── Download ──────────────────────────────────────────────────────────
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        return {"ym": ym, "status": f"download_error: {e}", "rows": 0}

    # ── Extract equity xlsx ───────────────────────────────────────────────
    xlsx_bytes = get_equity_xlsx_from_zip(resp.content)
    if xlsx_bytes is None:
        return {"ym": ym, "status": "no_equity_xlsx", "rows": 0}

    xl = pd.ExcelFile(io.BytesIO(xlsx_bytes))
    available_sheets = set(xl.sheet_names)

    # ── Parse each equity fund sheet ─────────────────────────────────────
    all_rows = []
    as_of_date = None

    for sheet, (code, canonical_name) in SHEET_MAP.items():
        if sheet not in available_sheets:
            continue  # fund didn't exist yet in this month

        holdings = parse_sheet(xl, sheet)
        if holdings is None or holdings.empty:
            continue

        if as_of_date is None:
            as_of_date = parse_date_from_sheet(xl, sheet)

        holdings["scheme_name"]  = canonical_name
        holdings["scheme_code"]  = float(code)
        holdings["amc"]          = "DSP"
        holdings["_sheet"]       = sheet
        all_rows.append(holdings)

    if not all_rows:
        return {"ym": ym, "status": "no_data", "rows": 0}

    new_df = pd.concat(all_rows, ignore_index=True)

    # ── Set date columns ──────────────────────────────────────────────────
    if as_of_date is None:
        y, m = int(ym[:4]), int(ym[5:])
        as_of_date = pd.Timestamp(y, m, 1) + pd.offsets.MonthEnd(0)

    new_df["as_of_date"] = pd.to_datetime(as_of_date)
    new_df["year"]       = as_of_date.year
    new_df["month"]      = as_of_date.month

    if dry_run:
        return {"ym": ym, "status": "ok(dry)", "rows": len(new_df),
                "date": str(as_of_date.date())}

    # ── Merge into existing parquet ───────────────────────────────────────
    if parquet_path.exists():
        existing = pd.read_parquet(parquet_path)
        # Remove stale DSP equity rows we are replacing
        keep = ~(
            (existing["amc"] == "DSP") &
            (existing["scheme_code"].isin(MANAGED_CODES))
        )
        base = existing[keep].copy()
        # Ensure as_of_date is datetime in base
        if "as_of_date" in base.columns:
            base["as_of_date"] = pd.to_datetime(base["as_of_date"])
    else:
        base = pd.DataFrame(columns=new_df.columns)

    merged = pd.concat([base, new_df], ignore_index=True)
    merged["scheme_code"] = pd.array(
        merged["scheme_code"].tolist(), dtype="Float64"
    )
    merged["as_of_date"] = pd.to_datetime(merged["as_of_date"])
    merged.to_parquet(parquet_path, index=False)

    return {"ym": ym, "status": "ok", "rows": len(new_df),
            "date": str(as_of_date.date())}


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Parse but don't save parquets")
    p.add_argument("--month", help="Only process this month, e.g. 2024-03")
    args = p.parse_args()

    targets = [(ym, url) for ym, url in DSP_ZIP_URLS
               if args.month is None or ym == args.month]

    print(f"Processing {len(targets)} months  (dry_run={args.dry_run})")
    total_rows = 0

    for ym, url in targets:
        result = ingest_month(ym, url, dry_run=args.dry_run)
        status = result["status"]
        rows   = result.get("rows", 0)
        date   = result.get("date", "")
        total_rows += rows
        icon = "✓" if status.startswith("ok") else "✗"
        print(f"  {icon} {ym}  {rows:4d} rows  ({date})  [{status}]")
        sys.stdout.flush()

    print(f"\nTotal rows written: {total_rows}")


if __name__ == "__main__":
    main()
