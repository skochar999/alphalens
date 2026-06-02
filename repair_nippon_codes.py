#!/usr/bin/env python3
"""
repair_nippon_codes.py
======================
The old ingest_nippon.py fuzzy-matched scheme names → AMFI codes incorrectly,
assigning the same scheme_code to dozens of unrelated Nippon fund names.

This script rebuilds correct scheme_code assignments for Nippon holdings rows
by keyword-matching on the stored scheme_name, then saves updated parquets.

Funds not matching any equity key → scheme_code set to NaN (excluded downstream).
"""
import glob, re
import pandas as pd
from pathlib import Path

DATA_DIR = Path(__file__).parent / "mf_data" / "holdings"

# ── Correct AMFI scheme codes for Nippon equity funds ───────────────────────
# Each entry: (regex_pattern, scheme_code, canonical_name)
# Patterns are matched against UPPER-CASED scheme_name.
# More specific patterns must come first.
NIPPON_MAP = [
    # Thematic / specialty (important to match before generic "LARGE CAP" etc.)
    (r"PHARMA",                   None,    "Nippon India Pharma Fund"),
    (r"BANKING.*FINANCIAL|BANK.*FIN SERV|BANKING & FINANCIAL",
                                  None,    "Nippon India Banking & Fin Services"),
    (r"CONSUMPTION",              None,    "Nippon India Consumption Fund"),
    (r"HEALTHCARE",               None,    "Nippon India Healthcare Fund"),
    (r"TECHNOLOGY|TECH OPP",      None,    "Nippon India Technology Fund"),
    (r"POWER.*INFRA|INFRA.*POWER", None,   "Nippon India Power & Infra Fund"),
    (r"TAIWAN",               149329,      "Nippon India Taiwan Equity Fund"),
    (r"JAPAN",                    None,    "Nippon India Japan Equity Fund"),
    (r"MNC",                      None,    "Nippon India MNC Fund"),

    # Index / passive
    (r"NIFTY SMALLCAP 250",   148519,      "Nippon India Nifty Smallcap 250 Index Fund"),
    (r"NIFTY 50 VALUE 20|NIFTY50 VALUE 20",
                              148721,      "Nippon India Nifty 50 Value 20 Index Fund"),
    (r"NIFTY MIDCAP 150",     148726,      "Nippon India Nifty Midcap 150 Index Fund"),
    (r"NIFTY MIDCAP 50",          None,    "Nippon India Nifty Midcap 50 ETF"),
    # Any remaining ETFs / index funds → not active equity
    (r"ETF|INDEX FUND|BEES|JUNIOR BEES|CPSE|SENSEX|NIFTY (50|100|NEXT|SDL|AAA|ALPHA|AUTO|IT|REALT|G-SEC|INFRA|PSU|DIVID|MANUF|SILVER|BANK|PHARMA ETF)",
                                  None,    "Nippon India ETF/Index"),

    # Active equity — order matters: more specific / distinct names first
    (r"VISION",               118678,      "Nippon India Vision Large & Mid Cap Fund"),
    (r"LARGE CAP",            118632,      "Nippon India Large Cap Fund"),
    (r"MULTI.*CAP|MULTICAP",  118650,      "Nippon India Multi Cap Fund"),
    # Mid-cap: "GROWTH FUND" / "GROWTH MID CAP" — be specific to avoid matching "GROWTH OPTION"
    (r"GROWTH (FUND|MID CAP)|GROWTH MID|MID CAP FUND",
                              118668,      "Nippon India Growth Mid Cap Fund"),
    (r"FOCUSED (FUND|EQUITY)|FOCUSED EQUITY",
                              118692,      "Nippon India Focused Fund"),
    (r"BALANCED ADVANTAGE",   118736,      "Nippon India Balanced Advantage Fund"),
    (r"SMALL CAP",            118778,      "Nippon India Small Cap Fund"),
    # Value fund: careful to exclude "NIFTY 50 VALUE 20" (already matched above)
    (r"VALUE FUND",           118784,      "Nippon India Value Fund"),
    (r"ELSS|TAX SAVER",       118803,      "Nippon India ELSS Tax Saver Fund"),
    (r"EQUITY SAVINGS",       134594,      "Nippon India Equity Savings Fund"),
    # Passive FlexiCap FOF must be excluded first (it's a FoF, not the active fund)
    (r"PASSIVE FLEXI|FLEXICAP.*FOF|FLEXI.*FOF",
                                  None,    "Nippon India Passive Flexicap FoF"),
    (r"FLEXI.?CAP",           149094,      "Nippon India Flexi Cap Fund"),

    # Debt / hybrid / money market → exclude from equity analysis
    (r"LIQUID|OVERNIGHT|ULTRA SHORT|SHORT DURATION|SHORT TERM|"
     r"LOW DURATION|MEDIUM|DYNAMIC BOND|CORPORATE BOND|INCOME FUND|"
     r"GILT|MONEY MARKET|HYBRID BOND|CONSERVATIVE HYBRID|FLOATER|"
     r"CREDIT RISK|STRATEGIC DEBT|ARBITRAGE|GOLD|SILVER|CPSE BOND|"
     r"FIXED (HORIZON|MATURITY)|FMP|INTERVAL|QUARTERLY INTERVAL|"
     r"ANNUAL INTERVAL|CAPITAL PROTECT|NIVESH LAKSHYA|BANKING.*PSU DEBT|"
     r"PSU BANK|BANKING.*PSU|AGGRESSIVE HYBRID|EQUITY HYBRID|"
     r"ASSET ALLOC|MULTI.?ASSET|MULTI - ASSET|PASSIVE FLEXI",
                                  None,    "Nippon non-equity"),
    # Anything else → NaN (safe default)
]

