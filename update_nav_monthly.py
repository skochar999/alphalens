#!/usr/bin/env python3
"""
update_nav_monthly.py
=====================
Fetches new month-end NAVs from mfapi.in for all schemes in fund_meta.parquet
and appends them to nav_monthly.parquet.

Run daily or monthly — idempotent (skips months already present).

Usage:
    python update_nav_monthly.py
    python update_nav_monthly.py --data-dir /path/to/mf_data
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fundlens.nav")

HERE     = Path(__file__).parent
DATA_DIR = HERE / "mf_data"

MFAPI_BASE  = "https://api.mfapi.in/mf"
REQUEST_DELAY = 0.15   # seconds between API calls (be polite)
MAX_RETRIES   = 3


def fetch_nav_history(scheme_code: int, session: requests.Session) -> pd.Series | None:
    """Fetch full NAV history for one scheme. Returns a Series indexed by month-end dates."""
    url = f"{MFAPI_BASE}/{scheme_code}"
    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                return None
            # Parse rows: {"date": "31-05-2023", "nav": "12.3456"}
            records = {}
            for row in data:
                try:
                    d  = pd.to_datetime(row["date"], format="%d-%m-%Y")
                    nav = float(row["nav"])
                    records[d] = nav
                except (ValueError, KeyError):
                    continue
            if not records:
                return None
            s = pd.Series(records).sort_index()
            # Keep only month-end: last trading day per calendar month
            s.index = pd.DatetimeIndex(s.index)
            monthly = s.resample("ME").last().dropna()
            return monthly
        except requests.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
            else:
                log.warning(f"  Failed {scheme_code}: {e}")
                return None
    return None


def last_month_end() -> pd.Timestamp:
    """Return the last completed month-end date."""
    today = pd.Timestamp.today().normalize()
    return (today - pd.offsets.MonthBegin(1)) - pd.offsets.Day(1)


def run(data_dir: Path) -> None:
    nav_path    = data_dir / "nav_monthly.parquet"
    meta_path   = data_dir / "fund_meta.parquet"
    reg_map_path = data_dir / "direct_to_regular_map.csv"

    if not meta_path.exists():
        raise FileNotFoundError(f"fund_meta.parquet not found in {data_dir}")

    meta = pd.read_parquet(meta_path)

    # Build direct→regular plan mapping (fetch regular plan NAV, store under direct code)
    reg_map: dict[int, int] = {}
    if reg_map_path.exists():
        rm = pd.read_csv(reg_map_path)
        reg_map = {int(r.direct_code): int(r.regular_code) for r in rm.itertuples()}
        log.info(f"Regular plan map: {len(reg_map)} direct→regular pairs loaded")
    else:
        log.warning("direct_to_regular_map.csv not found — will use direct plan NAVs")

    # All unique scheme codes (funds + their proxy benchmarks)
    fund_codes  = meta["scheme_code"].dropna().astype(int).unique().tolist()
    proxy_codes = meta["proxy_code"].dropna().astype(int).unique().tolist()
    all_codes   = list(set(fund_codes + proxy_codes))
    log.info(f"Tracking {len(fund_codes)} funds + {len(proxy_codes)} proxies = {len(all_codes)} total")
    log.info(f"  {sum(1 for c in fund_codes if c in reg_map)} funds will use regular plan NAVs")

    # Load existing data
    if nav_path.exists():
        existing = pd.read_parquet(nav_path)
        existing.index = pd.DatetimeIndex(existing.index)
        log.info(f"Existing nav_monthly: {existing.shape[0]} months × {existing.shape[1]} schemes")
        log.info(f"  Latest month: {existing.index.max().date()}")
    else:
        existing = pd.DataFrame()
        log.info("No existing nav_monthly.parquet — building from scratch")

    cutoff = last_month_end()
    log.info(f"Target latest month: {cutoff.date()}")

    # Work out which schemes need updating
    codes_to_fetch: list[int] = []
    for code in all_codes:
        if code not in existing.columns:
            codes_to_fetch.append(code)
        elif existing[code].dropna().index.max() < cutoff:
            codes_to_fetch.append(code)

    if not codes_to_fetch:
        log.info("All schemes are up to date. Nothing to fetch.")
        return

    log.info(f"Fetching {len(codes_to_fetch)} schemes …")

    session = requests.Session()
    session.headers["User-Agent"] = "AlphaLens/1.0 (research; contact skochar999@gmail.com)"

    new_series: dict[int, pd.Series] = {}
    for i, code in enumerate(codes_to_fetch):
        if i % 25 == 0:
            log.info(f"  {i}/{len(codes_to_fetch)} …")

        # Use regular plan code for fund NAVs if mapping exists;
        # proxy benchmark codes always use their own code (they are index funds)
        fetch_code = reg_map.get(code, code)
        is_regular = fetch_code != code
        s = fetch_nav_history(fetch_code, session)
        if s is not None and len(s) > 0:
            # Store under the direct plan code (pipeline key) regardless
            new_series[code] = s
            if is_regular and i < 5:
                log.info(f"  [{code}] using regular plan {fetch_code}")
        time.sleep(REQUEST_DELAY)

    if not new_series:
        log.warning("No new data fetched.")
        return

    log.info(f"Fetched data for {len(new_series)} schemes")

    # Merge into existing
    new_df = pd.DataFrame(new_series)
    new_df.index = pd.DatetimeIndex(new_df.index)

    if existing.empty:
        combined = new_df
    else:
        # Align on full date range, update existing with new data
        combined = existing.reindex(
            existing.index.union(new_df.index)
        ).copy()
        for code, series in new_series.items():
            combined.loc[series.index, code] = series.values

    combined.sort_index(inplace=True)
    combined.to_parquet(nav_path)

    log.info(f"Saved nav_monthly.parquet: {combined.shape[0]} months × {combined.shape[1]} schemes")
    log.info(f"  Date range: {combined.index.min().date()} → {combined.index.max().date()}")


def main() -> None:
    p = argparse.ArgumentParser(description="Update monthly NAV data from mfapi.in")
    p.add_argument("--data-dir", default=str(DATA_DIR), help="Path to mf_data folder")
    args = p.parse_args()
    run(Path(args.data_dir))


if __name__ == "__main__":
    main()
