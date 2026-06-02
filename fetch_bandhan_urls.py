import requests, json, time, re, os
from collections import defaultdict

headers = {
    'Origin': 'https://bandhanmutual.com',
    'Referer': 'https://bandhanmutual.com/',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
}
api = "https://cmsnew.bandhanmutual.com/wp-json/finance-api/v1/posts/disclosures"

OUT = '/sessions/admiring-nifty-dijkstra/mnt/outputs/bandhan_monthly_urls.json'

# Load progress if exists
if os.path.exists(OUT):
    with open(OUT) as f:
        saved = json.load(f)
    period_urls = defaultdict(list, {k: v for k, v in saved.items()})
    # Load seen_ids from a sidecar file
    seen_path = OUT.replace('.json', '_seen.json')
    seen_ids = set(json.load(open(seen_path))) if os.path.exists(seen_path) else set()
    print(f"Resuming: {len(seen_ids)} seen IDs, {len(period_urls)} periods")
else:
    period_urls = defaultdict(list)
    seen_ids = set()
    seen_path = OUT.replace('.json', '_seen.json')

def extract_date_from_title(title):
    m = re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', title)
    if m:
        try:
            from datetime import datetime
            dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y")
            return dt.strftime('%Y-%m')
        except: pass
    return None

import sys
fy = sys.argv[1] if len(sys.argv) > 1 else '2025'
start_page = int(sys.argv[2]) if len(sys.argv) > 2 else 1
end_page   = int(sys.argv[3]) if len(sys.argv) > 3 else 50

print(f"FY={fy} pages {start_page}-{end_page}")

for page in range(start_page, end_page + 1):
    params = {'financial_year': fy, 'per_page': 50, 'page': page}
    try:
        resp = requests.get(api, headers=headers, params=params, timeout=20)
        data = resp.json()
        entries = data.get('data', [])
        if not entries:
            print(f"Empty at page {page}")
            break
        
        new_entries = [e for e in entries if e.get('id') not in seen_ids]
        for e in new_entries:
            seen_ids.add(e.get('id'))
            acf = e.get('acf_fields', {}) or {}
            dtype = (acf.get('disclosures_type', '') or '').lower()
            title = e.get('title', '') or ''
            
            if 'monthly' in dtype:
                period = extract_date_from_title(title)
                if period and period >= '2023-01':
                    disc_files = acf.get('disclosure_files', []) or []
                    for df in disc_files:
                        if not isinstance(df, dict): continue
                        doc = df.get('document_link', {}) or {}
                        url = doc.get('url', '') if isinstance(doc, dict) else ''
                        if url and url.startswith('http'):
                            entry = {'url': url, 'title': title[:80], 'filename': doc.get('filename', '')}
                            # Avoid dups
                            if not any(x['url'] == url for x in period_urls[period]):
                                period_urls[period].append(entry)
        
        if page % 10 == 0:
            periods_found = sorted(period_urls.keys())
            print(f"  Page {page}: {len(seen_ids)} IDs, periods: {periods_found}")
        
    except Exception as ex:
        if "'bool'" in str(ex) or 'bool' in str(ex):
            continue
        print(f"Error page={page}: {ex}")
        time.sleep(1)
    time.sleep(0.1)

# Save
with open(OUT, 'w') as f:
    json.dump(dict(period_urls), f, indent=2)
with open(seen_path, 'w') as f:
    json.dump(list(seen_ids), f)

print(f"\nSaved. Periods: {sorted(period_urls.keys())}")
for p in sorted(period_urls.keys()):
    print(f"  {p}: {len(period_urls[p])} schemes")
