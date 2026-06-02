"""
ingest_absl_full.py
────────────────────
Download and ingest the 12 missing months of Aditya Birla Sun Life (ABSL)
monthly portfolio data using the direct CDN URLs harvested from AdvisorKhoj.

Missing months: Feb–Dec 2023 (11 months) + Jul 2025 (1 month).

Reuses parse_absl_file() and _merge_df_into_cache() from ingest_absl_kotak.py.

Usage:
    python ingest_absl_full.py              # download + ingest
    python ingest_absl_full.py --dry-run    # preview only
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import zipfile
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("ingest_absl")

# ── Paths ──────────────────────────────────────────────────────────────────────
OUTPUTS_DIR = Path("/sessions/admiring-nifty-dijkstra/mnt/outputs")
HOLD_DIR    = OUTPUTS_DIR / "mf_data/holdings"
SCHEME_PATH = OUTPUTS_DIR / "mf_data/scheme_list.parquet"

# ── All 39 ABSL URLs from AdvisorKhoj — hardcoded so no re-scrape needed ──────
# Full set; the script skips months already in parquets (idempotent).
ABSL_URLS: list[tuple[str, str]] = [
    # 2023
    ("2023-01", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2023/sebi_monthly_portfolio-31-jan-2023.zip"),
    ("2023-02", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2023/sebi_monthly_portfolio-28-feb-2023.zip"),
    ("2023-03", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2023/sebi_monthly_portfolio-31-mar-2023.zip"),
    ("2023-04", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2023/sebi_monthly_portfolio-30-apr-2023.zip"),
    ("2023-05", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2023/sebi_monthly_portfolio-31-may-2023.zip"),
    ("2023-06", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2023/sebi_monthly_portfolio-30-june-2023.zip"),
    ("2023-07", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2023/sebi_monthly_portfolio-31-july-2023.zip"),
    ("2023-08", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2023/sebi_monthly_portfolio-31-august-2023.zip"),
    ("2023-09", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2023/sebi_monthly_portfolio-30-september-2023.zip"),
    ("2023-10", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2023/sebi_monthly_portfolio-31-october-2023.zip"),
    ("2023-11", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2023/sebi_monthly_portfolio-30-november-2023.zip"),
    ("2023-12", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2023/sebi_monthly_portfolio-31-december-2023.zip"),
    # 2024
    ("2024-01", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2024/sebi_monthly_portfolio-31-january-2024.zip"),
    ("2024-02", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2024/sebi_monthly_portfolio-29-february-2024.zip"),
    ("2024-03", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2024/sebi-monthly-portfolio-as-on-march-2024.zip"),
    ("2024-04", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2024/sebi_monthly_portfolio-30-apr-2024.zip"),
    ("2024-05", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2024/sebi_monthly_portfolio-31-may-2024.zip"),
    ("2024-06", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2024/sebi_monthly_portfolio-30-june-2024.zip"),
    ("2024-07", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2024/sebi_monthly_portfolio-31-july-2024_r.xls"),
    ("2024-08", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2024/sebi_monthly_portfolio-31-aug-2024.zip"),
    ("2024-09", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2024/sebi_monthly_portfolio-30-sep-2024.zip"),
    ("2024-10", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2024/sebi_monthly_portfolio-31-oct-2024.zip"),
    ("2024-11", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2024/sebi_monthly_portfolio-30-nov-2024.zip"),
    ("2024-12", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2024/sebi_monthly_portfolio-31-dec-2024.zip"),
    # 2025
    ("2025-01", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2025/sebi_monthly_portfolio-31-jan-2025.zip"),
    ("2025-02", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2025/sebi_monthly_portfolio-28-feb-2025.zip"),
    ("2025-03", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2025/sebi_monthly_portfolio-31-mar-2025.zip"),
    ("2025-04", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2025/sebi_monthly_portfolio-30-apr-2025.zip"),
    ("2025-05", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2025/sebi_monthly_portfolio-31-may-2025.zip"),
    ("2025-06", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2025/sebi_monthly_portfolio-30-june-2025.zip"),
    ("2025-07", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2025/sebi_monthly_portfolio-31-july-2025.zip"),
    ("2025-08", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2025/sebi_monthly_portfolio-31-aug-2025.zip"),
    ("2025-09", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2025/sebi_monthly_portfolio--30-sep-2025.zip"),
    ("2025-10", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2025/sebi_monthly_portfolio-31-oct-2025-2.xls"),
    ("2025-11", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2025/sebi_monthly_portfolio-30-nov-2025.zip"),
    ("2025-12", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2025/sebi_monthly_portfolio-31-dec-2025-1.zip"),
    # 2026
    ("2026-01", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2026/sebi_monthly_portfolio-31-jan-2026.zip"),
    ("2026-02", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2026/sebi_monthly_portfolio-28-feb-2026.zip"),
    ("2026-03", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2026/monthly-portfolio-mar-2026.zip"),
    ("2026-04", "https://mutualfund.adityabirlacapital.com/-/media/bsl/files/resources/monthly-portfolio/2026/monthly-disclosure-april-30-2026.zip"),
]


def get_xls_from_url(session: requests.Session, url: str, ym: str) -> bytes | None:
    """Download URL; if ZIP extract the .xls/.xlsx inside; return raw bytes."""
    try:
        r = session.get(url, timeout=90)
        if r.status_code != 200:
            log.warning("  HTTP %d for %s", r.status_code, url.split("/")[-1])
            return None
        data = r.content
    except Exception as e:
        log.warning("  Download error %s: %s", ym, e)
        return None

    if data[:4] == b'PK\x03\x04':
        # It's a ZIP — extract the xls/xlsx inside
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                xls_names = [n for n in zf.namelist()
                             if n.lower().endswith(('.xls', '.xlsx'))
                             and '__MACOSX' not in n]
                if not xls_names:
                    log.warning("  No .xls in ZIP for %s", ym)
                    return None
                return zf.read(xls_names[0])
        except Exception as e:
            log.warning("  ZIP extract error %s: %s", ym, e)
            return None

    # Raw .xls
    return data


def already_ingested(ym: str) -> bool:
    """True if Aditya_Birla rows already exist in this month's parquet."""
    import pandas as pd
    parq = HOLD_DIR / f"{ym}.parquet"
    if not parq.exists():
        return False
    df = pd.read_parquet(parq, columns=["amc"])
    return "Aditya_Birla" in df["amc"].values


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force",   action="store_true",
                    help="Re-ingest months that already have ABSL data")
    args = ap.parse_args()

    # Import parsers from existing script
    sys.path.insert(0, str(OUTPUTS_DIR))
    from ingest_absl_kotak import parse_absl_file, _merge_df_into_cache
    import pandas as pd

    scheme_df = pd.read_parquet(SCHEME_PATH) if SCHEME_PATH.exists() else None

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (FundLens/1.0)"})

    results = []
    for ym, url in sorted(ABSL_URLS):
        if not args.force and already_ingested(ym):
            log.info("  ○  %s  already ingested — skip", ym)
            results.append((ym, "skip"))
            continue

        log.info("↓  %s  %s", ym, url.split("/")[-1])
        if args.dry_run:
            results.append((ym, "ok(dry)"))
            continue

        xls_data = get_xls_from_url(session, url, ym)
        if xls_data is None:
            results.append((ym, "fail"))
            continue

        df, parsed_ym = parse_absl_file(xls_data)
        if df is None or df.empty:
            log.warning("  Parse returned empty for %s", ym)
            results.append((ym, "empty"))
            continue

        # Use filename-derived ym (more reliable than sheet date for edge cases)
        actual_ym = parsed_ym or ym
        if actual_ym != ym:
            log.warning("  Date mismatch: URL says %s, sheet says %s — using %s", ym, parsed_ym, ym)
            actual_ym = ym

        saved = _merge_df_into_cache(
            df, actual_ym, "Aditya_Birla", HOLD_DIR,
            scheme_df, "ADITYA BIRLA", False
        )
        status = "ok" if saved else "skip"
        log.info("  %s  %s  [%s]  %d rows", "✓" if saved else "○", actual_ym, status, len(df))
        results.append((ym, status))

    ok   = sum(1 for _, s in results if s == "ok")
    skip = sum(1 for _, s in results if s == "skip")
    fail = sum(1 for _, s in results if s in ("fail", "empty"))
    log.info("Done: %d ingested, %d skipped, %d failed", ok, skip, fail)


if __name__ == "__main__":
    main()
