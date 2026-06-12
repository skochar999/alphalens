#!/usr/bin/env python3
"""
sweep_score_weights.py
======================
Disciplined weight search over the Score v2 ingredients, evaluated walk-forward
(same protocol as backtest_score_v2.py). A SMALL grid of defensible variants —
not thousands — selected on mean rank-IC with consistency tiebreaks
(median IC, % formations IC>0), to limit overfitting to one regime.

Signals per formation t (all data <= t): skill_t (trailing 24m active-return
t-stat), sel (1−R², static), patience (−turnover), conc (−effective N),
top10, cost (−TER). Forward: next-12m net active return.

Output: mf_data/score_weight_sweep.csv + console ranking.
"""
from __future__ import annotations
import glob
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("sweep")

HERE = Path(__file__).parent
DATA = HERE / "mf_data"
MIN_FUNDS = 60
TRAIL = 24
FWD = 12

# variant name -> weights (skill, sel, pat, conc, top10, cost)
VARIANTS = {
    "v2_base":        (.35, .15, .15, .10, .05, .20),
    "skill_heavy":    (.50, .10, .15, .05, .00, .20),
    "skill_50_cost":  (.50, .00, .00, .00, .00, .50),
    "conviction_hvy": (.25, .10, .25, .15, .05, .20),
    "patience_heavy": (.25, .10, .30, .10, .05, .20),
    "cost_heavy":     (.30, .10, .10, .05, .05, .40),
    "no_sel":         (.40, .00, .20, .10, .05, .25),
    "no_top10":       (.40, .15, .15, .10, .00, .20),
    "equalish":       (.17, .17, .17, .17, .15, .17),
    "skill_pat":      (.45, .00, .35, .00, .00, .20),
    "pure_skill":     (1.0, .00, .00, .00, .00, .00),
    "pure_patience":  (.00, .00, 1.0, .00, .00, .00),
    "pure_conc":      (.00, .00, .00, 1.0, .00, .00),
    "pure_cost":      (.00, .00, .00, .00, .00, 1.0),
    "pure_sel":       (.00, 1.0, .00, .00, .00, .00),
    "pure_top10":     (.00, .00, .00, .00, 1.0, .00),
}


def pct(s):
    return s.rank(pct=True, method="average") * 100


def month_str(ts):
    return f"{ts.year}-{ts.month:02d}"


