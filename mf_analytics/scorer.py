#!/usr/bin/env python3
"""
mf_analytics/scorer.py

Composite 0–100 outperformance score for mutual fund schemes.

Score = 50 × Alpha_Quality + 30 × Alpha_Proportion + 20 × Style_Stability
        (each sub-score is a percentile rank among all scored funds, 0–1)

Sub-scores
----------
Alpha_Quality (50 pts)
    Information Ratio percentile: IR = alpha_ann / tracking_error_ann
    Higher IR → more consistent, risk-adjusted alpha

Alpha_Proportion (30 pts)
    What fraction of the fund's gross positive return comes from alpha.
    proportion = max(alpha_ann, 0) / max(total_return_ann, epsilon)
    A fund with the same total return but more alpha is scored higher.

Style_Stability (20 pts)
    Consistency of factor betas over time.
    For each fund, compute the rolling 12-month beta_M, beta_S, beta_I series.
    Stability = 1 − average CV(beta) across the 3 betas.
    CV = std / (|mean| + epsilon) to handle near-zero means.
    Lower CV → more stable style → higher stability score.

Backtest (optional)
-------------------
Rolling forward return analysis: for each historical scoring date, compute
the score and check the fund's forward 1/3/6/12-month return.
This validates whether the score predicts outperformance.

Outputs (under --mf-data)
    scores.parquet      — one row per scheme (current score):
        scheme_code | scheme_name | amc | score | alpha_quality |
        alpha_proportion | style_stability | ir | alpha_ann |
        tracking_error_ann | total_return_ann | beta_M | beta_S | beta_I

    backtest.parquet    — one row per scheme × historical date:
        scheme_code | score_date | score | fwd_1m | fwd_3m | fwd_6m | fwd_12m

Usage
-----
    python mf_analytics/scorer.py
    python mf_analytics/scorer.py --mf-data ./mf_data --backtest
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
log = logging.getLogger("mf.scorer")

EPSILON = 1e-6   # prevent division by zero


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IEC-1 mutual fund outperformance scorer",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--mf-data",  default="./mf_data")
    p.add_argument("--window",   type=int, default=12,
                   help="Trailing months for rolling metrics")
    p.add_argument("--backtest", action="store_true",
                   help="Run rolling historical backtest")
    p.add_argument("--force",    action="store_true",
                   help="Overwrite existing output files")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Percentile rank helper
# ---------------------------------------------------------------------------

def _pct_rank(series: pd.Series) -> pd.Series:
    """Percentile rank within the series, 0–1. NaN is propagated."""
    return series.rank(pct=True, na_option="keep")


# ---------------------------------------------------------------------------
# Style stability from attribution detail
# ---------------------------------------------------------------------------

def compute_style_stability(
    attr: pd.DataFrame,
    scheme_code: int,
    window: int,
) -> float:
    """
    For a single fund, compute style stability over trailing `window` months.
    Returns a value in [0, 1] — higher = more stable betas.
    """
    fund_attr = attr[attr["scheme_code"] == scheme_code].tail(window)
    if len(fund_attr) < 4:
        return np.nan

    stab = 0.0
    count = 0
    for col in ["beta_M", "beta_S", "beta_I"]:
        if col not in fund_attr.columns:
            continue
        vals = fund_attr[col].dropna().values
        if len(vals) < 2:
            continue
        mean_abs = np.abs(vals.mean()) + EPSILON
        cv = vals.std(ddof=1) / mean_abs
        stab += cv
        count += 1

    if count == 0:
        return np.nan

    avg_cv = stab / count
    # Map to [0, 1]: CV of 0 → 1.0, CV of 2+ → 0.0
    stability = max(0.0, 1.0 - avg_cv / 2.0)
    return float(stability)


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------

def compute_scores(
    metrics: pd.DataFrame,
    attr: pd.DataFrame,
    window: int,
) -> pd.DataFrame:
    """
    Compute composite scores for all funds.

    Four components, each percentile-ranked across all funds:

    1. Skill Consistency  (35 pts) — IR: how steadily does the manager generate alpha?
    2. Skill Share        (25 pts) — alpha / |active_return|: of the manager's ACTIVE
                                     decisions (style + industry + alpha, beyond market
                                     baseline), what fraction is genuine stock picking?
    3. Style Discipline   (20 pts) — rolling beta consistency over time
    4. Positioning Fit    (20 pts) — is today's portfolio risk justified by historical
                                     stock-picking strength? Funds with high alpha but
                                     low current style/sector loading score best; funds
                                     taking large style bets without corresponding alpha
                                     history are penalised.

    Parameters
    ----------
    metrics : fund_metrics.parquet content (one row per fund)
    attr    : attribution.parquet content (long-format)
    window  : trailing months for stability calc
    """
    df = metrics.copy()

    # ------------------------------------------------------------------ #
    # Sub-score 1: Skill Consistency — percentile rank of IR
    # ------------------------------------------------------------------ #
    df["alpha_quality_raw"] = df["info_ratio"]
    df["alpha_quality"]     = _pct_rank(df["alpha_quality_raw"])

    # ------------------------------------------------------------------ #
    # Sub-score 2: Skill Share of Active Decisions
    # ------------------------------------------------------------------ #
    # All equity funds have market exposure — that's not a skill, it's the product.
    # The active decisions are: style tilts + sector bets + stock picking (alpha).
    # We measure what fraction of those active decisions is genuine alpha.
    # Denominator = |active_return| so negative active return funds don't get
    # artificially high proportions.
    active_ret  = df["active_ret_ann"] if "active_ret_ann" in df.columns else (
        df["total_return_ann"] - df.get("market_ret_ann", df["total_return_ann"] * 0.0)
    )
    pos_alpha   = df["alpha_ann"].clip(lower=0.0)
    denom       = active_ret.abs().clip(lower=EPSILON)
    df["alpha_proportion_raw"] = pos_alpha / denom
    df["alpha_proportion"]     = _pct_rank(df["alpha_proportion_raw"])

    # ------------------------------------------------------------------ #
    # Sub-score 3: Style Discipline — rolling beta consistency
    # ------------------------------------------------------------------ #
    log.info("  Computing style stability …")
    stability_map = {}
    for sc in df["scheme_code"].unique():
        stability_map[int(sc)] = compute_style_stability(attr, int(sc), window)

    df["style_stability_raw"] = df["scheme_code"].map(stability_map)
    df["style_stability"]     = _pct_rank(df["style_stability_raw"])

    # ------------------------------------------------------------------ #
    # Sub-score 4: Positioning Fit — current bets justified by alpha history?
    # ------------------------------------------------------------------ #
    # Prefer holdings-based active exposure (from fetch_holdings_and_score.py)
    # over regression-estimated current_active_exposure when available.
    #
    # holdings_active_exposure  — computed from actual portfolio weights ×
    #   IEC-1 stock factor exposures (precise, from latest AMFI disclosure)
    # current_active_exposure   — estimated from rolling OLS betas (fallback)
    #
    # positioning_raw = alpha_ann / (active_exposure + ε)
    #   → High alpha + low current bets → well-positioned
    #   → Low alpha  + high current bets → poor fit
    #
    # We also penalise recent beta drift (manager changed positioning recently).
    if "holdings_active_exposure" in df.columns and df["holdings_active_exposure"].notna().sum() > 10:
        # Use proper holdings-based exposure where coverage >= 30%
        good_coverage = (
            df.get("holdings_coverage_pct", pd.Series(0.0, index=df.index)).fillna(0) >= 30
        )
        active_exp = df["current_active_exposure"].copy()
        active_exp[good_coverage] = df.loc[good_coverage, "holdings_active_exposure"]
        n_holdings = int(good_coverage.sum())
        log.info(f"  Positioning: using holdings-based exposure for {n_holdings} funds, "
                 f"regression fallback for {len(df) - n_holdings}")
    elif "current_active_exposure" in df.columns:
        active_exp = df["current_active_exposure"]
    else:
        active_exp = pd.Series(EPSILON, index=df.index)

    pos_raw       = df["alpha_ann"] / (active_exp + EPSILON)
    drift         = df.get("beta_drift", pd.Series(0.0, index=df.index)).fillna(0.0)
    drift_penalty = (drift / 2.0).clip(upper=1.0)
    df["positioning_raw"] = pos_raw * (1.0 - drift_penalty)

    df["positioning"] = _pct_rank(df["positioning_raw"])

    # ------------------------------------------------------------------ #
    # Composite score (0–100)
    # ------------------------------------------------------------------ #
    # Weights tuned via holdings backtest (Jan 2024–Apr 2026, 28 scoring months):
    #   IR signal:              IC = +0.121 @ 6m  (p=0.001) → 45 pts
    #   Alpha proportion:       IC = +0.078 @ 6m  (p=0.028) → 30 pts
    # Weights re-validated on 1,698 fund-months (8 AMCs, Jan 2024–Apr 2026):
    #   IR (alpha_quality):     IC=+0.101 @ 6m (p<0.001)  IC²∝ → 55 pts
    #   Alpha (alpha_proportion): IC=+0.045 @ 6m (p=0.063) IC²∝ → 20 pts
    #   Positioning:            alpha_ann-based proxy        →  15 pts
    #   Style discipline:       IC=−0.039 @ 6m (p=0.106)   →  10 pts
    #                           (not predictive, kept as tiebreaker/filter)
    df["score"] = (
        55.0 * df["alpha_quality"].fillna(0.0)
      + 20.0 * df["alpha_proportion"].fillna(0.0)
      + 10.0 * df["style_stability"].fillna(0.0)
      + 15.0 * df["positioning"].fillna(0.0)
    )

    return df


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def run_backtest(
    attr: pd.DataFrame,
    metrics_template: pd.DataFrame,
    nav_returns: pd.DataFrame,
    window: int,
) -> pd.DataFrame:
    """
    Rolling backtest: at each historical month, compute the score using data
    up to that month, then record forward 1/3/6/12-month realised returns.

    Returns long-format DataFrame: scheme_code × score_date.
    """
    log.info("  Running backtest …")

    # Get all unique dates where we have attribution
    all_dates = sorted(attr["date"].unique())

    # Need at least `window` months of history before scoring
    score_dates = all_dates[window:]

    bt_rows: list[dict] = []

    for i, score_date in enumerate(score_dates):
        # Attribution data up to this date
        attr_window = attr[attr["date"] <= score_date]

        # Build metrics from this window
        schemes_in_window = attr_window["scheme_code"].unique()
        mini_metrics_rows = []

        for sc in schemes_in_window:
            sc_attr = attr_window[attr_window["scheme_code"] == sc]
            tail    = sc_attr.tail(window)
            if len(tail) < 4:
                continue

            alphas    = tail["alpha"].values
            rets      = tail["actual_ret"].values
            alpha_mean = float(np.nanmean(alphas))
            alpha_std  = float(np.nanstd(alphas, ddof=1)) if len(alphas) > 1 else np.nan
            te_ann     = float(alpha_std * np.sqrt(12)) if not np.isnan(alpha_std) else np.nan
            alpha_ann  = float(alpha_mean * 12)
            ir         = float(alpha_ann / te_ann) if (te_ann and te_ann > EPSILON) else np.nan
            total_ret  = float(np.nanmean(rets) * 12)

            mini_metrics_rows.append({
                "scheme_code":        sc,
                "scheme_name":        sc_attr["scheme_name"].iloc[-1] if "scheme_name" in sc_attr else "",
                "amc":                sc_attr["amc"].iloc[-1] if "amc" in sc_attr else "",
                "alpha_ann":          alpha_ann,
                "tracking_error_ann": te_ann,
                "info_ratio":         ir,
                "total_return_ann":   total_ret,
                "beta_M":             float(tail["beta_M"].mean()),
                "beta_S":             float(tail["beta_S"].mean()),
                "beta_I":             float(tail["beta_I"].mean()),
                "r_squared":          float(tail["r_squared"].mean()),
                "n_months":           len(tail),
            })

        if not mini_metrics_rows:
            continue

        mini_metrics = pd.DataFrame(mini_metrics_rows)
        scored_mini  = compute_scores(mini_metrics, attr_window, window)

        # Forward returns
        # nav_returns has monthly returns; cumulate 1/3/6/12 months ahead
        fwd_months = {"fwd_1m": 1, "fwd_3m": 3, "fwd_6m": 6, "fwd_12m": 12}

        for _, row in scored_mini.iterrows():
            sc = int(row["scheme_code"])
            if sc not in nav_returns.columns:
                continue
            fund_nav = nav_returns[sc].sort_index()
            future   = fund_nav[fund_nav.index > pd.Timestamp(score_date)]

            fwd = {}
            for label, n in fwd_months.items():
                if len(future) >= n:
                    # Compound n monthly returns
                    r = (1 + future.iloc[:n]).prod() - 1
                    fwd[label] = float(r)
                else:
                    fwd[label] = np.nan

            bt_rows.append({
                "scheme_code": sc,
                "score_date":  score_date,
                "score":       float(row["score"]),
                **fwd,
            })

        if (i + 1) % 6 == 0:
            log.info(f"  … backtest {i+1}/{len(score_dates)} months done")

    log.info(f"  Backtest: {len(bt_rows)} score×fund observations")
    return pd.DataFrame(bt_rows)


def summarise_backtest(bt: pd.DataFrame) -> None:
    """Print a simple quintile summary of the backtest."""
    if bt.empty or "fwd_12m" not in bt.columns:
        return

    bt = bt.copy()
    bt["quintile"] = pd.qcut(bt["score"], 5, labels=["Q1 (low)", "Q2", "Q3", "Q4", "Q5 (high)"])

    log.info("\n  === Backtest: forward 12-month return by score quintile ===")
    summary = (
        bt.groupby("quintile")["fwd_12m"]
        .agg(["mean", "median", "count"])
        .rename(columns={"mean": "mean_fwd_12m", "median": "median_fwd_12m"})
    )
    for row in summary.itertuples():
        log.info(
            f"  {row.Index}: mean={row.mean_fwd_12m*100:.1f}%  "
            f"median={row.median_fwd_12m*100:.1f}%  n={row.count}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args    = _parse_args()
    mf_data = Path(args.mf_data)

    scores_path  = mf_data / "scores.parquet"
    bt_path      = mf_data / "backtest.parquet"

    if not args.force and scores_path.exists():
        log.info("scores.parquet already exists. Use --force to recompute.")
        return

    # Load inputs
    attr_path    = mf_data / "attribution.parquet"
    metrics_path = mf_data / "fund_metrics.parquet"
    ret_path     = mf_data / "returns_monthly.parquet"

    for p in [attr_path, metrics_path]:
        if not p.exists():
            log.error(f"Missing required file: {p}. Run attribution.py first.")
            sys.exit(1)

    log.info("Loading attribution data …")
    attr    = pd.read_parquet(attr_path)
    metrics = pd.read_parquet(metrics_path)
    attr["date"] = pd.to_datetime(attr["date"])

    log.info(f"  Schemes in metrics: {len(metrics)}")
    log.info(f"  Attribution rows:   {len(attr):,}")

    # ------------------------------------------------------------------ #
    # Compute current scores
    # ------------------------------------------------------------------ #
    log.info("\nComputing scores …")
    scored = compute_scores(metrics, attr, args.window)

    # Reorder columns
    front_cols = [
        "scheme_code", "scheme_name", "amc", "score",
        "alpha_quality", "alpha_proportion", "style_stability", "positioning",
        "info_ratio", "alpha_ann", "tracking_error_ann", "total_return_ann",
        "active_ret_ann", "market_ret_ann",
        "beta_M", "beta_S", "beta_I",
        "recent_beta_M", "recent_beta_S", "recent_beta_I",
        "current_active_exposure", "beta_drift",
        "holdings_active_exposure", "holdings_style_active",
        "holdings_coverage_pct", "holdings_month",
        "r_squared", "n_months",
    ]
    scored = scored[[c for c in front_cols if c in scored.columns]]
    scored = scored.sort_values("score", ascending=False)
    scored.to_parquet(scores_path, index=False)

    log.info(f"\n  Saved scores.parquet  ({len(scored)} funds)")
    log.info(f"  Score distribution: min={scored['score'].min():.1f}  "
             f"median={scored['score'].median():.1f}  "
             f"max={scored['score'].max():.1f}")

    log.info("\n  === Top 10 Funds by Score ===")
    top10 = scored.head(10)[["scheme_name", "amc", "score", "info_ratio", "alpha_ann"]]
    for _, r in top10.iterrows():
        log.info(
            f"  [{r['score']:5.1f}] {r['scheme_name'][:50]:<50} "
            f"IR={r['info_ratio']:+.2f}  α={r['alpha_ann']*100:.1f}%/yr"
        )

    # ------------------------------------------------------------------ #
    # Backtest (optional)
    # ------------------------------------------------------------------ #
    if args.backtest:
        if not ret_path.exists():
            log.warning("returns_monthly.parquet not found — skipping backtest")
        else:
            nav_returns = pd.read_parquet(ret_path)
            nav_returns.index = pd.to_datetime(nav_returns.index)
            nav_returns.columns = [int(c) for c in nav_returns.columns]

            bt = run_backtest(attr, metrics, nav_returns, args.window)
            if not bt.empty:
                bt.to_parquet(bt_path, index=False)
                log.info(f"\n  Saved backtest.parquet  ({len(bt):,} rows)")
                summarise_backtest(bt)

    log.info("\nScoring complete.")


if __name__ == "__main__":
    main()
