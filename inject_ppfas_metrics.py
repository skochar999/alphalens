#!/usr/bin/env python3
"""
inject_ppfas_metrics.py
=======================
Inject PPFAS Flexi Cap Fund (scheme 122639) into fund_metrics.parquet
so it appears in scorer output and scores.parquet.

Steps:
  1. Compute NAV-based factor regression metrics (alpha, TE, IR, betas)
     using the same factor return series already in attribution.parquet
  2. Pull holdings-based metrics from holdings_alpha.parquet (already computed)
  3. Pull factor exposures from holdings_alpha.parquet
  4. Build a complete fund_metrics row and append (or replace) in fund_metrics.parquet
  5. Re-run scorer to regenerate scores.parquet
  6. Rebuild fundlens.html and mf_website.html

Only scheme 122639 (Flexi Cap Direct Growth) qualifies — the other PPFAS schemes
don't have enough months in holdings_alpha (they aren't equity-heavy enough).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

MF_DATA = Path(__file__).parent / "mf_data"

# PPFAS scheme codes to inject
PPFAS_SCHEMES = {
    122639: ("Parag Parikh Flexi Cap Fund - Direct Plan - Growth", "PPFAS"),
    147481: ("Parag Parikh ELSS Tax Saver Fund - Direct Growth", "PPFAS"),
    148958: ("Parag Parikh Conservative Hybrid Fund - Direct Plan - Growth", "PPFAS"),
}


def compute_factor_metrics(scheme_code: int, scheme_name: str) -> dict | None:
    """Run 3-factor regression for a PPFAS scheme using existing factor return series."""
    nav = pd.read_parquet(MF_DATA / "nav_monthly.parquet")
    attr = pd.read_parquet(MF_DATA / "attribution.parquet")

    if scheme_code not in nav.columns:
        print(f"  WARN: scheme {scheme_code} not in nav_monthly — skipping")
        return None

    # ── Factor returns (market/style/industry) — deduplicated by date ──────
    factor_rets = (
        attr[["date", "market_ret", "style_ret", "industry_ret"]]
        .drop_duplicates("date")
        .set_index("date")
        .sort_index()
    )
    factor_rets.index = pd.to_datetime(factor_rets.index)

    # ── Monthly NAV returns for this scheme ─────────────────────────────────
    nav_series = nav[scheme_code].dropna()
    if len(nav_series) < 6:
        print(f"  WARN: only {len(nav_series)} NAV points for {scheme_code} — skipping")
        return None

    ret = nav_series.pct_change().dropna()
    ret.index = ret.index.to_period("M").to_timestamp("M")  # normalize to month-end

    # ── Align ───────────────────────────────────────────────────────────────
    merged = pd.merge(
        ret.rename("fund"),
        factor_rets,
        left_index=True, right_index=True,
        how="inner",
    )
    if len(merged) < 6:
        print(f"  WARN: only {len(merged)} aligned months for {scheme_code} — skipping")
        return None

    n = len(merged)
    X = merged[["market_ret", "style_ret", "industry_ret"]].values
    y = merged["fund"].values

    reg = LinearRegression().fit(X, y)
    betas = reg.coef_  # [beta_M, beta_S, beta_I]
    resid = y - reg.predict(X)

    alpha_monthly = reg.intercept_
    alpha_ann = alpha_monthly * 12
    tracking_error_ann = resid.std() * np.sqrt(12)
    info_ratio = alpha_ann / tracking_error_ann if tracking_error_ann > 0 else 0.0
    r_squared = reg.score(X, y)

    total_return_ann = (
        (nav_series.iloc[-1] / nav_series.iloc[0]) ** (12 / len(ret)) - 1
    )
    market_ret_ann   = merged["market_ret"].mean() * 12
    style_ret_ann    = merged["style_ret"].mean() * 12
    industry_ret_ann = merged["industry_ret"].mean() * 12

    # active_ret ≈ alpha + style_active + industry_active
    active_ret_ann = alpha_ann + (
        betas[1] * merged["style_ret"].mean()
        + betas[2] * merged["industry_ret"].mean()
    ) * 12

    # Beta drift proxy: compare first half vs second half market beta
    half = n // 2
    reg_first = LinearRegression().fit(X[:half], y[:half])
    reg_last  = LinearRegression().fit(X[half:], y[half:])
    beta_drift = abs(reg_last.coef_[0] - reg_first.coef_[0])

    # Recent beta = last 12 months
    X_rec = X[-min(12, n):]
    y_rec = y[-min(12, n):]
    reg_rec = LinearRegression().fit(X_rec, y_rec) if len(X_rec) >= 4 else reg
    recent_betas = reg_rec.coef_

    return {
        "scheme_code": scheme_code,
        "scheme_name": scheme_name,
        "amc": "PPFAS",
        "alpha_ann": alpha_ann,
        "tracking_error_ann": tracking_error_ann,
        "info_ratio": info_ratio,
        "total_return_ann": total_return_ann,
        "market_ret_ann": market_ret_ann,
        "style_ret_ann": style_ret_ann,
        "industry_ret_ann": industry_ret_ann,
        "active_ret_ann": active_ret_ann,
        "beta_M": betas[0],
        "beta_S": betas[1],
        "beta_I": betas[2],
        "recent_beta_M": recent_betas[0],
        "recent_beta_S": recent_betas[1],
        "recent_beta_I": recent_betas[2],
        "beta_drift": beta_drift,
        "r_squared": r_squared,
        "n_months": n,
        # Will be filled from holdings_alpha below
        "current_active_exposure": None,
        "style_drift_x": None,
        "alpha_source": "factor_regression",
        "style_drift_y": None,
        "holdings_month": None,
        "holdings_coverage_pct": None,
        "holdings_matched_stocks": None,
        "holdings_style_active": None,
        "holdings_industry_active": None,
        "holdings_active_exposure": None,
        "holdings_alpha_ann": None,
        "holdings_ir": None,
        "holdings_n_months": None,
        "holdings_avg_coverage": None,
        "holdings_alpha_monthly_mean": None,
        "holdings_alpha_monthly_std": None,
        "style_drift": None,
    }


def build_ppfas_rows() -> pd.DataFrame:
    """Build complete fund_metrics rows for all qualifying PPFAS schemes."""
    ha = pd.read_parquet(MF_DATA / "holdings_alpha.parquet")
    fm_ref = pd.read_parquet(MF_DATA / "fund_metrics.parquet")

    # All factor exposure columns in fund_metrics
    exp_cols = [c for c in fm_ref.columns if c.startswith("exp_")]

    rows = []
    for code, (name, amc) in PPFAS_SCHEMES.items():
        print(f"\nProcessing scheme {code}: {name}")

        metrics = compute_factor_metrics(code, name)
        if metrics is None:
            continue

        # ── Merge holdings_alpha data ────────────────────────────────────────
        ha_row = ha[ha["scheme_code"] == code]
        if ha_row.empty:
            print(f"  INFO: {code} not in holdings_alpha — using NAV metrics only")
            # Fill exposure columns with 0
            for col in exp_cols:
                metrics[col] = 0.0
            metrics["current_active_exposure"] = 0.0
            metrics["style_drift"] = 0.0
        else:
            hr = ha_row.iloc[0]
            metrics["holdings_alpha_ann"]        = hr["holdings_alpha_ann"]
            metrics["holdings_ir"]               = hr["holdings_ir"]
            metrics["holdings_n_months"]         = hr["holdings_n_months"]
            metrics["holdings_avg_coverage"]     = hr["holdings_avg_coverage"]
            metrics["holdings_alpha_monthly_mean"] = hr["holdings_alpha_monthly_mean"]
            metrics["holdings_alpha_monthly_std"]  = hr["holdings_alpha_monthly_std"]
            metrics["style_drift"]               = hr["style_drift"]
            metrics["style_drift_x"]             = hr["style_drift"]
            metrics["style_drift_y"]             = hr["style_drift"]
            metrics["current_active_exposure"]   = hr.get("exp_MARKET", 1.0)
            metrics["holdings_active_exposure"]  = hr.get("exp_MARKET", 1.0)
            metrics["holdings_coverage_pct"]     = hr["holdings_avg_coverage"]
            metrics["holdings_style_active"]     = hr.get("avg_style_attr", 0.0)
            metrics["holdings_industry_active"]  = hr.get("avg_industry_attr", 0.0)

            # Factor exposure columns
            for col in exp_cols:
                factor = col.replace("exp_", "")
                metrics[col] = hr.get(f"exp_{factor}", 0.0)

        rows.append(metrics)
        print(f"  ✓ Built row: alpha_ann={metrics['alpha_ann']:.3f}  "
              f"IR={metrics['info_ratio']:.3f}  "
              f"holdings_alpha={metrics.get('holdings_alpha_ann')}")

    if not rows:
        print("No PPFAS rows built — nothing to inject")
        return pd.DataFrame()

    return pd.DataFrame(rows)


def inject_into_fund_metrics(new_rows: pd.DataFrame) -> None:
    """Append (or replace) PPFAS rows in fund_metrics.parquet."""
    fm_path = MF_DATA / "fund_metrics.parquet"
    fm = pd.read_parquet(fm_path)

    # Remove any existing PPFAS rows
    ppfas_codes = list(PPFAS_SCHEMES.keys())
    fm = fm[~fm["scheme_code"].isin(ppfas_codes)]
    print(f"\nfund_metrics before: {len(fm)} rows (dropped old PPFAS if any)")

    # Align columns — add missing cols as NaN
    for col in fm.columns:
        if col not in new_rows.columns:
            new_rows[col] = np.nan

    # Keep same column order
    new_rows = new_rows[fm.columns]

    fm_updated = pd.concat([fm, new_rows], ignore_index=True)
    print(f"fund_metrics after: {len(fm_updated)} rows")

    # Backup
    fm_path.with_suffix(".parquet.bak").write_bytes(fm_path.read_bytes())
    fm_updated.to_parquet(fm_path, index=False)
    print(f"Saved: {fm_path}")


def run_scorer() -> None:
    """Re-run scorer.py to regenerate scores.parquet."""
    scorer = Path(__file__).parent / "scorer.py"
    if not scorer.exists():
        print("scorer.py not found — skipping")
        return
    print("\nRunning scorer.py --force …")
    r = subprocess.run(
        [sys.executable, str(scorer), "--force",
         "--mf-data", str(MF_DATA)],
        capture_output=True, text=True,
    )
    print(r.stdout[-2000:] if r.stdout else "")
    if r.returncode != 0:
        print("STDERR:", r.stderr[-1000:])
        raise RuntimeError("scorer.py failed")
    print("scorer.py complete")


def rebuild_sites() -> None:
    """Rebuild FundLens and mf_website HTML files."""
    for script in ["build_fundlens.py", "build_mf_website.py"]:
        p = Path(__file__).parent / script
        if not p.exists():
            print(f"{script} not found — skipping")
            continue
        print(f"\nRunning {script} …")
        r = subprocess.run(
            [sys.executable, str(p)],
            capture_output=True, text=True,
        )
        print(r.stdout[-500:] if r.stdout else "")
        if r.returncode != 0:
            print("STDERR:", r.stderr[-500:])


def main():
    print("=" * 60)
    print("PPFAS → fund_metrics injection")
    print("=" * 60)

    new_rows = build_ppfas_rows()
    if new_rows.empty:
        print("Nothing to inject — exiting")
        return

    inject_into_fund_metrics(new_rows)
    run_scorer()
    rebuild_sites()

    # ── Verify ───────────────────────────────────────────────────────────────
    print("\n── Verification ─────────────────────────────────────────────────")
    fm = pd.read_parquet(MF_DATA / "fund_metrics.parquet")
    print(f"fund_metrics rows: {len(fm)}")
    ppfas = fm[fm["scheme_code"].isin(list(PPFAS_SCHEMES.keys()))]
    print(f"PPFAS rows in fund_metrics: {len(ppfas)}")
    print(ppfas[["scheme_code","scheme_name","alpha_ann","info_ratio",
                  "holdings_alpha_ann","holdings_ir","n_months"]].to_string())

    scores_path = MF_DATA / "scores.parquet"
    if scores_path.exists():
        sc = pd.read_parquet(scores_path)
        print(f"\nscores.parquet rows: {len(sc)}")
        ppfas_sc = sc[sc["scheme_code"].isin(list(PPFAS_SCHEMES.keys()))]
        print(f"PPFAS rows in scores: {len(ppfas_sc)}")
        if not ppfas_sc.empty:
            print(ppfas_sc[["scheme_code","scheme_name","total_score"]].to_string())


if __name__ == "__main__":
    main()
