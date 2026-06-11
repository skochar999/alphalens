#!/usr/bin/env python3
"""
remap_holdings_to_regular.py

Remaps scheme_code in holdings_attribution.parquet AND holdings_alpha.parquet
from direct plan codes to the corresponding regular plan codes in the new
fund_meta.parquet. Both files are produced by holdings_attribution.py in direct
codes and consumed by compute_scored_funds.py, so both must be remapped.

This runs as step 2.5 of run_monthly_update.py, immediately after step 2
regenerates the holdings files in direct codes. It is NOT idempotent — it bails
out safely if the holdings are already in regular codes.

The portfolio (holdings, factor exposures) is identical between regular and
direct plans of the same fund — only the NAV differs due to TER. So we can
safely reuse the holdings data by updating the scheme_code.

Matching is done by normalising scheme names (stripping plan/option suffixes)
and matching on normalised name + AMC.

Usage:
    python3 remap_holdings_to_regular.py --dry-run    # preview only
    python3 remap_holdings_to_regular.py              # write updated file
"""
from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

import pandas as pd
from rapidfuzz import process, fuzz  # pip install rapidfuzz

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("alphapicker.remap")

HERE = Path(__file__).parent

# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------
_STRIP_PATTERNS = [
    r"\bDirect\b", r"\bRegular\b",
    r"\bPlan\b", r"\bOption\b",
    r"\bGrowth\b", r"\bIDCW\b", r"\bDividend\b",
    r"\bReinvestment\b", r"\bPayout\b",
    r"\bGrowth Option\b", r"\bGrowth Plan\b",
    r"[-–—]+",
]
_STRIP_RE = re.compile(
    "|".join(_STRIP_PATTERNS), flags=re.IGNORECASE
)


def normalise(name: str) -> str:
    n = _STRIP_RE.sub(" ", str(name))
    n = re.sub(r"\s+", " ", n).strip().lower()
    return n


