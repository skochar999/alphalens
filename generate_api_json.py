#!/usr/bin/env python3
"""
generate_api_json.py

Exports fund scores to api/data/funds.json and api/data/stats.json
for the Railway-hosted FastAPI backend.

Run after run_monthly_update.py completes successfully.

Usage:
    python3 generate_api_json.py
    python3 generate_api_json.py --mf-data ./mf_data --api-data ./api/data
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("alphapicker.export")

HERE = Path(__file__).parent


def _pct(v, d=1):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return round(float(v) * 100, d)


def _f(v, d=2):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return round(float(v), d)


def _r0(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return int(round(float(v)))


def build_funds(scores: pd.DataFrame) -> list:
    funds = []
    for _, r in scores.iterrows():
        funds.append({
            "code":     int(r["scheme_code"]) if pd.notna(r.get("scheme_code")) else None,
            "name":     str(r.get("scheme_name", "")),
            "amc":      str(r.get("amc", "")).replace("_", " "),
            "cat":      str(r.get("category_display", r.get("category", ""))),
            "score":    _f(r.get("total_score"), 1),
            "aret":     _pct(r.get("net_active_ann") or r.get("active_ann"), 1),
            "hrate":    _r0((r.get("hit_rate") or 0) * 100),
            "pickAnn":  _pct(r.get("pick_ann_pp") or r.get("d_pick"), 1),
            "ter":      _pct(r.get("ter_est"), 2),
            "ret":      _pct(r.get("ann_ret") or r.get("net_ann_ret") or r.get("total_return_ann"), 1),
            "navOnly":  bool(r["nav_only"]) if pd.notna(r.get("nav_only")) else True,
            "decomp":   bool(r.get("decomp_ok", False)),
            "dStyle":   _f(r.get("d_style"), 1),
            "dSector":  _f(r.get("d_sector"), 1),
            "dPick":    _f(r.get("d_pick"), 1),
            "dTiming":  _f(r.get("d_timing"), 1),
        })
    return funds


def build_stats(scores: pd.DataFrame) -> dict:
    active = scores[scores.get("category_display", pd.Series(dtype=str)) != "Index Funds"] \
        if "category_display" in scores.columns else scores

    n_funds = len(active)
    pct_pos = round(float((active["net_active_ann"] > 0).mean() * 100)) \
        if "net_active_ann" in active.columns else 0
    avg_ret = round(float(active["ann_ret"].mean() * 100), 1) \
        if "ann_ret" in active.columns else 0

    cat_beat = {}
    if "category_display" in active.columns and "net_active_ann" in active.columns:
        for cat, g in active.groupby("category_display"):
            total = len(g)
            beat = int((g["net_active_ann"] > 0).sum())
            if total >= 5:
                cat_beat[cat] = {"total": total, "beat": beat,
                                 "pct": round(beat / total * 100)}

    return {
        "n_funds":       n_funds,
        "pct_pos_alpha": pct_pos,
        "avg_total_ann": avg_ret,
        "cat_beat":      cat_beat,
        "methodology":   _methodology(),
    }


def _methodology() -> dict:
    """User-facing methodology notes. Served at /api/stats so the frontend can
    render them without hardcoding copy. Keep the numeric thresholds in sync
    with compute_scored_funds.py (MIN_PICK_MONTHS, MONTHLY_PICK_CLIP)."""
    return {
        "universe": (
            "We cover active, regular-plan, growth-option mutual fund schemes "
            "across equity, hybrid and solution-oriented categories. Index "
            "funds, ETFs, fund-of-funds, passive and arbitrage funds are "
            "excluded, since they aren't trying to pick stocks."
        ),
        "stock_picking_history": (
            "A fund is rated on stock-picking skill only once it has at least "
            "12 months of disclosed monthly holdings. Until then it's scored "
            "on returns-based measures alone and labelled accordingly, so a "
            "few lucky months on a new fund can't masquerade as skill."
        ),
        "min_holdings_months": 12,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Export fund data to API JSON files")
    # --data-dir is the canonical flag the pipeline passes (points at mf_data/).
    p.add_argument("--data-dir", default=None, help="Path to mf_data directory")
    p.add_argument("--mf-data",  default=str(HERE / "mf_data"))
    p.add_argument("--api-data", default=str(HERE / "api" / "data"))
    args = p.parse_args()

    mf_data  = Path(args.data_dir or args.mf_data)
    api_data = Path(args.api_data)
    api_data.mkdir(parents=True, exist_ok=True)

    # Load scored_funds.parquet
    scores_path = (mf_data / "scored_funds.parquet"
                   if (mf_data / "scored_funds.parquet").exists()
                   else mf_data / "scores.parquet")

    if not scores_path.exists():
        log.error(f"No scores file found at {scores_path}")
        return 1

    log.info(f"Loading {scores_path} …")
    scores = pd.read_parquet(scores_path)
    log.info(f"  {len(scores)} funds loaded")

    # Build funds list and stats
    funds = build_funds(scores)
    stats = build_stats(scores)

    # Sort by score descending
    funds.sort(key=lambda x: (x.get("score") is None, -(x.get("score") or 0)))

    # Write JSON files
    funds_out = api_data / "funds.json"
    stats_out = api_data / "stats.json"

    funds_out.write_text(json.dumps(funds, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    stats_out.write_text(json.dumps(stats, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")

    log.info(f"✓ {len(funds)} funds → {funds_out}")
    log.info(f"✓ stats → {stats_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
