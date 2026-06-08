#!/usr/bin/env python3
"""
check_freshness.py — AlphaLens data freshness check.

Reports whether the data behind the live site is current: latest NAVs, the
IEC-1 factor-returns feed, the scored table, and what the live API is actually
serving. Prints a short ✅ / ⚠️ report and exits non-zero if anything is stale,
so a scheduled task can surface it.

Usage:
    python3 check_freshness.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

import pandas as pd

HERE  = Path(__file__).parent
MF    = HERE / "mf_data"
INEC1 = HERE / "inec1_outputs"
API_HEALTH = "https://alphalens-production-21b7.up.railway.app/health"

# Staleness thresholds (calendar days). Generous enough to absorb weekends.
NAV_MAX_AGE    = 4    # nav_monthly written within last 4 days
FACTOR_MAX_AGE = 8    # factor-returns feed within last 8 days
API_MAX_AGE    = 3    # live API loaded data within last 3 days

issues: list[str] = []
lines:  list[str] = []


def _age(d: date) -> int:
    return (date.today() - d).days


def _mtime(p: Path) -> date:
    return datetime.fromtimestamp(p.stat().st_mtime).date()


# 1 ── NAV freshness (nav_monthly uses month-end labels, so use file mtime) ──
try:
    nav = pd.read_parquet(MF / "nav_monthly.parquet")
    meta = pd.read_parquet(MF / "fund_meta.parquet")
    reg = [c for c in nav.columns if int(c) in set(meta["scheme_code"].astype(int))]
    latest = nav.iloc[-1][reg]
    have, total = int(latest.notna().sum()), len(reg)
    missing = total - have
    age = _age(_mtime(MF / "nav_monthly.parquet"))
    flag = age > NAV_MAX_AGE
    lines.append(f"{'⚠️' if flag else '✅'} NAV data: updated {age}d ago "
                 f"({have}/{total} schemes in latest month; {missing} missing)")
    if flag:
        issues.append(f"NAV data is {age} days old (threshold {NAV_MAX_AGE})")
    if missing > total * 0.1:
        issues.append(f"{missing} schemes missing a recent NAV")
except Exception as e:
    issues.append(f"NAV check failed: {e}")
    lines.append(f"⚠️ NAV data: check failed — {e}")

# 2 ── Factor-returns feed (real dated index) ──
try:
    fr = pd.read_parquet(INEC1 / "factor_returns_history.parquet")
    last_factor = pd.to_datetime(fr.index.max()).date()
    age = _age(last_factor)
    flag = age > FACTOR_MAX_AGE
    lines.append(f"{'⚠️' if flag else '✅'} Factor feed: last {last_factor} ({age}d ago)")
    if flag:
        issues.append(f"Factor-returns feed is {age} days stale (last {last_factor}) "
                      f"— run_daily.py may not be running")
except Exception as e:
    issues.append(f"Factor feed check failed: {e}")
    lines.append(f"⚠️ Factor feed: check failed — {e}")

# 3 ── Scored table ──
try:
    sf = pd.read_parquet(MF / "scored_funds.parquet")
    n, n_null = len(sf), int(sf["total_score"].isna().sum())
    age = _age(_mtime(MF / "scored_funds.parquet"))
    flag = n_null > 0
    lines.append(f"{'⚠️' if flag else '✅'} Scores: {n} funds, "
                 f"{n_null} unscored, rebuilt {age}d ago")
    if n_null > 0:
        issues.append(f"{n_null} funds have no score")
except Exception as e:
    issues.append(f"Score check failed: {e}")
    lines.append(f"⚠️ Scores: check failed — {e}")

# 4 ── Live API (what alphapicker.in actually serves) ──
try:
    with urllib.request.urlopen(f"{API_HEALTH}?cb={datetime.now().timestamp()}", timeout=15) as r:
        h = json.loads(r.read().decode())
    api_funds = h.get("funds")
    api_loaded = datetime.fromisoformat(h["loaded_at"]).date()
    age = _age(api_loaded)
    flag = age > API_MAX_AGE
    lines.append(f"{'⚠️' if flag else '✅'} Live API: serving {api_funds} funds, "
                 f"loaded {age}d ago")
    if flag:
        issues.append(f"Live API data is {age} days old — deploy/push may have stalled")
except Exception as e:
    issues.append(f"Live API unreachable: {e}")
    lines.append(f"⚠️ Live API: unreachable — {e}")

# ── Report ──
print("AlphaLens freshness —", date.today().isoformat())
print("\n".join(lines))
if issues:
    print("\n⚠️ ACTION NEEDED:")
    for i in issues:
        print(f"  - {i}")
    sys.exit(1)
print("\n✅ All systems current.")
sys.exit(0)
