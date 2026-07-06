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
# Pipeline version — bump this whenever enrichment logic changes so that
# scheduled full runs re-enrich everyone instead of resuming stale results.
# The resume logic only skips faculty whose stored version matches this.
# ---------------------------------------------------------------------------
PIPELINE_VERSION = "2026.07.06-orcid-fallback-v2"

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
    import ssl
    headers = {
        "User-Agent": "UNC-Research-Explorer/1.0 (student research tool; contact: jgbresearch@unc.edu)",
        "Accept": "text/html,application/xhtml+xml",
    }
    req = urllib.request.Request(url, headers=headers)
    # Some UNC subdomains have certificate chain issues — use unverified context for .unc.edu
    ctx = ssl.create_default_context()
    if "unc.edu" in url or "unclineberger.org" in url:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
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
    import ssl
    headers = {
        "User-Agent": "UNC-Research-Explorer/1.0 (student research tool; contact: jgbresearch@unc.edu)",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    if "unc.edu" in url or "unclineberger.org" in url:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
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
    # Navigation / UI chrome
    r"\b(home|search|contact|login|admin|menu|navigation|toggle|skip|"
    r"back|next|previous|apply|submit|calendar|event|blog|video|photo|"
    r"gallery|map|career|job|giving|donate|privacy|accessibility|intranet|"
    r"follow|connect|links|local|notice|nondiscrimination|read\s+more|"
    r"more|learn\s+more|view\s+more|see\s+more|continue|details)\b|"

    # Generic page/section labels
    r"\b(faculty|directory|people|team|staff|about|news|education|"
    r"research|program|division|department|center|institute|"
    r"services|resources|information|overview|history|commitment|"
    r"positions|open|emeritus|adjunct|current|interdisciplinary|"
    r"perspectives|residents|fellows|providers|assistants)\b|"

    # Clinical/location junk
    r"\b(patient|appointment|building|clinic|hospital|location|floor|"
    r"suite|wing|campus|parking|hours|phone|fax|address|directions|"
    r"physicians|community|school|university|population|health|sciences|"
    r"scheduling|referrals|my chart|find a doctor|make a gift|"
    r"show your support|grand rounds|annual report|strategic plan)\b|"

    # Department names (appear as nav links on dept pages)
    r"\b(medicine|neurology|cardiology|surgery|pediatrics|oncology|"
    r"radiology|imaging|neuroradiology|interventional|musculoskeletal|"
    r"abdominal|breast|cardiac|nuclear|mammography|ultrasound|"
    r"anesthesiology|psychiatry|dermatology|ophthalmology|urology|"
    r"obstetric|regional|movement\s+disorders|multiple\s+sclerosis|"
    r"neurocritical|neuromuscular|memory|cognitive)\b|"

    # Radiology-specific nav junk
    r"\b(know before you go|referring physician|living in chapel hill|"
    r"striving for|that define us|faces that define)\b|"

    # Pediatrics-specific section headers
    r"\b(inflammatory bowel|endoscopy|colonoscopy|primary care|"
    r"adolescent care|complex.*diagnostic|development.*behavior|"
    r"child maltreatment|genetics.*metabolism|hematology.*sickle|"
    r"developmental therapeutics|thompson laboratory|vogt laboratory|"
    r"pandya|darville)\b|"

    # Section/nav headers that slipped through (seen in Peds/Neuro/OBGYN/Psych/Radiology)
    r"\b(specialty care|quality\s*&?\s*safety|making a referral|"
    r"making an appointment|impactful publications|social media|"
    r"aviso de practicas privadas|practicas privadas|patient care|"
    r"clinical trials|our providers|our team|meet the team|"
    r"support groups|refer a patient|request an appointment|"
    r"transitioning to adult|resident documents|"
    r"our locations|contact us|for patients|for providers)\b",

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
        # Strip credentials first, then check if it looks like a name
        # e.g. "Wendell G. Yarbrough, MD, MMHC, FACS" → "Wendell G. Yarbrough"
        cleaned_text = extract_name(text)
        if not looks_like_name(cleaned_text):
            continue
        # Accept links that look like profile pages on any UNC domain
        if href and ("people" in href or "directory" in href or
                     "faculty" in href or "profile" in href or
                     "unclineberger.org/directory/" in href):
            name = cleaned_text
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

    # If HTML scraping returned very few results, the page is likely
    # JavaScript-rendered. Try the WordPress REST API as a fallback.
    # Threshold of 5 (was 10) avoids firing on small-but-complete departments;
    # genuinely JS-rendered pages return 0-2 nav-junk items, well under 5.
    if len(faculty) < 5:
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
        scrape_faculty_from_page._wp_rest_cache[wp_key] = True
        if rest_faculty:
            # MERGE HTML + WP REST results, deduplicating by name.
            # Previously this discarded HTML results entirely — a bug that
            # silently dropped faculty when both sources had distinct people.
            existing_names = {f["name"].lower() for f in faculty}
            added = 0
            for rf in rest_faculty:
                if rf["name"].lower() not in existing_names:
                    faculty.append(rf)
                    existing_names.add(rf["name"].lower())
                    added += 1
            print(f"  Found {len(rest_faculty)} via WP REST API "
                  f"({added} new, {len(rest_faculty) - added} already in HTML results)")

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
        with urllib.request.urlopen(req, timeout=30) as r:
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
    Fetch PMIDs from a faculty member's MyNCBI public bibliography page.

    The MyNCBI URL slug (e.g. 'brent.hanks.1' or '1JMfwJ7FbPr') is used to
    build the bibliography URL directly:
      https://www.ncbi.nlm.nih.gov/myncbi/{slug}/bibliography/public/

    This is more reliable than the [Author Identifier] E-utilities field, which:
      - Returns 0 for newer hash-style IDs (e.g. '1JMfwJ7FbPr', '1BwOVEH1gPc')
      - Returns absurdly large counts for URL-encoded slugs with spaces

    The bibliography page embeds a JSON payload in the page HTML that we can
    parse directly for PMIDs.
    """
    bib_url = f"https://www.ncbi.nlm.nih.gov/myncbi/{author_id}/bibliography/public/"
    html = fetch_url(bib_url)
    if not html:
        return [], 0

    # The bibliography page embeds citation data as a JSON blob in the HTML.
    # Look for PMIDs in the page — they appear as data-pmid attributes or
    # in a JSON structure like "pmid":"12345678"
    pmids = []
    seen = set()

    # Method 1: data-pmid attributes (most reliable)
    for m in re.finditer(r'data-pmid=["\'](\d+)["\']', html):
        pid = m.group(1)
        if pid not in seen:
            seen.add(pid)
            pmids.append(pid)

    # Method 2: JSON-embedded pmid fields
    if not pmids:
        for m in re.finditer(r'"pmid"\s*:\s*"?(\d+)"?', html):
            pid = m.group(1)
            if pid not in seen:
                seen.add(pid)
                pmids.append(pid)

    # Method 3: citation links like /pubmed/12345678/
    if not pmids:
        for m in re.finditer(r'/pubmed/(\d{7,8})/', html):
            pid = m.group(1)
            if pid not in seen:
                seen.add(pid)
                pmids.append(pid)

    count = len(pmids)
    return pmids[:max_results], count


def search_orcid_by_name(name):
    """
    Search ORCID public API by name, return best-matching ORCID iD or None.
    Does NOT filter by affiliation since UNC faculty rarely list it on ORCID.
    Returns (orcid_id, given_name, family_name) or (None, None, None).
    """
    parts = name.strip().split()
    if len(parts) < 2:
        return None, None, None
    first = parts[0]
    last = parts[-1]

    # Normalize accents to ASCII and URL-encode — ORCID's API rejects raw
    # non-ASCII in the query string (causes 'ascii codec' errors), and accented
    # names are indexed inconsistently anyway.
    import unicodedata
    def _ascii(s):
        s = unicodedata.normalize("NFKD", s)
        return "".join(c for c in s if not unicodedata.combining(c))
    first_a = _ascii(first)
    last_a = _ascii(last)
    # Skip junk names (HTML artifacts like '–>', single chars, non-alpha)
    if not re.search(r"[A-Za-z]{2,}", last_a) or not re.search(r"[A-Za-z]{2,}", first_a):
        return None, None, None

    query = urllib.parse.quote(f"given-names:{first_a} AND family-name:{last_a}")
    url = f"https://pub.orcid.org/v3.0/expanded-search/?q={query}&rows=5"
    data = fetch_json(url)
    if not data:
        return None, None, None
    try:
        obj = json.loads(data)
    except Exception:
        return None, None, None
    if not isinstance(obj, dict):
        return None, None, None

    results = obj.get("expanded-result") or []
    for result in results:
        given = (result.get("given-names") or "").strip()
        family = (result.get("family-names") or "").strip()
        orcid = result.get("orcid-id")
        # Match against ASCII-normalized forms on both sides
        if _ascii(family).lower() != last_a.lower():
            continue
        gl, fl = _ascii(given).lower(), first_a.lower()
        exact = gl == fl
        prefix_ok = (len(fl) >= 3 and len(gl) >= 3 and
                     (gl.startswith(fl) or fl.startswith(gl)))
        if exact or prefix_ok:
            return orcid, given, family

    return None, None, None


def fetch_pmids_via_orcid(orcid_id, since_year=2018):
    """
    Fetch PMIDs from ORCID works API for a given ORCID iD.
    Also returns DOIs for works that don't have PMIDs, so we can
    look them up in PubMed via doi[AID].
    Returns (pmids, dois) — both lists sorted recent-first.
    """
    url = f"https://pub.orcid.org/v3.0/{orcid_id}/works"
    data = fetch_json(url)
    if not data:
        return [], []
    try:
        obj = json.loads(data)
    except Exception:
        return [], []
    if not isinstance(obj, dict):
        return [], []

    pmids = []
    dois = []
    seen_pmids = set()
    seen_dois = set()

    for group in (obj.get("group") or []):
        for summary in (group.get("work-summary") or []):
            if not isinstance(summary, dict):
                continue
            pub_date = summary.get("publication-date") or {}
            year_obj = pub_date.get("year") or {}
            year_val = year_obj.get("value") if isinstance(year_obj, dict) else None
            try:
                year = int(year_val)
            except (TypeError, ValueError):
                year = 0

            if year and year < since_year:
                continue

            ext = summary.get("external-ids") or {}
            eids = ext.get("external-id") or [] if isinstance(ext, dict) else []
            pmid = None
            doi = None
            for eid in eids:
                if not isinstance(eid, dict):
                    continue
                t = eid.get("external-id-type", "")
                v = (eid.get("external-id-value") or "").strip()
                if t == "pmid" and v and v not in seen_pmids:
                    pmid = v
                elif t == "doi" and v and v not in seen_dois:
                    doi = v.lower()

            if pmid:
                seen_pmids.add(pmid)
                pmids.append(pmid)
            elif doi:
                seen_dois.add(doi)
                dois.append(doi)

    return pmids, dois


def resolve_dois_to_pmids(dois, max_dois=10):
    """
    Convert a list of DOIs to PMIDs via PubMed's doi[AID] field.
    Returns list of PMIDs found.
    """
    pmids = []
    for doi in dois[:max_dois]:
        api_key_param = f"&api_key={NCBI_API_KEY}" if NCBI_API_KEY else ""
        term = urllib.parse.quote(f'"{doi}"[AID]')
        url = (
            f"{PUBMED_BASE}esearch.fcgi"
            f"?db=pubmed&term={term}&retmax=1&retmode=json"
            f"&email={PUBMED_EMAIL}{api_key_param}"
        )
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            ids = data["esearchresult"]["idlist"]
            if ids:
                pmids.append(ids[0])
        except Exception:
            pass
        time.sleep(PUBMED_SLEEP)
    return pmids


def clean_name_for_pubmed(name):
    """
    Strip credentials, nicknames, punctuation from a raw scraped name
    before building a PubMed search string. Also normalizes accented
    characters to ASCII, since PubMed indexes authors in ASCII
    (e.g. 'Aubé' → 'Aube', 'Giscombé' → 'Giscombe').
    E.g. 'Adeyemi "Yemi" Ogunleye' -> 'Adeyemi Ogunleye'
         'Delora Mount, FAAP'       -> 'Delora Mount'
         'Jeff Aubé'                -> 'Jeff Aube'
    """
    import unicodedata
    # Normalize accented chars to ASCII (NFKD splits base char + combining mark,
    # then we drop the combining marks)
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
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


def _surname_tokens(name_or_term):
    """
    Extract candidate surname forms from a name or search term.
    Handles multi-word surnames with particles (de Silva, van Duin, von Hippel).
    Returns a set of lowercase candidates to match against a PubMed <LastName>.

    'Hanks BA'              -> {'hanks'}
    'de Silva AM'           -> {'silva', 'de silva'}
    'van Duin D'            -> {'duin', 'van duin'}
    'Brent A. Hanks' (name) -> {'hanks'}   (last token, initials ignored)
    """
    if not name_or_term:
        return set()
    PARTICLES = {"de", "van", "von", "del", "der", "di", "da", "la", "le", "den", "ter"}
    tokens = [t.strip(".,").lower() for t in name_or_term.split() if t.strip(".,")]
    # Drop trailing initials blocks (1-2 char all-alpha tokens at the end)
    while len(tokens) > 1 and len(tokens[-1]) <= 2 and tokens[-1].isalpha():
        tokens.pop()
    if not tokens:
        return set()
    candidates = {tokens[-1]}
    # If the token before the last is a particle, include the compound surname
    if len(tokens) >= 2 and tokens[-2] in PARTICLES:
        candidates.add(f"{tokens[-2]} {tokens[-1]}")
        # Also accept the particle stripped (PubMed sometimes drops particles)
        candidates.add(tokens[-1])
    return candidates


def pubmed_fetch_summaries(pmids, verify_affiliation=True, search_term="", target_lastname=None):
    """
    Fetch article details for a list of PMIDs.
    Uses efetch (XML) to get affiliation data, then filters to only
    papers where the TARGET author is affiliated with UNC.

    target_lastname: the faculty member's actual surname, used for per-author
    affiliation matching. If not given, it's derived from search_term — but
    callers should pass it explicitly, because search_term is "Lastname Initials"
    for name searches but the full name for MyNCBI/ORCID paths, which would
    otherwise yield the wrong token (e.g. first name).
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
            with urllib.request.urlopen(url, timeout=30) as resp:
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

            # Determine the surname(s) to match. Prefer an explicit target_lastname;
            # otherwise derive from the search term (handles multi-word surnames).
            surname_candidates = _surname_tokens(target_lastname or search_term)
            # The "primary" surname for ambiguity checks is the bare last token
            # (the single-word surname). For 'de silva' candidates {'silva','de silva'}
            # the ambiguity check should use 'silva' — the form PubMed most often
            # stores in <LastName> and the form that appears in AMBIGUOUS_LASTNAMES.
            primary_last = (min(surname_candidates, key=len)
                            if surname_candidates else "")
            target_has_unc = False
            any_has_unc = affiliation_is_unc(all_aff_text)

            # Track whether ANY author block has per-author affiliations stored
            any_block_has_affs = False
            for block in author_blocks:
                last_match = re.search(r"<LastName>(.*?)</LastName>", block)
                block_affs = re.findall(r"<Affiliation>(.*?)</Affiliation>", block)
                block_aff_text = " ".join(block_affs)

                if block_affs:
                    any_block_has_affs = True

                if last_match:
                    last = last_match.group(1).strip().lower()
                    if last in surname_candidates:
                        # This is our target author — check their affiliation
                        if block_affs and affiliation_is_unc(block_aff_text):
                            target_has_unc = True

            # If the paper has NO per-author affiliations at all (older papers),
            # fall back to article-level — but only if the target last name is
            # rare enough to be unambiguous (not smith, lee, kim, wang, etc.)
            if not any_block_has_affs and any_has_unc:
                if primary_last not in AMBIGUOUS_LASTNAMES:
                    target_has_unc = True
                else:
                    print(f"      PMID {pmid} — UNC affiliation found but last name '{primary_last}' too common to verify per-author")

            if not any_has_unc:
                print(f"      Skipping PMID {pmid} — no UNC affiliation in article")
                continue
            # If we have per-author data and target isn't at UNC, skip
            if not target_has_unc and any_has_unc:
                print(f"      PMID {pmid} — UNC affiliation found but not for target author ({primary_last})")
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


def _pubmed_name_search_with_fallbacks(name, initial_term):
    """
    Try progressively broader PubMed name searches until we get results.
    Returns (search_term, result_dict, is_recent_recruit).

    Fallback order:
      1. Initial term (from hint or build_pubmed_search_string)
      2. All-initials form (e.g. 'Hanks B' → 'Hanks BA' if middle name known)
      3. Single-initial fallback (e.g. 'Hanks Brent' → 'Hanks B')
      4. No-affiliation search (catches recent recruits at prior institutions)
    """
    result = pubmed_search(initial_term, max_results=15)
    if result["count"] > 0:
        return initial_term, result, False

    # Fallback 1: try all-initials form from the full name
    clean = clean_name_for_pubmed(name)
    parts = [p for p in clean.split() if p]
    if len(parts) >= 2:
        last = parts[-1]
        initials = "".join(p[0] for p in parts[:-1] if p and p[0].isalpha())
        fallback = f"{last} {initials}"
        if fallback != initial_term:
            r = pubmed_search(fallback, max_results=15)
            if r["count"] > 0:
                print(f"    Fallback: '{initial_term}' → '{fallback}' ({r['count']} results)")
                return fallback, r, False

    # Fallback 2: single-initial form of the hint
    hint_parts = [p for p in initial_term.split() if p]
    if len(hint_parts) == 2 and len(hint_parts[1]) > 1:
        alt = f"{hint_parts[0]} {hint_parts[1][0]}"
        if alt != initial_term:
            r = pubmed_search(alt, max_results=15)
            if r["count"] > 0:
                print(f"    Fallback (hint initial): '{initial_term}' → '{alt}' ({r['count']} results)")
                return alt, r, False

    # Fallback 3: two-initial form if we only have one initial
    term_parts = initial_term.split()
    if len(term_parts) >= 2 and len(term_parts[-1]) == 1 and term_parts[-1].isalpha():
        clean_parts = [p.rstrip(".") for p in clean_name_for_pubmed(name).split() if p]
        clean_parts = [p for p in clean_parts if p and (len(p) > 1 or p.isalpha())]
        if len(clean_parts) >= 3:
            two_initials = "".join(p[0].upper() for p in clean_parts[:-1] if p and p[0].isalpha())
            if len(two_initials) >= 2:
                two_init = f"{term_parts[0]} {two_initials}"
                if two_init != initial_term:
                    r = pubmed_search(two_init, max_results=15)
                    if r["count"] > 0:
                        print(f"    Fallback (two initials): '{initial_term}' → '{two_init}' ({r['count']} results)")
                        return two_init, r, False

    # Fallback 4: drop affiliation filter (recent recruit)
    term_parts = initial_term.split()
    has_initials = len(term_parts) >= 2 and len(term_parts[-1]) <= 2 and term_parts[-1].isalpha()
    if has_initials:
        r = pubmed_search(initial_term, affiliation="", max_results=15)
        if r["count"] > 0:
            print(f"    No-affiliation fallback: '{initial_term}' found {r['count']} results (recent recruit?)")
            return initial_term, r, True

    return initial_term, {"count": 0, "ids": []}, False


def _upgrade_with_computed_authors(faculty_member, pubs, search_term, target_lastname=None):
    """
    Given a verified seed publication, use NCBI's Computed Authors API
    to get the full disambiguated publication list for this author.
    Updates faculty_member in place and returns updated pubs list.
    """
    if not pubs or search_term.startswith("ORCID:"):
        return pubs

    # Require at least 2 verified papers before trusting Computed Authors.
    # A single matching paper is too weak a seed — for common names like
    # "Miller B" one coincidental UNC match can seed an entirely wrong
    # author cluster. Two independent verified papers is much safer.
    if len(pubs) < 2:
        # Exception: if the search term is specific (2+ initials or full surname
        # with given name), one paper is acceptable since ambiguity is lower.
        term_parts = search_term.split()
        is_specific = (len(term_parts) >= 2 and
                       (len(term_parts[-1]) >= 2 or len(term_parts) >= 3))
        if not is_specific:
            print(f"    Computed Authors: only {len(pubs)} verified paper(s) "
                  f"with ambiguous term '{search_term}' — skipping to avoid wrong cluster")
            return pubs

    seed_pmid = pubs[0]["pmid"]
    print(f"    Computed Authors: seeding with PMID {seed_pmid}")
    ca_pmids, ca_count = fetch_pmids_via_computed_authors(faculty_member["name"], seed_pmid=seed_pmid)

    if ca_count == 0:
        return pubs
    if ca_count > 500:
        print(f"    Computed Authors: cluster too large ({ca_count}), skipping")
        return pubs

    print(f"    Computed Authors: found {ca_count} total PMIDs for this author")
    # Verify CA results still have UNC affiliation for the target author.
    # CA disambiguates by authorship but doesn't guarantee UNC — without this
    # check, a correct-but-non-UNC cluster (someone who left UNC) gets attached.
    ca_pubs = pubmed_fetch_summaries(ca_pmids[:20], search_term=search_term,
                                     verify_affiliation=True, target_lastname=target_lastname)
    if len(ca_pubs) >= 2:
        faculty_member["pubmed_count"] = ca_count
        faculty_member["pubmed_search"] = f"ComputedAuthors:{search_term}"
        return ca_pubs[:5]
    # CA cluster didn't verify against UNC — keep the original verified pubs
    print(f"    Computed Authors: cluster failed UNC re-verification ({len(ca_pubs)} confirmed) — keeping original")
    return pubs


def enrich_faculty_with_pubmed(faculty_member, pubmed_string=None):
    """
    Attach PubMed publication data to a faculty dict.

    Lookup priority:
      1. Manual override (OVERRIDE: prefix) — skip all logic
      2. MyNCBI bibliography page — most accurate, faculty-curated
      3. ORCID lookup — identity-verified, then DOI→PMID conversion
      4. Profile hint (name string from profile page)
      5. Name-based PubMed search with affiliation verification + fallbacks
      6. Computed Authors upgrade — once we have a verified seed PMID,
         use NCBI's ML disambiguation to get the full publication list
    """
    name = faculty_member["name"]
    raw_hint = pubmed_string or ""

    # Stamp the pipeline version so version-aware resume knows this record
    # was enriched by the current code, not a stale prior run.
    faculty_member["pipeline_version"] = PIPELINE_VERSION

    # ── 1. Manual override ────────────────────────────────────────────────────
    if raw_hint.startswith("OVERRIDE:"):
        search_term = raw_hint.replace("OVERRIDE:", "").strip()
        if search_term == "SKIP":
            print(f"    Skipping {name} per manual override")
            faculty_member.update({
                "pubmed_search": "SKIP", "pubmed_count": 0,
                "pubmed_verified": 0, "pubmed_ambiguous": False, "publications": []
            })
            return faculty_member
        print(f"    Using manual override: '{search_term}'")
        result = pubmed_search(search_term, max_results=15)
        print(f"    PubMed: {name} → '{search_term}' ({result['count']} results)")
        pubs = []
        if result["ids"]:
            pubs = pubmed_fetch_summaries(result["ids"][:15], search_term=search_term,
                                          verify_affiliation=False)[:5]
            pubs = _upgrade_with_computed_authors(faculty_member, pubs, search_term)
        faculty_member.update({
            "pubmed_search": faculty_member.get("pubmed_search") or search_term,
            "pubmed_count": faculty_member.get("pubmed_count") or result["count"],
            "pubmed_verified": len(pubs), "pubmed_ambiguous": False, "publications": pubs
        })
        time.sleep(PUBMED_SLEEP)
        return faculty_member

    clean_hint = sanitize_hint(pubmed_string, name)

    # ── 2. MyNCBI bibliography ────────────────────────────────────────────────
    if clean_hint and clean_hint.startswith("MYNCBI:"):
        bib_url = clean_hint.replace("MYNCBI:", "")
        author_id = bib_url.rstrip("/").split("/myncbi/")[-1].split("/")[0]
        print(f"    MyNCBI: fetching bibliography for '{author_id}'")
        pmids, count = fetch_pmids_by_author_id(author_id, max_results=100)
        print(f"    MyNCBI: found {count} PMIDs on bibliography page")
        if pmids:
            # The MyNCBI bibliography is the faculty member's own curated list,
            # so we trust it and skip UNC affiliation re-verification (which would
            # otherwise drop papers whose per-author affiliation lists a prior or
            # collaborating institution).
            pubs = pubmed_fetch_summaries(pmids[:15], search_term=name,
                                          verify_affiliation=False)[:5]
            faculty_member.update({
                "pubmed_search": f"MyNCBI:{author_id}", "pubmed_count": count,
                "pubmed_verified": len(pubs), "pubmed_ambiguous": False, "publications": pubs
            })
            time.sleep(0.4)
            return faculty_member
        # Bibliography empty — derive search hint from slug if possible
        print(f"    MyNCBI bibliography empty — falling back to name search")
        slug_parts = [p for p in author_id.split(".") if p and not p.isdigit()]
        if len(slug_parts) >= 2:
            clean_hint = f"{slug_parts[1].capitalize()} {slug_parts[0][0].upper()}"
            print(f"    Slug-derived fallback search: '{clean_hint}'")
        else:
            clean_hint = None

    # ── 3. Name-based PubMed search (primary path for most faculty) ──────────
    # Note: ORCID lookup was moved to a fallback (step 5) because calling it for
    # every faculty member added ~2 API calls + 0.6s sleep each, making full
    # runs time out. Now we try the cheaper name search first and only reach for
    # ORCID when the name search yields nothing or looks ambiguous.
    if clean_hint and clean_hint.startswith("ORCID:"):
        initial_term = clean_hint
    elif clean_hint:
        canonical = build_pubmed_search_string(name)
        if clean_hint != canonical:
            print(f"    Using hint '{clean_hint}' for {name}")
        initial_term = clean_hint
    else:
        initial_term = build_pubmed_search_string(name)

    print(f"    PubMed: {name} → '{initial_term}'")
    search_term, result, recent_recruit = _pubmed_name_search_with_fallbacks(name, initial_term)
    if recent_recruit:
        faculty_member["pubmed_recent_recruit"] = True

    target_lastname = clean_name_for_pubmed(name)

    pubs = []
    if result["ids"]:
        pubs = pubmed_fetch_summaries(
            result["ids"][:15],
            search_term=search_term,
            verify_affiliation=not recent_recruit,
            target_lastname=target_lastname,
        )[:5]
        if not recent_recruit:
            pubs = _upgrade_with_computed_authors(faculty_member, pubs, search_term, target_lastname)

    # ── 4. ORCID fallback — only when name search was weak ────────────────────
    # Trigger ORCID only if: (a) name search found nothing verified, or
    # (b) we got very few results on a common surname (likely incomplete).
    # This keeps ORCID off the critical path for the ~70% of faculty whose
    # name search already works, while still rescuing the hard cases.
    name_search_weak = (
        len(pubs) == 0 or
        (len(pubs) < 2 and target_lastname.split()[-1].lower() in AMBIGUOUS_LASTNAMES)
    )
    already_have_id_source = faculty_member.get("pubmed_search", "").startswith(
        ("MyNCBI", "ORCID", "OVERRIDE", "ComputedAuthors"))

    if name_search_weak and not already_have_id_source:
        orcid_id, orcid_given, orcid_family = search_orcid_by_name(name)
        time.sleep(0.2)
        if orcid_id:
            print(f"    ORCID fallback: found {orcid_id} for {orcid_given} {orcid_family}")
            orcid_pmids, orcid_dois = fetch_pmids_via_orcid(orcid_id, since_year=2018)
            time.sleep(0.2)
            if not orcid_pmids and orcid_dois:
                print(f"    ORCID: resolving {min(len(orcid_dois), 3)} DOIs...")
                orcid_pmids = resolve_dois_to_pmids(orcid_dois, max_dois=3)
            if orcid_pmids:
                verify = len(orcid_pmids) <= 2
                orcid_pubs = pubmed_fetch_summaries(orcid_pmids[:15], search_term=name,
                                                    verify_affiliation=verify,
                                                    target_lastname=target_lastname)[:5]
                # Only replace name-search results if ORCID found MORE papers
                if len(orcid_pubs) > len(pubs):
                    print(f"    ORCID: {len(orcid_pubs)} papers (better than name search's {len(pubs)})")
                    faculty_member.update({
                        "pubmed_search": f"ORCID:{orcid_id}",
                        "pubmed_count": len(orcid_pmids),
                        "pubmed_verified": len(orcid_pubs),
                        "pubmed_ambiguous": False,
                        "publications": orcid_pubs,
                    })
                    time.sleep(PUBMED_SLEEP)
                    return faculty_member

    # Flag low-confidence matches: single-initial search on a common surname
    term_parts = search_term.replace("ComputedAuthors:", "").split()
    primary_last = target_lastname.split()[-1].lower() if target_lastname else ""
    single_initial = len(term_parts) >= 2 and len(term_parts[-1]) == 1
    low_confidence = (single_initial and primary_last in AMBIGUOUS_LASTNAMES
                      and len(pubs) < 3 and not already_have_id_source)

    faculty_member.update({
        "pubmed_search": faculty_member.get("pubmed_search") or search_term,
        "pubmed_count": faculty_member.get("pubmed_count") or result["count"],
        "pubmed_verified": len(pubs),
        "pubmed_ambiguous": (result["count"] > 0 and len(pubs) == 0) or low_confidence,
        "publications": pubs,
    })
    if low_confidence:
        print(f"    ⚠ Low-confidence match for {name} (common surname + single initial)")
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

def dept_matches(filter_str, dept_name, dept_short=""):
    """
    Match a department filter string against a department name or short name.
    Uses word-boundary matching to avoid 'ENT' matching 'Gastroenterology'.
    Also checks the short name so 'ENT' matches 'Otolaryngology / Head & Neck Surgery'
    and 'PM&R' matches 'Physical Medicine & Rehabilitation'.
    """
    if not filter_str:
        return True
    pattern = r'\b' + re.escape(filter_str.strip()) + r'\b'
    return (bool(re.search(pattern, dept_name, re.IGNORECASE)) or
            bool(re.search(pattern, dept_short, re.IGNORECASE)))


def scrape_only(config_path="scraper/departments.json",
                raw_output_path="data/faculty_raw.json",
                dept_filter=None):
    """
    Steps 1 + 2: Scrape department pages and profile pages.
    Writes faculty_raw.json with names, profile URLs, departments, pubmed_hints.
    Does NOT do PubMed or NIH enrichment.
    """
    with open(config_path) as f:
        config = json.load(f)

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
        departments = [d for d in departments if dept_matches(dept_filter, d["name"], d.get("short", ""))]

    all_faculty = []

    # ---- Step 1: Scrape faculty pages ----
    print("\n=== Step 1: Scraping faculty pages ===")
    for dept in departments:
        print(f"\n[{dept['name']}]")
        urls = dept.get("urls") or [dept.get("url")]
        urls = [u for u in urls if u]
        dept_faculty = []
        for url in urls:
            faculty_list = scrape_faculty_from_page(url, dept["name"])
            dept_faculty.extend(faculty_list)
            time.sleep(0.5)

            # Auto-paginate directory-style pages
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
                    if page > 20:
                        break

        all_faculty.extend(dept_faculty)
        if len(urls) > 1:
            print(f"  Total from {len(urls)} pages: {len(dept_faculty)} faculty")
        time.sleep(0.5)

    print(f"\nTotal faculty scraped: {len(all_faculty)}")

    # ---- Deduplication ----
    print("\n=== Deduplicating faculty ===")
    seen = {}
    deduplicated = []
    for f in all_faculty:
        key = f["name"].lower().strip()
        if key in seen:
            existing = deduplicated[seen[key]]
            existing_depts = existing.get("departments", [existing["department"]])
            if f["department"] not in existing_depts:
                existing_depts.append(f["department"])
                existing["departments"] = existing_depts
                existing["department"] = existing_depts[0]
            if f.get("pubmed_hint") and not existing.get("pubmed_hint"):
                existing["pubmed_hint"] = f["pubmed_hint"]
            print(f"  Merged duplicate: {f['name']} ({f['department']})")
        else:
            seen[key] = len(deduplicated)
            f["departments"] = [f["department"]]
            deduplicated.append(f)
    all_faculty = deduplicated
    print(f"  {len(all_faculty)} unique faculty after deduplication")

    # ---- Step 2: Profile page visits ----
    print("\n=== Step 2: Checking profiles for PubMed search strings ===")
    trainees_removed = []
    for f in all_faculty:
        name_key = f["name"].lower().strip()
        if name_key in pubmed_overrides:
            override = pubmed_overrides[name_key]
            print(f"  {f['name']}: OVERRIDE → '{override}'")
            f["pubmed_hint"] = f"OVERRIDE:{override}"
            continue

        profile_url = f.get("profile_url") or ""

        if not profile_url and f.get("department"):
            dept_conf = next((d for d in config["departments"] if d["name"] == f["department"]), None)
            if dept_conf and dept_conf.get("profile_base"):
                slug = re.sub(r"[^a-z0-9]+", "-", f["name"].lower()).strip("-")
                candidate = f"{dept_conf['profile_base'].rstrip('/')}/{slug}/"
                probe = fetch_url(candidate, retries=1, delay=0.5)
                if probe:
                    profile_url = candidate
                    f["profile_url"] = profile_url
                    print(f"  {f['name']}: constructed profile URL → {candidate}")
                time.sleep(0.2)

        if profile_url:
            profile_html = fetch_url(profile_url, retries=2, delay=1.0)
            time.sleep(0.3)

            if "unclineberger.org" in profile_url:
                if not is_lineberger_clinical(profile_html):
                    print(f"  {f['name']} ({f['department']}): non-clinical Lineberger member — excluding")
                    f["_exclude"] = True
                    trainees_removed.append(f["name"])
                    continue

            if is_trainee_profile(profile_html):
                print(f"  {f['name']} ({f['department']}): excluded (no faculty credentials)")
                f["_exclude"] = True
                trainees_removed.append(f["name"])
                continue

            ps = scrape_profile_for_pubmed_string(profile_url, html=profile_html)
            if ps:
                print(f"  {f['name']}: found '{ps}'")
                f["pubmed_hint"] = ps

    if trainees_removed:
        all_faculty = [f for f in all_faculty if not f.get("_exclude")]
        print(f"  Removed {len(trainees_removed)} excluded: {', '.join(trainees_removed)}")

    # Write raw output
    raw_output = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total_faculty": len(all_faculty),
        "departments": [d["name"] for d in departments],
        "faculty": all_faculty,
    }
    os.makedirs(os.path.dirname(raw_output_path) or ".", exist_ok=True)
    with open(raw_output_path, "w") as f:
        json.dump(raw_output, f, indent=2)
    print(f"\n✓ Written to {raw_output_path}")
    print(f"  {len(all_faculty)} faculty across {len(departments)} departments")
    return all_faculty, departments


