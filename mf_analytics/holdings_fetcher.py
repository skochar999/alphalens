#!/usr/bin/env python3
"""
mf_analytics/holdings_fetcher.py

Downloads monthly portfolio holdings for all schemes from AMFI's
consolidated portfolio disclosure files (SEBI-mandated format).

AMFI publishes one text file per month covering ALL mutual fund schemes
in a standardised pipe-delimited format. This script downloads those
files for the past N months and parses holdings for our target schemes.

Outputs (under --output-dir):
    holdings/YYYY-MM.parquet  — one file per month, schema:
        scheme_code | isin_stock | stock_name | weight_pct | market_value_cr

Usage:
    python mf_analytics/holdings_fetcher.py
    python mf_analytics/holdings_fetcher.py --lookback-months 36 --output-dir ./mf_data
"""
from __future__ import annotations

import argparse
import io
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
log = logging.getLogger("mf.holdings_fetcher")

# AMFI consolidated portfolio disclosure URL pattern
# Published around the 10th of the following month
# e.g. for April 2026: https://www.amfiindia.com/modules/PortfolioHoldings_new?mfID=0&mfSchemeID=0&asondate=30-Apr-2026&as=1
AMFI_BASE = "https://www.amfiindia.com/modules/PortfolioHoldings_new"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.amfiindia.com/",
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    p.add_argument("--output-dir", default="./mf_data")
    p.add_argument("--lookback-months", type=int, default=36)
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds between AMFI requests")
    p.add_argument("--force", action="store_true",
                   help="Re-download months that are already cached")
    return p.parse_args()


def _month_end_dates(lookback_months: int) -> list[pd.Timestamp]:
    """Return list of month-end dates going back `lookback_months` months."""
    today = pd.Timestamp.today()
    dates = []
    for i in range(1, lookback_months + 1):
        # go back i months from today, then take month-end
        d = today - pd.DateOffset(months=i)
        d = d + pd.offsets.MonthEnd(0)
        dates.append(d)
    return sorted(dates)


def _amfi_date_str(ts: pd.Timestamp) -> str:
    """Format timestamp as AMFI expects: '30-Apr-2026'"""
    return ts.strftime("%-d-%b-%Y")


def fetch_amfi_holdings(date: pd.Timestamp, session: requests.Session) -> str | None:
    """
    Download the AMFI consolidated portfolio text for a given month-end date.
    Returns raw text content or None on failure.
    """
    date_str = _amfi_date_str(date)
    params = {
        "mfID": 0,
        "mfSchemeID": 0,
        "asondate": date_str,
        "as": 1,
    }
    url = AMFI_BASE
    try:
        r = session.get(url, params=params, headers=HEADERS, timeout=30)
        if r.status_code == 200 and len(r.text) > 1000:
            return r.text
        log.warning(f"  {date_str}: HTTP {r.status_code}, size {len(r.text)}")
        return None
    except Exception as exc:
        log.warning(f"  {date_str}: request failed — {exc}")
        return None


def parse_amfi_text(raw: str, scheme_codes: set[int]) -> pd.DataFrame:
    """
    Parse AMFI portfolio disclosure text into a tidy DataFrame.

    The AMFI format is:
        Scheme Name;Scheme Code;ISIN;Company Name;Rating;Market Value;% to NAV
        (semicolon-delimited, one section per scheme, with blank lines between)

    Returns DataFrame with columns:
        scheme_code, isin_stock, stock_name, weight_pct, market_value_cr
    """
    rows = []
    current_scheme_code = None

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        # Detect scheme header lines (they contain the scheme code)
        # AMFI format: lines that start a new scheme block contain the scheme name + code
        # We detect by checking if the line has the scheme code pattern
        parts = [p.strip() for p in line.split(";")]

        if len(parts) >= 2:
            # Try to detect scheme header: second field is numeric scheme code
            try:
                code = int(parts[1])
                if code in scheme_codes:
                    current_scheme_code = code
                elif len(parts) < 5:
                    # Might be a different scheme we don't care about
                    current_scheme_code = None
                continue
            except ValueError:
                pass

        # Stock holding row: expect at least 6 fields
        if current_scheme_code is None or len(parts) < 6:
            continue

        try:
            isin_stock    = parts[2].strip() if len(parts) > 2 else ""
            stock_name    = parts[3].strip() if len(parts) > 3 else ""
            market_value  = float(parts[4].replace(",", "")) if parts[4].replace(",", "").replace(".", "").lstrip("-").isdigit() else None
            weight_pct    = float(parts[5].replace(",", "")) if parts[5].replace(",", "").replace(".", "").lstrip("-").isdigit() else None

            if weight_pct is not None and weight_pct > 0 and stock_name:
                rows.append({
                    "scheme_code":    current_scheme_code,
                    "isin_stock":     isin_stock,
                    "stock_name":     stock_name,
                    "weight_pct":     weight_pct,
                    "market_value_cr": market_value,
                })
        except (ValueError, IndexError):
            continue

    return pd.DataFrame(rows)


def load_scheme_list(mf_data_dir: Path) -> pd.DataFrame:
    """Load scheme list saved by nav_fetcher.py."""
    path = mf_data_dir / "scheme_list.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"Scheme list not found at {path}. Run nav_fetcher.py first."
        )
    return pd.read_parquet(path)


def main() -> None:
    args = _parse_args()
    out_dir  = Path(args.output_dir)
    hold_dir = out_dir / "holdings"
    hold_dir.mkdir(parents=True, exist_ok=True)

    # Load target scheme codes
    schemes = load_scheme_list(out_dir)
    scheme_codes = set(schemes["scheme_code"].tolist())
    log.info(f"Target schemes: {len(scheme_codes)}")

    months = _month_end_dates(args.lookback_months)
    log.info(f"Months to fetch: {len(months)}  "
             f"({months[0].strftime('%b %Y')} → {months[-1].strftime('%b %Y')})")

    session = requests.Session()
    ok = skip = fail = 0

    for ts in months:
        month_str = ts.strftime("%Y-%m")
        out_path  = hold_dir / f"{month_str}.parquet"

        if not args.force and out_path.exists():
            log.info(f"  {month_str}: cached — skipping")
            skip += 1
            continue

        log.info(f"  Fetching {ts.strftime('%b %Y')} …")
        raw = fetch_amfi_holdings(ts, session)
        if raw is None:
            log.warning(f"  {month_str}: no data returned")
            fail += 1
            time.sleep(args.delay)
            continue

        df = parse_amfi_text(raw, scheme_codes)

        if df.empty:
            log.warning(f"  {month_str}: parsed 0 holdings — check AMFI format")
            fail += 1
        else:
            df.to_parquet(out_path)
            n_schemes = df["scheme_code"].nunique()
            log.info(f"  {month_str}: {len(df)} holdings across {n_schemes} schemes → {out_path.name}")
            ok += 1

        time.sleep(args.delay)

    log.info(f"\nHoldings fetch complete: {ok} months OK, {skip} cached, {fail} failed")

    if fail > 0:
        log.warning(
            "\nSome months failed to download. Common causes:\n"
            "  • AMFI hasn't published that month's data yet\n"
            "  • Network/SSL issue — try running again\n"
            "  • AMFI URL format changed — check amfiindia.com manually\n"
            "  If the issue persists, download the portfolio file manually from\n"
            "  amfiindia.com → Research → Portfolio Disclosure, save as text,\n"
            "  and place it in mf_data/holdings/{YYYY-MM}.txt"
        )


if __name__ == "__main__":
    main()
