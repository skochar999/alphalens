#!/usr/bin/env python3
"""
fetch_holdings_and_score.py
===========================
RUN THIS ON YOUR MAC (not in the sandbox).

What this does in one shot:
  1. Downloads SEBI-mandated monthly portfolio disclosures from each AMC
     (SBI, ICICI, HDFC, ABSL, Nippon, Axis, DSP, Franklin + Kotak/Mirae if manual)
  2. Fuzzy-matches scheme names → scheme_codes
  3. Downloads NSE equity master → builds ISIN→ticker map
  4. Computes proper holdings-based factor exposures using the
     IEC-1 exposure matrix (exposures_*.parquet from inec1_outputs/)
  5. Updates fund_metrics.parquet with accurate current positioning
  6. Reruns scorer.py --force
  7. Rebuilds both websites

Why this matters:
  Previously, "current positioning" was estimated from rolling OLS regression
  betas (backward-looking, 12-month lag, coarse 3-group model).
  Now it uses: Σ(portfolio_weight × IEC-1_stock_exposure) → fund factor exposure
  across all 39 factors — the same model used for return attribution.

AMC coverage (155/172 funds = 90%):
  ✅ SBI (30)  ICICI (25)  HDFC (19)  ABSL (16)  Nippon (16)
  ✅ Axis (14)  DSP (12)  Franklin (12)
  ⚠️  Kotak (17) + Mirae (11) — place manual xlsx files before running

Usage:
    python fetch_holdings_and_score.py
    python fetch_holdings_and_score.py --month 2026-04
    python fetch_holdings_and_score.py --amc SBI --skip-rescore
"""
from __future__ import annotations

import argparse
import calendar
import datetime
import io
import logging
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mf.holdings")

HERE = Path(__file__).parent

# Import AMC scraper (same directory)
sys.path.insert(0, str(HERE))
from amc_holdings_scraper import download_all_holdings, attach_scheme_codes  # noqa: E402

