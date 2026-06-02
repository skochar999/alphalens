"""
fetch_nav_fast.py — parallel NAV fetch for new AMC scheme codes
Uses ThreadPoolExecutor with 20 workers to fetch from mfapi.in
"""
import time, json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

DATA_DIR = Path('/sessions/admiring-nifty-dijkstra/mnt/outputs/mf_data')
MFAPI    = "https://api.mfapi.in/mf"
WORKERS  = 20

def fetch_one(code):
    try:
        r = requests.get(f"{MFAPI}/{code}", timeout=15,
                         headers={"User-Agent": "FundLens/1.0"})
        if r.status_code == 404:
            return code, None
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return code, None
        records = {}
        for row in data:
            try:
                d   = pd.to_datetime(row["date"], format="%d-%m-%Y")
                nav = float(row["nav"])
                records[d] = nav
            except:
                continue
        if not records:
            return code, None
        s = pd.Series(records).sort_index()
        s.index = pd.DatetimeIndex(s.index)
        monthly = s.resample("ME").last().dropna()
        return code, monthly
    except Exception as e:
        return code, None

def main():
    meta = pd.read_parquet(DATA_DIR / 'fund_meta.parquet')
    nav_path = DATA_DIR / 'nav_monthly.parquet'
    existing = pd.read_parquet(nav_path) if nav_path.exists() else pd.DataFrame()
    if not existing.empty:
        existing.index = pd.DatetimeIndex(existing.index)

    fund_codes  = meta['scheme_code'].dropna().astype(int).unique().tolist()
    proxy_codes = meta['proxy_code'].dropna().astype(int).unique().tolist()
    all_codes   = list(set(fund_codes + proxy_codes))
    to_fetch    = [c for c in all_codes if c not in existing.columns]
    
    print(f"Fetching {len(to_fetch)} schemes with {WORKERS} workers …")
    t0 = time.time()
    
    new_series = {}
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_one, c): c for c in to_fetch}
        for fut in as_completed(futs):
            code, series = fut.result()
            if series is not None and len(series) > 0:
                new_series[code] = series
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(to_fetch)} done ({len(new_series)} with data)")

    print(f"Fetched {len(new_series)}/{len(to_fetch)} in {time.time()-t0:.1f}s")

    if not new_series:
        print("No new data — nothing to save")
        return

    new_df = pd.DataFrame(new_series)
    new_df.index = pd.DatetimeIndex(new_df.index)

    if existing.empty:
        combined = new_df
    else:
        combined = existing.reindex(existing.index.union(new_df.index)).copy()
        for code, series in new_series.items():
            combined.loc[series.index, code] = series.values

    combined.sort_index(inplace=True)
    combined.to_parquet(nav_path)
    print(f"Saved nav_monthly: {combined.shape[0]} months × {combined.shape[1]} schemes")
    print(f"  Date range: {combined.index.min().date()} → {combined.index.max().date()}")

if __name__ == '__main__':
    main()