def build_formations():
    fm = pd.read_parquet(DATA / "fund_metrics.parquet")[["scheme_code", "r_squared"]]
    meta = pd.read_parquet(DATA / "fund_meta.parquet")[["scheme_code", "ter_est", "proxy_code"]]
    statics = meta.merge(fm, on="scheme_code", how="left")
    statics["sel"] = 1 - statics["r_squared"]

    nav = pd.read_parquet(DATA / "nav_monthly.parquet")
    nav.columns = [int(c) for c in nav.columns]
    nav.index = pd.to_datetime(nav.index)
    rets = nav.pct_change(fill_method=None)

    m = pd.read_csv(DATA / "direct_to_regular_map.csv")
    d2r = dict(zip(m["direct_code"], m["regular_code"]))
    wvecs = {}
    for f in sorted(glob.glob(str(DATA / "holdings" / "*.parquet"))):
        mon = Path(f).stem
        try:
            df = pd.read_parquet(f, columns=["scheme_code", "isin", "pct_nav"])
        except Exception:
            continue
        df = df.dropna(subset=["scheme_code", "isin"])
        if not len(df):
            continue
        df["scheme_code"] = pd.to_numeric(df["scheme_code"], errors="coerce") \
                              .map(lambda c: d2r.get(c, c))
        df = df.dropna(subset=["scheme_code"])
        df["scheme_code"] = df["scheme_code"].astype(int)
        w = df.groupby(["scheme_code", "isin"])["pct_nav"].sum()
        wvecs[mon] = w / w.groupby(level=0).sum()
    months = sorted(wvecs)

    pair_turn = {}
    for a, b in zip(months, months[1:]):
        if (int(b[:4]) * 12 + int(b[5:7])) - (int(a[:4]) * 12 + int(a[5:7])) != 1:
            continue
        both = wvecs[a].to_frame("w0").join(wvecs[b].to_frame("w1"), how="outer").fillna(0)
        pair_turn[b] = (both["w0"] - both["w1"]).abs().groupby(level=0).sum() / 2

    forms = []
    for t_idx, ts in enumerate(rets.index):
        t = month_str(ts)
        if t_idx + FWD >= len(rets.index):
            continue
        past = [mn for mn in months if mn <= t]
        if len(past) < 7:
            continue
        lastw = {}
        for mn in past[-3:]:
            for code, s in wvecs[mn].groupby(level=0):
                lastw[code] = s.droplevel(0)
        t12 = [mn for mn in pair_turn if mn <= t][-12:]
        tdf = pd.DataFrame({mn: pair_turn[mn] for mn in t12})
        turn = tdf.median(axis=1, skipna=True)[tdf.notna().sum(axis=1) >= 6] * 12
        conc = pd.Series({c: 1.0 / s.pow(2).sum() for c, s in lastw.items()})
        top10 = pd.Series({c: s.nlargest(10).sum() for c, s in lastw.items()})

        win = rets.iloc[max(0, t_idx - TRAIL + 1): t_idx + 1]
        fwd_win = rets.iloc[t_idx + 1: t_idx + 1 + FWD]
        df = statics.copy().set_index("scheme_code")
        df["turn"], df["conc"], df["top10"] = turn, conc, top10
        df = df[df["turn"].notna() & df["conc"].notna()]
        if len(df) < MIN_FUNDS:
            continue

        def stats_row(r):
            code, proxy = r.name, r["proxy_code"]
            out = pd.Series([np.nan] * 2, index=["skill_t", "fwd"])
            if code not in win.columns or pd.isna(proxy) or int(proxy) not in win.columns:
                return out
            fr, br = win[code].dropna(), win[int(proxy)].dropna()
            ix = fr.index.intersection(br.index)
            if len(ix) >= 18:
                act = fr[ix] - br[ix]
                if act.std() > 0:
                    out["skill_t"] = act.mean() / (act.std() / np.sqrt(len(act)))
            ffr, fbr = fwd_win.get(code), fwd_win.get(int(proxy))
            if ffr is not None and fbr is not None:
                ix2 = ffr.dropna().index.intersection(fbr.dropna().index)
                if len(ix2) >= 10:
                    out["fwd"] = ((1 + ffr[ix2]).prod() - (1 + fbr[ix2]).prod()
                                  - (r["ter_est"] or 0))
            return out

        df = df.join(df.apply(stats_row, axis=1))
        df = df[df["fwd"].notna() & df["skill_t"].notna()]
        if len(df) < MIN_FUNDS:
            continue
        med = df.median(numeric_only=True)
        sig = pd.DataFrame({
            "p_skill": pct(df["skill_t"]),
            "p_sel": pct(df["sel"].fillna(med["sel"])),
            "p_pat": pct(-df["turn"]),
            "p_conc": pct(-df["conc"]),
            "p_top10": pct(df["top10"]),
            "p_cost": pct(-df["ter_est"].fillna(med["ter_est"])),
            "fwd": df["fwd"],
        })
        forms.append((t, sig))
    return forms


def main():
    forms = build_formations()
    log.info(f"formations: {len(forms)} ({forms[0][0]} – {forms[-1][0]})")
    rows = []
    for name, (wk, ws, wp, wc, wt, wco) in VARIANTS.items():
        ics, spreads = [], []
        for t, sig in forms:
            sc = (sig["p_skill"] * wk + sig["p_sel"] * ws + sig["p_pat"] * wp
                  + sig["p_conc"] * wc + sig["p_top10"] * wt + sig["p_cost"] * wco)
            ic = sps.spearmanr(sc, sig["fwd"]).statistic
            q = pd.qcut(sc, 5, labels=False, duplicates="drop")
            qm = sig.groupby(q)["fwd"].mean()
            ics.append(ic)
            spreads.append(qm.get(4, np.nan) - qm.get(0, np.nan))
        ics = pd.Series(ics)
        rows.append(dict(variant=name, mean_ic=ics.mean(), median_ic=ics.median(),
                         pct_pos=(ics > 0).mean(),
                         t_ic=ics.mean() / (ics.std() / np.sqrt(len(ics))),
                         mean_spread_pp=np.nanmean(spreads) * 100))
    res = pd.DataFrame(rows).sort_values("mean_ic", ascending=False)
    res.to_csv(DATA / "score_weight_sweep.csv", index=False)
    log.info("\n" + res.to_string(index=False, float_format=lambda v: f"{v:+.3f}"))
    log.info("SWEEP_COMPLETE")


if __name__ == "__main__":
    main()
