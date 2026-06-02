"""
probe_next5amcs.py
------------------
Probe AdvisorKhoj + AMC websites for the next 5 AMCs portfolio disclosure URLs.
Checks: Baroda BNP Paribas, HSBC, Invesco India, Canara Robeco, WhiteOak Capital.
Also checks Sundaram and Edelweiss as backup candidates.
"""
import requests, re, json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
}

AMC_PAGES = {
    'Baroda_BNP': [
        'https://www.advisorkhoj.com/form-download-centre/Mutual/Baroda-BNP-Paribas-Mutual-Fund/Monthly-Portfolio-Disclosures',
        'https://www.barodabnpparibasmf.in/investor-services/portfolio-disclosure',
        'https://www.barodabnpparibasmf.in/monthly-portfolio',
    ],
    'HSBC': [
        'https://www.advisorkhoj.com/form-download-centre/Mutual/HSBC-Mutual-Fund/Monthly-Portfolio-Disclosures',
        'https://www.assetmanagement.hsbc.co.in/en/mutual-funds/tools-and-resources/portfolio-disclosures',
    ],
    'Invesco': [
        'https://www.advisorkhoj.com/form-download-centre/Mutual/Invesco-Mutual-Fund/Monthly-Portfolio-Disclosures',
        'https://www.invescomutualfund.com/investor-services/monthly-portfolio-statement',
    ],
    'Canara_Rob': [
        'https://www.advisorkhoj.com/form-download-centre/Mutual/Canara-Robeco-Mutual-Fund/Monthly-Portfolio-Disclosures',
        'https://www.canararobeco.com/investor-service/portfolio-disclosure',
    ],
    'WhiteOak': [
        'https://www.advisorkhoj.com/form-download-centre/Mutual/WhiteOak-Capital-Mutual-Fund/Monthly-Portfolio-Disclosures',
        'https://www.whiteoakmf.com/monthly-portfolio-disclosure',
    ],
    'Sundaram': [
        'https://www.advisorkhoj.com/form-download-centre/Mutual/Sundaram-Mutual-Fund/Monthly-Portfolio-Disclosures',
        'https://www.sundarammutual.com/portfolio-disclosure',
    ],
    'Edelweiss': [
        'https://www.advisorkhoj.com/form-download-centre/Mutual/Edelweiss-Mutual-Fund/Monthly-Portfolio-Disclosures',
        'https://www.edelweissmf.com/investor-services/portfolio-disclosure',
    ],
}

def probe_url(url, name):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return url, r.status_code, []
        text = r.text
        # Find all file download links
        file_links = re.findall(
            r'(?:href|src|url)[=:\s]+["\']?(https?://[^\s"\'<>]+\.(?:xlsx|xls|pdf|zip))',
            text, re.I
        )
        # Also bare URLs in JS/onclick
        bare_links = re.findall(
            r'https?://[^\s"\'<>]+\.(?:xlsx|xls|pdf|zip)',
            text, re.I
        )
        combined = list(dict.fromkeys(file_links + bare_links))
        # Sample unique domain patterns
        domains = list(dict.fromkeys(
            re.sub(r'(https?://[^/]+/).*', r'\1', lnk) for lnk in combined
        ))
        return url, r.status_code, combined[:8], domains
    except Exception as e:
        return url, f'ERR', [], []

results = {}
tasks = []
for amc, urls in AMC_PAGES.items():
    for url in urls:
        tasks.append((amc, url))

print("Probing AMC disclosure pages ...\n")
with ThreadPoolExecutor(max_workers=10) as ex:
    futs = {ex.submit(probe_url, url, amc): (amc, url) for amc, url in tasks}
    for fut in as_completed(futs):
        amc, url = futs[fut]
        result = fut.result()
        if amc not in results:
            results[amc] = []
        results[amc].append(result)

for amc in AMC_PAGES:
    print(f"\n{'='*60}")
    print(f"  {amc}")
    print(f"{'='*60}")
    for res in results.get(amc, []):
        url, status, links, domains = res if len(res) == 4 else (*res, [])
        short_url = url[:70]
        print(f"\n  [{status}] {short_url}")
        for lnk in links[:4]:
            print(f"    FILE: {lnk[:85]}")
        if not links:
            print("    (no file links)")
