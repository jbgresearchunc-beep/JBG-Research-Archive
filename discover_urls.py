#!/usr/bin/env python3
"""
One-off URL discovery script for departments that organize by division/specialty.
Run this as a GitHub Actions job to find the correct faculty page URLs.

Usage:
    python scraper/discover_urls.py

Output:
    Prints discovered faculty URLs for each hub department, ready to paste
    into departments.json.
"""

import urllib.request
import urllib.parse
import re
import time
import json
from html.parser import HTMLParser


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Keywords that suggest a page lists faculty (in the URL path)
FACULTY_URL_HINTS = re.compile(
    r"(faculty|people|team|staff|directory|providers|physicians|our-team|meet)",
    re.IGNORECASE
)

# Keywords that suggest a page is a division/specialty subpage
DIVISION_URL_HINTS = re.compile(
    r"(division|specialty|subspecialty|section|program|service|clinic)",
    re.IGNORECASE
)

# Keywords to reject — these are not faculty pages
REJECT_HINTS = re.compile(
    r"(login|wp-admin|wp-login|feed|rss|sitemap|privacy|contact|"
    r"calendar|event|news|blog|patient|appointment|map|giving|"
    r"intranet|accessibility|search|education|residency|fellowship|"
    r"research(?!/)|about(?!.*people)|history|mission|vision)",
    re.IGNORECASE
)


def fetch_html(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"    Error fetching {url}: {e}")
        return ""


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []  # (href, text)
        self._current_text = []
        self._in_a = False
        self._current_href = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._in_a = True
            self._current_href = dict(attrs).get("href", "")
            self._current_text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            self._in_a = False
            text = " ".join(self._current_text).strip()
            if self._current_href:
                self.links.append((self._current_href, text))
            self._current_href = None

    def handle_data(self, data):
        if self._in_a:
            self._current_text.append(data.strip())


def extract_links(html, base_url):
    """Extract all internal links from HTML."""
    parser = LinkParser()
    parser.feed(html)
    base_domain = urllib.parse.urlparse(base_url).netloc
    results = []
    for href, text in parser.links:
        if not href:
            continue
        # Make absolute
        full = urllib.parse.urljoin(base_url, href).rstrip("/")
        parsed = urllib.parse.urlparse(full)
        # Must be same domain
        if parsed.netloc != base_domain:
            continue
        # Must be http/https
        if parsed.scheme not in ("http", "https"):
            continue
        results.append((full, text))
    return list(dict.fromkeys(results))  # dedup preserving order


def score_as_faculty_page(url, text):
    """Score how likely a URL is a faculty listing page. Higher = more likely."""
    score = 0
    path = urllib.parse.urlparse(url).path.lower()

    if REJECT_HINTS.search(path):
        return -10

    if FACULTY_URL_HINTS.search(path):
        score += 3
    if DIVISION_URL_HINTS.search(path):
        score += 1
    if FACULTY_URL_HINTS.search(text):
        score += 2

    # Prefer deeper paths (division/faculty > just faculty)
    depth = path.count("/")
    score += min(depth, 4)

    return score


def discover_faculty_urls(hub_url, dept_name, max_depth=2):
    """
    Crawl a hub page and find faculty listing URLs within it.
    Returns a list of (url, label) tuples.
    """
    print(f"\n{'='*60}")
    print(f"Discovering: {dept_name}")
    print(f"Hub: {hub_url}")

    base_path = urllib.parse.urlparse(hub_url).path

    def is_subpage(url):
        path = urllib.parse.urlparse(url).path
        return path.startswith(base_path) and path != base_path

    # Level 1: scrape the hub page
    html = fetch_html(hub_url)
    if not html:
        return []
    time.sleep(0.5)

    level1_links = [(u, t) for u, t in extract_links(html, hub_url) if is_subpage(u)]

    # Score level 1 links
    candidates = []
    for url, text in level1_links:
        score = score_as_faculty_page(url, text)
        if score >= 2:
            candidates.append((score, url, text))

    # Level 2: follow promising division pages and look for faculty subpages
    if max_depth >= 2:
        division_pages = [(u, t) for u, t in level1_links
                         if DIVISION_URL_HINTS.search(urllib.parse.urlparse(u).path)
                         and not REJECT_HINTS.search(urllib.parse.urlparse(u).path)]

        for div_url, div_text in division_pages[:20]:  # cap at 20 to avoid timeout
            div_html = fetch_html(div_url)
            if not div_html:
                continue
            time.sleep(0.4)
            for sub_url, sub_text in extract_links(div_html, div_url):
                if not is_subpage(sub_url) and not sub_url.startswith(div_url):
                    continue
                score = score_as_faculty_page(sub_url, sub_text)
                if score >= 3:
                    candidates.append((score, sub_url, sub_text))

    # Deduplicate and sort by score
    seen = set()
    unique = []
    for score, url, text in sorted(candidates, reverse=True):
        if url not in seen:
            seen.add(url)
            unique.append((score, url, text))

    print(f"\nTop candidates:")
    for score, url, text in unique[:15]:
        print(f"  [{score:+d}] {text[:40]:40s}  {url}")

    return unique


# ── Hub departments to discover ──────────────────────────────────────────────

HUBS = [
    ("Pediatrics",                "https://www.med.unc.edu/pediatrics/specialty-care/"),
    ("Psychiatry",                "https://www.med.unc.edu/psych/"),
    ("Neurology",                 "https://www.med.unc.edu/neurology/"),
    ("Medicine — Endocrinology",  "https://www.med.unc.edu/medicine/endocrinology/"),
    ("Medicine — Nephrology",     "https://www.med.unc.edu/medicine/nephrology/"),
    ("Genetics",                  "https://www.med.unc.edu/genetics/"),
    ("Microbiology & Immunology", "https://www.med.unc.edu/microimm/"),
    ("Biochemistry & Biophysics", "https://www.med.unc.edu/biochem/"),
    ("Cell Biology & Physiology", "https://www.med.unc.edu/cellbiophysio/"),
    ("Pharmacology",              "https://www.med.unc.edu/pharm/"),
    ("Physical Medicine & Rehab", "https://www.med.unc.edu/phyrehab/"),
    ("Pathology",                 "https://www.med.unc.edu/pathology/"),
]


if __name__ == "__main__":
    print("UNC Faculty URL Discovery")
    print("=" * 60)
    print("This script crawls department hub pages to find faculty listing URLs.")
    print("Review the output and paste confirmed URLs into departments.json.")
    print()

    all_results = {}
    for dept_name, hub_url in HUBS:
        results = discover_faculty_urls(hub_url, dept_name, max_depth=2)
        all_results[dept_name] = results
        time.sleep(1)

    print("\n\n" + "="*60)
    print("SUMMARY — paste confirmed URLs into departments.json")
    print("="*60)
    for dept_name, results in all_results.items():
        print(f"\n# {dept_name}")
        if results:
            for score, url, text in results[:5]:
                print(f'  "{url}"')
        else:
            print("  (no candidates found — check hub URL manually)")
