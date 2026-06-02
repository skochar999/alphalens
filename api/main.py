"""
AlphaLens FastAPI Backend
=========================
Serves fund rankings, stats, and attribution data from parquet files.
Data is cached in memory and auto-reloads when parquets are updated.

Endpoints:
  GET /api/funds              — all ranked active funds
  GET /api/funds/{code}       — single fund detail
  GET /api/attribution/{code} — monthly attribution history for a fund
  GET /api/stats              — summary stats (hero section data)
  GET /api/categories         — list of categories with fund counts
  GET /api/amcs               — list of AMCs with fund counts
  GET /reload                 — force cache reload (called by daily pipeline)
  GET /health                 — health check + data freshness
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("alphalens.api")

app = FastAPI(title="AlphaLens API", version="1.0.0")

# Allow any origin (tighten once frontend URL is known)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Data directory — override with DATA_DIR env var in production
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.getenv("DATA_DIR", "./mf_data"))

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------
_cache: dict[str, Any] = {
    "funds":       None,   # list[dict] — ranked active funds
    "attr":        None,   # dict[str, list[dict]] — attribution by scheme_code
    "stats":       None,   # dict — hero stats
    "loaded_at":   None,   # datetime
    "parquet_mtime": 0.0,  # mtime of scored_funds.parquet at last load
}

INTL_PAT = (
    r"taiwan|japan|nasdaq|s&p 500|dow jones|asean|europe|china|"
    r"korea|global brand|hang seng|brazil|vietnam|us bluechip|"
    r"us equity|emerging market|offshore|world health|msci"
)


def _clean_name(name: str) -> str:
    """Strip direct plan / growth suffixes and fix ALL-CAPS words."""
    import re
    n = re.sub(r"\s*[-–]\s*Direct Plan\s*[-–]?\s*(Growth Option|Growth|IDCW)?\s*$", "", name, flags=re.I)
    n = re.sub(r"\s*[-–]\s*Direct\s*[-–]?\s*(Growth Option|Growth|IDCW)?\s*$", "", n, flags=re.I)
    n = re.sub(r"\s*[-–]?\s*(Growth Option|Growth Plan|Growth)\s*$", "", n, flags=re.I)
    n = re.sub(r"\s*[-–]\s*Direct\s*$", "", n, flags=re.I)
    return n.strip()


def _load_data() -> None:
    """Load all parquets into the in-memory cache."""
    t0 = time.time()
    sf_path  = DATA_DIR / "scored_funds.parquet"
    hat_path = DATA_DIR / "holdings_attribution.parquet"
    bm_path  = DATA_DIR / "benchmark_metrics.parquet"

    if not sf_path.exists():
        log.error(f"scored_funds.parquet not found in {DATA_DIR}")
        return

    # ── Scored funds ──────────────────────────────────────────────────────
    sf = pd.read_parquet(sf_path)

    # Filters (mirrors build_fundlens.py logic)
    excl = sf["scheme_name"].str.lower().str.contains(r"gold|silver", na=False)
    excl |= sf["scheme_name"].str.lower().str.contains(INTL_PAT, na=False)
    if "category_display" in sf.columns:
        excl |= sf["category_display"] == "Index Funds"
    sf = sf[~excl].copy()

    # Merge benchmark metrics for net_active_ann / hit_rate
    if bm_path.exists():
        bm = pd.read_parquet(bm_path)[
            ["scheme_code", "net_active_ann", "hit_rate", "ann_ret",
             "benchmark_ann_ret", "active_ann", "n_months"]
        ]
        sf = sf.merge(bm, on="scheme_code", how="left", suffixes=("", "_bm"))

    def _f(v, d=2):
        if v is None or (isinstance(v, float) and np.isnan(v)): return None
        return round(float(v), d)

    def _pct(v, d=1):
        if v is None or (isinstance(v, float) and np.isnan(v)): return None
        return round(float(v) * 100, d)

    funds = []
    for _, r in sf.iterrows():
        # TER premium adjustment for regular plan
        ter = float(r.get("ter_est", 0) or 0)

        funds.append({
            "code":      int(r["scheme_code"]),
            "name":      _clean_name(str(r.get("scheme_name", ""))),
            "name_raw":  str(r.get("scheme_name", "")),
            "amc":       str(r.get("amc", "")).replace("_", " "),
            "cat":       str(r.get("category_display", r.get("category", ""))),
            "score":     _f(r.get("total_score"), 1),
            # Returns
            "ret":       _pct(r.get("ann_ret") or r.get("total_return_ann")),
            "aret":      _pct(r.get("net_active_ann")),
            "activeAnn": _pct(r.get("active_ann")),
            "bmRet":     _pct(r.get("benchmark_ann_ret")),
            # Skill metrics
            "hrate":     _f(r.get("hit_rate"), 3),
            "pickAnn":   _f(r.get("pick_ann_pp"), 1),
            "pickHit":   _f(r.get("pick_hit_rate"), 3),
            # Decomposition
            "dStyle":    _f(r.get("d_style"), 1),
            "dSector":   _f(r.get("d_sector"), 1),
            "dPick":     _f(r.get("d_pick"), 1),
            "dTiming":   _f(r.get("d_timing"), 1),
            "decomp":    bool(r.get("decomp_ok", False)),
            # Cost
            "ter":       _f(ter, 4),
            # Metadata
            "navOnly":   bool(r.get("nav_only", True)),
            "skill":     str(r.get("skill_label", "")),
            "nMonths":   int(r.get("n_months", 0) or 0),
        })

    # Sort by score descending
    funds.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))

    # ── Holdings attribution ───────────────────────────────────────────────
    attr: dict[str, list] = {}
    if hat_path.exists():
        hat = pd.read_parquet(hat_path)
        hat["month"] = pd.to_datetime(hat["month"])
        code_to_name = {f["code"]: f["name"] for f in funds}
        for sc, grp in hat.groupby("scheme_code"):
            grp = grp.sort_values("month")
            attr[str(int(sc))] = [
                {
                    "date":   row["month"].strftime("%Y-%m"),
                    "ret":    _pct(row.get("fund_return"), 2),
                    "mkt":    _pct(row.get("market_attr"), 2),
                    "sty":    _pct(row.get("style_attr"), 2),
                    "ind":    _pct(row.get("industry_attr"), 2),
                    "alpha":  _pct(row.get("stock_selection"), 2),
                }
                for _, row in grp.iterrows()
            ]

    # ── Summary stats ──────────────────────────────────────────────────────
    n_funds = len(funds)
    pct_pos = round(sum(1 for f in funds if (f["aret"] or 0) > 0) / max(n_funds, 1) * 100)
    cat_beat: dict[str, dict] = {}
    for cat, grp_funds in pd.DataFrame(funds).groupby("cat"):
        total = len(grp_funds)
        beat  = int((grp_funds["aret"].fillna(-999) > 0).sum())
        if total >= 5:
            cat_beat[cat] = {"total": total, "beat": beat, "pct": round(beat/total*100)}

    stats = {
        "n_funds":     n_funds,
        "overall_beat": pct_pos,
        "cat_beat":    cat_beat,
        "as_of":       datetime.now().strftime("%B %Y"),
        "loaded_at":   datetime.now().isoformat(),
    }

    # ── Commit to cache ────────────────────────────────────────────────────
    _cache["funds"]          = funds
    _cache["attr"]           = attr
    _cache["stats"]          = stats
    _cache["loaded_at"]      = datetime.now()
    _cache["parquet_mtime"]  = sf_path.stat().st_mtime

    log.info(f"Data loaded: {n_funds} funds, {len(attr)} attribution series — {time.time()-t0:.1f}s")


def _maybe_reload() -> None:
    """Reload cache if parquet is newer than last load."""
    sf_path = DATA_DIR / "scored_funds.parquet"
    if not sf_path.exists():
        return
    if sf_path.stat().st_mtime > _cache.get("parquet_mtime", 0):
        log.info("Parquet updated — reloading cache …")
        _load_data()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def startup():
    _load_data()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {
        "status":    "ok",
        "funds":     len(_cache["funds"] or []),
        "loaded_at": _cache["loaded_at"].isoformat() if _cache["loaded_at"] else None,
        "data_dir":  str(DATA_DIR),
    }


RELOAD_SECRET = os.getenv("RELOAD_SECRET", "")

@app.get("/reload")
def reload(secret: str = ""):
    """Force a full cache reload. Called by the daily pipeline after it runs.
    Pass ?secret=YOUR_SECRET in production to protect this endpoint."""
    if RELOAD_SECRET and secret != RELOAD_SECRET:
        raise HTTPException(403, "Invalid reload secret")
    _load_data()
    return {"status": "reloaded", "funds": len(_cache["funds"] or []),
            "loaded_at": _cache["loaded_at"].isoformat()}


@app.get("/api/stats")
def get_stats():
    _maybe_reload()
    if _cache["stats"] is None:
        raise HTTPException(503, "Data not loaded")
    return _cache["stats"]


@app.get("/api/funds")
def get_funds(
    cat:      str | None = None,
    amc:      str | None = None,
    min_score: float     = 0,
    search:   str | None = None,
):
    _maybe_reload()
    if _cache["funds"] is None:
        raise HTTPException(503, "Data not loaded")
    funds = _cache["funds"]
    if cat:
        funds = [f for f in funds if f["cat"] == cat]
    if amc:
        funds = [f for f in funds if f["amc"].lower() == amc.lower()]
    if min_score:
        funds = [f for f in funds if (f["score"] or 0) >= min_score]
    if search:
        q = search.lower()
        funds = [f for f in funds if q in f["name"].lower() or q in f["amc"].lower()]
    return {"count": len(funds), "funds": funds}


@app.get("/api/funds/{code}")
def get_fund(code: int):
    _maybe_reload()
    if _cache["funds"] is None:
        raise HTTPException(503, "Data not loaded")
    for f in _cache["funds"]:
        if f["code"] == code:
            return f
    raise HTTPException(404, f"Fund {code} not found")


@app.get("/api/attribution/{code}")
def get_attribution(code: int):
    _maybe_reload()
    attr = _cache["attr"] or {}
    rows = attr.get(str(code))
    if rows is None:
        raise HTTPException(404, f"No attribution data for fund {code}")
    return {"code": code, "months": len(rows), "data": rows}


@app.get("/api/categories")
def get_categories():
    _maybe_reload()
    if _cache["funds"] is None:
        raise HTTPException(503, "Data not loaded")
    from collections import Counter
    counts = Counter(f["cat"] for f in _cache["funds"])
    return [{"cat": k, "count": v} for k, v in sorted(counts.items())]


@app.get("/api/amcs")
def get_amcs():
    _maybe_reload()
    if _cache["funds"] is None:
        raise HTTPException(503, "Data not loaded")
    from collections import Counter
    counts = Counter(f["amc"] for f in _cache["funds"])
    return [{"amc": k, "count": v} for k, v in sorted(counts.items())]
