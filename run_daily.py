#!/usr/bin/env python3
"""
IEC-1 daily pipeline runner for Nifty 100.

Fetches today's cross-sectional fundamental data and stock returns, estimates
factor returns via WLS, accumulates rolling history, and produces a dated
Factor Covariance Matrix (FCM) and factor exposure matrix.

Usage
-----
    python run_daily.py
    python run_daily.py --date 2025-05-22
    python run_daily.py --output-dir ./inec1_outputs --cache-dir ./inec1_cache
    python run_daily.py --no-screener      # skip Screener.in (faster, lower coverage)
    python run_daily.py --force            # overwrite existing outputs for --date

Output files  (under --output-dir)
-----------------------------------
    exposures_{date}.parquet          (N × 39) factor loading matrix
    fcm_{date}.parquet                (39 × 39) factor covariance matrix
    specific_risk_{date}.parquet      (N,) annualised specific variance
    factor_returns_{date}.parquet     (1 × 39) today's estimated factor returns
    factor_returns_history.parquet    rolling (T × 39) factor return panel
    residuals_history.parquet         rolling (T × N) idiosyncratic residual panel

Notes
-----
* FCM requires at least 30 days of factor-return history.  On the first
  few runs only exposures and factor returns are written; FCM and specific
  risk are added once sufficient history exists.

* --date only affects output file names and history indexing.  Daily
  returns are always fetched from the most recent available market data.
  For historical backfills, call the script once per day in sequence so
  the history file builds up properly.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("inec1.daily")

# Minimum history required before covariance estimation is attempted
_MIN_DAYS_FCM  = 30
_MIN_DAYS_SPEC = 20


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IEC-1 daily pipeline runner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--date",
        default=str(date.today()),
        metavar="YYYY-MM-DD",
        help="Label for this run (used in output filenames and history index)",
    )
    p.add_argument("--output-dir", default="./inec1_outputs",
                   help="Directory for output Parquet files")
    p.add_argument("--cache-dir",  default="./inec1_cache",
                   help="Directory for yfinance / Screener cache")
    p.add_argument("--no-screener", action="store_true",
                   help="Skip Screener.in fetch (yfinance only)")
    p.add_argument("--force", action="store_true",
                   help="Overwrite outputs if they already exist for --date")
    p.add_argument("--history-days", type=int, default=252,
                   help="Maximum rows to keep in rolling history files")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _fetch_daily_returns(tickers: list[str]) -> pd.Series:
    """
    Download 5 days of price history via yfinance and return the most
    recent single-day percentage change for each ticker.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance is required. Run: pip install yfinance")

    log.info(f"  Downloading price history for {len(tickers)} tickers …")
    raw = yf.download(
        tickers,
        period="5d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    # yfinance returns MultiIndex columns (field, ticker) for multiple tickers
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:
        # Single ticker edge-case (shouldn't happen for Nifty 100, but be safe)
        close = raw[["Close"]]
        close.columns = tickers[:1]

    daily = close.pct_change(fill_method=None).iloc[-1].dropna()
    log.info(f"  Returns available for {len(daily)}/{len(tickers)} tickers")
    return daily


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

def _load_history(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def _append_and_save(
    history: pd.DataFrame,
    new_row: pd.Series,
    path: Path,
    max_rows: int,
    run_date: str,
) -> pd.DataFrame:
    """
    Append `new_row` (a Series whose name is `run_date`) to `history`,
    drop any existing row with the same date label, trim to `max_rows`,
    and persist to `path`.

    Returns the updated history DataFrame.
    """
    row_df = new_row.to_frame().T  # (1, K)
    row_df.index = [run_date]

    if run_date in history.index:
        history = history.drop(index=run_date)

    updated = pd.concat([history, row_df])

    if len(updated) > max_rows:
        updated = updated.iloc[-max_rows:]

    updated.to_parquet(path)
    return updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = _parse_args()
    run_date = args.date
    out_dir  = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("=" * 64)
    log.info(f"  IEC-1 daily runner  |  date: {run_date}")
    log.info("=" * 64)

    # Paths for today's dated outputs
    exp_path    = out_dir / f"exposures_{run_date}.parquet"
    fcm_path    = out_dir / f"fcm_{run_date}.parquet"
    srisk_path  = out_dir / f"specific_risk_{run_date}.parquet"
    fr_day_path = out_dir / f"factor_returns_{run_date}.parquet"

    # Paths for rolling history
    hist_fr_path  = out_dir / "factor_returns_history.parquet"
    hist_res_path = out_dir / "residuals_history.parquet"
    hist_r2_path  = out_dir / "r2_history.parquet"

    if not args.force and exp_path.exists() and fr_day_path.exists():
        log.info(f"Outputs already exist for {run_date}. Use --force to overwrite.")
        return 0

    # ------------------------------------------------------------------ #
    # Step 1 — Fetch universe fundamentals
    # ------------------------------------------------------------------ #
    from inec1.data.pipeline import DataPipeline
    from inec1.data.universe import load_from_nse_csv, get_nifty500

    # Resolve the Nifty 500 universe from the first available source so the
    # daily run never breaks on a missing file:
    #   1) project-local copy (mf_data/ind_nifty500list.csv) — stable, preferred
    #   2) the original ~/Downloads CSV (legacy location)
    #   3) the built-in list in inec1/data/universe.py (always works, lower coverage)
    _here = Path(__file__).parent
    _csv_candidates = [
        _here / "mf_data" / "ind_nifty500list.csv",
        Path("/Users/siddharthakochar/Downloads/ind_nifty500list.csv"),
    ]
    tickers = None
    for _csv in _csv_candidates:
        if _csv.exists():
            tickers = load_from_nse_csv(_csv)
            log.info(f"  Universe: {len(tickers)} tickers from {_csv}")
            break
    if tickers is None:
        tickers = get_nifty500()
        log.warning(
            f"  Universe: no NSE CSV found — using built-in list ({len(tickers)} "
            f"tickers). To refresh constituents, drop an updated "
            f"ind_nifty500list.csv into mf_data/."
        )

    log.info(f"\n[1/5] Fetching fundamental data ({len(tickers)} tickers) …")

    pipeline = DataPipeline(
        cache_dir=args.cache_dir,
        use_screener=not args.no_screener,
    )
    security_data = pipeline.build(tickers, verbose=True)
    n_loaded = security_data.notna().any(axis=1).sum()
    log.info(f"\n  Loaded {n_loaded}/{len(tickers)} tickers with at least one descriptor")
    log.info(pipeline.coverage_report(security_data))

    if n_loaded < 50:
        log.error("Fewer than 50 securities loaded — cannot proceed.")
        return 1

    # ------------------------------------------------------------------ #
    # Step 2 — Fetch today's stock returns
    # ------------------------------------------------------------------ #
    log.info("\n[2/5] Fetching daily returns …")
    daily_returns = _fetch_daily_returns(tickers)

    # ------------------------------------------------------------------ #
    # Step 3 — Build factor exposures
    # ------------------------------------------------------------------ #
    log.info("\n[3/5] Building factor exposures …")
    from inec1 import ExposureBuilder

    builder   = ExposureBuilder()
    exposures = builder.build(security_data)

    n_complete = exposures.notna().all(axis=1).sum()
    log.info(f"  Exposure matrix: {exposures.shape}  "
             f"({n_complete} fully populated rows, "
             f"{exposures.isna().any(axis=1).sum()} rows with NaN)")

    # ------------------------------------------------------------------ #
    # Step 4 — Cross-sectional regression (today only)
    # ------------------------------------------------------------------ #
    log.info("\n[4/5] Running cross-sectional regression …")
    from inec1 import CrossSectionalRegressor

    regressor   = CrossSectionalRegressor(weight_scheme="market_cap")
    market_caps = security_data["market_cap"].reindex(exposures.index)

    # Drop stocks with any NaN exposure — the WLS solver requires complete rows.
    # The full exposure matrix (including NaN rows) is still saved to Parquet below.
    exposures_clean = exposures.dropna()
    n_dropped = len(exposures) - len(exposures_clean)
    if n_dropped:
        log.info(f"  Dropped {n_dropped} stocks with incomplete exposures from regression")

    try:
        result = regressor.fit_day(
            returns=daily_returns,
            exposures=exposures_clean,
            market_caps=market_caps.reindex(exposures_clean.index),
            date=run_date,
        )
    except ValueError as exc:
        log.error(f"  Regression failed: {exc}")
        return 1

    log.info(f"  R²: {result['r_squared']:.4f}  |  "
             f"securities in regression: {result['n_securities']}")

    # Log top 5 factor t-stats
    top_t = result["factor_t_stats"].abs().nlargest(5)
    log.info("  Top |t|-stats: " +
             "  ".join(f"{f} {v:.2f}" for f, v in top_t.items()))

    # ------------------------------------------------------------------ #
    # Step 5 — Update history, estimate FCM + specific risk
    # ------------------------------------------------------------------ #
    log.info("\n[5/5] Updating history and estimating covariance …")
    from inec1 import CovarianceEstimator

    # Factor return history
    fr_hist = _load_history(hist_fr_path)
    fr_hist = _append_and_save(
        fr_hist, result["factor_returns"],
        hist_fr_path, args.history_days, run_date,
    )
    log.info(f"  Factor return history: {len(fr_hist)} days stored")

    # Residual history
    res_hist = _load_history(hist_res_path)
    res_hist = _append_and_save(
        res_hist, result["residuals"],
        hist_res_path, args.history_days, run_date,
    )

    # R² history
    r2_hist = _load_history(hist_r2_path)
    r2_hist = _append_and_save(
        r2_hist, pd.Series({"r2": result["r_squared"]}),
        hist_r2_path, args.history_days, run_date,
    )

    # Factor Covariance Matrix
    cov_est = CovarianceEstimator(
        lookback=252,
        spec_lookback=60,
        shrinkage="ledoit_wolf",
    )

    fcm      = None
    spec_var = None

    if len(fr_hist) >= _MIN_DAYS_FCM:
        fcm = cov_est.fit_fcm(fr_hist)
        min_eig = np.linalg.eigvalsh(fcm.values).min()
        log.info(f"  FCM: {fcm.shape}  |  min eigenvalue: {min_eig:.6f}  (must be > 0)")
    else:
        log.warning(
            f"  {len(fr_hist)} day(s) of history — FCM needs {_MIN_DAYS_FCM}. "
            "Skipping FCM this run."
        )

    # Specific risk
    if len(res_hist) >= _MIN_DAYS_SPEC:
        spec_var = cov_est.fit_specific_risk(res_hist, min_obs=_MIN_DAYS_SPEC)
        median_risk = np.sqrt(spec_var.median()) * 100
        p95_risk    = np.sqrt(spec_var.quantile(0.95)) * 100
        log.info(f"  Specific risk  median: {median_risk:.2f}%  p95: {p95_risk:.2f}%")
    else:
        log.warning(
            f"  {len(res_hist)} day(s) of residual history — specific risk needs "
            f"{_MIN_DAYS_SPEC}. Skipping this run."
        )

    # ------------------------------------------------------------------ #
    # Save dated outputs
    # ------------------------------------------------------------------ #
    log.info("\nSaving outputs …")

    exposures.to_parquet(exp_path)
    log.info(f"  {exp_path}")

    # Save today's factor returns as a single-row DataFrame (consistent with history shape)
    result["factor_returns"].to_frame().T.assign(**{"date": run_date}).set_index(
        "date"
    ).to_parquet(fr_day_path)
    log.info(f"  {fr_day_path}")

    if fcm is not None:
        fcm.to_parquet(fcm_path)
        log.info(f"  {fcm_path}")

    if spec_var is not None:
        spec_var.to_frame("specific_var").to_parquet(srisk_path)
        log.info(f"  {srisk_path}")

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    log.info("")
    log.info("=" * 64)
    log.info(f"  Run complete  |  date: {run_date}")
    log.info(f"  Exposures  : {exposures.shape}")
    log.info(f"  FCM        : {'saved' if fcm is not None else 'pending (need more history)'}")
    log.info(f"  Spec risk  : {'saved' if spec_var is not None else 'pending (need more history)'}")
    log.info(f"  History    : {len(fr_hist)} day(s) accumulated")
    log.info("=" * 64)

    # Build dashboard HTML
    dash_out = Path(__file__).parent / "dashboard.html"
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable, "build_dashboard.py",
             "--output-dir", str(out_dir),
             "--out", str(dash_out)],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent),
        )
        if r.returncode == 0:
            log.info(r.stdout.strip())
        else:
            log.warning(f"Dashboard build failed: {r.stderr[:300]}")
    except Exception as exc:
        log.warning(f"Could not build dashboard: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
