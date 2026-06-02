"""
ExposureBuilder — computes the 39-factor loading matrix for IEC-1.

Usage
-----
    builder = ExposureBuilder()
    B = builder.build(security_data)   # pd.DataFrame (N × 39)

Input contract
--------------
`security_data` is a pd.DataFrame indexed by security identifier with the
columns described in `REQUIRED_COLUMNS` below. All financial quantities
should be point-in-time (PIT) as-of the valuation date to avoid look-ahead
bias. Missing values are tolerated; the builder uses NaN-aware composites.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import (
    ALL_FACTORS,
    INDUSTRY_FACTORS,
    MARKET_FACTOR,
    STYLE_FACTORS,
)
from .utils import composite, standardize, signed_standardize, zscore


# ---------------------------------------------------------------------------
# Required input columns (document as reference for data providers)
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS: list[str] = [
    # --- BETA ---
    "capm_beta",            # Stock-level CAPM beta vs market portfolio

    # --- SIZE ---
    "market_cap",           # Market capitalisation (local currency, positive)

    # --- STREV ---
    "ret_3m",               # Cumulative return over trailing 3 months

    # --- LTMOM ---
    "ret_12m",              # Cumulative return over trailing 12 months
    "ret_1m",               # Cumulative return over trailing 1 month

    # --- VALUE ---
    "book_to_price",        # Book value of equity / market price per share

    # --- GROWTH (5 sub-descriptors) ---
    "dividend_payout_5y",   # 5-yr avg dividend payout ratio  [0,1]
    "delta_equity",         # Change in book equity (latest vs 5yr ago)
    "delta_lt_debt",        # Change in long-term debt
    "delta_preferred",      # Change in preferred equity
    "book_total_5y",        # Denominator for capital-structure change (avg book)
    "asset_growth_5y",      # 5-yr compound annual asset growth rate
    "eps_growth_5y",        # 5-yr compound annual EPS growth rate
    "eps_change_1y",        # Year-on-year change in EPS (latest vs prior year)

    # --- LOWVOL (6 sub-descriptors) ---
    "residual_vol",         # Annualised residual vol from CAPM regression
    "daily_vol_3m",         # Annualised daily return std dev (trailing 63 days)
    "hl_ratio_1m",          # High/low price ratio over trailing month
    "log_price",            # Natural log of share price
    "monthly_range_1y",     # Cumulative sum of monthly high/low ranges (1yr)
    "vol_sensitivity",      # Sensitivity of stock volume to market volume

    # --- LIQUIDITY (4 sub-descriptors) ---
    "share_turnover_1y",    # Shares traded / shares outstanding (1 year)
    "share_turnover_q",     # Same metric, last quarter (annualised)
    "share_turnover_1m",    # Same metric, last month
    "vol_to_var",           # Daily trading volume / daily return variance

    # --- LEVERAGE (3 sub-descriptors) ---
    "mv_equity",            # Market value of common equity
    "mv_preferred",         # Market value of preferred equity
    "lt_debt",              # Book value of long-term debt
    "book_equity",          # Book value of common equity
    "book_preferred",       # Book value of preferred equity
    "st_debt",              # Short-term debt (current portion of LT + revolver)
    "total_assets",         # Total assets (book)

    # --- EARNYIELD (2 sub-descriptors) ---
    "trailing_ep",          # Trailing 12-month EPS / price
    "avg_ep_5y",            # Avg(EPS over 5 yrs) / avg(quarter-end price over 5 yrs)

    # --- EARNVAR (2 sub-descriptors) ---
    "cv_earnings_5y",       # Coefficient of variation of earnings (5 yr)
    "cv_cashflows_5y",      # Coefficient of variation of CFO proxy (5 yr)

    # --- PROFIT (4 sub-descriptors) ---
    "sales",                # Net revenue / net sales
    "cogs",                 # Cost of goods sold
    "earnings",             # Net income / earnings available to common

    # --- INDUSTRY ---
    "industry",             # String: one of INDUSTRY_FACTORS
]


# ---------------------------------------------------------------------------
# ExposureBuilder
# ---------------------------------------------------------------------------

class ExposureBuilder:
    """
    Construct the (N × 39) IEC-1 factor exposure matrix for one cross-section.

    Each call to `.build()` processes a single-date universe snapshot.
    For time-series use, call `.build()` per date and stack results.

    Parameters
    ----------
    winsor_lower : float   lower winsorization percentile (default 0.01)
    winsor_upper : float   upper winsorization percentile (default 0.99)
    """

    def __init__(
        self,
        winsor_lower: float = 0.01,
        winsor_upper: float = 0.99,
    ) -> None:
        self._wl = winsor_lower
        self._wu = winsor_upper

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        Compute all 39 factor exposures for the provided cross-section.

        Parameters
        ----------
        data : pd.DataFrame
            Index   : security identifiers (str / int)
            Columns : see REQUIRED_COLUMNS

        Returns
        -------
        pd.DataFrame, shape (N, 39)
            Columns ordered as ALL_FACTORS:
            [MARKET, BETA, SIZE, ..., MISCELLAN]
        """
        exp = pd.DataFrame(index=data.index)

        # 1 — Market (unit exposure for every security)
        exp[MARKET_FACTOR] = 1.0

        # 2 — Style factors
        exp["BETA"]      = self._beta(data)
        exp["SIZE"]      = self._size(data)
        exp["STREV"]     = self._strev(data)
        exp["LTMOM"]     = self._ltmom(data)
        exp["VALUE"]     = self._value(data)
        exp["GROWTH"]    = self._growth(data)
        exp["LOWVOL"]    = self._lowvol(data)
        exp["LIQUIDITY"] = self._liquidity(data)
        exp["LEVERAGE"]  = self._leverage(data)
        exp["EARNYIELD"] = self._earnyield(data)
        exp["EARNVAR"]   = self._earnvar(data)
        exp["PROFIT"]    = self._profit(data)

        # 3 — Industry dummies (one-hot; sum-to-zero constraint applied in regression)
        industry_dummies = (
            pd.get_dummies(data["industry"])
            .reindex(columns=INDUSTRY_FACTORS, fill_value=0)
            .astype(float)
        )
        # Any security with an unknown industry label falls into MISCELLAN
        no_match = industry_dummies.sum(axis=1) == 0
        industry_dummies.loc[no_match, "MISCELLAN"] = 1.0

        exp = pd.concat([exp, industry_dummies], axis=1)

        return exp[ALL_FACTORS]  # enforce canonical column order

    # ------------------------------------------------------------------
    # Private style-factor builders
    # ------------------------------------------------------------------

    def _std(self, s: pd.Series) -> pd.Series:
        """Convenience: winsorize + z-score."""
        return standardize(s, self._wl, self._wu)

    def _beta(self, d: pd.DataFrame) -> pd.Series:
        return self._std(d["capm_beta"])

    def _size(self, d: pd.DataFrame) -> pd.Series:
        ln_cap = np.log(d["market_cap"].clip(lower=1e-6))
        return self._std(-ln_cap)  # negative: higher = smaller

    def _strev(self, d: pd.DataFrame) -> pd.Series:
        return self._std(-d["ret_3m"])  # negative: reversal

    def _ltmom(self, d: pd.DataFrame) -> pd.Series:
        # 12-month return excluding the most recent month
        ltmom = (1 + d["ret_12m"]) / (1 + d["ret_1m"].clip(lower=-0.999)) - 1
        return self._std(ltmom)

    def _value(self, d: pd.DataFrame) -> pd.Series:
        return self._std(d["book_to_price"])

    def _growth(self, d: pd.DataFrame) -> pd.Series:
        # Sub-descriptor 1: retention ratio (inverse of payout)
        retention = self._std(1.0 - d["dividend_payout_5y"].clip(0, 1))

        # Sub-descriptor 2: Δ capital structure scaled to book total
        delta_cap = d["delta_equity"] + d["delta_lt_debt"] + d["delta_preferred"]
        denom = d["book_total_5y"].abs().clip(lower=1e-6)
        delta_cap_scaled = self._std(delta_cap / denom)

        # Sub-descriptors 3-5
        asset_g  = self._std(d["asset_growth_5y"])
        eps_g5   = self._std(d["eps_growth_5y"])
        eps_c1   = self._std(d["eps_change_1y"])

        return composite(retention, delta_cap_scaled, asset_g, eps_g5, eps_c1)

    def _lowvol(self, d: pd.DataFrame) -> pd.Series:
        d1 = self._std(d["residual_vol"])
        d2 = self._std(d["daily_vol_3m"])
        d3 = self._std(d["hl_ratio_1m"])
        d4 = self._std(d["log_price"])
        d5 = self._std(d["monthly_range_1y"])
        d6 = self._std(d["vol_sensitivity"])
        return composite(d1, d2, d3, d4, d5, d6)

    def _liquidity(self, d: pd.DataFrame) -> pd.Series:
        d1 = self._std(d["share_turnover_1y"])
        d2 = self._std(d["share_turnover_q"])
        d3 = self._std(d["share_turnover_1m"])
        d4 = self._std(d["vol_to_var"])
        return composite(d1, d2, d3, d4)

    def _leverage(self, d: pd.DataFrame) -> pd.Series:
        mv_eq  = d["mv_equity"].clip(lower=1e-6)
        bk_eq  = d["book_equity"].clip(lower=1e-6)

        # Market leverage: (MV equity + MV preferred + LT debt) / MV equity
        mv_lev = (mv_eq + d["mv_preferred"].fillna(0) + d["lt_debt"].fillna(0)) / mv_eq

        # Book leverage: (Book equity + Book preferred + LT debt) / Book equity
        bk_lev = (bk_eq + d["book_preferred"].fillna(0) + d["lt_debt"].fillna(0)) / bk_eq

        # Debt-to-assets: (ST + LT debt) / Total assets
        d_to_a = (
            (d["st_debt"].fillna(0) + d["lt_debt"].fillna(0))
            / d["total_assets"].clip(lower=1e-6)
        )

        d1 = self._std(mv_lev)
        d2 = self._std(bk_lev)
        d3 = self._std(d_to_a)
        return composite(d1, d2, d3)

    def _earnyield(self, d: pd.DataFrame) -> pd.Series:
        d1 = self._std(d["trailing_ep"])
        d2 = self._std(d["avg_ep_5y"])
        return composite(d1, d2)

    def _earnvar(self, d: pd.DataFrame) -> pd.Series:
        d1 = self._std(d["cv_earnings_5y"])
        d2 = self._std(d["cv_cashflows_5y"])
        return composite(d1, d2)

    def _profit(self, d: pd.DataFrame) -> pd.Series:
        assets = d["total_assets"].clip(lower=1e-6)
        sales  = d["sales"].clip(lower=0)

        asset_turnover   = sales / assets
        gross_pft_assets = (sales - d["cogs"].clip(lower=0)) / assets
        gross_margin     = (sales - d["cogs"].clip(lower=0)) / sales.clip(lower=1e-6)
        roa              = d["earnings"] / assets

        d1 = self._std(asset_turnover)
        d2 = self._std(gross_pft_assets)
        d3 = self._std(gross_margin)
        d4 = self._std(roa)
        return composite(d1, d2, d3, d4)
