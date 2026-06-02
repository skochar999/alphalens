#!/usr/bin/env python3
"""
compute_benchmark_metrics.py
============================
Recomputes benchmark_metrics.parquet from nav_monthly.parquet + fund_meta.parquet.

For each fund:
  - Computes monthly returns vs benchmark proxy
  - Annualised return, active return, net active return (after TER)
  - Hit rate (% months beating benchmark)
  - Rolling 12-month active return
  - Information ratio

Gate: fund dropped if < 24 months of overlapping data with its proxy.

Usage:
    python compute_benchmark_metrics.py
    python compute_benchmark_metrics.py --data-dir /path/to/mf_data
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
log = logging.getLogger("fundlens.benchmark")

HERE     = Path(__file__).parent
DATA_DIR = HERE / "mf_data"

MIN_MONTHS     = 24     # minimum history required
BAD_ACTIVE_THR = -0.20  # funds with active_ann < -20pp flagged as bad proxy match


def monthly_to_returns(prices: pd.Series) -> pd.Series:
    """Convert NAV series to monthly % returns."""
    return prices.pct_change().dropna()


def annualise(monthly_ret_series: pd.Series) -> float:
    """Compound monthly returns to annualised figure."""
    n = len(monthly_ret_series)
    if n == 0:
        return np.nan
    compound = (1 + monthly_ret_series).prod()
    return float(compound ** (12 / n) - 1)


def run(data_dir: Path) -> None:
    nav_path  = data_dir / "nav_monthly.parquet"
    meta_path = data_dir / "fund_meta.parquet"
    out_path  = data_dir / "benchmark_metrics.parquet"

    nav  = pd.read_parquet(nav_path)
    meta = pd.read_parquet(meta_path)
    nav.index = pd.DatetimeIndex(nav.index)
    nav.columns = nav.columns.astype(int)

    log.info(f"nav_monthly: {nav.shape[0]} months × {nav.shape[1]} schemes")
    log.info(f"fund_meta:   {len(meta)} funds")

    rows = []
    skipped_history = 0
    skipped_proxy   = 0

    for _, m in meta.iterrows():
        code      = int(m["scheme_code"])
        proxy     = int(m["proxy_code"]) if pd.notna(m["proxy_code"]) else None
        ter       = float(m["ter_est"]) if pd.notna(m["ter_est"]) else 0.0

        if code not in nav.columns:
            continue

        fund_nav = nav[code].dropna()
        fund_ret = monthly_to_returns(fund_nav)

        # ── Adjust from direct plan to regular plan returns ──
        # Our NAV data is from direct plans; ter is the regular plan TER.
        # Regular plan return ≈ direct plan return − TER_premium/12
        # where TER_premium is the distributor trail added on top of direct TER.
        # Index funds: ~0.10%/yr premium; equity active: ~0.85%/yr premium.
        cat_str = str(m.get("category", "")).lower()
        is_index = "index" in cat_str or "etf" in cat_str
        ter_premium = 0.0010 if is_index else 0.0085  # annual premium
        fund_ret = fund_ret - (ter_premium / 12)      # monthly adjustment

        # ── benchmark proxy ──
        has_benchmark = proxy is not None and proxy in nav.columns
        if has_benchmark:
            proxy_nav = nav[proxy].dropna()
            proxy_ret = monthly_to_returns(proxy_nav)
            # align on common dates
            common    = fund_ret.index.intersection(proxy_ret.index)
            fund_ret_c  = fund_ret.loc[common]
            proxy_ret_c = proxy_ret.loc[common]
            n_common  = len(fund_ret_c)
        else:
            fund_ret_c  = fund_ret
            proxy_ret_c = pd.Series(dtype=float)
            n_common    = len(fund_ret_c)

        if n_common < MIN_MONTHS:
            skipped_history += 1
            continue

        # ── metrics ──
        ann_ret       = annualise(fund_ret_c)
        net_ann_ret   = ann_ret - ter

        if has_benchmark and len(proxy_ret_c) >= MIN_MONTHS:
            benchmark_ann_ret = annualise(proxy_ret_c)
            active_monthly    = fund_ret_c - proxy_ret_c
            active_ann        = annualise(active_monthly)    # compound active
            net_active_ann    = active_ann - ter
            hit_rate          = float((active_monthly > 0).mean())

            # Rolling 12-month active (last 12 months of aligned data)
            if len(active_monthly) >= 12:
                rolling_12m_active = float(annualise(active_monthly.iloc[-12:]))
            else:
                rolling_12m_active = active_ann

            # IR: annualised active / annualised tracking error
            te = float(active_monthly.std() * np.sqrt(12))
            active_ir = float(active_ann / te) if te > 0 else np.nan

            # Flag bad proxy matches (e.g. international funds vs Nifty 500)
            if active_ann < BAD_ACTIVE_THR:
                has_benchmark = False
                active_ann = net_active_ann = benchmark_ann_ret = np.nan
                hit_rate = active_ir = rolling_12m_active = np.nan
        else:
            benchmark_ann_ret = np.nan
            active_ann = net_active_ann = np.nan
            hit_rate   = active_ir = rolling_12m_active = np.nan
            has_benchmark = False

        rows.append({
            "scheme_code":        code,
            "scheme_name":        m.get("scheme_name", ""),
            "amc":                m.get("amc", ""),
            "category":           m.get("category", ""),
            "proxy_code":         proxy,
            "n_months":           n_common,
            "ann_ret":            ann_ret,
            "net_ann_ret":        net_ann_ret,
            "benchmark_ann_ret":  benchmark_ann_ret,
            "active_ann":         active_ann,
            "net_active_ann":     net_active_ann,
            "hit_rate":           hit_rate,
            "rolling_12m_active": rolling_12m_active,
            "active_ir":          active_ir,
            "ter_est":            ter,
            "has_benchmark":      has_benchmark,
        })

    df = pd.DataFrame(rows)
    df.to_parquet(out_path, index=False)

    log.info(f"benchmark_metrics: {len(df)} funds saved  "
             f"(skipped {skipped_history} for <{MIN_MONTHS}m history, "
             f"{skipped_proxy} for no proxy)")
    log.info(f"  Mean active return:   {df['active_ann'].mean()*100:.2f}pp/yr")
    log.info(f"  Mean hit rate:        {df['hit_rate'].mean()*100:.1f}%")
    log.info(f"  With benchmark:       {df['has_benchmark'].sum()}")


def main() -> None:
    p = argparse.ArgumentParser(description="Recompute benchmark-relative metrics")
    p.add_argument("--data-dir", default=str(DATA_DIR))
    args = p.parse_args()
    run(Path(args.data_dir))


if __name__ == "__main__":
    main()
