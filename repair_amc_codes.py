#!/usr/bin/env python3
"""
repair_amc_codes.py
===================
Fix corrupted scheme_code assignments for ABSL, Kotak, ICICI, and Franklin
holdings data. The old ingest scripts used fuzzy name matching which incorrectly
assigned the same scheme_code to many different fund names.

Strategy: match each fund's SHORT NAME (before first '(') against a hand-verified
keyword map, using the correct AMFI direct-plan-growth scheme codes.

Usage:
    python repair_amc_codes.py --dry-run   # preview changes
    python repair_amc_codes.py             # apply to all parquets
"""
from __future__ import annotations
import glob, re
import pandas as pd
from pathlib import Path

DATA_DIR = Path(__file__).parent / "mf_data" / "holdings"

# ── Per-AMC keyword maps ────────────────────────────────────────────────────
# Each entry: (regex_pattern, amfi_scheme_code_or_None, canonical_name)
# None = non-equity or excluded; code = direct plan growth AMFI code.
# Patterns matched against UPPER short-name (before first '(').
# Order matters — more specific / longer patterns first.

ABSL_MAP = [
    # Exclusions first (debt, ETFs, specialty non-equity)
    (r"BANKING.*PSU|PSU.*BANK|ARBITRAGE|SAVINGS FUND.*ARBITRAGE|INCOME PLUS ARBITRAGE",
                None,  "ABSL non-equity"),
    (r"NIFTY|INDEX FUND|ETF|SENSEX|BSE ",
                None,  "ABSL passive/index"),
    (r"DEBT|BOND|LIQUID|OVERNIGHT|ULTRA SHORT|SHORT TERM|SHORT DURATION|"
     r"LOW DURATION|MEDIUM DURATION|DYNAMIC BOND|GILT|MONEY MARKET|"
     r"FLOATING RATE|CREDIT RISK|STRATEGIC|FIXED TERM|INTERVAL|"
     r"RETIREMENT|CAPITAL PROTECT|SAVINGS MANAGER|VISION",
                None,  "ABSL debt/other"),
    (r"TRANSPORTATION|LOGISTICS|BUSINESS CYCLE|INFRASTRUCTURE|SPECIAL OPP|"
     r"MANUFACTURING|CONGLOMERATE|QUANT|DIVIDEND|PHARMA|HEALTHCARE|"
     r"DIGITAL INDIA|GLOBAL EMERG|INTERNATIONAL|COMMODITY|ESG|"
     r"BAL BHAVISHYA|PSU EQUITY|PURE VALUE|MULTI ASSET|ASSET ALLOC",
                None,  "ABSL thematic/other"),
    # Equity funds with verified AMFI codes
    (r"LARGE & MID CAP|LARGE AND MID CAP",
             119436,  "Aditya Birla Sun Life Large & Mid Cap Fund"),
    (r"LARGE CAP",
             119528,  "Aditya Birla Sun Life Large Cap Fund"),
    (r"MULTI.?CAP|MULTI-CAP",
             148921,  "Aditya Birla Sun Life Multi-Cap Fund"),
    (r"MIDCAP|MID CAP",
             119620,  "Aditya Birla Sun Life Midcap Fund"),
    (r"SMALL CAP",
             119556,  "Aditya Birla Sun Life Small Cap Fund"),
    (r"FLEXI.?CAP",
             120564,  "Aditya Birla Sun Life Flexi Cap Fund"),
    (r"FOCUSED",
             119564,  "Aditya Birla Sun Life Focused Fund"),
    (r"VALUE FUND|VALUE$",
             119659,  "Aditya Birla Sun Life Value Fund"),
    (r"ELSS|TAX SAVER",
             119544,  "Aditya Birla Sun Life ELSS Tax Saver Fund"),
    (r"EQUITY SAVINGS",
             132995,  "Aditya Birla Sun Life Equity Savings Fund"),
    (r"EQUITY HYBRID|EQUITY '95|EQUITY.95",
             120517,  "Aditya Birla Sun Life Equity Hybrid '95 Fund"),
    (r"BALANCED ADVANTAGE",
             131670,  "Aditya Birla Sun Life Balanced Advantage Fund"),
]

