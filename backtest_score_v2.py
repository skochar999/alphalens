#!/usr/bin/env python3
"""
backtest_score_v2.py
====================
Walk-forward validation: does the score at month t predict the NEXT 12 months
of net active return?

At each formation month t (monthly, wherever >= MIN_FUNDS funds qualify):
  - v2 score   : skill t-stat + selectivity + patience + concentration + top10
                 + cost, all signals restricted to data <= t.
                 NOTE: the Brinson pick t-stat only exists since 2025-05 (the
                 IEC-1 factor model's start), so the backtest substitutes the
                 skill pillar with the t-stat of trailing 24m benchmark-active
                 returns — the same statistical idea (Kosowski et al. 2006),
                 computable at any historical date. Production v2 uses the
                 Brinson version, which is strictly cleaner.
  - v1_nav     : current dashboard NAV-tier formula at t (trailing net active
                 return 40%, hit rate 35%, steadiness 15%, cost 10%)
  - naive      : trailing net active return alone (what a user does on their own)
  - forward    : compounded fund return t+1..t+12 minus proxy benchmark
                 return, minus TER (net forward active return)

Metrics per score: mean Spearman IC across formations (with t-stat), and mean
forward net active return by formation-date score quintile (Q5 = best).

Known approximations (flagged, not hidden): TER, beta_drift, R² are
full-sample statics; forward returns use the regular-plan NAV series.

Output: mf_data/score_v2_backtest.csv (+ console summary)
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
log = logging.getLogger("bt_v2")

HERE = Path(__file__).parent
DATA = HERE / "mf_data"

MIN_FUNDS = 60          # minimum qualifying funds at a formation date
MIN_PICK_MONTHS = 12
CLIP = 0.25
TRAIL_NAV_MONTHS = 24   # trailing window for v1 NAV stats
FWD = 12                # forward horizon (months)

W2 = dict(pick_t=0.35, sel=0.15, pat=0.15, conc=0.10, top=0.05, cost=0.20)


def pct(s):
    return s.rank(pct=True, method="average") * 100


def month_str(ts):
    return f"{ts.year}-{ts.month:02d}"


def main():
    # ── static inputs ──
    hat = pd.read_parquet(DATA / "holdings_attribution.parquet")
    hat["month"] = hat["month"].astype(str)
    hat["ss"] = hat["stock_selection"].clip(-CLIP, CLIP)

    fm = pd.read_parquet(DATA / "fund_metrics.parquet")[
        ["scheme_code", "r_squared", "beta_drift"]]
    meta = pd.read_parquet(DATA / "fund_meta.parquet")[
        ["scheme_code", "ter_est", "proxy_code"]]
    nav = pd.read_parquet(DATA / "nav_monthly.parquet")
    nav.columns = [int(c) for c in nav.columns]
    nav.index = pd.to_datetime(nav.index)
    rets = nav.pct_change()

    # ── holdings: per-month normalised weight vectors (load once) ──
    map_f = DATA / "direct_to_regular_map.csv"
    m = pd.read_csv(map_f)
    d2r = dict(zip(m["direct_code"], m["regular_code"]))

    wvecs: dict[str, pd.Series] = {}
    for f in sorted(glob.glob(str(DATA / "holdings" / "*.parquet"))):
        mon = Path(f).stem
        try:
            df = pd.read_parquet(f, columns=["scheme_code", "isin", "pct_nav"])
        except Exception:
            continue
        df = df.dropna(subset=["scheme_code", "isin"])
        if not len(df):
            continue
        df["scheme_code"] = pd.to_numeric(df["scheme_code"], errors="coerce")
        df["scheme_code"] = df["scheme_code"].map(lambda c: d2r.get(c, c))
        df = df.dropna(subset=["scheme_code"])
        df["scheme_code"] = df["scheme_code"].astype(int)
        w = df.groupby(["scheme_code", "isin"])["pct_nav"].sum()
        wvecs[mon] = w / w.groupby(level=0).sum()
    months = sorted(wvecs)
    log.info(f"holdings months loaded: {len(months)} ({months[0]}–{months[-1]})")

    # pre-compute monthly per-fund turnover for adjacent pairs
    pair_turn: dict[str, pd.Series] = {}   # month -> Series(scheme_code -> turnover)
    for a, b in zip(months, months[1:]):
        ya, ma_ = int(a[:4]), int(a[5:7])
        yb, mb = int(b[:4]), int(b[5:7])
        if (yb * 12 + mb) - (ya * 12 + ma_) != 1:
            continue
        both = wvecs[a].to_frame("w0").join(wvecs[b].to_frame("w1"), how="outer").fillna(0)
        pair_turn[b] = (both["w0"] - both["w1"]).abs().groupby(level=0).sum() / 2

    statics = meta.merge(fm, on="scheme_code", how="left")
    statics["sel"] = 1 - statics["r_squared"]

    # return gap monthly rows (for the rg-based skill variant)
    rg_path = DATA / "return_gap.parquet"
    rgm = pd.read_parquet(rg_path) if rg_path.exists() else pd.DataFrame()

    # ── formation loop ──
    rows = []
    for t_idx, ts in enumerate(rets.index):
        t = month_str(ts)
        if t_idx + FWD >= len(rets.index):
            continue                      # need 12 forward months

        # conviction up to t: last weights <= t, trailing-12m turnover
        past = [mn for mn in months if mn <= t]
        if len(past) < 7:                 # need turnover pairs
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

        # trailing NAV stats for skill t-stat / v1 at t
        win = rets.iloc[max(0, t_idx - TRAIL_NAV_MONTHS + 1): t_idx + 1]
        fwd_win = rets.iloc[t_idx + 1: t_idx + 1 + FWD]

        df = statics.copy().set_index("scheme_code")
        df["turn"] = turn
        df["conc"] = conc
        df["top10"] = top10
        df = df[df["turn"].notna() & df["conc"].notna()]
        if len(df) < MIN_FUNDS:
            continue

        def trail_stats(r):
            code, proxy = r.name, r["proxy_code"]
            out = pd.Series([np.nan] * 4, index=["tr_act", "tr_hit", "skill_t", "fwd"])
            if code not in win.columns or pd.isna(proxy) or int(proxy) not in win.columns:
                return out
            fr, br = win[code].dropna(), win[int(proxy)].dropna()
            ix = fr.index.intersection(br.index)
            if len(ix) >= 18:
                act = fr[ix] - br[ix]
                fa = (1 + fr[ix]).prod() ** (12 / len(ix)) - 1
                ba = (1 + br[ix]).prod() ** (12 / len(ix)) - 1
                out["tr_act"] = fa - ba - (r["ter_est"] or 0)
                out["tr_hit"] = (act > 0).mean()
                if act.std() > 0:
                    out["skill_t"] = act.mean() / (act.std() / np.sqrt(len(act)))
            ffr, fbr = fwd_win.get(code), fwd_win.get(int(proxy))
            if ffr is not None and fbr is not None:
                ix2 = ffr.dropna().index.intersection(fbr.dropna().index)
                if len(ix2) >= 10:
                    out["fwd"] = ((1 + ffr[ix2]).prod() - (1 + fbr[ix2]).prod()
                                  - (r["ter_est"] or 0))
            return out

        df = df.join(df.apply(trail_stats, axis=1))
        df = df[df["fwd"].notna() & df["skill_t"].notna()]
        if len(df) < MIN_FUNDS:
            continue

        med = df.median(numeric_only=True)
        df["score_v2"] = (pct(df["skill_t"]) * W2["pick_t"]
                          + pct(df["sel"].fillna(med["sel"])) * W2["sel"]
                          + pct(-df["turn"]) * W2["pat"]
                          + pct(-df["conc"]) * W2["conc"]
                          + pct(df["top10"]) * W2["top"]
                          + pct(-df["ter_est"].fillna(med["ter_est"])) * W2["cost"])
        df["score_v1nav"] = (pct(df["tr_act"].fillna(-9)) * 0.40
                             + pct(df["tr_hit"].fillna(0)) * 0.35
                             + pct(-df["beta_drift"].fillna(med["beta_drift"])) * 0.15
                             + pct(-df["ter_est"].fillna(med["ter_est"])) * 0.10)
        df["score_naive"] = pct(df["tr_act"].fillna(-9))

        # v2 + return gap: skill pillar split between active-return t-stat
        # and return-gap t-stat (data <= t, >= 12 months)
        scores = ["score_v2", "score_v1nav", "score_naive"]
        if len(rgm):
            r12 = rgm[rgm["month"] <= t]
            gg = r12.groupby("scheme_code")["return_gap"]
            rgs = pd.DataFrame({"m": gg.mean(), "s": gg.std(), "n": gg.count()})
            rgs = rgs[rgs["n"] >= 12]
            rgs["rg_t"] = rgs["m"] / (rgs["s"] / np.sqrt(rgs["n"]))
            df["rg_t"] = rgs["rg_t"]
            if df["rg_t"].notna().sum() >= MIN_FUNDS:
                sub = df[df["rg_t"].notna()]
                med2 = sub.median(numeric_only=True)
                df.loc[sub.index, "score_v2rg"] = (
                    pct(sub["skill_t"]) * 0.20
                    + pct(sub["rg_t"]) * 0.15
                    + pct(sub["sel"].fillna(med2["sel"])) * W2["sel"]
                    + pct(-sub["turn"]) * W2["pat"]
                    + pct(-sub["conc"]) * W2["conc"]
                    + pct(sub["top10"]) * W2["top"]
                    + pct(-sub["ter_est"].fillna(med2["ter_est"])) * W2["cost"])
                df.loc[sub.index, "score_rgonly"] = pct(sub["rg_t"])
                scores += ["score_v2rg", "score_rgonly"]

        for sc in scores:
            d = df[[sc, "fwd"]].dropna()
            if len(d) < MIN_FUNDS:
                continue
            ic = sps.spearmanr(d[sc], d["fwd"]).statistic
            q = pd.qcut(d[sc], 5, labels=False, duplicates="drop")
            qm = d.groupby(q)["fwd"].mean()
            rows.append(dict(formation=t, score=sc, n=len(df), ic=ic,
                             q1=qm.get(0, np.nan), q5=qm.get(4, np.nan),
                             spread=qm.get(4, np.nan) - qm.get(0, np.nan)))
        log.info(f"{t}: n={len(df)}  " + "  ".join(
            f"{r['score'][6:]}: IC={r['ic']:+.2f} spr={r['spread']*100:+.1f}pp"
            for r in rows[-3:]))

    res = pd.DataFrame(rows)
    res.to_csv(DATA / "score_v2_backtest.csv", index=False)
    log.info("\n===== SUMMARY (mean across formations) =====")
    for sc, grp in res.groupby("score"):
        ics = grp["ic"].dropna()
        tstat = ics.mean() / (ics.std() / np.sqrt(len(ics))) if len(ics) > 1 else np.nan
        log.info(f"{sc:14s} formations={len(grp):2d}  meanIC={ics.mean():+.3f} "
                 f"(t={tstat:+.1f})  meanQ5-Q1={grp['spread'].mean()*100:+.2f}pp  "
                 f"meanQ5={grp['q5'].mean()*100:+.2f}pp  meanQ1={grp['q1'].mean()*100:+.2f}pp")


if __name__ == "__main__":
    main()
