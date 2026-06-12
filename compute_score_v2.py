#!/usr/bin/env python3
"""
compute_score_v2.py
===================
Research-backed forward-looking fund score ("Skill Score v2").

Design principle: score the BEHAVIOURS the academic literature shows persist,
not past returns (which it shows don't). Three pillars:

  SKILL (50%)        — does the manager demonstrably pick stocks well?
    35%  pick_t        t-statistic of monthly Brinson stock-selection alpha
                       (Kosowski et al. 2006; Barras-Scaillet-Wermers 2010:
                       alpha scaled by its noise is persistent, raw alpha isn't)
    15%  selectivity   1 − R² vs the factor model
                       (Amihud & Goyenko 2013: low-R² funds outperform)

  CONVICTION (30%)   — does the fund behave like an active, patient manager?
    15%  patience      LOW portfolio turnover, from month-to-month holdings
                       (Cremers & Pareek 2016: patient active funds outperform;
                       high-turnover active funds don't)
    10%  concentration sector/name concentration (effective-N of holdings)
                       (Kacperczyk-Sialm-Zheng 2005: concentrated funds win)
     5%  top10_w       weight in top-10 positions — "best ideas" conviction
                       (Cohen-Polk-Silli 2021)

  COST (20%)         — the most robust predictor of all (Carhart 1997)
    20%  low TER

Tier-2 (no/insufficient holdings): NAV-only fallback
    40%  alpha t-stat proxy (info_ratio × sqrt(years))   15% selectivity
    25%  cost                                            20% steadiness

All sub-scores are percentile ranks within the scoring universe (peer-relative).
Funds need >= MIN_PICK_MONTHS attribution months for tier-1.

--asof YYYY-MM limits every holdings-derived signal to data <= that month
(used by the walk-forward backtest; avoids look-ahead).
NOTE: selectivity (R²) and TER are full-sample/static — flagged as a known
backtest approximation.

Output: mf_data/scores_v2.parquet (or --out)
"""
from __future__ import annotations
import argparse
import glob
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("score_v2")

HERE = Path(__file__).parent
DATA = HERE / "mf_data"

MIN_PICK_MONTHS = 12
MONTHLY_PICK_CLIP = 0.25
MIN_TURNOVER_PAIRS = 6

# FINAL weights — selected by walk-forward sweep (25 formations 2023-06..
# 2025-06, 16 variants + 4 refinements): mean rank-IC +0.088, median +0.068,
# IC>0 in 76% of formations, t=4.0, Q5−Q1 = +2.2pp/yr.
# (vs current dashboard formula: IC +0.018, t=0.2 — indistinguishable from 0.)
W_TIER1 = dict(pick_t=0.20, selectivity=0.25, patience=0.15,
               concentration=0.15, top10=0.15, cost=0.10)
W_TIER2 = dict(alpha_t=0.40, selectivity=0.15, steady=0.20, cost=0.25)


def pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True, method="average") * 100


def pick_tstats(hat: pd.DataFrame, asof: str | None) -> pd.DataFrame:
    """t-stat of monthly stock-selection alpha per fund (winsorised)."""
    df = hat.copy()
    df["month"] = df["month"].astype(str)
    if asof:
        df = df[df["month"] <= asof]
    df["ss"] = df["stock_selection"].clip(-MONTHLY_PICK_CLIP, MONTHLY_PICK_CLIP)
    g = df.groupby("scheme_code")["ss"]
    out = pd.DataFrame({
        "pick_mean": g.mean(), "pick_std": g.std(), "pick_n": g.count(),
    }).reset_index()
    out["pick_t"] = out["pick_mean"] / (out["pick_std"] / np.sqrt(out["pick_n"]))
    out.loc[out["pick_n"] < MIN_PICK_MONTHS, "pick_t"] = np.nan
    out["pick_ann_pp"] = out["pick_mean"] * 12 * 100
    return out[["scheme_code", "pick_t", "pick_ann_pp", "pick_n"]]


def load_holdings(asof: str | None) -> dict[str, pd.DataFrame]:
    """{month: holdings df} for all months <= asof, with scheme codes mapped
    direct→regular (the on-disk files keep direct codes for the pre-Phase-1
    houses; the pipeline remaps them in step 2.5 — replicate that here)."""
    d2r = {}
    map_f = DATA / "direct_to_regular_map.csv"
    if map_f.exists():
        m = pd.read_csv(map_f)
        dc = next(c for c in m.columns if "direct" in c.lower())
        rc = next(c for c in m.columns if "regular" in c.lower())
        d2r = dict(zip(pd.to_numeric(m[dc], errors="coerce"),
                       pd.to_numeric(m[rc], errors="coerce")))
    out = {}
    for f in sorted(glob.glob(str(DATA / "holdings" / "*.parquet"))):
        mon = Path(f).stem
        if asof and mon > asof:
            continue
        try:
            df = pd.read_parquet(f, columns=["scheme_code", "isin", "pct_nav"])
        except Exception as e:
            log.warning(f"unreadable {mon}: {e}")
            continue
        df = df.dropna(subset=["scheme_code", "isin"])
        if len(df):
            df["scheme_code"] = pd.to_numeric(df["scheme_code"], errors="coerce")
            df["scheme_code"] = df["scheme_code"].map(lambda c: d2r.get(c, c))
            out[mon] = df.dropna(subset=["scheme_code"])
    return out