KOTAK_MAP = [
    # Exclusions
    (r"ETF|INDEX FUND|SENSEX|NIFTY|BSE |SILVER|GOLD|OVERNIGHT|LIQUID|"
     r"MONEY MARKET|BOND|DEBT|GILT|SHORT TERM|DURATION|CREDIT RISK|"
     r"DYNAMIC ASSET|HYBRID DEBT|FLOATER|INTERVAL",
                None,  "Kotak passive/debt"),
    (r"TECHNOLOGY|HEALTHCARE|INFRA|MANUFACTURE|SERVICES|GLOBAL EMERG|"
     r"INTERNATIONAL|MNC|BANKING.*FIN|QUANT|MOMENTUM|RETIREMENT|"
     r"PIONEER|INNOVATION|INDIA ESG|PASSIVE.*FOF|ARBITRAGE",
                None,  "Kotak thematic/other"),
    # Equity funds
    (r"LARGE & MIDCAP|LARGE AND MIDCAP",
             120158,  "Kotak Large & Midcap Fund"),
    (r"LARGE CAP",
             120152,  "Kotak Large Cap Fund"),
    (r"MIDCAP|MID CAP",
             119775,  "Kotak Midcap Fund"),
    (r"SMALL CAP",
             120164,  "Kotak Small Cap Fund"),
    (r"FLEXI.?CAP|FLEXICAP",
             120166,  "Kotak Flexicap Fund"),
    (r"MULTICAP|MULTI.?CAP",
             149185,  "Kotak Multicap Fund"),
    (r"CONTRA",
             119769,  "Kotak Contra Fund"),
    (r"FOCUSED",
             147473,  "Kotak Focused Fund"),
    (r"ELSS|TAX SAVER",
             119773,  "Kotak ELSS Tax Saver Fund"),
    (r"EQUITY SAVINGS",
             131373,  "Kotak Equity Savings Fund"),
    (r"BALANCED ADVANTAGE",
             144335,  "Kotak Balanced Advantage Fund"),
]

ICICI_MAP = [
    # Exclusions
    (r"ETF|INDEX FUND|NIFTY|SENSEX|BSE |OVERNIGHT|LIQUID|"
     r"MONEY MARKET|BOND|DEBT|GILT|SHORT TERM|DURATION|CREDIT RISK|"
     r"FLOATING|INTERVAL|SAVINGS FUND.*DEBT",
                None,  "ICICI passive/debt"),
    (r"TECHNOLOGY|HEALTHCARE|INFRA|MANUFACTURE|BANKING.*FINANCIAL|"
     r"COMMODITIES|GLOBAL|INTERNATIONAL|PHARMA|DIVIDEND|PSU|"
     r"QUANT|MOMENTUM|RETIREMENT|BHARAT|MULTI ASSET|"
     r"ASSET ALLOC|INDO ASIA|DISCOVERY|BUSINESS CYCLE",
                None,  "ICICI thematic/other"),
    (r"US BLUECHIP",
             120186,  "ICICI Prudential US Bluechip Equity Fund"),
    # Equity funds
    (r"LARGE & MID CAP|LARGE AND MID CAP",
             120596,  "ICICI Prudential Large & Mid Cap Fund"),
    (r"LARGE CAP|BLUECHIP FUND",
             120586,  "ICICI Prudential Large Cap Fund"),
    (r"MIDCAP|MID CAP",
             120381,  "ICICI Prudential MidCap Fund"),
    (r"SMALLCAP|SMALL CAP",
             120591,  "ICICI Prudential Smallcap Fund"),
    (r"FLEXI.?CAP|FLEXICAP",
             148990,  "ICICI Prudential Flexicap Fund"),
    (r"MULTICAP|MULTI.?CAP",
             120599,  "ICICI Prudential Multicap Fund"),
    (r"VALUE FUND|VALUE DISCOVERY|VALUE$",
             120323,  "ICICI Prudential Value Fund"),
    (r"FOCUSED",
             120722,  "ICICI Prudential Focused Equity Fund"),
    (r"ELSS|TAX SAVER",
             120592,  "ICICI Prudential ELSS Tax Saver Fund"),
    (r"EQUITY SAVINGS",
             None,    "ICICI Prudential Equity Savings Fund"),
    (r"BALANCED ADVANTAGE",
             120377,  "ICICI Prudential Balanced Advantage Fund"),
    (r"EQUITY HYBRID|EQUITY.*HYBRID",
             None,    "ICICI Prudential Equity & Debt Fund"),
]

