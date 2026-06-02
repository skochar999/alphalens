#!/usr/bin/env python3
"""
probe_amc_deep.py
=================
Deeper probe: extract portfolio-related links from each AMC page,
try known direct CDN/API patterns, and check NSE MF archives.
"""
import subprocess, re, sys, json

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

def curl_get(url, max_time=25, extra_args=None):
    cmd = ["curl", "-s", "-L", "--max-time", str(max_time), "-A", UA]
    if extra_args:
        cmd += extra_args
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=max_time + 5)
        return r.stdout
    except Exception as e:
        return f"ERROR: {e}"

def curl_head(url, max_time=15):
    """Returns HTTP status code."""
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(max_time), "-A", UA, "-L", url],
            capture_output=True, text=True, timeout=max_time + 5
        )
        return r.stdout.strip()
    except:
        return "ERR"

def portfolio_links(html, base_url=""):
    """Extract any link that looks portfolio/download related."""
    all_links = re.findall(r'href=["\']([^"\']+)["\']', html)
    keywords = ["portfolio", "download", "disclosure", "holding", ".xlsx", ".xls", ".csv", ".txt", ".zip"]
    found = []
    for l in all_links:
        if any(k in l.lower() for k in keywords):
            found.append(l)
    return list(dict.fromkeys(found))  # deduplicate preserving order

# ─────────────────────────────────────────────────────────────────────────────
# 1. Deep look at DSP (377K page)
# ─────────────────────────────────────────────────────────────────────────────
print("=" * 68)
print("[1] DSP deep scan …")
dsp = curl_get("https://www.dspim.com/downloads?category=portfolio-disclosure")
links = portfolio_links(dsp)
print(f"  {len(dsp):,} bytes | {len(links)} portfolio-related links:")
for l in links[:20]:
    print(f"    {l}")

# Also check if DSP has a JSON API endpoint
print("\n  Trying DSP API …")
dsp_api = curl_get("https://www.dspim.com/api/downloads?category=portfolio-disclosure")
if "xlsx" in dsp_api.lower() or "xls" in dsp_api.lower():
    print("  ✅ DSP API returned download links!")
    print(dsp_api[:500])
else:
    print(f"  DSP API: {len(dsp_api)} bytes, no xlsx found")

# ─────────────────────────────────────────────────────────────────────────────
# 2. NSE India MF archives (known to have portfolio data)
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 68)
print("[2] NSE MF archives …")

nse_urls = [
    "https://archives.nseindia.com/content/mf/portfolio/",
    "https://archives.nseindia.com/content/mf/",
    "https://www.nseindia.com/products/content/equities/mf/mf_portfolio.htm",
    "https://nsearchives.nseindia.com/content/mf/",
]
for url in nse_urls:
    code = curl_head(url)
    print(f"  {code}  {url}")

# ─────────────────────────────────────────────────────────────────────────────
# 3. Try known direct CDN URL patterns for each AMC
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 68)
print("[3] Direct CDN / file URL patterns …")

import datetime
now = datetime.date.today()
# Generate month strings: Apr-2026, Mar-2026, etc.
months = []
for i in range(1, 4):
    d = datetime.date(now.year, now.month, 1) - datetime.timedelta(days=i*28)
    months.append((d.strftime("%B")[:3], d.year, d.strftime("%b")))

m0_full, m0_yr, m0_short = months[0]
m1_full, m1_yr, m1_short = months[1]

direct_patterns = [
    # SBI
    f"https://www.sbimf.com/Uploads/PortfolioDisclosure/Monthly/SBI-MF-Portfolio-{m0_full}-{m0_yr}.xlsx",
    f"https://www.sbimf.com/Uploads/PortfolioDisclosure/Monthly/SBI-MF-Portfolio-{m1_full}-{m1_yr}.xlsx",
    "https://www.sbimf.com/Uploads/PortfolioDisclosure/Monthly/",
    # ICICI Pru (try their CDN)
    f"https://www.icicipruamc.com/docs/portfolio-{m0_full.lower()}-{m0_yr}.xlsx",
    "https://www.icicipruamc.com/api/portfolio-disclosure",
    # Mirae - they use /docs/default-source/ pattern
    f"https://www.miraeassetmf.co.in/docs/default-source/portfolio-disclosures/mirae-asset-portfolio-{m0_full.lower()}-{m0_yr}.xlsx",
    "https://www.miraeassetmf.co.in/docs/default-source/portfolio-disclosures/",
    # Franklin (small page — might have direct link)
    "https://www.franklintempletonindia.com/investor/mutual-fund/portfolio-disclosure",
    "https://www.franklintempletonindia.com/api/download/portfolio",
    # Axis
    "https://www.axismf.com/api/portfolio-disclosure",
    f"https://www.axismf.com/downloads/portfolio/portfolio-disclosure-{m0_full.lower()}-{m0_yr}.xlsx",
    # ABSL
    "https://mutualfund.adityabirlacapital.com/api/portfolio-disclosure",
    f"https://mutualfund.adityabirlacapital.com/-/media/bsl/files/portfolio-disclosure/portfolio-{m0_full.lower()}-{m0_yr}.xlsx",
    # Nippon — they use SharePoint, try direct
    "https://mf.nipponindiaim.com/MediaGallery/DocumentGallery/PortfolioDisclosures/",
    # Kotak
    f"https://www.kotakmf.com/api/portfolio?month={m0_full}&year={m0_yr}",
]

for url in direct_patterns:
    code = curl_head(url)
    icon = "✅" if code == "200" else "❌"
    print(f"  {icon} {code}  {url.split('/')[-1] or url}")

# ─────────────────────────────────────────────────────────────────────────────
# 4. Try AMFI's new data pages more carefully
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 68)
print("[4] AMFI new data pages …")

for url in [
    "https://www.amfiindia.com/research-information/amfi-data",
    "https://www.amfiindia.com/otherdata",
    "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx",
]:
    html = curl_get(url, max_time=15)
    links = portfolio_links(html)
    api_links = [l for l in re.findall(r'href=["\']([^"\']+)["\']', html)
                 if "download" in l.lower() or ".xlsx" in l.lower() or ".txt" in l.lower()]
    print(f"\n  {url.split('/')[-1] or url} ({len(html):,} bytes):")
    for l in (links + api_links)[:8]:
        print(f"    {l}")

# ─────────────────────────────────────────────────────────────────────────────
# 5. Check Franklin's small page content
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 68)
print("[5] Franklin (small page — full content):")
franklin = curl_get("https://www.franklintempletonindia.com/investor/mutual-fund/portfolio-disclosure")
print(franklin[:3000])

print("\nDone.")
