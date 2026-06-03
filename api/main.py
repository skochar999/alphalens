"""
AlphaLens FastAPI Backend
=========================
Serves fund rankings and stats from JSON files in the data/ directory.
Data files are updated daily by the pipeline and committed to GitHub.
Railway auto-redeploys on each push, so data stays fresh.

Endpoints:
  GET /api/funds              — all ranked active funds (filterable)
  GET /api/funds/{code}       — single fund detail
  GET /api/stats              — summary stats (hero section data)
  GET /api/categories         — list of categories with fund counts
  GET /api/amcs               — list of AMCs with fund counts
  GET /reload                 — force cache reload
  GET /health                 — health check + data freshness
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("alphalens.api")

app = FastAPI(title="AlphaLens API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Data directory — data/funds.json and data/stats.json live here
HERE     = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(HERE.parent / "data")))

RELOAD_SECRET = os.getenv("RELOAD_SECRET", "")

_cache: dict[str, Any] = {
    "funds":     None,
    "stats":     None,
    "loaded_at": None,
    "mtime":     0.0,
}


def _load_data() -> None:
    funds_path = DATA_DIR / "funds.json"
    stats_path = DATA_DIR / "stats.json"

    if not funds_path.exists():
        log.error(f"funds.json not found in {DATA_DIR}")
        return

    funds = json.loads(funds_path.read_text())
    stats = json.loads(stats_path.read_text()) if stats_path.exists() else {}

    funds.sort(key=lambda x: (x.get("score") is None, -(x.get("score") or 0)))

    stats["n_funds"]   = len(funds)
    stats["loaded_at"] = datetime.now().isoformat()

    cat_totals: dict[str, int] = defaultdict(int)
    cat_beats:  dict[str, int] = defaultdict(int)
    for f in funds:
        cat = f.get("cat", "")
        if cat and cat != "Index Funds":
            cat_totals[cat] += 1
            if (f.get("aret") or 0) > 0:
                cat_beats[cat] += 1
    stats["cat_beat"] = {
        cat: {"total": cat_totals[cat], "beat": cat_beats[cat],
              "pct": round(cat_beats[cat] / cat_totals[cat] * 100)}
        for cat in cat_totals if cat_totals[cat] >= 5
    }

    _cache["funds"]     = funds
    _cache["stats"]     = stats
    _cache["loaded_at"] = datetime.now()
    _cache["mtime"]     = funds_path.stat().st_mtime

    log.info(f"Loaded {len(funds)} funds from {funds_path}")


def _maybe_reload() -> None:
    funds_path = DATA_DIR / "funds.json"
    if funds_path.exists() and funds_path.stat().st_mtime > _cache.get("mtime", 0):
        _load_data()


@app.on_event("startup")
def startup():
    _load_data()


@app.get("/health")
def health():
    return {
        "status":    "ok",
        "funds":     len(_cache["funds"] or []),
        "loaded_at": _cache["loaded_at"].isoformat() if _cache["loaded_at"] else None,
        "data_dir":  str(DATA_DIR),
    }


@app.get("/reload")
def reload(secret: str = ""):
    if RELOAD_SECRET and secret != RELOAD_SECRET:
        raise HTTPException(403, "Invalid reload secret")
    _load_data()
    return {"status": "reloaded", "funds": len(_cache["funds"] or []),
            "loaded_at": _cache["loaded_at"].isoformat()}


@app.get("/api/stats")
def get_stats():
    _maybe_reload()
    if not _cache["stats"]:
        raise HTTPException(503, "Data not loaded")
    return _cache["stats"]


@app.get("/api/funds")
def get_funds(
    cat:       str | None = None,
    amc:       str | None = None,
    min_score: float      = 0,
    search:    str | None = None,
):
    _maybe_reload()
    if _cache["funds"] is None:
        raise HTTPException(503, "Data not loaded")
    funds = _cache["funds"]
    if cat:
        funds = [f for f in funds if f.get("cat") == cat]
    if amc:
        funds = [f for f in funds if (f.get("amc") or "").lower() == amc.lower()]
    if min_score:
        funds = [f for f in funds if (f.get("score") or 0) >= min_score]
    if search:
        q = search.lower()
        funds = [f for f in funds
                 if q in (f.get("name") or "").lower() or q in (f.get("amc") or "").lower()]
    return {"count": len(funds), "funds": funds}


@app.get("/api/funds/{code}")
def get_fund(code: int):
    _maybe_reload()
    if _cache["funds"] is None:
        raise HTTPException(503, "Data not loaded")
    for f in _cache["funds"]:
        if f.get("code") == code:
            return f
    raise HTTPException(404, f"Fund {code} not found")


@app.get("/api/categories")
def get_categories():
    _maybe_reload()
    if not _cache["funds"]:
        raise HTTPException(503, "Data not loaded")
    counts = Counter(f.get("cat", "") for f in _cache["funds"])
    return [{"cat": k, "count": v} for k, v in sorted(counts.items()) if k]


@app.get("/api/amcs")
def get_amcs():
    _maybe_reload()
    if not _cache["funds"]:
        raise HTTPException(503, "Data not loaded")
    counts = Counter(f.get("amc", "") for f in _cache["funds"])
    return [{"amc": k, "count": v} for k, v in sorted(counts.items()) if k]
