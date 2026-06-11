PHASE 1 HOLDINGS — MANUAL SOURCING
==================================
The reliable catalogs (AdvisorKhoj) render their download links with JavaScript,
so they can't be scraped with plain HTTP. So we source the files by hand, ONCE,
per fund house. The engine (ingest_phase1.py) then does everything else:
reads each file's real month from its header, splits it into schemes, matches
each to its regular scheme code, and writes mf_data/holdings/{YYYY-MM}.parquet.

You do NOT need to label months — the month is read from inside each file.

------------------------------------------------------------------------
OPTION A — DROP FILES  (recommended; most robust)
------------------------------------------------------------------------
1. In your browser, open the fund house's Monthly Portfolio Disclosures page,
   e.g. AdvisorKhoj:
   https://www.advisorkhoj.com/form-download-centre/Mutual/<AMC-Name>-Mutual-Fund/Monthly-Portfolio-Disclosures
   (or the AMC's own statutory-disclosure page).
2. Download each month's FULL monthly portfolio Excel (NOT the "Additional
   Portfolio Disclosure"/top-10 summary — that one has no ISINs and won't parse).
3. Move all the downloaded .xls/.xlsx files into this folder, under the house key:
       mf_data/_phase1_drop/<key>/        e.g. mf_data/_phase1_drop/mahindra/
   Keys: mahindra, boi, iti, groww, shriram, 360one, bajaj, nj
4. Run:   python3 _phase1_run.py <key>            e.g.  python3 _phase1_run.py mahindra

------------------------------------------------------------------------
OPTION B — URL LIST
------------------------------------------------------------------------
1. Copy the direct file URLs (one per line) into:
       mf_data/_phase1_urls/<key>.txt
   Lines starting with # are ignored.
2. Run:   python3 _phase1_run.py <key>

------------------------------------------------------------------------
AFTER BACKFILLING a house, recompute so its schemes light up:
       python3 run_monthly_update.py --from-step 2
------------------------------------------------------------------------
Priority lookup (a file that loads but yields "no schemes matched" is usually a
top-10 summary, not the full portfolio). The engine prefers _phase1_drop over
_phase1_urls over auto-discovery.
