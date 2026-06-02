#!/usr/bin/env python3
"""
Patch Edelweiss scheme_code values across all holdings parquets.

The original ingest used fuzzy word-overlap matching which collapsed many
Edelweiss schemes to scheme_code 140196 (Liquid Fund).

This script:
1. Downloads AMFI NAVAll.txt
2. Builds a Direct-Plan-preferring base_name → scheme_code map for Edelweiss
3. Normalizes every Edelweiss scheme_name in the holdings parquets
4. Applies correct scheme_codes
"""

import re, glob, sys
import pandas as pd
import requests
from pathlib import Path

HOLD = Path('mf_data/holdings')

# ─── Step 1: Build AMFI map ─────────────────────────────────────────────────

def strip_plan_suffix(full_name: str) -> str:
    """Extract the base fund name (before any plan/option qualifiers)."""
    stop_tokens = {
        'direct plan', 'regular plan', 'retail plan', 'institutional plan',
        'direct', 'regular', 'growth option', 'growth', 'idcw', 'dividend',
        'bonus option', 'bonus', 'weekly', 'monthly', 'daily', 'annual',
        'fortnightly', 'quarterly', 'payout', 'reinvestment',
    }
    parts = re.split(r'\s*[-–]\s*', full_name)
    result = []
    for p in parts:
        pl = p.strip().lower()
        if pl in stop_tokens:
            break
        # also break if the token IS a plan marker as a standalone segment
        if any(pl == tok for tok in stop_tokens):
            break
        result.append(p.strip())
    return ' - '.join(result) if result else full_name


def norm(s: str) -> str:
    """Lowercase, remove all non-alphanumeric except space, collapse whitespace."""
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]', ' ', s.lower())).strip()


def build_amfi_map():
    """Return dict: norm(base_name) -> scheme_code, preferring Direct Plan."""
    print("Downloading AMFI NAVAll.txt …")
    r = requests.get('https://www.amfiindia.com/spages/NAVAll.txt', timeout=30)
    r.raise_for_status()
    lines = r.text.splitlines()

    # First pass: collect all Edelweiss entries
    entries = []  # (code, base_norm, is_direct)
    for line in lines:
        parts = line.split(';')
        if len(parts) < 4:
            continue
        try:
            code = int(parts[0].strip())
        except ValueError:
            continue
        full = parts[3].strip()
        fl = full.lower()
        if 'edelweiss' not in fl and ('bharat bond' not in fl):
            continue
        base = strip_plan_suffix(full)
        bn = norm(base)
        if not bn:
            continue
        is_direct = 'direct' in fl.split(' - ') or 'direct plan' in fl or \
                    re.search(r'\bdirect\b', fl) is not None
        # Only keep Growth / plain (no IDCW/Dividend) entries to avoid dupe codes
        is_growth = 'growth' in fl or ('idcw' not in fl and 'dividend' not in fl and
                                       'bonus' not in fl and 'weekly' not in fl and
                                       'monthly' not in fl and 'daily' not in fl and
                                       'annual' not in fl and 'fortnightly' not in fl and
                                       'quarterly' not in fl and 'payout' not in fl)
        if not is_growth:
            continue
        entries.append((code, bn, is_direct))

    # Second pass: build map preferring Direct Plan
    amfi_map = {}  # base_norm -> (code, is_direct)
    for code, bn, is_direct in entries:
        existing = amfi_map.get(bn)
        if existing is None:
            amfi_map[bn] = (code, is_direct)
        elif is_direct and not existing[1]:
            # Overwrite a Regular-plan entry with a Direct-plan entry
            amfi_map[bn] = (code, is_direct)

    print(f"  AMFI Edelweiss base names: {len(amfi_map)}")
    return amfi_map


# ─── Step 2: Normalize holding scheme names ─────────────────────────────────

def normalize_holding_name(raw: str) -> str:
    """
    Convert raw scheme_name as it appears in the holdings to a clean fund name.

    Cases:
    - "PORTFOLIO STATEMENT OF EDELWEISS FLEXI-CAP FUND AS ON SEPTEMBER 30, 2025"
      → "Edelweiss Flexi-Cap Fund"
    - "Edelweiss ELSS Tax saver Fund - Direct Plan - Growth"
      → "Edelweiss ELSS Tax saver Fund"
    - Short codes like "EDBE25", "AEHYLS" are left unchanged (will get no match)
    """
    s = raw.strip()
    # Pattern 1: "PORTFOLIO STATEMENT OF ... AS ON ..."
    m = re.match(
        r'PORTFOLIO\s+STATEMENT\s+OF\s+(.+?)\s+AS\s+ON\s+',
        s, re.IGNORECASE
    )
    if m:
        fund_part = m.group(1).strip()
        # Title-case it
        s = fund_part.title()
        # Fix common capitalisation: keep hyphens, handle &
        s = re.sub(r'\bAnd\b', 'and', s)
        s = re.sub(r'\bOf\b', 'of', s)
        s = re.sub(r'\b(Etf|Fof|Nav|Psu|Ibb|Sdl|Ibx|Aaa|Nbfc|Hfc|Crl|Nifty|Elss|Bse|Nse)\b',
                   lambda m: m.group(1).upper(), s)
        return s

    # Pattern 2: "Edelweiss XYZ - Direct Plan ..." → strip plan suffix
    return strip_plan_suffix(s)