def enrich_only(raw_input_path="data/faculty_raw.json",
                output_path="data/faculty.json",
                config_path="scraper/departments.json",
                skip_nih=False,
                dept_filter=None):
    """
    Steps 3 + 4: PubMed and NIH enrichment.
    Reads faculty_raw.json, writes faculty.json.
    Can be re-run independently without re-scraping.
    """
    with open(raw_input_path) as f:
        raw = json.load(f)

    all_faculty = raw["faculty"]
    departments_in_raw = raw.get("departments", [])

    # Optionally filter to a single department
    if dept_filter:
        all_faculty = [f for f in all_faculty
                       if any(dept_matches(dept_filter, d) for d in f.get("departments", [f.get("department", "")]))]
        departments_in_raw = [d for d in departments_in_raw if dept_matches(dept_filter, d)]

    # ---- Resume support (version-aware) ----
    # If a prior run wrote results to output_path, we can skip faculty already
    # enriched — but ONLY if they were enriched by the CURRENT pipeline version.
    # This prevents the stale-data bug: a scheduled full run after a code change
    # must re-enrich everyone, while a timed-out run resuming minutes later
    # (same version) correctly skips completed work.
    already_done = {}
    stale_skipped = 0
    if os.path.exists(output_path):
        try:
            with open(output_path) as f:
                prev = json.load(f)
            for pf in prev.get("faculty", []):
                if "pubmed_verified" not in pf and "publications" not in pf:
                    continue
                if pf.get("pipeline_version") == PIPELINE_VERSION:
                    already_done[pf["name"].lower().strip()] = pf
                else:
                    stale_skipped += 1
            if already_done:
                print(f"Resume: {len(already_done)} faculty already enriched "
                      f"by current pipeline v{PIPELINE_VERSION} — will skip those")
            if stale_skipped:
                print(f"Resume: {stale_skipped} faculty have OLD pipeline version "
                      f"— will RE-ENRICH with current logic")
        except Exception as e:
            print(f"Resume: could not read prior output ({e}) — starting fresh")

    # ---- Step 3: PubMed enrichment ----
    print("\n=== Step 3: Enriching with PubMed ===")
    for i, f in enumerate(all_faculty):
        name_key = f["name"].lower().strip()
        # Skip only if enriched by the CURRENT pipeline version
        if name_key in already_done:
            prev = already_done[name_key]
            for k in ("pubmed_search", "pubmed_count", "pubmed_verified",
                      "pubmed_ambiguous", "publications", "nih_grants",
                      "pubmed_recent_recruit", "pipeline_version"):
                if k in prev:
                    f[k] = prev[k]
            continue

        print(f"  [{i+1}/{len(all_faculty)}] {f['name']}")
        try:
            enrich_faculty_with_pubmed(f, pubmed_string=f.get("pubmed_hint"))
        except Exception as e:
            # One bad record must never kill the whole run — log and continue.
            print(f"    ⚠ ERROR enriching {f['name']}: {type(e).__name__}: {e}")
            f.setdefault("pubmed_search", "ERROR")
            f.setdefault("pubmed_count", 0)
            f.setdefault("pubmed_verified", 0)
            f.setdefault("pubmed_ambiguous", False)
            f.setdefault("publications", [])
            f["pipeline_version"] = PIPELINE_VERSION
        time.sleep(PUBMED_SLEEP)

        # Periodic checkpoint: save progress every 100 faculty so a late
        # crash or timeout doesn't lose everything.
        if (i + 1) % 100 == 0:
            checkpoint = {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "total_faculty": len(all_faculty),
                "departments": departments_in_raw,
                "faculty": all_faculty,
                "_checkpoint": f"{i+1}/{len(all_faculty)}",
            }
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            with open(output_path, "w") as cf:
                json.dump(checkpoint, cf, indent=2)
            print(f"    💾 checkpoint saved at {i+1}/{len(all_faculty)}")

    # ---- Step 4: NIH RePORTER ----
    if not skip_nih:
        print("\n=== Step 4: Checking NIH RePORTER ===")
        for f in all_faculty:
            try:
                grants = fetch_nih_grants(f["name"])
            except Exception as e:
                print(f"    ⚠ ERROR fetching NIH grants for {f['name']}: {e}")
                grants = []
            f["nih_grants"] = grants
            if grants:
                print(f"  {f['name']}: {len(grants)} grant(s)")
            time.sleep(0.3)
    else:
        for f in all_faculty:
            f["nih_grants"] = []

    # Write final output
    output = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total_faculty": len(all_faculty),
        "departments": departments_in_raw,
        "faculty": all_faculty,
    }
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n✓ Written to {output_path}")
    print(f"  {len(all_faculty)} faculty across {len(departments_in_raw)} departments")


