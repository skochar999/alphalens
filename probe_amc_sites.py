#!/usr/bin/env python3
"""
probe_amc_sites.py
==================
Quick test: which AMC portfolio disclosure pages return useful HTML
(vs. blank React/Angular shells), and do they contain .xlsx/.csv links?

Run this first to plan which AMCs to build full scrapers for.
"""
import subprocess, re, sys
from urllib.parse import urljoin

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

AMC_PAGES = {
    "SBI":          "https://www.sbimf.com/en-us/portfolio-disclosures",
    "ICICI_Pru":    "https://www.icicipruamc.com/mutual-fund-investments/portfolio-disclosures",
    "HDFC":         "https://www.hdfcfund.com/tools-and-resources/portfolio-disclosure",
    "Kotak":        "https://www.kotakmf.com/service/portfolio-disclosure",
    "Aditya_Birla": "https://mutualfund.adityabirlacapital.com/Investor-Service/invest-in-mutual-funds/portfolio-disclosure",
    "Nippon":       "https://mf.nipponindiaim.com/investor-service/tools-and-downloads/portfolio-disclosures",
    "Axis":         "https://www.axismf.com/portfolio-disclosure",
    "DSP":          "https://www.dspim.com/downloads?category=portfolio-disclosure",
    "Franklin":     "https://www.franklintempletonindia.com/investor/mutual-fund/portfolio-disclosure",
    "Mirae":        "https://www.miraeassetmf.co.in/investor-service/portfolio-disclosures",
}

# Also try some known direct-download URL patterns
DIRECT_URLS = {
    "SBI_direct":       "https://www.sbimf.com/Uploads/PortfolioDisclosure",
    "Nippon_direct":    "https://mf.nipponindiaim.com/FundsandPerformance/Documents/PortfolioDisclosure",
    "AMFI_portal":      "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx",
    "AMFI_data":        "https://www.amfiindia.com/research-information/amfi-data",
    "AMFI_otherdata":   "https://www.amfiindia.com/otherdata",
}

def curl_fetch(url, max_time=20):
    try:
        r = subprocess.run(
            ["curl", "-s", "-L", "--max-time", str(max_time),
             "-A", UA, url],
            capture_output=True, text=True, timeout=max_time + 5
        )
        return r.stdout
    except Exception as e:
        return f"ERROR: {e}"

def analyse(name, url, html):
    size = len(html)
    is_react = any(x in html for x in ["__NEXT_DATA__", "ng-version", "ReactDOM", "__nuxt"])
    # Find download links
    xlsx_links = re.findall(r'href=["\']([^"\']*\.xlsx)["\']', html, re.I)
    xls_links  = re.findall(r'href=["\']([^"\']*\.xls)["\']',  html, re.I)
    csv_links  = re.findall(r'href=["\']([^"\']*\.csv)["\']',  html, re.I)
    txt_links  = re.findall(r'href=["\']([^"\']*portfolio[^"\']*\.(?:txt|zip))["\']', html, re.I)
    pdf_links  = re.findall(r'href=["\']([^"\']*portfolio[^"\']*\.pdf)["\']', html, re.I)

    all_dl = xlsx_links + xls_links + csv_links + txt_links + pdf_links

    status = "✅ USEFUL" if all_dl else ("⚠️  React/SPA" if is_react else "📄 HTML-no-links")

    print(f"\n{name:15s} [{size:7,} bytes] {status}")
    print(f"  URL: {url}")
    if all_dl:
        for link in all_dl[:5]:
            print(f"    → {link}")
    elif is_react:
        print(f"  (JS-rendered, no static download links found)")
    else:
        # Show any links at all
        any_links = re.findall(r'href=["\']([^"\']+)["\']', html)
        dl_like = [l for l in any_links if any(ext in l.lower() for ext in ['.xls', '.csv', '.txt', '.zip', '.pdf', 'download', 'portfolio'])]
        for l in dl_like[:5]:
            print(f"    link: {l}")

print("=" * 70)
print("Probing AMC portfolio disclosure pages …")
print("=" * 70)

for name, url in AMC_PAGES.items():
    print(f"\n  Fetching {name} …", end="", flush=True)
    html = curl_fetch(url)
    print(" done")
    analyse(name, url, html)

print("\n" + "=" * 70)
print("Probing direct/alternative URLs …")
print("=" * 70)

for name, url in DIRECT_URLS.items():
    print(f"\n  Fetching {name} …", end="", flush=True)
    html = curl_fetch(url)
    print(" done")
    analyse(name, url, html)

print("\nDone. Use results above to decide which AMCs to build full scrapers for.")
