#!/usr/bin/env python3
"""
Phase 4: faculty are stored as 'ud_entry' custom post type (Toolset).
Query it and map entries back to divisions via their parent page IDs.
"""
import urllib.request, json, time

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            total = r.headers.get('X-WP-Total', '?')
            return json.loads(raw), total
    except Exception as e:
        print(f"  ERROR: {e}")
        return None, 0

base = "https://www.med.unc.edu/pediatrics/wp-json/wp/v2"

# Step 1: get a sample of ud_entry posts to understand the structure
print("=== Sample ud_entry posts ===")
data, total = fetch_json(f"{base}/ud_entry?per_page=3&_fields=id,title,link,slug,parent,meta")
print(f"Total ud_entry posts: {total}")
if data and isinstance(data, list):
    for item in data:
        title = item.get('title', {})
        if isinstance(title, dict): title = title.get('rendered', '')
        print(f"\n  id={item['id']} parent={item.get('parent',0)}")
        print(f"  title={title}")
        print(f"  link={item.get('link','')}")
        print(f"  meta={json.dumps(item.get('meta',{}))[:200]}")

time.sleep(0.5)

# Step 2: check what fields are available
print("\n=== Full first ud_entry (all fields) ===")
data2, _ = fetch_json(f"{base}/ud_entry?per_page=1")
if data2 and isinstance(data2, list) and data2:
    print(json.dumps(data2[0], indent=2)[:2000])

time.sleep(0.5)

# Step 3: the faculty page IDs we found in phase 2
# Map: division name -> faculty page ID
FACULTY_PAGE_IDS = {
    "Allergy & Immunology":    35958,
    "Cardiology":              36010,
    "Critical Care (PCCM)":   36082,
    "Emergency Medicine":      36841,
    "Endocrinology":           36810,
    "GI":                      36812,
    "Genetics":                36845,
    "General Peds & Adol Med": 36843,
    "Hem-Onc":                 36814,
    "Hospital Medicine":       36847,
    "Infectious Disease":      36849,
    "Neonatology (NPM)":       36816,
    "Nephrology":              39244,
    "DBL":                     39249,
    "Pulmonology":             36808,
    "Rheumatology":            36851,
}

# Step 4: try querying ud_entry filtered by parent page
print("\n=== ud_entry filtered by parent (Allergy & Immunology, id=35958) ===")
data3, total3 = fetch_json(f"{base}/ud_entry?parent=35958&per_page=20&_fields=id,title,link,parent")
print(f"  Results: {total3}")
if data3 and isinstance(data3, list):
    for item in data3[:10]:
        title = item.get('title', {})
        if isinstance(title, dict): title = title.get('rendered', '')
        print(f"  {title} — {item.get('link','')}")

time.sleep(0.5)

# Step 5: get ALL ud_entry posts and see if we can identify faculty by link pattern
print("\n=== All ud_entry posts (first 20, checking link patterns) ===")
data4, total4 = fetch_json(f"{base}/ud_entry?per_page=20&_fields=id,title,link,parent&orderby=title&order=asc")
print(f"Total: {total4}")
if data4 and isinstance(data4, list):
    for item in data4:
        title = item.get('title', {})
        if isinstance(title, dict): title = title.get('rendered', '')
        print(f"  parent={item.get('parent',0):6d}  {title[:40]:40s}  {item.get('link','')}")
