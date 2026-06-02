#!/usr/bin/env python3
"""
holdings_attribution.py
=======================
Brinson-style holdings-based attribution for IEC-1 FundLens.

For each fund × month where holdings data is available, decomposes the fund's
actual return into:

    fund_return  =  market_attr  +  style_attr  +  industry_attr  +  stock_selection

where:
    portfolio_exposure_k  =  Σ_i  (w_i × B_i_k)          [weighted avg factor loading]
    factor_attribution_k  =  portfolio_exposure_k × F_k   [k-th factor contribution]
    stock_selection       =  fund_return  –  Σ_k factor_attribution_k

This is TRUE stock selection alpha: what the manager earns above and beyond what
any passive factor tilt would have earned. It is calculated without needing
individual stock returns — only weights, factor loadings, and monthly factor returns.

Coverage note
─────────────
Not all holdings can be matched to factor exposures (debt, foreign, unlisted stocks).
We track `equity_coverage_pct`: the fraction of the portfolio's equity weight that
is matched. For equity funds this is typically 85–99%. The unmatched portion is
implicitly included in stock_selection (it gets 0 factor attribution, which biases
stock_selection slightly but is the honest approach).

Outputs
───────
  mf_data/holdings_attribution.parquet   — detailed per-fund per-month results
  mf_data/holdings_alpha.parquet         — per-fund summary (alpha, IR, consistency)

Usage
─────
    python holdings_attribution.py
    python holdings_attribution.py --mf-data ./mf_data --min-coverage 0.5
    python holdings_attribution.py --debug
"""

from __future__ import annotations

import argparse
import glob
import logging
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("holdings_attr")

# ─────────────────────────────────────────────────────────────────────────────
# Factor group definitions (IEC-1)
# ─────────────────────────────────────────────────────────────────────────────

MARKET_FACTORS = ["MARKET"]

STYLE_FACTORS = [
    "BETA", "SIZE", "STREV", "LTMOM", "VALUE", "GROWTH",
    "LOWVOL", "LIQUIDITY", "LEVERAGE", "EARNYIELD", "EARNVAR", "PROFIT",
]

INDUSTRY_FACTORS = [
    "AERODEF", "AUTOMOBL", "AUTOCOMP", "BANKS", "BUSINSERV", "CAPGOODS",
    "CHEMICALS", "CONSUMDISC", "CONSUMDUR", "CONSUMSTAP", "FINLSERV",
    "HEALTHCARE", "ITSERVIC", "MATERIALS", "METMINING", "OILGAS", "PHARMA",
    "POWERGEN", "REALESTATE", "SOFTWARE", "TECHCOMP", "TELECOM", "TRADDIST",
    "TRANSPORT", "UTILITIES", "MISCELLAN",
]

ALL_FACTORS = MARKET_FACTORS + STYLE_FACTORS + INDUSTRY_FACTORS  # 39 total


# ─────────────────────────────────────────────────────────────────────────────
# Data loaders
# ─────────────────────────────────────────────────────────────────────────────

def load_factor_returns_monthly(mf_data: Path) -> pd.DataFrame:
    """
    Load daily IEC-1 factor returns and compound to monthly.
    Returns DataFrame indexed by month-end dates (datetime), columns = 39 factors.
    """
    path = mf_data.parent / "inec1_outputs" / "factor_returns_history.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Factor returns not found: {path}")

    frh = pd.read_parquet(path)
    frh.index = pd.to_datetime(frh.index)
    frh = frh.sort_index()

    # Compound daily → monthly: (1 + r_daily).resample("ME").prod() - 1
    monthly = (1 + frh).resample("ME").prod() - 1
    log.info(f"Factor returns: {len(monthly)} months "
             f"({monthly.index[0].strftime('%Y-%m')} – {monthly.index[-1].strftime('%Y-%m')})")
    return monthly


