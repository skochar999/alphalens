#!/usr/bin/env python3
"""
build_fundlens_v3.py
====================
Rebuilds fundlens_v3.html by injecting fresh DATA from scored_funds.parquet
into the existing HTML template.  The template is fundlens_v3.html itself;
we replace the DATA const in-place.

Usage:
    python build_fundlens_v3.py
    python build_fundlens_v3.py --data-dir /path/to/mf_data
"""
from __future__ import annotations
import argparse, json, logging, math, re
from pathlib import Path
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("fundlens.build")

HERE     = Path(__file__).parent
DATA_DIR = HERE / "mf_data"
OUT_HTML = HERE / "fundlens_v3.html"

CAT_ORDER = [
    "Large Cap","Mid Cap","Small Cap","Large & Mid Cap","Flexi Cap",
    "Multi Cap","Focused","Value / Contrarian","Tax Saver (ELSS)",
    "Thematic / Sectoral","Balanced Advantage","Hybrid","Retirement","Index Funds",
]
SUBTITLES = {
    "Large Cap":          "Funds investing in India's 100 biggest companies",
    "Mid Cap":            "Funds investing in companies ranked 101–250 by size",
    "Small Cap":          "Funds investing in smaller, high-growth companies",
    "Large & Mid Cap":    "Funds split between large and mid-sized companies",
    "Flexi Cap":          "Funds that can invest anywhere — large, mid, or small",
    "Multi Cap":          "Funds required to spread bets across all company sizes",
    "Focused":            "Funds that concentrate on their very best ideas",
    "Value / Contrarian": "Funds that buy stocks others are avoiding",
    "Tax Saver (ELSS)":   "Funds that save you tax (Section 80C) with a 3-year lock-in",
    "Thematic / Sectoral":"Funds focused on one industry or theme",
    "Balanced Advantage": "Funds that automatically shift between stocks and bonds",
    "Hybrid":             "Funds that mix stocks and bonds in fixed proportions",
    "Retirement":         "Funds designed for long-term retirement goals",
    "Index Funds":        "Funds that copy an index — low cost, no active bets",
}

def r1(v): return round(float(v), 1) if pd.notna(v) and not math.isinf(v) else None
def r0(v): return int(round(float(v))) if pd.notna(v) and not math.isinf(v) else None

def build_data(data_dir: Path) -> dict:
    df = pd.read_parquet(data_dir / "scored_funds.parquet")

    out = {}
    for cat in CAT_ORDER:
        cat_df = df[df["category_display"] == cat].sort_values("cat_rank")
        if cat_df.empty:
            continue
        funds = []
        for _, row in cat_df.iterrows():
            # decomp
            decomp = None
            if row.get("decomp_ok", False):
                d_style  = r1(row.get("d_style"))
                d_sector = r1(row.get("d_sector"))
                d_pick   = r1(row.get("d_pick"))
                d_timing = r1(row.get("d_timing"))
                if all(v is not None for v in [d_style, d_sector, d_pick, d_timing]):
                    total = r1(abs(d_style or 0) + abs(d_sector or 0) + abs(d_pick or 0) + abs(d_timing or 0))
                    decomp = {"style":d_style,"sector":d_sector,"pick":d_pick,"timing":d_timing,"total":total}

            # clean name — strip scheme suffix junk
            name = str(row.get("scheme_name",""))
            name = re.sub(r'\s*-\s*(Growth Option|Growth Plan|Direct Plan|Growth)\s*$','',name,flags=re.I).strip()
            name = re.sub(r'\s*-\s*Direct\s*$','',name,flags=re.I).strip()

            funds.append({
                "rank":          int(row["cat_rank"]),
                "name":          name,
                "amc":           str(row.get("amc","")),
                "score":         r1(row.get("total_score")),
                "n_months":      r0(row.get("n_months")),
                "active_pp":     r1((row.get("active_ann") or 0)*100),
                "net_active_pp": r1((row.get("net_active_ann") or 0)*100),
                "hit_rate_pct":  r0((row.get("hit_rate") or 0)*100),
                "ann_ret_pp":    r1((row.get("ann_ret") or 0)*100),
                "net_ann_ret_pp":r1((row.get("net_ann_ret") or 0)*100),
                "ter_pct":       r1((row.get("ter_est") or 0)*100),
                "is_index":      bool(row.get("category_display") == "Index Funds"),
                "skill_label":   str(row.get("skill_label","")),
                "decomp":        decomp,
                "has_benchmark": bool(row.get("has_benchmark", False)),
            })
        out[cat] = {"subtitle": SUBTITLES.get(cat,""), "count": len(funds), "funds": funds}

    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default=str(DATA_DIR))
    args = p.parse_args()
    data_dir = Path(args.data_dir)

    log.info("Building DATA from scored_funds.parquet …")
    data = build_data(data_dir)
    data_js = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    n_funds = sum(c["count"] for c in data.values())
    log.info(f"  {len(data)} categories, {n_funds} funds")

    # Read existing HTML, replace DATA blob
    if not OUT_HTML.exists():
        log.error(f"Template not found: {OUT_HTML}")
        return

    html = OUT_HTML.read_text(encoding="utf-8")
    new_html = re.sub(
        r'(const DATA = )(\{.*?\})(;)',
        lambda m: m.group(1) + data_js + m.group(3),
        html, count=1, flags=re.DOTALL
    )
    if new_html == html:
        log.warning("  No DATA block found — check template")
    else:
        OUT_HTML.write_text(new_html, encoding="utf-8")
        log.info(f"  Saved: {OUT_HTML}  ({OUT_HTML.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