DSP_MAP = [
    # Non-equity exclusions
    (r"INDEX FUND|NIFTY|SENSEX|ETF|BOND|DEBT|LIQUID|OVERNIGHT|MONEY MARKET|"
     r"DURATION|CREDIT|FLOATING|BANKING.*PSU|GILT|SAVINGS.*REGULAR|REGULAR SAVINGS|"
     r"GLOBAL|INTERNATIONAL|COMMODITY|ARBITRAGE|NATURAL RESOURCES|WORLD",
                None,  "DSP non-equity"),
    # Equity funds — using correct AMFI Direct-Growth codes
    (r"LARGE & MID CAP|LARGE AND MID CAP",
             119218,  "DSP Large & Mid Cap Fund"),
    (r"LARGE CAP",
             119250,  "DSP Large Cap Fund"),
    (r"MID CAP|MIDCAP",
             119071,  "DSP Mid Cap Fund"),
    (r"SMALL CAP",
             119212,  "DSP Small Cap Fund"),
    (r"FLEXI.?CAP|FLEXICAP|OPPORTUNITIES",
             119076,  "DSP Flexi Cap Fund"),
    (r"MULTI.?CAP|MULTICAP",
             152310,  "DSP Multicap Fund"),
    (r"FOCUSED",
             119096,  "DSP Focused Fund"),
    (r"VALUE",
             148595,  "DSP Value Fund"),
    (r"ELSS|TAX SAVER",
             119242,  "DSP ELSS Tax Saver Fund"),
    (r"EQUITY SAVINGS",
             136567,  "DSP Equity Savings Fund"),
    (r"EQUITY.*BALANCED|BALANCED.*EQUITY",
             None,    "DSP equity balanced"),
]

AXIS_MAP = [
    # Non-equity exclusions
    (r"NIFTY|INDEX FUND|ETF|SENSEX|BSE |BOND|DEBT|LIQUID|OVERNIGHT|MONEY MARKET|"
     r"DURATION|CREDIT|FLOATING|GILT|ARBITRAGE|SILVER|GOLD|COMMODITY",
                None,  "Axis non-equity"),
    (r"SERVICES|INFRASTRUCTURE|HEALTHCARE|TECHNOLOGY|BANKING|CONSUMPTION|"
     r"SPECIAL SITUATIONS|GLOBAL|INTERNATIONAL|QUANT|RETIREMENT|BUSINESS CYCLE|"
     r"INNOVATION|MOMENTUM|PASSIVE",
                None,  "Axis thematic/other"),
    # Equity funds
    (r"LARGE & MID CAP|LARGE AND MID CAP",
             145110,  "Axis Large & Mid Cap Fund"),
    (r"LARGE CAP",
             120465,  "Axis Large Cap Fund"),
    (r"MID CAP|MIDCAP",
             120505,  "Axis Midcap Fund"),
    (r"SMALL CAP",
             125354,  "Axis Small Cap Fund"),
    (r"FLEXI.?CAP|FLEXICAP",
             141925,  "Axis Flexi Cap Fund"),
    (r"MULTI.?CAP|MULTICAP",
             149383,  "Axis Multicap Fund"),
    (r"FOCUSED",
             120468,  "Axis Focused Fund"),
    (r"VALUE",
             149166,  "Axis Value Fund"),
    (r"ELSS|TAX SAVER",
             120503,  "Axis ELSS Tax Saver Fund"),
    (r"EQUITY SAVINGS",
             135120,  "Axis Equity Savings Fund"),
    (r"BALANCED ADVANTAGE",
             141642,  "Axis Balanced Advantage Fund"),
]

FRANKLIN_MAP = [
    # Exclusions
    (r"ETF|INDEX FUND|NIFTY|SENSEX|BSE |OVERNIGHT|LIQUID|"
     r"MONEY MARKET|BOND|DEBT|GILT|DURATION|CREDIT|FLOATING|"
     r"DYNAMIC ACCRUAL|INCOME|SAVINGS.*DEBT|PENSION|FIXED MATURITY",
                None,  "Franklin debt/passive"),
    (r"TECHNOLOGY|HEALTHCARE|BUILD INDIA|ASIAN EQUITY|GLOBAL|"
     r"INTERNATIONAL|QUANT|RETIREMENT|INDIA PRIMA PLUS|PRIMA PLUS",
                None,  "Franklin thematic/other"),
    # Equity funds
    (r"LARGE & MID CAP|LARGE AND MID CAP",
             118510,  "Franklin India Large & Mid Cap Fund"),
    (r"LARGE CAP",
             118531,  "Franklin India Large Cap Fund"),
    (r"MID CAP|MIDCAP",
             118533,  "Franklin India Mid Cap Fund"),
    (r"SMALL CAP",
             118525,  "Franklin India Small Cap Fund"),
    (r"FLEXI.?CAP|FLEXICAP",
             118535,  "Franklin India Flexi Cap Fund"),
    (r"MULTI.?CAP|MULTICAP",
             152739,  "Franklin India Multi Cap Fund"),
    (r"FOCUSED",
             118564,  "Franklin India Focused Equity Fund"),
    (r"PRIMA FUND|PRIMA$",
             None,    "Franklin India Prima Fund"),  # mid cap legacy
    (r"BLUECHIP|BLUE CHIP",
             None,    "Franklin India Bluechip Fund"),
    (r"ELSS|TAX SAVER|TAX SHIELD",
             118540,  "Franklin India ELSS Tax Saver Fund"),
    (r"EQUITY SAVINGS",
             144466,  "Franklin India Equity Savings Fund"),
    (r"BALANCED ADVANTAGE",
             150481,  "Franklin India Balanced Advantage Fund"),
]