def load_exposures(mf_data: Path) -> pd.DataFrame:
    """
    Load the most recent IEC-1 stock exposure matrix.
    Returns DataFrame indexed by NSE ticker, columns = 39 factors.
    """
    pattern = str(mf_data.parent / "inec1_outputs" / "exposures_*.parquet")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No exposure files found matching: {pattern}")
    path = files[-1]  # most recent
    exp = pd.read_parquet(path)
    # Keep only the 39 IEC-1 factors we need
    available = [f for f in ALL_FACTORS if f in exp.columns]
    exp = exp[available]
    log.info(f"Exposures: {len(exp)} stocks × {len(available)} factors  [{Path(path).name}]")
    return exp


def load_isin_ticker_map(mf_data: Path) -> pd.Series:
    """Returns a Series: isin → ticker."""
    path = mf_data / "isin_ticker_map.parquet"
    if not path.exists():
        raise FileNotFoundError(f"ISIN–ticker map not found: {path}")
    df = pd.read_parquet(path)
    return df.set_index("isin")["ticker"]


def load_nav_returns_monthly(mf_data: Path) -> pd.DataFrame:
    """
    Load NAV monthly prices and compute month-over-month returns.
    Returns DataFrame indexed by month-end datetime, columns = scheme_code (int).
    """
    path = mf_data / "nav_monthly.parquet"
    nav = pd.read_parquet(path)
    nav.index = pd.to_datetime(nav.index)
    nav = nav.sort_index()
    # Convert columns to int
    nav.columns = nav.columns.astype(int)
    ret = nav.pct_change(fill_method=None).dropna(how="all")
    log.info(f"NAV returns: {len(ret)} months, {ret.shape[1]} funds")
    return ret


