#!/usr/bin/env python3
"""
_phase1_run.py — clean + backfill one Phase-1 AMC, then report.
Usage:  python3 _phase1_run.py mahindra      (or any key in ingest_phase1.AMC_CONFIG)
Avoids shell-quoting headaches from inline one-liners.
"""
import sys
from pathlib import Path
import pandas as pd
import ingest_phase1 as p

key = sys.argv[1] if len(sys.argv) > 1 else "mahindra"
months_back = int(sys.argv[2]) if len(sys.argv) > 2 else 36
cfg = p.AMC_CONFIG[key]
label = cfg["label"]
HOLD = Path("mf_data/holdings")

# 1 ── remove any prior rows for this AMC (e.g. a mislabeled earlier run) ──
cleaned = 0
for f in sorted(HOLD.glob("*.parquet")):
    df = pd.read_parquet(f)
    mask = df["amc"].astype(str).str.contains(label, case=False, regex=False)
    if mask.any():
        df[~mask].to_parquet(f, index=False)
        cleaned += int(mask.sum())
print(f"cleaned {cleaned} prior {label} rows")

# 2 ── backfill (reads each file's real month from its header) ──
fm = pd.read_parquet("mf_data/fund_meta.parquet")
matcher = p.CodeMatcher(fm, cfg["meta"])
print(f"{matcher.n} regular {label} schemes in fund_meta")
stat = p.ingest_amc(key, p.recent_months(months_back), matcher, HOLD, dry_run=False)
print(f"\nSUMMARY  {stat['amc']}  schemes_matched={stat['schemes_matched']}  "
      f"rows={stat['rows']}  months={len(stat['months'])}")

# 3 ── report months populated ──
hits = []
for f in sorted(HOLD.glob("*.parquet")):
    n = int(pd.read_parquet(f, columns=["amc"])["amc"].astype(str)
            .str.contains(label, case=False, regex=False).sum())
    if n:
        hits.append((f.stem, n))
print(f"months populated with {label}: {len(hits)}")
print(hits)
