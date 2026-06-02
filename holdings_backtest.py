#!/usr/bin/env python3
"""
holdings_backtest.py — Rolling backtest on holdings-based attribution signals.

For each scoring month T (Jan 2024 – Feb 2026):
  1. Filter to clean active-equity fund-months (|market_attr| < 0.30, coverage > 30%, etc.)
  2. Compute trailing signals per fund using all data up to and including month T:
       - TTM stock_selection     : avg monthly stock_selection over last 12 months
       - TTM information_ratio   : mean / std of monthly stock_selection
       - Style drift             : std of monthly style_attr (inconsistency in style bets)
       - Factor purity           : avg |industry_attr| (how much sector bets drive returns)
  3. Rank funds into quintiles on each signal
  4. Measure forward NAV returns at T+1, T+3, T+6
  5. Compute quintile spreads (Q1-Q5) and IC (rank correlation of signal vs fwd return)
  6. Report results + optimal weight suggestion

Usage:
  python3 holdings_backtest.py --mf-data ./mf_data
"""

import argparse
import logging
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("holdings_backtest")
warnings.filterwarnings("ignore", category=RuntimeWarning)


# ── Constants ────────────────────────────────────────────────────────────────

# Filtering thresholds (matching attribution.py)
MIN_COVERAGE       = 0.30    # equity_coverage_pct ≥ 30%
MAX_MARKET_ATTR    = 0.30    # |market_attr| < 0.30 to exclude index funds
MAX_STOCK_SEL      = 0.30    # |stock_selection| < 0.30 to remove extreme outliers
MIN_SIGNAL_MONTHS  = 3       # minimum months of history to compute a signal
TTM_MONTHS         = 12      # trailing window for rolling signals

# Quintile labels (1=best signal, 5=worst signal)
N_QUINTILES = 5

# Forward horizons to test (in months)
FWD_HORIZONS = [1, 3, 6]


# ── Data loading ─────────────────────────────────────────────────────────────

def load_attribution(mf_data: Path) -> pd.DataFrame:
    """Load and clean holdings_attribution.parquet."""
    path = mf_data / "holdings_attribution.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Not found: {path}")
    df = pd.read_parquet(path)

    # Parse month column
    df["month_dt"] = pd.to_datetime(df["month"] + "-01")

    # Clean filter
    mask = (
        (df["equity_coverage_pct"] >= MIN_COVERAGE) &
        (df["market_attr"].abs() < MAX_MARKET_ATTR) &
        (df["fund_return"].notna()) &
        (df["stock_selection"].notna()) &
        (df["stock_selection"].abs() < MAX_STOCK_SEL)
    )
    clean = df[mask].copy()
    log.info(f"Attribution: {len(df)} total → {len(clean)} after cleaning "
             f"({clean['scheme_code'].nunique()} funds, {clean['month'].nunique()} months)")
    return clean


def load_nav_returns(mf_data: Path) -> pd.DataFrame:
    """
    Load nav_monthly.parquet, compute month-over-month log returns.
    Returns a long-form DataFrame: scheme_code, month_dt, nav_return
    """
    path = mf_data / "nav_monthly.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Not found: {path}")
    nav = pd.read_parquet(path)
    nav.index = pd.to_datetime(nav.index)

    # Compute simple monthly returns
    ret = nav.pct_change()
    ret.index.name = "date"

    # Melt to long form
    ret_long = ret.reset_index().melt(
        id_vars="date", var_name="scheme_code", value_name="nav_return"
    )
    ret_long["scheme_code"] = pd.to_numeric(ret_long["scheme_code"], errors="coerce")
    ret_long = ret_long.dropna(subset=["nav_return", "scheme_code"])
    ret_long["month_dt"] = ret_long["date"].dt.to_period("M").dt.to_timestamp("M")

    # Compound multi-month forward returns
    # Build pivot for fast lookup: scheme_code → month → return
    pivot = ret_long.pivot_table(
        index="month_dt", columns="scheme_code", values="nav_return"
    )
    return pivot


