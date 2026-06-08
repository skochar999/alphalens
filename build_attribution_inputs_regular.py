#!/usr/bin/env python3
"""
build_attribution_inputs_regular.py

mf_analytics/attribution.py reads two inputs that the monthly pipeline does NOT
maintain: returns_monthly.parquet (monthly return matrix) and
scheme_list.parquet (scheme metadata). They were built for the old DIRECT
universe and never updated for the regular-plan switch, so the factor model
kept regenerating direct-coded output.

This rebuilds both from the regular-plan data we DO maintain:
  - returns_monthly.parquet  ← monthly % returns derived from nav_monthly.parquet,
                               filtered to regular scheme codes
  - scheme_list.parquet      ← scheme_code / scheme_name / amc from fund_meta.parquet

After running this, run:
    python3 mf_analytics/attribution.py --force --mf-data mf_data --inec1-dir inec1_outputs

Usage:
    python3 build_attribution_inputs_regular.py [--data-dir mf_data] [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("alphapicker.attr_inputs")

HERE = Path(__file__).parent

# Match the factor-returns window so the regression has overlapping dates.
START_MONTH = "2023-06-30"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default=None)
    p.add_argument("--mf-data",  default=str(HERE / "mf_data"))
    p.add_argument("--dry-run",  action="store_true")
    args = p.parse_args()

    mf = Path(args.data_dir or args.mf_data)
    nav_path  = mf / "nav_monthly.parquet"
    meta_path = mf / "fund_meta.parquet"
    for fp in (nav_path, meta_path):
        if not fp.exists():
            log.error(f"Missing file: {fp}")
            return

    nav  = pd.read_parquet(nav_path)
    meta = pd.read_parquet(meta_path)
    nav.index = pd.to_datetime(nav.index)
    nav = nav.sort_index()

    reg = set(meta["scheme_code"].astype(int))
    keep = [c for c in nav.columns if int(c) in reg]
    log.info(f"nav_monthly: {nav.shape[1]} cols → {len(keep)} regular schemes kept")

    # ── Monthly returns from month-end NAV levels ────────────────────────
    navr = nav[keep].astype(float)
    rets = navr.pct_change()
    rets = rets.replace([np.inf, -np.inf], np.nan)
    rets = rets.loc[rets.index >= START_MONTH]
    rets = rets.dropna(axis=1, how="all")
    rets.columns = [int(c) for c in rets.columns]

    ge12 = int((rets.notna().sum() >= 12).sum())
    log.info(f"returns_monthly: {rets.shape[0]} months × {rets.shape[1]} schemes "
             f"({rets.index[0].date()} → {rets.index[-1].date()}); "
             f"{ge12} schemes with ≥12 months")

    # ── Scheme list from fund_meta ───────────────────────────────────────
    cols = [c for c in ["scheme_code", "scheme_name", "amc", "isin"] if c in meta.columns]
    scheme_list = meta[cols].copy()
    scheme_list["scheme_code"] = scheme_list["scheme_code"].astype(int)
    scheme_list = scheme_list.drop_duplicates(subset="scheme_code")
    log.info(f"scheme_list: {len(scheme_list)} schemes")

    if args.dry_run:
        log.info("Dry run — files NOT written.")
        return

    for name, df in [("returns_monthly.parquet", rets),
                     ("scheme_list.parquet", scheme_list)]:
        path = mf / name
        if path.exists():
            shutil.copy(path, mf / name.replace(".parquet", "_direct_backup.parquet"))
        df.to_parquet(path)
    log.info("✓ Wrote returns_monthly.parquet + scheme_list.parquet (direct versions backed up)")


if __name__ == "__main__":
    main()