def run(config_path="scraper/departments.json", output_path="data/faculty.json",
        pubmed_delay=0.4, skip_nih=False, dept_filter=None):
    """Combined pipeline: scrape + enrich in one shot."""
    raw_path = output_path.replace("faculty.json", "faculty_raw.json")
    scrape_only(config_path=config_path, raw_output_path=raw_path, dept_filter=dept_filter)
    enrich_only(raw_input_path=raw_path, output_path=output_path,
                config_path=config_path, skip_nih=skip_nih, dept_filter=dept_filter)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="UNC SOM faculty scraper")
    parser.add_argument("--config", default="scraper/departments.json")
    parser.add_argument("--output", default="data/faculty.json")
    parser.add_argument("--raw-output", default="data/faculty_raw.json")
    parser.add_argument("--skip-nih", action="store_true")
    parser.add_argument("--dept", help="Only scrape departments matching this string")
    parser.add_argument("--pubmed-delay", type=float, default=0.4)
    parser.add_argument("--scrape-only", action="store_true", help="Only run Steps 1+2")
    parser.add_argument("--enrich-only", action="store_true", help="Only run Steps 3+4")
    args = parser.parse_args()

    if args.scrape_only:
        scrape_only(config_path=args.config, raw_output_path=args.raw_output, dept_filter=args.dept)
    elif args.enrich_only:
        enrich_only(raw_input_path=args.raw_output, output_path=args.output,
                    config_path=args.config, skip_nih=args.skip_nih, dept_filter=args.dept)
    else:
        run(config_path=args.config, output_path=args.output,
            skip_nih=args.skip_nih, dept_filter=args.dept, pubmed_delay=args.pubmed_delay)
