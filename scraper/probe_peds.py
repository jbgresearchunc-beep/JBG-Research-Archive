#!/usr/bin/env python3
"""
Probe the Radiology WordPress REST API to find the faculty post type.
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
            total = r.headers.get('X-WP-Total', '?')
            return json.loads(r.read()), total
    except Exception as e:
        if not silent:
            print(f"  ERROR: {e}")
        return None, 0

base = "https://www.med.unc.edu/radiology/wp-json"

# Step 1: list all post types
print("=== All registered post types ===")
types, _ = fetch_json(f"{base}/wp/v2/types")
if types:
    for slug, info in types.items():
        print(f"  {slug:30s} rest_base={info.get('rest_base','')}")

time.sleep(0.5)

# Step 2: try common custom post type names
print("\n=== Try custom post type REST bases ===")
candidates = [
    "ud_entry", "faculty", "people", "person", "staff",
    "directory", "provider", "physician", "team",
]
for slug in candidates:
    data, total = fetch_json(f"{base}/wp/v2/{slug}?per_page=3&_fields=id,title,link", silent=True)
    if data and isinstance(data, list) and len(data) > 0:
        print(f"  FOUND: /{slug} — total={total}")
        for item in data[:3]:
            title = item.get('title', {})
            if isinstance(title, dict): title = title.get('rendered', '')
            print(f"    {title} — {item.get('link','')}")
    time.sleep(0.2)

# Step 3: check all routes for people-related endpoints
print("\n=== Routes containing 'faculty', 'people', 'person', 'directory' ===")
root, _ = fetch_json(f"{base}/")
if root and 'routes' in root:
    for route in root['routes']:
        if any(w in route.lower() for w in ['faculty', 'people', 'person', 'directory', 'staff', 'provider']):
            print(f"  {route}")
