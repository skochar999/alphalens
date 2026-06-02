"""
IEC-1 End-to-End Demo — Synthetic India Equity Universe

Generates 500 synthetic securities over 300 trading days, then runs:
  1.  ExposureBuilder   — 39-factor loadings per day
  2.  CrossSectionalRegressor — daily factor return estimation
  3.  CovarianceEstimator    — FCM + specific risk
  4.  PortfolioRisk          — risk decomposition & attribution vs benchmark

Run:
    python -m inec1.example
    # or from the outputs/ directory:
    python -c "import inec1.example"
"""

from __future__ import annotations

import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=ImportWarning)

# ---------------------------------------------------------------------------
# Synthetic data generator
# ---------------------------------------------------------------------------

def make_universe(
    n_securities: int = 500,
    n_days: int = 300,
    seed: int = 42,
) -> tuple[dict[int, pd.DataFrame], dict[int, pd.Series], dict[int, pd.Series]]:
    """
    Return (security_data_by_day, returns_by_day, mktcap_by_day).

    security_data_by_day[t] : cross-sectional DataFrame of descriptors on day t
    returns_by_day[t]       : stock returns on day t
    mktcap_by_day[t]        : market caps on day t (for WLS weights)
    """
    from inec1.config import INDUSTRY_FACTORS

    rng = np.random.default_rng(seed)
    tickers = [f"IN{i:04d}" for i in range(n_securities)]

    # ---- Static fundamentals (updated annually in practice; fixed here for demo)
    mktcap_base = np.exp(rng.normal(8, 2, n_securities)).clip(1e3, 1e7)  # INR crore
    beta_base   = rng.normal(1.0, 0.4, n_securities).clip(0.1, 2.5)
    industries  = rng.choice(INDUSTRY_FACTORS, n_securities)

    # Build time-varying data (simulating daily updates)
    security_data_by_day: dict[int, pd.DataFrame] = {}
    returns_by_day: dict[int, pd.Series]           = {}
    mktcap_by_day: dict[int, pd.Series]            = {}

    for t in range(n_days):
        # Evolve market caps slightly each day
        mktcap = mktcap_base * np.exp(rng.normal(0, 0.01, n_securities))
        mktcap = mktcap.clip(1e2, 1e8)

        # Descriptors (all in realistic ranges)
        book_to_price   = rng.lognormal(-0.5, 0.7, n_securities).clip(0.05, 5.0)
        capm_beta       = beta_base + rng.normal(0, 0.05, n_securities)
        ret_1m          = rng.normal(0.01, 0.06, n_securities)
        ret_3m          = rng.normal(0.03, 0.10, n_securities)
        ret_12m         = rng.normal(0.12, 0.25, n_securities)

        data = pd.DataFrame({
            # BETA
            "capm_beta":          capm_beta,
            # SIZE
            "market_cap":         mktcap,
            # STREV
            "ret_3m":             ret_3m,
            # LTMOM
            "ret_12m":            ret_12m,
            "ret_1m":             ret_1m,
            # VALUE
            "book_to_price":      book_to_price,
            # GROWTH
            "dividend_payout_5y": rng.beta(2, 5, n_securities),
            "delta_equity":       rng.normal(0, 1e4, n_securities),
            "delta_lt_debt":      rng.normal(0, 5e3, n_securities),
            "delta_preferred":    rng.normal(0, 1e3, n_securities),
            "book_total_5y":      np.abs(rng.normal(5e4, 2e4, n_securities)).clip(1e3),
            "asset_growth_5y":    rng.normal(0.08, 0.12, n_securities),
            "eps_growth_5y":      rng.normal(0.06, 0.20, n_securities),
            "eps_change_1y":      rng.normal(0.05, 0.30, n_securities),
            # LOWVOL
            "residual_vol":       rng.lognormal(-3, 0.6, n_securities).clip(0.05, 1.5),
            "daily_vol_3m":       rng.lognormal(-3, 0.6, n_securities).clip(0.05, 1.5),
            "hl_ratio_1m":        1 + rng.exponential(0.05, n_securities),
            "log_price":          rng.normal(5, 1.5, n_securities),
            "monthly_range_1y":   rng.lognormal(1, 0.5, n_securities),
            "vol_sensitivity":    rng.normal(1.0, 0.5, n_securities),
            # LIQUIDITY
            "share_turnover_1y":  rng.lognormal(-1, 1, n_securities).clip(0.01, 20),
            "share_turnover_q":   rng.lognormal(-1, 1, n_securities).clip(0.01, 20),
            "share_turnover_1m":  rng.lognormal(-1, 1, n_securities).clip(0.01, 20),
            "vol_to_var":         rng.lognormal(5, 1.5, n_securities).clip(1, 1e6),
            # LEVERAGE
            "mv_equity":          mktcap,
            "mv_preferred":       rng.exponential(1e2, n_securities),
            "lt_debt":            rng.exponential(5e3, n_securities),
            "book_equity":        mktcap / book_to_price.clip(0.1),
            "book_preferred":     rng.exponential(1e2, n_securities),
            "st_debt":            rng.exponential(2e3, n_securities),
            "total_assets":       rng.lognormal(10, 1.5, n_securities).clip(1e3),
            # EARNYIELD
            "trailing_ep":        rng.normal(0.06, 0.04, n_securities).clip(-0.2, 0.4),
            "avg_ep_5y":          rng.normal(0.05, 0.03, n_securities).clip(-0.2, 0.4),
            # EARNVAR
            "cv_earnings_5y":     rng.lognormal(0, 0.6, n_securities).clip(0.01, 5),
            "cv_cashflows_5y":    rng.lognormal(0, 0.5, n_securities).clip(0.01, 5),
            # PROFIT
            "sales":              rng.lognormal(10, 1.5, n_securities).clip(100),
            "cogs":               rng.lognormal(9.5, 1.5, n_securities).clip(50),
            "earnings":           rng.normal(500, 1000, n_securities),
            # INDUSTRY
            "industry":           industries,
        }, index=tickers)

        # Simulate stock returns with a factor structure
        market_ret   = rng.normal(0.0005, 0.012)   # daily market return
        factor_noise = rng.normal(0, 0.008, n_securities)
        stock_ret    = capm_beta * market_ret + factor_noise
        stock_ret    = pd.Series(stock_ret, index=tickers)

        security_data_by_day[t] = data
        returns_by_day[t]       = stock_ret
        mktcap_by_day[t]        = pd.Series(mktcap, index=tickers)

    return security_data_by_day, returns_by_day, mktcap_by_day


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------