def load_holdings_months(mf_data: Path) -> list[tuple[str, pd.DataFrame]]:
    """Load all cached holdings parquets. Returns list of (month_str, df)."""
    hold_dir = mf_data / "holdings"
    files = sorted(hold_dir.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(
            f"No holdings cached at {hold_dir}. "
            "Run backfill_holdings.py first on your Mac."
        )
    result = []
    for f in files:
        df = pd.read_parquet(f)
        df["scheme_code"] = pd.to_numeric(df["scheme_code"], errors="coerce").astype("Int64")
        result.append((f.stem, df))
    log.info(f"Holdings cache: {len(result)} months "
             f"({result[0][0]} – {result[-1][0]})")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Core attribution computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_portfolio_exposures(
    holdings_df: pd.DataFrame,
    isin_to_ticker: pd.Series,
    exposures: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    For a single month's holdings DataFrame, compute per-fund factor exposures
    and equity coverage statistics.

    Returns:
        port_exp   : DataFrame (scheme_code × factors) — portfolio-level exposures
        coverage   : DataFrame (scheme_code) with equity_weight, mapped_weight, coverage_pct
    """
    # Map ISINs to tickers
    h = holdings_df.copy()
    h["ticker"] = h["isin"].map(isin_to_ticker)
    h["is_equity"] = h["ticker"].notna()

    # Merge with exposures
    h_eq = h[h["is_equity"]].copy()
    h_eq = h_eq.join(exposures, on="ticker", how="inner")

    if h_eq.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Per-fund computations
    port_exposures = {}
    coverage_stats = {}

    for sc, grp in h.groupby("scheme_code"):
        if pd.isna(sc):
            continue
        sc = int(sc)

        total_pct = grp["pct_nav"].sum()
        eq_grp = grp[grp["is_equity"]]
        eq_weight = eq_grp["pct_nav"].sum()

        # Get matched (equity + has exposure)
        matched = h_eq[h_eq["scheme_code"] == grp["scheme_code"].iloc[0]]
        matched_weight = matched["pct_nav"].sum()

        if matched.empty or matched_weight < 0.5:
            coverage_stats[sc] = {
                "total_pct_nav": total_pct,
                "equity_weight": eq_weight,
                "mapped_weight": matched_weight,
                "coverage_pct": 0.0,
                "n_stocks": 0,
            }
            continue

        coverage_pct = matched_weight / total_pct if total_pct > 0 else 0.0

        # Portfolio exposure: Σ (w_i / 100) × B_i_k
        # w_i is pct_nav (0–100), divide by 100 to get fractional weight
        factor_cols = [f for f in ALL_FACTORS if f in matched.columns]
        weights = matched["pct_nav"].values / 100.0  # fractional weights
        exp_matrix = matched[factor_cols].values     # (n_stocks × n_factors)

        port_exp_vec = weights @ exp_matrix          # (n_factors,)

        port_exposures[sc] = dict(zip(factor_cols, port_exp_vec))
        coverage_stats[sc] = {
            "total_pct_nav": total_pct,
            "equity_weight": eq_weight,
            "mapped_weight": matched_weight,
            "coverage_pct": coverage_pct,
            "n_stocks": len(matched),
        }

    port_exp_df = pd.DataFrame(port_exposures).T  # (scheme_code × factors)
    port_exp_df.index.name = "scheme_code"

    coverage_df = pd.DataFrame(coverage_stats).T
    coverage_df.index.name = "scheme_code"

    return port_exp_df, coverage_df


def run_attribution(
    mf_data: Path,
    min_coverage: float = 0.30,
) -> pd.DataFrame:
    """
    Main attribution loop.

    For each holdings month × fund:
      1. Compute portfolio factor exposures from holdings
      2. Multiply by monthly factor returns → factor attributions
      3. Residual vs actual fund return → stock selection

    Returns detailed DataFrame with one row per (scheme_code, month).
    """
    # Load all inputs
    factor_ret_m = load_factor_returns_monthly(mf_data)
    exposures    = load_exposures(mf_data)
    isin_to_tick = load_isin_ticker_map(mf_data)
    nav_ret      = load_nav_returns_monthly(mf_data)
    holdings_all = load_holdings_months(mf_data)

    all_records = []

    for month_str, hold_df in holdings_all:
        log.info(f"\n{'─'*60}")
        log.info(f"  Processing month: {month_str}")

        # Match month to factor returns (month-end date)
        month_end = pd.to_datetime(month_str + "-01") + pd.offsets.MonthEnd(0)
        if month_end not in factor_ret_m.index:
            log.warning(f"  No factor returns for {month_str} — skipping")
            continue

        fr_row = factor_ret_m.loc[month_end]  # Series: factor → return

        # Compute portfolio exposures for this month
        port_exp, coverage = compute_portfolio_exposures(hold_df, isin_to_tick, exposures)

        if port_exp.empty:
            log.warning(f"  No exposures computed for {month_str}")
            continue

        log.info(f"  Computed exposures for {len(port_exp)} funds")

        for sc in port_exp.index:
            sc_int = int(sc)
            cov = coverage.loc[sc] if sc in coverage.index else None
            cov_pct = float(cov["coverage_pct"]) if cov is not None else 0.0

            if cov_pct < min_coverage:
                log.debug(f"    {sc_int}: coverage {cov_pct:.0%} < {min_coverage:.0%} — skipping")
                continue

            # Factor attributions: exposure × factor return (vectorized, NaN-safe)
            exp_row = port_exp.loc[sc]

            # Build aligned vectors for all 39 factors, filling missing with 0
            exp_vec = np.array([float(exp_row.get(f, 0) or 0) if f in exp_row.index else 0.0
                                for f in ALL_FACTORS])
            fr_vec  = np.array([float(fr_row.get(f, 0)  or 0) if f in fr_row.index  else 0.0
                                for f in ALL_FACTORS])
            # Replace any remaining NaN with 0 before multiplication
            exp_vec = np.where(np.isfinite(exp_vec), exp_vec, 0.0)
            fr_vec  = np.where(np.isfinite(fr_vec),  fr_vec,  0.0)

            attr_vec = exp_vec * fr_vec  # element-wise: exposure × factor return

            n_mkt = len(MARKET_FACTORS)
            n_sty = len(STYLE_FACTORS)
            market_attr   = float(attr_vec[:n_mkt].sum())
            style_attr    = float(attr_vec[n_mkt:n_mkt + n_sty].sum())
            industry_attr = float(attr_vec[n_mkt + n_sty:].sum())
            total_factor_attr = market_attr + style_attr + industry_attr

            # Actual fund return
            fund_ret = np.nan
            if sc_int in nav_ret.columns and month_end in nav_ret.index:
                fund_ret = float(nav_ret.loc[month_end, sc_int])

            # Stock selection = actual - factor model prediction
            stock_selection = (fund_ret - total_factor_attr) if np.isfinite(fund_ret) else np.nan

            # Individual factor exposures for output
            factor_exp_dict = {f"exp_{f}": exp_vec[i] for i, f in enumerate(ALL_FACTORS)}

            record = {
                "scheme_code":     sc_int,
                "month":           month_str,
                "month_end":       month_end,
                "fund_return":     fund_ret,
                "market_attr":     market_attr,
                "style_attr":      style_attr,
                "industry_attr":   industry_attr,
                "total_factor_attr": total_factor_attr,
                "stock_selection": stock_selection,
                "equity_coverage_pct": cov_pct,
                "n_mapped_stocks": int(cov["n_stocks"]) if cov is not None else 0,
                "equity_weight":   float(cov["equity_weight"]) if cov is not None else 0.0,
                "mapped_weight":   float(cov["mapped_weight"]) if cov is not None else 0.0,
                **factor_exp_dict,
            }
            all_records.append(record)

    if not all_records:
        log.error("No attribution records produced. Run backfill_holdings.py first.")
        return pd.DataFrame()

    results = pd.DataFrame(all_records)
    results = results.sort_values(["scheme_code", "month"]).reset_index(drop=True)
    log.info(f"\n{'='*60}")
    log.info(f"Attribution: {len(results)} fund-month records")
    log.info(f"  Funds: {results['scheme_code'].nunique()}")
    log.info(f"  Months: {sorted(results['month'].unique())}")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Per-fund summary metrics
# ─────────────────────────────────────────────────────────────────────────────

def compute_fund_alpha_summary(
    attr_df: pd.DataFrame,
    min_months: int = 3,
) -> pd.DataFrame:
    """
    Compute per-fund summary from month-level attribution records.

    Metrics:
      holdings_alpha_ann   : annualized stock selection (geometric mean)
      holdings_ir          : information ratio of monthly stock selection
      holdings_n_months    : number of months with attribution data
      holdings_avg_coverage: average equity coverage pct across months
      avg_market_attr      : average monthly market attribution
      avg_style_attr       : average monthly style attribution
      avg_industry_attr    : average monthly industry attribution
      style_drift          : std dev of aggregate style exposure across months
                             (low = consistent style; high = style drift)
      avg_exp_{factor}     : time-average exposure to each factor
    """
    records = []
    factor_exp_cols = [f"exp_{f}" for f in ALL_FACTORS]

    for sc, grp in attr_df.groupby("scheme_code"):
        grp = grp.sort_values("month")
        n = len(grp)

        if n < min_months:
            continue

        ss = grp["stock_selection"].dropna()
        n_valid = len(ss)

        if n_valid < min_months:
            continue

        # Annualized alpha (geometric compounding of monthly stock selection)
        alpha_ann = (1 + ss).prod() ** (12 / n_valid) - 1

        # Information ratio: annualized mean / annualized std
        ir = (ss.mean() / ss.std() * np.sqrt(12)) if ss.std() > 1e-8 else np.nan

        # Style drift: std of the sum of all style factor exposures across months
        style_exp_cols = [f"exp_{f}" for f in STYLE_FACTORS if f"exp_{f}" in grp.columns]
        if style_exp_cols:
            agg_style = grp[style_exp_cols].sum(axis=1)
            style_drift = float(agg_style.std())
        else:
            style_drift = np.nan

        # Average factor exposures
        avg_exp = {
            col: float(grp[col].mean()) if col in grp.columns else np.nan
            for col in factor_exp_cols
        }

        record = {
            "scheme_code":          int(sc),
            "holdings_alpha_ann":   float(alpha_ann),
            "holdings_ir":          float(ir) if not np.isnan(ir) else np.nan,
            "holdings_n_months":    n_valid,
            "holdings_avg_coverage": float(grp["equity_coverage_pct"].mean()),
            "holdings_alpha_monthly_mean": float(ss.mean()),
            "holdings_alpha_monthly_std":  float(ss.std()),
            "avg_market_attr":      float(grp["market_attr"].mean()),
            "avg_style_attr":       float(grp["style_attr"].mean()),
            "avg_industry_attr":    float(grp["industry_attr"].mean()),
            "style_drift":          style_drift,
            **avg_exp,
        }
        records.append(record)

    if not records:
        log.warning(f"No funds had ≥{min_months} months of attribution data yet. "
                    "Run backfill_holdings.py on your Mac to build more history.")
        return pd.DataFrame()
    summary = pd.DataFrame(records).sort_values("holdings_alpha_ann", ascending=False)
    summary = summary.reset_index(drop=True)
    log.info(f"Alpha summary: {len(summary)} funds with ≥{min_months} months of data")
    if len(summary):
        log.info(f"  Alpha range: {summary['holdings_alpha_ann'].min():.1%} – "
                 f"{summary['holdings_alpha_ann'].max():.1%}")
        log.info(f"  IR range:    {summary['holdings_ir'].min():.2f} – "
                 f"{summary['holdings_ir'].max():.2f}")
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Validation — sanity-check the attribution math
# ─────────────────────────────────────────────────────────────────────────────

def validate_attribution(attr_df: pd.DataFrame) -> None:
    """
    Print sanity checks on the attribution results.
    """
    log.info("\n=== Attribution Validation ===")

    # 1. Check: factor attribution columns should not blow up
    for col in ["market_attr", "style_attr", "industry_attr", "stock_selection", "fund_return"]:
        if col not in attr_df.columns:
            continue
        vals = attr_df[col].dropna()
        log.info(f"  {col:25s}: mean={vals.mean():+.3f}  std={vals.std():.3f}  "
                 f"min={vals.min():+.3f}  max={vals.max():+.3f}")

    # 2. Check: market_attr + style_attr + industry_attr + stock_selection ≈ fund_return
    reconstruct = (attr_df["market_attr"] + attr_df["style_attr"] +
                   attr_df["industry_attr"] + attr_df["stock_selection"])
    residual = (reconstruct - attr_df["fund_return"]).dropna()
    log.info(f"  Reconstruction error (should be ~0): "
             f"mean={residual.mean():+.2e}  max_abs={residual.abs().max():.2e}")

    # 3. Check: coverage distribution
    cov = attr_df["equity_coverage_pct"]
    log.info(f"  Coverage: mean={cov.mean():.1%}  min={cov.min():.1%}  "
             f"p25={cov.quantile(0.25):.1%}  median={cov.quantile(0.5):.1%}")

    # 4. Warning if many fund-months have no actual return (NAV gap)
    missing_ret = attr_df["fund_return"].isna().sum()
    log.info(f"  Fund-months missing NAV return: {missing_ret} / {len(attr_df)}")

    # 5. Stock selection distribution — flag if suspiciously large
    ss = attr_df["stock_selection"].dropna()
    if ss.abs().max() > 0.30:
        log.warning(f"  ⚠ Stock selection has |values| > 30% — check coverage or NAV alignment")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Brinson holdings-based attribution for IEC-1 FundLens")
    p.add_argument("--mf-data",      default="./mf_data",
                   help="Path to mf_data directory (default: ./mf_data)")
    p.add_argument("--min-coverage", type=float, default=0.30,
                   help="Min equity coverage fraction to include fund-month (default: 0.30)")
    p.add_argument("--min-months",   type=int, default=3,
                   help="Min months for per-fund alpha summary (default: 3)")
    p.add_argument("--debug",        action="store_true")
    args = p.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    mf_data = Path(args.mf_data)

    # ── Step 1: Run attribution ────────────────────────────────────────────
    log.info("Step 1: Computing Brinson attribution …")
    attr_df = run_attribution(mf_data, min_coverage=args.min_coverage)

    if attr_df.empty:
        log.error("No results. Ensure backfill_holdings.py has been run on your Mac.")
        return

    # ── Step 2: Validate ──────────────────────────────────────────────────
    validate_attribution(attr_df)

    # ── Step 3: Save detailed results ────────────────────────────────────
    out_detail = mf_data / "holdings_attribution.parquet"
    attr_df.to_parquet(out_detail, index=False)
    log.info(f"\nSaved: {out_detail}  ({len(attr_df)} rows)")

    # ── Step 4: Per-fund alpha summary ───────────────────────────────────
    log.info("\nStep 4: Computing per-fund alpha summaries …")
    summary = compute_fund_alpha_summary(attr_df, min_months=args.min_months)

    out_summary = mf_data / "holdings_alpha.parquet"
    summary.to_parquet(out_summary, index=False)
    log.info(f"Saved: {out_summary}  ({len(summary)} funds)")

    # ── Step 5: Print top/bottom alpha funds ────────────────────────────
    if summary.empty:
        log.info("\nInsufficient history for fund alpha rankings yet.")
        log.info("After running backfill_holdings.py, re-run this script for full results.")
    elif len(summary) > 0:
        # Join with scheme names
        try:
            fm = pd.read_parquet(mf_data / "fund_metrics.parquet")[
                ["scheme_code", "scheme_name", "amc"]
            ]
            summary = summary.merge(fm, on="scheme_code", how="left")
        except Exception:
            pass

        print("\n" + "=" * 80)
        print("TOP 10 FUNDS BY HOLDINGS-BASED ALPHA (annualized stock selection)")
        print("=" * 80)
        top10 = summary.head(10)[["scheme_code", "scheme_name", "amc",
                                   "holdings_alpha_ann", "holdings_ir",
                                   "holdings_n_months", "holdings_avg_coverage"]].copy()
        top10["alpha"] = top10["holdings_alpha_ann"].map(lambda x: f"{x:+.2%}")
        top10["IR"]    = top10["holdings_ir"].map(lambda x: f"{x:+.2f}" if not pd.isna(x) else "—")
        top10["cov"]   = top10["holdings_avg_coverage"].map(lambda x: f"{x:.0%}")
        top10["mo"]    = top10["holdings_n_months"].astype(str)
        for _, r in top10.iterrows():
            nm = str(r.get("scheme_name", r["scheme_code"]))[:55]
            print(f"  {nm:55s}  alpha={r['alpha']}  IR={r['IR']}  coverage={r['cov']}  months={r['mo']}")

        print("\nBOTTOM 5 FUNDS BY HOLDINGS-BASED ALPHA")
        print("-" * 60)
        bot5 = summary.tail(5)[["scheme_code", "scheme_name",
                                  "holdings_alpha_ann", "holdings_ir",
                                  "holdings_n_months"]].copy()
        for _, r in bot5.iterrows():
            nm = str(r.get("scheme_name", r["scheme_code"]))[:55]
            alpha = f"{r['holdings_alpha_ann']:+.2%}"
            ir    = f"{r['holdings_ir']:+.2f}" if not pd.isna(r["holdings_ir"]) else "—"
            print(f"  {nm:55s}  alpha={alpha}  IR={ir}")

    log.info("\nDone.")


if __name__ == "__main__":
    main()
