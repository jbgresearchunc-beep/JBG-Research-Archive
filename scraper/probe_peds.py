#!/usr/bin/env python3
"""
Phase 3: faculty are not child pages — they're a Toolset custom post type.
Find the right post type and query it filtered by parent division.
"""
import urllib.request, json, time

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def fetch_json(url, silent=False):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        if not silent:
            print(f"  ERROR {url}: {e}")
        return None

base = "https://www.med.unc.edu/pediatrics/wp-json"

# Step 1: list ALL registered post types
print("=== All registered post types ===")
types = fetch_json(f"{base}/wp/v2/types")
if types:
    for slug, info in types.items():
        rest_base = info.get('rest_base', '')
        print(f"  {slug:30s} rest_base={rest_base}")

time.sleep(0.5)

# Step 2: try Toolset's custom post search endpoint
print("\n=== Toolset PostSearch endpoint ===")
toolset = fetch_json(f"{base}/ToolsetBlocks/Rest/API/v1/PostSearch")
if toolset:
    print(json.dumps(toolset, indent=2)[:500])

time.sleep(0.5)

# Step 3: try common custom post type names for faculty/people
print("\n=== Try common custom post type REST bases ===")
candidates = [
    "faculty", "people", "person", "staff", "team",
    "provider", "physician", "researcher", "member",
    "ped-faculty", "peds-faculty", "faculty-member",
]
for slug in candidates:
    data = fetch_json(f"{base}/wp/v2/{slug}?per_page=3&_fields=id,title,link", silent=True)
    if data and isinstance(data, list) and len(data) > 0:
        print(f"  FOUND: /{slug} — {len(data)} results")
        for item in data[:3]:
            title = item.get('title', {})
            if isinstance(title, dict): title = title.get('rendered', '')
            print(f"    id={item['id']} title={title}")
    time.sleep(0.2)

# Step 4: check all routes for anything faculty-related
print("\n=== All API routes containing 'faculty' or 'people' or 'person' ===")
root = fetch_json(f"{base}/")
if root and 'routes' in root:
    for route in root['routes']:
        if any(word in route.lower() for word in ['faculty', 'people', 'person', 'staff', 'provider']):
            print(f"  {route}")
