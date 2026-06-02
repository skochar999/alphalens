#!/usr/bin/env python3
"""
build_mf_website.py

Generates a self-contained HTML analytics website for mutual fund performance.

Reads:
    mf_data/scores.parquet       — fund scores & metrics
    mf_data/attribution.parquet  — monthly attribution per fund

Writes:
    mf_website.html              — fully self-contained HTML (no server needed)

Open mf_website.html in any browser — all data is embedded as JSON.

Usage:
    python build_mf_website.py
    python build_mf_website.py --mf-data ./mf_data --out mf_website.html
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mf.build_website")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--mf-data", default="./mf_data")
    p.add_argument("--out",     default="./mf_website.html")
    return p.parse_args()


def _safe_float(x, decimals=4):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return round(float(x), decimals)


def prepare_scores(scores: pd.DataFrame) -> list[dict]:
    out = []
    for _, r in scores.iterrows():
        out.append({
            "code":       int(r["scheme_code"]),
            "name":       str(r.get("scheme_name", "")),
            "amc":        str(r.get("amc", "")),
            "score":      _safe_float(r.get("score"), 1),
            "aq":         _safe_float(r.get("alpha_quality"), 3),
            "ap":         _safe_float(r.get("alpha_proportion"), 3),
            "ss":         _safe_float(r.get("style_stability"), 3),
            "ir":         _safe_float(r.get("info_ratio"), 2),
            "alpha_ann":  _safe_float(r.get("alpha_ann"), 4),
            "te_ann":     _safe_float(r.get("tracking_error_ann"), 4),
            "ret_ann":    _safe_float(r.get("total_return_ann"), 4),
            "beta_M":     _safe_float(r.get("beta_M"), 3),
            "beta_S":     _safe_float(r.get("beta_S"), 3),
            "beta_I":     _safe_float(r.get("beta_I"), 3),
            "r2":         _safe_float(r.get("r_squared"), 3),
            "n_months":   int(r.get("n_months", 0)),
        })
    return out


def prepare_attribution(attr: pd.DataFrame) -> dict[str, list]:
    """Return dict: str(scheme_code) → list of monthly attribution dicts."""
    result = {}
    attr = attr.copy()
    attr["date"] = pd.to_datetime(attr["date"])
    for sc, grp in attr.groupby("scheme_code"):
        grp = grp.sort_values("date")
        result[str(int(sc))] = [
            {
                "date":  row["date"].strftime("%Y-%m"),
                "ret":   _safe_float(row.get("actual_ret"), 4),
                "mkt":   _safe_float(row.get("market_ret"), 4),
                "sty":   _safe_float(row.get("style_ret"), 4),
                "ind":   _safe_float(row.get("industry_ret"), 4),
                "alpha": _safe_float(row.get("alpha"), 4),
                "bM":    _safe_float(row.get("beta_M"), 3),
                "bS":    _safe_float(row.get("beta_S"), 3),
                "bI":    _safe_float(row.get("beta_I"), 3),
                "r2":    _safe_float(row.get("r_squared"), 3),
            }
            for _, row in grp.iterrows()
        ]
    return result


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MF Alpha — Indian Mutual Fund Analytics</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117; --bg2: #1a1d27; --bg3: #22263a;
    --border: #2e3350; --text: #e8eaf0; --muted: #8b93b0;
    --accent: #4f8ef7; --green: #34d399; --red: #f87171;
    --yellow: #fbbf24; --purple: #a78bfa; --orange: #fb923c;
    --score-hi: #34d399; --score-mid: #fbbf24; --score-lo: #f87171;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }

  /* Header */
  .header { padding: 18px 32px; background: var(--bg2); border-bottom: 1px solid var(--border);
            display: flex; align-items: center; gap: 16px; }
  .header h1 { font-size: 20px; font-weight: 700; color: var(--accent); letter-spacing: -0.3px; }
  .header .subtitle { color: var(--muted); font-size: 12px; }

  /* Tabs */
  .tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border); background: var(--bg2); padding: 0 32px; }
  .tab  { padding: 12px 20px; cursor: pointer; color: var(--muted); font-weight: 500;
           border-bottom: 3px solid transparent; transition: all 0.15s; }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }

  /* Pages */
  .page { display: none; padding: 24px 32px; }
  .page.active { display: block; }

  /* Controls row */
  .controls { display: flex; gap: 12px; margin-bottom: 18px; align-items: center; flex-wrap: wrap; }
  .controls input, .controls select {
    background: var(--bg2); border: 1px solid var(--border); color: var(--text);
    padding: 7px 12px; border-radius: 6px; font-size: 13px; }
  .controls input:focus, .controls select:focus { outline: none; border-color: var(--accent); }
  .controls label { color: var(--muted); font-size: 12px; }

  /* Table */
  .table-wrap { overflow-x: auto; border-radius: 8px; border: 1px solid var(--border); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  thead th { background: var(--bg3); padding: 10px 12px; text-align: left; color: var(--muted);
              font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
              cursor: pointer; user-select: none; white-space: nowrap; }
  thead th:hover { color: var(--text); }
  thead th.sorted-asc::after  { content: " ↑"; color: var(--accent); }
  thead th.sorted-desc::after { content: " ↓"; color: var(--accent); }
  tbody tr { border-top: 1px solid var(--border); transition: background 0.1s; cursor: pointer; }
  tbody tr:hover { background: var(--bg3); }
  td { padding: 10px 12px; }
  .td-name { max-width: 260px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 500; }
  .td-amc  { color: var(--muted); font-size: 12px; }
  .td-num  { text-align: right; font-variant-numeric: tabular-nums; font-family: monospace; }
  .td-pct  { text-align: right; font-variant-numeric: tabular-nums; font-family: monospace; }

  /* Score pill */
  .score-pill { display: inline-block; padding: 2px 10px; border-radius: 12px; font-weight: 700;
                font-size: 13px; min-width: 42px; text-align: center; }
  .score-hi  { background: rgba(52,211,153,0.15); color: var(--green); }
  .score-mid { background: rgba(251,191,36,0.15);  color: var(--yellow); }
  .score-lo  { background: rgba(248,113,113,0.15); color: var(--red); }

  /* Fund detail */
  .fund-header { display: flex; align-items: center; gap: 20px; margin-bottom: 24px;
                  padding: 20px; background: var(--bg2); border-radius: 10px; border: 1px solid var(--border); }
  .fund-score-circle { width: 72px; height: 72px; border-radius: 50%; display: flex;
                        flex-direction: column; align-items: center; justify-content: center;
                        font-weight: 800; font-size: 22px; flex-shrink: 0; }
  .fund-meta h2 { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
  .fund-meta .amc-tag { background: var(--bg3); color: var(--muted); padding: 2px 8px;
                         border-radius: 4px; font-size: 11px; font-weight: 600; }

  /* Sub-score bars */
  .sub-scores { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }
  .sub-score-card { flex: 1; min-width: 160px; background: var(--bg2); border: 1px solid var(--border);
                     border-radius: 8px; padding: 14px 16px; }
  .sub-score-card .label { color: var(--muted); font-size: 11px; font-weight: 600;
                            text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
  .sub-score-card .value { font-size: 24px; font-weight: 800; margin-bottom: 6px; }
  .sub-score-card .bar-bg { height: 4px; background: var(--bg3); border-radius: 2px; }
  .sub-score-card .bar-fill { height: 4px; border-radius: 2px; transition: width 0.4s; }

  /* Metric chips */
  .metrics-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }
  .metric-chip { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
                  padding: 10px 16px; min-width: 120px; }
  .metric-chip .chip-label { color: var(--muted); font-size: 11px; text-transform: uppercase;
                               letter-spacing: 0.5px; margin-bottom: 4px; }
  .metric-chip .chip-value { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }

  /* Charts */
  .chart-section { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
                    padding: 20px; margin-bottom: 20px; }
  .chart-section h3 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase;
                       letter-spacing: 0.5px; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 260px; }

  /* Backtest */
  .backtest-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 12px; }
  .bt-card { background: var(--bg3); border-radius: 6px; padding: 12px; text-align: center; }
  .bt-card .bt-label { font-size: 11px; color: var(--muted); margin-bottom: 6px; }
  .bt-card .bt-val   { font-size: 20px; font-weight: 700; }

  /* Pagination */
  .pagination { display: flex; gap: 8px; align-items: center; margin-top: 14px; justify-content: flex-end; }
  .page-btn { background: var(--bg2); border: 1px solid var(--border); color: var(--text);
               padding: 5px 12px; border-radius: 5px; cursor: pointer; font-size: 12px; }
  .page-btn:hover { border-color: var(--accent); color: var(--accent); }
  .page-btn.active { background: var(--accent); color: white; border-color: var(--accent); }
  .page-info { color: var(--muted); font-size: 12px; }

  .back-btn { background: none; border: 1px solid var(--border); color: var(--muted);
               padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 12px;
               display: inline-flex; align-items: center; gap: 6px; margin-bottom: 20px; }
  .back-btn:hover { color: var(--text); border-color: var(--text); }

  .empty-state { text-align: center; padding: 60px 20px; color: var(--muted); }
  .empty-state h2 { font-size: 18px; margin-bottom: 8px; }

  @media (max-width: 700px) {
    .page { padding: 16px; }
    .header { padding: 14px 16px; }
    .tabs { padding: 0 16px; }
  }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>MF Alpha</h1>
    <div class="subtitle">Indian Mutual Fund Performance Analytics · IEC-1 Factor Model</div>
  </div>
</div>

<div class="tabs">
  <div class="tab active" onclick="showTab('leaderboard')">Leaderboard</div>
  <div class="tab" onclick="showTab('detail')" id="tab-detail" style="display:none">Fund Detail</div>
  <div class="tab" onclick="showTab('about')">About</div>
</div>

<!-- LEADERBOARD PAGE -->
<div class="page active" id="page-leaderboard">
  <div class="controls">
    <div>
      <label>Search fund</label><br>
      <input type="text" id="search-input" placeholder="e.g. HDFC Mid Cap" style="width:240px">
    </div>
    <div>
      <label>AMC</label><br>
      <select id="amc-filter"><option value="">All AMCs</option></select>
    </div>
    <div>
      <label>Min score</label><br>
      <select id="score-filter">
        <option value="0">All scores</option>
        <option value="70">70+ (Strong)</option>
        <option value="50">50+ (Above avg)</option>
        <option value="30">30+ (Below avg)</option>
      </select>
    </div>
    <div style="margin-left:auto; color:var(--muted); font-size:12px; align-self:flex-end">
      <span id="count-label"></span>
    </div>
  </div>

  <div class="table-wrap">
    <table id="lb-table">
      <thead>
        <tr>
          <th onclick="sortBy('rank')"     data-key="rank"     >#</th>
          <th onclick="sortBy('name')"     data-key="name"     >Fund</th>
          <th onclick="sortBy('score')"    data-key="score"    >Score</th>
          <th onclick="sortBy('ir')"       data-key="ir"       >Info Ratio</th>
          <th onclick="sortBy('alpha_ann')"data-key="alpha_ann">Alpha/yr</th>
          <th onclick="sortBy('ret_ann')"  data-key="ret_ann"  >Return/yr</th>
          <th onclick="sortBy('te_ann')"   data-key="te_ann"   >Track.Err</th>
          <th onclick="sortBy('beta_M')"   data-key="beta_M"   >β Market</th>
          <th onclick="sortBy('r2')"       data-key="r2"       >R²</th>
        </tr>
      </thead>
      <tbody id="lb-body"></tbody>
    </table>
  </div>
  <div class="pagination" id="lb-pagination"></div>
</div>

<!-- FUND DETAIL PAGE -->
<div class="page" id="page-detail">
  <button class="back-btn" onclick="showTab('leaderboard')">← Back to Leaderboard</button>
  <div id="detail-content">
    <div class="empty-state">
      <h2>Select a fund from the leaderboard</h2>
    </div>
  </div>
</div>

<!-- ABOUT PAGE -->
<div class="page" id="page-about">
  <div style="max-width:780px; line-height:1.7; color:var(--text)">
    <h2 style="font-size:18px; margin-bottom:16px; color:var(--accent)">How the Score Works</h2>

    <p style="margin-bottom:16px; color:var(--muted)">
      Each fund is scored 0–100 based on three pillars of evidence that a fund is likely to outperform its peers in the future.
    </p>

    <h3 style="color:var(--yellow); margin-bottom:8px; font-size:14px">Alpha Quality — 50 points</h3>
    <p style="margin-bottom:16px; color:var(--muted)">
      The Information Ratio (IR) measures how much alpha the fund generates per unit of active risk (tracking error).
      IR = annualised alpha / annualised tracking error. A higher IR means the fund consistently generates returns
      that cannot be explained by factor exposures — the true hallmark of skilled stock picking.
    </p>

    <h3 style="color:var(--green); margin-bottom:8px; font-size:14px">Alpha Proportion — 30 points</h3>
    <p style="margin-bottom:16px; color:var(--muted)">
      Of the fund's total return, what fraction comes from alpha (stock picking) rather than passive factor exposure?
      A fund that earns 15% per year with 10% from alpha scores higher than a fund earning 15% with only 2% from alpha,
      even if both have the same total return.
    </p>

    <h3 style="color:var(--purple); margin-bottom:8px; font-size:14px">Style Stability — 20 points</h3>
    <p style="margin-bottom:16px; color:var(--muted)">
      Funds that drift between styles (market beta, style tilts, sector bets) are harder to analyse and
      often do so reactively — chasing recent winners. Style-stable funds are more transparent,
      easier to combine in a portfolio, and tend to have more persistent alpha.
    </p>

    <h3 style="color:var(--accent); margin-bottom:8px; font-size:14px">Factor Attribution</h3>
    <p style="margin-bottom:16px; color:var(--muted)">
      Monthly returns are decomposed using the IEC-1 39-factor Indian equity model:
    </p>
    <ul style="margin-left:20px; color:var(--muted); margin-bottom:16px">
      <li><strong style="color:var(--text)">Market</strong> — beta to the broad equity market</li>
      <li><strong style="color:var(--text)">Style</strong> — tilts to size, value, momentum, quality, low-vol, etc.</li>
      <li><strong style="color:var(--text)">Industry</strong> — sector overweights (banks, IT, pharma, etc.)</li>
      <li><strong style="color:var(--text)">Alpha</strong> — residual return unexplained by any factor (stock picking + timing)</li>
    </ul>
    <p style="color:var(--muted); font-size:12px; margin-top:24px; border-top:1px solid var(--border); padding-top:16px">
      Built with IEC-1 factor model · Data from mfapi.in, AMFI, NSE, yfinance · For research purposes only.
    </p>
  </div>
</div>

<!-- DATA INJECTION POINT -->
<script>
const SCORES = __SCORES_JSON__;
const ATTRIBUTION = __ATTR_JSON__;
</script>

<script>
// =========================================================================
// State
// =========================================================================
let sortKey = "score";
let sortDir = -1;  // -1 = descending
let currentPage = 1;
const PAGE_SIZE = 25;

let filteredData = [...SCORES];
let selectedFund = null;
let detailCharts = {};

// =========================================================================
// Utilities
// =========================================================================
function scoreClass(s) {
  if (s == null) return "";
  if (s >= 65) return "score-hi";
  if (s >= 40) return "score-mid";
  return "score-lo";
}
function pct(v, decimals=1) {
  if (v == null) return "—";
  return (v * 100).toFixed(decimals) + "%";
}
function num(v, decimals=2) {
  if (v == null) return "—";
  return v.toFixed(decimals);
}
function clr(v) {
  if (v == null) return "inherit";
  return v >= 0 ? "var(--green)" : "var(--red)";
}

// =========================================================================
// Tab switching
// =========================================================================
function showTab(id) {
  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  document.getElementById("page-" + id).classList.add("active");
  const tabEl = document.querySelector(`.tab[onclick="showTab('${id}')"]`);
  if (tabEl) tabEl.classList.add("active");
  if (id === "detail" && !selectedFund) showTab("leaderboard");
}

// =========================================================================
// Leaderboard
// =========================================================================
function buildAmcDropdown() {
  const amcs = [...new Set(SCORES.map(s => s.amc).filter(Boolean))].sort();
  const sel = document.getElementById("amc-filter");
  amcs.forEach(a => {
    const o = document.createElement("option");
    o.value = a; o.textContent = a.replace("_", " ");
    sel.appendChild(o);
  });
}

function applyFilters() {
  const q      = document.getElementById("search-input").value.toLowerCase();
  const amc    = document.getElementById("amc-filter").value;
  const minSc  = +document.getElementById("score-filter").value;

  filteredData = SCORES.filter(f => {
    if (q   && !f.name.toLowerCase().includes(q) && !f.amc.toLowerCase().includes(q)) return false;
    if (amc && f.amc !== amc) return false;
    if (minSc && (f.score == null || f.score < minSc)) return false;
    return true;
  });

  // Apply sort
  filteredData.sort((a, b) => {
    const va = a[sortKey] ?? (sortDir < 0 ? -Infinity : Infinity);
    const vb = b[sortKey] ?? (sortDir < 0 ? -Infinity : Infinity);
    if (typeof va === "string") return sortDir * va.localeCompare(vb);
    return sortDir * (vb - va);
  });

  currentPage = 1;
  renderTable();
}

function sortBy(key) {
  if (sortKey === key) sortDir *= -1;
  else { sortKey = key; sortDir = -1; }

  document.querySelectorAll("thead th").forEach(th => {
    th.classList.remove("sorted-asc", "sorted-desc");
    if (th.dataset.key === key) {
      th.classList.add(sortDir < 0 ? "sorted-desc" : "sorted-asc");
    }
  });
  applyFilters();
}

function renderTable() {
  const start = (currentPage - 1) * PAGE_SIZE;
  const slice = filteredData.slice(start, start + PAGE_SIZE);

  const tbody = document.getElementById("lb-body");
  tbody.innerHTML = "";

  const globalRank = {};
  SCORES.forEach((f, i) => { globalRank[f.code] = i + 1; });

  slice.forEach(f => {
    const rank = globalRank[f.code] ?? "—";
    const tr = document.createElement("tr");
    tr.onclick = () => openFundDetail(f.code);
    tr.innerHTML = `
      <td class="td-num" style="color:var(--muted)">${rank}</td>
      <td>
        <div class="td-name" title="${f.name}">${f.name}</div>
        <div class="td-amc">${(f.amc||"").replace("_"," ")}</div>
      </td>
      <td><span class="score-pill ${scoreClass(f.score)}">${f.score != null ? f.score.toFixed(1) : "—"}</span></td>
      <td class="td-num" style="color:${clr(f.ir)}">${num(f.ir)}</td>
      <td class="td-pct" style="color:${clr(f.alpha_ann)}">${pct(f.alpha_ann)}</td>
      <td class="td-pct" style="color:${clr(f.ret_ann)}">${pct(f.ret_ann)}</td>
      <td class="td-pct">${pct(f.te_ann)}</td>
      <td class="td-num">${num(f.beta_M, 2)}</td>
      <td class="td-num">${num(f.r2, 2)}</td>
    `;
    tbody.appendChild(tr);
  });

  document.getElementById("count-label").textContent =
    `${filteredData.length} fund${filteredData.length !== 1 ? "s" : ""}`;

  renderPagination();
}

function renderPagination() {
  const total = Math.ceil(filteredData.length / PAGE_SIZE);
  const div = document.getElementById("lb-pagination");
  div.innerHTML = "";

  if (total <= 1) return;

  const info = document.createElement("span");
  info.className = "page-info";
  info.textContent = `Page ${currentPage} of ${total}`;
  div.appendChild(info);

  for (let i = 1; i <= Math.min(total, 7); i++) {
    const btn = document.createElement("button");
    btn.className = "page-btn" + (i === currentPage ? " active" : "");
    btn.textContent = i;
    btn.onclick = () => { currentPage = i; renderTable(); };
    div.appendChild(btn);
  }
}

// =========================================================================
// Fund Detail
// =========================================================================
function destroyCharts() {
  Object.values(detailCharts).forEach(c => c.destroy());
  detailCharts = {};
}

function openFundDetail(code) {
  const fund = SCORES.find(f => f.code === code);
  if (!fund) return;
  selectedFund = fund;

  // Show tab
  document.getElementById("tab-detail").style.display = "";
  showTab("detail");

  destroyCharts();

  const attr = (ATTRIBUTION[String(code)] || []).sort((a, b) => a.date.localeCompare(b.date));
  const scr = fund.score || 0;
  const scoreColor = scr >= 65 ? "var(--green)" : scr >= 40 ? "var(--yellow)" : "var(--red)";

  const aqPct  = (fund.aq  || 0) * 100;
  const apPct  = (fund.ap  || 0) * 100;
  const ssPct  = (fund.ss  || 0) * 100;

  document.getElementById("detail-content").innerHTML = `
    <div class="fund-header">
      <div class="fund-score-circle" style="background:${scoreColor}22; color:${scoreColor}; border: 2px solid ${scoreColor}">
        <span>${scr != null ? scr.toFixed(0) : "—"}</span>
        <span style="font-size:10px; font-weight:500; opacity:0.8">/ 100</span>
      </div>
      <div class="fund-meta">
        <h2>${fund.name}</h2>
        <span class="amc-tag">${(fund.amc||"").replace("_", " ")}</span>
        <div style="color:var(--muted); font-size:12px; margin-top:6px">${fund.n_months} months of data</div>
      </div>
    </div>

    <div class="sub-scores">
      <div class="sub-score-card">
        <div class="label">Alpha Quality</div>
        <div class="value" style="color:var(--yellow)">${aqPct.toFixed(0)}<span style="font-size:14px; color:var(--muted)">%ile</span></div>
        <div class="bar-bg"><div class="bar-fill" style="width:${aqPct}%;background:var(--yellow)"></div></div>
        <div style="color:var(--muted);font-size:11px;margin-top:6px">IR = ${num(fund.ir)}</div>
      </div>
      <div class="sub-score-card">
        <div class="label">Alpha Proportion</div>
        <div class="value" style="color:var(--green)">${apPct.toFixed(0)}<span style="font-size:14px; color:var(--muted)">%ile</span></div>
        <div class="bar-bg"><div class="bar-fill" style="width:${apPct}%;background:var(--green)"></div></div>
        <div style="color:var(--muted);font-size:11px;margin-top:6px">α/yr = ${pct(fund.alpha_ann)}</div>
      </div>
      <div class="sub-score-card">
        <div class="label">Style Stability</div>
        <div class="value" style="color:var(--purple)">${ssPct.toFixed(0)}<span style="font-size:14px; color:var(--muted)">%ile</span></div>
        <div class="bar-bg"><div class="bar-fill" style="width:${ssPct}%;background:var(--purple)"></div></div>
        <div style="color:var(--muted);font-size:11px;margin-top:6px">β_M consistency</div>
      </div>
    </div>

    <div class="metrics-row">
      <div class="metric-chip">
        <div class="chip-label">Total Return/yr</div>
        <div class="chip-value" style="color:${clr(fund.ret_ann)}">${pct(fund.ret_ann)}</div>
      </div>
      <div class="metric-chip">
        <div class="chip-label">Alpha/yr</div>
        <div class="chip-value" style="color:${clr(fund.alpha_ann)}">${pct(fund.alpha_ann)}</div>
      </div>
      <div class="metric-chip">
        <div class="chip-label">Tracking Error/yr</div>
        <div class="chip-value">${pct(fund.te_ann)}</div>
      </div>
      <div class="metric-chip">
        <div class="chip-label">Market Beta</div>
        <div class="chip-value">${num(fund.beta_M, 2)}</div>
      </div>
      <div class="metric-chip">
        <div class="chip-label">Style Beta</div>
        <div class="chip-value">${num(fund.beta_S, 2)}</div>
      </div>
      <div class="metric-chip">
        <div class="chip-label">Industry Beta</div>
        <div class="chip-value">${num(fund.beta_I, 2)}</div>
      </div>
      <div class="metric-chip">
        <div class="chip-label">R² (explained)</div>
        <div class="chip-value">${num(fund.r2, 2)}</div>
      </div>
    </div>

    <div class="chart-section">
      <h3>Monthly Attribution — Market / Style / Industry / Alpha</h3>
      <div class="chart-wrap"><canvas id="chart-attr"></canvas></div>
    </div>

    <div class="chart-section">
      <h3>Cumulative Attribution (stacked)</h3>
      <div class="chart-wrap"><canvas id="chart-cumattr"></canvas></div>
    </div>

    <div class="chart-section">
      <h3>Rolling Factor Betas (β Market · β Style · β Industry)</h3>
      <div class="chart-wrap"><canvas id="chart-betas"></canvas></div>
    </div>
  `;

  renderAttrChart(attr);
  renderCumAttrChart(attr);
  renderBetaChart(attr);
}

const CHART_DEFAULTS = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { labels: { color: "#8b93b0", font: { size: 11 } } },
    tooltip: { backgroundColor: "#1a1d27", borderColor: "#2e3350", borderWidth: 1 }
  },
  scales: {
    x: { grid: { color: "#2e3350" }, ticks: { color: "#8b93b0", font: { size: 10 } } },
    y: { grid: { color: "#2e3350" }, ticks: { color: "#8b93b0", font: { size: 10 },
         callback: v => (v*100).toFixed(1)+"%" } }
  }
};

function renderAttrChart(attr) {
  if (!attr.length) return;
  const labels = attr.map(d => d.date);
  const toArr  = key => attr.map(d => d[key]);

  const ctx = document.getElementById("chart-attr").getContext("2d");
  detailCharts.attr = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "Market",   data: toArr("mkt"),   backgroundColor: "rgba(79,142,247,0.75)", stack: "s" },
        { label: "Style",    data: toArr("sty"),   backgroundColor: "rgba(167,139,250,0.75)", stack: "s" },
        { label: "Industry", data: toArr("ind"),   backgroundColor: "rgba(251,191,36,0.75)", stack: "s" },
        { label: "Alpha",    data: toArr("alpha"), backgroundColor: "rgba(52,211,153,0.85)", stack: "s" },
      ]
    },
    options: { ...CHART_DEFAULTS, interaction: { mode: "index" } }
  });
}

function renderCumAttrChart(attr) {
  if (!attr.length) return;
  const labels = attr.map(d => d.date);

  // Running cumulative sums
  let cMkt=0, cSty=0, cInd=0, cAlpha=0;
  const mktArr=[],styArr=[],indArr=[],alphaArr=[];
  for (const d of attr) {
    cMkt   += d.mkt   || 0; mktArr.push(cMkt);
    cSty   += d.sty   || 0; styArr.push(cSty);
    cInd   += d.ind   || 0; indArr.push(cInd);
    cAlpha += d.alpha || 0; alphaArr.push(cAlpha);
  }

  const ctx = document.getElementById("chart-cumattr").getContext("2d");
  detailCharts.cumattr = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Cumul. Market",   data: mktArr,   borderColor:"rgba(79,142,247,0.9)",  backgroundColor:"rgba(79,142,247,0.08)", fill:true, tension:0.3, pointRadius:0 },
        { label: "Cumul. Style",    data: styArr,   borderColor:"rgba(167,139,250,0.9)", backgroundColor:"rgba(167,139,250,0.08)", fill:true, tension:0.3, pointRadius:0 },
        { label: "Cumul. Industry", data: indArr,   borderColor:"rgba(251,191,36,0.9)",  backgroundColor:"rgba(251,191,36,0.08)", fill:true, tension:0.3, pointRadius:0 },
        { label: "Cumul. Alpha",    data: alphaArr, borderColor:"rgba(52,211,153,0.9)",  backgroundColor:"rgba(52,211,153,0.08)", fill:true, tension:0.3, pointRadius:0, borderWidth:2 },
      ]
    },
    options: CHART_DEFAULTS
  });
}

function renderBetaChart(attr) {
  if (!attr.length) return;
  const labels = attr.map(d => d.date);

  const ctx = document.getElementById("chart-betas").getContext("2d");
  detailCharts.betas = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "β Market",   data: attr.map(d=>d.bM), borderColor:"rgba(79,142,247,0.9)",  pointRadius:2, tension:0.3 },
        { label: "β Style",    data: attr.map(d=>d.bS), borderColor:"rgba(167,139,250,0.9)", pointRadius:2, tension:0.3 },
        { label: "β Industry", data: attr.map(d=>d.bI), borderColor:"rgba(251,191,36,0.9)",  pointRadius:2, tension:0.3 },
      ]
    },
    options: {
      ...CHART_DEFAULTS,
      scales: {
        x: { grid:{color:"#2e3350"}, ticks:{color:"#8b93b0",font:{size:10}} },
        y: { grid:{color:"#2e3350"}, ticks:{color:"#8b93b0",font:{size:10}} }
      }
    }
  });
}

// =========================================================================
// Init
// =========================================================================
(function init() {
  buildAmcDropdown();
  applyFilters();

  document.getElementById("search-input").addEventListener("input", applyFilters);
  document.getElementById("amc-filter").addEventListener("change", applyFilters);
  document.getElementById("score-filter").addEventListener("change", applyFilters);

  // Default sort desc by score
  const th = document.querySelector('th[data-key="score"]');
  if (th) th.classList.add("sorted-desc");
})();
</script>
</body>
</html>
"""


def main() -> None:
    args     = _parse_args()
    mf_data  = Path(args.mf_data)
    out_path = Path(args.out)

    scores_path = mf_data / "scores.parquet"
    attr_path   = mf_data / "attribution.parquet"

    for p in [scores_path, attr_path]:
        if not p.exists():
            log.error(f"Missing: {p}\nRun attribution.py and scorer.py first.")
            sys.exit(1)

    log.info("Loading scores …")
    scores = pd.read_parquet(scores_path)
    log.info(f"  {len(scores)} funds")

    log.info("Loading attribution …")
    attr = pd.read_parquet(attr_path)
    log.info(f"  {len(attr):,} rows")

    scores_json = json.dumps(prepare_scores(scores), separators=(",", ":"))
    attr_json   = json.dumps(prepare_attribution(attr), separators=(",", ":"))

    html = HTML_TEMPLATE.replace("__SCORES_JSON__", scores_json)
    html = html.replace("__ATTR_JSON__", attr_json)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    size_kb = out_path.stat().st_size / 1024
    log.info(f"\nWebsite saved → {out_path}  ({size_kb:.0f} KB)")
    log.info("Open in any browser — no server required.\n")


if __name__ == "__main__":
    main()
