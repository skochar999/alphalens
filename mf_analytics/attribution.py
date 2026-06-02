#!/usr/bin/env python3
"""
mf_analytics/attribution.py

Monthly return attribution for mutual fund schemes using IEC-1 factor returns.

Method
------
1.  Load daily IEC-1 factor returns → sum within each calendar month to get
    monthly factor returns (summation approximates compounding for short intervals).

2.  Build three grouped factor indices:
        MARKET   — the MARKET factor return (unchanged)
        STYLE    — equal-weighted mean of the 12 style factor returns
        INDUSTRY — equal-weighted mean of the 26 industry factor returns

3.  For each scheme, run a rolling OLS regression (window = --window months):
        R_fund,t = α + β_M · R_MARKET,t + β_S · R_STYLE,t + β_I · R_IND,t + ε_t
    Betas are estimated on the trailing window; applied to the same window.

4.  Monthly attribution:
        market_ret   = β_M × R_MARKET,t
        style_ret    = β_S × R_STYLE,t
        industry_ret = β_I × R_IND,t
        alpha        = actual_ret − market_ret − style_ret − industry_ret

5.  Rolling IR and tracking error for the scorer:
        IR  = (12 × mean(α_monthly)) / (√12 × std(α_monthly))
            = mean(α) / std(α) × √12

Outputs (under --mf-data):
    attribution.parquet  — long-format per scheme × month:
        scheme_code | scheme_name | amc | date | actual_ret |
        market_ret | style_ret | industry_ret | alpha |
        beta_M | beta_S | beta_I | intercept | r_squared

    fund_metrics.parquet — one row per scheme (trailing --window months):
        scheme_code | scheme_name | amc | alpha_ann | tracking_error_ann |
        info_ratio | total_return_ann | beta_M | beta_S | beta_I | r_squared

Usage
-----
    python mf_analytics/attribution.py
    python mf_analytics/attribution.py \\
        --mf-data ./mf_data \\
        --inec1-dir ./inec1_outputs \\
        --window 12
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
log = logging.getLogger("mf.attribution")

# Factor taxonomy — must match inec1/config.py
MARKET_FACTOR = "MARKET"
STYLE_FACTORS = [
    "BETA", "SIZE", "STREV", "LTMOM", "VALUE", "GROWTH",
    "LOWVOL", "LIQUIDITY", "LEVERAGE", "EARNYIELD", "EARNVAR", "PROFIT",
]
# Industry factors = everything that is not MARKET and not a style factor
# (auto-detected from column names at runtime)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IEC-1 mutual fund attribution",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--mf-data",   default="./mf_data",
                   help="Directory containing NAV and scheme parquet files")
    p.add_argument("--inec1-dir", default="./inec1_outputs",
                   help="Directory containing IEC-1 output parquet files")
    p.add_argument("--window",    type=int, default=12,
                   help="Rolling OLS window (months)")
    p.add_argument("--min-obs",   type=int, default=12,
                   help="Minimum observations required to estimate betas")
    p.add_argument("--force",     action="store_true",
                   help="Overwrite existing output files")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_factor_returns(inec1_dir: Path) -> pd.DataFrame:
    """
    Load daily factor returns history and resample to monthly by summing.
    Returns a DataFrame indexed by month-end dates, columns = factor names.
    """
    path = inec1_dir / "factor_returns_history.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"factor_returns_history.parquet not found in {inec1_dir}.\n"
            "Run backfill.py first."
        )
    daily = pd.read_parquet(path)
    # Ensure datetime index
    daily.index = pd.to_datetime(daily.index)
    daily = daily.sort_index()
    # Resample to monthly: sum daily factor returns within each month
    monthly = daily.resample("ME").sum()
    log.info(
        f"  Factor returns: {len(daily)} daily rows → "
        f"{len(monthly)} monthly rows  "
        f"({monthly.index[0].date()} → {monthly.index[-1].date()})"
    )
    return monthly


def load_nav_returns(mf_data: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load monthly NAV returns and scheme metadata.
    Returns (returns_df, scheme_df).
        returns_df: index=month-end dates, columns=scheme_code (int)
        scheme_df:  columns = scheme_code, scheme_name, amc
    """
    ret_path    = mf_data / "returns_monthly.parquet"
    scheme_path = mf_data / "scheme_list.parquet"

    if not ret_path.exists():
        raise FileNotFoundError(
            f"returns_monthly.parquet not found in {mf_data}.\n"
            "Run nav_fetcher.py first."
        )
    if not scheme_path.exists():
        raise FileNotFoundError(
            f"scheme_list.parquet not found in {mf_data}.\n"
            "Run nav_fetcher.py first."
        )

    returns  = pd.read_parquet(ret_path)
    schemes  = pd.read_parquet(scheme_path)
    returns.index = pd.to_datetime(returns.index)
    returns  = returns.sort_index()

    log.info(
        f"  NAV returns: {returns.shape[0]} months × "
        f"{returns.shape[1]} schemes  "
        f"({returns.index[0].date()} → {returns.index[-1].date()})"
    )
    log.info(f"  Schemes loaded: {len(schemes)}")
    return returns, schemes


