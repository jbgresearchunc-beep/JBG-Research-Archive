#!/usr/bin/env python3
"""
UNC SOM Research Explorer — faculty scraper
Scrapes faculty names from department pages, enriches with PubMed data,
and writes output to data/faculty.json
"""

import json
import time
import re
import sys
import os
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

class LinkTextParser(HTMLParser):
    """Extracts (href, text) pairs from anchor tags."""
    def __init__(self):
        super().__init__()
        self.links = []
        self._current_href = None
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs_dict = dict(attrs)
            self._current_href = attrs_dict.get("href", "")
            self._current_text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._current_href is not None:
            text = " ".join(self._current_text).strip()
            if text:
                self.links.append((self._current_href, text))
            self._current_href = None
            self._current_text = []

    def handle_data(self, data):
        if self._current_href is not None:
            self._current_text.append(data.strip())


class MetaTextParser(HTMLParser):
    """Extracts all visible text and meta tags."""
    def __init__(self):
        super().__init__()
        self.texts = []
        self.skip_tags = {"script", "style", "noscript"}
        self._skip = 0
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        if tag in self.skip_tags:
            self._skip += 1
        if tag == "meta":
            attrs_dict = dict(attrs)
            name = attrs_dict.get("name", attrs_dict.get("property", ""))
            content = attrs_dict.get("content", "")
            if name and content:
                self.meta[name] = content

    def handle_endtag(self, tag):
        if tag in self.skip_tags:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            stripped = data.strip()
            if stripped:
                self.texts.append(stripped)