# ─── Step 3: Lookup code ────────────────────────────────────────────────────

def lookup_code(scheme_name: str, amfi_map: dict):
    """
    Try to find a scheme_code for a holding's scheme_name.
    Returns (code, matched_key) or (None, None).
    """
    clean = normalize_holding_name(scheme_name)
    cn = norm(clean)

    # 1. Exact match
    if cn in amfi_map:
        return amfi_map[cn][0], cn

    # 2. Prefix: the normalized holding name is a prefix of an AMFI key
    # e.g. "edelweiss consumption fund" vs "edelweiss consumption fund  direct"
    matches = [(k, v) for k, v in amfi_map.items() if k.startswith(cn + ' ')]
    if matches:
        # Prefer Direct
        direct = [(k, v) for k, v in matches if v[1]]
        best = direct[0] if direct else matches[0]
        return best[1][0], best[0]

    # 3. AMFI key is a prefix of the holding name
    # (shouldn't happen often but catches abbreviations)
    matches2 = [(k, v) for k, v in amfi_map.items() if cn.startswith(k + ' ')]
    if matches2:
        # Longest match wins
        best = max(matches2, key=lambda x: len(x[0]))
        return best[1][0], best[0]

    # 4. Word-overlap but ONLY among Edelweiss schemes (more restrictive)
    cn_words = set(cn.split())
    best_score, best_code, best_key = 0, None, None
    for k, (code, is_d) in amfi_map.items():
        k_words = set(k.split())
        overlap = cn_words & k_words
        # Exclude common words that appear in every scheme name
        meaningful = overlap - {'edelweiss', 'fund', 'the', 'of', 'and', 'for',
                                 'bharat', 'edel', 'a', 'an', 'in', 'plan', 'fof'}
        score = len(meaningful)
        if score > best_score or (score == best_score and is_d and best_code is not None):
            best_score = score
            best_code = code
            best_key = k
    # Only accept if meaningful overlap >= 2 to avoid false positives
    if best_score >= 2:
        return best_code, best_key

    return None, None


# ─── Step 4: Patch all parquets ─────────────────────────────────────────────

def patch_holdings(amfi_map: dict, dry_run: bool = False):
    files = sorted(HOLD.glob('*.parquet'))
    print(f"\nPatching {len(files)} holdings files …")

    total_fixed = 0
    total_unfixed = 0
    unfixed_names = set()

    for fpath in files:
        df = pd.read_parquet(fpath)
        edel_mask = df['amc'] == 'Edelweiss'
        if not edel_mask.any():
            continue

        changed = False
        for idx in df[edel_mask].index:
            sname = df.at[idx, 'scheme_name']
            code, matched = lookup_code(sname, amfi_map)
            if code is not None:
                old_code = df.at[idx, 'scheme_code']
                if pd.isna(old_code) or int(old_code) != code:
                    df.at[idx, 'scheme_code'] = float(code)
                    changed = True
                    total_fixed += 1
            else:
                unfixed_names.add(sname)
                total_unfixed += 1

        if changed and not dry_run:
            df.to_parquet(fpath, index=False)
            print(f"  updated {fpath.name}")

    print(f"\nFixed: {total_fixed} rows")
    print(f"Unfixed: {total_unfixed} rows, {len(unfixed_names)} unique names")
    if unfixed_names:
        print("Unfixed scheme names:")
        for s in sorted(unfixed_names):
            print(f"  '{s}'")


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    dry_run = '--dry-run' in sys.argv

    amfi_map = build_amfi_map()

    # Smoke test
    tests = [
        ("Edelweiss ELSS Tax saver Fund",           118620),
        ("Edelweiss Flexi-Cap Fund",                 140353),
        ("Edelweiss Large Cap Fund",                 118617),
        ("PORTFOLIO STATEMENT OF EDELWEISS MID CAP FUND AS ON NOVEMBER 30, 2025", 140228),
        ("PORTFOLIO STATEMENT OF EDELWEISS FLEXI-CAP FUND AS ON DECEMBER 31, 2025", 140353),
        ("PORTFOLIO STATEMENT OF EDELWEISS ARBITRAGE FUND AS ON OCTOBER 31, 2023",  130206),
        ("PORTFOLIO STATEMENT OF EDELWEISS BALANCED ADVANTAGE FUND AS ON NOVEMBER 30, 2025", 118615),
        ("Edelweiss Mid Cap Fund",                   140228),
        ("Edelweiss Arbitrage Fund",                 130206),
        ("Edelweiss Balanced Advantage Fund",        118615),
        ("Edelweiss Liquid Fund",                    140196),  # Direct Plan Growth = 140196
        ("Edelweiss Small Cap Fund",                 146196),
        ("Edelweiss Consumption Fund",               153214),
    ]

    print("\n=== Smoke test ===")
    ok = 0
    for name, expected in tests:
        got, key = lookup_code(name, amfi_map)
        mark = '✓' if got == expected else '✗'
        if got == expected:
            ok += 1
        print(f"  {mark} got={got} want={expected} | name='{name}' | matched='{key}'")

    print(f"\n{ok}/{len(tests)} smoke tests passed")

    if ok < len(tests):
        print("Some smoke tests failed — fix the map before patching.")
        if '--force' not in sys.argv:
            sys.exit(1)

    print(f"\nDry run: {dry_run}")
    patch_holdings(amfi_map, dry_run=dry_run)