def build_grouped_indices(factor_monthly: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse 39 factor returns into 3 grouped indices.

    Returns DataFrame with columns: [MARKET, STYLE, INDUSTRY]
    """
    cols = factor_monthly.columns.tolist()

    # Auto-detect industry factors = all remaining columns
    known = {MARKET_FACTOR} | set(STYLE_FACTORS)
    industry_factors = [c for c in cols if c not in known]

    log.info(
        f"  Factor groups — "
        f"MARKET: 1, "
        f"STYLE: {len([c for c in STYLE_FACTORS if c in cols])}, "
        f"INDUSTRY: {len(industry_factors)}"
    )

    style_present = [c for c in STYLE_FACTORS if c in cols]

    grouped = pd.DataFrame(index=factor_monthly.index)
    grouped["MARKET"]   = factor_monthly[MARKET_FACTOR]
    grouped["STYLE"]    = (
        factor_monthly[style_present].mean(axis=1)
        if style_present else 0.0
    )
    grouped["INDUSTRY"] = (
        factor_monthly[industry_factors].mean(axis=1)
        if industry_factors else 0.0
    )
    return grouped


# ---------------------------------------------------------------------------
# Rolling OLS
# ---------------------------------------------------------------------------

def _ols(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, float]:
    """
    Solve min ||y - X β||² via least squares.
    Returns (β, r_squared).
    X should include a column of ones for the intercept.
    """
    result = np.linalg.lstsq(X, y, rcond=None)
    beta   = result[0]
    y_hat  = X @ beta
    ss_res = np.sum((y - y_hat) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r2     = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return beta, float(r2)


def run_rolling_attribution(
    fund_returns: pd.Series,
    grouped: pd.DataFrame,
    window: int,
    min_obs: int,
) -> pd.DataFrame:
    """
    For a single fund, run rolling OLS and return monthly attribution rows.

    Parameters
    ----------
    fund_returns : pd.Series  (index = month-end dates)
    grouped      : pd.DataFrame  columns = [MARKET, STYLE, INDUSTRY]
    window       : rolling window length in months
    min_obs      : minimum observations to run regression

    Returns
    -------
    DataFrame with columns:
        date, actual_ret, market_ret, style_ret, industry_ret, alpha,
        beta_M, beta_S, beta_I, intercept, r_squared
    """
    # Align on common dates
    common = fund_returns.index.intersection(grouped.index)
    if len(common) < min_obs:
        return pd.DataFrame()

    y_all = fund_returns.reindex(common).values
    X_all = grouped.reindex(common).values   # shape: (T, 3)
    dates = common

    rows = []
    for end_i in range(min_obs - 1, len(dates)):
        start_i = max(0, end_i - window + 1)
        y_w = y_all[start_i : end_i + 1]
        X_w = X_all[start_i : end_i + 1]

        # Drop any NaN rows
        mask = ~(np.isnan(y_w) | np.any(np.isnan(X_w), axis=1))
        if mask.sum() < min_obs:
            continue

        y_clean = y_w[mask]
        X_clean = X_w[mask]
        X_intercept = np.column_stack([np.ones(len(X_clean)), X_clean])

        try:
            beta, r2 = _ols(y_clean, X_intercept)
        except np.linalg.LinAlgError:
            continue

        intercept, b_M, b_S, b_I = beta

        # Attribution for THIS month (end_i), using betas from rolling window
        y_t = y_all[end_i]
        x_t = X_all[end_i]

        if np.isnan(y_t) or np.any(np.isnan(x_t)):
            continue

        market_ret   = b_M * x_t[0]
        style_ret    = b_S * x_t[1]
        industry_ret = b_I * x_t[2]
        alpha        = y_t - market_ret - style_ret - industry_ret

        rows.append({
            "date":         dates[end_i],
            "actual_ret":   float(y_t),
            "market_ret":   float(market_ret),
            "style_ret":    float(style_ret),
            "industry_ret": float(industry_ret),
            "alpha":        float(alpha),
            "beta_M":       float(b_M),
            "beta_S":       float(b_S),
            "beta_I":       float(b_I),
            "intercept":    float(intercept),
            "r_squared":    float(r2),
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Fund-level metrics (for scoring)
# ---------------------------------------------------------------------------

def compute_fund_metrics(
    attr: pd.DataFrame,
    scheme_code: int,
    window: int,
) -> dict:
    """
    Compute trailing-window aggregate metrics from monthly attribution rows.
    Uses the last `window` months of data.

    Also computes "current positioning" metrics from the most recent 3 months,
    which reflect the fund's live factor exposures rather than historical returns.
    """
    tail = attr.tail(window)
    if len(tail) < 4:
        return {}

    alphas        = tail["alpha"].values
    actual_rets   = tail["actual_ret"].values
    market_rets   = tail["market_ret"].values
    style_rets    = tail["style_ret"].values
    industry_rets = tail["industry_ret"].values
    beta_M_vals   = tail["beta_M"].values
    beta_S_vals   = tail["beta_S"].values
    beta_I_vals   = tail["beta_I"].values

    # ---- Full-window annualised metrics ----
    alpha_monthly_mean  = float(np.mean(alphas))
    alpha_monthly_std   = float(np.std(alphas, ddof=1)) if len(alphas) > 1 else np.nan
    tracking_error_ann  = float(alpha_monthly_std * np.sqrt(12))
    alpha_ann           = float(alpha_monthly_mean * 12)

    info_ratio = (
        float(alpha_ann / tracking_error_ann)
        if tracking_error_ann > 1e-8 else np.nan
    )

    total_return_ann    = float(np.nanmean(actual_rets)   * 12)
    market_ret_ann      = float(np.nanmean(market_rets)   * 12)
    style_ret_ann       = float(np.nanmean(style_rets)    * 12)
    industry_ret_ann    = float(np.nanmean(industry_rets) * 12)
    # Active return = what the manager added BEYOND the market baseline
    # This is what investors actually pay active fees for
    active_ret_ann      = float(np.nanmean(actual_rets - market_rets) * 12)

    # ---- Current positioning: last 3 months of rolling betas ----
    # Betas are more persistent than factor returns; they reveal today's exposure
    # even when the factor return has been quiet.
    recent = attr.tail(3)
    recent_beta_M = float(recent["beta_M"].mean())
    recent_beta_S = float(recent["beta_S"].mean())
    recent_beta_I = float(recent["beta_I"].mean())

    # How much the current betas have drifted from the full-window average
    # High drift = manager changed positioning recently → less predictable
    beta_drift = (
        abs(recent_beta_S - float(np.mean(beta_S_vals))) +
        abs(recent_beta_I - float(np.mean(beta_I_vals)))
    )

    # Current active exposure (style + industry) — the non-market bets right now
    # Low = manager running close to market; high = taking style/sector bets
    current_active_exposure = abs(recent_beta_S) + abs(recent_beta_I)

    return {
        "scheme_code":             scheme_code,
        "alpha_ann":               alpha_ann,
        "tracking_error_ann":      tracking_error_ann,
        "info_ratio":              info_ratio,
        "total_return_ann":        total_return_ann,
        "market_ret_ann":          market_ret_ann,
        "style_ret_ann":           style_ret_ann,
        "industry_ret_ann":        industry_ret_ann,
        "active_ret_ann":          active_ret_ann,
        "beta_M":                  float(np.mean(beta_M_vals)),
        "beta_S":                  float(np.mean(beta_S_vals)),
        "beta_I":                  float(np.mean(beta_I_vals)),
        "recent_beta_M":           recent_beta_M,
        "recent_beta_S":           recent_beta_S,
        "recent_beta_I":           recent_beta_I,
        "beta_drift":              float(beta_drift),
        "current_active_exposure": float(current_active_exposure),
        "r_squared":               float(tail["r_squared"].mean()),
        "n_months":                len(tail),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()
    mf_data   = Path(args.mf_data)
    inec1_dir = Path(args.inec1_dir)

    attr_path    = mf_data / "attribution.parquet"
    metrics_path = mf_data / "fund_metrics.parquet"

    if not args.force and attr_path.exists() and metrics_path.exists():
        log.info("Attribution outputs already exist. Use --force to recompute.")
        return

    # ------------------------------------------------------------------ #
    # Load data
    # ------------------------------------------------------------------ #
    log.info("[1/4] Loading IEC-1 factor returns …")
    factor_monthly = load_factor_returns(inec1_dir)

    log.info("[2/4] Loading NAV returns …")
    nav_returns, schemes = load_nav_returns(mf_data)

    # Build grouped factor indices
    grouped = build_grouped_indices(factor_monthly)

    # ------------------------------------------------------------------ #
    # Run attribution for every scheme
    # ------------------------------------------------------------------ #
    log.info(f"[3/4] Running rolling {args.window}-month attribution …")

    all_attr_rows: list[pd.DataFrame] = []
    all_metrics:  list[dict]          = []
    n_ok = n_skip = 0

    scheme_meta = (
        schemes.set_index("scheme_code")[["scheme_name", "amc"]]
        if "scheme_code" in schemes.columns
        else pd.DataFrame()
    )

    scheme_codes = [
        int(c) for c in nav_returns.columns
        if not pd.isna(nav_returns[c]).all()
    ]

    for i, sc in enumerate(scheme_codes):
        fund_ret = nav_returns[sc].dropna()

        if len(fund_ret) < args.min_obs:
            n_skip += 1
            continue

        attr_df = run_rolling_attribution(
            fund_returns=fund_ret,
            grouped=grouped,
            window=args.window,
            min_obs=args.min_obs,
        )

        if attr_df.empty:
            n_skip += 1
            continue

        attr_df["scheme_code"] = sc
        # Attach name/amc if available
        if sc in scheme_meta.index:
            attr_df["scheme_name"] = scheme_meta.loc[sc, "scheme_name"]
            attr_df["amc"]         = scheme_meta.loc[sc, "amc"]
        else:
            attr_df["scheme_name"] = ""
            attr_df["amc"]         = ""

        all_attr_rows.append(attr_df)

        # Fund-level metrics
        metrics = compute_fund_metrics(attr_df, sc, args.window)
        if metrics:
            if sc in scheme_meta.index:
                metrics["scheme_name"] = scheme_meta.loc[sc, "scheme_name"]
                metrics["amc"]         = scheme_meta.loc[sc, "amc"]
            else:
                metrics["scheme_name"] = ""
                metrics["amc"]         = ""
            all_metrics.append(metrics)

        n_ok += 1
        if (i + 1) % 50 == 0:
            log.info(f"  … {i+1}/{len(scheme_codes)} schemes processed")

    log.info(f"  Done: {n_ok} schemes attributed, {n_skip} skipped")

    if not all_attr_rows:
        log.error("No attribution data produced — check that factor and NAV date ranges overlap.")
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # Save
    # ------------------------------------------------------------------ #
    log.info("[4/4] Saving outputs …")

    attr_full = pd.concat(all_attr_rows, ignore_index=True)
    # Reorder columns for clarity
    col_order = [
        "scheme_code", "scheme_name", "amc", "date",
        "actual_ret", "market_ret", "style_ret", "industry_ret", "alpha",
        "beta_M", "beta_S", "beta_I", "intercept", "r_squared",
    ]
    attr_full = attr_full[[c for c in col_order if c in attr_full.columns]]
    attr_full.to_parquet(attr_path, index=False)
    log.info(f"  attribution.parquet   → {attr_path}  ({len(attr_full):,} rows)")

    metrics_df = pd.DataFrame(all_metrics)
    metrics_df.to_parquet(metrics_path, index=False)
    log.info(f"  fund_metrics.parquet  → {metrics_path}  ({len(metrics_df)} funds)")

    # Quick sanity print
    if not metrics_df.empty:
        valid_ir = metrics_df["info_ratio"].dropna()
        log.info(
            f"\n  IR distribution (n={len(valid_ir)}): "
            f"p10={valid_ir.quantile(0.1):.2f}  "
            f"median={valid_ir.median():.2f}  "
            f"p90={valid_ir.quantile(0.9):.2f}"
        )
        top5 = metrics_df.nlargest(5, "info_ratio")[["scheme_name", "amc", "info_ratio", "alpha_ann"]]
        log.info(f"\n  Top 5 by IR:\n{top5.to_string(index=False)}")

    log.info("\nAttribution complete.")


if __name__ == "__main__":
    main()
