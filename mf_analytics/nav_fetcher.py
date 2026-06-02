#!/usr/bin/env python3
"""
mf_analytics/nav_fetcher.py

Downloads historical daily NAV for all active equity schemes across
the top 10 AMCs using the free mfapi.in API.

Outputs:
    mf_data/nav_history.parquet   — (dates × scheme_codes) monthly NAV
    mf_data/scheme_list.parquet   — scheme metadata (code, name, AMC, category)

Usage:
    python mf_analytics/nav_fetcher.py
    python mf_analytics/nav_fetcher.py --output-dir ./mf_data --lookback-months 36
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mf.nav_fetcher")

BASE_URL = "https://api.mfapi.in/mf"

# Top 10 AMCs by equity AUM — (display name, list of name prefixes to match)
TOP10_AMCS = {
    "SBI":          ["SBI"],
    "HDFC":         ["HDFC"],
    "ICICI_Pru":    ["ICICI Prudential", "ICICI Pru"],
    "Nippon":       ["Nippon India", "Reliance"],
    "Kotak":        ["Kotak"],
    "Mirae":        ["Mirae"],
    "Axis":         ["Axis"],
    "DSP":          ["DSP"],
    "Franklin":     ["Franklin"],
    "Aditya_Birla": ["Aditya Birla", "ABSL"],
}

EQUITY_KEYWORDS = [
    "equity", "large cap", "mid cap", "small cap", "flexi", "multi cap",
    "large & mid", "large and mid", "focused", "value fund", "value ",
    "contra", "dividend yield", "elss", "tax saver", "sectoral",
    "thematic", "bluechip", "blue chip", "multicap", "midcap", "largecap",
    "smallcap", "opportunities", "advantage",
]

EXCLUDE_KEYWORDS = [
    "idcw", "dividend", "payout", "bonus", "segregated",
    "fund of fund", "fof", "international", "overseas", "global",
    "us opportunities", "asian equity",   # FoF-style, no Indian holdings
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    p.add_argument("--output-dir", default="./mf_data")
    p.add_argument("--lookback-months", type=int, default=36)
    p.add_argument("--delay", type=float, default=0.15,
                   help="Seconds between API calls (be polite to mfapi.in)")
    p.add_argument("--force", action="store_true",
                   help="Re-download even if cache exists")
    return p.parse_args()


def fetch_scheme_list() -> list[dict]:
    """Return filtered list of active equity direct-growth schemes for top 10 AMCs."""
    log.info("Fetching master scheme list from mfapi.in …")
    r = requests.get(BASE_URL, timeout=30)
    r.raise_for_status()
    all_schemes = r.json()
    log.info(f"  Total schemes: {len(all_schemes)}")

    result = []
    for s in all_schemes:
        name = s.get("schemeName", "")
        name_lower = name.lower()

        # Must have ISIN (active scheme)
        if not s.get("isinGrowth"):
            continue
        # Must be equity
        if not any(kw in name_lower for kw in EQUITY_KEYWORDS):
            continue
        # Must be direct plan
        if "direct" not in name_lower:
            continue
        # Exclude dividend / FoF / international
        if any(kw in name_lower for kw in EXCLUDE_KEYWORDS):
            continue

        # Match to one of the top 10 AMCs
        amc_matched = None
        for amc_key, prefixes in TOP10_AMCS.items():
            if any(name.startswith(p) or name.upper().startswith(p.upper())
                   for p in prefixes):
                amc_matched = amc_key
                break
        if not amc_matched:
            continue

        result.append({
            "scheme_code": s["schemeCode"],
            "scheme_name": name,
            "amc":         amc_matched,
            "isin":        s["isinGrowth"],
        })

    log.info(f"  Active equity direct schemes (top 10 AMCs): {len(result)}")
    return result


def fetch_nav_history(scheme_code: int, lookback_months: int) -> pd.Series | None:
    """
    Fetch daily NAV history for one scheme. Returns a Series indexed by date,
    filtered to the last `lookback_months` months. Returns None on failure.
    """
    url = f"{BASE_URL}/{scheme_code}"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as exc:
        log.warning(f"  Scheme {scheme_code}: fetch failed — {exc}")
        return None

    if not data:
        return None

    records = [(d["date"], float(d["nav"])) for d in data if d["nav"] != "N.A."]
    if not records:
        return None

    s = pd.Series(
        dict(records),
        name=scheme_code,
    )
    s.index = pd.to_datetime(s.index, format="%d-%m-%Y", dayfirst=True)
    s = s.sort_index()

    # Filter to lookback window
    cutoff = pd.Timestamp.today() - pd.DateOffset(months=lookback_months)
    return s[s.index >= cutoff]


def compute_monthly_nav(daily_nav: pd.DataFrame) -> pd.DataFrame:
    """Resample daily NAV to month-end values."""
    return daily_nav.resample("ME").last()


def main() -> None:
    args = _parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    scheme_path = out_dir / "scheme_list.parquet"
    nav_path    = out_dir / "nav_monthly.parquet"

    # ------------------------------------------------------------------ #
    # Step 1 — Scheme list
    # ------------------------------------------------------------------ #
    if not args.force and scheme_path.exists():
        log.info("Loading cached scheme list …")
        schemes = pd.read_parquet(scheme_path).to_dict("records")
    else:
        schemes = fetch_scheme_list()
        pd.DataFrame(schemes).to_parquet(scheme_path)
        log.info(f"  Saved → {scheme_path}")

    # ------------------------------------------------------------------ #
    # Step 2 — NAV history
    # ------------------------------------------------------------------ #
    if not args.force and nav_path.exists():
        log.info("NAV history already cached. Use --force to re-download.")
        nav_monthly = pd.read_parquet(nav_path)
    else:
        log.info(f"\nDownloading NAV history for {len(schemes)} schemes …")
        daily_navs: dict[int, pd.Series] = {}

        for i, s in enumerate(schemes):
            code = s["scheme_code"]
            nav = fetch_nav_history(code, args.lookback_months)
            if nav is not None and len(nav) >= 20:
                daily_navs[code] = nav
            else:
                log.warning(f"  [{i+1}/{len(schemes)}] {s['scheme_name'][:50]}: insufficient data")

            if (i + 1) % 20 == 0:
                log.info(f"  … {i+1}/{len(schemes)} schemes fetched")
            time.sleep(args.delay)

        log.info(f"  NAV data collected for {len(daily_navs)}/{len(schemes)} schemes")

        # Combine into daily panel, then resample to month-end
        daily_panel = pd.DataFrame(daily_navs)
        nav_monthly = compute_monthly_nav(daily_panel)
        nav_monthly.to_parquet(nav_path)
        log.info(f"  Saved → {nav_path}  shape: {nav_monthly.shape}")

    # ------------------------------------------------------------------ #
    # Step 3 — Monthly returns
    # ------------------------------------------------------------------ #
    returns_monthly = nav_monthly.pct_change(fill_method=None).iloc[1:]
    returns_path = out_dir / "returns_monthly.parquet"
    returns_monthly.to_parquet(returns_path)

    log.info(f"\nMonthly returns: {returns_monthly.shape}")
    log.info(f"  Date range: {returns_monthly.index[0].date()} → {returns_monthly.index[-1].date()}")
    log.info(f"  Saved → {returns_path}")
    log.info("\nNAV fetcher complete.")


if __name__ == "__main__":
    main()