# Compile maps
AMC_COMPILED = {
    'Aditya_Birla': [(re.compile(p), c, n) for p, c, n in ABSL_MAP],
    'Kotak':        [(re.compile(p), c, n) for p, c, n in KOTAK_MAP],
    'ICICI_Pru':    [(re.compile(p), c, n) for p, c, n in ICICI_MAP],
    'Franklin':     [(re.compile(p), c, n) for p, c, n in FRANKLIN_MAP],
    'DSP':          [(re.compile(p), c, n) for p, c, n in DSP_MAP],
    'Axis':         [(re.compile(p), c, n) for p, c, n in AXIS_MAP],
}


def resolve_code(scheme_name: str, amc: str) -> int | None:
    """Return correct AMFI scheme_code for the given fund name and AMC."""
    compiled = AMC_COMPILED.get(amc)
    if compiled is None:
        return None
    short = str(scheme_name).split("(")[0].strip()
    upper = short.upper()
    for regex, code, _ in compiled:
        if regex.search(upper):
            return code
    return None  # unknown → NaN


def repair_parquet(path: Path, dry_run: bool = False) -> dict:
    df = pd.read_parquet(path)
    total_changed = 0

    for amc in ['Aditya_Birla', 'Kotak', 'ICICI_Pru', 'Franklin', 'DSP', 'Axis']:
        mask = df["amc"] == amc
        if not mask.any():
            continue

        nip = df[mask].copy()
        new_codes_raw = nip["scheme_name"].apply(lambda nm: resolve_code(nm, amc))
        new_codes = pd.array(new_codes_raw.tolist(), dtype="Float64")

        old_vals = nip["scheme_code"].fillna(-1).astype("float64")
        new_vals = pd.Series(new_codes, index=nip.index).fillna(-1).astype("float64")
        changed = int((old_vals != new_vals).sum())
        total_changed += changed

        if not dry_run:
            df.loc[mask, "scheme_code"] = new_codes

    if not dry_run and total_changed > 0:
        df.to_parquet(path, index=False)

    return {"path": path.name, "changed": total_changed}


def audit_assignments(path: Path) -> None:
    """Print a summary of code→name assignments for one parquet (dry-run audit)."""
    df = pd.read_parquet(path)
    for amc in ['Aditya_Birla', 'Kotak', 'ICICI_Pru', 'Franklin', 'DSP', 'Axis']:
        sub = df[df["amc"] == amc].copy()
        if sub.empty:
            continue
        sub["new_code"] = sub["scheme_name"].apply(lambda nm: resolve_code(nm, amc))
        sub["new_code"] = pd.array(sub["new_code"].tolist(), dtype="Float64")
        print(f"\n  {amc}:")
        for code, grp in sub.dropna(subset=["new_code"]).groupby("new_code"):
            names = grp["scheme_name"].unique()
            if len(names) > 1:
                print(f"    *** {int(code)}: {len(names)} fund names (collision!) ***")
                for nm in names[:3]:
                    print(f"         {nm[:65]}")
            else:
                print(f"    {int(code)}: {names[0][:65]}")


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--audit-month", default=None, help="e.g. 2026-04")
    p.add_argument("--data-dir", default=str(DATA_DIR))
    args = p.parse_args()

    files = sorted(Path(args.data_dir).glob("*.parquet"))

    if args.audit_month:
        target = [f for f in files if args.audit_month in f.name]
        if target:
            print(f"Audit for {target[0].name}:")
            audit_assignments(target[0])
        return

    print(f"Repairing {len(files)} parquets  (dry_run={args.dry_run})")
    total = 0
    for f in files:
        res = repair_parquet(f, dry_run=args.dry_run)
        if res["changed"]:
            print(f"  {res['path']}: changed={res['changed']}")
        total += res["changed"]

    print(f"\nTotal rows changed: {total}")
    if args.dry_run:
        print("Dry-run complete — re-run without --dry-run to apply.")
    else:
        print("Done.")


if __name__ == "__main__":
    main()
