#!/usr/bin/env python3
"""
build_alphalens.py

Builds the AlphaLens public-facing website: a retail-investor-friendly page
that explains the theory behind factor attribution, tells the cautionary-tale
story with real data, and then shows the fund rankings.

Separate from build_mf_website.py (the internal analyst dashboard).

Usage:
    python build_alphalens.py
    python build_alphalens.py --mf-data ./mf_data --out alphalens.html
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
log = logging.getLogger("mf.alphalens")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--mf-data", default="./mf_data")
    p.add_argument("--out",     default="./alphalens.html")
    return p.parse_args()


def _pct(v, d=1):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return round(float(v) * 100, d)


def _f(v, d=2):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return round(float(v), d)


# ---------------------------------------------------------------------------
# Compute editorial stats
# ---------------------------------------------------------------------------

def compute_stats(attr: pd.DataFrame, scores: pd.DataFrame) -> dict:
    attr = attr.copy()
    attr["date"] = pd.to_datetime(attr["date"])

    avg = attr[["actual_ret", "market_ret", "style_ret", "industry_ret", "alpha"]].mean()
    total = avg["actual_ret"]

    fund_alpha_ann = attr.groupby("scheme_code")["alpha"].mean() * 12
    pct_pos_alpha  = float((fund_alpha_ann > 0).mean() * 100)

    all_dates = sorted(attr["date"].unique())
    p1_dates  = all_dates[:12]
    p2_dates  = all_dates[-12:]

    p1 = (attr[attr["date"].isin(p1_dates)]
          .groupby(["scheme_code", "scheme_name"])
          [["actual_ret", "market_ret", "style_ret", "industry_ret", "alpha"]].sum())
    p2 = (attr[attr["date"].isin(p2_dates)]
          .groupby(["scheme_code", "scheme_name"])
          [["actual_ret", "market_ret", "style_ret", "industry_ret", "alpha"]].sum())

    p1_label = (f"{pd.Timestamp(p1_dates[0]).strftime('%b %Y')} – "
                f"{pd.Timestamp(p1_dates[-1]).strftime('%b %Y')}")
    p2_label = (f"{pd.Timestamp(p2_dates[0]).strftime('%b %Y')} – "
                f"{pd.Timestamp(p2_dates[-1]).strftime('%b %Y')}")

    # Find the cautionary tale fund: well-known fund where style swing is biggest
    tale = None
    priority_keywords = ["hdfc elss", "hdfc focused", "kotak focused", "axis focused",
                         "sbi focused", "icici pru focused", "nippon india focused"]
    common_idx = p1.index.intersection(p2.index)
    style_swing = (p1.loc[common_idx, "style_ret"] -
                   p2.loc[common_idx, "style_ret"]).sort_values(ascending=False)

    def _get_tale_betas(code, p1_dates, p2_dates):
        """Average style/industry betas for this fund in each period."""
        fc = attr[attr["scheme_code"] == code]
        b_p1 = fc[fc["date"].isin(p1_dates)]
        b_p2 = fc[fc["date"].isin(p2_dates)]
        return {
            "p1_beta_S": _f(b_p1["beta_S"].mean()) if len(b_p1) else None,
            "p1_beta_I": _f(b_p1["beta_I"].mean()) if len(b_p1) else None,
            "p2_beta_S": _f(b_p2["beta_S"].mean()) if len(b_p2) else None,
            "p2_beta_I": _f(b_p2["beta_I"].mean()) if len(b_p2) else None,
        }

    for kw in priority_keywords:
        for (code, name) in style_swing.index:
            if kw in name.lower() and (code, name) in p2.index:
                r1 = p1.loc[(code, name)]
                r2 = p2.loc[(code, name)]
                if (r2["actual_ret"] < r1["actual_ret"] and
                        abs(r1["style_ret"] - r2["style_ret"]) > 0.05):
                    betas = _get_tale_betas(code, p1_dates, p2_dates)
                    tale = {
                        "name":    name.replace(" - Growth Option", "")
                                       .replace(" - Direct Plan", " Direct"),
                        "p1_label": p1_label,
                        "p2_label": p2_label,
                        "p1_total":    _pct(r1["actual_ret"]),
                        "p1_market":   _pct(r1["market_ret"]),
                        "p1_style":    _pct(r1["style_ret"]),
                        "p1_industry": _pct(r1["industry_ret"]),
                        "p1_alpha":    _pct(r1["alpha"]),
                        "p2_total":    _pct(r2["actual_ret"]),
                        "p2_market":   _pct(r2["market_ret"]),
                        "p2_style":    _pct(r2["style_ret"]),
                        "p2_industry": _pct(r2["industry_ret"]),
                        "p2_alpha":    _pct(r2["alpha"]),
                        **betas,
                    }
                    break
        if tale:
            break

    if tale is None:
        (code, name) = style_swing.index[0]
        r1 = p1.loc[(code, name)]; r2 = p2.loc[(code, name)]
        betas = _get_tale_betas(code, p1_dates, p2_dates)
        tale = {
            "name": name[:60], "p1_label": p1_label, "p2_label": p2_label,
            "p1_total": _pct(r1["actual_ret"]), "p1_market": _pct(r1["market_ret"]),
            "p1_style": _pct(r1["style_ret"]),  "p1_industry": _pct(r1["industry_ret"]),
            "p1_alpha": _pct(r1["alpha"]),
            "p2_total": _pct(r2["actual_ret"]), "p2_market": _pct(r2["market_ret"]),
            "p2_style": _pct(r2["style_ret"]),  "p2_industry": _pct(r2["industry_ret"]),
            "p2_alpha": _pct(r2["alpha"]),
            **betas,
        }

    # ── Category-level benchmark beat rates (active funds only) ──
    active_scores = scores[scores.get("category_display", scores.get("category", pd.Series())).isin([
        "Large Cap", "Mid Cap", "Small Cap", "Flexi Cap", "Multi Cap",
        "Large & Mid Cap", "Focused", "Value / Contrarian", "Tax Saver (ELSS)",
        "Thematic / Sectoral", "Balanced Advantage", "Hybrid"
    ])].copy() if "category_display" in scores.columns else scores.copy()

    cat_beat = {}
    if "net_active_ann" in scores.columns and "category_display" in scores.columns:
        grp = scores[scores["category_display"] != "Index Funds"].groupby("category_display")
        for cat, g in grp:
            total_cat = len(g)
            beat_cat  = int((g["net_active_ann"] > 0).sum())
            if total_cat >= 5:
                cat_beat[cat] = {"total": total_cat, "beat": beat_cat,
                                 "pct": round(beat_cat / total_cat * 100)}
    # Overall active beat rate
    active_only = scores[scores.get("category_display", pd.Series()) != "Index Funds"] \
        if "category_display" in scores.columns else scores
    overall_beat = round(float((active_only["net_active_ann"] > 0).mean() * 100)) \
        if "net_active_ann" in active_only.columns else 0

    return {
        "n_funds":           len(scores),
        "pct_pos_alpha":     round(pct_pos_alpha),
        "avg_total_ann":     round(float(total * 12 * 100), 1),
        "avg_style_ann":     round(float(avg["style_ret"] * 12 * 100), 1),
        "avg_alpha_ann":     round(float(avg["alpha"] * 12 * 100), 1),
        "overall_beat":      overall_beat,
        "cat_beat":          cat_beat,
        "tale":              tale,
        "as_of":             pd.Timestamp(all_dates[-1]).strftime("%B %Y"),
    }


# ---------------------------------------------------------------------------
# Prepare scores for JS
# ---------------------------------------------------------------------------

def prepare_leaderboard(scores: pd.DataFrame, attr: pd.DataFrame) -> tuple[list, dict]:
    rows = []
    for _, r in scores.iterrows():
        rows.append({
            "code":     int(r["scheme_code"]),
            "name":     str(r.get("scheme_name", "")),
            "amc":      str(r.get("amc", "")).replace("_", " "),
            "score":    _f(r.get("score"), 1),
            "ir":       _f(r.get("info_ratio"), 2),
            "alpha":    _pct(r.get("alpha_ann"), 1),
            "ret":      _pct(r.get("total_return_ann"), 1),
            "mret":     _pct(r.get("market_ret_ann"), 1),
            "aret":     _pct(r.get("active_ret_ann"), 1),
            "te":       _pct(r.get("tracking_error_ann"), 1),
            "bM":       _f(r.get("beta_M"), 2),
            "aq":       _f(r.get("alpha_quality"), 3),
            "ap":       _f(r.get("alpha_proportion"), 3),
            "ss":       _f(r.get("style_stability"), 3),
            "pf":       _f(r.get("positioning"), 3),
            "rbS":      _f(r.get("recent_beta_S"), 2),
            "rbI":      _f(r.get("recent_beta_I"), 2),
            "cae":      _f(r.get("current_active_exposure"), 2),
            "bdr":      _f(r.get("beta_drift"), 2),
            "r2":       _f(r.get("r_squared"), 2),
            # New fields from scored_funds.parquet
            "cat":      str(r.get("category_display", r.get("category", ""))),
            "catRank":  int(r["cat_rank"])  if pd.notna(r.get("cat_rank"))  else None,
            "catSize":  int(r["cat_size"])  if pd.notna(r.get("cat_size"))  else None,
            "hrate":    _f(r.get("hit_rate"), 3),
            "ter":      _f(r.get("ter_est"), 4),
            "navOnly":  bool(r["nav_only"]) if pd.notna(r.get("nav_only")) else True,
            "skill":    str(r.get("skill_label", "")),
            "dStyle":   _f(r.get("d_style"), 1),
            "dSector":  _f(r.get("d_sector"), 1),
            "dPick":    _f(r.get("d_pick"), 1),
            "dTiming":  _f(r.get("d_timing"), 1),
            "pickAnn":  _f(r.get("pick_ann_pp"), 1),
            "pickHit":  _f(r.get("pick_hit_rate"), 3),
            "decomp":   bool(r["decomp_ok"]) if pd.notna(r.get("decomp_ok")) else False,
        })

    attr_by_fund = {}
    attr["date"] = pd.to_datetime(attr["date"])
    for sc, grp in attr.groupby("scheme_code"):
        grp = grp.sort_values("date")
        attr_by_fund[str(int(sc))] = [
            {
                "date":  row["date"].strftime("%Y-%m"),
                "ret":   _pct(row.get("actual_ret"), 2),
                "mkt":   _pct(row.get("market_ret"), 2),
                "sty":   _pct(row.get("style_ret"), 2),
                "ind":   _pct(row.get("industry_ret"), 2),
                "alpha": _pct(row.get("alpha"), 2),
                "bS":    _f(row.get("beta_S"), 2),
                "bI":    _f(row.get("beta_I"), 2),
            }
            for _, row in grp.iterrows()
        ]

    return rows, attr_by_fund


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def build_html(stats: dict, leaderboard: list, attr_by_fund: dict) -> str:
    tale = stats["tale"]
    scores_json = json.dumps(leaderboard, separators=(",", ":"))
    attr_json   = json.dumps(attr_by_fund, separators=(",", ":"))
    stats_json  = json.dumps(stats, separators=(",", ":"))

    # Beta delta string for the tale
    p1bS = tale.get("p1_beta_S")
    p2bS = tale.get("p2_beta_S")
    beta_delta_html = ""
    if p1bS is not None and p2bS is not None:
        delta = round(abs(p2bS - p1bS), 2)
        beta_delta_html = (
            f'<strong>Style exposure (beta): {p1bS:+.2f} → {p2bS:+.2f}</strong> '
            f'(changed by only {delta:.2f}). '
            f'The exposure was there the whole time. '
            f'What changed was the <em>market environment</em> for that style — '
            f'turning a {p1bS:+.2f} × (positive factor return) '
            f'into a {p2bS:+.2f} × (negative factor return).'
        )
    else:
        beta_delta_html = (
            "The manager's style <em>exposure</em> was similar in both periods. "
            "What changed was the factor environment — the same bet, reversed outcome."
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlphaLens — See what's really driving your mutual fund</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root {{
  --bg:      #F5F7FC;
  --bg2:     #FFFFFF;
  --bg3:     #EDF0F8;
  --bg4:     #E2E6F0;
  --border:  #D4D9E8;
  --text:    #1A1F36;
  --muted:   #6B7490;
  --accent:  #3B6EE8;
  --green:   #059669;
  --red:     #DC2626;
  --yellow:  #D97706;
  --purple:  #7C3AED;
  --orange:  #EA580C;
  --market-c:#3B6EE8;
  --style-c: #7C3AED;
  --ind-c:   #D97706;
  --alpha-c: #059669;
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
html {{ scroll-behavior:smooth; }}
body {{ background:var(--bg); color:var(--text);
        font-family:'Segoe UI',system-ui,sans-serif; font-size:15px; line-height:1.6; }}

/* ---- NAV ---- */
nav {{ position:sticky; top:0; z-index:100;
       background:rgba(245,247,252,0.95); backdrop-filter:blur(12px);
       border-bottom:1px solid var(--border);
       padding:0 32px; height:56px;
       display:flex; align-items:center; justify-content:space-between; }}
.nav-logo {{ font-size:18px; font-weight:800; color:var(--accent); letter-spacing:-0.5px; }}
.nav-logo span {{ color:var(--green); }}
.nav-links a {{ color:var(--muted); text-decoration:none; font-size:13px; font-weight:500;
                 margin-left:24px; transition:color 0.15s; }}
.nav-links a:hover {{ color:var(--text); }}

/* ---- SECTIONS ---- */
section {{ padding:80px 0; }}
.container {{ max-width:960px; margin:0 auto; padding:0 32px; }}
.container-wide {{ max-width:1200px; margin:0 auto; padding:0 32px; }}

/* ---- HERO ---- */
.hero {{ padding:80px 0 64px; }}
.hero-eyebrow {{ font-size:11px; font-weight:700; letter-spacing:2.5px; color:var(--accent);
                  text-transform:uppercase; margin-bottom:16px; }}
.hero h1 {{ font-size:clamp(28px,3.5vw,46px); font-weight:800; line-height:1.15;
             letter-spacing:-1px; margin-bottom:18px; color:var(--text); }}
.hero h1 em {{ color:var(--accent); font-style:normal; }}
.hero-sub {{ font-size:17px; color:var(--muted); max-width:560px;
              line-height:1.75; margin-bottom:32px; }}

/* Stat pills */
.hero-stats {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:0; }}
.stat-pill {{ background:var(--bg2); border:1px solid var(--border);
               border-radius:12px; padding:14px 22px; text-align:center; }}
.stat-pill .sp-num {{ font-size:30px; font-weight:800; display:block; letter-spacing:-0.5px; }}
.stat-pill .sp-label {{ font-size:11px; color:var(--muted); text-transform:uppercase;
                          letter-spacing:0.5px; margin-top:3px; line-height:1.4; }}

/* ---- SECTION HEADER ---- */
.section-label {{ font-size:10px; font-weight:700; letter-spacing:2.5px; text-transform:uppercase;
                   color:var(--accent); margin-bottom:10px; }}
.section-title {{ font-size:clamp(22px,3.5vw,34px); font-weight:800; letter-spacing:-0.6px;
                   margin-bottom:14px; line-height:1.2; color:var(--text); }}
.section-body {{ font-size:15px; color:var(--muted); line-height:1.8; max-width:680px; }}
.section-body strong {{ color:var(--text); }}

/* ---- BASELINE CALLOUT ---- */
.baseline-callout {{
  background: linear-gradient(135deg, rgba(93,142,240,0.08) 0%, rgba(93,142,240,0.03) 100%);
  border: 1px solid rgba(93,142,240,0.25);
  border-radius:12px; padding:24px 28px; margin:32px 0;
  display:flex; gap:16px; align-items:flex-start;
}}
.bc-icon {{ font-size:24px; flex-shrink:0; margin-top:2px; }}
.bc-body {{ font-size:14px; color:var(--muted); line-height:1.75; }}
.bc-body strong {{ color:var(--accent); }}

/* ---- BUCKETS ---- */
.buckets {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
             gap:16px; margin-top:32px; }}
.bucket-card {{ background:var(--bg2); border:1px solid var(--border); border-radius:12px;
                 padding:24px; position:relative; overflow:hidden; }}
.bucket-card::before {{ content:''; position:absolute; top:0; left:0; right:0; height:3px; }}
.bucket-card.market::before {{ background:var(--market-c); }}
.bucket-card.style::before  {{ background:var(--style-c); }}
.bucket-card.industry::before {{ background:var(--ind-c); }}
.bucket-card.alpha::before  {{ background:var(--alpha-c); }}
.bucket-icon {{ font-size:28px; margin-bottom:12px; }}
.bucket-tag {{ display:inline-block; font-size:10px; font-weight:700; text-transform:uppercase;
               letter-spacing:0.5px; padding:2px 8px; border-radius:4px; margin-bottom:8px; }}
.bucket-tag.baseline {{ background:rgba(93,142,240,0.15); color:var(--market-c); }}
.bucket-tag.active {{ background:rgba(52,211,153,0.12); color:var(--green); }}
.bucket-name {{ font-size:13px; font-weight:700; text-transform:uppercase;
                 letter-spacing:0.5px; margin-bottom:8px; }}
.bucket-name.market {{ color:var(--market-c); }}
.bucket-name.style  {{ color:var(--style-c); }}
.bucket-name.industry {{ color:var(--ind-c); }}
.bucket-name.alpha  {{ color:var(--alpha-c); }}
.bucket-desc {{ font-size:14px; color:var(--muted); line-height:1.65; }}
.bucket-stat {{ margin-top:14px; font-size:12px; background:var(--bg3); border-radius:6px;
                 padding:8px 12px; color:var(--muted); }}
.bucket-stat strong {{ color:var(--text); }}

/* ---- AVERAGE BAR ---- */
.avg-bar-section {{ margin-top:56px; }}
.avg-bar-label {{ font-size:13px; color:var(--muted); margin-bottom:10px; }}
.avg-bar {{ display:flex; height:36px; border-radius:8px; overflow:hidden; }}
.avg-bar-seg {{ display:flex; align-items:center; justify-content:center;
                 font-size:11px; font-weight:700; color:rgba(255,255,255,0.85);
                 transition:width 0.6s ease; white-space:nowrap; overflow:hidden; }}
.avg-bar-legend {{ display:flex; gap:20px; margin-top:12px; flex-wrap:wrap; }}
.legend-item {{ display:flex; align-items:center; gap:6px; font-size:12px; color:var(--muted); }}
.legend-dot {{ width:10px; height:10px; border-radius:2px; flex-shrink:0; }}

/* ---- CAUTIONARY TALE ---- */
.tale-section {{ background:var(--bg2); border-top:1px solid var(--border);
                  border-bottom:1px solid var(--border); }}
.tale-intro {{ margin-bottom:40px; }}
.tale-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; margin-top:32px; }}
.tale-card {{ background:var(--bg3); border-radius:12px; padding:28px; border:1px solid var(--border); }}
.tale-card .period {{ font-size:11px; font-weight:700; text-transform:uppercase;
                       letter-spacing:0.5px; color:var(--muted); margin-bottom:8px; }}
.tale-card .total-ret {{ font-size:42px; font-weight:800; margin-bottom:20px; letter-spacing:-1px; }}
.tale-card .bars {{ display:flex; flex-direction:column; gap:8px; }}
.tale-bar-row {{ display:flex; align-items:center; gap:10px; }}
.tale-bar-label {{ font-size:11px; color:var(--muted); width:72px; flex-shrink:0; text-align:right; }}
.tale-bar-wrap {{ flex:1; height:20px; background:var(--bg4); border-radius:4px; overflow:hidden;
                   position:relative; }}
.tale-bar-fill {{ height:100%; border-radius:4px; position:absolute; top:0;
                   transition:width 0.5s ease; min-width:2px; }}
.tale-bar-val {{ font-size:11px; font-weight:700; width:48px; text-align:right;
                  flex-shrink:0; font-variant-numeric:tabular-nums; font-family:monospace; }}
.tale-highlight {{ margin-top:28px; background:rgba(93,142,240,0.08);
                    border:1px solid rgba(93,142,240,0.2); border-radius:8px;
                    padding:16px; font-size:14px; color:var(--muted); line-height:1.7; }}
.tale-highlight strong {{ color:var(--accent); }}
.tale-insight {{ margin-top:32px; padding:24px; background:var(--bg3);
                  border-radius:12px; border-left:3px solid var(--alpha-c); }}
.tale-insight p {{ color:var(--muted); font-size:15px; line-height:1.75; }}
.tale-insight strong {{ color:var(--text); }}
.tale-beta-box {{ margin-top:24px; padding:20px 24px; background:var(--bg3);
                   border-radius:12px; border:1px solid var(--border); }}
.tale-beta-box h4 {{ font-size:13px; font-weight:700; color:var(--purple);
                      text-transform:uppercase; letter-spacing:0.5px; margin-bottom:12px; }}
.tale-beta-compare {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }}
.tbc-cell {{ background:var(--bg4); border-radius:8px; padding:14px 16px; }}
.tbc-period {{ font-size:10px; color:var(--muted); text-transform:uppercase;
                letter-spacing:0.5px; margin-bottom:6px; }}
.tbc-beta {{ font-size:22px; font-weight:800; color:var(--purple); }}
.tbc-ret  {{ font-size:12px; margin-top:4px; color:var(--muted); }}
.tale-beta-insight {{ font-size:14px; color:var(--muted); line-height:1.75; }}
.tale-beta-insight strong {{ color:var(--text); }}
.tale-beta-insight em {{ color:var(--purple); font-style:normal; font-weight:600; }}

/* ---- SCORE CARDS ---- */
.score-cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
                 gap:20px; margin-top:40px; }}
.score-card {{ background:var(--bg2); border:1px solid var(--border); border-radius:12px;
                padding:28px; }}
.sc-pts {{ font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:0.5px;
            margin-bottom:12px; }}
.sc-pts.a {{ color:var(--yellow); }}
.sc-pts.b {{ color:var(--green); }}
.sc-pts.c {{ color:var(--purple); }}
.sc-pts.d {{ color:var(--orange); }}
.sc-name {{ font-size:20px; font-weight:800; margin-bottom:10px; }}
.sc-sub  {{ font-size:13px; color:var(--muted); font-weight:600; margin-bottom:12px; }}
.sc-desc {{ font-size:14px; color:var(--muted); line-height:1.7; }}
.sc-example {{ margin-top:14px; background:var(--bg3); border-radius:6px;
                padding:10px 14px; font-size:13px; color:var(--muted); }}
.sc-example strong {{ color:var(--text); }}

/* ---- RANKINGS ---- */
.rankings-section {{ background:var(--bg); }}
.controls {{ display:flex; gap:12px; margin-bottom:20px; flex-wrap:wrap; align-items:flex-end; }}
.ctrl-group label {{ display:block; font-size:11px; text-transform:uppercase;
                      letter-spacing:0.5px; color:var(--muted); margin-bottom:6px; }}
.ctrl-group input, .ctrl-group select {{
  background:var(--bg2); border:1px solid var(--border); color:var(--text);
  padding:9px 14px; border-radius:8px; font-size:13px; }}
.ctrl-group input:focus, .ctrl-group select:focus {{
  outline:none; border-color:var(--accent); }}
.result-count {{ color:var(--muted); font-size:13px; margin-left:auto; align-self:flex-end; padding-bottom:2px; }}

.table-wrap {{ border-radius:10px; border:1px solid var(--border); overflow:hidden; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
thead th {{ background:var(--bg3); padding:11px 14px; text-align:left; color:var(--muted);
             font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:0.5px;
             cursor:pointer; user-select:none; white-space:nowrap; }}
thead th:hover {{ color:var(--text); }}
thead th.sorted-asc::after  {{ content:" ↑"; color:var(--accent); }}
thead th.sorted-desc::after {{ content:" ↓"; color:var(--accent); }}
tbody tr {{ border-top:1px solid var(--border); cursor:pointer; transition:background 0.1s; }}
tbody tr:hover {{ background:var(--bg3); }}
td {{ padding:11px 14px; }}
.td-name {{ max-width:280px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-weight:600; }}
.td-amc  {{ color:var(--muted); font-size:11px; }}
.td-r {{ text-align:right; font-variant-numeric:tabular-nums; font-family:monospace; }}
.score-badge {{ display:inline-block; padding:3px 12px; border-radius:20px; font-weight:800;
                 font-size:14px; min-width:46px; text-align:center; }}
.sh {{ background:rgba(52,211,153,0.15); color:var(--green); }}
.sm {{ background:rgba(251,191,36,0.15);  color:var(--yellow); }}
.sl {{ background:rgba(248,113,113,0.15); color:var(--red); }}

/* ---- FUND DETAIL DRAWER ---- */
.drawer-overlay {{ position:fixed; inset:0; background:rgba(26,31,54,0.5);
                    z-index:200; display:none; }}
.drawer-overlay.open {{ display:block; }}
.drawer {{ position:fixed; top:0; right:0; bottom:0; width:min(680px,100vw);
            background:var(--bg2); border-left:1px solid var(--border);
            z-index:201; overflow-y:auto; transform:translateX(100%);
            transition:transform 0.3s ease; padding:32px; }}
.drawer.open {{ transform:translateX(0); }}
.drawer-close {{ position:absolute; top:20px; right:20px; background:var(--bg3);
                  border:1px solid var(--border); color:var(--muted); width:36px; height:36px;
                  border-radius:8px; cursor:pointer; font-size:18px; display:flex;
                  align-items:center; justify-content:center; }}
.drawer-close:hover {{ color:var(--text); }}
.drawer-fund-name {{ font-size:20px; font-weight:800; margin:40px 0 4px; line-height:1.3; }}
.drawer-amc {{ display:inline-block; background:var(--bg3); color:var(--muted);
                padding:3px 10px; border-radius:4px; font-size:11px; font-weight:600;
                text-transform:uppercase; margin-bottom:24px; }}
.drawer-score-row {{ display:flex; gap:12px; margin-bottom:16px; flex-wrap:wrap; }}
.drawer-score-box {{ background:var(--bg3); border-radius:8px; padding:12px 16px;
                      flex:1; min-width:90px; text-align:center; }}
.dsb-val   {{ font-size:20px; font-weight:800; display:block; }}
.dsb-label {{ font-size:10px; color:var(--muted); text-transform:uppercase;
               letter-spacing:0.5px; margin-top:4px; display:block; }}
.drawer-chart-wrap {{ height:220px; margin-bottom:24px; position:relative; }}
.drawer-chart-wrap2 {{ height:160px; margin-bottom:24px; position:relative; }}
.drawer-section-title {{ font-size:11px; font-weight:700; text-transform:uppercase;
                           letter-spacing:0.5px; color:var(--muted); margin:20px 0 12px; }}
.explain-row {{ display:flex; gap:8px; align-items:flex-start; margin-bottom:8px; font-size:13px; }}
.explain-dot {{ width:10px; height:10px; border-radius:2px; flex-shrink:0; margin-top:4px; }}
.explain-text {{ color:var(--muted); line-height:1.6; }}
.explain-text strong {{ color:var(--text); }}
.risk-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:12px; }}
.risk-cell {{ background:var(--bg3); border-radius:8px; padding:12px 14px; }}
.risk-label {{ font-size:10px; color:var(--muted); text-transform:uppercase;
                letter-spacing:0.5px; margin-bottom:4px; }}
.risk-val {{ font-size:20px; font-weight:800; }}
.risk-note {{ font-size:11px; color:var(--muted); margin-top:4px; line-height:1.4; }}
.risk-alert {{ background:rgba(251,191,36,0.08); border:1px solid rgba(251,191,36,0.2);
               border-radius:8px; padding:12px 14px; font-size:13px;
               color:var(--muted); line-height:1.6; margin-top:8px; }}
.risk-alert strong {{ color:var(--yellow); }}

/* ---- METHODOLOGY ---- */
.method-section {{ background:var(--bg2); border-top:1px solid var(--border); }}
.accordion-item {{ border-bottom:1px solid var(--border); }}
.accordion-q {{ padding:18px 0; cursor:pointer; display:flex; justify-content:space-between;
                 align-items:center; font-weight:600; user-select:none; }}
.accordion-q:hover {{ color:var(--accent); }}
.accordion-a {{ display:none; padding-bottom:20px; color:var(--muted);
                 font-size:14px; line-height:1.8; max-width:700px; }}
.accordion-a.open {{ display:block; }}

/* ---- FOOTER ---- */
footer {{ padding:48px 32px; text-align:center; color:var(--muted); font-size:12px;
           border-top:1px solid var(--border); line-height:1.8; }}
footer strong {{ color:var(--text); }}

/* ---- UTILS ---- */
.positive {{ color:var(--green); }}
.negative {{ color:var(--red); }}
.divider {{ height:1px; background:var(--border); margin:64px 0; }}

@media(max-width:700px) {{
  .hero h1 {{ font-size:28px; }}
  .tale-grid {{ grid-template-columns:1fr; }}
  .tale-beta-compare {{ grid-template-columns:1fr 1fr; }}
  .score-cards {{ grid-template-columns:1fr; }}
  nav {{ padding:0 16px; }}
  .container {{ padding:0 16px; }}
  section {{ padding:56px 0; }}
}}
</style>
</head>
<body>

<!-- NAV -->
<nav>
  <div class="nav-logo">Alpha<span>Lens</span></div>
  <div class="nav-links">
    <a href="#how-it-works">How it works</a>
    <a href="#rankings">Rankings</a>
  </div>
</nav>

<!-- HERO + SPIVA COMBINED -->
<section class="hero">
  <div class="container">
    <div style="display:grid;grid-template-columns:1fr auto;gap:48px;align-items:center;text-align:left">
      <div>
        <div class="hero-eyebrow">India's most data-driven mutual fund rankings · Updated daily</div>
        <h1>Most mutual funds<br>lose to the index.<br><em>We find the ones that don't.</em></h1>
        <p class="hero-sub">
          Over 10 years, most active funds in India have failed to beat a simple index fund.
          But the ones that do outperform can make a meaningful difference to your wealth.
          AlphaLens identifies them — daily.
        </p>

        <!-- SPIVA stats — large, glaring -->
        <div style="margin-bottom:24px">
          <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--muted);margin-bottom:16px">
            Over 10 years, how many active funds beat the index? · SPIVA India 2025
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:10px">
            <div style="background:rgba(220,38,38,0.06);border:1.5px solid rgba(220,38,38,0.2);border-radius:12px;padding:20px 18px">
              <div style="font-size:11px;font-weight:600;color:var(--muted);margin-bottom:6px">Large Cap funds</div>
              <div style="font-size:38px;font-weight:800;color:var(--red);letter-spacing:-1px;line-height:1">73%</div>
              <div style="font-size:13px;font-weight:700;color:var(--red);margin-top:6px">lost to the index</div>
            </div>
            <div style="background:rgba(220,38,38,0.06);border:1.5px solid rgba(220,38,38,0.2);border-radius:12px;padding:20px 18px">
              <div style="font-size:11px;font-weight:600;color:var(--muted);margin-bottom:6px">Mid &amp; Small Cap funds</div>
              <div style="font-size:38px;font-weight:800;color:var(--red);letter-spacing:-1px;line-height:1">82%</div>
              <div style="font-size:13px;font-weight:700;color:var(--red);margin-top:6px">lost to the index</div>
            </div>
          </div>
          <div style="font-size:11px;color:var(--muted)">Source: SPIVA India Mid-Year 2025 · S&P Dow Jones Indices · 10-year period ending June 2025</div>
        </div>

        <!-- Philosophy -->
        <div style="background:#fff;border:1.5px solid rgba(59,110,232,0.2);border-radius:10px;padding:14px 18px;font-size:13px;color:var(--muted);line-height:1.7">
          We don't say avoid active funds. Index funds are great for the core of your portfolio.
          But <strong style="color:var(--text)">a portion belongs in the best actively managed funds</strong> — the ones that
          genuinely outperform. The hard part is finding them. That's AlphaLens.
        </div>
      </div>
      <!-- SVG artwork: stylized lens / data universe -->
      <div style="flex-shrink:0;display:flex;align-items:center;justify-content:center">
        <svg width="260" height="260" viewBox="0 0 260 260" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
          <!-- Outer ring -->
          <circle cx="120" cy="120" r="108" stroke="#D4D9E8" stroke-width="1.5" stroke-dasharray="4 6"/>
          <!-- Mid ring -->
          <circle cx="120" cy="120" r="78" stroke="#D4D9E8" stroke-width="1" stroke-dasharray="3 5"/>
          <!-- Inner circle (lens) -->
          <circle cx="120" cy="120" r="52" fill="#EDF0F8" stroke="#3B6EE8" stroke-width="2"/>
          <circle cx="120" cy="120" r="52" fill="url(#lensGrad)" opacity="0.6"/>
          <!-- Lens cross-hairs -->
          <line x1="120" y1="78" x2="120" y2="162" stroke="#3B6EE8" stroke-width="0.8" stroke-opacity="0.4"/>
          <line x1="78" y1="120" x2="162" y2="120" stroke="#3B6EE8" stroke-width="0.8" stroke-opacity="0.4"/>
          <!-- Centre dot -->
          <circle cx="120" cy="120" r="5" fill="#3B6EE8"/>
          <!-- Fund data dots — scattered around the rings, color-coded by score -->
          <circle cx="120" cy="18" r="5" fill="#059669" opacity="0.85"/>
          <circle cx="178" cy="36" r="4" fill="#059669" opacity="0.7"/>
          <circle cx="212" cy="82" r="5" fill="#D97706" opacity="0.8"/>
          <circle cx="218" cy="140" r="4" fill="#059669" opacity="0.65"/>
          <circle cx="190" cy="192" r="5" fill="#DC2626" opacity="0.75"/>
          <circle cx="136" cy="222" r="4" fill="#D97706" opacity="0.7"/>
          <circle cx="70" cy="214" r="5" fill="#059669" opacity="0.8"/>
          <circle cx="26" cy="162" r="4" fill="#DC2626" opacity="0.65"/>
          <circle cx="18" cy="96" r="5" fill="#D97706" opacity="0.75"/>
          <circle cx="54" cy="40" r="4" fill="#059669" opacity="0.8"/>
          <!-- Mid-ring dots -->
          <circle cx="120" cy="48" r="4" fill="#3B6EE8" opacity="0.7"/>
          <circle cx="172" cy="72" r="3.5" fill="#059669" opacity="0.8"/>
          <circle cx="192" cy="130" r="4" fill="#D97706" opacity="0.7"/>
          <circle cx="158" cy="178" r="3.5" fill="#059669" opacity="0.75"/>
          <circle cx="80" cy="178" r="4" fill="#DC2626" opacity="0.65"/>
          <circle cx="50" cy="120" r="3.5" fill="#059669" opacity="0.7"/>
          <circle cx="72" cy="62" r="4" fill="#3B6EE8" opacity="0.65"/>
          <!-- Connecting lines from centre to top dots (subtle) -->
          <line x1="120" y1="120" x2="120" y2="48" stroke="#3B6EE8" stroke-width="0.5" stroke-opacity="0.2"/>
          <line x1="120" y1="120" x2="172" y2="72" stroke="#059669" stroke-width="0.5" stroke-opacity="0.2"/>
          <line x1="120" y1="120" x2="158" y2="178" stroke="#059669" stroke-width="0.5" stroke-opacity="0.2"/>
          <!-- Magnifying glass handle -->
          <line x1="162" y1="162" x2="232" y2="232" stroke="#6B7490" stroke-width="7" stroke-linecap="round"/>
          <line x1="162" y1="162" x2="232" y2="232" stroke="#D4D9E8" stroke-width="3" stroke-linecap="round"/>
          <!-- Score legend dots (bottom right) -->
          <circle cx="198" cy="230" r="4" fill="#059669"/>
          <text x="208" y="234" font-size="9" fill="#6B7490" font-family="system-ui">High skill</text>
          <circle cx="198" cy="244" r="4" fill="#D97706"/>
          <text x="208" y="248" font-size="9" fill="#6B7490" font-family="system-ui">Average</text>
          <defs>
            <radialGradient id="lensGrad" cx="40%" cy="35%" r="60%">
              <stop offset="0%" stop-color="#FFFFFF" stop-opacity="0.9"/>
              <stop offset="100%" stop-color="#3B6EE8" stop-opacity="0.08"/>
            </radialGradient>
          </defs>
        </svg>
      </div>
    </div>
  </div>
</section>

<!-- SECTION 2: CALCULATOR -->
<section id="how-it-works" style="background:var(--bg2);border-top:1px solid var(--border);padding:72px 0">
  <div class="container">
    <div style="text-align:center;max-width:560px;margin:0 auto 40px">
      <div class="section-label">See the difference</div>
      <h2 class="section-title">The right fund grows your money <em style="color:var(--green)">dramatically faster</em> over time.</h2>
    </div>

    <div style="max-width:600px;margin:0 auto;background:var(--bg3);border-radius:16px;padding:32px;border:1px solid var(--border)">
      <!-- Mode toggle -->
      <div style="display:flex;gap:0;background:var(--bg4);border-radius:8px;padding:3px;margin-bottom:24px;width:fit-content">
        <button id="modeLS" onclick="calcSetMode('lumpsum')"
          style="padding:8px 24px;border-radius:6px;border:none;font-size:13px;font-weight:600;cursor:pointer;background:var(--accent);color:#fff">Lump Sum</button>
        <button id="modeSIP" onclick="calcSetMode('sip')"
          style="padding:8px 24px;border-radius:6px;border:none;font-size:13px;font-weight:600;cursor:pointer;background:transparent;color:var(--muted)">Monthly SIP</button>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
        <div>
          <label style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);display:block;margin-bottom:6px" id="amtLabel">Amount (₹)</label>
          <input type="number" id="calcAmount" value="100000" min="1000" step="1000"
            oninput="calcUpdate()"
            style="width:100%;padding:10px 14px;border:1px solid var(--border);border-radius:8px;font-size:15px;font-weight:600;background:#fff;color:var(--text)">
        </div>
        <div>
          <label style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);display:block;margin-bottom:6px">Time: <span id="yearsLabel">15 years</span></label>
          <input type="range" id="calcYears" min="5" max="25" value="15" step="1"
            oninput="calcUpdate()"
            style="width:100%;margin-top:12px;accent-color:var(--accent)">
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:4px"><span>5 yrs</span><span>25 yrs</span></div>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px">
        <div style="background:#fff;border-radius:10px;padding:16px;border:1.5px solid rgba(5,150,105,0.25)">
          <div style="font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--green);font-weight:700;margin-bottom:6px">AlphaLens picks</div>
          <div id="calcAlpha" style="font-size:26px;font-weight:800;color:var(--text);letter-spacing:-0.5px">₹10.67L</div>
        </div>
        <div style="background:#fff;border-radius:10px;padding:16px;border:1px solid var(--border)">
          <div style="font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:700;margin-bottom:6px">Average fund</div>
          <div id="calcAvg" style="font-size:26px;font-weight:800;color:var(--muted);letter-spacing:-0.5px">₹5.93L</div>
        </div>
      </div>
      <div id="calcDiff" style="background:rgba(5,150,105,0.08);border:1px solid rgba(5,150,105,0.2);border-radius:8px;padding:12px 16px;font-size:14px;color:var(--text);text-align:center;font-weight:600"></div>
      <p style="font-size:10px;color:var(--muted);margin-top:12px;line-height:1.6;text-align:center">
        Based on AlphaLens historical picks (16.3%/yr) vs average regular plan fund (11.3%/yr). Illustrative only. Past performance is not indicative of future results.
      </p>
    </div>
  </div>
</section>

<!-- SECTION: HOW WE FIND WINNERS -->
<section style="background:var(--bg);border-top:1px solid var(--border);padding:72px 0">
  <div class="container">
    <div style="text-align:center;max-width:680px;margin:0 auto 48px">
      <div class="section-label">How AlphaLens works</div>
      <h2 class="section-title">Every fund return has four ingredients. Only one of them is skill. We bet on managers who have it.</h2>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px">

      <div style="background:var(--bg2);border:1px solid var(--border);border-radius:14px;padding:28px;border-top:3px solid var(--market-c)">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
          <div style="width:10px;height:10px;border-radius:2px;background:var(--market-c);flex-shrink:0"></div>
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--market-c)">The market rising</div>
        </div>
        <div style="font-size:16px;font-weight:800;color:var(--text);margin-bottom:10px">Most returns aren't skill — they're the market</div>
        <p style="font-size:13px;color:var(--muted);line-height:1.75">When equity markets rise, every fund rises. This is not the manager's doing — any index fund captures this at a fraction of the cost. Yet most fund returns are explained by this alone.</p>
      </div>

      <div style="background:var(--bg2);border:1px solid var(--border);border-radius:14px;padding:28px;border-top:3px solid var(--style-c)">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
          <div style="width:10px;height:10px;border-radius:2px;background:var(--style-c);flex-shrink:0"></div>
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--style-c)">Style & sector bets</div>
        </div>
        <div style="font-size:16px;font-weight:800;color:var(--text);margin-bottom:10px">Factor tilts look like skill — until they reverse</div>
        <p style="font-size:13px;color:var(--muted);line-height:1.75">Leaning into small-caps, value stocks, or overweighting banks can drive outperformance for years. But these are market conditions, not manager skill. When the tide turns, the same fund looks like it lost its edge.</p>
      </div>

      <div style="background:var(--bg2);border:1px solid var(--border);border-radius:14px;padding:28px;border-top:3px solid var(--alpha-c)">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
          <div style="width:10px;height:10px;border-radius:2px;background:var(--alpha-c);flex-shrink:0"></div>
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--alpha-c)">Genuine stock selection</div>
        </div>
        <div style="font-size:16px;font-weight:800;color:var(--text);margin-bottom:10px">This is the only part that's truly repeatable</div>
        <p style="font-size:13px;color:var(--muted);line-height:1.75">After removing market, style, and sector effects, what remains is pure stock-picking alpha — the manager's actual skill. <strong style="color:var(--text)">This is what AlphaLens measures.</strong> Managers who generate it consistently are far more likely to keep doing so.</p>
      </div>

    </div>

    <!-- Bottom callout -->
    <div style="margin-top:32px;background:var(--bg2);border:1px solid var(--border);border-radius:14px;padding:28px;display:grid;grid-template-columns:1fr auto;gap:32px;align-items:center">
      <div>
        <div style="font-size:16px;font-weight:800;color:var(--text);margin-bottom:8px">We flag when a manager's skill starts fading — before it shows up in returns</div>
        <p style="font-size:13px;color:var(--muted);line-height:1.75;margin:0">Most rankings update once a year. Ours update daily. When a fund's stock-selection behaviour drifts from the discipline that made it successful, our score reflects it immediately — giving you time to act before the broader market notices.</p>
      </div>
      <div style="text-align:center;flex-shrink:0">
        <div style="font-size:36px;font-weight:800;color:var(--accent)">Daily</div>
        <div style="font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-top:4px">Score updates</div>
      </div>
    </div>
  </div>
</section>

<!-- SECTION: PROOF IT WORKS -->
<section style="background:var(--bg2);border-top:1px solid var(--border);padding:72px 0">
  <div class="container">
    <div style="text-align:center;max-width:560px;margin:0 auto 48px">
      <div class="section-label">Does it work?</div>
      <h2 class="section-title">7 out of 10 funds we pick beat their benchmark.</h2>
      <p style="font-size:15px;color:var(--muted);line-height:1.8;margin-top:12px">
        We track how our top-ranked funds actually perform after we rank them.
        The results speak for themselves.
      </p>
    </div>

    <!-- Simple visual: 10 dots, 7 green 3 grey -->
    <div style="display:flex;justify-content:center;gap:12px;margin-bottom:40px;flex-wrap:wrap">
      <div style="width:56px;height:56px;border-radius:50%;background:rgba(5,150,105,0.12);border:2px solid var(--green);display:flex;align-items:center;justify-content:center;font-size:20px">✓</div>
      <div style="width:56px;height:56px;border-radius:50%;background:rgba(5,150,105,0.12);border:2px solid var(--green);display:flex;align-items:center;justify-content:center;font-size:20px">✓</div>
      <div style="width:56px;height:56px;border-radius:50%;background:rgba(5,150,105,0.12);border:2px solid var(--green);display:flex;align-items:center;justify-content:center;font-size:20px">✓</div>
      <div style="width:56px;height:56px;border-radius:50%;background:rgba(5,150,105,0.12);border:2px solid var(--green);display:flex;align-items:center;justify-content:center;font-size:20px">✓</div>
      <div style="width:56px;height:56px;border-radius:50%;background:rgba(5,150,105,0.12);border:2px solid var(--green);display:flex;align-items:center;justify-content:center;font-size:20px">✓</div>
      <div style="width:56px;height:56px;border-radius:50%;background:rgba(5,150,105,0.12);border:2px solid var(--green);display:flex;align-items:center;justify-content:center;font-size:20px">✓</div>
      <div style="width:56px;height:56px;border-radius:50%;background:rgba(5,150,105,0.12);border:2px solid var(--green);display:flex;align-items:center;justify-content:center;font-size:20px">✓</div>
      <div style="width:56px;height:56px;border-radius:50%;background:var(--bg3);border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:20px;color:var(--muted)">✗</div>
      <div style="width:56px;height:56px;border-radius:50%;background:var(--bg3);border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:20px;color:var(--muted)">✗</div>
      <div style="width:56px;height:56px;border-radius:50%;background:var(--bg3);border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:20px;color:var(--muted)">✗</div>
    </div>
    <div style="text-align:center;font-size:13px;color:var(--muted);margin-bottom:48px">
      Each circle = 1 fund picked by AlphaLens · <span style="color:var(--green);font-weight:600">✓ beat its benchmark</span> · <span style="color:var(--muted)">✗ did not</span>
      <br><span style="font-size:11px">Compared to just 27% of all active funds over 10 years (SPIVA India 2025)</span>
    </div>

    <!-- Two stat boxes -->
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;max-width:640px;margin:0 auto">
      <div style="background:rgba(5,150,105,0.06);border:1.5px solid rgba(5,150,105,0.25);border-radius:14px;padding:24px;text-align:center">
        <div style="font-size:48px;font-weight:800;color:var(--green);letter-spacing:-2px">69%</div>
        <div style="font-size:14px;color:var(--text);font-weight:600;margin-top:4px">of our picks beat<br>their benchmark</div>
      </div>
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:14px;padding:24px;text-align:center">
        <div style="font-size:48px;font-weight:800;color:var(--muted);letter-spacing:-2px">27%</div>
        <div style="font-size:14px;color:var(--muted);margin-top:4px">of active funds beat<br>their benchmark over 10 years</div>
        <div style="font-size:10px;color:var(--muted);margin-top:6px">Source: SPIVA India 2025</div>
      </div>
    </div>


    <!-- Proof section bottom -->
    <div style="max-width:480px;margin:32px auto 0;text-align:center">
      <p style="font-size:13px;color:var(--muted);line-height:1.7">
        Past performance is not indicative of future results.
        Rankings are based on historical data and updated daily.
      </p>
    </div>
  </div>
</section>


<!-- Compliance disclaimer -->
<div style="background:var(--bg3);border-top:1px solid var(--border);padding:16px 0">
  <div class="container">
    <p style="font-size:11px;color:var(--muted);line-height:1.7;margin:0">
      Mutual Fund investments are subject to market risks. Past performance is not indicative of future results.
      AlphaLens rankings are based on historical data and are for informational purposes only.
      Please read all scheme related documents carefully before investing.
      We are a registered Mutual Fund Distributor (ARN: <strong style="color:var(--text)">[YOUR ARN]</strong>).
      The information on this site does not constitute investment advice.
    </p>
  </div>
</div>

<!-- SECTION 4: RANKINGS -->
<section class="rankings-section" id="rankings">
  <div class="container-wide">
    <div class="section-label">Fund Rankings · Updated daily</div>
    <h2 class="section-title">Where does your fund stand?</h2>
    <p class="section-body" style="margin-bottom:20px">
      {stats['n_funds']} actively managed Indian equity mutual funds across 21 AMCs,
      ranked by AlphaLens Score — highest to lowest. Click any fund to see a full
      return breakdown and manager skill analysis. Data as of <strong>{stats['as_of']}</strong>.
    </p>
    <div style="display:inline-flex;align-items:center;gap:8px;background:rgba(59,110,232,0.07);
                border:1px solid rgba(59,110,232,0.2);border-radius:8px;
                padding:8px 14px;margin-bottom:28px;font-size:13px;color:var(--muted)">
      <span style="color:var(--accent);font-size:16px">ℹ</span>
      <span><strong style="color:var(--text)">Actively managed funds only.</strong>
      Index funds and ETFs are not included — they track an index and manager skill is not applicable.</span>
    </div>

    <div class="controls">
      <div class="ctrl-group">
        <label>Search fund</label>
        <input type="text" id="search" placeholder="e.g. HDFC Mid Cap" style="width:220px">
      </div>
      <div class="ctrl-group">
        <label>AMC</label>
        <select id="amc-filter"><option value="">All AMCs</option></select>
      </div>
      <div class="ctrl-group">
        <label>Category</label>
        <select id="cat-filter"><option value="">All Categories</option></select>
      </div>
      <div class="ctrl-group">
        <label>Min score</label>
        <select id="score-filter">
          <option value="0">All funds</option>
          <option value="70">70+ Strong evidence of skill</option>
          <option value="50">50+ Above average</option>
        </select>
      </div>
      <div class="result-count" id="count-label"></div>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th data-key="name"    onclick="sortBy('name')">Fund</th>
            <th data-key="score"   onclick="sortBy('score')"   title="Composite 0–100 score based on stock-pick alpha, beat rate, active return and cost">AlphaLens Score</th>
            <th data-key="aret"    onclick="sortBy('aret')"    title="Annualised net active return vs benchmark — how much more (or less) the fund returned above its index after fees">Vs Benchmark/yr</th>
            <th data-key="hrate"   onclick="sortBy('hrate')"   title="% of months the fund beat its benchmark — a higher number means the manager consistently outperforms, not just occasionally">Consistency</th>
            <th data-key="pickAnn" onclick="sortBy('pickAnn')" title="Annualised stock-selection alpha from Brinson attribution — excess return from individual stock choices, stripped of market and sector moves">Stock Pick/yr</th>
            <th data-key="ter"     onclick="sortBy('ter')"     title="Estimated annual expense ratio for the regular plan — based on AMFI-disclosed direct plan TER plus standard distributor trail. Actual TER may vary; verify with the fund house.">Est. Fee</th>
            <th data-key="ret"     onclick="sortBy('ret')"     title="Total annualised NAV return over the full period">Total Return/yr</th>
          </tr>
        </thead>
        <tbody id="lb-body"></tbody>
      </table>
    </div>
    <div id="pagination" style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end"></div>
  </div>
</section>

<!-- METHODOLOGY ACCORDION -->
<section class="method-section" id="methodology">
  <div class="container">
    <div class="section-label">For the curious</div>
    <h2 class="section-title" style="margin-bottom:32px">How does AlphaLens actually work?</h2>

    <div class="accordion-item">
      <div class="accordion-q" onclick="toggleAccordion(this)">
        What data does AlphaLens use?
        <span>+</span>
      </div>
      <div class="accordion-a">
        AlphaLens uses monthly NAV data for all funds from mfapi.in (a free, SEBI-registered
        data provider). Factor return data comes from the our proprietary risk models,
        which is estimated daily from NSE Nifty 500 stock prices and fundamental data.
        All analysis covers the top 10 AMCs by equity AUM across the last 3 years.
      </div>
    </div>

    <div class="accordion-item">
      <div class="accordion-q" onclick="toggleAccordion(this)">
        How does AlphaLens analyse fund returns?
        <span>+</span>
      </div>
      <div class="accordion-a">
        AlphaLens uses proprietary risk models built specifically for the Indian equity market. The models include
        one market factor, 12 style factors (size, value, momentum, quality, low-volatility,
        profitability, and others), and 26 industry factors covering all major NSE sectors.
        Each month, we estimate how much of each fund's return is explained by each factor
        group, and what's left over is the manager's genuine alpha contribution.
      </div>
    </div>

    <div class="accordion-item">
      <div class="accordion-q" onclick="toggleAccordion(this)">
        How is the fund attribution calculated?
        <span>+</span>
      </div>
      <div class="accordion-a">
        For each fund, we run a rolling 12-month ordinary least-squares (OLS) regression of
        the fund's monthly NAV returns against three grouped factor return indices:
        (1) the market factor, (2) the equal-weighted average of 12 style factors, and
        (3) the equal-weighted average of 26 industry factors. The regression coefficients
        (betas) tell us the fund's sensitivity to each group. Each month's return
        is then decomposed: Market contribution = β_market × market return that month;
        Style contribution = β_style × style index return; Industry = β_industry × industry index
        return. The residual is the fund's alpha for that month.
      </div>
    </div>

    <div class="accordion-item">
      <div class="accordion-q" onclick="toggleAccordion(this)">
        What is "Positioning Fit" and why does it matter?
        <span>+</span>
      </div>
      <div class="accordion-a">
        Attribution shows you <em>what happened</em> — where returns came from historically.
        But the betas (factor exposures) embedded in the portfolio today tell you
        <em>what risk is sitting there now</em>. A fund could have near-zero style
        contribution in recent months simply because the style factor returns were flat —
        but if the style beta is still high, that exposure will activate when factor
        conditions change. Positioning Fit scores whether a manager's current active
        exposures (style betas + sector betas) are proportionate to their demonstrated
        alpha-generating ability. A great stock picker with low current style bets
        scores highly. A weak stock picker with large style bets scores poorly.
      </div>
    </div>

    <div class="accordion-item">
      <div class="accordion-q" onclick="toggleAccordion(this)">
        What are the limitations of this analysis?
        <span>+</span>
      </div>
      <div class="accordion-a">
        A few important limitations to be aware of. First, we use NAV returns (not holdings)
        to estimate factor betas via regression — this is a signal-based approach, not a
        position-level decomposition. Second, 3 years of monthly data (36 data points) is a
        relatively short history for statistical reliability, particularly for funds with
        concentrated portfolios. Third, equal-weighted factor indices for style and industry
        groups mean individual factor tilts (e.g., specifically momentum or specifically banking)
        are averaged together. Fourth, past alpha is not a guarantee of future alpha.
        This analysis is intended to help evaluate managers, not to predict returns.
      </div>
    </div>

    <div class="accordion-item">
      <div class="accordion-q" onclick="toggleAccordion(this)">
        Is AlphaLens financial advice?
        <span>+</span>
      </div>
      <div class="accordion-a">
        No. AlphaLens is an analytical research tool. Nothing on this website constitutes
        investment advice, a recommendation to buy or sell any fund, or a solicitation of
        any kind. Always consult a SEBI-registered investment advisor before making investment
        decisions. Past performance and analytical scores are not guarantees of future results.
      </div>
    </div>
  </div>
</section>

<footer>
  <strong>AlphaLens</strong> · Indian Mutual Fund Analytics · Powered by proprietary risk models<br>
  Data: mfapi.in · NSE · AMFI · For research purposes only · Not investment advice<br>
  Data as of {stats['as_of']}
</footer>

<!-- FUND DETAIL DRAWER -->
<div class="drawer-overlay" id="drawer-overlay" onclick="closeDrawer()"></div>
<div class="drawer" id="drawer">
  <button class="drawer-close" onclick="closeDrawer()">×</button>
  <div id="drawer-content"></div>
</div>

<script>
const SCORES = {scores_json};
const ATTR   = {attr_json};
const STATS  = {stats_json};

// =========================================================================
// Leaderboard
// =========================================================================
let sortKey = "score", sortDir = -1, currentPage = 1;
const PAGE_SIZE = 20;
let filtered = [...SCORES];
let activeChart = null, activeChart2 = null;

// Fund name cleaner — strips "Direct Plan", "Growth Option" suffixes and fixes casing
const ACRONYMS = new Set(["SBI","HDFC","ICICI","ABSL","DSP","UTI","ELSS","HSBC","BNP",
  "NRI","AMC","PPFAS","ITI","JM","LIC","IDFC","BOI","PGIM","IDBI","IIFL","TATA"]);
function cleanFundName(raw) {{
  let n = raw
    .replace(/\s*[-–]\s*Direct Plan\s*[-–]?\s*(Growth Option|Growth|IDCW|Dividend)?\s*$/i, '')
    .replace(/\s*[-–]\s*Direct\s*[-–]?\s*(Growth Option|Growth|IDCW|Dividend)?\s*$/i, '')
    .replace(/\s*[-–]?\s*(Growth Option|Growth Plan|Growth)\s*$/i, '')
    .replace(/\s*[-–]\s*Direct\s*$/i, '')
    .replace(/\s+/g, ' ').trim();
  // Smart title-case: preserve known acronyms, lowercase the rest
  return n.split(' ').map(w => {{
    const u = w.replace(/[^A-Za-z]/g,'').toUpperCase();
    if (ACRONYMS.has(u)) return u + w.slice(u.length); // preserve trailing chars like &
    if (w.length <= 2) return w.toUpperCase();
    // If word is ALL CAPS and longer, convert to title case
    if (w === w.toUpperCase() && /[A-Z]{{2,}}/.test(w)) return w[0] + w.slice(1).toLowerCase();
    return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
  }}).join(' ');
}}

function scoreClass(s) {{
  if (s == null) return "";
  return s >= 65 ? "sh" : s >= 40 ? "sm" : "sl";
}}
function clr(v) {{ return (v == null || v >= 0) ? "positive" : "negative"; }}
function fmt(v, d=1) {{ return v == null ? "—" : v.toFixed(d); }}
function fmtPct(v) {{
  if (v == null) return "—";
  return (v >= 0 ? "+" : "") + v.toFixed(1) + "%";
}}
function fmtB(v) {{
  if (v == null) return "—";
  return (v >= 0 ? "+" : "") + v.toFixed(2);
}}

function buildAmcDropdown() {{
  const amcs = [...new Set(SCORES.map(s => s.amc))].filter(Boolean).sort();
  const sel  = document.getElementById("amc-filter");
  amcs.forEach(a => {{
    const o = document.createElement("option");
    o.value = a; o.textContent = a;
    sel.appendChild(o);
  }});
}}

function buildCatDropdown() {{
  // Show equity categories in a logical order, then anything else alphabetically
  const ORDER = [
    "Large Cap","Mid Cap","Small Cap","Large & Mid Cap",
    "Flexi Cap","Multi Cap","Focused","Value / Contrarian",
    "Tax Saver (ELSS)","Thematic / Sectoral","Balanced Advantage",
    "Hybrid","Retirement","Other"
  ];
  const allCats = [...new Set(SCORES.map(s => s.cat))].filter(Boolean);
  const ordered = ORDER.filter(c => allCats.includes(c));
  // Append any categories not in the preset order
  allCats.filter(c => !ORDER.includes(c)).sort().forEach(c => ordered.push(c));
  const sel = document.getElementById("cat-filter");
  ordered.forEach(c => {{
    const o = document.createElement("option");
    o.value = c; o.textContent = c;
    sel.appendChild(o);
  }});
}}

function applyFilters() {{
  const q    = document.getElementById("search").value.toLowerCase();
  const amc  = document.getElementById("amc-filter").value;
  const cat  = document.getElementById("cat-filter").value;
  const minS = +document.getElementById("score-filter").value;
  filtered = SCORES.filter(f => {{
    if (q   && !f.name.toLowerCase().includes(q) && !f.amc.toLowerCase().includes(q)) return false;
    if (amc && f.amc !== amc) return false;
    if (cat && f.cat !== cat) return false;
    if (minS && (f.score == null || f.score < minS)) return false;
    return true;
  }});
  filtered.sort((a, b) => {{
    const va = a[sortKey] ?? (sortDir < 0 ? -Infinity : Infinity);
    const vb = b[sortKey] ?? (sortDir < 0 ? -Infinity : Infinity);
    if (typeof va === "string") return sortDir * va.localeCompare(vb);
    return sortDir * (va - vb);
  }});
  currentPage = 1;
  renderTable();
}}

function sortBy(key) {{
  if (sortKey === key) sortDir *= -1;
  else {{ sortKey = key; sortDir = -1; }}
  document.querySelectorAll("thead th").forEach(th => {{
    th.classList.remove("sorted-asc","sorted-desc");
    if (th.dataset.key === key) th.classList.add(sortDir < 0 ? "sorted-desc" : "sorted-asc");
  }});
  applyFilters();
}}

function renderTable() {{
  const start = (currentPage - 1) * PAGE_SIZE;
  const slice = filtered.slice(start, start + PAGE_SIZE);
  const globalRank = {{}};
  SCORES.forEach((f,i) => {{ globalRank[f.code] = i + 1; }});

  const tbody = document.getElementById("lb-body");
  tbody.innerHTML = "";
  slice.forEach(f => {{
    const tr = document.createElement("tr");
    tr.onclick = () => openDrawer(f.code);
    // Beat rate: convert 0–1 fraction to % string
    const beatStr  = f.hrate != null ? (f.hrate * 100).toFixed(0) + "%" : "—";
    const beatColor = f.hrate == null ? "" : f.hrate >= 0.55 ? "positive" : f.hrate >= 0.45 ? "" : "negative";
    // Expense ratio: show as % with 2 dp
    const terStr   = f.ter != null ? (f.ter * 100).toFixed(2) + "%" : "—";
    // Stock pick: show as pp/yr with sign
    const pickStr  = f.pickAnn != null ? (f.pickAnn > 0 ? "+" : "") + f.pickAnn.toFixed(1) + "pp" : "—";
    const pickColor = f.pickAnn == null ? "" : f.pickAnn > 0.5 ? "positive" : f.pickAnn < -0.5 ? "negative" : "";
    tr.innerHTML = `
      <td><div class="td-name" title="${{f.name}}">${{cleanFundName(f.name)}}</div><div class="td-amc">${{f.amc.replace(/_/g,' ')}}</div></td>
      <td><span class="score-badge ${{scoreClass(f.score)}}">${{f.score != null ? f.score.toFixed(1) : "—"}}</span></td>
      <td class="td-r ${{clr(f.aret)}}">${{fmtPct(f.aret)}}</td>
      <td class="td-r ${{beatColor}}">${{beatStr}}</td>
      <td class="td-r ${{pickColor}}">${{pickStr}}</td>
      <td class="td-r" style="color:var(--muted)">${{terStr}}</td>
      <td class="td-r ${{clr(f.ret)}}">${{fmtPct(f.ret)}}</td>
    `;
    tbody.appendChild(tr);
  }});
  document.getElementById("count-label").textContent =
    `${{filtered.length}} fund${{filtered.length !== 1 ? "s" : ""}}`;
  renderPagination();
}}

function renderPagination() {{
  const total = Math.ceil(filtered.length / PAGE_SIZE);
  const div   = document.getElementById("pagination");
  div.innerHTML = "";
  if (total <= 1) return;
  for (let i = 1; i <= Math.min(total, 8); i++) {{
    const btn = document.createElement("button");
    btn.style.cssText = "background:var(--bg2);border:1px solid var(--border);color:var(--text);padding:5px 12px;border-radius:5px;cursor:pointer;font-size:12px";
    if (i === currentPage) btn.style.background = "var(--accent)";
    btn.textContent = i;
    btn.onclick = () => {{ currentPage = i; renderTable(); }};
    div.appendChild(btn);
  }}
}}

// =========================================================================
// Drawer / Fund Detail
// =========================================================================
function openDrawer(code) {{
  const f    = SCORES.find(s => s.code === code);
  if (!f) return;
  const attr = (ATTR[String(code)] || []).sort((a,b) => a.date.localeCompare(b.date));
  const scr  = f.score || 0;
  const sc   = scoreClass(scr);
  const scoreColor = sc === "sh" ? "var(--green)" : sc === "sm" ? "var(--yellow)" : "var(--red)";

  // Compute average attribution from monthly history
  const avgMkt  = attr.length ? attr.reduce((s,d)=>s+(d.mkt||0),0)/attr.length : 0;
  const avgSty  = attr.length ? attr.reduce((s,d)=>s+(d.sty||0),0)/attr.length : 0;
  const avgAlpha= attr.length ? attr.reduce((s,d)=>s+(d.alpha||0),0)/attr.length : 0;
  const avgTotal= attr.length ? attr.reduce((s,d)=>s+(d.ret||0),0)/attr.length : 0;
  const mktShare= avgTotal ? Math.round(avgMkt/avgTotal*100) : 0;

  // Beat rate and active return formatting
  const beatStr  = f.hrate != null ? (f.hrate*100).toFixed(0)+"%" : "—";
  const beatColor = f.hrate == null ? "var(--muted)" : f.hrate >= 0.55 ? "var(--green)" : f.hrate >= 0.45 ? "var(--yellow)" : "var(--red)";
  const aretColor = f.aret == null ? "var(--muted)" : f.aret > 0 ? "var(--green)" : "var(--red)";

  // Return decomposition (annualised pp from Brinson)
  const hasDecomp = f.decomp === true;
  const bdr = f.bdr || 0;
  const driftHigh = bdr > 0.8;
  const driftDesc = driftHigh
    ? "<strong>Recent style drift detected.</strong> The fund's exposures have shifted in recent months."
    : "Style exposures have been relatively stable — consistent with the fund's longer-term character.";

  document.getElementById("drawer-content").innerHTML = `
    <div class="drawer-fund-name">${{cleanFundName(f.name)}}</div>
    <span class="drawer-amc">${{f.amc}}</span>

    <div class="drawer-score-row">
      <div class="drawer-score-box" style="border-top:3px solid ${{scoreColor}}">
        <span class="dsb-val" style="color:${{scoreColor}}">${{scr != null ? scr.toFixed(1) : "—"}}</span>
        <span class="dsb-label">AlphaLens Score</span>
      </div>
      <div class="drawer-score-box" style="border-top:3px solid ${{aretColor}}">
        <span class="dsb-val" style="color:${{aretColor}}">${{fmtPct(f.aret)}}</span>
        <span class="dsb-label">Vs Benchmark/yr</span>
      </div>
      <div class="drawer-score-box" style="border-top:3px solid ${{beatColor}}">
        <span class="dsb-val" style="color:${{beatColor}}">${{beatStr}}</span>
        <span class="dsb-label">Beat Rate</span>
      </div>
      <div class="drawer-score-box" style="border-top:3px solid var(--alpha-c)">
        <span class="dsb-val" style="color:var(--alpha-c)">${{f.pickAnn != null ? (f.pickAnn>0?"+":"")+f.pickAnn.toFixed(1)+"pp" : "—"}}</span>
        <span class="dsb-label">Stock Pick/yr</span>
      </div>
    </div>

    <!-- Active decisions breakdown -->
    <div class="drawer-section-title">What's driving returns? (Beyond the market baseline)</div>
    <div style="background:var(--bg3);border-radius:8px;padding:14px;font-size:13px;color:var(--muted);line-height:1.6;margin-bottom:12px">
      <strong style="color:var(--market-c)">Market baseline: ~${{mktShare}}% of total return</strong> —
      this is what any index fund would have captured. The active decisions below are what you're paying the manager for.
    </div>
    <div class="explain-row">
      <div class="explain-dot" style="background:var(--style-c)"></div>
      <div class="explain-text">
        <strong>Style tilts:</strong> Average contribution
        <span class="${{avgSty >= 0 ? 'positive' : 'negative'}}">${{fmtPct(avgSty*12)}}/yr</span>.
        ${{avgSty < -0.001 ? "Style bets have been a headwind — the fund's factor tilts cost returns." : avgSty > 0.001 ? "Style tilts have added value." : "Style tilts have been roughly neutral."}}
      </div>
    </div>
    <div class="explain-row">
      <div class="explain-dot" style="background:var(--alpha-c)"></div>
      <div class="explain-text">
        <strong>Genuine skill (alpha):</strong>
        <span class="${{avgAlpha >= 0 ? 'positive' : 'negative'}}">${{fmtPct(avgAlpha*12)}}/yr</span>
        after removing all market, style, and sector effects.
        ${{avgAlpha > 0.002 ? "Positive evidence of stock-selection skill." : avgAlpha < -0.002 ? "Negative alpha — market and style exposure is doing the work." : "Roughly neutral alpha — returns are driven by exposures, not selection."}}
      </div>
    </div>

    <div class="drawer-section-title">Monthly attribution history</div>
    <div class="drawer-chart-wrap"><canvas id="drawer-chart"></canvas></div>
    <p style="font-size:11px;color:var(--muted);margin-top:-16px;margin-bottom:16px">
      Green = genuine alpha | Blue = market | Purple = style | Yellow = sector
    </p>

    <!-- RETURN DECOMPOSITION SUMMARY -->
    <div class="drawer-section-title">Where do returns come from? (Annualised)</div>
    ${{hasDecomp ? `
    <div class="risk-grid">
      <div class="risk-cell">
        <div class="risk-label">Style Tilts</div>
        <div class="risk-val" style="color:var(--style-c)">${{f.dStyle != null ? (f.dStyle>0?"+":"")+f.dStyle.toFixed(1)+"pp" : "—"}}</div>
        <div class="risk-note">Return from factor tilts (size, value, momentum, etc.) vs benchmark</div>
      </div>
      <div class="risk-cell">
        <div class="risk-label">Sector Bets</div>
        <div class="risk-val" style="color:var(--ind-c)">${{f.dSector != null ? (f.dSector>0?"+":"")+f.dSector.toFixed(1)+"pp" : "—"}}</div>
        <div class="risk-note">Return from industry overweights/underweights vs benchmark</div>
      </div>
      <div class="risk-cell">
        <div class="risk-label">Stock Pick</div>
        <div class="risk-val" style="color:var(--alpha-c)">${{f.dPick != null ? (f.dPick>0?"+":"")+f.dPick.toFixed(1)+"pp" : "—"}}</div>
        <div class="risk-note">Return from individual stock selection — the purest measure of manager skill</div>
      </div>
      <div class="risk-cell">
        <div class="risk-label">Timing / Other</div>
        <div class="risk-val" style="color:var(--muted)">${{f.dTiming != null ? (f.dTiming>0?"+":"")+f.dTiming.toFixed(1)+"pp" : "—"}}</div>
        <div class="risk-note">Residual: cash calls, timing decisions, fees, and other effects</div>
      </div>
    </div>
    ${{driftHigh ? `<div class="risk-alert"><strong>⚠ Style drift detected.</strong> ${{driftDesc}}</div>` : ""}}
    ` : `<p style="font-size:13px;color:var(--muted);line-height:1.6">
      Full decomposition not available for this fund — requires 12+ months of portfolio holdings data.
    </p>`}}

    <div class="drawer-section-title">Fund details</div>
    <div class="risk-grid">
      <div class="risk-cell">
        <div class="risk-label">Total Return/yr</div>
        <div class="risk-val" style="color:var(--text)">${{fmtPct(f.ret)}}</div>
        <div class="risk-note">Annualised NAV return over the full period</div>
      </div>
      <div class="risk-cell">
        <div class="risk-label">Est. Annual Fee (Regular Plan)</div>
        <div class="risk-val" style="color:var(--muted)">${{f.ter != null ? (f.ter*100).toFixed(2)+"%" : "—"}}</div>
        <div class="risk-note">Annual cost deducted from NAV — directly reduces your return</div>
      </div>
      <div class="risk-cell">
        <div class="risk-label">Peer Rank</div>
        <div class="risk-val" style="color:var(--text)">${{f.catRank != null ? f.catRank+" / "+f.catSize : "—"}}</div>
        <div class="risk-note">Rank within the ${{f.cat}} category by AlphaLens score</div>
      </div>
      <div class="risk-cell">
        <div class="risk-label">Skill Label</div>
        <div class="risk-val" style="font-size:11px;color:${{f.skill && f.skill.startsWith("Strong") ? "var(--green)" : f.skill && f.skill.startsWith("Some") ? "var(--yellow)" : "var(--muted)"}}">${{f.skill ? f.skill.split("—")[0].trim() : "—"}}</div>
        <div class="risk-note">Based on beat rate and active return over 3 years</div>
      </div>
    </div>

    <div class="drawer-section-title">Cumulative active return vs benchmark</div>
    <div class="drawer-chart-wrap2"><canvas id="drawer-chart2"></canvas></div>
    <p style="font-size:11px;color:var(--muted);margin-top:-16px;margin-bottom:8px">
      Running cumulative sum of monthly active return — above zero means the fund is ahead of its benchmark.
    </p>
  `;

  // Chart 1: Monthly attribution
  if (activeChart) {{ activeChart.destroy(); activeChart = null; }}
  const ctx = document.getElementById("drawer-chart");
  if (ctx && attr.length) {{
    activeChart = new Chart(ctx.getContext("2d"), {{
      type: "bar",
      data: {{
        labels: attr.map(d => d.date),
        datasets: [
          {{ label:"Market",  data:attr.map(d=>d.mkt),   backgroundColor:"rgba(93,142,240,0.65)", stack:"s" }},
          {{ label:"Style",   data:attr.map(d=>d.sty),   backgroundColor:"rgba(167,139,250,0.75)", stack:"s" }},
          {{ label:"Sector",  data:attr.map(d=>d.ind),   backgroundColor:"rgba(251,191,36,0.75)", stack:"s" }},
          {{ label:"Skill",   data:attr.map(d=>d.alpha), backgroundColor:"rgba(52,211,153,0.9)", stack:"s" }},
        ]
      }},
      options: {{
        responsive:true, maintainAspectRatio:false,
        plugins: {{
          legend: {{ labels:{{ color:"#8892B0", font:{{ size:10 }} }} }},
          tooltip:{{ backgroundColor:"#0F1525", borderColor:"#252D45", borderWidth:1,
                     callbacks:{{ label: ctx => ` ${{ctx.dataset.label}}: ${{ctx.parsed.y != null ? ctx.parsed.y.toFixed(2)+"%" : "—"}}` }} }}
        }},
        scales:{{
          x:{{ grid:{{ color:"#252D45" }}, ticks:{{ color:"#7A86A8", font:{{ size:9 }} }} }},
          y:{{ grid:{{ color:"#252D45" }}, ticks:{{ color:"#7A86A8", font:{{ size:9 }},
               callback: v => v.toFixed(1)+"%" }} }}
        }}
      }}
    }});
  }}

  // Chart 2: Cumulative active return vs benchmark
  if (activeChart2) {{ activeChart2.destroy(); activeChart2 = null; }}
  const ctx2 = document.getElementById("drawer-chart2");
  if (ctx2 && attr.length) {{
    // Cumulative sum of (fund return - market attribution) ≈ active return
    let cumActive = 0, cumAlpha = 0;
    const cumActiveData = attr.map(d => {{ cumActive += (d.ret||0) - (d.mkt||0); return +cumActive.toFixed(3); }});
    const cumAlphaData  = attr.map(d => {{ cumAlpha  += (d.alpha||0); return +cumAlpha.toFixed(3); }});
    activeChart2 = new Chart(ctx2.getContext("2d"), {{
      type: "line",
      data: {{
        labels: attr.map(d => d.date),
        datasets: [
          {{ label:"Cumulative active return",
             data: cumActiveData,
             borderColor:"rgba(93,142,240,0.9)", backgroundColor:"rgba(93,142,240,0.1)",
             tension:0.3, fill:true, pointRadius:2 }},
          {{ label:"Cumulative stock-pick alpha",
             data: cumAlphaData,
             borderColor:"rgba(52,211,153,0.9)", backgroundColor:"rgba(52,211,153,0.05)",
             tension:0.3, fill:false, pointRadius:2, borderDash:[4,3] }},
        ]
      }},
      options: {{
        responsive:true, maintainAspectRatio:false,
        plugins: {{
          legend: {{ labels:{{ color:"#8892B0", font:{{ size:10 }} }} }},
          tooltip:{{ backgroundColor:"#0F1525", borderColor:"#252D45", borderWidth:1,
                     callbacks:{{ label: ctx => ` ${{ctx.dataset.label}}: ${{ctx.parsed.y != null ? ctx.parsed.y.toFixed(2)+"%" : "—"}}` }} }}
        }},
        scales:{{
          x:{{ grid:{{ color:"#252D45" }}, ticks:{{ color:"#7A86A8", font:{{ size:9 }} }} }},
          y:{{ grid:{{ color:"#252D45" }}, ticks:{{ color:"#7A86A8", font:{{ size:9 }},
               callback: v => v.toFixed(1)+"%" }},
               title:{{ display:true, text:"Cumulative %", color:"#7A86A8", font:{{size:9}} }} }}
        }}
      }}
    }});
  }}

  document.getElementById("drawer-overlay").classList.add("open");
  document.getElementById("drawer").classList.add("open");
}}

function closeDrawer() {{
  document.getElementById("drawer-overlay").classList.remove("open");
  document.getElementById("drawer").classList.remove("open");
}}

// =========================================================================
// =========================================================================
// Growth Chart — ₹1L compounded over 15 years
// =========================================================================
(function buildGrowthChart() {{
  const canvas = document.getElementById("growthChart");
  if (!canvas) return;
  const years      = [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15];
  const avgRate    = 0.113;   // avg regular plan fund: market 12% − 0.7pp underperformance
  const alphaRate  = 0.163;   // AlphaLens picks (regular plan): market 12% + 4.3pp alpha
  const initial    = 100000;
  const avgVals    = years.map(y => Math.round(initial * Math.pow(1+avgRate,  y)));
  const alphaVals  = years.map(y => Math.round(initial * Math.pow(1+alphaRate,y)));
  const fmtL = v => "₹" + (v/100000).toFixed(2) + "L";

  new Chart(canvas.getContext("2d"), {{
    type: "line",
    data: {{
      labels: years.map(y => y === 0 ? "Today" : "Yr " + y),
      datasets: [
        {{
          label: "AlphaLens picks",
          data: alphaVals,
          borderColor: "#059669",
          backgroundColor: "rgba(5,150,105,0.06)",
          borderWidth: 2.5,
          pointRadius: years.map(y => y === 15 ? 7 : 0),
          pointBackgroundColor: "#059669",
          fill: false,
          tension: 0.35,
        }},
        {{
          label: "Average active fund",
          data: avgVals,
          borderColor: "#9CA3AF",
          backgroundColor: "rgba(156,163,175,0.06)",
          borderWidth: 2,
          borderDash: [6,4],
          pointRadius: years.map(y => y === 15 ? 7 : 0),
          pointBackgroundColor: "#9CA3AF",
          fill: false,
          tension: 0.35,
        }},
      ],
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{ mode: "index", intersect: false }},
      plugins: {{
        legend: {{
          position: "top",
          labels: {{ boxWidth: 14, font: {{ size: 13 }}, color: "#1A1F36" }},
        }},
        tooltip: {{
          callbacks: {{
            label: ctx => " " + ctx.dataset.label + ": " + fmtL(ctx.parsed.y),
            afterBody: items => {{
              if (items.length < 2) return [];
              const diff = items[0].parsed.y - items[1].parsed.y;
              return ["", "  Extra from AlphaLens: +" + fmtL(diff)];
            }},
          }},
          backgroundColor: "#fff",
          borderColor: "#D4D9E8",
          borderWidth: 1,
          titleColor: "#1A1F36",
          bodyColor: "#6B7490",
          padding: 14,
        }},
        annotation: {{}}
      }},
      scales: {{
        x: {{
          grid: {{ color: "rgba(0,0,0,0.04)" }},
          ticks: {{ color: "#6B7490", font: {{ size: 12 }} }},
        }},
        y: {{
          grid: {{ color: "rgba(0,0,0,0.04)" }},
          ticks: {{
            color: "#6B7490",
            font: {{ size: 12 }},
            callback: v => "₹" + (v/100000).toFixed(1) + "L",
          }},
        }},
      }},
    }},
  }});
}})();

// Average bar visual
// =========================================================================
function buildAvgBar() {{
  const bar = document.getElementById("avg-bar");
  if (!bar) return;
  const mktPct   = 75; // approximate market contribution
  const alphaPct = Math.max(0, Math.round(STATS.avg_alpha_ann / STATS.avg_total_ann * 100));
  const rest     = Math.max(0, 100 - mktPct - alphaPct);
  bar.innerHTML = `
    <div class="avg-bar-seg" style="width:${{mktPct}}%;background:var(--market-c)">Market</div>
    <div class="avg-bar-seg" style="width:${{rest}}%;background:var(--style-c);color:rgba(255,255,255,0.5)">Style</div>
    <div class="avg-bar-seg" style="width:${{alphaPct}}%;background:var(--alpha-c)">Skill</div>
  `;
}}

// =========================================================================
// Tale bars
// =========================================================================
function buildTaleBars(containerId, data, maxAbs) {{
  const c = document.getElementById(containerId);
  if (!c) return;
  const rows = [
    {{ label:"Market",  val:data.mkt,   col:"var(--market-c)" }},
    {{ label:"Style",   val:data.sty,   col:"var(--style-c)"  }},
    {{ label:"Sector",  val:data.ind,   col:"var(--ind-c)"    }},
    {{ label:"Skill",   val:data.alpha, col:"var(--alpha-c)"  }},
  ];
  c.innerHTML = rows.map(r => {{
    const pct = Math.abs(r.val || 0) / maxAbs * 100;
    const valStr = (r.val != null ? ((r.val >= 0 ? "+" : "") + r.val.toFixed(1) + "%") : "—");
    const col = (r.val || 0) < 0 ? "var(--red)" : r.col;
    return `<div class="tale-bar-row">
      <div class="tale-bar-label">${{r.label}}</div>
      <div class="tale-bar-wrap">
        <div class="tale-bar-fill" style="width:${{pct}}%;background:${{col}}"></div>
      </div>
      <div class="tale-bar-val" style="color:${{col}}">${{valStr}}</div>
    </div>`;
  }}).join("");
}}

// =========================================================================
// Compounding Calculator
// =========================================================================
let calcMode = 'lumpsum';
const ALPHA_RATE = 0.163;  // 16.3%/yr — AlphaLens picks on regular plan basis (12% market + 4.3pp alpha)
const AVG_RATE   = 0.113;  // 11.3%/yr — avg regular plan fund (12% market − 0.7pp underperformance)

function calcSetMode(mode) {{
  calcMode = mode;
  document.getElementById('modeLS').style.background  = mode === 'lumpsum' ? 'var(--accent)' : 'transparent';
  document.getElementById('modeLS').style.color       = mode === 'lumpsum' ? '#fff' : 'var(--muted)';
  document.getElementById('modeSIP').style.background = mode === 'sip' ? 'var(--accent)' : 'transparent';
  document.getElementById('modeSIP').style.color      = mode === 'sip' ? '#fff' : 'var(--muted)';
  document.getElementById('amtLabel').textContent     = mode === 'sip' ? 'Monthly SIP amount (₹)' : 'Investment amount (₹)';
  document.getElementById('calcAmount').value         = mode === 'sip' ? 5000 : 100000;
  calcUpdate();
}}

function calcUpdate() {{
  const amt   = parseFloat(document.getElementById('calcAmount').value) || 0;
  const years = parseInt(document.getElementById('calcYears').value) || 15;
  document.getElementById('yearsLabel').textContent = years + ' year' + (years !== 1 ? 's' : '');

  let alphaVal, avgVal;
  if (calcMode === 'lumpsum') {{
    alphaVal = amt * Math.pow(1 + ALPHA_RATE, years);
    avgVal   = amt * Math.pow(1 + AVG_RATE,   years);
  }} else {{
    // SIP future value: P × [((1+r)^n - 1) / r] × (1+r)
    const rA = ALPHA_RATE / 12, rV = AVG_RATE / 12, n = years * 12;
    alphaVal = amt * ((Math.pow(1+rA, n) - 1) / rA) * (1+rA);
    avgVal   = amt * ((Math.pow(1+rV, n) - 1) / rV) * (1+rV);
  }}

  const fmtL = v => {{
    if (v >= 10000000) return '₹' + (v/10000000).toFixed(2) + ' Cr';
    return '₹' + (v/100000).toFixed(2) + ' L';
  }};
  const diff = alphaVal - avgVal;

  document.getElementById('calcAlpha').textContent = fmtL(alphaVal);
  document.getElementById('calcAvg').textContent   = fmtL(avgVal);
  document.getElementById('calcDiff').innerHTML    =
    `AlphaLens picks could give you <strong style="color:var(--green)">${{fmtL(diff)}} more</strong> over ${{years}} years`;
}}

// =========================================================================
// Accordion
// =========================================================================
function toggleAccordion(el) {{
  const a = el.nextElementSibling;
  const isOpen = a.classList.contains("open");
  document.querySelectorAll(".accordion-a").forEach(x => x.classList.remove("open"));
  document.querySelectorAll(".accordion-q span").forEach(x => x.textContent = "+");
  if (!isOpen) {{
    a.classList.add("open");
    el.querySelector("span").textContent = "−";
  }}
}}

// =========================================================================
// Init
// =========================================================================
(function init() {{
  buildAmcDropdown();
  applyFilters();
  buildAvgBar();

  const tale = STATS.tale;
  if (tale) {{
    const allVals = [tale.p1_total,tale.p1_market,tale.p1_style,tale.p1_industry,tale.p1_alpha,
                     tale.p2_total,tale.p2_market,tale.p2_style,tale.p2_industry,tale.p2_alpha];
    const maxAbs = Math.max(...allVals.map(v => Math.abs(v||0)), 1);
    if (document.getElementById("tale-bars-p1")) {{
      buildTaleBars("tale-bars-p1",
        {{mkt:tale.p1_market,sty:tale.p1_style,ind:tale.p1_industry,alpha:tale.p1_alpha}}, maxAbs);
      buildTaleBars("tale-bars-p2",
        {{mkt:tale.p2_market,sty:tale.p2_style,ind:tale.p2_industry,alpha:tale.p2_alpha}}, maxAbs);
    }}
  }}

  // Category beat-rate strip in hero
  const catBeat = STATS.cat_beat || {{}};
  const strip = document.getElementById("cat-beat-strip");
  if (strip) {{
    // Priority order — most interesting to users
    const order = ["Large Cap","Mid Cap","Small Cap","Flexi Cap","Focused","Large & Mid Cap",
                   "Multi Cap","Value / Contrarian","Tax Saver (ELSS)","Thematic / Sectoral"];
    order.forEach(cat => {{
      if (!catBeat[cat]) return;
      const pct = catBeat[cat].pct;
      const color = pct >= 40 ? "var(--green)" : pct >= 20 ? "var(--yellow)" : "var(--red)";
      const pill = document.createElement("div");
      pill.style.cssText = `background:#fff;border:1px solid var(--border);border-radius:20px;
        padding:5px 14px;display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer`;
      pill.innerHTML = `<span style="font-weight:700;color:${{color}}">${{pct}}%</span>
        <span style="color:var(--muted)">${{cat}}</span>`;
      pill.title = `${{catBeat[cat].beat}} of ${{catBeat[cat].total}} ${{cat}} funds beat their benchmark`;
      pill.onclick = () => {{
        document.getElementById("cat-filter").value = cat;
        applyFilters();
        document.getElementById("rankings").scrollIntoView({{behavior:"smooth"}});
      }};
      strip.appendChild(pill);
    }});
  }}

  buildCatDropdown();
  document.getElementById("search").addEventListener("input", applyFilters);
  document.getElementById("amc-filter").addEventListener("change", applyFilters);
  document.getElementById("cat-filter").addEventListener("change", applyFilters);
  document.getElementById("score-filter").addEventListener("change", applyFilters);

  document.querySelector('th[data-key="score"]').classList.add("sorted-desc");

  // Init calculator
  calcUpdate();
}})();
</script>
</body>
</html>"""


