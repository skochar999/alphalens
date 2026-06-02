#!/usr/bin/env python3
"""
run_monthly_update.py
=====================
Orchestrates the full FundLens monthly refresh:

  Step 0  fetch_new_holdings.py     — download latest portfolio disclosures
                                      from Mirae Asset (AjaxService API) and
                                      HDFC (S3 URL pattern), then ingest into
                                      holdings/{YYYY-MM}.parquet
  Step 1  update_nav_monthly.py     — fetch new month-end NAVs from mfapi.in
  Step 2  holdings_attribution.py   — recompute holdings-based attribution
  Step 3  compute_benchmark_metrics.py  — recompute benchmark-relative metrics
  Step 4  compute_scored_funds.py   — rescore all funds + decomposition
  Step 5  build_fundlens_v3.py      — rebuild HTML dashboard

Run this on the 15th of each month (holdings data is typically published
by AMCs within the first 10 days of the following month).

Usage:
    python run_monthly_update.py
    python run_monthly_update.py --skip-fetch     # skip Step 0 (holdings download)
    python run_monthly_update.py --skip-nav       # skip Step 1 (NAV fetch)
    python run_monthly_update.py --skip-holdings  # skip Step 2 (holdings attr)
    python run_monthly_update.py --from-step 2    # start from step 2
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
log = logging.getLogger("fundlens.monthly")

HERE     = Path(__file__).parent
DATA_DIR = HERE / "mf_data"

STEPS = {
    0: ("Fetch AMC holdings",      "fetch_new_holdings.py"),
    1: ("Fetch NAV data",          "update_nav_monthly.py"),
    2: ("Recompute holdings attr", "holdings_attribution.py"),
    3: ("Recompute bench metrics", "compute_benchmark_metrics.py"),
    4: ("Rescore all funds",       "compute_scored_funds.py"),
    5: ("Rebuild HTML",            "build_fundlens_v3.py"),
}


def run_step(script: str, extra_args: list[str] | None = None) -> bool:
    cmd = [sys.executable, str(HERE / script)] + (extra_args or [])
    log.info("  $ " + " ".join(cmd))
    t0 = time.time()
    result = subprocess.run(cmd, cwd=str(HERE))
    elapsed = time.time() - t0
    if result.returncode != 0:
        log.error(f"  FAILED (exit {result.returncode}) after {elapsed:.1f}s")
        return False
    log.info(f"  ✓ Done in {elapsed:.1f}s")
    return True


# Every file the pipeline depends on, keyed by the step that produces it
# and the step that needs it. Checked upfront so nothing silently skips.
REQUIRED_FILES = {
    "fund_meta.parquet":          "Step 1 input  — scheme + proxy mapping (run benchmark setup to regenerate)",
    "nav_monthly.parquet":        "Step 1 output — produced by update_nav_monthly.py",
    "holdings_attribution.parquet": "Step 2 output — produced by holdings_attribution.py",
    "benchmark_metrics.parquet":  "Step 3 output — produced by compute_benchmark_metrics.py",
    "fund_metrics.parquet":       "Step 4 input  — regression-based factor model output (from IEC-1 backfill)",
    "holdings_alpha.parquet":     "Step 4 input  — holdings-based attribution (from holdings_attribution.py)",
    "scored_funds.parquet":       "Step 4 output — produced by compute_scored_funds.py",
}

# Which step PRODUCES each file (so we know what to re-run if it's missing)
PRODUCED_BY = {
    "nav_monthly.parquet":          1,
    "holdings_attribution.parquet": 2,
    "benchmark_metrics.parquet":    3,
    "scored_funds.parquet":         4,
    "fundlens_v3.html":             5,
}

# Which step REQUIRES each file as input (blocks execution if absent)
REQUIRED_BY = {
    "fund_meta.parquet":          1,   # needed by NAV fetch and all downstream
    "nav_monthly.parquet":        3,   # needed by benchmark metrics
    "holdings_alpha.parquet":     4,   # needed by scorer
    "fund_metrics.parquet":       4,   # needed by scorer
    "benchmark_metrics.parquet":  4,   # needed by scorer
    "scored_funds.parquet":       5,   # needed by HTML builder
}


def check_all_files(data_dir: Path) -> tuple[list[str], list[str]]:
    """
    Check every file the pipeline needs.
    Returns (present, missing) lists with descriptive messages.
    """
    present = []
    missing = []
    for fname, description in REQUIRED_FILES.items():
        path = data_dir / fname
        if path.exists():
            size_kb = path.stat().st_size // 1024
            present.append(f"  ✓  {fname}  ({size_kb} KB)")
        else:
            missing.append(f"  ✗  MISSING: {fname}\n         → {description}")
    # Also check the scripts themselves
    for _, script in STEPS.values():
        spath = HERE / script
        if not spath.exists():
            missing.append(f"  ✗  MISSING SCRIPT: {script}\n         → Required to run pipeline step")
    return present, missing


def main() -> int:
    p = argparse.ArgumentParser(description="FundLens monthly update pipeline")
    p.add_argument("--skip-fetch",    action="store_true", help="Skip step 0 (AMC holdings download)")
    p.add_argument("--skip-nav",      action="store_true", help="Skip step 1 (NAV fetch)")
    p.add_argument("--skip-holdings", action="store_true", help="Skip step 2 (holdings attr)")
    p.add_argument("--from-step",     type=int, default=0, choices=range(0, 6),
                   help="Start from this step number (0-5)")
    p.add_argument("--data-dir",      default=str(DATA_DIR))
    args = p.parse_args()
    data_dir = Path(args.data_dir)

    log.info("=" * 60)
    log.info("  FundLens — Monthly Update Pipeline")
    log.info(f"  Data dir : {data_dir}")
    log.info(f"  From step: {args.from_step}")
    log.info(f"  Steps:     0=fetch_holdings  1=nav  2=attr  3=bench  4=score  5=html")
    log.info("=" * 60)

    # ── Upfront file audit ──────────────────────────────────────────────
    log.info("\nFile check:")
    present, missing = check_all_files(data_dir)
    for line in present:
        log.info(line)
    if missing:
        log.error("\n  ⚠️  MISSING FILES DETECTED:")
        for line in missing:
            log.error(line)
        # Decide whether any missing file is a hard blocker for the requested steps
        hard_blocked = False
        for fname, step_needed in REQUIRED_BY.items():
            if step_needed >= args.from_step:
                path = data_dir / fname
                if not path.exists() and PRODUCED_BY.get(fname, 0) < args.from_step:
                    # File is needed but won't be produced earlier in this run
                    log.error(f"\n  FATAL: {fname} is needed by step {step_needed} "
                              f"and won't be produced in this run. "
                              f"Re-run from step {PRODUCED_BY.get(fname, '?')} or restore the file.")
                    hard_blocked = True
        if hard_blocked:
            log.error("\n  Pipeline aborted. Fix missing files then re-run.")
            return 2
        else:
            log.warning("\n  Some files missing but they will be produced during this run. Continuing.")
    else:
        log.info("\n  All files present ✓")

    data_args = ["--data-dir", str(data_dir)]
    failures  = []

    for step_num, (label, script) in STEPS.items():
        if step_num < args.from_step:
            continue

        # Apply skip flags
        if step_num == 0 and args.skip_fetch:
            log.info(f"\n[{step_num}/5] {label} — SKIPPED (--skip-fetch)")
            continue
        if step_num == 1 and args.skip_nav:
            log.info(f"\n[{step_num}/5] {label} — SKIPPED (--skip-nav)")
            continue
        if step_num == 2 and args.skip_holdings:
            log.info(f"\n[{step_num}/5] {label} — SKIPPED (--skip-holdings)")
            continue

        log.info(f"\n[{step_num}/5] {label} …")

        # Hard prerequisite check immediately before each step
        step_prereqs = {
            0: [],   # fetch_new_holdings needs no parquet prereqs
            1: ["fund_meta.parquet"],
            2: ["nav_monthly.parquet", "fund_meta.parquet"],
            3: ["nav_monthly.parquet", "fund_meta.parquet"],
            4: ["benchmark_metrics.parquet"],
            5: ["scored_funds.parquet"],
        }
        missing_now = [f for f in step_prereqs.get(step_num, []) if not (data_dir / f).exists()]
        if missing_now:
            for f in missing_now:
                log.error(f"  ✗  MISSING prerequisite for step {step_num}: {f}")
                log.error(f"     → {REQUIRED_FILES.get(f, 'See pipeline docs')}")
            log.error(f"  Step {step_num} SKIPPED due to missing files. Fix and re-run with --from-step {step_num}")
            failures.append(f"step_{step_num}_missing_prereqs")
            continue

        ok = run_step(script, data_args)
        if not ok:
            failures.append(f"step_{step_num}_{label.replace(' ', '_')}")

    # ── Final file audit ────────────────────────────────────────────────
    log.info("\n" + "=" * 60)
    log.info("Final file state:")
    present2, missing2 = check_all_files(data_dir)
    for line in present2:
        log.info(line)
    if missing2:
        log.error("  Files still missing after pipeline run:")
        for line in missing2:
            log.error(line)

    if failures:
        log.error(f"\n  Pipeline finished with {len(failures)} failure(s): {', '.join(failures)}")
        return 1
    elif missing2:
        log.warning(f"\n  Pipeline ran but {len(missing2)} file(s) still missing — check logs above.")
        return 1
    else:
        log.info("\n  AlphaLens monthly update — ALL STEPS COMPLETE ✓")
        out_html = HERE / "alphalens.html"
        if out_html.exists():
            log.info(f"  Dashboard: {out_html}")
        _notify_api()
        return 0


def _notify_api() -> None:
    """Tell the FastAPI backend to reload its cache with fresh data."""
    import os, urllib.request, urllib.error
    api_url = os.getenv("ALPHALENS_API_URL", "")
    secret  = os.getenv("RELOAD_SECRET", "")
    if not api_url:
        log.info("ALPHALENS_API_URL not set — skipping API reload notification")
        return
    url = f"{api_url.rstrip('/')}/reload"
    if secret:
        url += f"?secret={secret}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            log.info(f"API reloaded: {r.read().decode()}")
    except urllib.error.URLError as e:
        log.warning(f"Could not notify API at {url}: {e}")


if __name__ == "__main__":
    sys.exit(main())
