"""
CovarianceEstimator — Factor Covariance Matrix (FCM) and Specific Risk for IEC-1.

Factor Covariance Matrix
------------------------
Built from the time series of daily factor returns produced by
CrossSectionalRegressor.  Ledoit-Wolf analytical shrinkage is applied
by default to:
  • reduce estimation error from finite samples
  • guarantee positive-definiteness (required for valid portfolio variances)

An additional eigenvalue-floor step enforces strict positive-definiteness
even after shrinkage.

Specific Risk
-------------
Estimated as the rolling sample variance of each security's daily
idiosyncratic residuals from the cross-sectional regression.
A floor is applied to prevent negative or near-zero specific variances
from causing numerical issues downstream.
"""

from __future__ import annotations

import warnings
from typing import Optional

import numpy as np
import pandas as pd

from .config import (
    ALL_FACTORS,
    ANNUALISATION_FACTOR,
    DEFAULT_FCM_LOOKBACK,
    DEFAULT_SPEC_LOOKBACK,
    N_FACTORS,
)

# sklearn is an optional (but strongly recommended) dependency
try:
    from sklearn.covariance import LedoitWolf as _LedoitWolf
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False
    warnings.warn(
        "scikit-learn not found; falling back to sample covariance (no shrinkage). "
        "Install scikit-learn for Ledoit-Wolf shrinkage.",
        ImportWarning,
        stacklevel=2,
    )


class CovarianceEstimator:
    """
    Estimate the IEC-1 Factor Covariance Matrix (FCM) and specific risk.

    Parameters
    ----------
    lookback : int
        Trading days of factor-return history used for FCM estimation.
        Default: 252 (one calendar year).
    spec_lookback : int
        Trading days of residual history used for specific risk estimation.
        Default: 60 (~3 months).
    annualize : bool
        If True, multiply variances by 252 (covariances by 252) so that
        all risk figures are expressed in annualised terms. Default: True.
    shrinkage : str
        'ledoit_wolf'  — Ledoit-Wolf analytical shrinkage (recommended)
        'none'         — raw sample covariance (no shrinkage)
    min_periods : int
        Minimum non-NaN observations required per factor before estimation.
        Factors with fewer observations get variance set to NaN.
    spec_floor : float
        Minimum annualised specific variance (prevents division-by-zero and
        negative values). Default: 1e-6 (~0.1 bp² annualised).
    """

    def __init__(
        self,
        lookback: int = DEFAULT_FCM_LOOKBACK,
        spec_lookback: int = DEFAULT_SPEC_LOOKBACK,
        annualize: bool = True,
        shrinkage: str = "ledoit_wolf",
        min_periods: int = 30,
        spec_floor: float = 1e-6,
    ) -> None:
        self.lookback     = lookback
        self.spec_lookback = spec_lookback
        self.annualize    = annualize
        self.shrinkage    = shrinkage
        self.min_periods  = min_periods
        self.spec_floor   = spec_floor

    # ------------------------------------------------------------------
    # Factor Covariance Matrix
    # ------------------------------------------------------------------

    def fit_fcm(self, factor_returns: pd.DataFrame) -> pd.DataFrame:
        """
        Estimate the (K × K) factor covariance matrix.

        Parameters
        ----------
        factor_returns : pd.DataFrame
            (T × K) daily factor returns; columns should be ALL_FACTORS.
            Typically the output of CrossSectionalRegressor.get_factor_return_panel().

        Returns
        -------
        pd.DataFrame, shape (K, K)
            Symmetric, positive-definite factor covariance matrix.
            If annualize=True, units are (annual return)².
        """
        fr = (
            factor_returns
            .reindex(columns=ALL_FACTORS)
            .tail(self.lookback)
            .dropna(how="all")
        )

        if len(fr) < self.min_periods:
            raise ValueError(
                f"Only {len(fr)} periods of factor returns available; "
                f"minimum is {self.min_periods}."
            )

        # Drop factors with too few observations (fill with 0 for the matrix)
        valid = fr.dropna(axis=1, thresh=self.min_periods)
        missing_cols = [c for c in ALL_FACTORS if c not in valid.columns]

        if self.shrinkage == "ledoit_wolf" and _HAS_SKLEARN:
            lw = _LedoitWolf(assume_centered=False)
            lw.fit(valid.fillna(0.0).values)
            cov_arr = lw.covariance_
        else:
            cov_arr = valid.fillna(0.0).cov().values

        if self.annualize:
            cov_arr = cov_arr * ANNUALISATION_FACTOR

        # Enforce strict positive-definiteness
        cov_arr = self._enforce_pd(cov_arr)

        cov_df = pd.DataFrame(cov_arr, index=valid.columns, columns=valid.columns)

        # Re-insert missing factors with a small diagonal variance
        full = cov_df.reindex(index=ALL_FACTORS, columns=ALL_FACTORS, fill_value=0.0)
        for col in missing_cols:
            full.loc[col, col] = self.spec_floor

        return full

    # ------------------------------------------------------------------
    # Specific Risk
    # ------------------------------------------------------------------

    def fit_specific_risk(
        self,
        residuals: pd.DataFrame,
        min_obs: int = 20,
    ) -> pd.Series:
        """
        Estimate per-security specific variance from regression residuals.

        Parameters
        ----------
        residuals : pd.DataFrame
            (T × N) daily idiosyncratic residuals; rows = dates, cols = securities.
            Typically CrossSectionalRegressor.get_residual_panel().
        min_obs : int
            Minimum non-NaN observations required per security.
            Securities with fewer observations receive NaN specific risk.

        Returns
        -------
        pd.Series (N,)
            Annualised specific variance per security, floored at spec_floor.
        """
        panel = residuals.tail(self.spec_lookback)

        counts = panel.notna().sum()
        spec_var = panel.var(ddof=1)

        # Zero out securities with insufficient history
        spec_var[counts < min_obs] = np.nan

        if self.annualize:
            spec_var = spec_var * ANNUALISATION_FACTOR

        # Apply floor — prevents negative / near-zero values
        spec_var = spec_var.clip(lower=self.spec_floor).fillna(self.spec_floor)

        return spec_var

    # ------------------------------------------------------------------
    # Correlation matrix (diagnostic utility)
    # ------------------------------------------------------------------

    @staticmethod
    def cov_to_corr(fcm: pd.DataFrame) -> pd.DataFrame:
        """Convert a covariance matrix to a correlation matrix."""
        std = np.sqrt(np.diag(fcm.values))
        std_outer = np.outer(std, std)
        corr = fcm.values / np.where(std_outer > 0, std_outer, 1.0)
        np.fill_diagonal(corr, 1.0)
        return pd.DataFrame(corr, index=fcm.index, columns=fcm.columns)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _enforce_pd(matrix: np.ndarray, epsilon: float = 1e-8) -> np.ndarray:
        """
        Floor negative eigenvalues to `epsilon` to enforce positive-definiteness.
        Uses eigendecomposition; reconstructs the matrix from clipped eigenvalues.
        """
        # Symmetrize first (guard against floating-point asymmetry)
        mat = (matrix + matrix.T) / 2.0
        eigvals, eigvecs = np.linalg.eigh(mat)
        eigvals_clipped = np.maximum(eigvals, epsilon)
        return (eigvecs * eigvals_clipped) @ eigvecs.T
