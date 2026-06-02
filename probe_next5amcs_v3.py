"""
probe_next5amcs_v3.py
---------------------
Systematically probe for monthly portfolio disclosure Excel files for:
Baroda BNP Paribas, HSBC, Invesco, Canara Robeco, WhiteOak Capital.

Strategy:
1. Probe AMFI portal for central disclosures
2. Try API endpoints that serve file lists (JSON)
3. Guess URL patterns for each AMC
4. Sample one recent file to confirm it's holdings data (not factsheet)
"""
import requests, re, json, io
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': '*/*',
}

def get(url, referer='', timeout=20):
    h = dict(HEADERS)
    if referer: h['Referer'] = referer
    try:
        r = requests.get(url, headers=h, timeout=timeout)
        return r.status_code, r.text, r.content
    except Exception as e:
        return 0, str(e), b''

def peek_excel(content, label=''):
    """Quick check of what an Excel file contains."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        flat = ' '.join(str(v) for row in rows[:15] for v in row if v)
        has_isin = bool(re.search(r'IN[A-Z0-9]{10}', flat))
        has_pct  = bool(re.search(r'% to NAV|pct|percent', flat, re.I))
        has_hold = bool(re.search(r'portfolio|holdings|instrument|equity', flat, re.I))
        print(f"    {label}: {len(rows)} rows, ISIN={has_isin}, %NAV={has_pct}, holdings={has_hold}")
        if rows:
            print(f"    Row[0]: {[str(v)[:20] for v in rows[0][:5]]}")
        return has_isin
    except Exception as e:
        print(f"    {label}: parse error — {e}")
        return False


# ════════════════════════════════════════════════════════════════
# 1. BARODA BNP PARIBAS
# ════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("1. BARODA BNP PARIBAS")
print("="*65)

# Try their API (React site typically uses /api/ routes)
baroda_apis = [
    'https://www.barodabnpparibasmf.in/api/portfolio-disclosure',
    'https://www.barodabnpparibasmf.in/api/documents',
    'https://www.barodabnpparibasmf.in/api/content/portfolio',
    'https://www.barodabnpparibasmf.in/api/monthly-portfolio',
    # Try their known download_documents path with portfolio keywords
    'https://www.barodabnpparibasmf.in/assets/download_documents/',
]
for url in baroda_apis:
    code, text, _ = get(url)
    if code == 200 and len(text) > 100:
        xlsx = re.findall(r'https?://[^\s"\'<>]+\.xlsx', text, re.I)
        print(f"  [200] {url[-50:]}: {xlsx[:2] if xlsx else text[:120]}")
    else:
        print(f"  [{code}] {url[-50:]}")

# Try direct URL patterns for recent months
baroda_patterns = [
    # Pattern from AdvisorKhoj fund facts - but for portfolio
    'https://www.barodabnpparibasmf.in/assets/download_documents/BBNPP_Monthly_Portfolio_April2026.xlsx',
    'https://www.barodabnpparibasmf.in/assets/download_documents/BBNPP_Monthly_Portfolio_April_2026.xlsx',
    'https://www.barodabnpparibasmf.in/assets/download_documents/Monthly_Portfolio_April2026.xlsx',
    'https://www.barodabnpparibasmf.in/assets/download_documents/Portfolio_April2026.xlsx',
    'https://www.barodabnpparibasmf.in/assets/pdf/MonthlyPortfolio_April2026.pdf',
    # Their fund facts pattern
    'https://www.barodabnpparibasmf.in/assets/download_documents/BBNPP_MF_Monthly_Portfolio_April_2026.xlsx',
    'https://www.barodabnpparibasmf.in/assets/download_documents/BBNPP_MonthlyPortfolio_Apr2026.xlsx',
]
print("\n  Direct URL guesses:")
for url in baroda_patterns:
    code, _, content = get(url, referer='https://www.barodabnpparibasmf.in/')
    if code == 200 and len(content) > 5000:
        print(f"  ✓ [{code}] {url[-70:]} ({len(content)} bytes)")
        peek_excel(content, 'sample')
    else:
        print(f"  ✗ [{code}] {url[-70:]}")


# ════════════════════════════════════════════════════════════════
# 2. HSBC
# ════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("2. HSBC")
print("="*65)

# Try HSBC portfolio disclosure section API
hsbc_apis = [
    'https://www.assetmanagement.hsbc.co.in/api/v1/documents?type=portfolio',
    'https://www.assetmanagement.hsbc.co.in/api/documents/portfolio',
    'https://www.assetmanagement.hsbc.co.in/en/mutual-funds/tools-and-resources/portfolio-disclosures',
    'https://www.assetmanagement.hsbc.co.in/en/mutual-funds/portfolio-disclosure',
]
for url in hsbc_apis:
    code, text, content = get(url, referer='https://www.assetmanagement.hsbc.co.in/')
    print(f"  [{code}] {url[-65:]}")
    if code == 200:
        xlsx = re.findall(r'https?://[^\s"\'<>]+\.xlsx', text, re.I)
        if xlsx: print(f"    xlsx: {xlsx[:2]}")
        # check if JSON
        try:
            d = json.loads(text)
            print(f"    JSON keys: {list(d.keys())[:5]}")
        except: pass

# Try to find HSBC portfolio disclosures on their known domain
# Their files are under /assets/documents/mutual-funds/en/ with GUIDs
# Check AdvisorKhoj for other HSBC file patterns
code, text, _ = get('https://www.advisorkhoj.com/form-download-centre/Mutual/HSBC-Mutual-Fund/Monthly-Portfolio-Disclosures')
# Look for any links that aren't factsheets
all_links = re.findall(r'https?://assetmanagement\.hsbc\.co\.in/[^\s"\'<>]+', text, re.I)
print(f"\n  HSBC links from AdvisorKhoj ({len(all_links)} total):")
for l in all_links[:6]: print(f"    {l[:90]}")

# Try HSBC portfolio URL patterns directly
hsbc_direct = [
    'https://www.assetmanagement.hsbc.co.in/assets/documents/mutual-funds/en/monthly-portfolio-april-2026.xlsx',
    'https://www.assetmanagement.hsbc.co.in/assets/documents/mutual-funds/en/hsbc-mf-portfolio-april-2026.xlsx',
    'https://www.assetmanagement.hsbc.co.in/assets/documents/mutual-funds/en/portfolio-disclosure-april-2026.xlsx',
]
for url in hsbc_direct:
    code, _, content = get(url, referer='https://www.assetmanagement.hsbc.co.in/')
    if code == 200 and len(content) > 5000:
        print(f"  ✓ {url[-65:]}")
    else:
        print(f"  ✗ [{code}] {url[-65:]}")

# Check their robots.txt / sitemap
code, text, _ = get('https://www.assetmanagement.hsbc.co.in/sitemap.xml')
if code == 200:
    xlsx_in_sitemap = re.findall(r'https?://[^\s<>]+\.xlsx', text, re.I)
    print(f"  sitemap xlsx: {xlsx_in_sitemap[:3]}")


# ════════════════════════════════════════════════════════════════
# 3. INVESCO
# ════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("3. INVESCO")
print("="*65)

# Invesco uses a .NET/Sitefinity CMS — check their docs API
invesco_apis = [
    'https://invescomutualfund.com/api/documents?type=portfolio',
    'https://invescomutualfund.com/docs/default-source/monthly-portfolio/',
    'https://www.invescomutualfund.com/docs/default-source/portfolio-disclosure/',
    'https://www.invescomutualfund.com/investor-education/portfolio-disclosure',
    'https://invescomutualfund.com/en-in/tools-and-resources/portfolio-disclosure',
]
for url in invesco_apis:
    code, text, _ = get(url)
    print(f"  [{code}] {url[-65:]}")
    if code == 200:
        xlsx = re.findall(r'https?://[^\s"\'<>]+\.xlsx', text, re.I)
        if xlsx: print(f"    xlsx: {xlsx[:2]}")

# Try Sitefinity pattern — Invesco India often uses /docs/default-source/
# The factsheets were at: invescomutualfund.com/docs/default-source/factsheet/
# Portfolio disclosures might be at:
invesco_direct = [
    'https://invescomutualfund.com/docs/default-source/monthly-portfolio/invesco-mf-monthly-portfolio-april-2026.xlsx',
    'https://invescomutualfund.com/docs/default-source/portfolio-disclosure/invesco-monthly-portfolio-april2026.xlsx',
    'https://invescomutualfund.com/docs/default-source/portfolio-statement/invesco-mf-portfolio-april-2026.xlsx',
    'https://invescomutualfund.com/docs/default-source/monthly-portfolio-disclosure/invesco-portfolio-april2026.xlsx',
    # Maybe PDF?
    'https://invescomutualfund.com/docs/default-source/monthly-portfolio/invesco-mf-monthly-portfolio-april-2026.pdf',
    'https://invescomutualfund.com/docs/default-source/portfolio-disclosure/invesco-mf-portfolio-april-2026.pdf',
]
print("\n  Direct URL guesses:")
for url in invesco_direct:
    code, _, content = get(url)
    size = len(content) if content else 0
    if code == 200 and size > 5000:
        print(f"  ✓ [{code}] {url[-65:]} ({size} bytes)")
        peek_excel(content, 'sample')
    else:
        print(f"  ✗ [{code}] {url[-65:]}")


# ════════════════════════════════════════════════════════════════
# 4. CANARA ROBECO
# ════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("4. CANARA ROBECO")
print("="*65)

# Canara Robeco is WordPress — check their wp-json API
canara_apis = [
    'https://www.canararobeco.com/wp-json/wp/v2/media?search=portfolio&per_page=20',
    'https://www.canararobeco.com/wp-json/wp/v2/posts?search=portfolio+disclosure&per_page=10',
    'https://www.canararobeco.com/wp-content/uploads/2026/04/',  # directory listing?
    'https://www.canararobeco.com/wp-content/uploads/2026/05/',
]
for url in canara_apis:
    code, text, _ = get(url)
    print(f"  [{code}] {url[-65:]}")
    if code == 200:
        xlsx_links = re.findall(r'https?://[^\s"\'<>]+\.xlsx', text, re.I)
        pdf_links  = [l for l in re.findall(r'https?://[^\s"\'<>]+\.pdf', text, re.I)
                       if any(k in l.lower() for k in ['portfolio', 'monthly', 'sebi', 'disclosure'])]
        if xlsx_links: print(f"    xlsx: {xlsx_links[:3]}")
        if pdf_links:  print(f"    port PDFs: {pdf_links[:3]}")
        if code == 200 and '{' in text[:5]:
            try:
                d = json.loads(text)
                urls = [item.get('source_url','') for item in (d if isinstance(d,list) else [d])[:5]]
                if urls: print(f"    API items: {[u[-50:] for u in urls]}")
            except: pass

# Try direct URL patterns
canara_direct = [
    # WordPress upload pattern: /wp-content/uploads/YYYY/MM/filename.xlsx
    'https://www.canararobeco.com/wp-content/uploads/2026/05/Canara-Robeco-Monthly-Portfolio-April-2026.xlsx',
    'https://www.canararobeco.com/wp-content/uploads/2026/04/Canara-Robeco-Monthly-Portfolio-March-2026.xlsx',
    'https://www.canararobeco.com/wp-content/uploads/2026/05/CRMF-Monthly-Portfolio-April-2026.xlsx',
    'https://www.canararobeco.com/wp-content/uploads/2026/04/Monthly-Portfolio-April-2026.xlsx',
    'https://www.canararobeco.com/wp-content/uploads/2026/05/Canara-Robeco-Portfolio-Disclosure-April-2026.xlsx',
]
print("\n  Direct URL guesses:")
for url in canara_direct:
    code, _, content = get(url)
    size = len(content) if content else 0
    if code == 200 and size > 5000:
        print(f"  ✓ {url[-70:]} ({size} bytes)")
        peek_excel(content)
    else:
        print(f"  ✗ [{code}] {url[-70:]}")

# Try WP REST API for media files
code, text, _ = get('https://www.canararobeco.com/wp-json/wp/v2/media?per_page=50&mime_type=application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
if code == 200:
    try:
        items = json.loads(text)
        xlsx_items = [(i.get('title',{}).get('rendered',''), i.get('source_url','')) for i in items]
        print(f"\n  WP REST xlsx media ({len(xlsx_items)} items):")
        for title, url in xlsx_items[:5]:
            print(f"    {title[:40]} -> {url[-60:]}")
    except: pass


# ════════════════════════════════════════════════════════════════
# 5. WHITEOAK CAPITAL
# ════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("5. WHITEOAK CAPITAL")
print("="*65)

# WhiteOak uses S3 — check their portfolio disclosure S3 bucket patterns
# Factsheets pattern: wocamc-prd-prelogin-s3-00.s3.ap-south-1.amazonaws.com/
# Let's try different filename patterns for portfolio disclosures
wo_apis = [
    'https://api.whiteoakamc.com/api/documents/portfolio',
    'https://www.whiteoakmf.com/api/portfolio-disclosures',
    # Their website API
    'https://www.whiteoakmf.com/api/v1/documents?category=portfolio',
]
for url in wo_apis:
    code, text, _ = get(url)
    print(f"  [{code}] {url[-65:]}")
    if code == 200 and len(text) > 50:
        print(f"    {text[:200]}")

# Check their S3 bucket for portfolio disclosure pattern
wo_s3_patterns = [
    # Based on factsheet pattern: Factsheet_December_24_Update_November_24_Portfolio_...pdf
    # Portfolio disclosure would be a different file
    'https://wocamc-prd-prelogin-s3-00.s3.ap-south-1.amazonaws.com/WOC_Monthly_Portfolio_April_2026.xlsx',
    'https://wocamc-prd-prelogin-s3-00.s3.ap-south-1.amazonaws.com/WhiteOak_Monthly_Portfolio_April_2026.xlsx',
    'https://wocamc-prd-prelogin-s3-00.s3.ap-south-1.amazonaws.com/WOC_Portfolio_Disclosure_April_2026.xlsx',
    'https://content.whiteoakamc.com/Monthly_Portfolio_April_2026.xlsx',
    'https://content.whiteoakamc.com/WOC_Monthly_Portfolio_April_2026.xlsx',
]
print("\n  S3/CDN URL guesses:")
for url in wo_s3_patterns:
    code, _, content = get(url)
    if code == 200 and len(content) > 5000:
        print(f"  ✓ {url[-70:]} ({len(content)} bytes)")
        peek_excel(content)
    else:
        print(f"  ✗ [{code}] {url[-70:]}")

# Check their website more carefully for the SEBI-mandated disclosure page
code, text, _ = get('https://www.whiteoakmf.com/monthly-portfolio-disclosure')
if code == 200:
    # Look for data- attributes or script tags with URLs
    s3_links = re.findall(r'https?://wocamc[^\s"\'<>]+', text, re.I)
    content_links = re.findall(r'https?://content\.whiteoakamc[^\s"\'<>]+', text, re.I)
    print(f"\n  WO disclosure page S3 links: {s3_links[:5]}")
    print(f"  WO content CDN links: {content_links[:5]}")
    # Check script tags for JSON data
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', text, re.DOTALL)
    for sc in scripts:
        if 'portfolio' in sc.lower() and ('xlsx' in sc.lower() or 'url' in sc.lower()):
            print(f"  Script with portfolio data: {sc[:200]}")

print("\nDone.")