# ---------------------------------------------------------------------------
# Factor taxonomy (mirrors inec1/config.py)
# ---------------------------------------------------------------------------
STYLE_FACTORS = [
    "BETA", "SIZE", "STREV", "LTMOM", "VALUE", "GROWTH",
    "LOWVOL", "LIQUIDITY", "LEVERAGE", "EARNYIELD", "EARNVAR", "PROFIT",
]
INDUSTRY_FACTORS = [
    "AERODEF", "AUTOMOBL", "AUTOCOMP", "BANKS", "BUSINSERV", "CAPGOODS",
    "CHEMICALS", "CONSUMDISC", "CONSUMDUR", "CONSUMSTAP", "FINLSERV",
    "HEALTHCARE", "ITSERVIC", "MATERIALS", "METMINING", "OILGAS",
    "PHARMA", "POWERGEN", "REALESTATE", "SOFTWARE", "TECHCOMP",
    "TELECOM", "TRADDIST", "TRANSPORT", "UTILITIES", "MISCELLAN",
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


# ===========================================================================
# Step 1 — Download AMC portfolio holdings (replaces defunct AMFI endpoint)
# ===========================================================================

def _default_month() -> str:
    """Return previous calendar month as 'YYYY-MM' (what SEBI requires AMCs to publish)."""
    today = datetime.date.today()
    first = today.replace(day=1)
    prev  = first - datetime.timedelta(days=1)
    return prev.strftime("%Y-%m")


def download_holdings(
    mf_data: Path,
    month_str: str,
    force: bool,
    amc_filter: str | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Download SEBI-mandated monthly portfolio disclosures from each AMC website
    using amc_holdings_scraper.py, fuzzy-match scheme names → scheme_codes,
    and cache the result.

    Returns: {month_str: DataFrame[scheme_code, isin, pct_nav, stock_name, amc]}
    """
    hold_dir = mf_data / "holdings"
    hold_dir.mkdir(parents=True, exist_ok=True)

    out_path = hold_dir / f"{month_str}.parquet"

    if not force and out_path.exists():
        log.info(f"  {month_str}: loaded from cache ({out_path})")
        return {month_str: pd.read_parquet(out_path)}

    log.info(f"  Downloading AMC portfolio disclosures for {month_str} …")
    raw = download_all_holdings(month_str, amc_filter=amc_filter)

    if raw.empty:
        log.warning(f"  {month_str}: no holdings downloaded")
        return {}

    # ── Fuzzy match scheme names → scheme_codes ──────────────────────────────
    scheme_list_path = mf_data / "scheme_list.parquet"
    if scheme_list_path.exists():
        scheme_df = pd.read_parquet(scheme_list_path)
        # Ensure 'amc' column exists on scheme_df (may be called 'amc_name')
        if "amc" not in scheme_df.columns and "amc_name" in scheme_df.columns:
            scheme_df = scheme_df.rename(columns={"amc_name": "amc"})
        raw = attach_scheme_codes(raw, scheme_df)
        # Coerce to nullable Int64 so merges with fund_metrics (int64) work cleanly
        raw["scheme_code"] = pd.to_numeric(raw["scheme_code"], errors="coerce")
        raw["scheme_code"] = raw["scheme_code"].where(
            raw["scheme_code"].notna(),
            other=pd.NA
        ).astype("Int64")
    else:
        log.warning(f"  scheme_list.parquet not found at {scheme_list_path}")
        raw["scheme_code"] = pd.NA

    n_matched = raw["scheme_code"].notna().sum()
    n_total   = len(raw)
    log.info(f"  {month_str}: {n_total:,} holding rows, "
             f"{n_matched:,} matched to scheme_codes "
             f"({n_matched/n_total*100:.0f}%)")

    raw.to_parquet(out_path, index=False)
    log.info(f"  Saved: {out_path}")
    return {month_str: raw}


# ===========================================================================
# Step 2 — Build ISIN → NSE ticker map
# ===========================================================================

def build_isin_ticker_map(mf_data: Path, force: bool) -> dict[str, str]:
    """
    Download NSE equity master CSV → ISIN → ticker (.NS) mapping.
    NSE publishes a complete list at their archives.
    """
    cache_path = mf_data / "isin_ticker_map.parquet"

    if not force and cache_path.exists():
        log.info("  ISIN map: loaded from cache")
        df = pd.read_parquet(cache_path)
        return dict(zip(df["isin"], df["ticker"]))

    log.info("  Downloading NSE equity master …")

    nse_url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

    def _parse_nse_csv(text: str) -> dict[str, str]:
        df = pd.read_csv(io.StringIO(text))
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        isin_col   = next(c for c in df.columns if "isin" in c)
        symbol_col = next(c for c in df.columns if "symbol" in c)
        df = df[[isin_col, symbol_col]].dropna()
        df.columns = ["isin", "symbol"]
        df["ticker"] = df["symbol"].str.strip() + ".NS"
        df["isin"]   = df["isin"].str.strip()
        df = df[["isin", "ticker"]].drop_duplicates("isin")
        df.to_parquet(cache_path)
        log.info(f"  ISIN map: {len(df):,} entries")
        return dict(zip(df["isin"], df["ticker"]))

    # Try 1: requests
    try:
        r = requests.get(nse_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        if len(r.text) > 1_000:
            return _parse_nse_csv(r.text)
    except Exception as e:
        log.debug(f"  requests failed for NSE CSV: {e}")

    # Try 2: curl fallback
    log.info("  Trying curl for NSE CSV …")
    text = _fetch_via_curl(nse_url, timeout=30)
    if text and len(text) > 1_000:
        try:
            return _parse_nse_csv(text)
        except Exception as e:
            log.warning(f"  NSE CSV parse failed: {e}")

    log.warning("  Could not build ISIN map — holdings-based exposure will be skipped")
    return {}


# ===========================================================================
# Step 3 — Compute holdings-based factor exposures
# ===========================================================================

def find_latest_exposure_file(inec1_dir: Path) -> Path | None:
    files = sorted(inec1_dir.glob("exposures_*.parquet"), reverse=True)
    return files[0] if files else None


def compute_fund_exposures(
    holdings_by_month: dict[str, pd.DataFrame],
    isin_map: dict[str, str],
    exposure_matrix: pd.DataFrame,
) -> pd.DataFrame:
    """
    For each fund, compute the AUM-weighted average IEC-1 factor exposure
    across all 39 factors using the most recent month's holdings.

    Holdings DataFrame columns (from amc_holdings_scraper):
        scheme_code | isin | pct_nav | stock_name | amc | scheme_name | as_of_date

    Returns DataFrame with one row per matched fund:
        scheme_code | holdings_month | holdings_coverage_pct | holdings_style_active
        | holdings_industry_active | holdings_active_exposure | exp_{FACTOR} × 39
    """
    if not holdings_by_month:
        return pd.DataFrame()

    all_factors = STYLE_FACTORS + INDUSTRY_FACTORS
    records = []

    # Use the most recent month that has data
    latest_month = max(holdings_by_month.keys())
    holdings     = holdings_by_month[latest_month].copy()
    log.info(f"  Computing exposures from {latest_month} holdings …")

    # Only process rows with a matched scheme_code
    holdings = holdings[holdings["scheme_code"].notna()].copy()
    if holdings.empty:
        log.warning("  No holdings rows with matched scheme_codes — check fuzzy matching")
        return pd.DataFrame()

    # Normalise column names (isin, pct_nav are the new scraper names)
    isin_col   = "isin"
    weight_col = "pct_nav"

    for sc, grp in holdings.groupby("scheme_code"):
        # Normalise weights to sum to 1 within this fund
        total_w = grp[weight_col].sum()
        if total_w <= 0:
            continue
        grp = grp.copy()
        grp["w"] = grp[weight_col] / total_w

        fund_exp       = pd.Series(0.0, index=all_factors)
        covered_w      = 0.0
        matched_stocks = 0

        for _, row in grp.iterrows():
            ticker = isin_map.get(row[isin_col])
            if ticker is None or ticker not in exposure_matrix.index:
                continue
            stock_exp    = exposure_matrix.loc[ticker, all_factors].fillna(0.0)
            fund_exp    += row["w"] * stock_exp
            covered_w   += row["w"]
            matched_stocks += 1

        coverage = covered_w  # fraction of AUM weight matched to IEC-1 universe

        if coverage < 0.05:
            # Less than 5% coverage — data not reliable enough
            continue

        # ── Derived positioning metrics ───────────────────────────────────
        style_exp       = fund_exp[STYLE_FACTORS]
        industry_exp    = fund_exp[INDUSTRY_FACTORS]

        # Active style tilt: RMS across 12 style factor exposures
        # (z-scored, so 1.0 = one std-dev tilt vs. market-cap benchmark)
        style_active    = float(np.sqrt((style_exp ** 2).mean()))
        # Industry concentration: Σ|industry exposures|
        industry_active = float(industry_exp.abs().sum())
        # Composite active exposure (blended, comparable to regression betas)
        active_exposure = style_active + industry_active * 0.1

        rec = {
            "scheme_code":              int(sc),  # always store as plain int for merge compat
            "holdings_month":           latest_month,
            "holdings_coverage_pct":    round(coverage * 100, 1),
            "holdings_matched_stocks":  matched_stocks,
            "holdings_style_active":    round(style_active, 4),
            "holdings_industry_active": round(industry_active, 4),
            "holdings_active_exposure": round(active_exposure, 4),
        }
        for f in all_factors:
            rec[f"exp_{f}"] = round(float(fund_exp[f]), 4)

        records.append(rec)

    df = pd.DataFrame(records)
    if not df.empty:
        log.info(f"  Exposures computed: {len(df)} funds  "
                 f"(avg coverage {df['holdings_coverage_pct'].mean():.0f}%)")
    return df


# ===========================================================================
# Step 4 — Update fund_metrics with holdings-based positioning
# ===========================================================================

def update_fund_metrics(mf_data: Path, fund_exposures: pd.DataFrame) -> None:
    """
    Merge holdings-based exposure fields into fund_metrics.parquet.
    Scorer.py will prefer these over regression-estimated values when present.
    """
    metrics_path = mf_data / "fund_metrics.parquet"
    if not metrics_path.exists():
        log.warning("fund_metrics.parquet not found — run attribution.py first")
        return

    metrics = pd.read_parquet(metrics_path)
    exp_cols = [c for c in fund_exposures.columns if c != "scheme_code"]

    # Drop any existing holdings columns
    existing_to_drop = [c for c in metrics.columns if c.startswith("holdings_") or c.startswith("exp_")]
    if existing_to_drop:
        metrics = metrics.drop(columns=existing_to_drop)

    metrics = metrics.merge(
        fund_exposures[["scheme_code"] + exp_cols],
        on="scheme_code", how="left"
    )

    # For funds with good holdings coverage (>30%), replace
    # current_active_exposure with the holdings-based value
    has_coverage = (
        metrics["holdings_coverage_pct"].notna() &
        (metrics["holdings_coverage_pct"] >= 30)
    )
    n_updated = has_coverage.sum()
    metrics.loc[has_coverage, "current_active_exposure"] = (
        metrics.loc[has_coverage, "holdings_active_exposure"]
    )
    log.info(f"  Updated current_active_exposure for {n_updated} funds from holdings")

    metrics.to_parquet(metrics_path, index=False)
    log.info(f"  Saved updated fund_metrics.parquet")


# ===========================================================================
# Step 4.5 — Holdings-based Brinson attribution + blend into metrics
# ===========================================================================

def run_holdings_attribution(mf_data: Path, min_coverage: float = 0.30) -> pd.DataFrame:
    """
    Run Brinson attribution engine on all cached holdings months.
    Returns holdings_alpha DataFrame (per-fund summary), or empty DataFrame.
    """
    try:
        from holdings_attribution import run_attribution, compute_fund_alpha_summary
    except ImportError:
        log.warning("holdings_attribution.py not found — skipping Brinson attribution")
        return pd.DataFrame()

    hold_dir = mf_data / "holdings"
    if not hold_dir.exists() or not list(hold_dir.glob("*.parquet")):
        log.info("  No holdings cache yet — Brinson attribution skipped")
        log.info("  (Run backfill_holdings.py on your Mac to build history)")
        return pd.DataFrame()

    log.info("  Running Brinson attribution on cached holdings …")
    attr_df = run_attribution(mf_data, min_coverage=min_coverage)
    if attr_df.empty:
        return pd.DataFrame()

    attr_df.to_parquet(mf_data / "holdings_attribution.parquet", index=False)
    log.info(f"  Saved holdings_attribution.parquet  ({len(attr_df)} fund-month rows)")

    summary = compute_fund_alpha_summary(attr_df, min_months=3)
    if not summary.empty:
        summary.to_parquet(mf_data / "holdings_alpha.parquet", index=False)
        log.info(f"  Saved holdings_alpha.parquet  ({len(summary)} funds)")
    return summary


def blend_holdings_alpha_into_metrics(mf_data: Path, holdings_alpha: pd.DataFrame) -> None:
    """
    Blend holdings-based stock selection alpha and IR into fund_metrics.parquet,
    overwriting the regression-estimated values with a confidence-weighted blend.

    Blending weight is determined by months of holdings data available:
        n >= 12 months : weight = 1.0 (fully holdings-based)
        6 <= n < 12   : weight = n/12 (linearly interpolated)
        3 <= n < 6    : weight = (n/12) × 0.5 (conservative)
        n < 3          : weight = 0   (pure regression, no change)

    This ensures the scorer (which reads fund_metrics) will use the more
    accurate holdings-based alpha once sufficient history exists, while
    gracefully falling back to regression for funds not yet backfilled.
    """
    if holdings_alpha.empty:
        return

    metrics_path = mf_data / "fund_metrics.parquet"
    if not metrics_path.exists():
        log.warning("fund_metrics.parquet not found — cannot blend holdings alpha")
        return

    metrics = pd.read_parquet(metrics_path)

    # Prepare holdings summary (keep only essential columns for the merge)
    ha_cols = ["scheme_code", "holdings_alpha_ann", "holdings_ir",
               "holdings_n_months", "holdings_avg_coverage",
               "holdings_alpha_monthly_mean", "holdings_alpha_monthly_std",
               "style_drift"]
    ha = holdings_alpha[[c for c in ha_cols if c in holdings_alpha.columns]].copy()

    # Drop any holdings-side columns that already exist in metrics (avoid duplicate-column merge error)
    overlap = [c for c in ha.columns if c != "scheme_code" and c in metrics.columns]
    if overlap:
        metrics = metrics.drop(columns=overlap)

    # Merge holdings alpha into metrics
    metrics = metrics.merge(ha, on="scheme_code", how="left")

    # Compute blend weight per fund
    n = metrics["holdings_n_months"].fillna(0)
    blend_w = pd.Series(0.0, index=metrics.index)
    blend_w = blend_w.where(n < 3,  other=(n / 12 * 0.5).clip(upper=0.25))   # 3–5 months
    blend_w = blend_w.where(n < 6,  other=(n / 12).clip(upper=1.0))            # 6–11 months
    blend_w = blend_w.where(n < 12, other=1.0)                                  # 12+ months

    # Coverage gate: funds with <80% average holdings coverage have significant
    # unmodelled positions (e.g. PPFAS with ~21% international stocks not in
    # the IEC-1 universe).  Below 80% coverage the holdings-based alpha is an
    # unreliable proxy for stock-selection skill — fall back to NAV-based alpha.
    # Between 80–90%: linearly scale the blend weight down toward 0 at 80%.
    #   coverage >= 90% → no coverage penalty (use n-based weight as-is)
    #   coverage  = 80% → blend_w multiplied by 0
    #   coverage  = 85% → blend_w multiplied by 0.5
    cov = metrics.get("holdings_avg_coverage", pd.Series(1.0, index=metrics.index)).fillna(1.0)
    cov_scale = ((cov - 0.80) / (0.90 - 0.80)).clip(0.0, 1.0)
    blend_w = blend_w * cov_scale

    # Log what we're blending
    full_replace = (blend_w >= 0.999).sum()
    partial      = ((blend_w > 0) & (blend_w < 1.0)).sum()
    unchanged    = (blend_w == 0).sum()
    log.info(f"  Holdings alpha blend: {int(full_replace)} full, "
             f"{int(partial)} partial, {int(unchanged)} regression-only")

    # Blend alpha_ann and info_ratio
    h_alpha = metrics["holdings_alpha_ann"].fillna(metrics["alpha_ann"])
    h_ir    = metrics["holdings_ir"].fillna(metrics["info_ratio"])

    metrics["alpha_ann"]   = blend_w * h_alpha   + (1 - blend_w) * metrics["alpha_ann"]
    metrics["info_ratio"]  = blend_w * h_ir      + (1 - blend_w) * metrics["info_ratio"]

    # Tag which method was used for transparency
    metrics["alpha_source"] = "regression"
    metrics.loc[blend_w > 0,   "alpha_source"] = "blended"
    metrics.loc[blend_w == 1.0, "alpha_source"] = "holdings"

    metrics.to_parquet(metrics_path, index=False)
    log.info(f"  Updated fund_metrics.parquet with blended alpha/IR")
    if full_replace > 0:
        best = metrics.nlargest(3, "alpha_ann")[["scheme_name","alpha_ann","info_ratio","alpha_source"]]
        log.info(f"  Top 3 by blended alpha:")
        for _, r in best.iterrows():
            log.info(f"    {str(r['scheme_name'])[:50]:50s}  "
                     f"α={r['alpha_ann']*100:+.1f}%  IR={r['info_ratio']:+.2f}  [{r['alpha_source']}]")


# ===========================================================================
# Step 5 — Rerun scorer + rebuild websites
# ===========================================================================

def run(cmd: list[str]) -> bool:
    log.info("  $ " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(HERE))
    if result.returncode != 0:
        log.error(f"  FAILED (exit {result.returncode})")
        return False
    return True


# ===========================================================================
# Main
# ===========================================================================

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download AMC portfolio holdings, compute IEC-1 factor exposures, rescore funds",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--mf-data",      default="./mf_data")
    p.add_argument("--inec1-dir",    default="./inec1_outputs")
    p.add_argument("--month",        default=None,
                   help="YYYY-MM to download (default: previous calendar month)")
    p.add_argument("--amc",          default=None,
                   help="Single AMC to download, e.g. SBI, HDFC (default: all)")
    p.add_argument("--force",        action="store_true",
                   help="Re-download even if cached")
    p.add_argument("--skip-download", action="store_true",
                   help="Skip download; use cached holdings parquet")
    p.add_argument("--skip-rescore", action="store_true",
                   help="Skip scorer + website rebuild")
    p.add_argument("--debug",        action="store_true",
                   help="Verbose logging")
    return p.parse_args()


def main() -> None:
    args     = _parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    mf_data  = Path(args.mf_data)
    inec1dir = Path(args.inec1_dir)

    # Default month = previous calendar month
    month_str = args.month or _default_month()

    log.info("=" * 64)
    log.info("  AMC Holdings → IEC-1 Exposure → FundLens Score")
    log.info("=" * 64)
    log.info(f"  Target month : {month_str}")
    if args.amc:
        log.info(f"  AMC filter   : {args.amc}")

    # ------------------------------------------------------------------ #
    # 1. Download AMC portfolio holdings
    # ------------------------------------------------------------------ #
    if not args.skip_download:
        log.info(f"\n[1/5] Downloading AMC portfolio disclosures for {month_str} …")
        holdings_by_month = download_holdings(
            mf_data, month_str, args.force, amc_filter=args.amc
        )
    else:
        log.info("\n[1/5] Loading cached holdings …")
        hold_dir          = mf_data / "holdings"
        holdings_by_month = {}
        # Load just the target month if it exists
        p = hold_dir / f"{month_str}.parquet"
        if p.exists():
            holdings_by_month[month_str] = pd.read_parquet(p)
        else:
            for f in sorted(hold_dir.glob("*.parquet")):
                holdings_by_month[f.stem] = pd.read_parquet(f)
        log.info(f"  Loaded: {sorted(holdings_by_month.keys())}")

    _holdings_ok = bool(holdings_by_month)
    if not _holdings_ok:
        log.warning(
            "No holdings data available — skipping holdings-based positioning.\n"
            "  Check that AMC websites are reachable and try again."
        )

    latest = max(holdings_by_month.keys()) if holdings_by_month else None
    if latest:
        log.info(f"  Latest available: {latest}")

    # ------------------------------------------------------------------ #
    # 2. Build ISIN → ticker map
    # ------------------------------------------------------------------ #
    isin_map: dict[str, str] = {}
    if _holdings_ok:
        log.info("\n[2/5] Building ISIN → NSE ticker map …")
        isin_map = build_isin_ticker_map(mf_data, args.force)
        if not isin_map:
            log.warning("Could not build ISIN map — holdings-based exposure will be skipped.")
            _holdings_ok = False
    else:
        log.info("\n[2/5] Skipping ISIN map (no holdings data)")

    # ------------------------------------------------------------------ #
    # 3. Load IEC-1 exposure matrix
    # ------------------------------------------------------------------ #
    fund_exposures = pd.DataFrame()
    if _holdings_ok:
        log.info("\n[3/5] Loading IEC-1 exposure matrix …")
        exp_file = find_latest_exposure_file(inec1dir)
        if exp_file is None:
            log.warning(f"No exposures_*.parquet found in {inec1dir} — skipping holdings exposures")
            _holdings_ok = False
        else:
            log.info(f"  Using: {exp_file.name}")
            exposure_matrix = pd.read_parquet(exp_file)
            log.info(f"  {len(exposure_matrix)} stocks × {len(exposure_matrix.columns)} factors")
    else:
        log.info("\n[3/5] Skipping exposure matrix (no holdings data)")
        exposure_matrix = pd.DataFrame()

    # ------------------------------------------------------------------ #
    # 4. Compute holdings-based factor exposures
    # ------------------------------------------------------------------ #
    if _holdings_ok and not exposure_matrix.empty:
        log.info("\n[4/5] Computing holdings-based factor exposures …")
        fund_exposures = compute_fund_exposures(holdings_by_month, isin_map, exposure_matrix)
    else:
        log.info("\n[4/5] Skipping holdings-based exposures")

    if fund_exposures.empty:
        log.warning("No holdings-based exposures — positioning score uses regression betas")
    else:
        # Save standalone exposures file
        exp_out = mf_data / "fund_exposures_latest.parquet"
        fund_exposures.to_parquet(exp_out, index=False)
        log.info(f"  Saved {exp_out.name}  ({len(fund_exposures)} funds)")

        # Show top/bottom by style active exposure
        top = fund_exposures.nlargest(5, "holdings_style_active")[
            ["scheme_code", "holdings_coverage_pct", "holdings_style_active",
             "holdings_active_exposure"]
        ]
        log.info(f"\n  Top 5 by style active exposure:\n{top.to_string()}")

        # Merge into fund_metrics
        update_fund_metrics(mf_data, fund_exposures)

    # ------------------------------------------------------------------ #
    # 4.5. Brinson holdings attribution → blend alpha into fund_metrics
    # ------------------------------------------------------------------ #
    log.info("\n[4.5/5] Holdings-based Brinson attribution …")
    holdings_alpha = run_holdings_attribution(mf_data)
    if not holdings_alpha.empty:
        n_funds = len(holdings_alpha)
        n_months_median = int(holdings_alpha["holdings_n_months"].median())
        log.info(f"  Attribution complete: {n_funds} funds, "
                 f"median {n_months_median} months of history")
        blend_holdings_alpha_into_metrics(mf_data, holdings_alpha)
    else:
        log.info("  No holdings attribution yet — scorer will use regression alpha")
        log.info("  → Run backfill_holdings.py on your Mac to unlock this")

    # ------------------------------------------------------------------ #
    # 5. Rescore + rebuild websites
    # ------------------------------------------------------------------ #
    if not args.skip_rescore:
        log.info("\n[5/5] Rescoring and rebuilding websites …")

        ok = run([sys.executable, str(HERE / "mf_analytics" / "scorer.py"),
                  "--mf-data", str(mf_data), "--force"])
        if ok:
            run([sys.executable, str(HERE / "build_mf_website.py"),
                 "--mf-data", str(mf_data)])
            run([sys.executable, str(HERE / "build_fundlens.py"),
                 "--mf-data", str(mf_data)])
    else:
        log.info("\n[5/5] Skipping rescore (--skip-rescore)")

    log.info("\n" + "=" * 64)
    log.info("  Done.")
    if not fund_exposures.empty:
        cov = fund_exposures["holdings_coverage_pct"].mean()
        log.info(f"  Average holdings coverage: {cov:.0f}%")
        log.info(f"  Holdings date: {latest}")
    log.info("=" * 64)


if __name__ == "__main__":
    main()
