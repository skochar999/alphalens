#!/usr/bin/env python3
"""
run_mf_pipeline.py

Full mutual fund analytics pipeline runner.

Steps:
  1. nav_fetcher.py      — Download 3yr monthly NAV history (mfapi.in)
  2. holdings_fetcher.py — Download 3yr AMFI monthly holdings (amfiindia.com)
  3. attribution.py      — Decompose returns: Market / Style / Industry / Alpha
  4. scorer.py           — Score each fund 0–100 on outperformance likelihood
  5. build_mf_website.py — Build self-contained HTML analytics website

Usage:
    python run_mf_pipeline.py                         # full run
    python run_mf_pipeline.py --skip-nav              # skip re-fetching NAV (already cached)
    python run_mf_pipeline.py --skip-holdings         # skip re-fetching AMFI holdings
    python run_mf_pipeline.py --from attribution      # start from step 3
    python run_mf_pipeline.py --backtest              # include scorer backtest
    python run_mf_pipeline.py --force                 # force re-run of all steps

    Typical first run (downloads everything, ~5–15 min):
        python run_mf_pipeline.py

    Daily/weekly refresh (uses cached data, just re-scores):
        python run_mf_pipeline.py --skip-nav --skip-holdings
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mf.pipeline")

HERE = Path(__file__).parent
MF_DATA    = HERE / "mf_data"
IEC1_DIR  = HERE / "inec1_outputs"
MF_WEBSITE = HERE / "mf_website.html"

STEPS = ["nav", "holdings", "attribution", "scorer", "website"]

STEP_SCRIPTS = {
    "nav":         [sys.executable, str(HERE / "mf_analytics" / "nav_fetcher.py")],
    "holdings":    [sys.executable, str(HERE / "mf_analytics" / "holdings_fetcher.py")],
    "attribution": [sys.executable, str(HERE / "mf_analytics" / "attribution.py")],
    "scorer":      [sys.executable, str(HERE / "mf_analytics" / "scorer.py")],
    "website":     [sys.executable, str(HERE / "build_mf_website.py")],
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="MF analytics pipeline runner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--mf-data",       default=str(MF_DATA))
    p.add_argument("--inec1-dir",     default=str(IEC1_DIR))
    p.add_argument("--skip-nav",      action="store_true", help="Skip NAV download")
    p.add_argument("--skip-holdings", action="store_true", help="Skip AMFI holdings download")
    p.add_argument("--from",          dest="from_step", default="nav",
                   choices=STEPS, help="Start from this step (skips earlier ones)")
    p.add_argument("--lookback-months", type=int, default=36)
    p.add_argument("--window",        type=int, default=12,
                   help="Rolling window (months) for attribution and scoring")
    p.add_argument("--backtest",      action="store_true")
    p.add_argument("--force",         action="store_true")
    p.add_argument("--out",           default=str(MF_WEBSITE),
                   help="Output path for website HTML")
    return p.parse_args()


def _run(cmd: list[str], extra_args: list[str] | None = None) -> bool:
    """Run a subprocess command, stream output, return True on success."""
    full = cmd + (extra_args or [])
    log.info("  $ " + " ".join(full))
    t0 = time.time()
    result = subprocess.run(full, cwd=str(HERE))
    elapsed = time.time() - t0
    if result.returncode != 0:
        log.error(f"  FAILED (exit {result.returncode}) in {elapsed:.1f}s")
        return False
    log.info(f"  ✓ Done in {elapsed:.1f}s")
    return True


def main() -> int:
    args = _parse_args()
    mf_data  = Path(args.mf_data)
    inec1dir = Path(args.inec1_dir)

    force_flag = ["--force"] if args.force else []
    from_idx   = STEPS.index(args.from_step)

    log.info("=" * 60)
    log.info("  MF Alpha — Full Analytics Pipeline")
    log.info(f"  Data dir  : {mf_data}")
    log.info(f"  IEC1 dir : {inec1dir}")
    log.info(f"  Lookback  : {args.lookback_months} months")
    log.info(f"  Window    : {args.window} months")
    log.info("=" * 60)

    failures = []

    # ------------------------------------------------------------------ #
    # Step 1: NAV fetch
    # ------------------------------------------------------------------ #
    if from_idx <= STEPS.index("nav") and not args.skip_nav:
        log.info("\n[1/5] Fetching NAV history …")
        ok = _run(STEP_SCRIPTS["nav"], [
            "--output-dir", str(mf_data),
            "--lookback-months", str(args.lookback_months),
        ] + force_flag)
        if not ok:
            failures.append("nav_fetcher")
    else:
        log.info("[1/5] NAV fetch skipped.")

    # ------------------------------------------------------------------ #
    # Step 2: Holdings fetch
    # ------------------------------------------------------------------ #
    if from_idx <= STEPS.index("holdings") and not args.skip_holdings:
        # Check scheme list exists before running holdings fetcher
        if not (mf_data / "scheme_list.parquet").exists():
            log.warning("[2/5] scheme_list.parquet not found — skipping holdings (run nav_fetcher first)")
        else:
            log.info("\n[2/5] Fetching AMFI monthly holdings …")
            ok = _run(STEP_SCRIPTS["holdings"], [
                "--output-dir", str(mf_data),
                "--lookback-months", str(args.lookback_months),
            ] + force_flag)
            if not ok:
                log.warning("  Holdings fetch failed — continuing without holdings data")
    else:
        log.info("[2/5] Holdings fetch skipped.")

    # ------------------------------------------------------------------ #
    # Step 3: Attribution
    # ------------------------------------------------------------------ #
    if from_idx <= STEPS.index("attribution"):
        # Check prerequisites
        missing = []
        if not (mf_data / "returns_monthly.parquet").exists():
            missing.append("returns_monthly.parquet (run nav_fetcher first)")
        if not (inec1dir / "factor_returns_history.parquet").exists():
            missing.append("factor_returns_history.parquet (run backfill.py first)")
        if missing:
            log.error(f"[3/5] Attribution prerequisites missing:\n  " + "\n  ".join(missing))
            failures.append("attribution")
        else:
            log.info("\n[3/5] Running monthly attribution …")
            ok = _run(STEP_SCRIPTS["attribution"], [
                "--mf-data", str(mf_data),
                "--inec1-dir", str(inec1dir),
                "--window", str(args.window),
            ] + force_flag)
            if not ok:
                failures.append("attribution")

    # ------------------------------------------------------------------ #
    # Step 4: Scoring
    # ------------------------------------------------------------------ #
    if from_idx <= STEPS.index("scorer"):
        if not (mf_data / "attribution.parquet").exists():
            log.error("[4/5] attribution.parquet missing — skipping scorer")
            failures.append("scorer")
        else:
            log.info("\n[4/5] Computing fund scores …")
            bt_flag = ["--backtest"] if args.backtest else []
            ok = _run(STEP_SCRIPTS["scorer"], [
                "--mf-data", str(mf_data),
                "--window", str(args.window),
            ] + bt_flag + force_flag)
            if not ok:
                failures.append("scorer")

    # ------------------------------------------------------------------ #
    # Step 5: Website
    # ------------------------------------------------------------------ #
    if from_idx <= STEPS.index("website"):
        if not (mf_data / "scores.parquet").exists():
            log.error("[5/5] scores.parquet missing — skipping website build")
            failures.append("website")
        else:
            log.info("\n[5/5] Building analytics website …")
            ok = _run(STEP_SCRIPTS["website"], [
                "--mf-data", str(mf_data),
                "--out", args.out,
            ])
            if not ok:
                failures.append("website")
            else:
                log.info(f"\n  Open: {args.out}")

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    log.info("\n" + "=" * 60)
    if failures:
        log.error(f"  Pipeline finished with failures: {', '.join(failures)}")
        log.info("=" * 60)
        return 1
    else:
        log.info("  MF Analytics Pipeline — ALL STEPS COMPLETE ✓")
        log.info(f"  Website: {args.out}")
        log.info("=" * 60)
        return 0


if __name__ == "__main__":
    sys.exit(main())