def conviction_signals(hold: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Turnover (annualised), effective-N concentration, top-10 weight."""
    months = sorted(hold)
    if not months:
        return pd.DataFrame(columns=["scheme_code", "turnover_ann", "eff_n", "top10_w"])

    # per-fund per-month normalised weight vectors
    def wvec(df):
        w = df.groupby(["scheme_code", "isin"])["pct_nav"].sum()
        tot = w.groupby(level=0).sum()
        return w / tot  # normalised within fund

    # turnover: mean over consecutive-month pairs of  sum|w_t - w_{t-1}| / 2
    pair_turn: dict[int, list[float]] = {}
    prev = None
    prev_mon = None
    for mon in months:
        cur = wvec(hold[mon])
        if prev is not None:
            # only adjacent calendar months count as a pair
            py, pm = int(prev_mon[:4]), int(prev_mon[5:7])
            cy, cm = int(mon[:4]), int(mon[5:7])
            if (cy * 12 + cm) - (py * 12 + pm) == 1:
                both = prev.to_frame("w0").join(cur.to_frame("w1"), how="outer").fillna(0)
                t = (both["w0"] - both["w1"]).abs().groupby(level=0).sum() / 2
                for code, v in t.items():
                    pair_turn.setdefault(int(code), []).append(float(v))
        prev, prev_mon = cur, mon

    # concentration from each fund's LAST AVAILABLE month (within 3 months of
    # the window end — houses publish on slightly different lags)
    cutoff_idx = max(0, len(months) - 3)
    last_w: dict[int, pd.Series] = {}
    for mon in months[cutoff_idx:]:
        cur = wvec(hold[mon])
        for code, s in cur.groupby(level=0):
            last_w[int(code)] = s.droplevel(0)

    rows = []
    for code in set(last_w) | set(pair_turn):
        turns = pair_turn.get(int(code), [])
        w = last_w.get(int(code))
        rows.append(dict(
            scheme_code=int(code),
            turnover_ann=(np.median(turns) * 12 if len(turns) >= MIN_TURNOVER_PAIRS else np.nan),
            n_turn_pairs=len(turns),
            eff_n=(float(1.0 / w.pow(2).sum()) if w is not None else np.nan),
            top10_w=(float(w.nlargest(10).sum()) if w is not None else np.nan),
        ))
    return pd.DataFrame(rows)


def run(asof: str | None, out_path: Path) -> pd.DataFrame:
    hat = pd.read_parquet(DATA / "holdings_attribution.parquet")
    fm = pd.read_parquet(DATA / "fund_metrics.parquet")[
        ["scheme_code", "r_squared", "info_ratio", "n_months", "beta_drift"]]
    bm = pd.read_parquet(DATA / "benchmark_metrics.parquet")[
        ["scheme_code", "scheme_name", "amc", "category", "net_active_ann", "hit_rate"]]
    meta = pd.read_parquet(DATA / "fund_meta.parquet")[["scheme_code", "ter_est", "category"]]

    picks = pick_tstats(hat, asof)
    conv = conviction_signals(load_holdings(asof))
    log.info(f"signals: picks={picks['pick_t'].notna().sum()} funds, "
             f"turnover={conv['turnover_ann'].notna().sum()} funds")

    # Equity-dominance gate: the skill/conviction signals are built for
    # equity portfolios. Debt-heavy hybrids (conservative hybrid, equity
    # savings, multi-asset) game them — their "stock selection" residual is
    # mostly steady debt carry (an ultra-consistent t-stat) and the factor
    # model explains little of a debt book (spuriously high selectivity).
    # Funds below the threshold are scored on the NAV tier instead.
    # Category gate: stock-picking signals only apply to funds MANDATED to
    # pick stocks. Debt-heavy categories (conservative hybrid, equity savings,
    # arbitrage, multi-asset, BAF) game them — their "stock selection"
    # residual is mostly steady debt carry / cash timing.
    EQUITY_CATS = ("large cap", "mid cap", "small cap", "large & mid",
                   "flexi cap", "multi cap", "focused", "value", "contra",
                   "elss", "sectoral", "thematic", "dividend yield",
                   "aggressive hybrid")
    cat_map = pd.read_parquet(DATA / "fund_meta.parquet")[
        ["scheme_code", "category"]].set_index("scheme_code")["category"]
    is_eq = picks["scheme_code"].map(
        lambda c: any(k in str(cat_map.get(c, "")).lower() for k in EQUITY_CATS))
    n_gated = int((~is_eq).sum())
    picks.loc[~is_eq, "pick_t"] = np.nan
    log.info(f"category gate: {n_gated} non-equity-mandate funds -> NAV tier")

    df = bm.merge(meta[["scheme_code", "ter_est"]], on="scheme_code", how="left") \
           .merge(fm, on="scheme_code", how="left") \
           .merge(picks, on="scheme_code", how="left") \
           .merge(conv, on="scheme_code", how="left")
    df["selectivity"] = 1 - df["r_squared"]

    tier1 = (df["pick_t"].notna() & df["turnover_ann"].notna()
             & df["eff_n"].notna())
    log.info(f"tier-1 (full holdings signals): {tier1.sum()} / {len(df)}")

    # ── tier-1 percentile blend (ranked within tier-1 universe) ──
    t1 = df[tier1]
    df.loc[tier1, "p_skill_pick"] = pct(t1["pick_t"])
    df.loc[tier1, "p_skill_sel"] = pct(t1["selectivity"].fillna(t1["selectivity"].median()))
    df.loc[tier1, "p_conv_patience"] = pct(-t1["turnover_ann"])
    df.loc[tier1, "p_conv_conc"] = pct(-t1["eff_n"])           # fewer eff names = concentrated
    df.loc[tier1, "p_conv_top10"] = pct(t1["top10_w"])
    df.loc[tier1, "p_cost"] = pct(-t1["ter_est"].fillna(t1["ter_est"].median()))
    df.loc[tier1, "score_v2"] = (
        df.loc[tier1, "p_skill_pick"] * W_TIER1["pick_t"]
        + df.loc[tier1, "p_skill_sel"] * W_TIER1["selectivity"]
        + df.loc[tier1, "p_conv_patience"] * W_TIER1["patience"]
        + df.loc[tier1, "p_conv_conc"] * W_TIER1["concentration"]
        + df.loc[tier1, "p_conv_top10"] * W_TIER1["top10"]
        + df.loc[tier1, "p_cost"] * W_TIER1["cost"]
    )
    # pillar roll-ups for display (proportional to final weights:
    # Skill 45% = pick 20 + selectivity 25; Conviction 45% = patience 15 +
    # concentration 15 + top10 15; Cost 10%)
    df.loc[tier1, "pillar_skill"] = (df.loc[tier1, "p_skill_pick"] * (20/45)
                                     + df.loc[tier1, "p_skill_sel"] * (25/45))
    df.loc[tier1, "pillar_conviction"] = (df.loc[tier1, "p_conv_patience"] / 3
                                          + df.loc[tier1, "p_conv_conc"] / 3
                                          + df.loc[tier1, "p_conv_top10"] / 3)
    df.loc[tier1, "pillar_cost"] = df.loc[tier1, "p_cost"]

    # ── tier-2 NAV-only fallback ──
    # Benchmark-relative signals ONLY (active IR + hit rate vs the fund's own
    # proxy). Deliberately NOT the factor-model info_ratio or selectivity:
    # for debt-heavy funds those contain steady debt carry / unexplained debt
    # variance and rank them spuriously at the very top of the table.
    t2m = ~tier1
    t2 = df[t2m]
    bm_ir = pd.read_parquet(DATA / "benchmark_metrics.parquet")[
        ["scheme_code", "active_ir"]].set_index("scheme_code")["active_ir"]
    air = t2["scheme_code"].map(bm_ir)
    raw_t2 = (
        pct(air.fillna(air.median())) * 0.40
        + pct(t2["hit_rate"].fillna(0)) * 0.20
        + pct(-t2["beta_drift"].fillna(t2["beta_drift"].median())) * 0.15
        + pct(-t2["ter_est"].fillna(t2["ter_est"].median())) * 0.25
    )
    # Confidence shrink: tier-2 funds lack 12 months of verified holdings
    # signals, so their (returns-led) score is pulled 30% toward neutral —
    # an unverified hot streak shouldn't outrank holdings-verified skill.
    df.loc[t2m, "score_v2"] = 50 + (raw_t2 - 50) * 0.70
    df["tier"] = np.where(tier1, 1, 2)
    df["asof"] = asof or "latest"

    df.to_parquet(out_path, index=False)
    log.info(f"saved {out_path}  (tier-1 score range "
             f"{df.loc[tier1,'score_v2'].min():.0f}–{df.loc[tier1,'score_v2'].max():.0f})")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None, help="YYYY-MM cap for holdings signals")
    ap.add_argument("--out", default=str(DATA / "scores_v2.parquet"))
    a = ap.parse_args()
    run(a.asof, Path(a.out))


if __name__ == "__main__":
    main()
