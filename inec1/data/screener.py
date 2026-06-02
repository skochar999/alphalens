"""
Screener.in scraper for IEC-1 — 5-year fundamental history.

Screener.in provides 10-year P&L, Balance Sheet, and Cash Flow data
for all NSE-listed companies, free without login (5-year view).

Descriptor coverage
-------------------
    dividend_payout_5y    — from P&L table "Dividend Payout %"
    cv_earnings_5y        — CoV of Net Profit over 5 years
    cv_cashflows_5y       — CoV of Operating Cash Flow over 5 years
    eps_growth_5y         — CAGR of EPS over available years
    asset_growth_5y       — CAGR of Total Assets over available years
    eps_change_1y         — Year-on-year EPS change
    delta_equity, delta_lt_debt, book_total_5y  — from Balance Sheet
    avg_ep_5y             — average E/P over 5 years (needs current price)

Usage
-----
    from inec1.data.screener import ScreenerSource
    src = ScreenerSource(cache_dir="./inec1_cache")
    row = src.fetch_one("RELIANCE", current_price=2900.0)

Rate limiting
-------------
Default 1.5s delay between requests. Screener.in is a free service;
please be respectful and avoid running large batches in rapid succession.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np
import pandas as pd

from .cache import FileCache, make_key
from .universe import strip_suffix

logger = logging.getLogger(__name__)

BASE_URL     = "https://www.screener.in/company/{ticker}/consolidated/"
FALLBACK_URL = "https://www.screener.in/company/{ticker}/"
FETCH_DELAY  = 1.5   # seconds between requests

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.screener.in/",
}


class ScreenerSource:
    """
    Scrape 5-year fundamental history from Screener.in.

    Parameters
    ----------
    cache_dir    : str    path for file cache (None = disable)
    delay        : float  seconds between HTTP requests
    prefer_consolidated : bool
        If True (default), prefer consolidated financials; fall back to standalone.
    """

    def __init__(
        self,
        cache_dir: str = "./inec1_cache",
        delay: float = FETCH_DELAY,
        prefer_consolidated: bool = True,
    ) -> None:
        try:
            import requests
            from bs4 import BeautifulSoup
            self._requests = requests
            self._bs4 = BeautifulSoup
        except ImportError:
            raise ImportError(
                "requests and beautifulsoup4 are required. "
                "Run: pip install requests beautifulsoup4"
            )

        self.cache = FileCache(cache_dir) if cache_dir else None
        self.delay = delay
        self.prefer_consolidated = prefer_consolidated

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_universe(
        self,
        tickers: list[str],
        prices: Optional[dict[str, float]] = None,
        verbose: bool = True,
    ) -> pd.DataFrame:
        """
        Fetch Screener data for a universe of tickers.

        Parameters
        ----------
        tickers : list[str]   NSE symbols (with or without .NS suffix)
        prices  : dict        {ticker: current_price} for E/P computation
        verbose : bool        log progress

        Returns
        -------
        pd.DataFrame indexed by ticker
        """
        prices = prices or {}
        rows = {}
        n = len(tickers)
        for i, ticker in enumerate(tickers):
            nse_sym = strip_suffix(ticker)
            if verbose and (i % 20 == 0 or i == n - 1):
                logger.info(f"  Screener: {i+1}/{n} — {nse_sym}")
            price = prices.get(ticker) or prices.get(nse_sym)
            try:
                rows[ticker] = self.fetch_one(nse_sym, current_price=price)
            except Exception as e:
                logger.warning(f"  Screener failed {nse_sym}: {e}")
                rows[ticker] = {}
            time.sleep(self.delay)

        return pd.DataFrame.from_dict(rows, orient="index")

    def fetch_one(
        self,
        nse_symbol: str,
        current_price: Optional[float] = None,
    ) -> dict:
        """
        Fetch Screener.in fundamental history for one NSE symbol.

        Parameters
        ----------
        nse_symbol    : str   bare NSE ticker without .NS (e.g. 'RELIANCE')
        current_price : float current share price (used for avg_ep_5y)

        Returns
        -------
        dict of descriptor → value
        """
        nse_symbol = strip_suffix(nse_symbol).upper()
        cache_key  = make_key("screener", nse_symbol)

        if self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        html = self._fetch_html(nse_symbol)
        if html is None:
            return {}

        result = self._parse(html, current_price)

        if self.cache and result:
            self.cache.set(cache_key, result)

        return result

    # ------------------------------------------------------------------
    # HTML fetch
    # ------------------------------------------------------------------

    def _fetch_html(self, nse_symbol: str) -> Optional[str]:
        urls = (
            [BASE_URL.format(ticker=nse_symbol), FALLBACK_URL.format(ticker=nse_symbol)]
            if self.prefer_consolidated
            else [FALLBACK_URL.format(ticker=nse_symbol)]
        )
        for url in urls:
            try:
                resp = self._requests.get(url, headers=_HEADERS, timeout=15)
                if resp.status_code == 200:
                    return resp.text
                elif resp.status_code == 404:
                    continue
                else:
                    logger.debug(f"  Screener {nse_symbol}: HTTP {resp.status_code}")
            except Exception as e:
                logger.debug(f"  Screener {nse_symbol} request error: {e}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse(self, html: str, current_price: Optional[float]) -> dict:
        bs = self._bs4(html, "html.parser")
        result: dict = {}

        pl   = self._parse_section(bs, "profit-loss")
        bsht = self._parse_section(bs, "balance-sheet")
        cf   = self._parse_section(bs, "cash-flow")

        # --- P&L metrics ---
        if pl is not None:
            result.update(self._pl_descriptors(pl, current_price))

        # --- Balance sheet metrics ---
        if bsht is not None:
            result.update(self._bs_descriptors(bsht))

        # --- Cash flow ---
        if cf is not None:
            result.update(self._cf_descriptors(cf, pl))

        return result

    def _parse_section(self, bs, section_id: str) -> Optional[pd.DataFrame]:
        """
        Parse an HTML table from a named section on Screener.in.
        Returns a DataFrame with row labels as index and years as columns.
        """
        section = bs.find("section", {"id": section_id})
        if section is None:
            return None
        table = section.find("table")
        if table is None:
            return None

        try:
            # Extract header row (years)
            thead = table.find("thead")
            if thead:
                header_cells = thead.find_all("th")
                cols = [c.get_text(strip=True) for c in header_cells]
            else:
                cols = None

            rows = []
            for tr in table.find("tbody").find_all("tr"):
                cells = tr.find_all(["td", "th"])
                if cells:
                    rows.append([c.get_text(strip=True) for c in cells])

            if not rows:
                return None

            df = pd.DataFrame(rows)
            if cols and len(cols) == df.shape[1]:
                df.columns = cols

            df = df.set_index(df.columns[0])
            df.index = df.index.str.strip()

            # Convert numeric strings (remove commas, handle negatives)
            for col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].astype(str)
                    .str.replace(",", "")
                    .str.replace("%", "")
                    .str.strip(),
                    errors="coerce",
                )
            return df

        except Exception as e:
            logger.debug(f"  Screener table parse error ({section_id}): {e}")
            return None

    # ------------------------------------------------------------------
    # Descriptor computation from parsed tables
    # ------------------------------------------------------------------

    def _pl_descriptors(self, pl: pd.DataFrame, price: Optional[float]) -> dict:
        out: dict = {}

        # Helper: find a row by partial name match, return numeric series
        def find_row(candidates: list[str]) -> Optional[pd.Series]:
            for cand in candidates:
                matches = [r for r in pl.index if cand.lower() in r.lower()]
                if matches:
                    return pd.to_numeric(pl.loc[matches[0]], errors="coerce").dropna()
            return None

        # Net Profit
        profit = find_row(["Net Profit", "PAT", "Profit after tax"])
        if profit is not None and len(profit) >= 2:
            vals = profit.values.astype(float)
            mean_p = np.nanmean(vals)
            std_p  = np.nanstd(vals, ddof=1) if len(vals) > 1 else np.nan
            out["cv_earnings_5y"] = float(abs(std_p / mean_p)) if mean_p != 0 else np.nan

        # EPS
        eps = find_row(["EPS", "Earnings per share", "Basic EPS"])
        if eps is not None and len(eps) >= 2:
            eps_vals = eps.values.astype(float)
            # Growth CAGR
            if eps_vals[-1] > 0 and eps_vals[0] > 0:
                n = len(eps_vals) - 1
                out["eps_growth_5y"] = float((eps_vals[0] / eps_vals[-1]) ** (1 / n) - 1)
            # YoY change
            if len(eps_vals) >= 2 and eps_vals[1] != 0:
                out["eps_change_1y"] = float((eps_vals[0] - eps_vals[1]) / abs(eps_vals[1]))
            # avg E/P
            if price and price > 0:
                ep_vals = eps_vals / price
                out["avg_ep_5y"] = float(np.nanmean(ep_vals))

        # Dividend Payout
        div = find_row(["Dividend Payout", "Payout"])
        if div is not None and len(div) >= 1:
            # Screener shows as percentage (e.g. 25 = 25%)
            out["dividend_payout_5y"] = float(np.nanmean(div.values) / 100)

        return out

    def _bs_descriptors(self, bsht: pd.DataFrame) -> dict:
        out: dict = {}

        def find_row(candidates: list[str]) -> Optional[pd.Series]:
            for cand in candidates:
                matches = [r for r in bsht.index if cand.lower() in r.lower()]
                if matches:
                    return pd.to_numeric(bsht.loc[matches[0]], errors="coerce").dropna()
            return None

        # Total Assets
        assets = find_row(["Total Assets", "Balance Sheet Total"])
        if assets is not None and len(assets) >= 2:
            vals = assets.values.astype(float)
            out["book_total_5y"]  = float(np.nanmean(vals))
            n = len(vals) - 1
            if vals[-1] > 0 and vals[0] > 0:
                out["asset_growth_5y"] = float((vals[0] / vals[-1]) ** (1 / n) - 1)

        # Equity (Reserves + Share Capital)
        reserves = find_row(["Reserves", "Reserves & Surplus"])
        share_cap = find_row(["Share Capital", "Equity Share Capital"])
        if reserves is not None and share_cap is not None:
            eq = (reserves + share_cap).dropna()
            if len(eq) >= 2:
                out["delta_equity"] = float(eq.iloc[0] - eq.iloc[-1])

        # Long-term Borrowings / Debt
        lt_borrow = find_row(["Borrowings", "Long Term Borrowings", "Long-term Debt"])
        if lt_borrow is not None and len(lt_borrow) >= 2:
            out["delta_lt_debt"] = float(lt_borrow.iloc[0] - lt_borrow.iloc[-1])

        out.setdefault("delta_preferred", 0.0)

        return out

    def _cf_descriptors(
        self, cf: pd.DataFrame, pl: Optional[pd.DataFrame]
    ) -> dict:
        out: dict = {}

        def find_row(table: pd.DataFrame, candidates: list[str]) -> Optional[pd.Series]:
            for cand in candidates:
                matches = [r for r in table.index if cand.lower() in r.lower()]
                if matches:
                    return pd.to_numeric(table.loc[matches[0]], errors="coerce").dropna()
            return None

        op_cf = find_row(cf, ["Cash from Operations", "Operating Activities", "CFO"])
        if op_cf is not None and len(op_cf) >= 2:
            vals = op_cf.values.astype(float)
            mean_cf = np.nanmean(vals)
            std_cf  = np.nanstd(vals, ddof=1) if len(vals) > 1 else np.nan
            out["cv_cashflows_5y"] = float(abs(std_cf / mean_cf)) if mean_cf != 0 else np.nan

        return out
