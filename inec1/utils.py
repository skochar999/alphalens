"""
Shared utility functions for IEC-1 factor construction.

All descriptor-level operations follow the three-step pipeline:
  1. Winsorize     — cap extreme values at chosen percentiles
  2. Z-score       — cross-sectional mean-zero, unit-variance
  3. Composite     — equal-weight average of standardized sub-descriptors
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import DEFAULT_WINSOR_LOWER, DEFAULT_WINSOR_UPPER


# ---------------------------------------------------------------------------
# Core transformations
# ---------------------------------------------------------------------------

def winsorize(
    series: pd.Series,
    lower: float = DEFAULT_WINSOR_LOWER,
    upper: float = DEFAULT_WINSOR_UPPER,
) -> pd.Series:
    """
    Clip extreme values at the given cross-sectional percentiles.

    NaNs are excluded from quantile computation but propagated in output.

    Parameters
    ----------
    series : pd.Series
    lower  : float  lower quantile (e.g. 0.01 = 1st pctile)
    upper  : float  upper quantile (e.g. 0.99 = 99th pctile)

    Returns
    -------
    pd.Series — same index, values capped at [lo, hi]
    """
    lo = series.quantile(lower)
    hi = series.quantile(upper)
    return series.clip(lower=lo, upper=hi)


def zscore(series: pd.Series) -> pd.Series:
    """
    Cross-sectional standardization: subtract mean, divide by std.

    Returns a zero-mean, unit-variance series. If std == 0 or series is
    constant, returns a zero-filled series (no division by zero).

    Parameters
    ----------
    series : pd.Series

    Returns
    -------
    pd.Series — standardized, NaNs preserved
    """
    mu = series.mean()
    sigma = series.std(ddof=1)
    if sigma == 0.0 or np.isnan(sigma):
        return pd.Series(0.0, index=series.index)
    return (series - mu) / sigma


def standardize(
    series: pd.Series,
    winsor_lower: float = DEFAULT_WINSOR_LOWER,
    winsor_upper: float = DEFAULT_WINSOR_UPPER,
) -> pd.Series:
    """
    Full descriptor pipeline: winsorize → z-score.

    Missing values (NaN) are preserved; they do not participate in the
    winsorization or standardization cross-section but are returned as NaN.

    Parameters
    ----------
    series       : pd.Series   raw descriptor values
    winsor_lower : float       lower winsorization percentile
    winsor_upper : float       upper winsorization percentile

    Returns
    -------
    pd.Series — cleaned, standardized descriptor (same index as input)
    """
    notna = series.dropna()
    if notna.empty:
        return pd.Series(np.nan, index=series.index)
    clipped = winsorize(notna, winsor_lower, winsor_upper)
    scored = zscore(clipped)
    return scored.reindex(series.index)  # reintroduce NaN for missing names


def composite(*descriptors: pd.Series, fill_value: float = 0.0) -> pd.Series:
    """
    Equal-weight composite of fully standardized sub-descriptors.

    Each series should already have been passed through `standardize()`.
    NaN sub-descriptors are excluded from the average per security (i.e.
    composites are computed on available inputs). If all inputs are NaN
    for a security, the composite is NaN.

    After averaging, the composite is re-z-scored cross-sectionally so
    the final factor exposure is always on a comparable scale.

    Parameters
    ----------
    *descriptors : pd.Series   standardized sub-descriptor series
    fill_value   : float       not used for averaging (NaN-aware mean used)

    Returns
    -------
    pd.Series — equal-weight composite, re-standardized
    """
    df = pd.concat(descriptors, axis=1)
    avg = df.mean(axis=1, skipna=True)
    # Re-standardize so final exposure is unit-scale
    return zscore(avg.dropna()).reindex(avg.index)


# ---------------------------------------------------------------------------
# Convenience aliases
# ---------------------------------------------------------------------------

def signed_standardize(
    series: pd.Series,
    sign: float = 1.0,
    **kwargs,
) -> pd.Series:
    """
    Standardize and optionally flip sign (e.g. SIZE uses −ln(mktcap)).
    """
    return standardize(series * sign, **kwargs)