def compute_forward_returns(
    pivot: pd.DataFrame, scoring_months: list, horizons: list
) -> pd.DataFrame:
    """
    For each scoring month and each fund, compute forward compounded returns.
    Returns DataFrame indexed by (month_dt, scheme_code) with columns fwd_{h}m.
    """
    records = []
    for sm in scoring_months:
        sm_dt = pd.Timestamp(sm + "-01") + pd.offsets.MonthEnd(0)
        for h in horizons:
            # Forward months: sm+1, sm+2, ... sm+h
            fwd_months = [sm_dt + pd.offsets.MonthEnd(i) for i in range(1, h + 1)]
            # Compound return = product of (1 + monthly_ret) - 1
            for sc in pivot.columns:
                rets = [pivot.loc[m, sc] if m in pivot.index else np.nan
                        for m in fwd_months]
                rets = [r for r in rets if not np.isnan(r)]
                if len(rets) == h:
                    fwd_ret = np.prod([1 + r for r in rets]) - 1
                else:
                    fwd_ret = np.nan
                records.append({
                    "month_dt": sm_dt,
                    "scheme_code": sc,
                    f"fwd_{h}m": fwd_ret,
                })

    if not records:
        return pd.DataFrame()

    # Merge all horizons
    df = pd.DataFrame(records)
    fwd_cols = [f"fwd_{h}m" for h in horizons]
    result = df.pivot_table(
        index=["month_dt", "scheme_code"],
        values=fwd_cols,
        aggfunc="first",
    ).reset_index()
    return result


# ── Signal computation ────────────────────────────────────────────────────────

def compute_trailing_signals(attr: pd.DataFrame, scoring_month: str) -> pd.DataFrame:
    """
    For a given scoring month T (YYYY-MM), compute trailing signals for each fund
    using all attribution data up to and including T.

    Returns DataFrame: scheme_code → {signal_*, n_months_used}
    """
    cutoff = pd.Timestamp(scoring_month + "-01") + pd.offsets.MonthEnd(0)
    hist   = attr[attr["month_dt"] <= cutoff].copy()

    # Use last TTM_MONTHS months of history per fund
    records = []
    for sc, grp in hist.groupby("scheme_code"):
        grp = grp.sort_values("month_dt").tail(TTM_MONTHS)
        n = len(grp)
        if n < MIN_SIGNAL_MONTHS:
            continue

        ss = grp["stock_selection"].values  # monthly stock selection

        # Signal 1: TTM mean stock selection (alpha signal)
        ss_mean = np.mean(ss)

        # Signal 2: TTM information ratio (mean / std, Sharpe-like)
        ss_std = np.std(ss, ddof=1)
        ss_ir  = ss_mean / ss_std if ss_std > 1e-8 else 0.0

        # Signal 3: Style consistency (low style drift = better)
        # style_drift = std of monthly style_attr
        style_std = grp["style_attr"].std(ddof=1) if "style_attr" in grp else np.nan

        # Signal 4: Factor purity (low = less noise from industry bets)
        # Use |industry_attr| mean — lower means stock selection is cleaner
        ind_abs = grp["industry_attr"].abs().mean() if "industry_attr" in grp else np.nan

        # Signal 5: Coverage (higher = more reliable)
        avg_cov = grp["equity_coverage_pct"].mean()

        records.append({
            "scheme_code": sc,
            "sig_alpha":   ss_mean,   # higher = better
            "sig_ir":      ss_ir,     # higher = better
            "sig_style_stability": -style_std if pd.notna(style_std) else np.nan,  # negated → higher = better
            "sig_industry_purity": -ind_abs if pd.notna(ind_abs) else np.nan,      # negated → higher = better
            "sig_coverage": avg_cov,  # higher = better
            "n_months": n,
        })

    return pd.DataFrame(records) if records else pd.DataFrame()


# ── Quintile analysis ─────────────────────────────────────────────────────────

def quintile_spread(signal: pd.Series, fwd_ret: pd.Series) -> dict:
    """
    Rank signal into quintiles and compute:
      - Q1 (top) vs Q5 (bottom) mean forward return spread
      - Monotonicity (Spearman rank correlation of quintile rank vs return)
      - IC (Spearman correlation of raw signal vs forward return)
    Returns dict with metrics.
    """
    df = pd.DataFrame({"signal": signal, "fwd": fwd_ret}).dropna()
    if len(df) < 10:
        return {}

    # Quintile assignment (1=top signal, 5=bottom)
    df["q"] = pd.qcut(df["signal"], N_QUINTILES, labels=False, duplicates="drop")
    df["q"] = N_QUINTILES - df["q"]  # flip: higher signal → Q1

    qret = df.groupby("q")["fwd"].mean()
    q1 = qret.get(1, np.nan)
    q5 = qret.get(5, np.nan)
    spread = q1 - q5 if pd.notna(q1) and pd.notna(q5) else np.nan

    # IC
    ic, pval = stats.spearmanr(df["signal"], df["fwd"])

    # Quintile returns (ordered Q1 to Q5)
    qret_ordered = [qret.get(q, np.nan) for q in range(1, N_QUINTILES + 1)]

    return {
        "spread": spread,
        "ic":     ic,
        "pval":   pval,
        "n":      len(df),
        "q1":     q1,
        "q5":     q5,
        "qret":   qret_ordered,
    }


