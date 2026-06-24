#!/usr/bin/env python3
"""
build_fund_meta_regular.py

Builds a new fund_meta.parquet from AMFI's NAVAll.txt, filtered for:
  - Active equity, hybrid, and solution-oriented schemes
  - Regular plan, Growth option only
  - Excludes: Index funds, ETFs, FoFs, passives, IDCW/Dividend options

Also migrates proxy_code mappings from existing fund_meta.parquet where possible.

Usage:
    python3 build_fund_meta_regular.py --dry-run     # preview only, no file written
    python3 build_fund_meta_regular.py               # write mf_data/fund_meta.parquet
    python3 build_fund_meta_regular.py --output mf_data/fund_meta_regular_backup.parquet
"""
from __future__ import annotations

import argparse
import logging
import re
import urllib.request
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("alphapicker.build_meta")

HERE = Path(__file__).parent

# ---------------------------------------------------------------------------
# Target categories (AMFI sub-category strings)
# ---------------------------------------------------------------------------
EQUITY_CATS = {
    "Multi Cap Fund", "Large Cap Fund", "Large & Mid Cap Fund",
    "Mid Cap Fund", "Small Cap Fund", "Micro Cap Fund",
    "Value Fund", "Contra Fund", "Dividend Yield Fund",
    "Focused Fund", "Flexi Cap Fund", "ELSS",
    "Sectoral/ Thematic", "Thematic Fund",
}
HYBRID_CATS = {
    "Conservative Hybrid Fund", "Balanced Hybrid Fund",
    "Aggressive Hybrid Fund",
    "Dynamic Asset Allocation or Balanced Advantage",
    "Multi Asset Allocation", "Equity Savings",
}
SOLUTION_CATS = {
    "Retirement Fund", "Children's Fund",
}
ALL_CATS = EQUITY_CATS | HYBRID_CATS | SOLUTION_CATS

# ---------------------------------------------------------------------------
# Category → benchmark proxy (scheme_code of index fund used as proxy)
# ---------------------------------------------------------------------------
PROXY_DEFAULTS: dict[str, tuple[int, str]] = {
    "Large Cap Fund":                                       (118834, "Nifty 50 (category default)"),
    "Large & Mid Cap Fund":                                 (118510, "Nifty LargeMidcap 250 (category default)"),
    "Mid Cap Fund":                                         (118813, "Nifty Midcap 150 (category default)"),
    "Small Cap Fund":                                       (118525, "Nifty Smallcap 250 (category default)"),
    "Micro Cap Fund":                                       (118525, "Nifty Smallcap 250 (category default)"),
    "Multi Cap Fund":                                       (118534, "Nifty 500 (category default)"),
    "Flexi Cap Fund":                                       (118534, "Nifty 500 (category default)"),
    "Focused Fund":                                         (118834, "Nifty 50 (category default)"),
    "Value Fund":                                           (118834, "Nifty 50 (category default)"),
    "Contra Fund":                                          (118834, "Nifty 50 (category default)"),
    "Dividend Yield Fund":                                  (118834, "Nifty 50 (category default)"),
    "ELSS":                                                 (118834, "Nifty 50 (category default)"),
    "Sectoral/ Thematic":                                   (118534, "Nifty 500 (category default)"),
    "Thematic Fund":                                        (118534, "Nifty 500 (category default)"),
    "Aggressive Hybrid Fund":                               (118534, "Nifty 500 (category default)"),
    "Balanced Hybrid Fund":                                 (118534, "Nifty 500 (category default)"),
    "Conservative Hybrid Fund":                             (118534, "Nifty 500 (category default)"),
    "Dynamic Asset Allocation or Balanced Advantage":       (118534, "Nifty 500 (category default)"),
    "Multi Asset Allocation":                               (118534, "Nifty 500 (category default)"),
    "Equity Savings":                                       (118534, "Nifty 500 (category default)"),
    "Arbitrage Fund":                                       (118834, "Nifty 50 (category default)"),
    "Retirement Fund":                                      (118534, "Nifty 500 (category default)"),
    "Children's Fund":                                      (118534, "Nifty 500 (category default)"),
}

# ---------------------------------------------------------------------------
# TER estimates for regular plans (slightly higher than direct)
# ---------------------------------------------------------------------------
TER_DEFAULTS: dict[str, float] = {
    "Large Cap Fund":           0.0175,
    "Large & Mid Cap Fund":     0.0190,
    "Mid Cap Fund":             0.0200,
    "Small Cap Fund":           0.0200,
    "Micro Cap Fund":           0.0200,
    "Flexi Cap Fund":           0.0185,
    "Multi Cap Fund":           0.0185,
    "Focused Fund":             0.0190,
    "ELSS":                     0.0190,
    "Value Fund":               0.0185,
    "Contra Fund":              0.0185,
    "Dividend Yield Fund":      0.0185,
    "Sectoral/ Thematic":       0.0195,
    "Thematic Fund":            0.0195,
    "Aggressive Hybrid Fund":   0.0195,
    "Balanced Hybrid Fund":     0.0185,
    "Conservative Hybrid Fund": 0.0170,
    "Dynamic Asset Allocation or Balanced Advantage": 0.0195,
    "Multi Asset Allocation":   0.0190,
    "Equity Savings":           0.0175,
    "Arbitrage Fund":           0.0100,
    "Retirement Fund":          0.0195,
    "Children's Fund":          0.0195,
}

