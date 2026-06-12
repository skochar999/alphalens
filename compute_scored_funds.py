#!/usr/bin/env python3
"""
compute_scored_funds.py
=======================
Merges benchmark_metrics + fund_metrics + holdings_alpha into a single
scored_funds.parquet with 0–100 scores and return decomposition.

Scoring (active funds WITH holdings decomp — holdings-aware formula):
  40%  Stock-selection alpha annualised (pick_ann_pp)    — from Brinson decomp
  20%  Pick hit rate (% months positive stock selection) — from Brinson decomp
  25%  Net active return vs benchmark (after fees)
   5%  Style stability (low beta drift)
  10%  Cost (low TER)
  nav_only = False

Scoring (active funds WITHOUT holdings decomp — NAV-only fallback):
  40%  Net active return vs benchmark (after fees)
  35%  Hit rate (% months beating benchmark)
  15%  Style stability (low beta drift)
  10%  Cost (low TER)
  nav_only = True

Scoring (index funds):
  60%  Cost (TER)
  40%  Tracking accuracy (active return close to 0)

Decomposition components (annualised pp):
  d_style   — style factor attribution (from holdings_alpha)
  d_sector  — industry/sector attribution (from holdings_alpha)
  d_pick    — stock selection alpha (regression intercept from fund_metrics)
  d_timing  — residual (beta/timing/cash calls)

Skill label:
  Strong   — hit_rate >= 60% AND alpha > +0.5pp
  Some     — hit_rate >= 55% AND alpha > 0
  Neutral  — hit_rate >= 50%
  Luck     — otherwise

Usage:
    python compute_scored_funds.py
    python compute_scored_funds.py --data-dir /path/to/mf_data
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fundlens.scorer")

HERE     = Path(__file__).parent
DATA_DIR = HERE / "mf_data"

# Category display name mapping
CAT_DISPLAY = {
    "Large Cap Fund":                               "Large Cap",
    "Mid Cap Fund":                                 "Mid Cap",
    "Small Cap Fund":                               "Small Cap",
    "Large & Mid Cap Fund":                         "Large & Mid Cap",
    "Flexi Cap Fund":                               "Flexi Cap",
    "Multi Cap Fund":                               "Multi Cap",
    "Focused Fund":                                 "Focused",
    "Value Fund":                                   "Value / Contrarian",
    "Contra Fund":                                  "Value / Contrarian",
    "ELSS":                                         "Tax Saver (ELSS)",
    "Sectoral/ Thematic":                           "Thematic / Sectoral",
    "Balanced Advantage":                           "Balanced Advantage",
    "Dynamic Asset Allocation or Balanced Advantage": "Balanced Advantage",
    "Aggressive Hybrid Fund":                       "Hybrid",
    "Equity Savings":                               "Hybrid",
    "Arbitrage Fund":                               "Hybrid",
    "Multi Asset Allocation":                       "Hybrid",
    "Retirement Fund":                              "Retirement",
    "Index Funds":                                  "Index Funds",
    "ETF":                                          "Index Funds",
    "Fund of Funds":                                "Index Funds",
}

DECOMP_CAP = 25.0  # cap decomp components at ±25pp for display robustness


def percentile_rank(series: pd.Series) -> pd.Series:
    """Return 0–100 percentile rank within the series."""
    return series.rank(pct=True, method="average") * 100


def skill_label(hit_rate: float, alpha_ann: float) -> str:
    hr = hit_rate if pd.notna(hit_rate) else 0.0
    alp = alpha_ann * 100 if pd.notna(alpha_ann) else 0.0
    if hr >= 0.60 and alp > 0.5:
        return "Strong evidence of skill"
    elif hr >= 0.55 and alp > 0.0:
        return "Some evidence of skill"
    elif hr >= 0.50:
        return "Neutral — market moves explain most returns"
    else:
        return "Returns driven mainly by luck or market timing"


def run(data_dir: Path) -> None:
    bm_path   = data_dir / "benchmark_metrics.parquet"
    fm_path   = data_dir / "fund_metrics.parquet"
    ha_path   = data_dir / "holdings_alpha.parquet"
    hat_path  = data_dir / "holdings_attribution.parquet"
    out_path  = data_dir / "scored_funds.parquet"

    bm = pd.read_parquet(bm_path)
    log.info(f"benchmark_metrics: {len(bm)} funds")

    # ── fund_metrics (regression-based) ──
    fm = pd.read_parquet(fm_path) if fm_path.exists() else pd.DataFrame()
    if not fm.empty:
        log.info(f"fund_metrics:      {len(fm)} funds")
        fm = fm[["scheme_code", "beta_drift", "alpha_ann", "r_squared", "n_months"]].copy()
        fm["scheme_code"] = fm["scheme_code"].astype(int)

    # ── holdings_alpha (Brinson attribution summary per fund) ──
    ha = pd.read_parquet(ha_path) if ha_path.exists() else pd.DataFrame()
    if not ha.empty:
        log.info(f"holdings_alpha:    {len(ha)} funds")
        ha_cols = ["scheme_code", "avg_style_attr", "avg_industry_attr",
                   "holdings_avg_coverage", "holdings_n_months"]
        ha = ha[[c for c in ha_cols if c in ha.columns]].copy()
        ha["scheme_code"] = ha["scheme_code"].astype(int)

    # ── holdings_attribution (monthly rows — compute pick stats per fund) ──
    pick_stats = pd.DataFrame()
    if hat_path.exists():
        hat = pd.read_parquet(hat_path)
        log.info(f"holdings_attribution: {len(hat)} monthly rows, "
                 f"{hat['scheme_code'].nunique()} funds")
        # Winsorise monthly stock-selection contribution to a physically
        # plausible band before averaging. A single month cannot beat its
        # benchmark by more than ~25% on stock picking alone; values beyond
        # that are data errors (e.g. a garbled holdings snapshot showing
        # -375% in one month) and otherwise blow up the annualised figure.
        MONTHLY_PICK_CLIP = 0.25
        n_clipped = int((hat["stock_selection"].abs() > MONTHLY_PICK_CLIP).sum())
        if n_clipped:
            log.info(f"  Winsorised {n_clipped} month-rows with "
                     f"|stock_selection| > {MONTHLY_PICK_CLIP:.0%} (data errors)")
        hat["stock_selection_clip"] = hat["stock_selection"].clip(
            -MONTHLY_PICK_CLIP, MONTHLY_PICK_CLIP
        )
        pick_stats = (
            hat.groupby("scheme_code")
               .agg(
                   pick_ann_pp   = ("stock_selection_clip", lambda x: x.mean() * 12 * 100),
                   pick_hit_rate = ("stock_selection_clip", lambda x: (x > 0).mean()),
                   n_pick_months = ("stock_selection_clip", "count"),
               )
               .reset_index()
        )
        pick_stats["scheme_code"] = pick_stats["scheme_code"].astype(int)
        log.info(f"pick_stats:        {len(pick_stats)} funds  "
                 f"(pick_ann_pp range: {pick_stats['pick_ann_pp'].min():.1f}% "
                 f"to {pick_stats['pick_ann_pp'].max():.1f}%)")

    bm["scheme_code"] = bm["scheme_code"].astype(int)
    df = bm.copy()

    # Merge supplementary data
    if not fm.empty:
        df = df.merge(fm, on="scheme_code", how="left")
    if not ha.empty:
        df = df.merge(ha, on="scheme_code", how="left")
    if not pick_stats.empty:
        df = df.merge(pick_stats, on="scheme_code", how="left")

    # ── Category display names ──
    df["category_display"] = df["category"].map(CAT_DISPLAY).fillna("Other")

    # ── Detect index funds ──
    is_index = df["category_display"] == "Index Funds"

    # ── Score active funds ──
    active = df[~is_index].copy()
    if len(active) > 0:
        # Only use beta_drift if the column exists AND has data. fund_metrics
        # can be absent or keyed to a different universe (e.g. after the
        # direct→regular switch), leaving beta_drift all-NaN — in which case its
        # median is NaN and the steadiness score would poison every total_score.
        # Fall back to a neutral 50 so scoring degrades gracefully.
        bd_col = ("beta_drift"
                  if "beta_drift" in active.columns and active["beta_drift"].notna().any()
                  else None)

        # Shared sub-scores (computed across full active universe for peer-relative ranking)
        active["s_outperf"] = percentile_rank(active["net_active_ann"].fillna(-9))
        active["s_consist"] = percentile_rank(active["hit_rate"].fillna(0))
        if bd_col:
            active["s_steady"] = percentile_rank(-active[bd_col].fillna(active[bd_col].median()))
        else:
            active["s_steady"] = 50.0
        active["s_cost"] = percentile_rank(-active["ter_est"].fillna(active["ter_est"].median()))
        active["s_track"] = 0.0

        # ── Holdings-aware sub-scores (only for funds with pick stats) ──
        # Require >= MIN_PICK_MONTHS of holdings history before trusting the
        # stock-selection signal — thin histories produce wild annualised alpha
        # (e.g. -294%). Funds below the floor fall back to NAV-only scoring.
        MIN_PICK_MONTHS = 12
        if {"pick_ann_pp", "pick_hit_rate", "n_pick_months"}.issubset(active.columns):
            has_pick = (
                active["pick_ann_pp"].notna()
                & active["pick_hit_rate"].notna()
                & (active["n_pick_months"] >= MIN_PICK_MONTHS)
            )
        else:
            has_pick = pd.Series(False, index=active.index)
        log.info(f"Active funds with >= {MIN_PICK_MONTHS}mo holdings (stock-picking scored): "
                 f"{has_pick.sum()} / {len(active)}")

        if has_pick.sum() > 0:
            # Rank pick metrics within the subset that has data
            active.loc[has_pick, "ns_pick"] = percentile_rank(
                active.loc[has_pick, "pick_ann_pp"]
            )
            active.loc[has_pick, "ns_pickhit"] = percentile_rank(
                active.loc[has_pick, "pick_hit_rate"]
            )
            # Recompute outperf/steady/cost ranks within the same subset for consistency
            active.loc[has_pick, "ns_outperf"] = percentile_rank(
                active.loc[has_pick, "net_active_ann"].fillna(-9)
            )
            active.loc[has_pick, "ns_cost"] = percentile_rank(
                -active.loc[has_pick, "ter_est"].fillna(active["ter_est"].median())
            )
            active.loc[has_pick, "ns_steady"] = percentile_rank(
                -active.loc[has_pick, bd_col].fillna(active[bd_col].median())
            ) if bd_col else 50.0

        # ── Apply formula by tier ──
        # Tier 1: holdings-aware formula
        active["nav_only"] = ~has_pick
        active.loc[has_pick, "total_score"] = (
            active.loc[has_pick, "ns_pick"]    * 0.40 +
            active.loc[has_pick, "ns_pickhit"] * 0.20 +
            active.loc[has_pick, "ns_outperf"] * 0.25 +
            active.loc[has_pick, "ns_cost"]    * 0.10 +
            active.loc[has_pick, "ns_steady"]  * 0.05
        )
        # Tier 2: NAV-only fallback
        active.loc[~has_pick, "total_score"] = (
            active.loc[~has_pick, "s_outperf"] * 0.40 +
            active.loc[~has_pick, "s_consist"] * 0.35 +
            active.loc[~has_pick, "s_steady"]  * 0.15 +
            active.loc[~has_pick, "s_cost"]    * 0.10
        )

    # ── Score index funds ──
    index_df = df[is_index].copy()
    if len(index_df) > 0:
        index_df["s_outperf"] = 50.0
        index_df["s_consist"] = 50.0
        index_df["s_steady"]  = 50.0
        index_df["s_cost"]    = percentile_rank(-index_df["ter_est"].fillna(0.002))
        # Tracking accuracy: active_ann close to 0 is best
        index_df["s_track"]   = percentile_rank(-index_df["active_ann"].fillna(0).abs())
        index_df["total_score"] = (
            index_df["s_cost"]  * 0.60 +
            index_df["s_track"] * 0.40
        )
        index_df["nav_only"] = False

    # ── Combine & peer-rank within category ──
    df = pd.concat([active, index_df], ignore_index=True)

    # ── Score v2 override (walk-forward-validated formula) ────────────────
    # Selectivity 25 / pick t-stat 20 / patience 15 / concentration 15 /
    # top-10 conviction 15 / cost 10. Validated: mean rank-IC +0.088, IC>0 in
    # 76% of formations, t=4.0 (see sweep_score_weights.py). v1 kept as
    # total_score_v1 for comparison. Index funds keep their cost+tracking
    # score; active funds without v2 signals keep the NAV-only fallback.
    try:
        import compute_score_v2
        v2 = compute_score_v2.run(None, data_dir / "scores_v2.parquet")
        v2k = v2[["scheme_code", "score_v2", "tier",
                  "pillar_skill", "pillar_conviction", "pillar_cost"]]
        df["total_score_v1"] = df["total_score"]
        df = df.merge(v2k, on="scheme_code", how="left")
        ok = (df["category_display"].ne("Index Funds")) & df["score_v2"].notna()
        df.loc[ok, "total_score"] = df.loc[ok, "score_v2"]
        df.loc[ok, "nav_only"] = df.loc[ok, "tier"].eq(2)
        log.info(f"Score v2 applied to {int(ok.sum())} active funds "
                 f"({int((df.loc[ok,'tier']==1).sum())} on full holdings signals)")
    except Exception as e:
        log.warning(f"Score v2 unavailable — keeping v1 scores: {e}")

    def cat_rank(grp):
        grp = grp.copy()
        ranks = grp["total_score"].rank(ascending=False, method="min")
        # Funds with no/insufficient data have NaN total_score; rank them last
        # instead of crashing on .astype(int) (IntCastingNaNError).
        grp["cat_rank"] = ranks.fillna(len(grp)).astype(int)
        grp["cat_size"]  = len(grp)
        return grp

    df = df.groupby("category_display", group_keys=False).apply(cat_rank)

    # ── Return decomposition ──
    has_style   = "avg_style_attr"    in df.columns
    has_sector  = "avg_industry_attr" in df.columns
    has_alpha   = "alpha_ann"         in df.columns
    has_active  = "active_ann"        in df.columns

    if has_style and has_sector and has_alpha:
        df["style_ann_pp"]    = df["avg_style_attr"].apply(lambda x: x * 12 if pd.notna(x) else np.nan)
        df["industry_ann_pp"] = df["avg_industry_attr"].apply(lambda x: x * 12 if pd.notna(x) else np.nan)
        df["stockpick_ann_pp"] = df["alpha_ann"].apply(lambda x: x if pd.notna(x) else np.nan)

        if has_active:
            df["active_ann_pp"] = df["active_ann"]
            df["timing_ann_pp"] = (
                df["active_ann_pp"]
                - df["style_ann_pp"].fillna(0)
                - df["industry_ann_pp"].fillna(0)
                - df["stockpick_ann_pp"].fillna(0)
            )
        else:
            df["timing_ann_pp"] = np.nan

        # Coverage gate: only include decomp if holdings coverage >= 60%
        cov = df.get("holdings_avg_coverage", pd.Series(0, index=df.index))
        df["decomp_ok"] = (cov >= 0.60) & df["avg_style_attr"].notna()

        # Capped display values (annualised percentage points)
        df["d_style"]  = df["style_ann_pp"].apply(lambda v: round(float(np.clip(v, -DECOMP_CAP, DECOMP_CAP)), 1) if pd.notna(v) else np.nan)
        df["d_sector"] = df["industry_ann_pp"].apply(lambda v: round(float(np.clip(v, -DECOMP_CAP, DECOMP_CAP)), 1) if pd.notna(v) else np.nan)
        df["d_pick"]   = df["stockpick_ann_pp"].apply(lambda v: round(float(np.clip(v * 100, -DECOMP_CAP, DECOMP_CAP)), 1) if pd.notna(v) else np.nan)
        df["d_timing"] = df.apply(
            lambda r: round(float(np.clip(
                (r["active_ann_pp"] - r.get("style_ann_pp", 0) - r.get("industry_ann_pp", 0) - r.get("stockpick_ann_pp", 0) * 100) if pd.notna(r.get("active_ann_pp")) else np.nan,
                -DECOMP_CAP, DECOMP_CAP
            )), 1) if pd.notna(r.get("active_ann_pp")) else np.nan,
            axis=1
        )
        # Zero out decomp for funds below coverage gate
        for col in ["d_style", "d_sector", "d_pick", "d_timing"]:
            df.loc[~df["decomp_ok"], col] = np.nan

    else:
        for col in ["d_style", "d_sector", "d_pick", "d_timing", "decomp_ok"]:
            df[col] = np.nan if col != "decomp_ok" else False

    # ── Skill labels ──
    df["skill_label"] = df.apply(
        lambda r: skill_label(
            r.get("hit_rate", np.nan),
            r.get("alpha_ann", np.nan)
        ), axis=1
    )

    # ── Save ──
    df.to_parquet(out_path, index=False)

    nav_only_count = df.get("nav_only", pd.Series(False, index=df.index)).sum()
    log.info(f"scored_funds: {len(df)} funds saved to {out_path}")
    log.info(f"  Active (holdings formula): {(~is_index).sum() - nav_only_count}")
    log.info(f"  Active (NAV-only fallback): {nav_only_count}")
    log.info(f"  Index:                     {is_index.sum()}")
    log.info(f"  With decomp:               {df.get('decomp_ok', pd.Series()).sum()}")
    skill_counts = df["skill_label"].value_counts()
    for lbl, cnt in skill_counts.items():
        log.info(f"  {lbl}: {cnt}")


def main() -> None:
    p = argparse.ArgumentParser(description="Score funds and compute decomposition")
    p.add_argument("--data-dir", default=str(DATA_DIR))
    args = p.parse_args()
    run(Path(args.data_dir))


if __name__ == "__main__":
    main()
