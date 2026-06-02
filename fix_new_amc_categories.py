"""
fix_new_amc_categories.py
-------------------------
Fetches proper SEBI scheme categories for new AMC schemes from mfapi.in
and updates fund_meta.parquet. Uses parallel requests.
"""
import time, re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

DATA_DIR = Path('/sessions/admiring-nifty-dijkstra/mnt/outputs/mf_data')
MFAPI    = "https://api.mfapi.in/mf"
WORKERS  = 20

# Map mfapi scheme_category string → our internal category value
def parse_category(scheme_category: str) -> str:
    s = scheme_category.lower()
    # Extract part after last " - "
    if ' - ' in s:
        sub = s.split(' - ')[-1].strip()
    else:
        sub = s.strip()

    # Match to known categories
    if 'large cap' in sub and 'mid' not in sub:
        return 'Large Cap Fund'
    elif 'mid cap' in sub and 'large' not in sub:
        return 'Mid Cap Fund'
    elif 'small cap' in sub:
        return 'Small Cap Fund'
    elif 'large & mid cap' in sub or 'large and mid cap' in sub:
        return 'Large & Mid Cap Fund'
    elif 'flexi cap' in sub:
        return 'Flexi Cap Fund'
    elif 'multi cap' in sub:
        return 'Multi Cap Fund'
    elif 'focused' in sub:
        return 'Focused Fund'
    elif 'value fund' in sub:
        return 'Value Fund'
    elif 'contra' in sub:
        return 'Contra Fund'
    elif 'elss' in sub or 'tax saver' in sub or 'tax saving' in sub:
        return 'ELSS'
    elif 'sectoral' in sub or 'thematic' in sub:
        return 'Sectoral/ Thematic'
    elif 'balanced advantage' in sub or 'dynamic asset allocation' in sub:
        return 'Balanced Advantage'
    elif 'aggressive hybrid' in sub:
        return 'Aggressive Hybrid Fund'
    elif 'equity savings' in sub:
        return 'Equity Savings'
    elif 'arbitrage' in sub:
        return 'Arbitrage Fund'
    elif 'retirement' in sub:
        return 'Retirement Fund'
    elif 'index' in sub:
        return 'Index Funds'
    elif 'etf' in sub:
        return 'ETF'
    elif 'fund of fund' in sub or 'fof' in sub:
        return 'Fund of Funds'
    elif 'equity' in sub:
        return 'equity'   # generic — still better than nothing
    else:
        return 'equity'

def fetch_category(code):
    try:
        r = requests.get(f"{MFAPI}/{code}", timeout=15,
                         headers={"User-Agent": "FundLens/1.0"})
        if r.status_code == 404:
            return code, None
        r.raise_for_status()
        meta = r.json().get("meta", {})
        scheme_cat = meta.get("scheme_category", "")
        return code, parse_category(scheme_cat) if scheme_cat else None
    except:
        return code, None

def main():
    fm = pd.read_parquet(DATA_DIR / 'fund_meta.parquet')
    new_amcs = ['Quant', 'UTI', 'Bandhan', 'Tata', 'Motilal']
    mask = fm['amc'].isin(new_amcs) & (fm['category'] == 'equity')
    codes = fm.loc[mask, 'scheme_code'].dropna().astype(int).tolist()
    print(f"Fetching categories for {len(codes)} new AMC schemes …")

    t0 = time.time()
    results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_category, c): c for c in codes}
        for fut in as_completed(futs):
            code, cat = fut.result()
            if cat:
                results[code] = cat

    print(f"Got categories for {len(results)}/{len(codes)} schemes in {time.time()-t0:.1f}s")

    # Show category distribution
    from collections import Counter
    dist = Counter(results.values())
    print("\nCategory distribution:")
    for cat, n in sorted(dist.items(), key=lambda x: -x[1]):
        print(f"  {cat:<35} {n}")

    # Update fund_meta
    updated = 0
    for idx, row in fm.iterrows():
        code = row.get('scheme_code')
        if pd.notna(code) and int(code) in results:
            fm.at[idx, 'category'] = results[int(code)]
            updated += 1

    fm.to_parquet(DATA_DIR / 'fund_meta.parquet', index=False)
    print(f"\nUpdated {updated} rows in fund_meta.parquet")

if __name__ == '__main__':
    main()
