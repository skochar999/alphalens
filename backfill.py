#!/usr/bin/env python3
"""
IEC-1 historical backfill script.

Downloads one year of daily price returns via yfinance and runs the
cross-sectional WLS regression for each trading day, using today's
factor exposure matrix as a static proxy for historical exposures.

This bootstraps the factor-return history so that FCM and specific-risk
estimates are available immediately rather than after 30+ live daily runs.

IMPORTANT — known limitation
-----------------------------
True factor-model backfills require point-in-time fundamental data.
Because yfinance and Screener.in only expose *current* snapshots, this
script uses today's exposure matrix for all historical regressions.
This introduces a mild look-ahead bias in the exposure loadings but is
acceptable for the sole purpose of bootstrapping the covariance matrix.
It should NOT be used for performance attribution or strategy backtesting.

Usage
-----
    python backfill.py
    python backfill.py --lookback 252           # default: 1 trading year
    python backfill.py --output-dir ./inec1_outputs
    python backfill.py --force                  # overwrite existing history
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("inec1.backfill")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IEC-1 historical backfill",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--output-dir", default="./inec1_outputs",
                   help="Directory containing inec1 output Parquet files")
    p.add_argument("--lookback", type=int, default=252,
                   help="Number of trading days to backfill")
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing history files")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_latest_exposures(out_dir: Path) -> pd.DataFrame:
    """Load the most recent dated exposure Parquet file."""
    candidates = sorted(out_dir.glob("exposures_*.parquet"))
    if not candidates:
        raise FileNotFoundError(
            f"No exposure files found in {out_dir}. "
            "Run run_daily.py at least once first."
        )
    path = candidates[-1]
    log.info(f"  Using exposure snapshot: {path.name}")
    df = pd.read_parquet(path)
    log.info(f"  Shape: {df.shape}")
    return df


def _fetch_price_history(tickers_ns: list[str], lookback: int) -> pd.DataFrame:
    """
    Download enough price history to produce `lookback` daily return rows.
    Returns a DataFrame of shape (lookback, N) — index = date strings,
    columns = bare NSE symbols (no .NS suffix).
    """
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance required.  Run: pip install yfinance")

    # Download 1.6× calendar days to safely cover weekends + holidays
    cal_days = int(lookback * 1.6)
    log.info(
        f"  Downloading ~{cal_days} calendar days of price history "
        f"for {len(tickers_ns)} tickers …"
    )

    raw = yf.download(
        tickers_ns,
        period=f"{cal_days}d",
        auto_adjust=True,
        progress=True,
        threads=True,
    )

    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:
        close = raw[["Close"]]
        close.columns = tickers_ns[:1]

    # Daily returns; strip .NS suffix so columns match exposure index
    returns = close.pct_change(fill_method=None).iloc[1:]
    returns.columns = [c.replace(".NS", "") for c in returns.columns]

    # Keep only the most recent `lookback` trading days
    returns = returns.tail(lookback)
    log.info(
        f"  Price history: {len(returns)} trading days × "
        f"{returns.shape[1]} tickers"
    )
    return returns


def _merge_with_existing(
    new_df: pd.DataFrame,
    path: Path,
    max_rows: int,
) -> pd.DataFrame:
    """
    Merge new_df (backfilled rows) with an existing history file.
    Existing rows win on date conflicts (live runs are authoritative).
    Returns sorted, deduplicated, trimmed DataFrame.
    """
    if path.exists():
        existing = pd.read_parquet(path)
        combined = pd.concat([new_df, existing])
        # keep last occurrence of each date — existing rows are appended last
        combined = combined[~combined.index.duplicated(keep="last")]
        combined = combined.sort_index()
    else:
        combined = new_df.sort_index()

    if len(combined) > max_rows:
        combined = combined.iloc[-max_rows:]

    combined.to_parquet(path)
    return combined


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = _parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("=" * 64)
    log.info("  IEC-1 historical backfill")
    log.info(f"  lookback : {args.lookback} trading days")
    log.info(f"  output   : {out_dir}")
    log.info("=" * 64)

    hist_fr_path  = out_dir / "factor_returns_history.parquet"
    hist_res_path = out_dir / "residuals_history.parquet"
    hist_r2_path  = out_dir / "r2_history.parquet"

    # Guard: don't redo if history is already long enough
    if not args.force and hist_fr_path.exists():
        existing = pd.read_parquet(hist_fr_path)
        if len(existing) >= args.lookback:
            log.info(
                f"History already has {len(existing)} rows "
                f"(≥ {args.lookback}). Use --force to redo."
            )
            return 0

    # ------------------------------------------------------------------ #
    # Step 1 — Load today's exposure matrix
    # ------------------------------------------------------------------ #
    log.info("\n[1/5] Loading factor exposure matrix …")
    exposures = _load_latest_exposures(out_dir)
    exposures_clean = exposures.dropna()
    n_dropped = len(exposures) - len(exposures_clean)
    log.info(
        f"  {len(exposures_clean)} complete rows "
        f"({n_dropped} dropped — NaN exposures)"
    )

    # Build .NS ticker list for price download
    tickers_ns = [
        t + ".NS" if not t.endswith(".NS") else t
        for t in exposures_clean.index
    ]

    # ------------------------------------------------------------------ #
    # Step 2 — Download price history
    # ------------------------------------------------------------------ #
    log.info("\n[2/5] Fetching daily price history …")
    price_returns = _fetch_price_history(tickers_ns, args.lookback)

    # price_returns columns are bare symbols (no .NS); strip .NS from exposure index to match
    exposures_bare = exposures_clean.copy()
    exposures_bare.index = exposures_bare.index.str.replace(".NS", "", regex=False)

    common = exposures_bare.index.intersection(price_returns.columns)
    log.info(f"  Common tickers (exposures ∩ prices): {len(common)}")

    if len(common) < 50:
        log.error("Fewer than 50 common tickers — cannot proceed.")
        return 1

    exposures_reg = exposures_bare.loc[common]
    price_returns = price_returns[list(common)]

    # ------------------------------------------------------------------ #
    # Step 3 — Cross-sectional regression for every trading day
    # ------------------------------------------------------------------ #
    log.info(f"\n[3/5] Running cross-sectional regression "
             f"for {len(price_returns)} days …")

    from inec1 import CrossSectionalRegressor

    # Use equal weights — we don't have historical market caps
    regressor = CrossSectionalRegressor(weight_scheme="equal")

    fr_rows:  dict[str, pd.Series] = {}
    res_rows: dict[str, pd.Series] = {}
    r2_rows:  dict[str, float]     = {}

    n_ok = n_skip = 0

    for i, (ts, ret_row) in enumerate(price_returns.iterrows()):
        date_str = str(ts)[:10]  # "YYYY-MM-DD"

        # Drop stocks with missing return on this specific day
        ret_series = ret_row.dropna()
        if len(ret_series) < 50:
            log.warning(
                f"  {date_str}: only {len(ret_series)} stocks "
                "with returns — skipping"
            )
            n_skip += 1
            continue

        # Align exposures to available returns
        exp_day = exposures_reg.reindex(ret_series.index).dropna()
        ret_day = ret_series.reindex(exp_day.index)

        if len(ret_day) < 50:
            log.warning(
                f"  {date_str}: fewer than 50 stocks after "
                "exposure alignment — skipping"
            )
            n_skip += 1
            continue

        try:
            result = regressor.fit_day(
                returns=ret_day,
                exposures=exp_day,
                market_caps=None,
                date=date_str,
            )
        except Exception as exc:
            log.warning(f"  {date_str}: regression failed — {exc}")
            n_skip += 1
            continue

        fr_rows[date_str]  = result["factor_returns"]
        res_rows[date_str] = result["residuals"]
        r2_rows[date_str]  = result["r_squared"]
        n_ok += 1

        if (i + 1) % 25 == 0:
            log.info(
                f"  … {i+1}/{len(price_returns)} days processed "
                f"(R² last day: {result['r_squared']:.4f})"
            )

    log.info(
        f"  Done: {n_ok} days regressed, {n_skip} skipped "
        f"(insufficient data)"
    )

    if n_ok == 0:
        log.error("No days successfully regressed — aborting.")
        return 1

    # ------------------------------------------------------------------ #
    # Step 4 — Persist history (merge with any existing live runs)
    # ------------------------------------------------------------------ #
    log.info("\n[4/5] Saving history files …")

    fr_hist  = pd.DataFrame(fr_rows).T
    res_hist = pd.DataFrame(res_rows).T
    r2_hist  = pd.DataFrame({"r2": r2_rows})

    fr_final  = _merge_with_existing(fr_hist,  hist_fr_path,  max_rows=args.lookback)
    res_final = _merge_with_existing(res_hist, hist_res_path, max_rows=args.lookback)
    r2_final  = _merge_with_existing(r2_hist,  hist_r2_path,  max_rows=args.lookback)

    log.info(f"  factor_returns_history  : {len(fr_final)} days")
    log.info(f"  residuals_history       : {len(res_final)} days")
    log.info(f"  r2_history              : {len(r2_final)} days")

    # ------------------------------------------------------------------ #
    # Step 5 — Estimate FCM + specific risk with full history
    # ------------------------------------------------------------------ #
    log.info("\n[5/5] Estimating FCM and specific risk …")
    from inec1 import CovarianceEstimator

    cov_est = CovarianceEstimator(
        lookback=252,
        spec_lookback=60,
        shrinkage="ledoit_wolf",
    )

    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    fcm = spec_var = None

    if len(fr_final) >= 30:
        fcm = cov_est.fit_fcm(fr_final)
        min_eig = np.linalg.eigvalsh(fcm.values).min()
        fcm_path = out_dir / f"fcm_{today}.parquet"
        fcm.to_parquet(fcm_path)
        log.info(
            f"  FCM saved → {fcm_path.name}  "
            f"shape: {fcm.shape}  min eigenvalue: {min_eig:.6f}"
        )
    else:
        log.warning(
            f"  Only {len(fr_final)} days — FCM needs 30. "
            "Skipping FCM."
        )

    if len(res_final) >= 20:
        spec_var = cov_est.fit_specific_risk(res_final, min_obs=20)
        srisk_path = out_dir / f"specific_risk_{today}.parquet"
        spec_var.to_frame("specific_var").to_parquet(srisk_path)
        median_risk = np.sqrt(spec_var.median()) * 100
        p95_risk    = np.sqrt(spec_var.quantile(0.95)) * 100
        log.info(
            f"  Specific risk saved → {srisk_path.name}  "
            f"median: {median_risk:.2f}%  p95: {p95_risk:.2f}%"
        )
    else:
        log.warning(
            f"  Only {len(res_final)} days — specific risk needs 20. "
            "Skipping."
        )

    # Rebuild dashboard to reflect the new history
    log.info("\nRebuilding dashboard …")
    import subprocess
    here = Path(__file__).parent
    r = subprocess.run(
        [sys.executable, "build_dashboard.py",
         "--output-dir", str(out_dir),
         "--out", str(here / "dashboard.html")],
        capture_output=True, text=True, cwd=str(here),
    )
    if r.returncode == 0:
        log.info(r.stdout.strip() or "  dashboard.html updated")
    else:
        log.warning(f"  Dashboard build failed: {r.stderr[:300]}")

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    log.info("")
    log.info("=" * 64)
    log.info("  Backfill complete")
    log.info(f"  History       : {len(fr_final)} days")
    log.info(f"  FCM           : {'saved' if fcm is not None else 'pending (need 30 days)'}")
    log.info(f"  Specific risk : {'saved' if spec_var is not None else 'pending (need 20 days)'}")
    log.info(
        "\n  NOTE: exposures are static (today's snapshot used for all days).\n"
        "  This is fine for risk-model bootstrapping; do not use for\n"
        "  strategy backtesting or attribution."
    )
    log.info("=" * 64)

    return 0


if __name__ == "__main__":
    sys.exit(main())