# ---------------------------------------------------------------------------
# AMC name normalisation
# ---------------------------------------------------------------------------
AMC_MAP = [
    ("Aditya Birla Sun Life",   "Aditya_Birla"),
    ("Axis",                    "Axis"),
    ("Bajaj Finserv",           "Bajaj"),
    ("Bandhan",                 "Bandhan"),
    ("Baroda BNP Paribas",      "Baroda_BNP"),
    ("BOI",                     "BOI"),
    ("Canara Robeco",           "Canara_Robeco"),
    ("DSP",                     "DSP"),
    ("Edelweiss",               "Edelweiss"),
    ("Franklin Templeton",      "Franklin"),
    ("Groww",                   "Groww"),
    ("HDFC",                    "HDFC"),
    ("Helios",                  "Helios"),
    ("HSBC",                    "HSBC"),
    ("ICICI Prudential",        "ICICI_Pru"),
    ("Invesco India",           "Invesco"),
    ("ITI",                     "ITI"),
    ("JM Financial",            "JM"),
    ("Kotak Mahindra",          "Kotak"),
    ("LIC",                     "LIC"),
    ("Mahindra Manulife",       "Mahindra"),
    ("Mirae Asset",             "Mirae"),
    ("Motilal Oswal",           "Motilal"),
    ("Navi",                    "Navi"),
    ("Nippon India",            "Nippon"),
    ("NJ",                      "NJ"),
    ("Old Bridge",              "Old_Bridge"),
    ("PGIM India",              "PGIM"),
    ("PPFAS",                   "PPFAS"),
    ("Quant",                   "Quant"),
    ("Samco",                   "Samco"),
    ("SBI",                     "SBI"),
    ("Shriram",                 "Shriram"),
    ("Sundaram",                "Sundaram"),
    ("Tata",                    "Tata"),
    ("Trust",                   "Trust"),
    ("Union",                   "Union"),
    ("UTI",                     "UTI"),
    ("WhiteOak Capital",        "WhiteOak"),
    ("360 ONE",                 "360_ONE"),
    ("Zerodha",                 "Zerodha"),
]


def normalize_amc(raw: str) -> str:
    r = raw.strip()
    for src, dst in AMC_MAP:
        if src.lower() in r.lower():
            return dst
    # Fallback: first two words joined by underscore
    words = r.split()
    return "_".join(words[:2]) if words else r


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------
IDCW_TOKENS = {"idcw", "dividend", "bonus", "payout", "reinvestment",
               "weekly", "monthly", "quarterly", "annual"}
EXCLUDE_TOKENS = {"etf", "index fund", "index ", " nifty ", " sensex ",
                  " bse ", " nse ", "fund of fund", " fof ", "passive"}


def is_regular_growth(name: str) -> bool:
    nl = name.lower()
    if "direct" in nl:
        return False
    if "growth" not in nl:
        return False
    if any(t in nl for t in IDCW_TOKENS):
        return False
    return True


def is_excluded(name: str, category_amfi: str) -> bool:
    nl = name.lower()
    cl = category_amfi.lower()
    if any(t in nl for t in EXCLUDE_TOKENS):
        return True
    if "index" in cl or "etf" in cl or "fund of fund" in cl or "exchange traded" in cl:
        return True
    return False


def map_category(amfi_cat: str) -> str | None:
    al = amfi_cat.lower()
    # Prefer the most specific (longest) matching category, so that e.g.
    # "Mid Cap Fund" does not match inside "Large & Mid Cap Fund". ALL_CATS is an
    # (unordered) set, so without longest-match the shorter substring could win.
    best = None
    for cat in ALL_CATS:
        if cat.lower() in al:
            if best is None or len(cat) > len(best):
                best = cat
    return best


