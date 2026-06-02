# FundLens — Project State
_Last updated: 2026-05-28_

## What this project is
FundLens is a free Indian equity mutual fund analytics website. It scores 434 funds across 21 AMCs using Brinson attribution (stock selection alpha, beat rate, active return vs benchmark, cost). The eventual goal is to add mutual fund purchases from the site, making it a distribution platform.

User: Siddhartha Kochar (skochar999@gmail.com)

---

## Key files (all in `/outputs/`)

| File | Purpose |
|---|---|
| `index.html` / `fundlens.html` | The live website — open directly in browser |
| `build_fundlens.py` | Builds the HTML from parquet data |
| `compute_scored_funds.py` | Scoring engine — produces `scored_funds.parquet` |
| `compute_benchmark_metrics.py` | Benchmark-relative metrics — produces `benchmark_metrics.parquet` |
| `holdings_attribution.py` | Brinson attribution engine — produces `holdings_attribution.parquet` |
| `fetch_nav_fast.py` | Downloads NAV history from AMFI |
| `fix_new_amc_categories.py` | Fixes SEBI categories for new AMCs |
| `fix_edelweiss_codes.py` | Patches Edelweiss scheme_code collisions |
| `ingest_next5amcs_v2.py` | Ingest scripts for Baroda_BNP, Edelweiss, HSBC, Sundaram, WhiteOak |

---

## Data directory: `mf_data/`

| File | Description |
|---|---|
| `scored_funds.parquet` | 434 funds, final scores + decomposition |
| `benchmark_metrics.parquet` | 439 funds, benchmark-relative metrics |
| `holdings_attribution.parquet` | 10,469 monthly Brinson attribution rows |
| `fund_meta.parquet` | AMFI scheme master — categories, TER, benchmarks |
| `nav_monthly.parquet` | Monthly NAV returns, 644 schemes × 161 months |
| `holdings/YYYY-MM.parquet` | Monthly holdings parquets per period |
| `fund_metrics.parquet` | Factor regression metrics (175 legacy funds) |

---

## 21 AMCs covered

**Original 11:** Franklin, Nippon, Mirae, HDFC, DSP, Aditya Birla, SBI, Kotak, ICICI Pru, Axis, PPFAS

**Added (Tasks 70–75):** Tata, UTI, Bandhan, Quant, Motilal, Mirae (extended), SBI (extended)

**Added (Tasks 79–81):** Baroda BNP Paribas, Edelweiss, HSBC, Sundaram, WhiteOak Capital

---

## Pipeline execution order (run after new holdings added)

```bash
cd /outputs

# 1. Fetch latest NAVs
python3 fetch_nav_fast.py

# 2. Fix categories for any new AMC schemes
python3 fix_new_amc_categories.py

# 3. Rebuild benchmark metrics (root table for scorer)
python3 compute_benchmark_metrics.py

# 4. Run Brinson attribution
python3 holdings_attribution.py

# 5. Score all funds
python3 compute_scored_funds.py

# 6. Build dashboard HTML
python3 build_fundlens.py
cp fundlens.html index.html
```

---

## Current dashboard stats
- **434 funds** (439 minus 5 gold/silver ETFs filtered at build time)
- **267** scored with holdings-aware formula (stock pick alpha dominant)
- **92** NAV-only fallback (no/insufficient holdings)
- **80** index funds (scored on TER + tracking accuracy)
- **294** with full return decomposition (style / sector / stock-pick / timing)

---

## Scoring formula

**Active funds WITH holdings (267 funds):**
- 40% Stock-selection alpha annualised (pick_ann_pp from Brinson)
- 20% Pick hit rate (% months positive stock selection)
- 25% Net active return vs benchmark (after fees)
- 5% Style stability (low beta drift)
- 10% Cost (low TER)

**Active funds WITHOUT holdings (92 funds):**
- 40% Net active return vs benchmark
- 35% Hit rate (% months beating benchmark)
- 15% Style stability
- 10% Cost

**Index funds (80 funds):**
- 60% Cost (TER)
- 40% Tracking accuracy

---

## Table columns (in order)
`#` | Fund | FundLens Score | Vs Benchmark/yr | Beat Rate | Stock Pick/yr | Expense Ratio | Total Return/yr

## Filters available
Search | AMC | Category | Min Score

---

## Known data gaps / caveats
- **HSBC**: Only 2025-01 onwards (16 months) — earlier URL pattern returns 404
- **Baroda BNP**: 2023-11 onwards (15 months) — website only hosts recent files
- **WhiteOak**: Has some broken xlsx months (silently skipped)
- **Edelweiss**: 2 AIFs (AEHYLS, "Altiva Hybrid Long-Short Fund") have no AMFI code — intentional
- **29 "Other" funds**: Debt/FMP/G-sec funds that ended up in scored_funds — show in "Other" category, do not affect equity rankings
- `fund_metrics.parquet` (regression-based alpha, beta_S, beta_I) only covers original 175 funds — not extended to new AMCs

---

## Deployment plan
- Target hosting: **Netlify** (drag-and-drop `index.html`)
- Domain: To be purchased — candidates: `fundlens.in`, `alphafunds.in`
- Future: Add mutual fund purchases (requires SEBI ARN / MFD registration or white-label via MFU/BSE StAR MF)
- UI redesign: Consider **v0.dev** (Vercel) for React + Tailwind rebuild

---

## Working style notes
- User prefers things done directly without asking too many upfront questions for straightforward tasks
- Shell timeout is ~44 seconds — long scripts must use `--chunk N` / `--from-period` flags
- Always run `compute_benchmark_metrics.py` before `compute_scored_funds.py` when new AMCs are added
- After scoring, always `cp fundlens.html index.html`
- Gold/silver ETFs are excluded at build time in `build_fundlens.py` (not from the parquet)
