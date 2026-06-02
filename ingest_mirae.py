"""
Parse Mirae Asset monthly portfolio xlsx files and merge into holdings/{YYYY-MM}.parquet
Format: col[1]=Name, col[2]=ISIN, col[3]=Industry, col[4]=Qty, col[5]=MktVal, col[6]=% NAV
pct_nav col already decimal fraction (0.1028 = 10.28% NAV)
"""
import re
import os
import pandas as pd
import openpyxl
from pathlib import Path

ISIN_RE = re.compile(r'^IN[A-Z0-9]{10}$')

RAW_DIR  = Path("/sessions/admiring-nifty-dijkstra/mnt/outputs/mf_data/holdings_raw/Mirae")
HOLD_DIR = Path("/sessions/admiring-nifty-dijkstra/mnt/outputs/mf_data/holdings")

# slug → (scheme_code, scheme_name, amc)
FUND_MAP = {
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
    # Thematic — parse if present (scheme_code can be looked up later)
    "mirae_aggressive_hybrid":   (None,   "Mirae Asset Aggressive Hybrid Fund Direct Plan Growth",    "Mirae"),
    "mirae_banking_fin":         (None,   "Mirae Asset Banking and Financial Services Fund Direct Plan Growth", "Mirae"),
    "mirae_consumer":            (None,   "Mirae Asset Great Consumer Fund Direct Plan Growth",        "Mirae"),
    "mirae_healthcare":          (None,   "Mirae Asset Healthcare Fund Direct Plan Growth",            "Mirae"),
    "mirae_infrastructure":      (None,   "Mirae Asset Infrastructure Fund Direct Plan Growth",        "Mirae"),
    "mirae_multi_asset":         (None,   "Mirae Asset Multi Asset Allocation Fund Direct Plan Growth","Mirae"),
}

DEBT_KEYWORDS = ['DEBT', 'MONEY MARKET', 'CASH', 'NET ASSETS', 'GRAND TOTAL',
                 'MUTUAL FUND', 'CERTIFICATE', 'COMMERCIAL PAPER', 'TREASURY',
                 'GOVERNMENT', 'SECURIT', 'REPO ', 'CBLO', 'TOTAL ASSETS']

def parse_mirae_xlsx(path, scheme_code, scheme_name, amc):
    """Return list of dicts with isin, stock_name, pct_nav, sector"""
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as e:
        print(f"  ERR loading {path}: {e}")
        return []

    holdings = []
    in_equity = False

    for row in rows:
        if not row or len(row) < 3:
            continue

        # Detect equity section start
        cell1 = str(row[1]).strip() if row[1] is not None else ""
        if 'EQUITY' in cell1.upper() and 'RELATED' in cell1.upper():
            in_equity = True
            continue
        if 'LISTED' in cell1.upper() and 'AWAITING' in cell1.upper():
            # "(a) Listed / awaiting listing" — still equity
            continue

        # Detect section end
        if in_equity and cell1:
            upper = cell1.upper()
            if any(kw in upper for kw in DEBT_KEYWORDS):
                in_equity = False
                continue
            if upper.startswith('(B)') or upper.startswith('(C)') or upper.startswith('(D)'):
                in_equity = False
                continue

        if not in_equity:
            continue

        # Try col[2] for ISIN
        if len(row) < 7:
            continue
        isin = str(row[2]).strip() if row[2] is not None else ""
        if not ISIN_RE.match(isin):
            continue

        try:
            pct_nav = float(row[6]) if row[6] is not None else None
        except (ValueError, TypeError):
            pct_nav = None

        if pct_nav is None or pct_nav <= 0:
            continue

        stock_name = str(row[1]).strip() if row[1] else ""
        sector = str(row[3]).strip() if row[3] else ""

        holdings.append({
            "isin": isin,
            "stock_name": stock_name,
            "pct_nav": pct_nav,
            "sector": sector,
            "scheme_code": scheme_code,
            "scheme_name": scheme_name,
            "amc": amc,
        })

    return holdings

def process_all():
    results = {}  # date_str → list of dicts

    for slug_dir in sorted(RAW_DIR.iterdir()):
        slug = slug_dir.name
        if slug not in FUND_MAP:
            continue
        scheme_code, scheme_name, amc = FUND_MAP[slug]

        for xlsx_path in sorted(slug_dir.glob("*.xlsx")):
            date_str = xlsx_path.stem  # e.g. "2025-03"
            if not re.match(r'^\d{4}-\d{2}$', date_str):
                continue

            rows = parse_mirae_xlsx(xlsx_path, scheme_code, scheme_name, amc)
            if not rows:
                print(f"  WARN: 0 rows for {slug} {date_str}")
                continue

            pct_sum = sum(r["pct_nav"] for r in rows)
            print(f"  {slug} {date_str}: {len(rows)} holdings, {pct_sum:.1%} NAV coverage")

            if date_str not in results:
                results[date_str] = []
            results[date_str].extend(rows)

    return results

def merge_into_holdings(new_data):
    """Merge new Mirae rows into existing holdings parquets, replacing old Mirae entries."""
    for date_str, rows in sorted(new_data.items()):
        parq_path = HOLD_DIR / f"{date_str}.parquet"
        new_df = pd.DataFrame(rows)
        new_df["as_of_date"] = date_str
        new_df["_sheet"] = "Mirae"
        new_df["year"] = float(date_str[:4])
        new_df["month"] = float(date_str[5:7])
        # scheme_code as Float64
        new_df["scheme_code"] = pd.array(new_df["scheme_code"], dtype="Float64")

        # Keep only pipeline columns
        keep_cols = ["isin","stock_name","pct_nav","scheme_name","_sheet","amc",
                     "scheme_code","as_of_date","year","month"]
        new_df = new_df[[c for c in keep_cols if c in new_df.columns]]

        if parq_path.exists():
            existing = pd.read_parquet(parq_path)
            # Remove old Mirae rows
            existing = existing[existing["amc"] != "Mirae"]
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df

        combined.to_parquet(parq_path, index=False)

    print(f"\nMerged Mirae data into {len(new_data)} holding parquets")

if __name__ == "__main__":
    print("Parsing Mirae xlsx files...")
    new_data = process_all()
    print(f"\nParsed {sum(len(v) for v in new_data.values())} rows across {len(new_data)} months")
    merge_into_holdings(new_data)
    print("Done.")