def normalise_amc(amc: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(amc).lower())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(
        description="Remap holdings_attribution scheme codes: direct → regular"
    )
    # --data-dir is the canonical flag the pipeline passes (points at mf_data/).
    # --mf-data kept as a backward-compatible alias.
    p.add_argument("--data-dir",   default=None,
                   help="Path to the mf_data directory (pipeline passes this)")
    p.add_argument("--mf-data",    default=str(HERE / "mf_data"),
                   help="Alias for --data-dir")
    p.add_argument("--dry-run",    action="store_true",
                   help="Print report only — do not write file")
    p.add_argument("--threshold",  type=int, default=85,
                   help="Fuzzy match score threshold 0-100 (default 85)")
    args = p.parse_args()

    mf_data = Path(args.data_dir or args.mf_data)

    # ── Load files ───────────────────────────────────────────────────────
    holdings_path = mf_data / "holdings_attribution.parquet"
    direct_meta_path = mf_data / "fund_meta_direct_backup.parquet"
    regular_meta_path = mf_data / "fund_meta.parquet"

    for fp in [holdings_path, direct_meta_path, regular_meta_path]:
        if not fp.exists():
            log.error(f"Missing file: {fp}")
            return

    ha      = pd.read_parquet(holdings_path)
    direct  = pd.read_parquet(direct_meta_path)
    regular = pd.read_parquet(regular_meta_path)

    log.info(f"Holdings: {len(ha):,} rows, {ha['scheme_code'].nunique()} unique scheme codes")
    log.info(f"Direct meta: {len(direct)} schemes")
    log.info(f"Regular meta: {len(regular)} schemes")

    # ── Idempotency guard ────────────────────────────────────────────────
    # Codes ALREADY in the regular universe (e.g. holdings sourced directly for
    # the regular plan — the Phase-1+ AMCs) are passed through unchanged in the
    # loop below, so mixed direct+regular holdings remap safely and this step is
    # idempotent. Only bail if EVERYTHING is already regular (nothing to map).
    regular_code_set = set(regular["scheme_code"].astype(int))
    ha_code_set      = set(int(c) for c in ha["scheme_code"].unique())
    already_regular  = len(ha_code_set & regular_code_set) / max(len(ha_code_set), 1)
    if already_regular >= 0.99:
        log.info(
            f"Holdings already fully regular-coded "
            f"({already_regular:.0%} in regular meta) — nothing to remap."
        )
        return

    # ── Build lookup: direct code → scheme name + amc ────────────────────
    direct_lookup: dict[int, tuple[str, str]] = {
        int(r.scheme_code): (str(r.scheme_name), str(r.amc))
        for _, r in direct.iterrows()
    }

    # ── Build regular name index (normalised_name+amc → scheme_code) ─────
    regular["_norm_name"] = regular["scheme_name"].apply(normalise)
    regular["_norm_amc"]  = regular["amc"].apply(normalise_amc)
    regular["_key"]       = regular["_norm_name"] + "|||" + regular["_norm_amc"]

    regular_by_key: dict[str, int] = dict(
        zip(regular["_key"], regular["scheme_code"].astype(int))
    )
    regular_keys = list(regular_by_key.keys())

    # ── Map each direct code to a regular code ───────────────────────────
    ha_codes = ha["scheme_code"].unique()
    code_map: dict[int, int] = {}  # direct_code → regular_code
    unmatched: list[int] = []
    neither: list[int] = []

    for code in ha_codes:
        code = int(code)
        if code in regular_code_set:
            # Already a regular-plan code (holdings sourced directly for the
            # regular universe) — keep as-is, no mapping needed.
            code_map[code] = code
            continue
        if code not in direct_lookup:
            neither.append(code)
            continue

        name, amc = direct_lookup[code]
        norm_name = normalise(name)
        norm_amc  = normalise_amc(amc)
        query_key = norm_name + "|||" + norm_amc

        # Exact match first
        if query_key in regular_by_key:
            code_map[code] = regular_by_key[query_key]
            continue

        # Fuzzy match on normalised name within same AMC
        amc_keys = [k for k in regular_keys if k.endswith("|||" + norm_amc)]
        if amc_keys:
            best, score, _ = process.extractOne(
                query_key, amc_keys, scorer=fuzz.token_sort_ratio
            )
            if score >= args.threshold:
                code_map[code] = regular_by_key[best]
                continue

        # Fuzzy across all if AMC filter missed
        best, score, _ = process.extractOne(
            query_key, regular_keys, scorer=fuzz.token_sort_ratio
        )
        if score >= args.threshold:
            code_map[code] = regular_by_key[best]
        else:
            unmatched.append(code)

    # ── Report ───────────────────────────────────────────────────────────
    log.info(f"\n{'='*60}")
    log.info(f"  Exact / fuzzy matched:  {len(code_map)}")
    log.info(f"  Unmatched (no regular): {len(unmatched)}")
    log.info(f"  Neither meta (proxies): {len(neither)}")

    if unmatched:
        log.info("\n  Unmatched direct codes (will be dropped):")
        for code in unmatched[:20]:
            name = direct_lookup.get(code, ("?",))[0]
            log.info(f"    {code}  {name}")
        if len(unmatched) > 20:
            log.info(f"    ... and {len(unmatched)-20} more")

    if neither:
        log.info(f"\n  'Neither' codes (proxy benchmarks, will be dropped): {neither[:10]}")

    # ── Apply remap ──────────────────────────────────────────────────────
    # Both holdings_attribution.parquet (monthly rows) and holdings_alpha.parquet
    # (per-fund summary) are produced by holdings_attribution.py in DIRECT codes
    # and read by compute_scored_funds.py — so BOTH must be remapped.
    drop_codes    = set(unmatched) | set(neither)
    regular_codes = set(regular["scheme_code"].astype(int))
    import shutil

    def remap_file(path: Path, backup_name: str) -> None:
        """Remap scheme_code direct→regular in `path`, backing up first."""
        if not path.exists():
            log.warning(f"  {path.name} not found — skipping")
            return
        df = pd.read_parquet(path)
        if "scheme_code" not in df.columns:
            log.warning(f"  {path.name} has no scheme_code column — skipping")
            return
        before = len(df)
        df = df[~df["scheme_code"].isin(drop_codes)].copy()
        df["scheme_code"] = df["scheme_code"].apply(
            lambda c: code_map.get(int(c), int(c))
        )
        df = df[df["scheme_code"].isin(regular_codes)]
        after = len(df)
        log.info(f"  {path.name}: {before:,} → {after:,} rows, "
                 f"{df['scheme_code'].nunique()} schemes")
        if args.dry_run:
            return
        shutil.copy(path, mf_data / backup_name)
        df.to_parquet(path, index=False)
        log.info(f"    ✓ written (backup → {backup_name})")

    log.info(f"\n  Applying remap to holdings files:")
    remap_file(holdings_path,                 "holdings_attribution_direct_backup.parquet")
    remap_file(mf_data / "holdings_alpha.parquet", "holdings_alpha_direct_backup.parquet")
    log.info(f"{'='*60}")

    if args.dry_run:
        log.info("  Dry run — files NOT written.")
        return
    log.info("\nNext: run python3 run_monthly_update.py --from-step 3 to rescore")


if __name__ == "__main__":
    main()