def fetch_url(url, retries=3, delay=1.5):
    headers = {
        "User-Agent": "UNC-Research-Explorer/1.0 (student research tool; contact: jgbresearch@unc.edu)",
        "Accept": "text/html,application/xhtml+xml",
    }
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            print(f"  HTTP {e.code} fetching {url} (attempt {attempt+1})")
        except Exception as e:
            print(f"  Error fetching {url}: {e} (attempt {attempt+1})")
        if attempt < retries - 1:
            time.sleep(delay * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# Faculty name extraction
# ---------------------------------------------------------------------------

# Titles/credentials to strip when detecting if a link is a faculty name
DEGREE_SUFFIXES = re.compile(
    r",?\s*(MD|PhD|DO|MS|MPH|MBA|FACS|FACP|FAAN|RN|NP|PA|PA-C|"
    r"PharmD|DDS|DVM|DrPH|ScD|JD|MBBS|BMBS|MHS|MHA|MSCR|MSPH|"
    r"FRCSC|FACOG|FAAD|FACR|FACEP|FAHA|FHRS|SM|MPP|BSN|ARNP|CNM|"
    r"RD|BCPS|BCGP|CPP|BCNP|ABR|FACR|FASN|FANA|FASGE|FRCR|FACG|"
    r"AGAF|FATA|FAC[A-Z]+|Jr\.|Sr\.|II|III|IV)\b",
    re.IGNORECASE
)

TITLE_PREFIXES = re.compile(
    r"^(Dr\.?\s+|Prof\.?\s+|Professor\s+|Associate\s+Professor|"
    r"Assistant\s+Professor|Clinical\s+|Adjunct\s+)",
    re.IGNORECASE
)

NON_NAME_PATTERNS = re.compile(
    r"(faculty|directory|people|team|staff|home|search|contact|"
    r"about|news|education|research|residency|fellowship|program|"
    r"division|department|center|institute|login|admin|patient|"
    r"appointment|profile|view|all|more|click|here|back|next|"
    r"previous|apply|submit|calendar|event|blog|video|photo|"
    r"gallery|map|campus|career|job|giving|donate|privacy|"
    r"accessibility|intranet|skip|menu|navigation|toggle|search)",
    re.IGNORECASE
)


def looks_like_name(text):
    """Heuristic: does this string look like a person's name?"""
    text = text.strip()
    if not text or len(text) < 4 or len(text) > 80:
        return False
    # Strip credentials
    clean = DEGREE_SUFFIXES.sub("", text).strip().rstrip(",")
    clean = TITLE_PREFIXES.sub("", clean).strip()
    if NON_NAME_PATTERNS.search(clean):
        return False
    # Should have 2-4 space-separated words, each starting with uppercase
    words = clean.split()
    if len(words) < 2 or len(words) > 5:
        return False
    # Most words should start with uppercase
    caps = sum(1 for w in words if w and w[0].isupper())
    return caps >= len(words) - 1


def extract_name(raw_text):
    """Clean raw link text into a canonical name."""
    text = raw_text.strip()
    text = DEGREE_SUFFIXES.sub("", text).strip().rstrip(",").strip()
    text = TITLE_PREFIXES.sub("", text).strip()
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text


def scrape_faculty_from_page(url, dept_name):
    """
    Scrape a department faculty page and return list of faculty dicts.
    Strategy: find anchor tags whose text looks like a person's name
    and whose href points to a profile page on the same domain.
    """
    print(f"  Scraping: {url}")
    html = fetch_url(url)
    if not html:
        print(f"  Could not fetch {url}")
        return []

    parser = LinkTextParser()
    parser.feed(html)

    seen_names = set()
    faculty = []

    for href, text in parser.links:
        if not looks_like_name(text):
            continue
        # Prefer links that look like profile pages
        if href and ("people" in href or "directory" in href or
                     "faculty" in href or "profile" in href or
                     "/directory/" in href):
            name = extract_name(text)
            if name and name.lower() not in seen_names and len(name) > 4:
                seen_names.add(name.lower())
                # Try to get role from nearby text (best effort — often not available from link alone)
                faculty.append({
                    "name": name,
                    "profile_url": href if href.startswith("http") else f"https://www.med.unc.edu{href}",
                    "department": dept_name,
                    "role": "",
                })

    # Fallback: if we found very few links, try extracting names from visible text
    if len(faculty) < 3:
        text_parser = MetaTextParser()
        text_parser.feed(html)
        all_text = text_parser.texts
        for i, chunk in enumerate(all_text):
            if looks_like_name(chunk):
                name = extract_name(chunk)
                if name and name.lower() not in seen_names and len(name) > 4:
                    # Look for role in adjacent text chunks
                    role = ""
                    if i + 1 < len(all_text):
                        candidate_role = all_text[i + 1]
                        if ("professor" in candidate_role.lower() or
                                "chief" in candidate_role.lower() or
                                "director" in candidate_role.lower() or
                                "instructor" in candidate_role.lower() or
                                "lecturer" in candidate_role.lower()):
                            role = candidate_role[:100]
                    seen_names.add(name.lower())
                    faculty.append({
                        "name": name,
                        "profile_url": "",
                        "department": dept_name,
                        "role": role,
                    })

    print(f"  Found {len(faculty)} faculty")
    return faculty


def scrape_profile_for_pubmed_string(profile_url):
    """
    Try to extract a curated PubMed search string from a faculty profile page.
    Many UNC profiles list something like 'Search PubMed using Doe JA as search criteria'.
    """
    if not profile_url:
        return None
    html = fetch_url(profile_url)
    if not html:
        return None
    # Pattern: "using [search string] as search criteria" or "PubMed" near initials
    match = re.search(
        r"(?:search\s+(?:for\s+)?publications?\s+on\s+pubmed\s+using\s+|"
        r"pubmed\s+using\s+)([A-Za-z ,]+?)(?:\s+as\s+search\s+criteria|"
        r"\s+as\s+search|\s*\n)",
        html, re.IGNORECASE
    )
    if match:
        return match.group(1).strip()

    # Also look for ORCID
    orcid_match = re.search(
        r"orcid\.org/(\d{4}-\d{4}-\d{4}-\d{3}[\dX])",
        html, re.IGNORECASE
    )
    if orcid_match:
        return f"ORCID:{orcid_match.group(1)}"

    return None


# ---------------------------------------------------------------------------
# PubMed queries
# ---------------------------------------------------------------------------

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
PUBMED_EMAIL = "jgbresearch@unc.edu"  # NCBI requests a contact email


def build_pubmed_search_string(name):
    """Convert 'First Middle Last' -> 'Last FM' author search string."""
    parts = name.strip().split()
    if not parts:
        return name
    last = parts[-1]
    initials = "".join(p[0] for p in parts[:-1] if p)
    return f"{last} {initials}"


def pubmed_search(search_term, affiliation="University of North Carolina", max_results=5):
    """
    Query PubMed and return list of PMIDs.
    search_term: either a name-based string like 'Doe JA' or an ORCID.
    """
    if search_term.startswith("ORCID:"):
        orcid = search_term.replace("ORCID:", "")
        query = f"{orcid}[auid]"
    else:
        query = f'"{search_term}"[Author] AND "{affiliation}"[Affiliation] AND ("2018"[PDAT] : "2026"[PDAT])'

    params = urllib.parse.urlencode({
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
        "sort": "date",
        "tool": "unc-research-explorer",
        "email": PUBMED_EMAIL,
    })
    url = PUBMED_BASE + "esearch.fcgi?" + params

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        result = data.get("esearchresult", {})
        return {
            "count": int(result.get("count", 0)),
            "ids": result.get("idlist", []),
        }
    except Exception as e:
        print(f"    PubMed search error for '{search_term}': {e}")
        return {"count": 0, "ids": []}


def pubmed_fetch_summaries(pmids):
    """Fetch article summaries for a list of PMIDs."""
    if not pmids:
        return []

    params = urllib.parse.urlencode({
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "json",
        "tool": "unc-research-explorer",
        "email": PUBMED_EMAIL,
    })
    url = PUBMED_BASE + "esummary.fcgi?" + params

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        results = data.get("result", {})
        pubs = []
        for pmid in pmids:
            art = results.get(pmid, {})
            if not art or art.get("error"):
                continue
            title = re.sub(r"<[^>]+>", "", art.get("title", "")).strip()
            pubs.append({
                "pmid": pmid,
                "title": title,
                "journal": art.get("source", ""),
                "year": (art.get("pubdate", "") or "")[:4],
            })
        return pubs
    except Exception as e:
        print(f"    PubMed fetch error: {e}")
        return []


def enrich_faculty_with_pubmed(faculty_member, pubmed_string=None):
    """
    Given a faculty dict, query PubMed and attach publication data.
    """
    name = faculty_member["name"]

    if pubmed_string and pubmed_string.startswith("ORCID:"):
        search_term = pubmed_string
    elif pubmed_string:
        search_term = pubmed_string
    else:
        search_term = build_pubmed_search_string(name)

    print(f"    PubMed: {name} → '{search_term}'")
    result = pubmed_search(search_term)

    pubs = []
    if result["ids"]:
        pubs = pubmed_fetch_summaries(result["ids"][:5])

    faculty_member["pubmed_search"] = search_term
    faculty_member["pubmed_count"] = result["count"]
    faculty_member["publications"] = pubs

    # Rate limiting — NCBI allows 3 req/sec without API key
    time.sleep(0.4)
    return faculty_member


# ---------------------------------------------------------------------------
# NIH RePORTER enrichment (optional, best-effort)
# ---------------------------------------------------------------------------

def fetch_nih_grants(name):
    """Query NIH RePORTER for active grants by PI name at UNC."""
    parts = name.strip().split()
    if len(parts) < 2:
        return []
    last = parts[-1]
    first = parts[0]

    payload = json.dumps({
        "criteria": {
            "pi_names": [{"last_name": last, "first_name": first}],
            "org_names": ["UNIVERSITY OF NORTH CAROLINA AT CHAPEL HILL"],
            "project_start_date": {"from_date": "2020-01-01"},
        },
        "limit": 5,
        "offset": 0,
        "fields": ["project_title", "fiscal_year", "award_amount", "project_start_date", "project_end_date"],
    }).encode()

    req = urllib.request.Request(
        "https://api.reporter.nih.gov/v2/projects/search",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "UNC-Research-Explorer/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])
        grants = []
        for r in results:
            title = r.get("project_title", "")
            year = r.get("fiscal_year", "")
            if title:
                grants.append({"title": title, "fiscal_year": year})
        return grants
    except Exception as e:
        print(f"    NIH RePORTER error for {name}: {e}")
        return []


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(config_path="scraper/departments.json", output_path="data/faculty.json",
        pubmed_delay=0.4, skip_nih=False, dept_filter=None):

    with open(config_path) as f:
        config = json.load(f)

    departments = config["departments"]
    if dept_filter:
        departments = [d for d in departments if dept_filter.lower() in d["name"].lower()]

    all_faculty = []
    dept_index = {}  # dept_name -> list of faculty

    # ---- Step 1: Scrape faculty pages ----
    print("\n=== Step 1: Scraping faculty pages ===")
    for dept in departments:
        print(f"\n[{dept['name']}]")
        faculty_list = scrape_faculty_from_page(dept["url"], dept["name"])
        dept_index[dept["name"]] = faculty_list
        all_faculty.extend(faculty_list)
        time.sleep(1)  # polite crawl delay

    print(f"\nTotal faculty scraped: {len(all_faculty)}")

    # ---- Step 2: Scrape profile pages for curated PubMed strings ----
    print("\n=== Step 2: Checking profiles for PubMed search strings ===")
    for f in all_faculty:
        if f.get("profile_url"):
            ps = scrape_profile_for_pubmed_string(f["profile_url"])
            if ps:
                print(f"  {f['name']}: found '{ps}'")
                f["pubmed_hint"] = ps
            time.sleep(0.5)

    # ---- Step 3: Enrich with PubMed ----
    print("\n=== Step 3: Enriching with PubMed ===")
    for i, f in enumerate(all_faculty):
        print(f"  [{i+1}/{len(all_faculty)}] {f['name']}")
        enrich_faculty_with_pubmed(f, pubmed_string=f.get("pubmed_hint"))
        time.sleep(pubmed_delay)

    # ---- Step 4: NIH RePORTER (optional) ----
    if not skip_nih:
        print("\n=== Step 4: Checking NIH RePORTER ===")
        for f in all_faculty:
            grants = fetch_nih_grants(f["name"])
            f["nih_grants"] = grants
            if grants:
                print(f"  {f['name']}: {len(grants)} grant(s)")
            time.sleep(0.3)
    else:
        for f in all_faculty:
            f["nih_grants"] = []

    # ---- Step 5: Write output ----
    output = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total_faculty": len(all_faculty),
        "departments": [d["name"] for d in departments],
        "faculty": all_faculty,
    }

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✓ Written to {output_path}")
    print(f"  {len(all_faculty)} faculty across {len(departments)} departments")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="UNC SOM faculty scraper")
    parser.add_argument("--config", default="scraper/departments.json")
    parser.add_argument("--output", default="data/faculty.json")
    parser.add_argument("--skip-nih", action="store_true", help="Skip NIH RePORTER step")
    parser.add_argument("--dept", help="Only scrape departments matching this string")
    parser.add_argument("--pubmed-delay", type=float, default=0.4,
                        help="Seconds between PubMed requests (default 0.4)")
    args = parser.parse_args()

    run(
        config_path=args.config,
        output_path=args.output,
        skip_nih=args.skip_nih,
        dept_filter=args.dept,
        pubmed_delay=args.pubmed_delay,
    )