# ---------------------------------------------------------------------------
# AMFI NAVAll parser
# ---------------------------------------------------------------------------
def fetch_navall() -> str:
    url = "https://www.amfiindia.com/spages/NAVAll.txt"
    log.info(f"Fetching {url} …")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_navall(text: str) -> pd.DataFrame:
    records = []
    current_category = None
    current_amc = None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        # Category header: "Open Ended Schemes(Equity Scheme - Large Cap Fund)"
        m = re.match(r"Open Ended Schemes\((.+?)\)", line, re.I)
        if m:
            full = m.group(1)
            current_category = full.split(" - ", 1)[1].strip() if " - " in full else full.strip()
            continue

        # Skip closed/interval ended
        if re.match(r"(Close|Interval) Ended", line, re.I):
            current_category = None
            continue

        # AMC name line (no semicolons)
        if ";" not in line:
            current_amc = line
            continue

        # Data line: code;isin_div;isin_growth;name;nav;date
        parts = line.split(";")
        if len(parts) < 5:
            continue
        try:
            scheme_code = int(parts[0].strip())
        except ValueError:
            continue

        scheme_name = parts[3].strip()
        if not scheme_name:
            continue

        records.append({
            "scheme_code":   scheme_code,
            "isin":          parts[2].strip() or parts[1].strip(),
            "scheme_name":   scheme_name,
            "amc_raw":       current_amc or "",
            "category_amfi": current_category or "",
        })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Build fund_meta.parquet for regular growth plans")
    p.add_argument("--mf-data", default=str(HERE / "mf_data"))
    p.add_argument("--output",  default=None,
                   help="Output path (default: mf_data/fund_meta.parquet)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print summary only — do not write file")
    args = p.parse_args()

    mf_data = Path(args.mf_data)
    output  = Path(args.output) if args.output else mf_data / "fund_meta.parquet"

    # Load existing fund_meta for proxy migration
    existing_proxy: dict[int, tuple[int, str]] = {}
    existing_path = mf_data / "fund_meta.parquet"
    if existing_path.exists():
        existing = pd.read_parquet(existing_path)
        existing_proxy = {
            int(row.scheme_code): (int(row.proxy_code), str(row.proxy_reason))
            for _, row in existing.iterrows()
            if pd.notna(row.get("proxy_code"))
        }
        log.info(f"Loaded existing fund_meta: {len(existing)} schemes, "
                 f"{len(existing_proxy)} with proxy codes")

    # Fetch and parse
    text = fetch_navall()
    df   = parse_navall(text)
    log.info(f"Parsed {len(df):,} total records from AMFI")

    # Filter: regular + growth
    df = df[df["scheme_name"].apply(is_regular_growth)].copy()
    log.info(f"After regular+growth filter: {len(df):,}")

    # Map category
    df["category"] = df["category_amfi"].apply(map_category)
    df = df[df["category"].notna()].copy()
    log.info(f"After category filter:       {len(df):,}")

    # Exclude index/ETF/FoF
    df = df[~df.apply(
        lambda r: is_excluded(r["scheme_name"], r["category_amfi"]), axis=1
    )].copy()
    log.info(f"After exclusion filter:      {len(df):,}")

    # Normalise AMC
    df["amc"] = df["amc_raw"].apply(normalize_amc)

    # Assign proxy codes (migrate where possible, else category default)
    def assign_proxy(row) -> tuple[int, str]:
        sc = int(row["scheme_code"])
        if sc in existing_proxy:
            return existing_proxy[sc]
        cat = row["category"]
        return PROXY_DEFAULTS.get(cat, (118534, "Nifty 500 (category default)"))

    proxies = df.apply(assign_proxy, axis=1)
    df["proxy_code"]   = [x[0] for x in proxies]
    df["proxy_reason"] = [x[1] for x in proxies]

    # TER estimates
    df["ter_est"] = df["category"].map(TER_DEFAULTS).fillna(0.0190)

    # Final columns and dedup
    out = df[[
        "scheme_code", "scheme_name", "amc", "isin",
        "category_amfi", "category", "ter_est", "proxy_code", "proxy_reason",
    ]].drop_duplicates("scheme_code").reset_index(drop=True)

    # ── Summary ──────────────────────────────────────────────────────────────
    log.info(f"\n{'='*60}")
    log.info(f"  Final universe: {len(out)} regular growth schemes")
    log.info(f"\n  By category:")
    for cat, cnt in out["category"].value_counts().items():
        log.info(f"    {cat:<45} {cnt:>4}")
    log.info(f"\n  By AMC (top 15):")
    for amc, cnt in out["amc"].value_counts().head(15).items():
        log.info(f"    {amc:<30} {cnt:>4}")

    existing_codes = set(existing_proxy.keys())
    migrated = out[out["scheme_code"].isin(existing_codes)]
    new      = out[~out["scheme_code"].isin(existing_codes)]
    log.info(f"\n  Proxy codes migrated from existing fund_meta: {len(migrated)}")
    log.info(f"  New schemes (category-default proxy):         {len(new)}")
    log.info(f"{'='*60}")

    if args.dry_run:
        log.info("  Dry run — file NOT written.")
        return

    # Back up existing file
    if existing_path.exists() and not args.output:
        backup = mf_data / "fund_meta_direct_backup.parquet"
        import shutil
        shutil.copy(existing_path, backup)
        log.info(f"  Backed up existing fund_meta → {backup}")

    out.to_parquet(output)
    log.info(f"  ✓ Written {len(out)} schemes to {output}")
    log.info("\nNext steps:")
    log.info("  1. Review the output above")
    log.info("  2. Run:  python3 update_nav_monthly.py  (fetches NAV for all new scheme codes)")
    log.info("  3. Run:  python3 run_monthly_update.py  (full rebuild)")
    log.info("  4. Run:  python3 generate_api_json.py && git push  (deploy)")


if __name__ == "__main__":
    main()