def main() -> None:
    args    = _parse_args()
    mf_data = Path(args.mf_data)
    out     = Path(args.out)

    # Prefer new pipeline files; fall back to legacy files if absent
    scores_path = (mf_data / "scored_funds.parquet"
                   if (mf_data / "scored_funds.parquet").exists()
                   else mf_data / "scores.parquet")
    attr_path   = (mf_data / "holdings_attribution.parquet"
                   if (mf_data / "holdings_attribution.parquet").exists()
                   else mf_data / "attribution.parquet")

    for p in [attr_path, scores_path]:
        if not p.exists():
            log.error(f"Missing: {p}\nRun the analytics pipeline first.")
            sys.exit(1)

    log.info("Loading data …")
    scores_raw = pd.read_parquet(scores_path)
    attr_raw   = pd.read_parquet(attr_path)

    # ── Normalise scored_funds.parquet → legacy column names ──────────────────
    if scores_path.name == "scored_funds.parquet":
        scores = scores_raw.rename(columns={
            "total_score":       "score",
            "active_ir":         "info_ratio",
            "ann_ret":           "total_return_ann",
            "benchmark_ann_ret": "market_ret_ann",
            "active_ann":        "active_ret_ann",
            "n_months_y":        "n_months",
        })
        for col in ["tracking_error_ann", "beta_M", "beta_S", "beta_I",
                    "recent_beta_M", "recent_beta_S", "recent_beta_I",
                    "current_active_exposure", "alpha_quality",
                    "alpha_proportion", "style_stability", "positioning",
                    "holdings_active_exposure", "holdings_style_active",
                    "holdings_coverage_pct", "holdings_month"]:
            if col not in scores.columns:
                scores[col] = np.nan
    else:
        scores = scores_raw

    # ── Normalise holdings_attribution.parquet → legacy column names ──────────
    if attr_path.name == "holdings_attribution.parquet":
        attr = attr_raw.rename(columns={
            "month":          "date",
            "fund_return":    "actual_ret",
            "market_attr":    "market_ret",
            "style_attr":     "style_ret",
            "industry_attr":  "industry_ret",
            "stock_selection": "alpha",
        })
        # Add scheme_name from scores
        name_map = scores.set_index("scheme_code")["scheme_name"].to_dict()
        attr["scheme_name"] = attr["scheme_code"].map(name_map).fillna("Unknown Fund")
        attr["amc"]    = attr["scheme_code"].map(scores.set_index("scheme_code")["amc"].to_dict())
        attr["beta_S"] = np.nan
        attr["beta_I"] = np.nan
        # Drop rows where actual_ret is NaN (months with no NAV data)
        attr = attr.dropna(subset=["actual_ret"])
    else:
        attr = attr_raw

    # ── Filter out non-equity commodity ETFs (gold, silver) ──────────────────
    _excl = scores["scheme_name"].str.lower().str.contains(r"gold|silver", na=False)
    if _excl.any():
        log.info(f"Excluding {_excl.sum()} gold/silver ETF(s)")
        scores = scores[~_excl].reset_index(drop=True)

    # ── Filter out international/offshore funds ───────────────────────────
    _intl_pat = (r"taiwan|japan|nasdaq|s&p 500|dow jones|asean|europe|china|"
                 r"korea|global brand|hang seng|brazil|vietnam|us bluechip|"
                 r"us equity|emerging market|offshore|world health|msci")
    _intl = scores["scheme_name"].str.lower().str.contains(_intl_pat, na=False)
    if _intl.any():
        log.info(f"Excluding {_intl.sum()} international/offshore fund(s)")
        scores = scores[~_intl].reset_index(drop=True)

    # ── Filter out index funds — this site covers actively managed funds only ──
    if "category_display" in scores.columns:
        _idx = scores["category_display"] == "Index Funds"
        if _idx.any():
            log.info(f"Excluding {_idx.sum()} index funds (active-only site)")
            scores = scores[~_idx].reset_index(drop=True)

    log.info("Computing editorial stats …")
    stats = compute_stats(attr, scores)
    log.info(f"  Cautionary tale fund: {stats['tale']['name']}")
    log.info(f"  n_funds={stats['n_funds']}  "
             f"overall_beat={stats['overall_beat']}%  "
             f"pct_pos_alpha={stats['pct_pos_alpha']}%")

    log.info("Preparing leaderboard data …")
    leaderboard, attr_by_fund = prepare_leaderboard(scores, attr)

    log.info("Building HTML …")
    html = build_html(stats, leaderboard, attr_by_fund)
    out.write_text(html, encoding="utf-8")
    log.info(f"\nAlphaLens website → {out}  ({out.stat().st_size/1024:.0f} KB)")
    log.info("Open in any browser — no server required.")

    # ── Auto-push to GitHub so Netlify redeploys with fresh data ──────────
    _push_to_github(out)


