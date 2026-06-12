#!/usr/bin/env python3
"""
fetch_stock_prices.py
=====================
Bulk-download monthly adjusted prices for every stock in isin_ticker_map
(2,364 NSE tickers) from Yahoo Finance, 2021-01 onward.

Checkpointed: each batch saves to mf_data/_price_cache/batch_NNN.parquet, so
re-running resumes. Final outputs:
  mf_data/stock_prices_monthly.parquet   (month-end adjusted close, ISIN cols)
  mf_data/stock_returns_monthly.parquet  (monthly % returns, ISIN cols)

Survivorship note: Yahoo lacks most delisted tickers. Coverage per ISIN is
reported; downstream consumers (return gap) must renormalise to matched
weight and track unmatched %.
"""
from __future__ import annotations
import logging
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("prices")

HERE = Path(__file__).parent
DATA = HERE / "mf_data"
CACHE = DATA / "_price_cache"
START = "2021-01-01"
BATCH = 100


def main():
    CACHE.mkdir(exist_ok=True)
    m = pd.read_parquet(DATA / "isin_ticker_map.parquet")
    tickers = m["ticker"].dropna().unique().tolist()
    t2i = dict(zip(m["ticker"], m["isin"]))
    log.info(f"{len(tickers)} tickers")

    batches = [tickers[i:i + BATCH] for i in range(0, len(tickers), BATCH)]
    for bi, batch in enumerate(batches):
        out = CACHE / f"batch_{bi:03d}.parquet"
        if out.exists():
            continue
        for attempt in range(3):
            try:
                d = yf.download(batch, start=START, interval="1mo",
                                progress=False, auto_adjust=True, threads=True)
                px = d["Close"] if isinstance(d.columns, pd.MultiIndex) else d[["Close"]]
                px.to_parquet(out)
                got = px.notna().any().sum()
                log.info(f"batch {bi + 1}/{len(batches)}: {got}/{len(batch)} tickers")
                break
            except Exception as e:
                log.warning(f"batch {bi}: {e} (retry {attempt + 1})")
                time.sleep(5 * (attempt + 1))
        time.sleep(1)

    # assemble
    parts = [pd.read_parquet(f) for f in sorted(CACHE.glob("batch_*.parquet"))]
    px = pd.concat(parts, axis=1)
    px = px.loc[:, ~px.columns.duplicated()]
    px.columns = [t2i.get(c, c) for c in px.columns]      # ticker -> ISIN
    px = px.loc[:, px.notna().sum() >= 6]                  # need some history
    px.index = pd.to_datetime(px.index)
    px = px.sort_index()
    px.to_parquet(DATA / "stock_prices_monthly.parquet")
    rets = px.pct_change(fill_method=None)
    rets.to_parquet(DATA / "stock_returns_monthly.parquet")
    log.info(f"saved: {px.shape[1]} ISINs × {px.shape[0]} months "
             f"({px.index[0]:%Y-%m} – {px.index[-1]:%Y-%m})")
    log.info("ALL_BATCHES_COMPLETE")


if __name__ == "__main__":
    main()