# Compile patterns once
COMPILED = [(re.compile(pat), code, name) for (pat, code, name) in NIPPON_MAP]


def resolve_code(scheme_name: str) -> int | None:
    """Return correct AMFI scheme_code, or None if non-equity / unknown.

    We match only against the fund's SHORT NAME (text before the first '(')
    to avoid false positives from description text like
    "...investing in Large Cap stocks" appearing in non-large-cap fund names.
    """
    full = str(scheme_name)
    # Strip parenthetical description – everything from '(' onward
    short = full.split("(")[0].strip()
    upper = short.upper()
    for regex, code, _ in COMPILED:
        if regex.search(upper):
            return code
    return None   # unknown → treat as NaN


def repair_parquet(path: Path, dry_run: bool = False) -> dict:
    df = pd.read_parquet(path)
    mask = df["amc"] == "Nippon"
    if not mask.any():
        return {"path": str(path), "nippon_rows": 0, "changed": 0}

    nip = df[mask].copy()
    new_codes_raw = nip["scheme_name"].apply(resolve_code)
    new_codes = pd.array(new_codes_raw.tolist(), dtype="Float64")

    # Count rows where old != new (use -1 sentinel to handle NA comparisons)
    old_vals = nip["scheme_code"].fillna(-1).astype("float64")
    new_vals = pd.Series(new_codes, index=nip.index).fillna(-1).astype("float64")
    changed = int((old_vals != new_vals).sum())

    if not dry_run:
        df.loc[mask, "scheme_code"] = new_codes
        df.to_parquet(path, index=False)

    # Summary of code→name assignments (first parquet only, for dry-run display)
    summary = {}
    nip2 = nip.copy()
    nip2["new_code"] = pd.array(new_codes_raw.tolist(), dtype="Float64")
    for code, grp in nip2.dropna(subset=["new_code"]).groupby("new_code"):
        names = grp["scheme_name"].unique()
        summary[code] = list(names)

    return {
        "path": path.name,
        "nippon_rows": int(mask.sum()),
        "changed": int(changed),
        "assignments": {str(c): v for c, v in summary.items()},
    }


def main():
    import argparse, json
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--data-dir", default=str(DATA_DIR))
    args = p.parse_args()

    files = sorted(Path(args.data_dir).glob("*.parquet"))
    print(f"Repairing {len(files)} parquets  (dry_run={args.dry_run})")

    total_changed = 0
    for f in files:
        res = repair_parquet(f, dry_run=args.dry_run)
        if res["nippon_rows"]:
            print(f"  {res['path']}  nippon={res['nippon_rows']}  changed={res['changed']}")
            if args.dry_run and res["assignments"]:
                for code, names in sorted(res["assignments"].items()):
                    unique = list(dict.fromkeys(n[:60] for n in names))[:3]
                    print(f"    → {code}: {unique}")
        total_changed += res.get("changed", 0)

    print(f"\nTotal rows changed: {total_changed}")
    print("Done." if not args.dry_run else "Dry-run complete — rerun without --dry-run to apply.")


if __name__ == "__main__":
    main()
