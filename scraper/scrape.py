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


def fetch_json(url, retries=3, delay=1.5):
    """Like fetch_url but sends Accept: application/json — used for REST API calls."""
    headers = {
        "User-Agent": "UNC-Research-Explorer/1.0 (student research tool; contact: jgbresearch@unc.edu)",
        "Accept": "application/json",
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
# Credential suffix pattern — uses negative lookbehind to avoid matching
# mid-word (e.g. "MS" in "Adams"). Comma-separated creds are also handled
# by clean_name_for_pubmed which strips everything after the first comma.
DEGREE_SUFFIXES = re.compile(
    r"(?<![A-Za-z])(Ph\.D\.?|M\.D\.?|D\.O\.?|M\.P\.H\.?|M\.B\.A\.?|"
    r"M\.S\.c\.?|B\.S\.c\.?|M\.H\.S\.?|"
    r"MD|PhD|DO|MPH|MBA|FACS|FACP|FAAN|RN|NP|PA-C|"
    r"PharmD|DDS|DVM|DrPH|ScD|JD|MBBS|BMBS|MHS|MHA|MSCR|MSPH|"
    r"FRCSC|FACOG|FAAD|FACR|FACEP|FAHA|FHRS|MPP|BSN|ARNP|CNM|"
    r"RD|BCPS|BCGP|CPP|BCNP|ABR|FASN|FANA|FASGE|FRCR|FACG|"
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
    r"accessibility|intranet|skip|menu|navigation|toggle|search|"
    r"building|clinic|hospital|location|floor|suite|wing|annex|"
    r"physicians|community|engagement|school|university|medicine|"
    r"neurology|cardiology|surgery|pediatrics|oncology|radiology|"
    r"services|resources|directions|parking|hours|phone|fax|address|"
    r"imaging|neuroradiology|interventional|musculoskeletal|thoracic|"
    r"abdominal|nuclear|mammography|ultrasound|fluoroscopy|"
    r"subspecialty|section|division|group|section|trauma|vascular|"
    r"breast|cardiac|body|head|neck|spine|pediatric imaging|"
    r"fellowship|residency|rotation|conference|seminar|lecture|"
    r"quality|safety|council|positions|open|local|links|follow|"
    r"notice|nondiscrimination|aviso|practicas|privadas|gift|"
    r"make a gift|county|rounds|grand|strategic|plan|annual|"
    r"population|health|sciences|commitment|training|history|"
    r"specialty|procedures|request|information|interest|"
    r"academic|affairs|radiological|exams|procedures|"
    r"residents|current|interdisciplinary|perspectives|connect|"
    r"fellows|obstetric|anesthesiology|regional|"
    r"movement disorders|multiple sclerosis|neurocritical|"
    r"neuromuscular|memory|cognitive|providers|assistants|"
    r"adjunct|position|application|emeritus|associate professor|"
    r"professor|instructor|lecturer|"
    r"making a referral|making an appointment|sublingual|immunotherapy|"
    r"find a doctor|my chart|show your support|scheduling|referrals|"
    r"impactful publications|social media|rheumatology care|"
    r"transitioning to adult|resident documents|darville|thompson laboratory|"
    r"vogt laboratory|pandya|hematology & sickle|developmental therapeutics|"
    r"inflammatory bowel|endoscopy|colonoscopy|primary care|adolescent care|"
    r"complex & diagnostic|development, behavior|child maltreatment|"
    r"genetics & metabolism|infectious diseases(?! \w)|pediatric (?!faculty)|"
    r"know before you go|referring physician|living in chapel hill|"
    r"striving for|that define us|faces that define|our history|"
    r"make a gift|population health|strategic plan|annual report|"
    r"grand rounds|fellowship|application overview|specialty procedures)",
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
    # Reject all-caps strings (e.g. "CLINIC LOCATIONS", "UNC HOSPITALS")
    words = clean.split()
    if any(w.isupper() and len(w) > 2 and w.isalpha() for w in words):
        return False
    # Reject strings with digits (e.g. "3009 Old Clinic Building")
    if re.search(r"\d", clean):
        return False
    # Should have 2-4 space-separated words, each starting with uppercase
    if len(words) < 2 or len(words) > 5:
        return False
    # Most words should start with uppercase
    caps = sum(1 for w in words if w and w[0].isupper())
    return caps >= len(words) - 1


def extract_name(raw_text):
    """Clean raw link text into a canonical name (no credentials)."""
    text = raw_text.strip()
    # Remove parenthetical nicknames e.g. (Yemi)
    text = re.sub(r"\([^)]*\)", "", text)
    # Remove quoted nicknames e.g. "Yemi"
    text = re.sub(r'["\'\u201c\u201d][^"\'\u201c\u201d]*["\'\u201c\u201d]', "", text)
    text = TITLE_PREFIXES.sub("", text).strip()
    # Cut off at the first comma — everything after a comma is credentials
    # (MD, PhD, FACS, FSCMR, Jr, Sr, II, III, etc.), regardless of which
    # specific abbreviation it is. This is more robust than trying to
    # enumerate every credential abbreviation in DEGREE_SUFFIXES.
    text = text.split(",")[0].strip()
    # Also strip any trailing degree suffix that appears without a comma
    # (e.g. "John Smith MD" or "Jane Doe PhD")
    text = DEGREE_SUFFIXES.sub("", text).strip()
    # Strip trailing commas, dots, spaces left by degree removal
    text = re.sub(r"[\s,\.]+$", "", text).strip()
    # Collapse internal whitespace
    text = re.sub(r"\s+", " ", text)
    return text


def scrape_faculty_via_wp_rest(base_url, dept_name):
    """
    Scrape faculty from a WordPress/Toolset site using the ud_entry custom post type.
    Used for UNC Pediatrics whose faculty pages are JavaScript-rendered.

    The site stores all people as ud_entry posts at:
      /wp-json/wp/v2/ud_entry?per_page=100&page=N

    Each entry has:
      - title.rendered: full name with credentials
      - ud_entry_custom_fields: {ud_first_name, ud_last_name, ud_positions}
      - class_list: includes ud_division-{code} for division membership
      - link: profile URL

    We filter to faculty (MD, PhD, DO, etc.) and exclude admin/nursing/support staff.
    Only called once per department (first URL), subsequent division URLs are skipped.
    """
    # Only run this once for the whole Pediatrics department
    # (all divisions share the same wp-json endpoint)
    parsed = urllib.parse.urlparse(base_url)
    path_parts = parsed.path.strip("/").split("/")
    wp_base = f"{parsed.scheme}://{parsed.netloc}/{path_parts[0]}"
    api_url = f"{wp_base}/wp-json/wp/v2/ud_entry"

    # Faculty credential patterns — only include people with these in their title
    FACULTY_CREDENTIALS = re.compile(
        r"\b(MD|PhD|DO|PharmD|DrPH|DVM|DDS|MBBS|ScD|MD-PhD|MD/PhD)\b",
        re.IGNORECASE
    )

    # Titles that identify trainees — exclude anyone whose position matches these
    TRAINEE_TITLES = re.compile(
        r"\b(resident|residency|intern|interns|fellow(?!ship\s+director|ship\s+program\s+director)"
        r"|student|clerkship|trainee|house\s+officer|pgy[-\s]?\d|class\s+of\s+\d{4})\b",
        re.IGNORECASE
    )

    faculty = []
    seen = set()
    page = 1
    total_entries_seen = 0
    rejected_no_credential = 0
    rejected_wrong_dept = 0

    while True:
        url = (
            f"{api_url}?per_page=100&page={page}"
            f"&_fields=id,title,link,ud_entry_custom_fields,class_list,ud_division"
        )
        html = fetch_json(url)
        if not html:
            print(f"    WP REST: no response for page {page} (url: {api_url})")
            break
        try:
            entries = json.loads(html)
        except Exception as e:
            print(f"    WP REST: JSON parse error on page {page}: {e}")
            print(f"    Raw response (first 300 chars): {html[:300]}")
            break
        if not isinstance(entries, list) or len(entries) == 0:
            break

        total_entries_seen += len(entries)
        for entry in entries:
            # Get name from title
            raw_title = entry.get("title", {})
            if isinstance(raw_title, dict):
                raw_title = raw_title.get("rendered", "")
            raw_title = re.sub(r"<[^>]+>", "", raw_title).strip()  # strip HTML entities

            # Pull position text once — used for both credential and trainee checks
            custom = entry.get("ud_entry_custom_fields", {})
            if not isinstance(custom, dict):
                custom = {}
            positions = custom.get("ud_positions", [])
            if not isinstance(positions, list):
                positions = []
            position_text = " ".join(
                p.get("ud_title", "") for p in positions if isinstance(p, dict)
            )

            # Exclude trainees — check both title and position
            if TRAINEE_TITLES.search(raw_title) or TRAINEE_TITLES.search(position_text):
                rejected_no_credential += 1
                continue

            # Only include people with faculty credentials
            if not FACULTY_CREDENTIALS.search(raw_title):
                # Also check ud_positions for faculty title
                if not FACULTY_CREDENTIALS.search(position_text):
                    # Check for professor/instructor titles
                    if not re.search(
                        r"\b(professor|instructor|director|chief|lecturer)\b",
                        position_text, re.IGNORECASE
                    ):
                        rejected_no_credential += 1
                        continue

            name = extract_name(raw_title)
            if not name or len(name) < 4:
                continue
            name_key = name.lower()
            if name_key in seen:
                continue
            seen.add(name_key)

            link = entry.get("link", "")

            # Skip people whose profile URL belongs to a different department.
            # e.g. Brent Kinder appears in Neurology's ud_entry feed because he
            # treats TSC patients, but his profile is under /medicine/pulmonary/
            # The wp_base path (e.g. '/neurology') should appear in their profile URL.
            if link and wp_base:
                wp_path = urllib.parse.urlparse(wp_base).path  # e.g. '/neurology'
                profile_path = urllib.parse.urlparse(link).path
                if wp_path and wp_path not in profile_path:
                    rejected_wrong_dept += 1
                    continue  # belongs to a different department

            # Get division from class_list
            class_list = entry.get("class_list", [])
            if not isinstance(class_list, list):
                class_list = []
            division = ""
            for cls in class_list:
                if isinstance(cls, str) and cls.startswith("ud_division-") and cls != "ud_division-all_peds":
                    division = cls.replace("ud_division-", "").replace("_", " ").title()
                    break

            faculty.append({
                "name": name,
                "profile_url": link,
                "department": dept_name,
                "role": division,
            })

        print(f"    WP REST API page {page}: {len(entries)} entries, {len(faculty)} faculty so far")
        page += 1
        time.sleep(0.3)

    print(f"    WP REST summary: {total_entries_seen} total entries, "
          f"{rejected_no_credential} rejected (no credential), "
          f"{rejected_wrong_dept} rejected (wrong dept), "
          f"{len(faculty)} kept")

    return faculty


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
        # Accept links that look like profile pages on any UNC domain
        if href and ("people" in href or "directory" in href or
                     "faculty" in href or "profile" in href or
                     "unclineberger.org/directory/" in href):
            name = extract_name(text)
            if name and name.lower() not in seen_names and len(name) > 4:
                seen_names.add(name.lower())
                # Build absolute URL — handle med.unc.edu and unclineberger.org
                if href.startswith("http"):
                    profile_url = href
                elif href.startswith("/"):
                    parsed_base = urllib.parse.urlparse(url)
                    profile_url = f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
                else:
                    profile_url = f"https://www.med.unc.edu{href}"
                faculty.append({
                    "name": name,
                    "profile_url": profile_url,
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

    # If HTML scraping returned nothing or suspiciously few results, the page
    # is likely JavaScript-rendered. Try the WordPress REST API as a fallback.
    # Threshold of 10 catches cases where nav items slip through the name filter.
    if len(faculty) < 10:
        parsed = urllib.parse.urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        wp_key = f"{parsed.netloc}/{path_parts[0]}"
        if not hasattr(scrape_faculty_from_page, "_wp_rest_cache"):
            scrape_faculty_from_page._wp_rest_cache = {}
        if wp_key in scrape_faculty_from_page._wp_rest_cache:
            print(f"  WP REST already fetched for {wp_key} — skipping")
            return faculty  # return whatever HTML found (may be empty)
        print(f"  {'No' if len(faculty) == 0 else 'Only ' + str(len(faculty))} faculty from HTML — trying WP REST API...")
        rest_faculty = scrape_faculty_via_wp_rest(url, dept_name)
        if rest_faculty:
            scrape_faculty_from_page._wp_rest_cache[wp_key] = True
            print(f"  Found {len(rest_faculty)} faculty via WP REST API")
            return rest_faculty
        scrape_faculty_from_page._wp_rest_cache[wp_key] = True

    return faculty


HINT_GARBAGE_WORDS = re.compile(
    r"\b(including|biventricular|pacemaker|device|therapy|treatment|"
    r"surgery|medicine|health|care|service|program|department|division|"
    r"center|clinic|hospital|research|education|training)\b",
    re.IGNORECASE
)

# Common last names where article-level UNC affiliation is not enough to
# confirm the target author — require per-author affiliation data instead.
AMBIGUOUS_LASTNAMES = {
    "smith", "lee", "kim", "wang", "chen", "johnson", "brown",
    "jones", "taylor", "williams", "wilson", "moore", "harris",
    "martin", "thompson", "garcia", "martinez", "anderson",
    "clark", "lewis", "robinson", "walker", "young", "hall",
    "allen", "wright", "scott", "hill", "green", "adams",
    "baker", "nelson", "carter", "mitchell", "perez", "roberts",
    "turner", "phillips", "campbell", "parker", "evans", "edwards",
}


def scrape_profile_for_pubmed_string(profile_url, html=None):
    """
    Try to extract a curated PubMed search string from a faculty profile page.
    Accepts pre-fetched HTML to avoid double-fetching when is_trainee_profile
    has already retrieved the page.
    """
    if not profile_url:
        return None
    if html is None:
        html = fetch_url(profile_url)
    if not html:
        return None

    # Pattern 1: "using [search string] as search criteria"
    match = re.search(
        r"(?:search\s+(?:for\s+)?publications?\s+on\s+pubmed\s+using\s+|"
        r"pubmed\s+using\s+)([A-Za-z ,*]+?)(?:\s+as\s+search\s+criteria|"
        r"\s+as\s+search|\s*\n)",
        html, re.IGNORECASE
    )
    if match:
        hint = match.group(1).strip().lstrip("*").strip()
        hint = re.sub(r",\s*", " ", hint).strip()
        return hint

    # Pattern 2: parenthetical like "on PubMed (*Lee, CN)" or "(Lee CN)"
    match = re.search(
        r"pubmed[^(]*\(\*?([A-Za-z]+,?\s+[A-Za-z]+)\)",
        html, re.IGNORECASE
    )
    if match:
        hint = match.group(1).strip().lstrip("*").strip()
        hint = re.sub(r",\s*", " ", hint).strip()
        return hint

    # Pattern 3: PubMed URL — extract and parse the term parameter
    # Handles:
    #   ?term=Stouffer+GA
    #   ?term=Stouffer+GA[Author]
    #   ?term=Stouffer+GA[Author]+AND+UNC[Affiliation]
    pubmed_url_match = re.search(
        r"pubmed\.ncbi\.nlm\.nih\.gov/\S*[?&]term=(\S+)",
        html, re.IGNORECASE
    )
    if pubmed_url_match:
        raw = urllib.parse.unquote_plus(pubmed_url_match.group(1))
        raw = raw.strip('"\' <>)&')
        author_term = None

        # Sub-pattern A: extract [Author] or [au] tagged token
        # e.g. 'Stouffer GA[Author]' or '"Stouffer GA"[Author]'
        author_match = re.search(
            r"[\x22\x27]?([A-Za-z][A-Za-z\s\.\-*,]{1,40})[\x22\x27]?\[(?:Author|au)\]",
            raw, re.IGNORECASE
        )
        if author_match:
            author_term = author_match.group(1).strip()
            # strip surrounding quotes
            author_term = author_term.strip(chr(34)).strip(chr(39)).strip()

        # Sub-pattern B: simple short string, no operators or field tags
        elif (len(raw) <= 40
              and "AND" not in raw.upper()
              and "[" not in raw
              and "-" not in raw
              and re.match(r"^[A-Za-z ,.*+]+$", raw)):
            author_term = raw.replace("+", " ").strip()

        if author_term:
            author_term = re.sub(r",\s*", " ", author_term)
            author_term = re.sub(r"\s+", " ", author_term).strip()
            if not HINT_GARBAGE_WORDS.search(author_term) and len(author_term) >= 3:
                return author_term

    # Pattern 4: MyNCBI bibliography URL
    # e.g. https://www.ncbi.nlm.nih.gov/myncbi/brent.hanks.1/bibliography/public/
    # Return a MYNCBI: marker so the enrichment function can fetch PMIDs directly
    # from the faculty's curated bibliography page — bypassing PubMed search entirely.
    myncbi_match = re.search(
        r'(https?://www\.ncbi\.nlm\.nih\.gov/myncbi/[^/]+/bibliography[^\s"]*)',
        html, re.IGNORECASE
    )
    if myncbi_match:
        bib_url = myncbi_match.group(1).rstrip("/")
        if not bib_url.endswith("/public"):
            bib_url += "/public"
        return f"MYNCBI:{bib_url}"

    # Pattern 5: ORCID — matches both public profile and login URLs
    # e.g. https://orcid.org/0000-0002-2803-3272
    # e.g. https://orcid.org/my-orcid?orcid=0000-0002-2803-3272
    orcid_match = re.search(
        r"orcid[=:/]+(\d{4}-\d{4}-\d{4}-\d{3}[\dX])",
        html, re.IGNORECASE
    )
    if orcid_match:
        return f"ORCID:{orcid_match.group(1)}"

    return None


TRAINEE_PROFILE_PATTERN = re.compile(
    r"\b(residency|intern(?!al)|fellow(?!\s+(?:of|award|member))"
    r"|pgy[-\s]?\d|class\s+of\s+\d{4}|house\s+officer|medical\s+student"
    r"|dental\s+student|pharmacy\s+student|graduate\s+student"
    r"|(?:neurology|medicine|surgery|pediatrics|psychiatry|radiology"
    r"|emergency|internal\s+medicine|family\s+medicine)\s+resident)\b",
    re.IGNORECASE
)

ATTENDING_PROFILE_PATTERN = re.compile(
    r"\b(professor|assistant\s+professor|associate\s+professor|instructor"
    r"|lecturer|attending\s+physician|clinical\s+faculty|adjunct\s+professor"
    r"|division\s+chief|department\s+chair|program\s+director)\b",
    re.IGNORECASE
)

# Must have at least one of these to be included — filters out admin/staff/coordinators
FACULTY_CREDENTIAL_PATTERN = re.compile(
    r"\b(MD|PhD|DO|PharmD|DrPH|DVM|DDS|MBBS|ScD|NP|PA-C|CNM|ARNP"
    r"|professor|instructor|lecturer|attending\s+physician"
    r"|division\s+chief|department\s+chair)\b",
    re.IGNORECASE
)

# Clinical departments we want to include from Lineberger.
# Anyone whose Lineberger profile lists one of these departments is kept;
# everyone else (Biostatistics, Epidemiology, Pharmacy, Public Health, etc.) is excluded.
LINEBERGER_CLINICAL_DEPTS = {
    "medicine", "surgery", "pediatrics", "radiology", "radiation oncology",
    "oncology", "hematology", "pathology", "neurology", "neurosurgery",
    "obstetrics", "gynecology", "urology", "dermatology", "ophthalmology",
    "otolaryngology", "anesthesiology", "emergency medicine", "psychiatry",
    "orthopaedics", "physical medicine", "rehabilitation", "family medicine",
    "internal medicine", "cardiology", "gastroenterology", "pulmonary",
    "infectious disease", "nephrology", "rheumatology", "endocrinology",
    "geriatric", "hospital medicine", "general medicine", "clinical epidemiology",
    "microbiology", "immunology", "biochemistry", "genetics",
    "cell biology", "physiology", "pharmacology",
}


def is_lineberger_clinical(html):
    """
    For a Lineberger profile page, check whether the faculty member's
    listed department is a clinical/SOM department we care about.
    Returns True if they should be included, False if they're from a
    non-clinical school (Public Health, Pharmacy, Nursing, Arts & Sciences, etc.).
    Returns True by default if department can't be determined (don't exclude if unsure).
    """
    if not html:
        return True

    # Strip tags and look at the first ~500 chars where department appears
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    # The department line on Lineberger profiles looks like:
    # "MD, PhD\nAssociate Professor, Radiation Oncology, Biochemistry and Biophysics"
    # It appears near the top of the page body, after the name.
    # We grab the first 1500 chars to catch it reliably.
    snippet = text[:1500].lower()

    # If any clinical dept keyword appears, keep them
    for dept in LINEBERGER_CLINICAL_DEPTS:
        if dept in snippet:
            return True

    # Explicit non-clinical markers — if these appear and nothing clinical did, exclude
    NON_CLINICAL = [
        "gillings school", "school of public health", "eshelman school",
        "school of pharmacy", "school of nursing", "school of social work",
        "kenan-flagler", "school of information", "college of arts",
        "biostatistics", "epidemiology", "health behavior", "health policy",
        "environmental sciences", "nutrition", "maternal and child health",
    ]
    for marker in NON_CLINICAL:
        if marker in snippet:
            return False

    # Can't determine — include by default
    return True


def is_trainee_profile(html):
    """
    Given already-fetched profile HTML, determine if this person should be excluded.
    Returns True (exclude) for:
      - Trainees (residents, fellows, students)
      - Admin/staff with no faculty credentials (no MD/PhD/DO/professor title)
    Returns False (keep) for attending faculty.
    """
    if not html:
        return False

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    snippet = text[:3000]

    # If they have a clear attending/faculty title, keep them
    if ATTENDING_PROFILE_PATTERN.search(snippet):
        return False

    # If they have no faculty credential at all (MD, PhD, DO, professor, etc.),
    # they're admin/staff — exclude
    if not FACULTY_CREDENTIAL_PATTERN.search(snippet):
        return True

    # If we see a trainee title, exclude
    if TRAINEE_PROFILE_PATTERN.search(snippet):
        return True

    return False

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
PUBMED_EMAIL = "jgbresearch@unc.edu"  # NCBI requests a contact email
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "")  # set in GitHub Actions secrets
# With an API key: 10 req/sec allowed; without: 3 req/sec
PUBMED_SLEEP = 0.15 if NCBI_API_KEY else 0.4
COMPUTED_AUTHORS_API = "https://www.ncbi.nlm.nih.gov/research/litsense-api/api/author/"


def fetch_pmids_via_computed_authors(name, seed_pmid=None):
    """
    Use NCBI's Computed Authors API to get all disambiguated publications for
    a faculty member. This is NCBI's own ML-based author disambiguation system,
    updated weekly, and far more reliable than name+affiliation searching.

    Requires one seed PMID — any paper we know belongs to this author.
    Returns (pmids, total_count) where pmids is sorted newest-first.

    API docs: https://www.ncbi.nlm.nih.gov/research/bionlp/APIs/authors/
    Call format: ?query={pmid} {LastName} {Initial}
    """
    if not seed_pmid:
        return [], 0

    # Build the author name in 'LastName Initial' format
    clean = clean_name_for_pubmed(name)
    parts = [p.rstrip(".") for p in clean.split() if p]
    parts = [p for p in parts if p and (len(p) > 1 or p.isalpha())]
    if len(parts) < 2:
        return [], 0

    last = parts[-1]
    first_initial = parts[0][0].upper()
    author_query = f"{last} {first_initial}"

    query = f"{seed_pmid} {author_query}"
    url = f"{COMPUTED_AUTHORS_API}?query={urllib.parse.quote(query)}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"unc-research-explorer/{PUBMED_EMAIL}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        results = data.get("results", [])
        if not results:
            return [], 0
        # May return multiple author clusters — take the one containing our seed PMID
        for cluster in results:
            pmids = [str(p) for p in cluster.get("pmids", [])]
            if str(seed_pmid) in pmids:
                # Sort newest first (PMIDs are roughly chronological)
                pmids_sorted = sorted(pmids, key=lambda x: int(x), reverse=True)
                return pmids_sorted, len(pmids_sorted)
        # Seed not found in any cluster — don't guess, return empty
        print(f"    Computed Authors: seed PMID {seed_pmid} not found in any cluster")
        return [], 0
    except Exception as e:
        print(f"    Computed Authors API error for '{author_query}': {e}")
        return [], 0


def fetch_pmids_by_author_id(author_id, max_results=100):
    """
    Use the NCBI E-utilities [Author Identifier] field to retrieve PMIDs
    for a faculty member by their unique NCBI author ID (the slug from their
    MyNCBI URL, e.g. 'brent.hanks.1').

    API call:
      esearch.fcgi?db=pubmed&term=brent.hanks.1[Author Identifier]&retmax=100

    This is far more reliable than guessing name format (Hanks B vs Hanks BA).
    """
    import urllib.parse
    term = urllib.parse.quote(f"{author_id}[Author Identifier]")
    api_key_param = f"&api_key={NCBI_API_KEY}" if NCBI_API_KEY else ""
    url = (
        f"{PUBMED_BASE}esearch.fcgi"
        f"?db=pubmed&term={term}&retmax={max_results}&retmode=json&email={PUBMED_EMAIL}{api_key_param}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
        pmids = data["esearchresult"]["idlist"]
        count = int(data["esearchresult"]["count"])
        if count > 1000:
            print(f"    Author Identifier returned {count} PMIDs — slug '{author_id}' is invalid, skipping")
            return [], 0
        return pmids, count
    except Exception as e:
        print(f"    Author Identifier search error for '{author_id}': {e}")
        return [], 0


def clean_name_for_pubmed(name):
    """
    Strip credentials, nicknames, punctuation from a raw scraped name
    before building a PubMed search string.
    E.g. 'Adeyemi "Yemi" Ogunleye' -> 'Adeyemi Ogunleye'
         'Delora Mount, FAAP'       -> 'Delora Mount'
         'Katherine Rodby'          -> 'Katherine Rodby'
    """
    # Remove anything in quotes (nicknames)
    name = re.sub(r'["\u201c\u201d][^"]*["\u201c\u201d]', '', name)
    # Strip degree/credential suffixes (comma-separated at end)
    name = DEGREE_SUFFIXES.sub("", name)
    # Remove stray punctuation
    name = re.sub(r',.*$', '', name)
    name = re.sub(r'[;"]', '', name)
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def build_pubmed_search_string(name):
    """
    Build a PubMed author search string from a faculty name.
    PubMed indexes authors as 'Lastname FI' or 'Lastname FirstMiddle'.
    We include middle initials when present for specificity.
    E.g. 'William Y. Kim'  -> 'Kim WY'
         'Brian C. Miller' -> 'Miller BC'
         'Jennifer Carr'   -> 'Carr Jennifer'
         'E. Claire Dees'  -> 'Dees EC'
    """
    clean = clean_name_for_pubmed(name)
    # Strip stray dots/punctuation left by degree removal, filter empty parts
    parts = [p.rstrip(".") for p in clean.split() if p]
    parts = [p for p in parts if p and (len(p) > 1 or p.isalpha())]
    if len(parts) < 2:
        return clean
    last = parts[-1]
    first_parts = parts[:-1]  # everything before the last name

    # If any part is a single letter (initial), use initials format for all first parts
    # e.g. ['William', 'Y'] → 'WY', ['E', 'Claire'] → 'EC'
    has_initial = any(len(p) == 1 and p.isalpha() for p in first_parts)
    if has_initial:
        initials = "".join(p[0].upper() for p in first_parts if p and p[0].isalpha())
        return f"{last} {initials}"

    # No initials — use full first name only (middle name dropped to avoid mismatch)
    return f"{last} {first_parts[0]}"


def pubmed_search(search_term, affiliation="University of North Carolina", max_results=5):
    """
    Query PubMed and return list of PMIDs.
    search_term: either a name-based string like 'Doe JA' or an ORCID.
    """
    current_year = datetime.utcnow().year
    if search_term.startswith("ORCID:"):
        orcid = search_term.replace("ORCID:", "")
        query = f'{orcid}[auid] AND "{affiliation}"[Affiliation] AND ("2018"[PDAT] : "{current_year}"[PDAT])'
    elif affiliation:
        query = f'"{search_term}"[Author] AND "{affiliation}"[Affiliation] AND ("2018"[PDAT] : "{current_year}"[PDAT])'
    else:
        # No affiliation filter — used as last resort for recently recruited faculty
        query = f'"{search_term}"[Author] AND ("2018"[PDAT] : "{current_year}"[PDAT])'

    params = urllib.parse.urlencode({
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
        "sort": "date",
        "tool": "unc-research-explorer",
        "email": PUBMED_EMAIL,
        **({"api_key": NCBI_API_KEY} if NCBI_API_KEY else {}),
    })
    url = PUBMED_BASE + "esearch.fcgi?" + params

    try:
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = json.loads(resp.read())
                result = data.get("esearchresult", {})
                return {
                    "count": int(result.get("count", 0)),
                    "ids": result.get("idlist", []),
                }
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 2 ** attempt
                    time.sleep(wait)
                    continue
                raise
        return {"count": 0, "ids": []}
    except Exception as e:
        print(f"    PubMed search error for '{search_term}': {e}")
        return {"count": 0, "ids": []}


UNC_AFFILIATION_TERMS = [
    "north carolina", "unc", "chapel hill", "lineberger"
]


def affiliation_is_unc(aff_string):
    """Return True if the affiliation string mentions UNC."""
    aff_lower = aff_string.lower()
    return any(t in aff_lower for t in UNC_AFFILIATION_TERMS)


def pubmed_fetch_summaries(pmids, verify_affiliation=True, search_term=""):
    """
    Fetch article details for a list of PMIDs.
    Uses efetch (XML) to get affiliation data, then filters to only
    papers where at least one author is affiliated with UNC.
    Falls back to esummary if efetch fails.
    """
    if not pmids:
        return []

    # Use efetch XML to get affiliation info — retry up to 3x on 429
    params = urllib.parse.urlencode({
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "rettype": "abstract",
        "tool": "unc-research-explorer",
        "email": PUBMED_EMAIL,
        **({"api_key": NCBI_API_KEY} if NCBI_API_KEY else {}),
    })
    url = PUBMED_BASE + "efetch.fcgi?" + params

    xml_data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                xml_data = resp.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt  # 1s, 2s, 4s
                print(f"    PubMed efetch 429 — waiting {wait}s before retry {attempt+1}/3")
                time.sleep(wait)
            else:
                print(f"    PubMed efetch error: {e}, falling back to esummary")
                return pubmed_fetch_summaries_fallback(pmids)
        except Exception as e:
            print(f"    PubMed efetch error: {e}, falling back to esummary")
            return pubmed_fetch_summaries_fallback(pmids)

    if xml_data is None:
        print(f"    PubMed efetch failed after 3 retries, falling back to esummary")
        return pubmed_fetch_summaries_fallback(pmids)

    # Parse XML manually (avoid external deps)
    pubs = []
    # Split into individual articles
    articles = re.split(r"<PubmedArticle>", xml_data)[1:]
    for article_xml in articles:
        # Extract PMID
        pmid_match = re.search(r"<PMID[^>]*>(\d+)</PMID>", article_xml)
        if not pmid_match:
            continue
        pmid = pmid_match.group(1)

        # Extract title
        title_match = re.search(r"<ArticleTitle[^>]*>(.*?)</ArticleTitle>",
                                article_xml, re.DOTALL)
        title = ""
        if title_match:
            title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()

        # Extract journal
        journal_match = re.search(r"<ISOAbbreviation>(.*?)</ISOAbbreviation>",
                                  article_xml)
        if not journal_match:
            journal_match = re.search(r"<Title>(.*?)</Title>", article_xml)
        journal = journal_match.group(1).strip() if journal_match else ""

        # Extract year
        year_match = re.search(r"<PubDate>.*?<Year>(\d{4})</Year>.*?</PubDate>",
                               article_xml, re.DOTALL)
        if not year_match:
            year_match = re.search(r"<Year>(\d{4})</Year>", article_xml)
        year = year_match.group(1) if year_match else ""

        # In PubMed XML, affiliations are stored in two places:
        # 1. <AffiliationInfo> inside <Author> blocks (per-author, newer articles)
        # 2. <Affiliation> directly in <AuthorList> (older articles, article-level)
        # We check both and require the TARGET author's last name to appear
        # in a UNC-affiliated author block where possible.

        if verify_affiliation:
            # Collect all author blocks with their affiliations
            author_blocks = re.findall(r"<Author[^>]*>(.*?)</Author>", article_xml, re.DOTALL)
            # Also collect all affiliations at article level as fallback
            all_affiliations = re.findall(r"<Affiliation>(.*?)</Affiliation>", article_xml)
            all_aff_text = " ".join(all_affiliations)

            target_last = search_term.split()[0].lower() if search_term else ""
            target_has_unc = False
            any_has_unc = affiliation_is_unc(all_aff_text)

            # Track whether ANY author block has per-author affiliations stored
            any_block_has_affs = False
            for block in author_blocks:
                last_match = re.search(r"<LastName>(.*?)</LastName>", block)
                # Per-author affiliations are in <AffiliationInfo><Identifier>
                # or directly as <Affiliation> inside the author block
                block_affs = re.findall(r"<Affiliation>(.*?)</Affiliation>", block)
                block_aff_text = " ".join(block_affs)

                if block_affs:
                    any_block_has_affs = True

                if last_match:
                    last = last_match.group(1).strip().lower()
                    if last == target_last:
                        # This is our target author — check their affiliation
                        if block_affs and affiliation_is_unc(block_aff_text):
                            target_has_unc = True

            # If the paper has NO per-author affiliations at all (older papers),
            # fall back to article-level — but only if the target last name is
            # rare enough to be unambiguous (not smith, lee, kim, wang, etc.)
            if not any_block_has_affs and any_has_unc:
                if target_last not in AMBIGUOUS_LASTNAMES:
                    # No per-author data, but article has UNC affiliation and
                    # the last name is distinctive enough to trust the match
                    target_has_unc = True
                else:
                    print(f"      PMID {pmid} — UNC affiliation found but last name '{target_last}' too common to verify per-author")

            if not any_has_unc:
                print(f"      Skipping PMID {pmid} — no UNC affiliation in article")
                continue
            # If we have per-author data and target isn't at UNC, skip
            if not target_has_unc and any_has_unc:
                print(f"      PMID {pmid} — UNC affiliation found but not for target author ({target_last})")
                continue

        if title:
            pubs.append({
                "pmid": pmid,
                "title": title,
                "journal": journal,
                "year": year,
            })

    return pubs


def pubmed_fetch_summaries_fallback(pmids):
    """Fallback using esummary — basic affiliation check via affiliationlist field."""
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
            # esummary includes affiliationlist in some records — check it
            aff_list = art.get("affiliationlist", [])
            aff_text = " ".join(a.get("affiliation", "") for a in aff_list)
            if aff_list and not affiliation_is_unc(aff_text):
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
        print(f"    PubMed esummary fallback error: {e}")
        return []


def sanitize_hint(hint, faculty_name):
    """
    Validate and normalize a profile-scraped PubMed hint.
    Returns a clean search term or None if the hint is unusable.
    """
    if not hint:
        return None

    # ORCID — always valid
    if hint.startswith("ORCID:"):
        return hint

    hint_clean = hint.replace("*", "").strip()

    # Reject if contains non-name words (scraped from clinical description)
    if HINT_GARBAGE_WORDS.search(hint_clean):
        print(f"    Rejecting garbage hint '{hint}'")
        return None

    # Reject if contains digits — but NOT MYNCBI/ORCID markers which legitimately contain them
    if re.search(r"\d", hint_clean) and not hint_clean.startswith(("MYNCBI:", "ORCID:")):
        print(f"    Rejecting hint with digits '{hint}'")
        return None

    # Reject if too long to be an author string — but NOT MYNCBI/ORCID markers
    if len(hint_clean) > 40 and not hint_clean.startswith(("MYNCBI:", "ORCID:")):
        print(f"    Rejecting overly long hint '{hint}'")
        return None

    hint_parts = hint_clean.split()
    if not hint_parts:
        return None

    # Detect initials format: second+ tokens all ≤2 chars (e.g. 'Carr JC', 'Lee CN')
    is_initials_format = (
        len(hint_parts) >= 2 and
        all(len(p) <= 2 for p in hint_parts[1:])
    )

    # Detect natural-order full name (e.g. 'Anil K. Gehi', 'sameer prasada')
    # Signal: first token matches faculty first name, last token matches faculty last name
    clean_faculty = clean_name_for_pubmed(faculty_name)
    faculty_parts = clean_faculty.split()
    faculty_first = faculty_parts[0].lower() if faculty_parts else ""
    faculty_last = faculty_parts[-1].lower() if faculty_parts else ""
    hint_first = hint_parts[0].lower().rstrip(".")
    hint_last = hint_parts[-1].lower().rstrip(".")

    # All faculty name parts (lowercased, no punctuation) for matching
    faculty_all_parts = set(p.lower().rstrip(".") for p in faculty_parts)

    # Detect reversed order: hint starts with any part of faculty last name
    # e.g. 'nichols timothy', 'Lawrence Klein J' (middle name as first word)
    is_reversed = (
        len(hint_parts) >= 2 and
        (hint_first == faculty_last or
         (hint_first in faculty_all_parts and hint_first != faculty_first))
    )

    # Detect natural-order full name (first name first)
    # e.g. 'Anil K. Gehi', 'sameer prasada'
    is_natural_order = (
        len(hint_parts) >= 2 and
        hint_first == faculty_first and
        not is_reversed
    )

    if "*" in hint and not is_initials_format:
        # Curated wildcard — just strip the *
        return hint_clean
    elif is_initials_format or is_natural_order or is_reversed:
        # Any of these — convert to canonical Lastname Firstname
        return build_pubmed_search_string(faculty_name)
    else:
        # Unknown format — use as-is but log it
        return hint_clean


def enrich_faculty_with_pubmed(faculty_member, pubmed_string=None):
    """
    Given a faculty dict, query PubMed and attach publication data.
    """
    name = faculty_member["name"]

    # Manual override is stored as "OVERRIDE:searchterm" — check it BEFORE sanitize_hint
    # so the sanitizer doesn't strip the prefix
    raw_hint = pubmed_string or ""
    if raw_hint.startswith("OVERRIDE:"):
        search_term = raw_hint.replace("OVERRIDE:", "").strip()
        if search_term == "SKIP":
            print(f"    Skipping {name} per manual override")
            faculty_member["pubmed_search"] = "SKIP"
            faculty_member["pubmed_count"] = 0
            faculty_member["pubmed_verified"] = 0
            faculty_member["pubmed_ambiguous"] = False
            faculty_member["publications"] = []
            return faculty_member
        print(f"    Using manual override: '{search_term}'")
        result = pubmed_search(search_term, max_results=15)
        print(f"    PubMed: {name} → '{search_term}' ({result['count']} results)")
        pubs = []
        if result["ids"]:
            candidates = pubmed_fetch_summaries(result["ids"][:15], search_term=search_term, verify_affiliation=False)
            pubs = candidates[:5]
            if pubs:
                seed_pmid = pubs[0]["pmid"]
                print(f"    Computed Authors: seeding with PMID {seed_pmid}")
                ca_pmids, ca_count = fetch_pmids_via_computed_authors(name, seed_pmid=seed_pmid)
                if ca_count > 0 and ca_count <= 500:
                    print(f"    Computed Authors: found {ca_count} total PMIDs for this author")
                    ca_candidates = pubmed_fetch_summaries(ca_pmids[:20], search_term=search_term, verify_affiliation=False)
                    if ca_candidates:
                        pubs = ca_candidates[:5]
                        faculty_member["pubmed_count"] = ca_count
        faculty_member["pubmed_search"] = search_term
        faculty_member["pubmed_count"] = faculty_member.get("pubmed_count") or result["count"]
        faculty_member["pubmed_verified"] = len(pubs)
        faculty_member["pubmed_ambiguous"] = False
        faculty_member["publications"] = pubs
        time.sleep(PUBMED_SLEEP)
        return faculty_member

    clean_hint = sanitize_hint(pubmed_string, name)

    # MyNCBI bibliography — use E-utilities [Author Identifier] search.
    # The MyNCBI URL slug (e.g. 'brent.hanks.1') IS the NCBI author identifier,
    # searchable via: esearch?term=brent.hanks.1[Author Identifier]
    # This is exact and unambiguous — no guessing at 'Hanks B' vs 'Hanks BA'.
    if clean_hint and clean_hint.startswith("MYNCBI:"):
        bib_url = clean_hint.replace("MYNCBI:", "")
        # Extract the author ID slug from the URL
        # e.g. .../myncbi/brent.hanks.1/bibliography/... → 'brent.hanks.1'
        author_id = bib_url.rstrip("/").split("/myncbi/")[-1].split("/")[0]
        print(f"    MyNCBI Author ID '{author_id}' → querying via [Author Identifier]")
        pmids, count = fetch_pmids_by_author_id(author_id, max_results=100)
        print(f"    MyNCBI: found {count} total PMIDs, fetching top {min(len(pmids),15)}")
        if count > 2000:
            # Hashed/broken slug is matching a massive result set — skip and fall back
            print(f"    MyNCBI: result set too large ({count}), slug '{author_id}' is probably broken — falling back")
            pmids, count = [], 0
        if pmids:
            pubs = pubmed_fetch_summaries(pmids[:15], search_term=name)[:5]
            faculty_member["pubmed_search"] = f"{author_id}[Author Identifier]"
            faculty_member["pubmed_count"] = count
            faculty_member["pubmed_verified"] = len(pubs)
            faculty_member["pubmed_ambiguous"] = False
            faculty_member["publications"] = pubs
            time.sleep(0.4)
            return faculty_member
        # [Author Identifier] returned 0 — slug may be a hash (e.g. '1foVmTwbUHA5f')
        # or the author hasn't linked their papers. Fall back to name-based search.
        print(f"    MyNCBI Author ID search returned 0 — falling back to name search")
        slug_parts = [p for p in author_id.split(".") if p and not p.isdigit()]
        if len(slug_parts) >= 2:
            # Readable slug like 'brent.hanks.1' → derive 'Hanks B'
            slug_hint = f"{slug_parts[1].capitalize()} {slug_parts[0][0].upper()}"
            print(f"    Slug-derived fallback search: '{slug_hint}'")
            clean_hint = slug_hint
        else:
            clean_hint = None

    if clean_hint and clean_hint.startswith("ORCID:"):
        search_term = clean_hint
    elif clean_hint:
        if clean_hint != build_pubmed_search_string(name):
            print(f"    Using hint '{clean_hint}' for {name}")
        search_term = clean_hint
    else:
        search_term = build_pubmed_search_string(name)


    print(f"    PubMed: {name} → '{search_term}'")
    result = pubmed_search(search_term, max_results=15)

    # If full-name search returns nothing, try progressively broader fallbacks.
    # PubMed indexes authors as 'Lastname FI' or 'Lastname FirstI' — not full first name.
    if result["count"] == 0 and not search_term.startswith("ORCID:"):
        clean = clean_name_for_pubmed(name)
        parts = [p for p in clean.split() if p]
        if len(parts) >= 2:
            last = parts[-1]
            initials = "".join(p[0] for p in parts[:-1] if p and p[0].isalpha())
            fallback_term = f"{last} {initials}"
            if fallback_term != search_term:
                fallback_result = pubmed_search(fallback_term, max_results=15)
                if fallback_result["count"] > 0:
                    print(f"    Fallback: '{search_term}' → '{fallback_term}' ({fallback_result['count']} results)")
                    search_term = fallback_term
                    result = fallback_result

    # If STILL 0 results, the MyNCBI hint may be "Lastname Firstname" but PubMed
    # indexes the person under initials only (e.g. "Hanks Brent" → try "Hanks B")
    # Also covers cases where affiliation string is slightly off — retry with
    # the initials form derived from the hint itself
    if result["count"] == 0 and not search_term.startswith("ORCID:"):
        hint_parts = [p for p in search_term.split() if p]
        if len(hint_parts) == 2:
            hint_initial = hint_parts[1][0]  # first letter of first name in hint
            alt_term = f"{hint_parts[0]} {hint_initial}"
            if alt_term != search_term:
                alt_result = pubmed_search(alt_term, max_results=15)
                if alt_result["count"] > 0:
                    print(f"    Fallback (hint initial): '{search_term}' → '{alt_term}' ({alt_result['count']} results)")
                    search_term = alt_term
                    result = alt_result

    # Final fallback: drop the UNC affiliation filter entirely.
    # Catches recently recruited faculty whose papers list a prior institution.
    # Only trigger when search term ends with 1-2 letter initials — specific enough
    # to avoid flooding with false positives. Try two-initial form first (e.g. 'Hanks BA')
    # before dropping affiliation entirely.
    if result["count"] == 0 and not search_term.startswith("ORCID:"):
        term_parts = search_term.split()
        has_initials = len(term_parts) >= 2 and len(term_parts[-1]) <= 2 and term_parts[-1].isalpha()
        if has_initials:
            # If search term has only one initial (e.g. 'Hanks B'), try two initials
            # by checking if the raw name has a middle name/initial we can use
            if len(term_parts[-1]) == 1:
                clean = clean_name_for_pubmed(name)
                raw_parts = [p.rstrip(".") for p in clean.split() if p]
                raw_parts = [p for p in raw_parts if p and (len(p) > 1 or p.isalpha())]
                if len(raw_parts) >= 3:
                    # e.g. ['Brent', 'A', 'Hanks'] or ['Brent', 'Hanks'] - check for middle
                    first_parts = raw_parts[:-1]
                    if len(first_parts) >= 2:
                        two_initials = "".join(p[0].upper() for p in first_parts if p and p[0].isalpha())
                        if len(two_initials) >= 2:
                            two_init_term = f"{term_parts[0]} {two_initials}"
                            if two_init_term != search_term:
                                two_init_result = pubmed_search(two_init_term, max_results=15)
                                if two_init_result["count"] > 0:
                                    print(f"    Fallback (two initials): '{search_term}' → '{two_init_term}' ({two_init_result['count']} results)")
                                    search_term = two_init_term
                                    result = two_init_result

            # If still 0, drop affiliation (recent recruit at another institution)
            if result["count"] == 0:
                no_aff_result = pubmed_search(search_term, affiliation="", max_results=15)
                if no_aff_result["count"] > 0:
                    print(f"    No-affiliation fallback: '{search_term}' found {no_aff_result['count']} results (recent recruit?)")
                    result = no_aff_result
                    faculty_member["pubmed_recent_recruit"] = True  # flag for UI

    pubs = []
    recent_recruit = faculty_member.get("pubmed_recent_recruit", False)
    if result["ids"]:
        # For recent recruits, skip UNC affiliation verification
        candidates = pubmed_fetch_summaries(
            result["ids"][:15],
            search_term=search_term,
            verify_affiliation=not recent_recruit
        )
        pubs = candidates[:5]

        # ----------------------------------------------------------------
        # Computed Authors upgrade: once we have a UNC-verified seed PMID,
        # use NCBI's ML disambiguation API to get the full publication list.
        # IMPORTANT: only seed with a UNC-verified paper — if the seed is wrong,
        # Computed Authors will return an entirely wrong author cluster.
        # Skip for recent recruits (unverified seeds) and ORCID searches.
        # ----------------------------------------------------------------
        # Only seed Computed Authors from genuinely verified pubs.
        # recent_recruit pubs bypass UNC verification, so skip CA for them.
        # For normal searches, pubmed_fetch_summaries already filters to UNC-verified,
        # so any pub in pubs[] is confirmed.
        verified_pubs = pubs if not recent_recruit else []
        if verified_pubs and not search_term.startswith("ORCID:"):
            seed_pmid = verified_pubs[0]["pmid"]
            print(f"    Computed Authors: seeding with PMID {seed_pmid}")
            ca_pmids, ca_count = fetch_pmids_via_computed_authors(name, seed_pmid=seed_pmid)
            if ca_count > 0:
                print(f"    Computed Authors: found {ca_count} total PMIDs for this author")
                # Sanity check: reject obviously wrong clusters (>500 PMIDs suggests wrong person)
                if ca_count > 500:
                    print(f"    Computed Authors: cluster too large ({ca_count}), skipping")
                else:
                    ca_candidates = pubmed_fetch_summaries(
                        ca_pmids[:20],
                        search_term=search_term,
                        verify_affiliation=False  # CA already disambiguated — trust it
                    )
                    if ca_candidates:
                        pubs = ca_candidates[:5]
                        faculty_member["pubmed_count"] = ca_count
                        faculty_member["pubmed_search"] = f"ComputedAuthors:{search_term}"

    faculty_member["pubmed_search"] = faculty_member.get("pubmed_search") or search_term
    faculty_member["pubmed_count"] = faculty_member.get("pubmed_count") or result["count"]
    faculty_member["pubmed_verified"] = len(pubs)
    faculty_member["pubmed_ambiguous"] = (result["count"] > 0 and len(pubs) == 0)
    faculty_member["publications"] = pubs

    # Rate limiting — sleep respects NCBI API key rate limit
    time.sleep(PUBMED_SLEEP)
    return faculty_member


# ---------------------------------------------------------------------------
# NIH RePORTER enrichment (optional, best-effort)
# ---------------------------------------------------------------------------

def fetch_nih_grants(name):
    """Query NIH RePORTER for active grants by PI name at UNC."""
    clean = clean_name_for_pubmed(name)
    parts = clean.strip().split()
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

    # Load manual PubMed overrides (lowercased name → search string)
    overrides_path = config_path.replace("departments.json", "pubmed_overrides.json")
    pubmed_overrides = {}
    try:
        with open(overrides_path) as f:
            raw = json.load(f)
        pubmed_overrides = {k.lower(): v for k, v in raw.items() if not k.startswith("_")}
        if pubmed_overrides:
            print(f"Loaded {len(pubmed_overrides)} PubMed override(s): {list(pubmed_overrides.keys())}")
    except FileNotFoundError:
        pass

    departments = config["departments"]
    if dept_filter:
        departments = [d for d in departments if dept_filter.lower() in d["name"].lower()]

    all_faculty = []
    dept_index = {}  # dept_name -> list of faculty

    # ---- Step 1: Scrape faculty pages ----
    print("\n=== Step 1: Scraping faculty pages ===")
    for dept in departments:
        print(f"\n[{dept['name']}]")
        # Support both single "url" and multiple "urls" per department
        urls = dept.get("urls") or [dept.get("url")]
        urls = [u for u in urls if u]
        dept_faculty = []
        for url in urls:
            faculty_list = scrape_faculty_from_page(url, dept["name"])
            dept_faculty.extend(faculty_list)
            time.sleep(0.5)

            # Auto-paginate directory-style pages (e.g. unclineberger.org/directory/)
            # Try /page/2/, /page/3/, etc. until a page returns 0 new faculty
            if "/directory/" in url and not re.search(r"/page/\d+/", url):
                base_url = url.rstrip("/")
                page = 2
                while True:
                    paged_url = f"{base_url}/page/{page}/"
                    paged_faculty = scrape_faculty_from_page(paged_url, dept["name"])
                    if not paged_faculty:
                        break
                    dept_faculty.extend(paged_faculty)
                    page += 1
                    time.sleep(0.5)
                    if page > 20:  # safety cap
                        break

        dept_index[dept["name"]] = dept_faculty
        all_faculty.extend(dept_faculty)
        if len(urls) > 1:
            print(f"  Total from {len(urls)} pages: {len(dept_faculty)} faculty")
        time.sleep(0.5)

    print(f"\nTotal faculty scraped: {len(all_faculty)}")

    # ---- Deduplication: merge faculty with same name across departments ----
    # Faculty with joint appointments appear on multiple department pages.
    # Keep one entry per person, storing all departments as a list.
    print("\n=== Deduplicating faculty ===")
    seen = {}  # name_lower -> index in deduplicated list
    deduplicated = []
    for f in all_faculty:
        key = f["name"].lower().strip()
        if key in seen:
            # Merge: add this department to existing entry
            existing = deduplicated[seen[key]]
            existing_depts = existing.get("departments", [existing["department"]])
            if f["department"] not in existing_depts:
                existing_depts.append(f["department"])
                existing["departments"] = existing_depts
                existing["department"] = existing_depts[0]  # keep primary
            # Preserve pubmed_hint if the incoming entry has one and existing doesn't
            if f.get("pubmed_hint") and not existing.get("pubmed_hint"):
                existing["pubmed_hint"] = f["pubmed_hint"]
            print(f"  Merged duplicate: {f['name']} ({f['department']})")
        else:
            seen[key] = len(deduplicated)
            f["departments"] = [f["department"]]
            deduplicated.append(f)
    all_faculty = deduplicated
    print(f"  {len(all_faculty)} unique faculty after deduplication")

    # ---- Step 2: Scrape profile pages for curated PubMed strings ----
    print("\n=== Step 2: Checking profiles for PubMed search strings ===")
    trainees_removed = []
    for f in all_faculty:
        # Manual override takes priority over everything else
        name_key = f["name"].lower().strip()
        if name_key in pubmed_overrides:
            override = pubmed_overrides[name_key]
            print(f"  {f['name']}: OVERRIDE → '{override}'")
            # Prefix with OVERRIDE: so sanitize_hint passes it through unchanged
            f["pubmed_hint"] = f"OVERRIDE:{override}"
            continue
        profile_url = f.get("profile_url") or ""

        # If no profile URL was captured from the listing page, try to construct one.
        # Most UNC SOM departments use /people/{firstname-lastname}/ pattern.
        if not profile_url and f.get("department"):
            # Build slug from name: "Barbara Reid-Mills" → "barbara-reid-mills"
            slug = re.sub(r"[^a-z0-9]+", "-", f["name"].lower()).strip("-")
            # Find the base URL for this department from the config
            dept_conf = next((d for d in config["departments"] if d["name"] == f["department"]), None)
            if dept_conf:
                base = dept_conf.get("url") or (dept_conf.get("urls") or [""])[0]
                if base:
                    parsed_base = urllib.parse.urlparse(base)
                    dept_root = f"{parsed_base.scheme}://{parsed_base.netloc}{'/'.join(parsed_base.path.split('/')[:2])}/"
                    candidate = f"{dept_root.rstrip('/')}/people/{slug}/"
                    probe = fetch_url(candidate)
                    if probe:
                        profile_url = candidate
                        f["profile_url"] = profile_url
                        print(f"  {f['name']}: constructed profile URL → {candidate}")
                    time.sleep(0.2)

        if profile_url:
            # Fetch profile page once — reuse for trainee check, Lineberger filter, and PubMed hint
            profile_html = fetch_url(profile_url)
            time.sleep(0.3)

            # For Lineberger faculty, exclude non-clinical departments
            if "unclineberger.org" in profile_url:
                if not is_lineberger_clinical(profile_html):
                    print(f"  {f['name']} ({f['department']}): non-clinical Lineberger member — excluding")
                    f["_exclude"] = True
                    trainees_removed.append(f["name"])
                    continue

            # Check if this person is a trainee/admin before spending time on PubMed
            if is_trainee_profile(profile_html):
                print(f"  {f['name']} ({f['department']}): excluded (no faculty credentials)")
                f["_exclude"] = True
                trainees_removed.append(f["name"])
                continue

            ps = scrape_profile_for_pubmed_string(profile_url, html=profile_html)
            if ps:
                print(f"  {f['name']}: found '{ps}'")
                f["pubmed_hint"] = ps

    # Remove trainees flagged during profile check
    if trainees_removed:
        all_faculty = [f for f in all_faculty if not f.get("_exclude")]
        print(f"  Removed {len(trainees_removed)} trainee(s): {', '.join(trainees_removed)}")

    # ---- Step 3: Enrich with PubMed ----
    print("\n=== Step 3: Enriching with PubMed ===")
    for i, f in enumerate(all_faculty):
        print(f"  [{i+1}/{len(all_faculty)}] {f['name']}")
        enrich_faculty_with_pubmed(f, pubmed_string=f.get("pubmed_hint"))
        time.sleep(PUBMED_SLEEP)

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
