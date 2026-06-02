# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**IEC-1** (MethodTech India Equity Consolidated-1) — a 39-factor equity risk model for NSE-listed Indian equities. It estimates factor returns, builds a factor covariance matrix (FCM), and decomposes portfolio risk into factor and specific components.

## Running the demo

From the `outputs/` directory (parent of `inec1/`):

```bash
python -m inec1.example
```

This runs a full end-to-end pipeline on 500 synthetic securities over 300 trading days.

## Real data ingestion

```python
from inec1.data.pipeline import DataPipeline
from inec1.data.universe import get_nifty100

pipeline = DataPipeline(cache_dir="./inec1_cache")
df = pipeline.build(get_nifty100())
print(pipeline.coverage_report(df))
```

`DataPipeline` fetches from yfinance (~80% descriptor coverage) and Screener.in (5-year fundamentals), merging with Screener values taking precedence for the descriptors it covers more reliably. Cache TTL is 23 hours (one refresh per day); stored as pickle files under `cache_dir`.

## Dependencies

Core: `numpy`, `pandas`, `scipy`
Recommended: `scikit-learn` (Ledoit-Wolf shrinkage — falls back to sample covariance without it)
Data layer: `yfinance`, `requests`, `beautifulsoup4`

## Architecture

The pipeline has four sequential stages, each implemented as a standalone class:

```
ExposureBuilder.build(security_data)   → B: (N × 39) exposure matrix
    ↓
CrossSectionalRegressor.fit_day(...)   → daily factor returns f, residuals ε
    ↓
CovarianceEstimator.fit_fcm(...)       → F: (39 × 39) FCM
CovarianceEstimator.fit_specific_risk(...) → Δ: (N,) specific variance
    ↓
PortfolioRisk.risk_report(weights)     → σ²_P = w'BFB'w + w'Δw
```

### Factor taxonomy (`config.py`)

39 factors in canonical order: 1 MARKET + 12 style + 26 industry. `ALL_FACTORS` defines this order and is used as the column contract throughout. Index slices `IDX_MARKET`, `IDX_STYLE_START/END`, `IDX_INDUSTRY_START/END` allow sub-selection without hard-coding positions.

### Descriptor pipeline (`utils.py`)

Every sub-descriptor follows the same three-step transform before being composited:
1. **Winsorize** at [1st, 99th] percentile cross-sectionally (NaNs excluded but preserved)
2. **Z-score** (cross-sectional mean-zero, unit-variance)
3. **Composite** — equal-weight NaN-aware mean, then re-z-scored

`standardize()` = steps 1+2. `composite(*series)` = step 3. Style factors with multiple sub-descriptors (GROWTH, LOWVOL, LIQUIDITY, LEVERAGE, EARNYIELD, EARNVAR, PROFIT) all go through `composite()`.

### Exposure builder (`exposures.py`)

`ExposureBuilder.build(data)` processes a **single cross-section** (one date). For time-series use, call per date and stack results. The `data` DataFrame must be indexed by security ID with all `REQUIRED_COLUMNS` present (NaN-tolerant). Industry unknowns and unmapped labels fall into `MISCELLAN`.

Sign conventions: SIZE uses `−ln(market_cap)` (larger cap → lower exposure). STREV uses `−ret_3m` (reversal). LTMOM skips the most recent month: `(1+ret_12m)/(1+ret_1m) − 1`.

### Cross-sectional regression (`regression.py`)

WLS with sqrt-market-cap weights (Barra/Axioma convention). The 26 industry factor returns are constrained to sum to zero each day via KKT / Lagrange multiplier (one linear equality added to the WLS normal equations). `fit_day()` accumulates history internally; call `reset()` to clear. Retrieve history via `get_factor_return_panel()` (T×K) and `get_residual_panel()` (T×N).

### Covariance estimator (`covariance.py`)

- **FCM**: Ledoit-Wolf analytical shrinkage (sklearn) on the trailing `lookback=252` days of factor returns, then eigenvalue floor to enforce strict positive-definiteness.
- **Specific risk**: rolling sample variance of residuals over `spec_lookback=60` days, floored at `spec_floor=1e-6`.
- All outputs are annualised by default (× 252).

### Portfolio risk (`portfolio.py`)

`PortfolioRisk` holds F, B, and Δ at construction time. `risk_report(portfolio_weights, benchmark_weights)` computes total/factor/specific risk and per-factor variance contributions as a % of total variance. Active risk (tracking error) is computed when `benchmark_weights` is provided. `format_report()` renders a plain-text attribution table.

### Data layer (`data/`)

| File | Purpose |
|------|---------|
| `universe.py` | Nifty 500/100/50 ticker lists; `load_from_nse_csv()` for NSE downloads |
| `yf_source.py` | yfinance adapter; CAPM beta/residual vol computed vs `^NSEI`; 2-year price history |
| `screener.py` | Scrapes Screener.in HTML tables (P&L, balance sheet, cash flow); prefers consolidated |
| `cache.py` | Pickle-file cache keyed by `md5(source|ticker)`; 23-hour TTL |
| `pipeline.py` | Orchestrates yf + Screener, maps GICS → IEC-1 industry, reports coverage |

Tickers are normalised to yfinance format with `.NS` suffix throughout. `strip_suffix()` converts back to bare NSE symbols for Screener URLs.