# ── Main backtest loop ────────────────────────────────────────────────────────

def run_backtest(mf_data: Path) -> None:
    attr   = load_attribution(mf_data)
    nav_pv = load_nav_returns(mf_data)

    all_months = sorted(attr["month"].unique())

    # Scoring months: Jan 2024 onward (need ≥3 months of prior data)
    # We skip early months with <5 funds
    scoring_months = [m for m in all_months if m >= "2024-01"]
    log.info(f"Scoring months: {scoring_months[0]} → {scoring_months[-1]} "
             f"({len(scoring_months)} months)")

    # Pre-compute forward returns for all scoring months
    log.info("Computing forward NAV returns …")
    fwd_df = compute_forward_returns(nav_pv, scoring_months, FWD_HORIZONS)
    fwd_df["scheme_code"] = pd.to_numeric(fwd_df["scheme_code"], errors="coerce")

    # Per-signal, per-horizon: collect all (signal, fwd_ret) pairs across months
    SIGNALS = ["sig_alpha", "sig_ir", "sig_style_stability",
               "sig_industry_purity", "sig_coverage"]

    # Accumulator: {signal: {horizon: [(sig_val, fwd_val), ...]}}
    pairs: dict[str, dict[int, list]] = {
        s: {h: [] for h in FWD_HORIZONS} for s in SIGNALS
    }

    log.info("Building signal-return pairs …")
    for sm in scoring_months:
        sm_dt = pd.Timestamp(sm + "-01") + pd.offsets.MonthEnd(0)

        # Compute trailing signals
        sig_df = compute_trailing_signals(attr, sm)
        if sig_df.empty or len(sig_df) < 5:
            log.debug(f"  {sm}: too few funds ({len(sig_df)})")
            continue

        # Get forward returns for this scoring month
        fwd_this = fwd_df[fwd_df["month_dt"] == sm_dt].copy()
        if fwd_this.empty:
            log.debug(f"  {sm}: no forward returns available")
            continue

        # Merge
        merged = sig_df.merge(fwd_this, on="scheme_code", how="inner")
        log.debug(f"  {sm}: {len(sig_df)} signals, {len(merged)} matched with fwd returns")

        for s in SIGNALS:
            for h in FWD_HORIZONS:
                col = f"fwd_{h}m"
                if col in merged.columns:
                    sub = merged[[s, col]].dropna()
                    pairs[s][h].extend(zip(sub[s], sub[col]))

    # Compute summary stats per signal × horizon
    print("\n" + "=" * 70)
    print("HOLDINGS BACKTEST RESULTS")
    print("=" * 70)
    print(f"Period: {scoring_months[0]} → {scoring_months[-1]}  |  "
          f"Quintiles: {N_QUINTILES}  |  Min coverage: {MIN_COVERAGE:.0%}")

    SIGNAL_LABELS = {
        "sig_alpha":            "TTM Stock Selection Alpha",
        "sig_ir":               "TTM Information Ratio",
        "sig_style_stability":  "Style Stability (−drift)",
        "sig_industry_purity":  "Industry Purity (−|ind_attr|)",
        "sig_coverage":         "Equity Coverage %",
    }

    results = {}  # {signal: {horizon: metrics}}

    for s in SIGNALS:
        label = SIGNAL_LABELS[s]
        print(f"\n{'─'*70}")
        print(f"Signal: {label}")
        for h in FWD_HORIZONS:
            data = pairs[s][h]
            if len(data) < 20:
                print(f"  Fwd {h}m: insufficient data ({len(data)} pairs)")
                continue
            sig_arr = pd.Series([x[0] for x in data])
            fwd_arr = pd.Series([x[1] for x in data])
            m = quintile_spread(sig_arr, fwd_arr)
            if not m:
                continue

            results.setdefault(s, {})[h] = m

            q_str = "  ".join(f"Q{i+1}:{v*100:+.1f}%" if pd.notna(v) else f"Q{i+1}:  NA "
                              for i, v in enumerate(m["qret"]))
            print(f"  Fwd {h}m  n={m['n']:4d}  "
                  f"Spread={m['spread']*100:+.2f}%  "
                  f"IC={m['ic']:+.3f}  p={m['pval']:.3f}  "
                  f"| {q_str}")

    # Summarise IC per signal
    print("\n" + "=" * 70)
    print("IC SUMMARY (Spearman rank correlation: signal → forward return)")
    print("=" * 70)
    print(f"{'Signal':<30} {'IC@1m':>8} {'IC@3m':>8} {'IC@6m':>8}  {'Avg IC':>8}")
    ic_scores = {}
    for s in SIGNALS:
        ics = []
        row = [f"{SIGNAL_LABELS[s]:<30}"]
        for h in FWD_HORIZONS:
            ic_val = results.get(s, {}).get(h, {}).get("ic", np.nan)
            row.append(f"{ic_val:>+8.3f}" if pd.notna(ic_val) else f"{'NA':>8}")
            if pd.notna(ic_val):
                ics.append(ic_val)
        avg_ic = np.mean(ics) if ics else np.nan
        ic_scores[s] = avg_ic
        row.append(f"{avg_ic:>+8.3f}" if pd.notna(avg_ic) else f"{'NA':>8}")
        print("".join(row))

    # Suggest weights
    print("\n" + "=" * 70)
    print("SUGGESTED SCORING WEIGHTS (proportional to avg IC²)")
    print("=" * 70)
    ic2 = {s: ic_scores[s] ** 2 for s in SIGNALS if pd.notna(ic_scores.get(s))}
    total = sum(ic2.values())
    if total > 0:
        for s, v in sorted(ic2.items(), key=lambda x: -x[1]):
            w = v / total * 100
            print(f"  {SIGNAL_LABELS[s]:<35}  IC²={v:.4f}  weight={w:.1f}%")

    # Quintile spread table: Q1-Q5 at 6m horizon
    print("\n" + "=" * 70)
    print("QUINTILE RETURN SPREAD at 6-MONTH HORIZON")
    print("(Q1=top signal quintile, Q5=bottom signal quintile)")
    print("=" * 70)
    print(f"{'Signal':<30} {'Q1':>7} {'Q2':>7} {'Q3':>7} {'Q4':>7} {'Q5':>7}  {'Q1-Q5':>8}")
    for s in SIGNALS:
        m = results.get(s, {}).get(6)
        if not m:
            continue
        qr = m["qret"]
        vals = [f"{v*100:>+.1f}%" if pd.notna(v) else "    NA" for v in qr]
        spread = m["spread"] * 100
        print(f"{SIGNAL_LABELS[s]:<30} {'  '.join(vals)}  {spread:>+.2f}%")

    print()

    # Current weights in scorer.py for reference
    print("Current scorer.py weights (approximate):")
    print("  Alpha quality (alpha_ann + IR)  35%")
    print("  Consistency (rolling alpha)     25%")
    print("  Factor purity (style/sector)    20%")
    print("  Momentum/drawdown               20%")
    print()
    print("Recommendation based on backtest:")

    # Simple recommendation
    sig_alpha_ic6 = results.get("sig_alpha",{}).get(6,{}).get("ic", 0)
    sig_ir_ic6    = results.get("sig_ir",   {}).get(6,{}).get("ic", 0)
    sig_sty_ic6   = results.get("sig_style_stability",{}).get(6,{}).get("ic", 0)
    sig_ind_ic6   = results.get("sig_industry_purity",{}).get(6,{}).get("ic", 0)
    sig_cov_ic6   = results.get("sig_coverage",{}).get(6,{}).get("ic", 0)

    if abs(sig_alpha_ic6) > 0.05:
        print(f"  • Stock selection alpha has meaningful IC ({sig_alpha_ic6:+.3f} @ 6m) → keep alpha weight high")
    else:
        print(f"  • Stock selection alpha IC is weak ({sig_alpha_ic6:+.3f} @ 6m) → consider reducing alpha weight")

    if abs(sig_ir_ic6) > 0.05:
        print(f"  • IR signal is predictive ({sig_ir_ic6:+.3f} @ 6m) → IR deserves significant weight")
    else:
        print(f"  • IR signal is weak ({sig_ir_ic6:+.3f} @ 6m) → IR may not add much value alone")

    if abs(sig_sty_ic6) > 0.05:
        print(f"  • Style stability is predictive ({sig_sty_ic6:+.3f} @ 6m) → increase consistency weight")
    else:
        print(f"  • Style stability is weak ({sig_sty_ic6:+.3f} @ 6m) → deprioritize consistency signals")


def main():
    ap = argparse.ArgumentParser(description="Holdings-based attribution backtest")
    ap.add_argument("--mf-data", default="./mf_data")
    ap.add_argument("--debug",   action="store_true")
    args = ap.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    run_backtest(Path(args.mf_data))


if __name__ == "__main__":
    main()
