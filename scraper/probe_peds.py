#!/usr/bin/env python3
"""
Phase 2 probe: we know faculty pages exist with IDs 39244, 39249, 36843.
Now find ALL faculty page IDs and their children (individual faculty profiles).
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
            return json.loads(r.read())
    except Exception as e:
        print(f"  ERROR: {e}")
        return None

base = "https://www.med.unc.edu/pediatrics/wp-json/wp/v2"

# Step 1: Get ALL pages with slug=faculty (paginate to get all divisions)
print("=== Step 1: Get all 'faculty' pages (all divisions) ===")
all_faculty_pages = []
for page_num in range(1, 5):
    url = f"{base}/pages?slug=faculty&per_page=100&page={page_num}&_fields=id,title,link,slug,parent"
    data = fetch_json(url)
    if not data or not isinstance(data, list) or len(data) == 0:
        break
    all_faculty_pages.extend(data)
    print(f"  Page {page_num}: {len(data)} results")
    time.sleep(0.3)

print(f"\nTotal 'faculty' pages found: {len(all_faculty_pages)}")
for p in all_faculty_pages:
    print(f"  id={p['id']} parent={p.get('parent',0)} link={p.get('link','')}")

# Step 2: For each faculty page, get its children (individual faculty members)
print("\n=== Step 2: Get children of each faculty page ===")
all_people = []
for fpage in all_faculty_pages[:20]:  # cap at 20 divisions
    parent_id = fpage['id']
    parent_link = fpage.get('link', '')
    url = f"{base}/pages?parent={parent_id}&per_page=100&_fields=id,title,link,slug&status=publish"
    children = fetch_json(url)
    if not children or not isinstance(children, list):
        continue
    print(f"\n  Faculty page {parent_id} ({parent_link}): {len(children)} children")
    for child in children[:5]:  # show first 5
        title = child.get('title', {})
        if isinstance(title, dict):
            title = title.get('rendered', '')
        print(f"    id={child['id']} title={title} link={child.get('link','')}")
    all_people.extend(children)
    time.sleep(0.3)

print(f"\nTotal faculty profiles found: {len(all_people)}")
