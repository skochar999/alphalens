"""
CrossSectionalRegressor — daily WLS factor-return estimation for IEC-1.

Model
-----
    r_i = Σ_k  B_ik * f_k  +  ε_i

where
    r_i   : return of security i on day t
    B_ik  : factor exposure of security i to factor k  (from ExposureBuilder)
    f_k   : factor return for factor k on day t  (estimated here)
    ε_i   : idiosyncratic residual

Estimation
----------
WLS with square-root market-cap weights (default).

Constraint
----------
The 26 industry factor returns are constrained to sum to zero each day:
    Σ_{j ∈ Industries}  f_j  =  0
This is enforced via the KKT / Lagrange-multiplier approach (one linear
equality constraint added to the WLS normal equations), so industries
represent performance *relative* to the market factor.

History accumulation
--------------------
Each call to `fit_day()` appends results internally. Call
`get_factor_return_panel()` and `get_residual_panel()` to retrieve the
full time-series for covariance estimation.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
from scipy import linalg

from .config import (
    ALL_FACTORS,
    IDX_INDUSTRY_END,
    IDX_INDUSTRY_START,
    INDUSTRY_FACTORS,
    STYLE_FACTORS,
    N_FACTORS,
)


class CrossSectionalRegressor:
    """
    Fit one day's cross-sectional WLS regression and accumulate history.

    Parameters
    ----------
    weight_scheme : str
        'market_cap'  — weight by sqrt(market_cap)  [default, recommended]
        'equal'       — uniform weights (OLS)
    min_securities : int
        Minimum number of securities needed to fit; raises ValueError if not met.
    """

    def __init__(
        self,
        weight_scheme: str = "market_cap",
        min_securities: int = 50,
    ) -> None:
        self.weight_scheme = weight_scheme
        self.min_securities = min_securities

        self._factor_returns: list[pd.Series] = []
        self._residuals: list[pd.Series] = []
        self._dates: list = []
        self._r_squared: list[float] = []

        # Pre-build constraint matrix (1 × N_FACTORS)
        # Selects the industry columns; sum must equal zero
        self._C = np.zeros((1, N_FACTORS))
        self._C[0, IDX_INDUSTRY_START:IDX_INDUSTRY_END] = 1.0

    # ------------------------------------------------------------------
    # Public API — single-day fit
    # ------------------------------------------------------------------

    def fit_day(
        self,
        returns: pd.Series,
        exposures: pd.DataFrame,
        market_caps: Optional[pd.Series] = None,
        date=None,
    ) -> dict:
        """
        Estimate factor returns for one cross-section.

        Parameters
        ----------
        returns     : pd.Series   stock returns (index = security IDs)
        exposures   : pd.DataFrame  (N × 39) from ExposureBuilder.build()
        market_caps : pd.Series   optional weights; required if weight_scheme='market_cap'
        date        : any         label for this day (stored in history)

        Returns
        -------
        dict
            factor_returns  : pd.Series  (39,)
            residuals       : pd.Series  (N,)  idiosyncratic returns
            r_squared       : float
            factor_t_stats  : pd.Series  (39,)
            n_securities    : int
        """
        # --- Align universe ---
        idx = returns.index.intersection(exposures.index)
        if len(idx) < self.min_securities:
            raise ValueError(
                f"Only {len(idx)} securities after alignment; "
                f"minimum required is {self.min_securities}."
            )

        r = returns.loc[idx].values.astype(float)          # (N,)
        B = exposures.loc[idx, ALL_FACTORS].values.astype(float)  # (N, K)
        N, K = B.shape

        # --- Build WLS diagonal weights ---
        if self.weight_scheme == "market_cap" and market_caps is not None:
            raw_w = market_caps.reindex(idx).fillna(0.0).clip(lower=0.0).values
            # sqrt-cap weighting is standard (Barra / Axioma convention)
            w = np.sqrt(raw_w)
        else:
            w = np.ones(N)

        # Normalise weights to sum to N (keeps scale comparable to OLS)
        w_sum = w.sum()
        if w_sum > 0:
            w = w / w_sum * N

        # --- Weighted design matrix and RHS ---
        Bw = B * w[:, None]   # (N, K) — element-wise row scaling
        rw = r * w             # (N,)

        BtWB = Bw.T @ Bw       # (K, K)
        BtWr = Bw.T @ rw       # (K,)

        # --- KKT system with sum-to-zero industry constraint ---
        #  [ B'WB   C' ] [ f ]   [ B'Wr ]
        #  [ C      0  ] [ λ ] = [ 0    ]
        C = self._C                                   # (1, K)
        KKT = np.block([
            [BtWB,       C.T              ],          # (K, K+1)
            [C,          np.zeros((1, 1)) ],          # (1, K+1)
        ])                                             # (K+1, K+1)
        rhs = np.append(BtWr, 0.0)                   # (K+1,)

        try:
            sol = linalg.solve(KKT, rhs, assume_a="gen")
        except linalg.LinAlgError:
            # Fall back to least-squares if matrix is (near-)singular
            sol, *_ = linalg.lstsq(KKT, rhs)

        f_hat = sol[:K]   # factor returns — discard Lagrange multiplier

        # --- Residuals ---
        eps = r - B @ f_hat                           # (N,)

        # --- Diagnostics: R² and t-statistics ---
        wmean_r = np.average(r, weights=w**2) if w.sum() > 0 else r.mean()
        ss_res = float(np.sum((w * eps) ** 2))
        ss_tot = float(np.sum((w * (r - wmean_r)) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else np.nan

        dof = max(N - K, 1)
        sigma2 = ss_res / dof
        try:
            cov_f = sigma2 * linalg.pinv(BtWB)
            se_f = np.sqrt(np.maximum(np.diag(cov_f), 0.0))
        except Exception:
            se_f = np.full(K, np.nan)
        t_stats = np.where(se_f > 1e-12, f_hat / se_f, np.nan)

        # --- Package results ---
        factor_returns = pd.Series(f_hat, index=ALL_FACTORS)
        residuals      = pd.Series(eps,   index=idx)
        t_stats_s      = pd.Series(t_stats, index=ALL_FACTORS)

        # Accumulate history
        self._factor_returns.append(factor_returns)
        self._residuals.append(residuals)
        self._dates.append(date)
        self._r_squared.append(r2)

        return {
            "factor_returns": factor_returns,
            "residuals":      residuals,
            "r_squared":      r2,
            "factor_t_stats": t_stats_s,
            "n_securities":   N,
        }

    # ------------------------------------------------------------------
    # History retrieval
    # ------------------------------------------------------------------

    def get_factor_return_panel(self) -> pd.DataFrame:
        """
        Return the accumulated factor returns as a (T × K) DataFrame.

        Index is the `date` argument passed to each `fit_day()` call
        (integer day index if None was passed).
        """
        if not self._factor_returns:
            return pd.DataFrame(columns=ALL_FACTORS)
        idx = self._dates if any(d is not None for d in self._dates) else None
        return pd.DataFrame(self._factor_returns, index=idx)

    def get_residual_panel(self) -> pd.DataFrame:
        """
        Return accumulated residuals as a (T × N) DataFrame.

        Securities not present on a given day appear as NaN.
        """
        if not self._residuals:
            return pd.DataFrame()
        idx = self._dates if any(d is not None for d in self._dates) else None
        return pd.DataFrame(self._residuals, index=idx)

    def get_r_squared_series(self) -> pd.Series:
        """Return daily R² values as a pd.Series."""
        return pd.Series(
            self._r_squared,
            index=self._dates if any(d is not None for d in self._dates) else None,
            name="r_squared",
        )

    def reset(self) -> None:
        """Clear all accumulated history."""
        self._factor_returns.clear()
        self._residuals.clear()
        self._dates.clear()
        self._r_squared.clear()
