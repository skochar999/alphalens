#!/usr/bin/env python3
"""
build_dashboard.py — Generate a self-contained HTML dashboard from inec1_outputs/.

Usage
-----
    python3 build_dashboard.py
    python3 build_dashboard.py --output-dir ./inec1_outputs --out dashboard.html
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd


STYLE_FACTORS = [
    "BETA", "SIZE", "STREV", "LTMOM", "VALUE", "GROWTH",
    "LOWVOL", "LIQUIDITY", "LEVERAGE", "EARNYIELD", "EARNVAR", "PROFIT",
]

STYLE_COLORS = [
    "#e74c3c", "#3498db", "#2ecc71", "#f39c12",
    "#9b59b6", "#1abc9c", "#e67e22", "#607d8b",
    "#c0392b", "#27ae60", "#8e44ad", "#2980b9",
]


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _safe(v) -> object:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (np.floating, np.integer)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    return v


def _to_list(s: pd.Series) -> list:
    return [_safe(float(x)) for x in s]


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data(output_dir: Path) -> dict:
    """Read Parquet files and return a JSON-serialisable dict for the dashboard."""

    # Latest exposures file
    exp_files = sorted(output_dir.glob("exposures_*.parquet"))
    run_date = "—"
    exposure_rows: list[dict] = []

    if exp_files:
        latest = exp_files[-1]
        run_date = latest.stem[len("exposures_"):]
        try:
            exp_df = pd.read_parquet(latest)
            cols = [f for f in STYLE_FACTORS if f in exp_df.columns]
            for ticker, row in exp_df[cols].iterrows():
                d: dict = {"ticker": str(ticker)}
                for f in cols:
                    d[f] = _safe(float(row[f]))
                exposure_rows.append(d)
        except Exception as exc:
            print(f"  Warning: exposures read failed: {exc}", file=sys.stderr)

    # Factor return history
    fr_dates: list[str] = []
    cum_returns: dict = {}
    daily_returns: dict = {}
    fr_path = output_dir / "factor_returns_history.parquet"

    if fr_path.exists():
        try:
            fr = pd.read_parquet(fr_path)
            cols = [f for f in STYLE_FACTORS if f in fr.columns]
            fr = fr[cols].fillna(0.0)
            fr_dates = [str(d) for d in fr.index]
            cum = (1 + fr).cumprod() - 1
            for f in cols:
                cum_returns[f]   = _to_list(cum[f] * 100)
                daily_returns[f] = _to_list(fr[f] * 100)
        except Exception as exc:
            print(f"  Warning: factor return history read failed: {exc}", file=sys.stderr)

    # R² history
    r2_dates: list[str] = []
    r2_values: list = []
    r2_path = output_dir / "r2_history.parquet"

    if r2_path.exists():
        try:
            r2df = pd.read_parquet(r2_path)
            if "r2" in r2df.columns:
                r2_dates  = [str(d) for d in r2df.index]
                r2_values = _to_list(r2df["r2"])
        except Exception as exc:
            print(f"  Warning: R² history read failed: {exc}", file=sys.stderr)

    return {
        "run_date":      run_date,
        "n_stocks":      len(exposure_rows),
        "r2_today":      r2_values[-1] if r2_values else None,
        "style_factors": STYLE_FACTORS,
        "style_colors":  STYLE_COLORS,
        "fr_history":    {"dates": fr_dates, "cumulative": cum_returns, "daily": daily_returns},
        "r2_history":    {"dates": r2_dates, "values": r2_values},
        "exposures":     exposure_rows,
    }


# ── HTML template ─────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IEC-1 Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f0f2f5;color:#1a1a2e;font-size:14px}

/* ── Header ── */
.hdr{background:#1a1d3e;color:#fff;padding:16px 28px;display:flex;align-items:center;justify-content:space-between}
.hdr h1{font-size:1.1rem;font-weight:600;letter-spacing:.05em}
.hdr-meta{display:flex;gap:22px;font-size:.82rem;opacity:.9}
.hdr-meta b{color:#7ecfff}
.pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.72rem;font-weight:600;margin-left:5px}
.pill-g{background:#dcfce7;color:#166534}.pill-y{background:#fef9c3;color:#854d0e}.pill-r{background:#fee2e2;color:#991b1b}

/* ── Tabs ── */
.tabs{background:#fff;border-bottom:2px solid #e0e3ea;display:flex;padding:0 28px}
.tab-btn{padding:13px 18px;font-size:.85rem;font-weight:500;cursor:pointer;border:none;background:none;
  color:#666;border-bottom:2px solid transparent;margin-bottom:-2px;transition:color .15s,border-color .15s}
.tab-btn.active{color:#1a1d3e;border-bottom-color:#3b82f6}
.tab-btn:hover{color:#1a1d3e}

/* ── Panels ── */
.panel{display:none;padding:24px 28px}.panel.active{display:block}
.card{background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.07);padding:20px 22px;margin-bottom:20px}
.card-title{font-size:.88rem;font-weight:600;color:#444;margin-bottom:16px}
.chart-h400{position:relative;height:400px}
.chart-h280{position:relative;height:280px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.empty{text-align:center;padding:56px;color:#aaa;font-size:.88rem}

/* ── Table ── */
.tbl-bar{display:flex;align-items:center;gap:12px;margin-bottom:10px}
.tbl-bar input{padding:6px 11px;border:1px solid #dde1e9;border-radius:6px;font-size:.82rem;width:240px;outline:none}
.tbl-bar input:focus{border-color:#3b82f6}
.tbl-count{font-size:.78rem;color:#999}
.tbl-wrap{max-height:500px;overflow-y:auto;border:1px solid #eaecf0;border-radius:8px}
table{width:100%;border-collapse:collapse;font-size:.8rem}
thead th{position:sticky;top:0;background:#f7f8fa;padding:8px 10px;text-align:right;
  font-weight:600;cursor:pointer;white-space:nowrap;border-bottom:2px solid #e0e3ea;user-select:none}
thead th:first-child{text-align:left;min-width:120px}
thead th:hover{background:#eceff5}
thead th.asc::after{content:" ▲";font-size:.65em;color:#3b82f6}
thead th.desc::after{content:" ▼";font-size:.65em;color:#3b82f6}
tbody td{padding:6px 10px;text-align:right;border-bottom:1px solid #f2f3f5}
tbody td:first-child{text-align:left;font-weight:500;color:#1a1d3e}
tbody tr:hover td{background:rgba(59,130,246,.04)}

/* ── Factor buttons ── */
.fbtn-row{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:18px}
.fbtn{padding:5px 13px;border-radius:20px;border:1.5px solid #dde1e9;background:#fff;
  font-size:.78rem;cursor:pointer;font-weight:500;transition:all .15s}
.fbtn:hover{border-color:#3b82f6;color:#3b82f6}
.fbtn.active{background:#1a1d3e;color:#fff;border-color:#1a1d3e}
</style>
</head>
<body>

<div class="hdr">
  <h1>IEC-1 | India Equity Risk Model</h1>
  <div class="hdr-meta">
    <span>Date <b id="h-date">—</b></span>
    <span>Universe <b id="h-n">—</b></span>
    <span>R² <b id="h-r2">—</b></span>
    <span>History <b id="h-hist">—</b> days</span>
  </div>
</div>

<div class="tabs">
  <button class="tab-btn active" data-tab="fr">Factor Returns</button>
  <button class="tab-btn" data-tab="r2">R² History</button>
  <button class="tab-btn" data-tab="exp">Exposures</button>
  <button class="tab-btn" data-tab="tb">Top / Bottom 10</button>
</div>

<div id="p-fr" class="panel active">
  <div class="card">
    <div class="card-title">Cumulative Style Factor Returns</div>
    <div id="fr-empty" class="empty" style="display:none">Run the daily pipeline for at least 2 days to see return history.</div>
    <div class="chart-h400"><canvas id="fr-cv"></canvas></div>
  </div>
</div>

<div id="p-r2" class="panel">
  <div class="card">
    <div class="card-title">Daily Cross-Sectional R²</div>
    <div id="r2-empty" class="empty" style="display:none">No R² history yet — run the pipeline at least once.</div>
    <div class="chart-h400"><canvas id="r2-cv"></canvas></div>
  </div>
</div>

<div id="p-exp" class="panel">
  <div class="card">
    <div class="card-title">Factor Exposures by Stock &nbsp;<span style="font-weight:400;color:#888;font-size:.8rem">(z-scores, colour-coded)</span></div>
    <div id="exp-empty" class="empty" style="display:none">No exposure file found in output directory.</div>
    <div id="exp-body">
      <div class="tbl-bar">
        <input id="exp-q" type="text" placeholder="Search ticker…" oninput="filterExp()">
        <span class="tbl-count" id="exp-cnt"></span>
      </div>
      <div class="tbl-wrap"><table><thead id="exp-head"></thead><tbody id="exp-rows"></tbody></table></div>
    </div>
  </div>
</div>

<div id="p-tb" class="panel">
  <div class="card">
    <div class="card-title">Top &amp; Bottom 10 by Style Factor</div>
    <div id="tb-empty" class="empty" style="display:none">No exposure file found in output directory.</div>
    <div id="tb-body">
      <div class="fbtn-row" id="fb-row"></div>
      <div class="grid2">
        <div class="card" style="margin-bottom:0;border:1px solid #eaecf0;box-shadow:none">
          <div class="card-title" id="top-ttl">Top 10</div>
          <div class="chart-h280"><canvas id="top-cv"></canvas></div>
        </div>
        <div class="card" style="margin-bottom:0;border:1px solid #eaecf0;box-shadow:none">
          <div class="card-title" id="bot-ttl">Bottom 10</div>
          <div class="chart-h280"><canvas id="bot-cv"></canvas></div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const D = __IEC1_DATA__;

// ── Tab switching ─────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('p-' + btn.dataset.tab).classList.add('active');
  });
});

// ── Formatters ───────────────────────────────────────────────────────────────
const fmt2 = v => v === null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
const fmt3 = v => v === null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(3);
function cellBg(v) {
  if (v === null) return '';
  const a = (Math.min(Math.abs(v) / 2.5, 1) * 0.32).toFixed(2);
  return v > 0 ? `rgba(22,163,74,${a})` : `rgba(220,38,38,${a})`;
}

// ── Header ───────────────────────────────────────────────────────────────────
document.getElementById('h-date').textContent = D.run_date;
document.getElementById('h-n').textContent = D.n_stocks + ' stocks';
document.getElementById('h-hist').textContent = D.fr_history.dates.length;
if (D.r2_today !== null) {
  const cls = D.r2_today >= 0.5 ? 'pill-g' : D.r2_today >= 0.3 ? 'pill-y' : 'pill-r';
  const lbl = D.r2_today >= 0.5 ? 'good' : D.r2_today >= 0.3 ? 'low' : 'weak';
  document.getElementById('h-r2').innerHTML =
    D.r2_today.toFixed(3) + `<span class="pill ${cls}">${lbl}</span>`;
} else {
  document.getElementById('h-r2').textContent = '—';
}

// ── Factor Returns chart ──────────────────────────────────────────────────────
(function () {
  const fr = D.fr_history;
  if (fr.dates.length < 2) {
    document.getElementById('fr-empty').style.display = 'block';
    document.getElementById('fr-cv').style.display = 'none';
    return;
  }
  new Chart(document.getElementById('fr-cv'), {
    type: 'line',
    data: {
      labels: fr.dates,
      datasets: D.style_factors.map((f, i) => ({
        label: f,
        data: fr.cumulative[f] || [],
        borderColor: D.style_colors[i],
        backgroundColor: D.style_colors[i],
        borderWidth: 1.8, pointRadius: 0, tension: 0.15,
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 11, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => ` ${c.dataset.label}: ${fmt2(c.parsed.y)}` } },
      },
      scales: {
        x: { ticks: { maxTicksLimit: 12, font: { size: 11 } }, grid: { color: '#f0f0f0' } },
        y: { ticks: { callback: v => fmt2(v), font: { size: 11 } }, grid: { color: '#f0f0f0' } },
      },
    },
  });
})();

// ── R² chart ─────────────────────────────────────────────────────────────────
(function () {
  const r2 = D.r2_history;
  if (!r2.dates.length) {
    document.getElementById('r2-empty').style.display = 'block';
    document.getElementById('r2-cv').style.display = 'none';
    return;
  }
  const vals = r2.values.filter(v => v !== null);
  const avg = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
  new Chart(document.getElementById('r2-cv'), {
    type: 'line',
    data: {
      labels: r2.dates,
      datasets: [
        {
          label: 'R²',
          data: r2.values,
          borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,.08)',
          fill: true, borderWidth: 2,
          pointRadius: r2.dates.length < 60 ? 3 : 0, tension: 0.2,
        },
        avg !== null ? {
          label: `Avg ${avg.toFixed(3)}`,
          data: r2.dates.map(() => avg),
          borderColor: '#f59e0b', borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0,
        } : null,
      ].filter(Boolean),
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { position: 'bottom', labels: { font: { size: 11 } } } },
      scales: {
        x: { ticks: { maxTicksLimit: 12, font: { size: 11 } }, grid: { color: '#f0f0f0' } },
        y: { min: 0, max: 1, ticks: { callback: v => v.toFixed(2), font: { size: 11 } }, grid: { color: '#f0f0f0' } },
      },
    },
  });
})();

// ── Exposures table ───────────────────────────────────────────────────────────
(function () {
  const rows = D.exposures;
  if (!rows.length) {
    document.getElementById('exp-empty').style.display = 'block';
    document.getElementById('exp-body').style.display = 'none';
    return;
  }
  const fs = D.style_factors;
  // Build header
  const thead = document.getElementById('exp-head');
  const htr = document.createElement('tr');
  ['Ticker', ...fs].forEach((name, ci) => {
    const th = document.createElement('th');
    th.textContent = name;
    th.addEventListener('click', () => sort(ci));
    htr.appendChild(th);
  });
  thead.appendChild(htr);

  let sortCol = -1, sortAsc = true;

  function render(data) {
    const tb = document.getElementById('exp-rows');
    tb.innerHTML = '';
    data.forEach(r => {
      const tr = document.createElement('tr');
      const td0 = document.createElement('td');
      td0.textContent = r.ticker.replace('.NS', '');
      tr.appendChild(td0);
      fs.forEach(f => {
        const td = document.createElement('td');
        const v = r[f];
        td.textContent = fmt3(v);
        td.style.background = cellBg(v);
        if (v !== null && Math.abs(v) > 1.5) td.style.fontWeight = '600';
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    const q = document.getElementById('exp-q').value;
    document.getElementById('exp-cnt').textContent =
      data.length + ' of ' + rows.length + ' stocks shown';
  }

  function sort(ci) {
    document.querySelectorAll('#exp-head th').forEach((th, i) => {
      th.classList.remove('asc', 'desc');
    });
    if (sortCol === ci) sortAsc = !sortAsc; else { sortCol = ci; sortAsc = ci === 0; }
    const th = document.querySelectorAll('#exp-head th')[ci];
    th.classList.add(sortAsc ? 'asc' : 'desc');
    applyFilter();
  }

  function applyFilter() {
    const q = document.getElementById('exp-q').value.toLowerCase();
    let data = rows.filter(r => r.ticker.toLowerCase().includes(q) ||
                                r.ticker.replace('.NS','').toLowerCase().includes(q));
    if (sortCol >= 0) {
      data = [...data].sort((a, b) => {
        if (sortCol === 0) return sortAsc
          ? a.ticker.localeCompare(b.ticker) : b.ticker.localeCompare(a.ticker);
        const f = fs[sortCol - 1];
        const va = a[f] ?? -Infinity, vb = b[f] ?? -Infinity;
        return sortAsc ? va - vb : vb - va;
      });
    }
    render(data);
  }

  window.filterExp = applyFilter;
  render(rows);
  document.getElementById('exp-cnt').textContent = rows.length + ' of ' + rows.length + ' stocks shown';
})();

// ── Top / Bottom 10 ───────────────────────────────────────────────────────────
(function () {
  const rows = D.exposures;
  if (!rows.length) {
    document.getElementById('tb-empty').style.display = 'block';
    document.getElementById('tb-body').style.display = 'none';
    return;
  }
  const fs = D.style_factors;
  const cs = D.style_colors;

  // Build factor buttons
  const fbRow = document.getElementById('fb-row');
  fs.forEach((f, i) => {
    const btn = document.createElement('button');
    btn.className = 'fbtn' + (i === 0 ? ' active' : '');
    btn.textContent = f;
    btn.addEventListener('click', () => {
      document.querySelectorAll('.fbtn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      draw(f, i);
    });
    fbRow.appendChild(btn);
  });

  function hbar(canvasId, labels, data, color, title) {
    const ctx = document.getElementById(canvasId);
    const existing = Chart.getChart(ctx);
    if (existing) existing.destroy();
    document.getElementById(canvasId === 'top-cv' ? 'top-ttl' : 'bot-ttl').textContent = title;
    new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{ data, backgroundColor: color, borderRadius: 3, barThickness: 16 }],
      },
      options: {
        indexAxis: 'y', responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: c => ' ' + fmt3(c.parsed.x) } },
        },
        scales: {
          x: { ticks: { callback: v => fmt3(v), font: { size: 11 } }, grid: { color: '#f0f0f0' } },
          y: { ticks: { font: { size: 11 } } },
        },
      },
    });
  }

  function draw(factor, ci) {
    const sorted = [...rows]
      .filter(r => r[factor] !== null && r[factor] !== undefined)
      .sort((a, b) => b[factor] - a[factor]);

    // Top 10: highest first in array → highest at chart top
    const top = sorted.slice(0, 10);
    // Bottom 10: 10th-lowest first → most negative at chart bottom
    const bot = sorted.slice(-10);

    const strip = t => t.replace('.NS', '');
    hbar('top-cv', top.map(r => strip(r.ticker)), top.map(r => r[factor]),
         cs[ci] + 'cc', `Top 10 — ${factor}`);
    hbar('bot-cv', bot.map(r => strip(r.ticker)), bot.map(r => r[factor]),
         '#ef4444cc', `Bottom 10 — ${factor}`);
  }

  draw(fs[0], 0);
})();
</script>
</body>
</html>
"""


def build_html(data: dict) -> str:
    json_str = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    return _HTML.replace('__IEC1_DATA__', json_str)


def main(output_dir: str = "./inec1_outputs", out_file: str = "./dashboard.html") -> None:
    out = Path(output_dir)
    if not out.exists():
        print(f"Output directory not found: {out}", file=sys.stderr)
        sys.exit(1)
    data = load_data(out)
    html = build_html(data)
    Path(out_file).write_text(html, encoding="utf-8")
    size_kb = Path(out_file).stat().st_size // 1024
    print(
        f"Dashboard → {out_file}  "
        f"({data['n_stocks']} stocks · {len(data['fr_history']['dates'])} days history · {size_kb} KB)"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--output-dir", default="./inec1_outputs",
                   help="Directory containing inec1 output Parquet files")
    p.add_argument("--out", default="./dashboard.html",
                   help="Path for the generated HTML file")
    args = p.parse_args()
    main(args.output_dir, args.out)