def _push_to_github(html_path: Path) -> None:
    """Commit the rebuilt index.html and push to GitHub.
    Requires git to be configured in the project directory.
    Set GITHUB_REPO_DIR env var if the git repo is in a different location."""
    import subprocess, os

    repo_dir = Path(os.getenv("GITHUB_REPO_DIR", str(html_path.parent)))
    index_dst = repo_dir / "index.html"

    try:
        # Copy the built file to the repo (in case they differ)
        if html_path.resolve() != index_dst.resolve():
            import shutil
            shutil.copy2(html_path, index_dst)

        today = __import__("datetime").date.today().isoformat()

        cmds = [
            ["git", "-C", str(repo_dir), "add", "index.html"],
            ["git", "-C", str(repo_dir), "commit", "-m", f"Daily update {today}"],
            ["git", "-C", str(repo_dir), "push"],
        ]
        for cmd in cmds:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                # "nothing to commit" is not a real error
                if "nothing to commit" in result.stdout + result.stderr:
                    log.info("GitHub: nothing changed since last push.")
                    return
                log.warning(f"git command failed: {' '.join(cmd)}\n{result.stderr}")
                return

        log.info(f"GitHub: pushed index.html → Netlify will redeploy automatically.")

    except Exception as e:
        log.warning(f"GitHub push skipped: {e}")


if __name__ == "__main__":
    main()
