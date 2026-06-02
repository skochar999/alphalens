"""
yfinance data adapter for IEC-1.

Covers ~80% of REQUIRED_COLUMNS from two sources:
  - yfinance price history  → returns, vol, beta, liquidity, price-level descriptors
  - yfinance .info / financials → market cap, fundamentals, valuation

Descriptor coverage map
-----------------------
FULLY COVERED (from yfinance):
    market_cap, mv_equity, log_price, ret_1m, ret_3m, ret_12m,
    daily_vol_3m, hl_ratio_1m, monthly_range_1y, capm_beta, residual_vol,
    vol_sensitivity, share_turnover_1y, share_turnover_q, share_turnover_1m,
    vol_to_var, book_to_price, trailing_ep, sales, cogs, earnings,
    total_assets, lt_debt, st_debt, book_equity, mv_preferred, book_preferred,
    eps_change_1y, avg_ep_5y (approximated from 4yr history)

PARTIALLY COVERED (yfinance, quality varies):
    asset_growth_5y, eps_growth_5y  (4yr history used; 5yr from Screener preferred)
    delta_equity, delta_lt_debt, delta_preferred, book_total_5y

NOT COVERED (sourced from Screener.in):
    dividend_payout_5y, cv_earnings_5y, cv_cashflows_5y

Usage
-----
    from inec1.data.yf_source import YFinanceSource
    src = YFinanceSource(cache_dir="./inec1_cache")
    df = src.fetch_universe(tickers)   # pd.DataFrame indexed by ticker
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

MARKET_TICKER = "^NSEI"          # Nifty 50 — market proxy for beta
HISTORY_PERIOD = "2y"            # 2-year price history for all return/vol metrics
FETCH_DELAY    = 0.5             # seconds between ticker fetches (rate limiting)


class YFinanceSource:
    """
    Fetch all yfinance-available IEC-1 descriptors for a universe of NSE stocks.

    Parameters
    ----------
    cache_dir : str
        Directory for file cache. Set to None to disable caching.
    delay : float
        Seconds to wait between individual ticker fetches (rate limiting).
    market_ticker : str
        Index ticker used as the market portfolio for CAPM beta.
    """

    def __init__(
        self,
        cache_dir: str = "./inec1_cache",
        delay: float = FETCH_DELAY,
        market_ticker: str = MARKET_TICKER,
    ) -> None:
        try:
            import yfinance as yf
            self._yf = yf
        except ImportError:
            raise ImportError("yfinance not installed. Run: pip install yfinance")

        self.cache = FileCache(cache_dir) if cache_dir else None
        self.delay = delay
        self.market_ticker = market_ticker
        self._market_hist: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_universe(
        self,
        tickers: list[str],
        verbose: bool = True,
    ) -> pd.DataFrame:
        """
        Fetch descriptors for all tickers. Returns DataFrame indexed by ticker.

        Parameters
        ----------
        tickers : list[str]   NSE tickers with .NS suffix
        verbose : bool        log progress

        Returns
        -------
        pd.DataFrame  (N × descriptors) — NaN where data unavailable
        """
        # Fetch market index once for beta computation
        self._market_hist = self._fetch_market()

        rows = {}
        n = len(tickers)
        for i, ticker in enumerate(tickers):
            if verbose and (i % 25 == 0 or i == n - 1):
                logger.info(f"  yfinance: {i+1}/{n} — {ticker}")
            try:
                rows[ticker] = self._fetch_one(ticker)
            except Exception as e:
                logger.warning(f"  Failed {ticker}: {e}")
                rows[ticker] = {}
            time.sleep(self.delay)

        df = pd.DataFrame.from_dict(rows, orient="index")
        df.index.name = "ticker"
        return df

    def fetch_one(self, ticker: str) -> dict:
        """Fetch descriptors for a single ticker. Returns dict of descriptor → value."""
        if self._market_hist is None:
            self._market_hist = self._fetch_market()
        return self._fetch_one(ticker)

    # ------------------------------------------------------------------
    # Private — single ticker fetch
    # ------------------------------------------------------------------

    def _fetch_one(self, ticker: str) -> dict:
        cache_key = make_key("yf", ticker)
        if self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        result = {}
        try:
            t = self._yf.Ticker(ticker)
            hist = t.history(period=HISTORY_PERIOD, auto_adjust=True)

            if hist.empty or len(hist) < 20:
                logger.debug(f"{ticker}: insufficient price history")
                return result

            # --- Price-based descriptors ---
            result.update(self._price_descriptors(hist))

            # --- CAPM beta + residual vol ---
            result.update(self._capm_descriptors(ticker, hist))

            # --- Liquidity descriptors ---
            info = self._safe_info(t)
            shares_out = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
            result.update(self._liquidity_descriptors(hist, shares_out))

            # --- Fundamental descriptors ---
            result.update(self._fundamental_descriptors(t, info, hist))

        except Exception as e:
            logger.debug(f"{ticker} fetch error: {e}")

        if self.cache and result:
            self.cache.set(cache_key, result)

        return result

    # ------------------------------------------------------------------
    # Descriptor builders
    # ------------------------------------------------------------------

    def _price_descriptors(self, hist: pd.DataFrame) -> dict:
        close = hist["Close"].dropna()
        if len(close) < 5:
            return {}

        price_now = float(close.iloc[-1])

        # Returns
        def cum_ret(days: int) -> float:
            if len(close) < days:
                return np.nan
            return float(close.iloc[-1] / close.iloc[-days] - 1)

        ret_1m  = cum_ret(21)
        ret_3m  = cum_ret(63)
        ret_12m = cum_ret(252)

        # Daily vol (3 months)
        daily_rets = close.pct_change().dropna()
        daily_vol_3m = float(daily_rets.iloc[-63:].std() * np.sqrt(252)) if len(daily_rets) >= 20 else np.nan

        # High/low ratio — last month
        last_month = hist.tail(21)
        hl_ratio_1m = float(last_month["High"].max() / last_month["Low"].min()) if not last_month.empty else np.nan

        # Monthly range (1 year) — sum of monthly H-L ranges normalised by close
        monthly = hist.resample("ME").agg({"High": "max", "Low": "min", "Close": "last"})
        monthly_range_1y = float(
            ((monthly["High"] - monthly["Low"]) / monthly["Close"]).tail(12).sum()
        ) if len(monthly) >= 6 else np.nan

        return {
            "ret_1m":           ret_1m,
            "ret_3m":           ret_3m,
            "ret_12m":          ret_12m,
            "log_price":        float(np.log(price_now)) if price_now > 0 else np.nan,
            "daily_vol_3m":     daily_vol_3m,
            "hl_ratio_1m":      hl_ratio_1m,
            "monthly_range_1y": monthly_range_1y,
        }

    def _capm_descriptors(self, ticker: str, hist: pd.DataFrame) -> dict:
        if self._market_hist is None or self._market_hist.empty:
            return {"capm_beta": np.nan, "residual_vol": np.nan, "vol_sensitivity": np.nan}

        stock_ret = hist["Close"].pct_change().dropna()
        mkt_ret   = self._market_hist["Close"].pct_change().dropna()

        # Align dates
        common = stock_ret.index.intersection(mkt_ret.index)
        if len(common) < 40:
            return {"capm_beta": np.nan, "residual_vol": np.nan, "vol_sensitivity": np.nan}

        s = stock_ret.loc[common].values
        m = mkt_ret.loc[common].values

        # OLS: s = alpha + beta * m + epsilon
        X = np.column_stack([np.ones(len(m)), m])
        try:
            coeffs, resids, _, _ = np.linalg.lstsq(X, s, rcond=None)
        except Exception:
            return {"capm_beta": np.nan, "residual_vol": np.nan, "vol_sensitivity": np.nan}

        beta = float(coeffs[1])
        if len(resids) == 0:
            eps = s - X @ coeffs
        else:
            eps = s - X @ coeffs
        residual_vol = float(np.std(eps, ddof=2) * np.sqrt(252))

        # Volume beta — sensitivity of stock volume to market volume
        vol_sensitivity = np.nan
        if "Volume" in hist.columns and "Volume" in self._market_hist.columns:
            sv = np.log1p(hist["Volume"].reindex(common).fillna(0).values.astype(float))
            mv = np.log1p(self._market_hist["Volume"].reindex(common).fillna(0).values.astype(float))
            if mv.std() > 0:
                vol_sensitivity = float(np.cov(sv, mv)[0, 1] / np.var(mv))

        return {
            "capm_beta":     beta,
            "residual_vol":  residual_vol,
            "vol_sensitivity": vol_sensitivity,
        }

    def _liquidity_descriptors(self, hist: pd.DataFrame, shares_out: Optional[float]) -> dict:
        if "Volume" not in hist.columns or shares_out is None or shares_out <= 0:
            return {
                "share_turnover_1y": np.nan,
                "share_turnover_q":  np.nan,
                "share_turnover_1m": np.nan,
                "vol_to_var":        np.nan,
            }

        vol = hist["Volume"].fillna(0)
        daily_ret = hist["Close"].pct_change().dropna()

        def turnover(days: int, annualise_factor: float) -> float:
            v = vol.iloc[-days:].sum()
            return float(v / shares_out * annualise_factor) if shares_out > 0 else np.nan

        # vol-to-variance: mean daily volume / variance of daily returns
        vol_to_var = np.nan
        if len(daily_ret) >= 20:
            ret_var = float(daily_ret.var())
            if ret_var > 1e-10:
                vol_to_var = float(vol.iloc[-len(daily_ret):].mean() / ret_var)

        return {
            "share_turnover_1y": turnover(252, 1.0),
            "share_turnover_q":  turnover(63,  4.0),
            "share_turnover_1m": turnover(21,  12.0),
            "vol_to_var":        vol_to_var,
        }

    def _fundamental_descriptors(
        self, t, info: dict, hist: pd.DataFrame
    ) -> dict:
        desc: dict = {}

        price = float(hist["Close"].iloc[-1]) if not hist.empty else np.nan

        # --- Market cap & equity ---
        mktcap = info.get("marketCap") or np.nan
        desc["market_cap"] = mktcap
        desc["mv_equity"]  = mktcap

        # --- Book-to-price ---
        book_per_share = info.get("bookValue") or np.nan
        shares = info.get("sharesOutstanding") or np.nan
        book_equity = (book_per_share * shares) if not (np.isnan(book_per_share) or np.isnan(shares)) else np.nan
        desc["book_equity"]   = book_equity
        desc["book_to_price"] = (book_per_share / price) if (book_per_share and price) else np.nan

        # Preferred equity — rarely reported for Indian companies, default 0
        desc["mv_preferred"]   = 0.0
        desc["book_preferred"] = 0.0

        # --- Earnings yield ---
        trailing_eps = info.get("trailingEps") or np.nan
        desc["trailing_ep"] = (trailing_eps / price) if (trailing_eps and price) else np.nan

        # --- Balance sheet items ---
        try:
            bs = t.balance_sheet
            if bs is not None and not bs.empty:
                desc.update(self._parse_balance_sheet(bs))
        except Exception:
            pass

        # --- P&L items ---
        try:
            fin = t.financials
            if fin is not None and not fin.empty:
                desc.update(self._parse_financials(fin, price))
        except Exception:
            pass

        return desc

    def _parse_balance_sheet(self, bs: pd.DataFrame) -> dict:
        """Extract descriptor values from yfinance balance sheet (columns = dates)."""
        out: dict = {}

        def latest(label_candidates: list[str]) -> float:
            for label in label_candidates:
                matches = [r for r in bs.index if label.lower() in r.lower()]
                if matches:
                    val = bs.loc[matches[0]].dropna()
                    if not val.empty:
                        return float(val.iloc[0])
            return np.nan

        def series_values(label_candidates: list[str], n: int = 4) -> list[float]:
            for label in label_candidates:
                matches = [r for r in bs.index if label.lower() in r.lower()]
                if matches:
                    vals = bs.loc[matches[0]].dropna().head(n)
                    return [float(v) for v in vals]
            return []

        lt_debt    = latest(["Long Term Debt", "Long-Term Debt", "LongTermDebt"])
        st_debt    = latest(["Short Term Debt", "Current Debt", "ShortTermDebt", "Short Long Term Debt"])
        total_assets = latest(["Total Assets", "TotalAssets"])

        out["lt_debt"]      = lt_debt
        out["st_debt"]      = st_debt
        out["total_assets"] = total_assets

        # Capital structure change (delta over available history)
        equity_hist = series_values(["Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"])
        debt_hist   = series_values(["Long Term Debt", "LongTermDebt"])
        if len(equity_hist) >= 2:
            out["delta_equity"]  = equity_hist[0] - equity_hist[-1]
        if len(debt_hist) >= 2:
            out["delta_lt_debt"] = debt_hist[0] - debt_hist[-1]
        out["delta_preferred"] = 0.0   # preferred rare in India

        # book_total_5y — average total assets across history
        assets_hist = series_values(["Total Assets", "TotalAssets"])
        if assets_hist:
            out["book_total_5y"] = float(np.mean(assets_hist))

        # asset_growth — CAGR across available years
        if len(assets_hist) >= 2:
            n_yrs = len(assets_hist) - 1
            if assets_hist[-1] > 0:
                out["asset_growth_5y"] = float((assets_hist[0] / assets_hist[-1]) ** (1 / n_yrs) - 1)

        return out

    def _parse_financials(self, fin: pd.DataFrame, price: float) -> dict:
        out: dict = {}

        def latest(candidates: list[str]) -> float:
            for label in candidates:
                matches = [r for r in fin.index if label.lower() in r.lower()]
                if matches:
                    vals = fin.loc[matches[0]].dropna()
                    if not vals.empty:
                        return float(vals.iloc[0])
            return np.nan

        def series_vals(candidates: list[str], n: int = 4) -> list[float]:
            for label in candidates:
                matches = [r for r in fin.index if label.lower() in r.lower()]
                if matches:
                    vals = fin.loc[matches[0]].dropna().head(n)
                    return [float(v) for v in vals]
            return []

        sales    = latest(["Total Revenue", "Revenue"])
        cogs     = latest(["Cost Of Revenue", "Cost of Goods Sold", "COGS"])
        earnings = latest(["Net Income", "Net Income Common Stockholders"])

        out["sales"]    = sales
        out["cogs"]     = cogs if not np.isnan(cogs) else sales * 0.6   # rough fallback
        out["earnings"] = earnings

        # EPS-based metrics
        eps_hist = series_vals(["Diluted EPS", "Basic EPS", "EPS"])
        if not eps_hist:
            # Derive from net income + shares (shares from balance sheet unavailable here)
            pass
        else:
            out["eps_change_1y"] = float((eps_hist[0] - eps_hist[1]) / abs(eps_hist[1])) \
                if len(eps_hist) >= 2 and eps_hist[1] != 0 else np.nan

            if len(eps_hist) >= 2:
                n_yrs = len(eps_hist) - 1
                if eps_hist[-1] != 0:
                    out["eps_growth_5y"] = float(
                        (eps_hist[0] / eps_hist[-1]) ** (1 / n_yrs) - 1
                    ) if eps_hist[-1] > 0 else np.nan

            # 5yr avg E/P
            if price > 0:
                ep_vals = [e / price for e in eps_hist if e and not np.isnan(e)]
                out["avg_ep_5y"] = float(np.mean(ep_vals)) if ep_vals else np.nan

        return out

    # ------------------------------------------------------------------
    # Market index
    # ------------------------------------------------------------------

    def _fetch_market(self) -> pd.DataFrame:
        cache_key = make_key("yf_market", self.market_ticker)
        if self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
        try:
            hist = self._yf.Ticker(self.market_ticker).history(
                period=HISTORY_PERIOD, auto_adjust=True
            )
            logger.info(f"  Fetched market index {self.market_ticker}: {len(hist)} days")
            if self.cache:
                self.cache.set(cache_key, hist)
            return hist
        except Exception as e:
            logger.warning(f"  Could not fetch market index {self.market_ticker}: {e}")
            return pd.DataFrame()

    def _safe_info(self, ticker_obj) -> dict:
        try:
            return ticker_obj.info or {}
        except Exception:
            return {}
