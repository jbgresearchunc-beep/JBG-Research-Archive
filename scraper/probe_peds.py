#!/usr/bin/env python3
"""
Run this as a one-off GitHub Actions step to see what the WP REST API returns.
Add to discover_urls.yml as an extra step, or run standalone.
"""
import urllib.request, json, time

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def fetch(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode('utf-8', errors='replace'), r.status
    except Exception as e:
        return None, str(e)

base = "https://www.med.unc.edu/pediatrics"

print("=" * 60)
print("Probing UNC Pediatrics WordPress REST API")
print("=" * 60)

endpoints = [
    # Does the API exist at all?
    f"{base}/wp-json/",
    # Standard WP page types
    f"{base}/wp-json/wp/v2/types",
    # Search for pages with 'faculty' in slug
    f"{base}/wp-json/wp/v2/pages?slug=faculty&per_page=5&_fields=id,title,link,slug,parent",
    # Get ALL pages, look for faculty ones
    f"{base}/wp-json/wp/v2/pages?per_page=10&_fields=id,title,link,slug,parent",
    # Custom post type? Check posts too
    f"{base}/wp-json/wp/v2/posts?per_page=5&_fields=id,title,link,slug",
    # The tec api hinted in meta
    f"{base}/wp-json/tec/v1/",
    # Try the actual faculty page as a WP page
    f"{base}/wp-json/wp/v2/pages?slug=faculty&parent_slug=team&per_page=10",
]

for url in endpoints:
    print(f"\n--- GET {url}")
    data, status = fetch(url)
    print(f"Status: {status}")
    if data:
        print(f"Length: {len(data)} bytes")
        # Pretty print if JSON
        try:
            parsed = json.loads(data)
            if isinstance(parsed, list):
                print(f"Array of {len(parsed)} items")
                for item in parsed[:3]:
                    if isinstance(item, dict):
                        print(f"  id={item.get('id')} slug={item.get('slug')} title={item.get('title',{}).get('rendered','') if isinstance(item.get('title'),dict) else item.get('title','')}")
            elif isinstance(parsed, dict):
                print(f"Object with keys: {list(parsed.keys())[:10]}")
                if 'routes' in parsed:
                    print(f"  Routes (first 10): {list(parsed['routes'].keys())[:10]}")
        except:
            print(f"Raw (first 500 chars): {data[:500]}")
    time.sleep(0.5)
