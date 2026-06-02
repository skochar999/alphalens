"""
IEC-1 — MethodTech India Equity Consolidated-1
39-Factor Equity Risk Model

Modules
-------
config      : Factor names, industry list, constants
utils       : Winsorization, z-scoring, composite helpers
exposures   : ExposureBuilder — 39-factor loading matrix
regression  : CrossSectionalRegressor — daily WLS factor return estimation
covariance  : CovarianceEstimator — FCM + specific risk
portfolio   : PortfolioRisk — risk decomposition & attribution
"""

from .config import (
    MARKET_FACTOR,
    STYLE_FACTORS,
    INDUSTRY_FACTORS,
    ALL_FACTORS,
    N_FACTORS,
)
from .exposures import ExposureBuilder
from .regression import CrossSectionalRegressor
from .covariance import CovarianceEstimator
from .portfolio import PortfolioRisk

__all__ = [
    "MARKET_FACTOR",
    "STYLE_FACTORS",
    "INDUSTRY_FACTORS",
    "ALL_FACTORS",
    "N_FACTORS",
    "ExposureBuilder",
    "CrossSectionalRegressor",
    "CovarianceEstimator",
    "PortfolioRisk",
]
