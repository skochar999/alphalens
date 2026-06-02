"""
PortfolioRisk — risk decomposition and attribution for IEC-1.

Risk identity
-------------
    Total portfolio variance  =  Factor variance  +  Specific variance
    σ²_P  =  w'B F B'w  +  w'Δw

where
    w   : (N,)  portfolio weight vector
    B   : (N,K) factor exposure matrix  (from ExposureBuilder)
    F   : (K,K) factor covariance matrix  (from CovarianceEstimator)
    Δ   : (N,N) diagonal specific-variance matrix  (from CovarianceEstimator)

Factor-level risk contribution
-------------------------------
The marginal contribution of factor k to total portfolio variance is:

    FMCR_k  =  f_k_exp * (F @ f_exp)_k

where f_exp = B'w  (portfolio factor exposure vector).

Summing FMCR_k over all k gives the total factor variance.

Active risk
-----------
Computed identically using active weights  w_active = w_portfolio − w_benchmark.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .config import (
    ALL_FACTORS,
    INDUSTRY_FACTORS,
    MARKET_FACTOR,
    N_FACTORS,
    STYLE_FACTORS,
)


class PortfolioRisk:
    """
    Decompose and attribute portfolio risk using the IEC-1 factor model.

    Parameters
    ----------
    factor_cov : pd.DataFrame  (K × K)  factor covariance matrix (annualised)
    specific_var : pd.Series   (N,)     specific variance per security (annualised)
    exposures : pd.DataFrame   (N × K)  factor exposure matrix for the universe

    All three inputs are typically produced by the other IEC-1 modules:
        - ExposureBuilder.build()           → exposures
        - CovarianceEstimator.fit_fcm()     → factor_cov
        - CovarianceEstimator.fit_specific_risk() → specific_var
    """

    def __init__(
        self,
        factor_cov: pd.DataFrame,
        specific_var: pd.Series,
        exposures: pd.DataFrame,
    ) -> None:
        # Align factor covariance to canonical factor ordering
        self.F = factor_cov.reindex(index=ALL_FACTORS, columns=ALL_FACTORS).fillna(0.0).values  # (K,K)

        # Align exposures to canonical column ordering
        self.B = exposures.reindex(columns=ALL_FACTORS, fill_value=0.0)         # (N, K)

        # Align specific variances to exposure index
        self.spec_var = (
            specific_var
            .reindex(self.B.index)
            .fillna(specific_var.median() if not specific_var.empty else 1e-4)
            .clip(lower=1e-8)
            .values
        )  # (N,)

        self._securities = list(self.B.index)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def risk_report(
        self,
        portfolio_weights: pd.Series,
        benchmark_weights: Optional[pd.Series] = None,
    ) -> dict:
        """
        Full risk decomposition for a portfolio (and optionally vs benchmark).

        Parameters
        ----------
        portfolio_weights : pd.Series
            Security weights in the portfolio; should sum to ~1 for long-only.
            Any securities not in the universe are dropped with a warning.
        benchmark_weights : pd.Series, optional
            Benchmark weights. If provided, active risk metrics are computed.

        Returns
        -------
        dict
            total_risk              : float   annualised total risk (%)
            factor_risk             : float   annualised factor risk (%)
            specific_risk           : float   annualised specific risk (%)
            factor_exposures        : pd.Series  portfolio-level factor exposures (K,)
            factor_contributions    : pd.Series  each factor's % of total variance (K,)
            factor_group_summary    : pd.DataFrame  Market / Style / Industry subtotals
            active_risk             : float or None
            active_factor_contrib   : pd.Series or None
            active_factor_exposures : pd.Series or None
            summary_table           : pd.DataFrame  factor-by-factor detail
        """
        w = self._align_weights(portfolio_weights)

        # Portfolio-level factor exposures: h = B'w  (K,)
        h = self.B.values.T @ w

        # Factor variance: h'Fh
        Fh = self.F @ h
        factor_var = float(h @ Fh)

        # Specific variance: w'Δw  (Δ diagonal)
        spec_var_port = float((w ** 2) @ self.spec_var)

        total_var = factor_var + spec_var_port
        total_var = max(total_var, 1e-10)

        total_risk    = np.sqrt(total_var) * 100
        factor_risk   = np.sqrt(max(factor_var, 0.0)) * 100
        specific_risk = np.sqrt(max(spec_var_port, 0.0)) * 100

        # Factor-level variance contributions: h_k * (Fh)_k
        contrib_var = h * Fh          # (K,) — sums to factor_var
        # Express as % of total portfolio variance
        contrib_pct = (contrib_var / total_var) * 100
        factor_contributions = pd.Series(contrib_pct, index=ALL_FACTORS)
        factor_exposures     = pd.Series(h, index=ALL_FACTORS)

        # Factor group subtotals
        group_summary = self._group_summary(factor_contributions, factor_exposures)

        # Active risk
        active_risk              = None
        active_factor_contrib    = None
        active_factor_exposures  = None

        if benchmark_weights is not None:
            b = self._align_weights(benchmark_weights)
            aw = w - b          # active weights
            ah = self.B.values.T @ aw   # active factor exposures
            Fah = self.F @ ah
            active_factor_var  = float(ah @ Fah)
            active_spec_var    = float((aw ** 2) @ self.spec_var)
            active_total_var   = max(active_factor_var + active_spec_var, 1e-10)
            active_risk        = np.sqrt(active_total_var) * 100

            active_contrib_var = ah * Fah
            active_contrib_pct = (active_contrib_var / active_total_var) * 100
            active_factor_contrib   = pd.Series(active_contrib_pct, index=ALL_FACTORS)
            active_factor_exposures = pd.Series(ah, index=ALL_FACTORS)

        summary_table = self._build_summary(
            factor_exposures, factor_contributions, active_factor_exposures, active_factor_contrib
        )

        return {
            "total_risk":              total_risk,
            "factor_risk":             factor_risk,
            "specific_risk":           specific_risk,
            "factor_exposures":        factor_exposures,
            "factor_contributions":    factor_contributions,
            "factor_group_summary":    group_summary,
            "active_risk":             active_risk,
            "active_factor_contributions": active_factor_contrib,
            "active_factor_exposures": active_factor_exposures,
            "summary_table":           summary_table,
        }

    # ------------------------------------------------------------------
    # Formatted output
    # ------------------------------------------------------------------

    def format_report(self, report: dict) -> str:
        """
        Return a plain-text risk attribution report.
        """
        lines: list[str] = []
        sep  = "=" * 68
        dash = "-" * 68

        lines += [
            sep,
            "  IEC-1  |  PORTFOLIO RISK ATTRIBUTION SUMMARY",
            sep,
            f"  Total Risk              {report['total_risk']:>8.2f}%  (annualised)",
            f"    Factor Risk           {report['factor_risk']:>8.2f}%",
            f"    Specific Risk         {report['specific_risk']:>8.2f}%",
        ]
        if report["active_risk"] is not None:
            lines.append(
                f"  Active Risk (TE)        {report['active_risk']:>8.2f}%"
            )
        lines += [dash, ""]

        # Group summary
        lines.append("  Factor Group Contributions (% of total variance)")
        lines.append(f"  {'Group':<20} {'Exposure':>10} {'Risk Contrib%':>14}")
        lines.append("  " + "-" * 46)
        gs = report["factor_group_summary"]
        for _, row in gs.iterrows():
            lines.append(
                f"  {row['Group']:<20} {row['Exposure']:>10.3f} {row['Risk Contrib%']:>14.2f}"
            )

        lines += ["", dash, ""]
        lines.append("  Factor-Level Detail")
        if report["active_risk"] is not None:
            lines.append(
                f"  {'Factor':<14} {'Exposure':>10} {'Risk Contrib%':>14}"
                f"  {'Act Exposure':>12} {'Act Contrib%':>13}"
            )
        else:
            lines.append(
                f"  {'Factor':<14} {'Exposure':>10} {'Risk Contrib%':>14}"
            )
        lines.append("  " + "-" * 46)

        st = report["summary_table"]
        for _, row in st.iterrows():
            line = f"  {row['Factor']:<14} {row['Exposure']:>10.3f} {row['Risk Contrib%']:>14.2f}"
            if report["active_risk"] is not None and "Active Exposure" in st.columns:
                line += f"  {row['Active Exposure']:>10.3f} {row['Active Contrib%']:>13.2f}"
            lines.append(line)

        lines.append(sep)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _align_weights(self, weights: pd.Series) -> np.ndarray:
        """Align weights to the universe index, filling missing with 0."""
        w = weights.reindex(self._securities, fill_value=0.0).values.astype(float)
        return w

    def _group_summary(
        self,
        contributions: pd.Series,
        exposures: pd.Series,
    ) -> pd.DataFrame:
        groups = {
            "Market":   [MARKET_FACTOR],
            "Style":    STYLE_FACTORS,
            "Industry": INDUSTRY_FACTORS,
        }
        rows = []
        for g, factors in groups.items():
            rows.append({
                "Group":         g,
                "Exposure":      exposures[factors].sum(),
                "Risk Contrib%": contributions[factors].sum(),
            })
        return pd.DataFrame(rows)

    def _build_summary(
        self,
        exposures: pd.Series,
        contributions: pd.Series,
        active_exp: Optional[pd.Series],
        active_contrib: Optional[pd.Series],
    ) -> pd.DataFrame:
        data: dict = {
            "Factor":         ALL_FACTORS,
            "Exposure":       exposures.values,
            "Risk Contrib%":  contributions.values,
        }
        if active_exp is not None:
            data["Active Exposure"] = active_exp.values
            data["Active Contrib%"] = active_contrib.values
        return pd.DataFrame(data)