def run_demo():
    from inec1 import (
        ExposureBuilder,
        CrossSectionalRegressor,
        CovarianceEstimator,
        PortfolioRisk,
    )

    N_SECURITIES = 500
    N_DAYS       = 300

    print("=" * 68)
    print("  IEC-1  |  End-to-End Demo  |  Synthetic India Equity Universe")
    print("=" * 68)
    print(f"  Universe : {N_SECURITIES:,} securities   |   {N_DAYS} trading days")
    print()

    # --- Generate synthetic data ---
    print("[1/5] Generating synthetic universe ...", end=" ", flush=True)
    t0 = time.perf_counter()
    sec_data, returns, mktcaps = make_universe(N_SECURITIES, N_DAYS)
    print(f"done  ({time.perf_counter()-t0:.1f}s)")

    # --- Build exposures (sample 5 days for speed; real use: all days) ---
    print("[2/5] Building factor exposures (all days) ...", end=" ", flush=True)
    t0 = time.perf_counter()
    builder    = ExposureBuilder()
    exposures_by_day: dict[int, pd.DataFrame] = {}
    for t in range(N_DAYS):
        exposures_by_day[t] = builder.build(sec_data[t])
    print(f"done  ({time.perf_counter()-t0:.1f}s)")

    # Print exposure sample
    sample_exp = exposures_by_day[N_DAYS - 1].iloc[:5, :7]
    print(f"\n  Exposure matrix sample (day {N_DAYS}, first 5 stocks, first 7 factors):")
    print(sample_exp.to_string())
    print()

    # --- Cross-sectional regression ---
    print("[3/5] Running daily cross-sectional regressions ...", end=" ", flush=True)
    t0 = time.perf_counter()
    regressor = CrossSectionalRegressor(weight_scheme="market_cap")
    for t in range(N_DAYS):
        regressor.fit_day(
            returns=returns[t],
            exposures=exposures_by_day[t],
            market_caps=mktcaps[t],
            date=t,
        )
    factor_returns = regressor.get_factor_return_panel()
    residuals      = regressor.get_residual_panel()
    r2_series      = regressor.get_r_squared_series()
    print(f"done  ({time.perf_counter()-t0:.1f}s)")

    print(f"\n  Average daily R²: {r2_series.mean():.3f}  "
          f"(min {r2_series.min():.3f}, max {r2_series.max():.3f})")
    print(f"\n  Annualised factor return statistics (last 252 days):")
    fr_stats = (factor_returns.tail(252) * 252).describe().loc[["mean", "std"]]
    cols_to_show = ["MARKET", "BETA", "SIZE", "STREV", "LTMOM", "VALUE",
                    "GROWTH", "LOWVOL", "LIQUIDITY", "LEVERAGE", "BANKS", "SOFTWARE"]
    print(fr_stats[cols_to_show].round(4).to_string())
    print()

    # --- Factor covariance matrix + specific risk ---
    print("[4/5] Estimating FCM and specific risk ...", end=" ", flush=True)
    t0 = time.perf_counter()
    cov_est  = CovarianceEstimator(lookback=252, spec_lookback=60, shrinkage="ledoit_wolf")
    fcm      = cov_est.fit_fcm(factor_returns)
    spec_var = cov_est.fit_specific_risk(residuals, min_obs=20)
    print(f"done  ({time.perf_counter()-t0:.1f}s)")

    print(f"\n  FCM shape: {fcm.shape}  |  "
          f"Min eigenvalue: {np.linalg.eigvalsh(fcm.values).min():.6f}  (must be > 0)")
    print(f"  Specific risk — median: {np.sqrt(spec_var.median())*100:.2f}%  "
          f"| 95th pctile: {np.sqrt(spec_var.quantile(0.95))*100:.2f}%\n")

    # --- Portfolio risk & attribution ---
    print("[5/5] Computing portfolio risk attribution ...", end=" ", flush=True)
    t0 = time.perf_counter()

    # Use last-day exposures for the risk model
    last_exposures = exposures_by_day[N_DAYS - 1]
    tickers        = list(last_exposures.index)
    n              = len(tickers)

    # Portfolio: equal-weight, concentrated in first 50 names (active vs cap-weight benchmark)
    port_wts = pd.Series(0.0, index=tickers)
    port_wts.iloc[:50] = 1.0 / 50

    # Benchmark: cap-weight (proportional to market cap)
    mc_last = mktcaps[N_DAYS - 1].reindex(tickers).clip(lower=0)
    bench_wts = mc_last / mc_last.sum()

    risk_engine = PortfolioRisk(
        factor_cov=fcm,
        specific_var=spec_var,
        exposures=last_exposures,
    )

    report = risk_engine.risk_report(
        portfolio_weights=port_wts,
        benchmark_weights=bench_wts,
    )
    print(f"done  ({time.perf_counter()-t0:.1f}s)\n")

    # Print formatted report
    print(risk_engine.format_report(report))

    # Print top active factor bets
    if report["active_factor_exposures"] is not None:
        ae = report["active_factor_exposures"]
        print("\n  Top 5 active factor exposures (portfolio vs benchmark):")
        top = ae.abs().nlargest(5).index
        for f in top:
            print(f"    {f:<16} {ae[f]:+.3f}")

    print("\n  Demo complete.")


if __name__ == "__main__":
    run_demo()
