#!/usr/bin/env python3
"""
compute_return_gap.py
=====================
Return Gap (Kacperczyk, Sialm & Zheng 2008): the fund's actual (gross-of-fee)
return minus the return its last-disclosed holdings would have earned
buy-and-hold. Persistent positive gap = manager's unobserved actions (interim
trades, IPO allocations, execution) add value.

    RG_t = (fund_net_ret_t + TER/12)  −  Σ_i w_i,last_disclosure × r_i,t

- weights renormalised to the matched (priced) portion; months kept only if
  matched weight >= MIN_COVER of the disclosed equity portfolio
- holdings lag: uses the most recent disclosure 1–2 months before t (we know
  the portfolio as of month-end t-1 for return month t)
- scheme codes mapped direct→regular to align with NAV series

Outputs:
  mf_data/return_gap.parquet          per fund × month
  mf_data/return_gap_summary.parquet  per fund: rg_ann_pp, rg_t, n, coverage
"""
from __future__ import annotations
import glob
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("retgap")

HERE = Path(__file__).parent
DATA = HERE / "mf_data"
MIN_COVER = 0.60


def month_str(ts) -> str:
    return f"{ts.year}-{ts.month:02d}"


def main():
    rets = pd.read_parquet(DATA / "stock_returns_monthly.parquet")
    rets.index = pd.to_datetime(rets.index)
    # label each stock-return row by its month
    ret_by_month = {month_str(ts): rets.loc[ts] for ts in rets.index}

    nav = pd.read_parquet(DATA / "nav_monthly.parquet")
    nav.columns = [int(c) for c in nav.columns]
    nav.index = pd.to_datetime(nav.index)
    fund_rets = nav.pct_change(fill_method=None)
    fr_by_month = {month_str(ts): fund_rets.loc[ts] for ts in fund_rets.index}

    meta = pd.read_parquet(DATA / "fund_meta.parquet")[["scheme_code", "ter_est"]]
    ter = dict(zip(meta["scheme_code"], meta["ter_est"].fillna(0.018)))

    m = pd.read_csv(DATA / "direct_to_regular_map.csv")
    d2r = dict(zip(m["direct_code"], m["regular_code"]))

    # holdings weight vectors per month
    wvecs: dict[str, pd.DataFrame] = {}
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
        wvecs[mon] = df.groupby(["scheme_code", "isin"])["pct_nav"].sum().reset_index()
    months = sorted(wvecs)
    log.info(f"holdings months: {len(months)} ({months[0]}–{months[-1]})")

    def prev_months(t: str, k: int = 2) -> list[str]:
        y, mo = int(t[:4]), int(t[5:7])
        out = []
        for d in range(1, k + 1):
            yy, mm = y, mo - d
            while mm < 1:
                mm += 12
                yy -= 1
            out.append(f"{yy}-{mm:02d}")
        return out

    rows = []
    all_ret_months = sorted(set(ret_by_month) & set(fr_by_month))
    for t in all_ret_months:
        if t < "2022-01":
            continue
        # latest disclosure 1–2 months before t
        src = next((pm for pm in prev_months(t) if pm in wvecs), None)
        if src is None:
            continue
        r = ret_by_month[t]
        fr = fr_by_month[t]
        for code, grp in wvecs[src].groupby("scheme_code"):
            if code not in fr.index or pd.isna(fr[code]):
                continue
            w = grp.set_index("isin")["pct_nav"]
            tot = w.sum()
            if tot <= 0:
                continue
            sr = r.reindex(w.index)
            ok = sr.notna()
            cover = w[ok].sum() / tot
            if cover < MIN_COVER:
                continue
            # weights are % of NAV: equity sleeve earns the (renormalised)
            # matched-equity return; the non-equity remainder (cash/debt/
            # arbitrage book) earns ~risk-free. Without this split the gap
            # just measures cash drag and flips sign with the market.
            # scale-aware: most files store pct (sum≈96), some store fractions
            # (sum≈0.96) — detect by magnitude
            eq_w = min(max(tot / 100.0 if tot > 2.0 else tot, 0.0), 1.0)
            eq_ret = float((w[ok] / w[ok].sum() * sr[ok]).sum())
            RF_M = 0.0055                       # ~6.6% p.a. money-market
            implied = eq_w * eq_ret + (1.0 - eq_w) * RF_M
            gap = float(fr[code]) + ter.get(code, 0.018) / 12 - implied
            rows.append(dict(scheme_code=int(code), month=t, return_gap=gap,
                             implied_ret=implied, fund_ret=float(fr[code]),
                             coverage=float(cover), src_month=src))

    rg = pd.DataFrame(rows)
    rg.to_parquet(DATA / "return_gap.parquet", index=False)
    log.info(f"return_gap: {len(rg)} fund-months, {rg['scheme_code'].nunique()} funds, "
             f"months {rg['month'].min()}–{rg['month'].max()}")

    g = rg.groupby("scheme_code")["return_gap"]
    summ = pd.DataFrame({
        "rg_mean": g.mean(), "rg_std": g.std(), "rg_n": g.count(),
        "rg_cover": rg.groupby("scheme_code")["coverage"].mean(),
    }).reset_index()
    summ["rg_ann_pp"] = summ["rg_mean"] * 12 * 100
    summ["rg_t"] = summ["rg_mean"] / (summ["rg_std"] / np.sqrt(summ["rg_n"]))
    summ.to_parquet(DATA / "return_gap_summary.parquet", index=False)
    log.info(f"summary: {len(summ)} funds | median rg_ann "
             f"{summ['rg_ann_pp'].median():+.2f}pp | funds with rg_n>=24: "
             f"{(summ['rg_n'] >= 24).sum()}")
    log.info("RETURN_GAP_COMPLETE")


if __name__ == "__main__":
    main()
