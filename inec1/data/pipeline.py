"""
DataPipeline — orchestrates yfinance + Screener.in into the REQUIRED_COLUMNS schema.

This is the single entry point for real data ingestion. It:
  1. Fetches price/fundamental data from yfinance
  2. Fetches 5-year history from Screener.in
  3. Merges both, preferring Screener for metrics it covers more reliably
  4. Maps industry labels to IEC-1 INDUSTRY_FACTORS
  5. Reports descriptor coverage so you know what's populated vs NaN

Usage
-----
    from inec1.data.pipeline import DataPipeline
    from inec1.data.universe import get_nifty100

    pipeline = DataPipeline(cache_dir="./inec1_cache")
    df = pipeline.build(get_nifty100())     # → pd.DataFrame, REQUIRED_COLUMNS schema
    print(pipeline.coverage_report(df))     # descriptor fill rates
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ..exposures import REQUIRED_COLUMNS
from ..config import INDUSTRY_FACTORS
from .cache import FileCache
from .screener import ScreenerSource
from .universe import strip_suffix
from .yf_source import YFinanceSource

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GICS → IEC-1 industry mapping
# yfinance returns GICS sector strings; we map to IEC-1 industry labels.
# ---------------------------------------------------------------------------

_SECTOR_MAP: dict[str, str] = {
    # yfinance 'sector' → IEC-1 industry
    "Technology":                    "ITSERVIC",
    "Consumer Cyclical":             "CONSUMDISC",
    "Financial Services":            "FINLSERV",
    "Healthcare":                    "HEALTHCARE",
    "Industrials":                   "CAPGOODS",
    "Consumer Defensive":            "CONSUMSTAP",
    "Energy":                        "OILGAS",
    "Basic Materials":               "CHEMICALS",
    "Real Estate":                   "REALESTATE",
    "Communication Services":        "TELECOM",
    "Utilities":                     "UTILITIES",
}

_INDUSTRY_MAP: dict[str, str] = {
    # yfinance 'industry' (more granular) → IEC-1 label
    # Banks
    "Banks—Diversified":             "BANKS",
    "Banks—Regional":                "BANKS",
    "Mortgage Finance":              "BANKS",
    # Financial services (non-bank)
    "Asset Management":              "FINLSERV",
    "Capital Markets":               "FINLSERV",
    "Insurance—Life":                "FINLSERV",
    "Insurance—Diversified":         "FINLSERV",
    "Insurance—Property & Casualty": "FINLSERV",
    "Credit Services":               "FINLSERV",
    "Financial Conglomerates":       "FINLSERV",
    # IT services vs software
    "Information Technology Services": "ITSERVIC",
    "Software—Application":          "SOFTWARE",
    "Software—Infrastructure":       "SOFTWARE",
    # Tech hardware
    "Semiconductors":                "TECHCOMP",
    "Electronic Components":         "TECHCOMP",
    "Computer Hardware":             "TECHCOMP",
    "Communication Equipment":       "TECHCOMP",
    # Pharma vs healthcare
    "Drug Manufacturers—General":    "PHARMA",
    "Drug Manufacturers—Specialty":  "PHARMA",
    "Biotechnology":                 "PHARMA",
    "Medical Devices":               "HEALTHCARE",
    "Medical Care Facilities":       "HEALTHCARE",
    "Health Information Services":   "HEALTHCARE",
    # Auto
    "Auto Manufacturers":            "AUTOMOBL",
    "Auto Parts":                    "AUTOCOMP",
    "Auto & Truck Dealerships":      "AUTOCOMP",
    # Materials
    "Steel":                         "METMINING",
    "Industrial Metals & Mining":    "METMINING",
    "Copper":                        "METMINING",
    "Aluminum":                      "METMINING",
    "Chemicals":                     "CHEMICALS",
    "Specialty Chemicals":           "CHEMICALS",
    "Agricultural Inputs":           "CHEMICALS",
    "Building Materials":            "MATERIALS",
    "Paper & Paper Products":        "MATERIALS",
    "Packaging & Containers":        "MATERIALS",
    # Energy
    "Oil & Gas E&P":                 "OILGAS",
    "Oil & Gas Refining & Marketing":"OILGAS",
    "Oil & Gas Integrated":          "OILGAS",
    "Oil & Gas Equipment & Services":"OILGAS",
    # Power
    "Utilities—Regulated Electric":  "UTILITIES",
    "Utilities—Regulated Gas":       "UTILITIES",
    "Utilities—Diversified":         "UTILITIES",
    "Utilities—Independent Power":   "POWERGEN",
    "Solar":                         "POWERGEN",
    "Renewable Utilities":           "POWERGEN",
    # Real estate
    "Real Estate—Development":       "REALESTATE",
    "Real Estate Services":          "REALESTATE",
    "REIT—Diversified":              "REALESTATE",
    # Transport
    "Airlines":                      "TRANSPORT",
    "Trucking":                      "TRANSPORT",
    "Railroads":                     "TRANSPORT",
    "Marine Shipping":               "TRANSPORT",
    "Air Freight & Logistics":       "TRANSPORT",
    "Airports & Air Services":       "TRANSPORT",
    # Telecom
    "Telecom Services":              "TELECOM",
    "Wireless Telecommunications":   "TELECOM",
    # Consumer staples
    "Beverages—Non-Alcoholic":       "CONSUMSTAP",
    "Beverages—Alcoholic":           "CONSUMSTAP",
    "Food Distribution":             "CONSUMSTAP",
    "Packaged Foods":                "CONSUMSTAP",
    "Household & Personal Products": "CONSUMSTAP",
    "Tobacco":                       "CONSUMSTAP",
    # Consumer discretionary
    "Specialty Retail":              "CONSUMDISC",
    "Apparel Retail":                "CONSUMDISC",
    "Department Stores":             "CONSUMDISC",
    "Restaurants":                   "CONSUMDISC",
    "Leisure":                       "CONSUMDISC",
    "Gambling":                      "CONSUMDISC",
    "Entertainment":                 "CONSUMDISC",
    # Household durables
    "Appliances":                    "CONSUMDUR",
    "Household Durables":            "CONSUMDUR",
    "Furnishings":                   "CONSUMDUR",
    # Capital goods / industrials
    "Electrical Equipment & Parts":  "CAPGOODS",
    "Industrial Distribution":       "TRADDIST",
    "Farm & Heavy Construction Machinery": "CAPGOODS",
    "Specialty Industrial Machinery":"CAPGOODS",
    "Engineering & Construction":    "CAPGOODS",
    "Conglomerates":                 "CAPGOODS",
    # Business services
    "Staffing & Employment Services":"BUSINSERV",
    "Business Services":             "BUSINSERV",
    "Consulting Services":           "BUSINSERV",
    # Aerospace & defence
    "Aerospace & Defense":           "AERODEF",
}


def map_industry(yf_sector: Optional[str], yf_industry: Optional[str]) -> str:
    """Map yfinance sector/industry strings to an IEC-1 industry label."""
    if yf_industry and yf_industry in _INDUSTRY_MAP:
        return _INDUSTRY_MAP[yf_industry]
    if yf_sector and yf_sector in _SECTOR_MAP:
        return _SECTOR_MAP[yf_sector]
    return "MISCELLAN"


# ---------------------------------------------------------------------------
# DataPipeline
# ---------------------------------------------------------------------------

class DataPipeline:
    """
    Fetch and merge data from yfinance + Screener.in into REQUIRED_COLUMNS format.

    Parameters
    ----------
    cache_dir          : str    cache directory path
    yf_delay           : float  seconds between yfinance fetches
    screener_delay     : float  seconds between Screener.in fetches
    use_screener       : bool   set False to skip Screener (yfinance only)
    """

    def __init__(
        self,
        cache_dir: str = "./inec1_cache",
        yf_delay: float = 0.5,
        screener_delay: float = 1.5,
        use_screener: bool = True,
    ) -> None:
        self.yf_source  = YFinanceSource(cache_dir=cache_dir, delay=yf_delay)
        self.scr_source = ScreenerSource(cache_dir=cache_dir, delay=screener_delay) if use_screener else None
        self.use_screener = use_screener

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(
        self,
        tickers: list[str],
        verbose: bool = True,
    ) -> pd.DataFrame:
        """
        Fetch all data and return a cross-sectional DataFrame ready for
        ExposureBuilder.build().

        Parameters
        ----------
        tickers : list[str]   NSE tickers (with or without .NS suffix)
        verbose : bool        log progress

        Returns
        -------
        pd.DataFrame, shape (N, len(REQUIRED_COLUMNS))
            Index = ticker (with .NS suffix), columns = REQUIRED_COLUMNS.
            NaN where data is unavailable.
        """
        # Normalise tickers
        tickers = self._normalise(tickers)

        if verbose:
            logger.info(f"DataPipeline: fetching {len(tickers)} tickers")
            logger.info("Step 1/3: yfinance ...")

        # --- Step 1: yfinance ---
        yf_df = self.yf_source.fetch_universe(tickers, verbose=verbose)

        # --- Step 2: industry labels (from yfinance info) ---
        if verbose:
            logger.info("Step 2/3: industry classification ...")
        yf_df["industry"] = self._fetch_industries(tickers, yf_df)

        # --- Step 3: Screener.in ---
        scr_df = None
        if self.use_screener and self.scr_source is not None:
            if verbose:
                logger.info("Step 3/3: Screener.in ...")
            prices = yf_df.get("market_cap", pd.Series()).div(
                yf_df.get("mv_equity", pd.Series()).clip(lower=1)
            ).to_dict()  # rough price proxy; overridden below
            # Use log_price → price
            prices = {
                t: float(np.exp(yf_df.loc[t, "log_price"]))
                for t in tickers
                if t in yf_df.index and not np.isnan(yf_df.loc[t, "log_price"])
                if "log_price" in yf_df.columns
            }
            scr_df = self.scr_source.fetch_universe(
                tickers, prices=prices, verbose=verbose
            )

        # --- Merge ---
        merged = self._merge(yf_df, scr_df, tickers)

        # --- Reindex to canonical REQUIRED_COLUMNS (excluding 'industry' which is str) ---
        numeric_cols = [c for c in REQUIRED_COLUMNS if c != "industry"]
        for col in numeric_cols:
            if col not in merged.columns:
                merged[col] = np.nan

        merged["industry"] = merged["industry"].fillna("MISCELLAN")

        return merged[REQUIRED_COLUMNS]

    def coverage_report(self, df: pd.DataFrame) -> str:
        """
        Return a formatted string showing fill rate per descriptor.
        Useful for identifying which data gaps remain.
        """
        numeric_cols = [c for c in REQUIRED_COLUMNS if c != "industry"]
        lines = ["", "=" * 52, "  IEC-1 Descriptor Coverage Report", "=" * 52]
        lines.append(f"  Universe: {len(df)} securities\n")

        lines.append(f"  {'Descriptor':<28} {'Filled':>7} {'Fill %':>8}")
        lines.append("  " + "-" * 46)

        fully_covered   = []
        partially       = []
        mostly_missing  = []

        for col in numeric_cols:
            if col not in df.columns:
                pct = 0.0
                n   = 0
            else:
                n   = int(df[col].notna().sum())
                pct = n / len(df) * 100

            lines.append(f"  {col:<28} {n:>7} {pct:>7.1f}%")

            if pct >= 80:
                fully_covered.append(col)
            elif pct >= 30:
                partially.append(col)
            else:
                mostly_missing.append(col)

        lines += [
            "",
            f"  ✓ Well-covered  (≥80%): {len(fully_covered)} descriptors",
            f"  ~ Partial (30-80%):     {len(partially)} descriptors",
            f"  ✗ Sparse   (<30%):      {len(mostly_missing)} descriptors",
        ]
        if mostly_missing:
            lines.append(f"    → {', '.join(mostly_missing)}")
        lines.append("=" * 52)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalise(self, tickers: list[str]) -> list[str]:
        result = []
        for t in tickers:
            t = t.strip().upper()
            if not t.endswith(".NS"):
                t += ".NS"
            result.append(t)
        return result

    def _fetch_industries(
        self, tickers: list[str], yf_df: pd.DataFrame
    ) -> pd.Series:
        """
        Fetch sector/industry strings from yfinance .info and map to IEC-1 labels.
        This is done as a fast batch after the main yf fetch (most .info already cached).
        """
        try:
            import yfinance as yf
        except ImportError:
            return pd.Series("MISCELLAN", index=tickers)

        labels = {}
        for ticker in tickers:
            try:
                info = yf.Ticker(ticker).info
                sector   = info.get("sector")
                industry = info.get("industry")
                labels[ticker] = map_industry(sector, industry)
            except Exception:
                labels[ticker] = "MISCELLAN"

        return pd.Series(labels)

    def _merge(
        self,
        yf_df: pd.DataFrame,
        scr_df: Optional[pd.DataFrame],
        tickers: list[str],
    ) -> pd.DataFrame:
        """
        Merge yfinance and Screener data.
        Screener values take precedence for descriptors it covers reliably.
        """
        merged = yf_df.reindex(tickers)

        if scr_df is not None:
            # Screener-preferred descriptors
            SCREENER_PREFERRED = [
                "dividend_payout_5y",
                "cv_earnings_5y",
                "cv_cashflows_5y",
                "eps_growth_5y",
                "asset_growth_5y",
                "eps_change_1y",
                "avg_ep_5y",
                "delta_equity",
                "delta_lt_debt",
                "delta_preferred",
                "book_total_5y",
            ]
            scr_aligned = scr_df.reindex(tickers)
            for col in SCREENER_PREFERRED:
                if col in scr_aligned.columns:
                    # Use Screener value where available; fall back to yf
                    yf_vals  = merged.get(col, pd.Series(np.nan, index=tickers))
                    scr_vals = scr_aligned[col]
                    merged[col] = scr_vals.combine_first(yf_vals)

        return merged
