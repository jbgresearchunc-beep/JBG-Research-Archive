#!/usr/bin/env python3
"""
UNC SOM Research Explorer — faculty scraper
Scrapes faculty names from department pages, enriches with PubMed data,
and writes output to data/faculty.json
"""

import html as html_lib
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
PIPELINE_VERSION = "2026.11.26-strict-author-match-v3"

# How many years back to search for publications. Rolling, not a fixed year —
# recalculated from the actual run date each time so it stays current
# (e.g. run in 2027 searches 2022-2027, not a stale 2018 cutoff).
PUBLICATION_WINDOW_YEARS = 5

# Stop trying more Computed Authors seeds once the pooled clusters cover this
# fraction of the papers already verified by name search.
CA_MIN_OVERLAP = 0.5
# How many verified papers to try as Computed Authors seeds (NCBI often splits
# one person into several clusters; each extra seed is one API call).
CA_MAX_SEEDS = 3


def pubmed_start_year():
    return datetime.utcnow().year - PUBLICATION_WINDOW_YEARS

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


# ---------------------------------------------------------------------------
# HTTP fetching
#
# Nov 2026: med.unc.edu HTML pages now sit behind a bot-check interstitial
# ("checking your connection"), which answers our requests with HTTP 403.
# Retrying a bot-check 403 almost never helps and cost ~45 min per run in
# Step 2, so 403s are NOT retried unless the caller asks (retry_403=True).
# The last HTTP status of each call is kept in LAST_STATUS so callers can
# tell "blocked" (403) apart from "missing" (404) and network errors.
# ---------------------------------------------------------------------------

USER_AGENT = "UNC-Research-Explorer/1.0 (student research tool; contact: jgbresearch@unc.edu)"
LAST_STATUS = {"code": None}


def _http_get(url, accept, retries=3, delay=1.5, retry_403=False):
    import ssl
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    req = urllib.request.Request(url, headers=headers)
    # Some UNC subdomains have certificate chain issues — use unverified context for .unc.edu
    ctx = ssl.create_default_context()
    if "unc.edu" in url or "unclineberger.org" in url:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    LAST_STATUS["code"] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                LAST_STATUS["code"] = resp.status
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            LAST_STATUS["code"] = e.code
            if e.code == 404:
                return None
            # WP REST answers 400 (rest_post_invalid_page_number) when asked
            # for a page past the end — that is the normal end of pagination.
            if e.code == 400 and "/wp-json/" in url:
                return None
            print(f"  HTTP {e.code} fetching {url} (attempt {attempt+1})")
            if e.code == 403 and not retry_403:
                return None
        except Exception as e:
            LAST_STATUS["code"] = None
            print(f"  Error fetching {url}: {e} (attempt {attempt+1})")
        if attempt < retries - 1:
            time.sleep(delay * (attempt + 1))
    return None


def fetch_url(url, retries=3, delay=1.5, retry_403=False):
    return _http_get(url, "text/html,application/xhtml+xml",
                     retries=retries, delay=delay, retry_403=retry_403)


def fetch_json(url, retries=3, delay=1.5):
    """Like fetch_url but sends Accept: application/json — used for REST API calls."""
    return _http_get(url, "application/json", retries=retries, delay=delay)


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


def _wp_base_from_url(url):
    """Old heuristic: WP site = first path segment (e.g. https://www.med.unc.edu/pediatrics).
    Only correct for top-level department sites — Medicine divisions and
    Plastic Surgery are their own sites one level deeper, so departments.json
    now gives an explicit api.base instead."""
    parsed = urllib.parse.urlparse(url)
    path_parts = parsed.path.strip("/").split("/")
    return f"{parsed.scheme}://{parsed.netloc}/{path_parts[0]}"


def _flatten_strings(obj, out, depth=0):
    """Collect all string leaves of a nested dict/list (used to read position
    titles out of custom-field blobs whose exact shape varies by post type)."""
    if depth > 6:
        return
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten_strings(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _flatten_strings(v, out, depth + 1)


# Keys on a WP REST entry that may hold position/title custom fields.
# ud_entry uses ud_entry_custom_fields.ud_positions; the 'directory' post
# type's field names were not confirmed when this was written, so for any
# other key that looks like a custom-field container we flatten its strings.
_WP_SKIP_KEYS = {"content", "excerpt", "yoast_head", "yoast_head_json", "_links",
                 "guid", "title", "link", "slug", "class_list", "id", "date",
                 "date_gmt", "modified", "modified_gmt", "status", "type",
                 "template", "featured_media", "author"}


def _upd_field(entry, key):
    """'directory' entries store fields as top-level lists, e.g.
    "_upd_degree1": ["MD"], "_upd_gallery_summary": ["Assistant Professor"].
    Returns plain text with tags stripped and entities decoded."""
    v = entry.get(key)
    if isinstance(v, list):
        v = " ".join(str(x) for x in v if x)
    text = re.sub(r"<[^>]+>", " ", str(v or ""))
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _wp_degree_text(entry):
    """'directory' entries keep credentials in _upd_degree1/_upd_degree2
    (Rad Onc / Anesthesiology titles are bare names)."""
    return " ".join(_upd_field(entry, k) for k in entry
                    if isinstance(k, str) and k.startswith("_upd_degree"))


def _wp_position_text(entry):
    parts = []
    custom = entry.get("ud_entry_custom_fields", {})
    if isinstance(custom, dict):
        positions = custom.get("ud_positions", [])
        if isinstance(positions, list):
            parts.extend(p.get("ud_title", "") for p in positions if isinstance(p, dict))
    # 'directory' post type: the position/title line lives in
    # _upd_gallery_summary (e.g. "Assistant Professor", "Resident Physician",
    # "CA-1", "Medical School: ..." for residents).
    summary = _upd_field(entry, "_upd_gallery_summary")
    if summary:
        parts.append(summary)
    for k, v in entry.items():
        if k in _WP_SKIP_KEYS or k == "ud_entry_custom_fields" or k.startswith("_upd_"):
            continue
        if isinstance(v, (dict, list)) and (
                "field" in k or k in ("acf", "meta", "toolset", "wpcf") or "position" in k or "title" in k):
            _flatten_strings(v, parts)
    return " ".join(p for p in parts if isinstance(p, str))


def scrape_faculty_via_wp_rest(base_url, dept_name, post_type="ud_entry",
                               api_base=None, exclude_paths=None, division_role=True):
    """
    Scrape people from a UNC SOM WordPress site's public REST API.

    Nov 2026: this is now the PRIMARY source for every med.unc.edu department
    (see the 'api' blocks in departments.json), because the HTML pages sit
    behind a bot-check that returns 403. Two post types are in use:
      - ud_entry   — most departments + all Medicine divisions
      - directory  — Anesthesiology, Derm, Family Med, Neurosurgery, Ophth,
                     Ortho, Pathology, PM&R, Rad Onc, Urology

    Endpoint: <api_base>/wp-json/wp/v2/<post_type>?per_page=100&page=N

    Fields used:
      - title.rendered: full name with credentials
      - ud_entry_custom_fields.ud_positions (ud_entry) / other custom-field
        blobs (directory): position titles for credential + trainee checks
      - class_list: ud_division-{code} for division membership
      - link: profile URL
      - the entry's own text is scanned for a PubMed / MyNCBI / ORCID link so
        Step 2 doesn't need the (blocked) profile page for those people.

    We filter to faculty (MD, PhD, DO, etc.) and exclude admin/nursing/support staff.
    """
    wp_base = (api_base or _wp_base_from_url(base_url)).rstrip("/")
    api_url = f"{wp_base}/wp-json/wp/v2/{post_type}"
    wp_path = urllib.parse.urlparse(wp_base).path.rstrip("/")  # e.g. '/medicine/cardiology'
    exclude_paths = exclude_paths or []

    # Faculty credential patterns — only include people with these in their title
    FACULTY_CREDENTIALS = re.compile(
        r"\b(MD|PhD|DO|PharmD|DrPH|DVM|DDS|MBBS|ScD|MD-PhD|MD/PhD)\b",
        re.IGNORECASE
    )

    # Titles that identify trainees — exclude anyone whose position matches these
    TRAINEE_TITLES = re.compile(
        r"\b(resident|residency|intern|interns|fellow(?!ship\s+director|ship\s+program\s+director)"
        r"|student|clerkship|trainee|house\s+officer|pgy[-\s]?\d|class\s+of\s+\d{4}"
        # 'directory' resident markers (Anesthesiology "CA-1", PM&R
        # "Medical School: ...", "Alumni | Class of 2025")
        r"|ca-[123]|medical\s+school:?|alumni"
        # General Surgery residents' summary is just their med school
        r"|school\s+of\s+medicine|college\s+of\s+medicine|medical\s+college)\b",
        re.IGNORECASE
    )

    # A real faculty rank anywhere in the positions overrides a trainee keyword.
    FACULTY_RANK = re.compile(
        r"\b(professor|instructor|lecturer|chief(?!\s+resident)|chair|attending|faculty"
        r"|director)\b",
        re.IGNORECASE
    )

    PER_PAGE = 100
    # ud_entry: request only the fields we need (+ content, for PubMed links).
    # Other post types: fields aren't confirmed yet, so fetch whole entries.
    fields_param = ("&_fields=id,title,link,content,ud_entry_custom_fields,class_list,ud_division"
                    if post_type == "ud_entry" else "")

    faculty = []
    seen = set()
    page = 1
    total_entries_seen = 0
    rejected_no_credential = 0
    rejected_trainee = 0
    rejected_no_position = 0
    rejected_wrong_dept = 0
    hints_found = 0
    first_entry_keys = None

    while True:
        url = f"{api_url}?per_page={PER_PAGE}&page={page}{fields_param}"
        body = fetch_json(url)
        if not body:
            if page == 1:
                print(f"    WP REST: no response (HTTP {LAST_STATUS['code']}) from {api_url}")
            break
        try:
            entries = json.loads(body)
        except Exception as e:
            print(f"    WP REST: JSON parse error on page {page}: {e}")
            print(f"    Raw response (first 300 chars): {body[:300]}")
            break
        if not isinstance(entries, list) or len(entries) == 0:
            break
        if first_entry_keys is None and isinstance(entries[0], dict):
            first_entry_keys = sorted(entries[0].keys())

        total_entries_seen += len(entries)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # Get name from title. Strip tags, THEN decode entities —
            # titles arrive as e.g. "Mar&iacute;a L&oacute;pez, MD" or "&#8217;".
            raw_title = entry.get("title", {})
            if isinstance(raw_title, dict):
                raw_title = raw_title.get("rendered", "")
            raw_title = re.sub(r"<[^>]+>", "", raw_title or "")
            raw_title = html_lib.unescape(raw_title).replace("\u00a0", " ").strip()

            position_text = _wp_position_text(entry)
            degree_text = _wp_degree_text(entry)

            # 'directory' entries with NO position line are excluded (decided
            # 2026-11-19). In Surgery/Family Med these are mostly residents
            # added at July/September start dates; ~4 real faculty are lost
            # with them until their department fills in a title.
            # (Only when the site uses the field at all — key present, value empty.)
            if (post_type == "directory" and "_upd_gallery_summary" in entry
                    and not _upd_field(entry, "_upd_gallery_summary")):
                rejected_no_position += 1
                continue

            # Exclude trainees — but only when nothing marks them as faculty.
            # Positions like "Associate Professor; Residency Program Director"
            # or "Director of Student Research" mention trainees without being
            # one; the Nov 2026 full test lost ~60 General Surgery faculty and
            # many division faculty to this before the rank check was added.
            if (TRAINEE_TITLES.search(raw_title) or TRAINEE_TITLES.search(position_text)) \
                    and not FACULTY_RANK.search(position_text):
                rejected_trainee += 1
                continue

            # Only include people with faculty credentials
            if not FACULTY_CREDENTIALS.search(raw_title) and not FACULTY_CREDENTIALS.search(degree_text):
                # Also check positions for faculty title
                if not FACULTY_CREDENTIALS.search(position_text):
                    # Check for professor/instructor titles
                    # No MD/PhD anywhere: keep only with an academic rank.
                    # ("director" was dropped from this list Nov 2026 — it let
                    # admin staff like "Director of Clinical Operations, MHA" in.)
                    if not re.search(
                        r"\b(professor|instructor|lecturer|chief(?!\s+resident))\b",
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

            link = entry.get("link", "") or ""
            # WordPress leaves deleted posts reachable with a "__trashed" slug
            # (seen Nov 2026: .../people/mason-sanders-md__trashed/). Skip them.
            if "__trashed" in link:
                continue

            # Skip people whose profile URL belongs to a different site.
            # e.g. Brent Kinder appears in Neurology's ud_entry feed because he
            # treats TSC patients, but his profile is under /medicine/pulmonary/
            # Only applies to links on the same host. Some 'directory' entries
            # link straight to an external page (Rad Onc's REX/UNC Health
            # physician pages, or their Lineberger profile) — those people
            # are still this department's faculty, so keep them.
            same_host = urllib.parse.urlparse(link).netloc.lower() == \
                urllib.parse.urlparse(wp_base).netloc.lower()
            if link and wp_path and same_host:
                profile_path = urllib.parse.urlparse(link).path
                if not (profile_path == wp_path or profile_path.startswith(wp_path + "/")):
                    rejected_wrong_dept += 1
                    continue
                if any(profile_path.startswith(x) for x in exclude_paths):
                    rejected_wrong_dept += 1
                    continue
            seen.add(name_key)

            # Get division from class_list
            class_list = entry.get("class_list", [])
            if isinstance(class_list, dict):
                class_list = list(class_list.values())
            if not isinstance(class_list, list):
                class_list = []
            division = ""
            # Division tags are meaningful on Pediatrics (its subspecialties)
            # but are generic noise on most other sites ("Faculty",
            # "Clinician", "All", "Cecmh"), so other departments set
            # division_role: false in departments.json and get role "".
            for cls in (class_list if division_role else []):
                if not (isinstance(cls, str) and cls.startswith("ud_division-")):
                    continue
                slug = cls[len("ud_division-"):]
                # Skip catch-all / meaningless terms that Medicine sites use
                # (e.g. "all-faculty-profiles", "division-of-cardiology", "118")
                # — they'd show up as a bogus "role". Real subdivisions
                # (Pediatrics, Cardiology "hypertension") are kept.
                if (slug.isdigit() or slug.startswith(("all-", "all_")) or slug == "all_peds"
                        or slug.startswith(("division-of", "division_of"))):
                    continue
                division = slug.replace("_", " ").replace("-", " ").title()
                break

            rec = {
                "name": name,
                "profile_url": link,
                "department": dept_name,
                "role": division,
                "source": f"wp_rest:{post_type}",
            }
            # Look for a curated PubMed / MyNCBI / ORCID link in the entry
            # itself (bio content + custom fields) so Step 2 can skip the
            # blocked profile page for these people.
            try:
                hint = scrape_profile_for_pubmed_string(link or "wp_rest", html=json.dumps(entry))
            except Exception:
                hint = None
            if hint:
                rec["pubmed_hint"] = hint
                hints_found += 1
            faculty.append(rec)

        print(f"    WP REST page {page}: {len(entries)} entries, {len(faculty)} faculty so far")
        if len(entries) < PER_PAGE:
            break  # last page — no need to request one past the end
        page += 1
        if page > 50:
            print("    WP REST: stopping at 50 pages (safety cap)")
            break
        time.sleep(0.3)

    no_pos = f"{rejected_no_position} no position, " if post_type == "directory" else ""
    print(f"    WP REST summary [{post_type}]: {total_entries_seen} entries, "
          f"{rejected_trainee} trainees, {rejected_no_credential} no credential, {no_pos}"
          f"{rejected_wrong_dept} other site, {len(faculty)} kept, "
          f"{hints_found} with PubMed/ORCID link")
    if total_entries_seen > 0 and len(faculty) == 0:
        # Loud: this is the failure mode if a post type stores credentials
        # somewhere we don't look. Print the field names so the fix is obvious.
        print(f"    ⚠ WP REST: {total_entries_seen} entries but 0 kept from {api_url} — "
              f"check credential fields. Entry keys: {first_entry_keys}")

    return faculty


def scrape_faculty_from_page(url, dept_name):
    """
    Scrape a department faculty page and return list of faculty dicts.
    Strategy: find anchor tags whose text looks like a person's name
    and whose href points to a profile page on the same domain.
    """
    print(f"  Scraping: {url}")
    # Listing pages: keep retrying 403s — under the bot-check a retry
    # occasionally gets through, and there are only a few listing URLs.
    html = fetch_url(url, retry_403=True)
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
        r"orcid(?:\.org)?[=:/]+(\d{4}-\d{4}-\d{4}-\d{3}[\dX])",
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
    # It appears near the top of the page body, after the name — but a fixed
    # prefix window is fragile to page template changes (extra nav/header
    # markup pushes this text further down, silently breaking the check for
    # ~everyone at once, as happened in the 2026-07-12 run). Scan the full
    # page text instead of truncating.
    snippet = text.lower()

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
    # Previously this only scanned the first 3000 chars. That's fragile to
    # any page-template change (extra header/nav markup pushes the actual
    # credential/title text further down the page) and was the root cause
    # of the 2026-07-12 run wrongly excluding 2,916 of 3,030 scraped
    # faculty — including division chiefs and endowed professors — as
    # having "no faculty credentials". Scan the full page text instead.
    snippet = text

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


def fetch_pmids_via_orcid(orcid_id, since_year=None):
    """
    Fetch PMIDs from ORCID works API for a given ORCID iD, filtered to the
    rolling publication window (default: last PUBLICATION_WINDOW_YEARS years).
    Also returns DOIs for works that don't have PMIDs, so we can
    look them up in PubMed via doi[AID].
    Returns (pmids, dois) — both lists sorted recent-first.
    """
    if since_year is None:
        since_year = pubmed_start_year()
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
    # Generational suffixes ('James F. Howard Jr', 'Kenneth Brown Jr.', 'John Smith III')
    # — otherwise 'Jr' is taken as the surname.
    name = re.sub(r"[,\s]+(?:jr|sr|ii|iii|iv)\.?\s*$", "", name, flags=re.IGNORECASE)
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
    # Surname particles belong to the surname ('Linda Van Le' → 'Van Le L')
    if len(first_parts) >= 2 and first_parts[-1].lower() in {
            "de", "van", "von", "del", "der", "di", "da", "la", "le", "den", "ter", "dos", "du"}:
        last = f"{first_parts[-1]} {last}"
        first_parts = first_parts[:-1]

    # If any part is a single letter (initial), use initials format for all first parts
    # e.g. ['William', 'Y'] → 'WY', ['E', 'Claire'] → 'EC'
    has_initial = any(len(p) == 1 and p.isalpha() for p in first_parts)
    if has_initial:
        initials = "".join(p[0].upper() for p in first_parts if p and p[0].isalpha())
        return f"{last} {initials}"

    # No initials — use full first name only (middle name dropped to avoid mismatch)
    return f"{last} {first_parts[0]}"


def pubmed_search(search_term, affiliation="University of North Carolina", max_results=300):
    """
    Query PubMed and return list of PMIDs from the last PUBLICATION_WINDOW_YEARS.
    search_term: either a name-based string like 'Doe JA' or an ORCID.
    max_results defaults to 300 (was 5) so we capture a person's full recent
    publication list, not just a handful — PubMed's esearch returns this in
    ONE request regardless of size, so this doesn't add extra round trips.
    """
    current_year = datetime.utcnow().year
    start_year = pubmed_start_year()
    if search_term.startswith("ORCID:"):
        orcid = search_term.replace("ORCID:", "")
        query = f'{orcid}[auid] AND "{affiliation}"[Affiliation] AND ("{start_year}"[PDAT] : "{current_year}"[PDAT])'
    elif affiliation:
        query = f'"{search_term}"[Author] AND "{affiliation}"[Affiliation] AND ("{start_year}"[PDAT] : "{current_year}"[PDAT])'
    else:
        # No affiliation filter — used as last resort for recently recruited faculty
        query = f'"{search_term}"[Author] AND ("{start_year}"[PDAT] : "{current_year}"[PDAT])'

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


# ---------------------------------------------------------------------------
# Author verification — deciding whether a PubMed paper belongs to THIS faculty
# member. Every paper shown on the site must pass one of two checks:
#
#   strict:  an author on the paper matches the faculty member's surname AND
#            first initial (and middle initial when both sides have one), and
#            THAT author's own affiliation is UNC-Chapel Hill.
#   orcid:   that author block carries the faculty member's ORCID iD, where the
#            iD was itself established from strictly verified UNC papers (or
#            supplied by the faculty member's own profile page).
#
# Matching on surname alone, or on "north carolina" anywhere in the address,
# let same-surname UNC colleagues and Duke / Wake Forest / RTI authors through.
# ---------------------------------------------------------------------------

_OTHER_UNC_CAMPUSES = r"(?:charlotte|greensboro|wilmington|asheville|pembroke)"

UNC_AFFILIATION_PATTERNS = [
    re.compile(r"\bchapel\s+hill\b"),
    re.compile(r"\blineberger\b"),
    re.compile(r"\bgillings\s+school\b"),
    re.compile(r"\beshelman\s+school\b"),
    # "University of North Carolina", "Univ. of North Carolina", "University of NC"
    # — but not the other UNC-system campuses.
    re.compile(r"\buniv(?:ersity|\.)?\s+of\s+(?:north\s+carolina|n\.?\s?c\.?)(?![a-z])"
               r"(?![,\s\-]*(?:at\s+)?" + _OTHER_UNC_CAMPUSES + r")"),
    # "UNC", "UNC-CH", "UNC Health", "UNC School of Medicine" — not "UNC Charlotte"
    re.compile(r"\bunc\b(?![\s\-]*" + _OTHER_UNC_CAMPUSES + r")"),
]
# "UNC" is also Universidad Nacional de Córdoba (Argentina).
_UNC_ABBREV_FALSE_CONTEXT = re.compile(r"\b(?:argentina|cordoba)\b")


def _ascii_lower(s):
    import html
    import unicodedata
    # PubMed XML carries entities like 'Hendr&#xe9;e' — decode before folding.
    s = unicodedata.normalize("NFKD", html.unescape(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def affiliation_is_unc(aff_string):
    """
    True if ONE affiliation string names UNC-Chapel Hill. Callers with several
    affiliations should test each one (any_affiliation_is_unc), so that the
    Córdoba exclusion and campus exclusions apply per institution.
    """
    aff = _ascii_lower(aff_string)
    if not aff:
        return False
    for pat in UNC_AFFILIATION_PATTERNS:
        m = pat.search(aff)
        if not m:
            continue
        if m.group(0).startswith("unc") and _UNC_ABBREV_FALSE_CONTEXT.search(aff):
            continue
        return True
    return False


def any_affiliation_is_unc(affs):
    return any(affiliation_is_unc(a) for a in affs)


def _letters(s):
    """Lowercase ASCII letters only — 'O’Leary' → 'oleary', 'Baskin-Miller' → 'baskinmiller'."""
    return re.sub(r"[^a-z]", "", _ascii_lower(s))


def _surname_tokens(name_or_term):
    """
    Candidate surname forms (as _letters() keys) for matching a PubMed <LastName>.

    'Hanks BA'                 -> {'hanks'}
    'de Silva AM'              -> {'silva', 'desilva'}
    'Luis E. Borges Espinosa'  -> {'espinosa', 'borgesespinosa'}
    'Jacquelyn Baskin-Miller'  -> {'baskinmiller'}
    """
    if not name_or_term:
        return set()
    PARTICLES = {"de", "van", "von", "del", "der", "di", "da", "la", "le", "den", "ter", "dos", "du"}
    tokens = [t.strip(".,") for t in name_or_term.split() if t.strip(".,")]
    # Drop a trailing initials block ('Hanks BA'). Only ALL-CAPS tokens count:
    # a two-letter surname at the end of a full name ('Danny T.Y. Wu', 'Yun Li')
    # is the surname, not initials.
    while (len(tokens) > 1 and len(tokens[-1]) <= 3 and tokens[-1].isalpha()
           and tokens[-1].isupper()):
        tokens.pop()
    if not tokens:
        return set()
    last = tokens[-1]
    # Hyphenated surnames are indexed whole ('Baskin-Miller' → 'baskinmiller');
    # matching either half alone would let in every 'Miller J'.
    candidates = {_letters(last)}
    if len(tokens) >= 2:
        prev = tokens[-2]
        if prev.lower() in PARTICLES and len(tokens) >= 3:
            candidates.add(_letters(prev + last))
        elif (len(tokens) >= 3 and len(_letters(prev)) > 1
              and "." not in prev and not prev.isupper()):
            # Two-part surnames ('Borges Espinosa') are indexed as one LastName.
            candidates.add(_letters(prev + last))
    candidates.discard("")
    return candidates


# Common English nicknames → initial of the formal name PubMed would index.
NICKNAME_INITIALS = {
    "bill": "w", "billy": "w", "will": "w", "liam": "w",
    "bob": "r", "bobby": "r", "rob": "r", "robby": "r", "rick": "r", "ricky": "r",
    "dick": "r", "jim": "j", "jimmy": "j", "jack": "j", "jon": "j",
    "chuck": "c", "chip": "c", "ted": "e", "ned": "e", "ed": "e", "eddie": "e",
    "peggy": "m", "polly": "m", "molly": "m", "patsy": "m",
    "betty": "e", "beth": "e", "liz": "e", "libby": "e", "betsy": "e",
    "kate": "c", "katie": "k", "cathy": "c", "sally": "s", "sue": "s",
    "tony": "a", "andy": "a", "drew": "a", "alex": "a",     "hank": "h", "harry": "h", "larry": "l", "terry": "t", "jerry": "g",
    "tom": "t", "tommy": "t", "mike": "m", "mickey": "m", "nick": "n",
    "dan": "d", "danny": "d", "dave": "d", "joe": "j", "joey": "j",
    "sam": "s", "steve": "s", "chris": "c", "matt": "m", "pat": "p",
    "greg": "g", "jeff": "j", "ben": "b", "fred": "f", "ken": "k",
    "ann": "a", "annie": "a", "jen": "j", "jenny": "j",
    "meg": "m", "maggie": "m", "becky": "r", "abby": "a", "gabe": "g",
}


def build_author_identity(faculty_name, search_term=""):
    """
    What a PubMed author block must look like to count as this faculty member.
    Returns dict: surnames (set of _letters keys), first_initials (allowed set),
    first (first given-name initial), middle (middle initial or None).
    """
    clean = clean_name_for_pubmed(faculty_name)
    tokens = [t.strip(".,") for t in clean.split() if t.strip(".,")]
    surnames = _surname_tokens(clean)
    # Given names = everything before the surname (and before a surname
    # particle like 'de'/'van'). For two-part surnames we can't know where the
    # split is, so only the last token is treated as surname here.
    PARTICLES = {"de", "van", "von", "del", "der", "di", "da", "la", "le", "den", "ter", "dos", "du"}
    given = tokens[:-1] if len(tokens) >= 2 else []
    if len(given) >= 2 and given[-1].lower() in PARTICLES:
        given = given[:-1]
    given_letters = [_letters(g) for g in given if _letters(g)]
    first_initials = set()
    if given_letters:
        first_initials.add(given_letters[0][0])
        # Goes by middle name ('E. Claire Dees', 'J. Niklas Ulrich'): PubMed
        # may index the middle name as the first.
        if len(given_letters[0]) == 1 and len(given_letters) >= 2:
            first_initials.add(given_letters[1][0])
        if given_letters[0] in NICKNAME_INITIALS:
            first_initials.add(NICKNAME_INITIALS[given_letters[0]])
    # A curated "Lastname INITIALS" search term carries PubMed's own initials.
    st_parts = (search_term or "").replace("*", "").split()
    if (len(st_parts) >= 2 and 1 <= len(st_parts[-1]) <= 3 and st_parts[-1].isalpha()
            and st_parts[-1].isupper()
            and _letters(" ".join(st_parts[:-1])) in surnames):
        first_initials.add(st_parts[-1][0].lower())
    return {
        "surnames": surnames,
        "first_initials": first_initials,
        "given": given_letters,
        "hyphenated_first": bool(given) and "-" in given[0],
        "first": given_letters[0][0] if given_letters else None,
        # Full given names the faculty member could publish under: the first
        # given name, or the second if the first is just an initial
        # ('J. Keith Smith' → keith), plus a goes-by middle name.
        "full_firsts": {g for g in given_letters[:2] if len(g) >= 2}
                       if (given_letters and len(given_letters[0]) == 1)
                       else ({given_letters[0]} if given_letters else set()),
        "middle": given_letters[1][0] if len(given_letters) >= 2 else None,
    }


def _normalize_orcid(raw):
    digits = re.sub(r"[^0-9Xx]", "", raw or "").upper()
    if len(digits) != 16:
        return None
    return "-".join(digits[i:i + 4] for i in range(0, 16, 4))


def _block_initials(fore, initials):
    """PubMed initials for an author block, lowercase ('William Y' → 'wy')."""
    if initials:
        return _letters(initials)
    if fore:
        return "".join(_letters(p)[:1] for p in re.split(r"[\s\-]+", fore) if _letters(p))
    return ""


# Nickname ↔ formal given names. Prefixes ('Dan'/'Daniel', 'Alex'/'Alexandra')
# are matched separately, so only non-prefix pairs need listing.
NICKNAME_FORMAL = {
    "bill": {"william"}, "billy": {"william"}, "liam": {"william"},
    "bob": {"robert"}, "bobby": {"robert"}, "rick": {"richard", "eric"}, "ricky": {"richard"},
    "dick": {"richard"}, "jim": {"james"}, "jimmy": {"james"}, "jack": {"john"},
    "chuck": {"charles"}, "chip": {"charles"}, "ted": {"theodore", "edward"},
    "teddy": {"theodore", "edward"}, "ned": {"edward"}, "eddie": {"edward"},
    "peggy": {"margaret"}, "meg": {"margaret"}, "maggie": {"margaret"},
    "molly": {"mary"}, "polly": {"mary"}, "patsy": {"patricia"}, "trish": {"patricia"},
    "betty": {"elizabeth"}, "beth": {"elizabeth"}, "liz": {"elizabeth"},
    "libby": {"elizabeth"}, "betsy": {"elizabeth"}, "lisa": {"elizabeth"},
    "kate": {"katherine", "catherine", "kathryn"}, "katie": {"katherine", "catherine", "kathryn"},
    "kathy": {"katherine", "kathryn"}, "cathy": {"catherine"}, "sally": {"sarah"},
    "sue": {"susan", "suzanne"}, "tony": {"anthony", "antonio"}, "drew": {"andrew"},
    "sandy": {"sandra", "alexander"}, "hank": {"henry"}, "harry": {"henry", "harold"},
    "larry": {"lawrence", "laurence"}, "terry": {"terrence", "teresa", "theresa"},
    "jerry": {"gerald", "jerome"}, "mike": {"michael"}, "mickey": {"michael"},
    "dave": {"david"}, "joe": {"joseph"}, "joey": {"joseph"}, "steve": {"steven", "stephen"},
    "jen": {"jennifer"}, "jenny": {"jennifer"}, "becky": {"rebecca"}, "abby": {"abigail"},
    "gabe": {"gabriel"}, "sammy": {"samuel", "samantha"}, "kim": {"kimberly"},
    "tim": {"timothy"}, "nate": {"nathan", "nathaniel"}, "zach": {"zachary"},
    "jake": {"jacob"}, "rob": {"robert"}, "robbie": {"robert"}, "will": {"william"},
    "bert": {"albert", "robert", "herbert"}, "al": {"albert", "alan", "allen"},
    "manny": {"manuel", "emmanuel"}, "pepe": {"jose"}, "nacho": {"ignacio"},
    "paco": {"francisco"}, "pancho": {"francisco"}, "lupe": {"guadalupe"},
    "jackie": {"jacqueline", "jacquelyn"}, "jacky": {"jacqueline", "jacquelyn"},
    "kathie": {"katherine", "kathryn"}, "ellie": {"eleanor", "elizabeth", "ellen"},
}


def _given_names_compatible(a, b):
    """Two full given names (letters only) that could be the same person."""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 3 and long_.startswith(short):
        return True
    return b in NICKNAME_FORMAL.get(a, ()) or a in NICKNAME_FORMAL.get(b, ())


def _forename_tokens(fore):
    """
    Full given names in a PubMed <ForeName>, letters only, skipping initials
    ('J Keith' → ['keith'], 'JK' → [], 'Pew-Thian' → ['pewthian'],
    'Mohammad Anwar' → ['mohammad', 'anwar']).
    """
    out = []
    for t in (fore or "").split():
        letters = _letters(t)
        if len(letters) <= 1 or (t.replace(".", "").isupper() and len(letters) <= 3):
            continue
        out.append(letters)
    return out


def name_matches_identity(block_initials, identity, fore=""):
    """
    True / False / None (None = block has no given-name data to check).

    Full names: when PubMed has a full first name and so does the faculty
    record, the first given name on one side must be compatible with some
    given name on the other — same name, a prefix or nickname of it — so
    'Yukang' ≠ 'Yuli' and 'Jessica' ≠ 'Joshua', while 'Ted' = 'Theodore',
    'Mohammad Anwar' = 'Anwar', 'Soumya Rahima' = 'S. Rahima'. If the faculty
    record starts with an initial ('A. Sidney Barritt'), a PubMed first name
    starting with that letter also counts ('Alfred S').

    Initials: the first initial must be one the faculty member could publish
    under; if both sides carry a middle initial and the first initials agree
    directly, the middle initials must agree too ('Kim WY' ≠ 'Kim WK').
    """
    block_names = _forename_tokens(fore)
    given = identity.get("given", [])
    if block_names and identity.get("full_firsts"):
        fac_names = set(identity["full_firsts"]) | set(given[:2])
        fac_names = {g for g in fac_names if len(g) >= 2}
        compatible = any(_given_names_compatible(b, f) for b in block_names for f in fac_names)
        if not compatible and given and len(given[0]) == 1:
            compatible = block_names[0][0] == given[0]
        if not compatible:
            return False
        # Middle-name check from the full names, only when both sides have one
        # and the first names line up directly.
        fore_toks = [t for t in (fore or "").split() if _letters(t)]
        if (len(given) >= 2 and len(given[0]) >= 2 and len(fore_toks) >= 2
                and _given_names_compatible(block_names[0], given[0])
                and _letters(fore_toks[1])[0] != given[1][0]):
            return False
        return True
    if not block_initials:
        return None
    if not identity["first_initials"]:
        return None
    if block_initials[0] not in identity["first_initials"]:
        return False
    if (identity["middle"] and len(block_initials) >= 2
            and block_initials[0] == identity["first"]
            and block_initials[1] != identity["middle"]
            and not identity.get("hyphenated_first")):
        return False
    return True


def _evaluate_article(article_xml, identity):
    """
    Parse one <PubmedArticle> into (pub dict, match record). The match record
    lists every author block whose surname matches the faculty member, with
    whether its initials match, whether its own affiliation is UNC, and its
    ORCID iD if PubMed has one.
    """
    pmid_match = re.search(r"<PMID[^>]*>(\d+)</PMID>", article_xml)
    if not pmid_match:
        return None, None
    pmid = pmid_match.group(1)

    title_match = re.search(r"<ArticleTitle[^>]*>(.*?)</ArticleTitle>", article_xml, re.DOTALL)
    title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip() if title_match else ""

    journal_match = re.search(r"<ISOAbbreviation>(.*?)</ISOAbbreviation>", article_xml)
    if not journal_match:
        journal_match = re.search(r"<Title>(.*?)</Title>", article_xml)
    journal = journal_match.group(1).strip() if journal_match else ""

    year_match = re.search(r"<PubDate>.*?<Year>(\d{4})</Year>.*?</PubDate>", article_xml, re.DOTALL)
    if not year_match:
        year_match = re.search(r"<Year>(\d{4})</Year>", article_xml)
    year = year_match.group(1) if year_match else ""

    # Full author list (order preserved) — used for student co-author matching
    # and collaborator networks downstream. Note: <Author\b> so <AuthorList>
    # isn't swallowed as an author block.
    authors = []
    candidates = []
    any_block_has_affs = False
    for auth_block in re.findall(r"<Author\b[^>]*>(.*?)</Author>", article_xml, re.DOTALL):
        last_m = re.search(r"<LastName>(.*?)</LastName>", auth_block)
        fore_m = re.search(r"<ForeName>(.*?)</ForeName>", auth_block)
        init_m = re.search(r"<Initials>(.*?)</Initials>", auth_block)
        collective_m = re.search(r"<CollectiveName>(.*?)</CollectiveName>", auth_block, re.DOTALL)
        block_affs = [re.sub(r"<[^>]+>", "", a) for a in
                      re.findall(r"<Affiliation>(.*?)</Affiliation>", auth_block, re.DOTALL)]
        if block_affs:
            any_block_has_affs = True
        if last_m:
            last = re.sub(r"<[^>]+>", "", last_m.group(1)).strip()
            fore = re.sub(r"<[^>]+>", "", fore_m.group(1)).strip() if fore_m else ""
            inits = re.sub(r"<[^>]+>", "", init_m.group(1)).strip() if init_m else ""
            first = fore or inits
            full = f"{first} {last}".strip() if first else last
            if full:
                authors.append(full)
            if _letters(last) in identity["surnames"]:
                orcid_m = re.search(r'<Identifier[^>]*Source="ORCID"[^>]*>(.*?)</Identifier>', auth_block)
                candidates.append({
                    "name_ok": name_matches_identity(_block_initials(fore, inits), identity, fore),
                    "has_affs": bool(block_affs),
                    "unc": any_affiliation_is_unc(block_affs),
                    "affs": _ascii_lower(" | ".join(block_affs)),
                    "orcid": _normalize_orcid(orcid_m.group(1)) if orcid_m else None,
                })
        elif collective_m:
            coll = re.sub(r"<[^>]+>", "", collective_m.group(1)).strip()
            if coll:
                authors.append(coll)

    # Article-level affiliations (older records keep them outside author blocks)
    all_affs = [re.sub(r"<[^>]+>", "", a) for a in
                re.findall(r"<Affiliation>(.*?)</Affiliation>", article_xml, re.DOTALL)]
    pub = {"pmid": pmid, "title": title, "journal": journal, "year": year, "authors": authors}
    record = {
        "pmid": pmid,
        "candidates": candidates,
        "per_author_affs": any_block_has_affs,
        "article_unc": any_affiliation_is_unc(all_affs),
    }
    return (pub if title else None), record


def _strict_pass(record, identity):
    """
    Does some surname-matched author block pass the strict check?
    Returns (passed, passing_block_or_None, reason_if_failed).
    """
    cands = record["candidates"]
    if not cands:
        return False, None, "surname-not-on-paper"
    named = [c for c in cands if c["name_ok"] is not False]
    if not named:
        return False, None, "initials-mismatch"
    if record["per_author_affs"]:
        unc = [c for c in named if c["unc"]]
        if unc:
            # Prefer a block with a definite initials match over an unknown one
            best = sorted(unc, key=lambda c: c["name_ok"] is not True)[0]
            return True, best, None
        return False, None, "target-not-UNC"
    # No per-author affiliations at all (older records): article-level UNC is
    # only trusted when the surname is uncommon and initials matched.
    if not record["article_unc"]:
        return False, None, "no-UNC-on-paper"
    primary = min(identity["surnames"], key=len) if identity["surnames"] else ""
    if primary in AMBIGUOUS_LASTNAMES:
        return False, None, "common-surname-no-author-affs"
    sure = [c for c in named if c["name_ok"] is True]
    if sure:
        return True, sure[0], None
    return False, None, "initials-unknown-no-author-affs"


# Words too generic to identify a department inside an affiliation string.
_DEPT_STOPWORDS = {"medicine", "school", "department", "center", "centre", "health",
                   "general", "clinical", "sciences", "science", "university", "hospital",
                   "diseases", "disease", "laboratory", "care", "and", "of", "the",
                   "head", "neck", "comprehensive", "cancer"}
# Extra affiliation phrases that identify a department (beyond its own name).
_DEPT_ALIASES = {
    "radiology": [r"biomedical research imaging", r"\bbric\b"],
    "orthopaedics": [r"\borthoped"],
    "pediatrics": [r"\bchildren"],
    "lineberger": [r"\blineberger"],
}


def department_patterns(departments):
    """
    Regexes that match a faculty member's department inside an affiliation
    string, from names like 'Medicine — Cardiology' or 'Obstetrics & Gynecology'.
    Uses 7-letter word prefixes so 'Pediatrics' also matches 'Pediatric'.
    """
    pats = []
    for dept in departments or []:
        if not dept:
            continue
        part = dept.split("—")[-1]
        for word in re.findall(r"[a-z]+", _ascii_lower(part)):
            if word in _DEPT_STOPWORDS or len(word) < 5:
                continue
            pats.append(r"\b" + word[:7])
            pats.extend(_DEPT_ALIASES.get(word, []))
    return [re.compile(p) for p in dict.fromkeys(pats)]


def _mentions_department(aff_text, dept_patterns):
    return bool(dept_patterns) and any(p.search(aff_text or "") for p in dept_patterns)


def select_verified_publications(records_and_pubs, identity, known_orcid=None,
                                 require_strict_anchor=False, dept_patterns=None,
                                 dept_strict=False):
    """
    Apply the strict / ORCID rules across ALL of one faculty member's candidate
    papers at once (the ORCID anchor is learned from the strict passes).

    known_orcid: an iD we already trust (faculty's own profile, or one learned
        earlier from strictly verified papers). Strict passes whose matched
        block carries a DIFFERENT iD are rejected — that's another person.
    require_strict_anchor: for ORCID records found by name search, at least one
        paper must pass the strict UNC check or nothing is accepted.
    dept_patterns: from department_patterns(). When two different ORCID iDs
        each pass strict on 2+ papers (two UNC people with the same name), the
        one whose affiliations name the faculty member's department wins —
        if it does so decisively. Otherwise the result is a conflict.

    dept_strict: a same-name conflict was already settled by department (e.g.
        on the name-search pass); apply the same rule to papers with no iD.

    Returns (pubs, info) where info has keys: orcid, orcid_conflict,
    resolved_by_department, kept, strict, via_orcid, rejected (reason → count).
    """
    from collections import Counter
    strict = {}
    for pub, rec in records_and_pubs:
        if pub is None or rec is None:
            continue
        strict[rec["pmid"]] = _strict_pass(rec, identity)

    # Learn the ORCID anchor from strict passes.
    tally = Counter(b["orcid"] for ok, b, _ in strict.values() if ok and b and b["orcid"])
    anchor, conflict, by_dept = known_orcid, False, False
    if not anchor and tally:
        contenders = [o for o, c in tally.most_common() if c >= 2]
        if len(contenders) == 1:
            anchor = contenders[0]
        elif len(contenders) > 1:
            hits = {o: sum(1 for ok, b, _ in strict.values()
                           if ok and b and b["orcid"] == o
                           and _mentions_department(b["affs"], dept_patterns))
                    for o in contenders}
            ranked = sorted(contenders, key=lambda o: -hits[o])
            h1, h2 = hits[ranked[0]], hits[ranked[1]]
            if h1 >= 2 and h1 >= 3 * h2:
                anchor, by_dept = ranked[0], True
            else:
                conflict = True

    pubs, rejected = [], Counter()
    n_strict = n_orcid = 0
    for pub, rec in records_and_pubs:
        if pub is None or rec is None:
            continue
        ok, block, reason = strict[rec["pmid"]]
        orcid = block["orcid"] if block else None
        if ok and anchor and orcid and orcid != anchor:
            ok, reason = False, "different-ORCID"
        if (ok and (by_dept or dept_strict) and not orcid
                and not _mentions_department(block["affs"], dept_patterns)):
            # Two same-name UNC people: without an iD, only papers listing the
            # faculty member's department can be attributed to them.
            ok, reason = False, "same-name-UNC-other-department"
        if ok:
            pubs.append(pub)
            n_strict += 1
            continue
        if anchor and any(c["orcid"] == anchor and c["name_ok"] is not False
                          for c in rec["candidates"]):
            pubs.append(pub)
            n_orcid += 1
            continue
        rejected[reason] += 1

    if require_strict_anchor and n_strict == 0:
        if pubs:
            rejected["no-UNC-anchor"] += len(pubs)
        pubs = []
        n_orcid = 0

    info = {
        "orcid": anchor, "orcid_conflict": conflict, "resolved_by_department": by_dept,
        "kept": len(pubs), "strict": n_strict, "via_orcid": n_orcid,
        "rejected": dict(rejected),
    }
    return pubs, info


def _pubmed_fetch_records_batch(pmids, identity):
    """
    efetch one batch of PMIDs and return [(pub, record), ...] — no filtering.
    Falls back to esummary (coarser data) if efetch fails.
    """
    if not pmids:
        return []
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
                return pubmed_fetch_records_fallback(pmids, identity)
        except Exception as e:
            print(f"    PubMed efetch error: {e}, falling back to esummary")
            return pubmed_fetch_records_fallback(pmids, identity)

    if xml_data is None:
        print(f"    PubMed efetch failed after 3 retries, falling back to esummary")
        return pubmed_fetch_records_fallback(pmids, identity)

    out = []
    for article_xml in re.split(r"<PubmedArticle>", xml_data)[1:]:
        pub, rec = _evaluate_article(article_xml, identity)
        if rec is not None:
            out.append((pub, rec))
    return out


def pubmed_fetch_summaries(pmids, verify_affiliation=True, search_term="", target_lastname=None,
                           faculty_name=None, known_orcid=None, require_strict_anchor=False,
                           departments=None, dept_strict=False, return_info=False):
    """
    Fetch publication details for PMIDs and (if verify_affiliation) keep only
    papers that verifiably belong to this faculty member — see the block
    comment above select_verified_publications for the rules.

    faculty_name: the person's full name (preferred source of identity).
        target_lastname is accepted for backward compatibility and is treated
        as a full name, which is what callers already passed.
    Chunked into batches of 200 (NCBI's recommended per-request limit).
    Results are sorted by year, most recent first.
    """
    if not pmids:
        return ([], {"orcid": None, "orcid_conflict": False, "resolved_by_department": False,
                     "kept": 0, "strict": 0, "via_orcid": 0, "rejected": {}}) if return_info else []

    identity = build_author_identity(faculty_name or target_lastname or search_term, search_term)

    CHUNK_SIZE = 200
    records = []
    for i in range(0, len(pmids), CHUNK_SIZE):
        records.extend(_pubmed_fetch_records_batch(pmids[i:i + CHUNK_SIZE], identity))
        if i + CHUNK_SIZE < len(pmids):
            time.sleep(PUBMED_SLEEP)  # be polite between chunked requests

    if verify_affiliation:
        pubs, info = select_verified_publications(
            records, identity, known_orcid=known_orcid,
            require_strict_anchor=require_strict_anchor,
            dept_patterns=department_patterns(departments), dept_strict=dept_strict)
        if info["rejected"] or info["via_orcid"]:
            dropped = ", ".join(f"{k} {v}" for k, v in sorted(info["rejected"].items()))
            print(f"      Verify: kept {info['kept']}/{len(records)} "
                  f"(strict {info['strict']}, via ORCID {info['via_orcid']})"
                  + (f"; dropped: {dropped}" if dropped else ""))
        if info["orcid_conflict"]:
            print(f"      Verify: two different ORCID iDs each pass on 2+ papers — mixed identities")
        elif info.get("resolved_by_department"):
            print(f"      Verify: two same-name UNC people — kept the one in this department "
                  f"(ORCID {info['orcid']})")
    else:
        pubs = [p for p, r in records if p is not None]
        # Still learn an ORCID iD from name-matched blocks (useful for MyNCBI etc.)
        from collections import Counter
        tally = Counter(c["orcid"] for p, r in records if r for c in r["candidates"]
                        if c["orcid"] and c["name_ok"] is not False)
        top = tally.most_common(1)
        info = {"orcid": top[0][0] if top and top[0][1] >= 2 else None,
                "orcid_conflict": False, "resolved_by_department": False,
                "kept": len(pubs), "strict": 0, "via_orcid": 0, "rejected": {}}

    def _year_key(p):
        try:
            return int(p.get("year") or 0)
        except (TypeError, ValueError):
            return 0
    pubs.sort(key=_year_key, reverse=True)
    return (pubs, info) if return_info else pubs


def pubmed_fetch_records_fallback(pmids, identity):
    """
    esummary fallback (only when efetch fails). esummary has article-level
    affiliations and 'Last IN' author names only, so records built here can
    pass the strict check only via the article-level rule (uncommon surname,
    initials matched).
    """
    params = urllib.parse.urlencode({
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "json",
        "tool": "unc-research-explorer",
        "email": PUBMED_EMAIL,
        **({"api_key": NCBI_API_KEY} if NCBI_API_KEY else {}),
    })
    url = PUBMED_BASE + "esummary.fcgi?" + params
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        results = data.get("result", {})
        out = []
        for pmid in pmids:
            art = results.get(pmid, {})
            if not art or art.get("error"):
                continue
            aff_list = [a.get("affiliation", "") for a in art.get("affiliationlist", []) or []]
            authors = [a.get("name", "").strip() for a in art.get("authors", []) if a.get("name")]
            candidates = []
            for a in authors:
                parts = a.split()
                if len(parts) >= 2 and _letters(" ".join(parts[:-1])) in identity["surnames"]:
                    candidates.append({
                        "name_ok": name_matches_identity(_letters(parts[-1]), identity),
                        "has_affs": False, "unc": False, "orcid": None, "affs": "",
                    })
            title = re.sub(r"<[^>]+>", "", art.get("title", "")).strip()
            pub = {
                "pmid": pmid,
                "title": title,
                "journal": art.get("source", ""),
                "year": (art.get("pubdate", "") or "")[:4],
                "authors": authors,
            } if title else None
            out.append((pub, {"pmid": pmid, "candidates": candidates,
                              "per_author_affs": False,
                              "article_unc": any_affiliation_is_unc(aff_list)}))
        return out
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

    # A free-text hint must contain the faculty member's surname. Profile and
    # bio regexes sometimes capture unrelated phrases ('pregnant women',
    # 'Munich Germany', 'nez JC'); searching PubMed with those either finds
    # nothing or finds someone else. Rejecting falls back to the name.
    if not hint_clean.startswith("MYNCBI:"):
        surnames = _surname_tokens(clean_name_for_pubmed(faculty_name))
        hint_letters = _letters(hint_clean)
        hint_tokens = {_letters(t) for t in re.split(r"[\s,.\-]+", hint_clean)}
        if surnames and not any(
                (sn in hint_letters) if len(sn) >= 4 else (sn in hint_tokens)
                for sn in surnames):
            print(f"    Rejecting hint '{hint}' — doesn't contain surname of {faculty_name}")
            return None

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
    result = pubmed_search(initial_term, max_results=300)
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
            r = pubmed_search(fallback, max_results=300)
            if r["count"] > 0:
                print(f"    Fallback: '{initial_term}' → '{fallback}' ({r['count']} results)")
                return fallback, r, False

    # Fallback 2: single-initial form of the hint
    hint_parts = [p for p in initial_term.split() if p]
    if len(hint_parts) == 2 and len(hint_parts[1]) > 1:
        alt = f"{hint_parts[0]} {hint_parts[1][0]}"
        if alt != initial_term:
            r = pubmed_search(alt, max_results=300)
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
                    r = pubmed_search(two_init, max_results=300)
                    if r["count"] > 0:
                        print(f"    Fallback (two initials): '{initial_term}' → '{two_init}' ({r['count']} results)")
                        return two_init, r, False

    # Fallback 4: drop affiliation filter (recent recruit)
    term_parts = initial_term.split()
    has_initials = len(term_parts) >= 2 and len(term_parts[-1]) <= 2 and term_parts[-1].isalpha()
    if has_initials:
        r = pubmed_search(initial_term, affiliation="", max_results=300)
        if r["count"] > 0:
            print(f"    No-affiliation fallback: '{initial_term}' found {r['count']} results (recent recruit?)")
            return initial_term, r, True

    return initial_term, {"count": 0, "ids": []}, False


def _filter_recent_pubs(pubs):
    """
    Keep only publications within the rolling PUBLICATION_WINDOW_YEARS window.
    Needed for paths that don't filter by date in their query (Computed Authors,
    MyNCBI, ORCID) — the name-search path already filters via PDAT in the query,
    but this makes the final publications list consistent across all sources.
    Papers with no parseable year are kept (often "ahead of print" articles,
    which are current by definition).
    """
    start_year = pubmed_start_year()
    kept = []
    for p in pubs:
        try:
            y = int(p.get("year") or 0)
        except (TypeError, ValueError):
            y = 0
        if y == 0 or y >= start_year:
            kept.append(p)
    return kept


def _faculty_departments(faculty_member):
    return faculty_member.get("departments") or [faculty_member.get("department", "")]


def _pick_ca_seeds(pubs, max_seeds=3):
    """
    Seed papers for Computed Authors, best first. NCBI rebuilds its author
    clusters weekly, so the newest papers often aren't clustered yet — prefer
    verified papers from before the current year, newest of those first, then
    fall back to this year's papers.
    """
    this_year = datetime.utcnow().year

    def year(p):
        try:
            return int(p.get("year") or 0)
        except (TypeError, ValueError):
            return 0
    older = sorted([p for p in pubs if 0 < year(p) < this_year], key=year, reverse=True)
    recent = [p for p in pubs if p not in older]
    return [p["pmid"] for p in (older + recent)][:max_seeds]


def _upgrade_with_computed_authors(faculty_member, pubs, search_term, target_lastname=None,
                                  known_orcid=None, dept_strict=False):
    """
    Add papers from NCBI's Computed Authors clusters to the strictly verified
    list. NCBI often splits one person into several clusters, so up to
    CA_MAX_SEEDS seeds are tried (skipping seeds already covered) and the
    clusters are pooled. Every cluster paper must pass the same strict / ORCID
    verification as the name search; the result is the UNION of the verified
    name-search papers and the verified cluster papers.
    Updates faculty_member in place and returns the pubs list to use.
    """
    if not pubs or search_term.startswith("ORCID:"):
        return pubs

    # Require at least 2 verified papers before trusting Computed Authors,
    # unless the search term is specific (2+ initials or full given name).
    if len(pubs) < 2:
        term_parts = search_term.split()
        is_specific = (len(term_parts) >= 2 and
                       (len(term_parts[-1]) >= 2 or len(term_parts) >= 3))
        if not is_specific:
            print(f"    Computed Authors: only {len(pubs)} verified paper(s) "
                  f"with ambiguous term '{search_term}' — skipping to avoid wrong cluster")
            return pubs

    pooled, covered, n_clusters = [], set(), 0
    for seed in _pick_ca_seeds(pubs, CA_MAX_SEEDS):
        if seed in covered:
            continue
        ca_pmids, ca_count = fetch_pmids_via_computed_authors(faculty_member["name"], seed_pmid=seed)
        if ca_count == 0:
            print(f"    Computed Authors: no cluster for seed {seed}")
            continue
        if ca_count > 500:
            print(f"    Computed Authors: cluster for seed {seed} too large ({ca_count}), skipping")
            continue
        n_clusters += 1
        covered.update(ca_pmids)
        pooled.extend(p for p in ca_pmids if p not in pooled)
        # One cluster that already covers most verified papers is enough.
        verified_ids = {p["pmid"] for p in pubs}
        if len(verified_ids & covered) >= CA_MIN_OVERLAP * len(verified_ids):
            break

    if not pooled:
        return pubs

    verified_ids = {p["pmid"] for p in pubs}
    print(f"    Computed Authors: {n_clusters} cluster(s), {len(pooled)} PMIDs "
          f"(cover {len(verified_ids & covered)}/{len(verified_ids)} verified)")
    new_ids = [p for p in pooled if p not in verified_ids]
    if not new_ids:
        return pubs
    ca_pubs, info = pubmed_fetch_summaries(
        new_ids, search_term=search_term, verify_affiliation=True,
        faculty_name=faculty_member["name"], known_orcid=known_orcid,
        departments=_faculty_departments(faculty_member), dept_strict=dept_strict,
        return_info=True)
    ca_pubs = _filter_recent_pubs(ca_pubs)
    if info["orcid_conflict"]:
        print(f"    Computed Authors: cluster mixes two ORCID identities — not adding")
        return pubs
    if not ca_pubs:
        return pubs
    print(f"    Computed Authors: +{len(ca_pubs)} verified papers")
    faculty_member["pubmed_search"] = f"ComputedAuthors:{search_term}"
    if info["orcid"] and not faculty_member.get("pubmed_orcid"):
        faculty_member["pubmed_orcid"] = info["orcid"]
    merged = pubs + ca_pubs

    def _year_key(p):
        try:
            return int(p.get("year") or 0)
        except (TypeError, ValueError):
            return 0
    merged.sort(key=_year_key, reverse=True)
    faculty_member["pubmed_count"] = len(set(pooled) | verified_ids)
    return merged


def _hide_unverified(faculty_member, pubs, reason):
    """
    Low-confidence result: don't show papers we can't stand behind. Keep the
    candidate PMIDs (not shown on the site) so a maintainer can review them
    and add a pubmed_overrides.json entry.
    """
    print(f"    ⚠ Hiding {len(pubs)} unverified publication(s) for "
          f"{faculty_member['name']}: {reason}")
    faculty_member["pubmed_unverified_pmids"] = [p["pmid"] for p in pubs]
    faculty_member["pubmed_flag_reason"] = reason
    return []


def enrich_faculty_with_pubmed(faculty_member, pubmed_string=None):
    """
    Attach PubMed publication data to a faculty dict.

    Lookup priority:
      1. Manual override (OVERRIDE: prefix) — skip all logic
      2. MyNCBI bibliography page — faculty-curated, trusted
      3. Name-based PubMed search, strict verification (surname + initials +
         the author's own UNC affiliation), ORCID iD learned from those papers
      4. Computed Authors upgrade — NCBI's disambiguated cluster, accepted
         only if it overlaps the verified papers and re-verifies
      5. ORCID name-search fallback — only when 3 found nothing; papers must
         still include at least one strictly verified UNC paper
    When the evidence is weak (mixed identities, common surname + single
    initial, only found outside UNC), publications are hidden and the profile
    is flagged "needs verification" rather than showing a list that may be wrong.
    """
    name = faculty_member["name"]
    raw_hint = pubmed_string or ""

    faculty_member["pipeline_version"] = PIPELINE_VERSION
    # Clear flags from any previous enrichment of this record
    for k in ("pubmed_orcid", "pubmed_unverified_pmids", "pubmed_flag_reason",
              "pubmed_recent_recruit"):
        faculty_member.pop(k, None)

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
        result = pubmed_search(search_term)
        print(f"    PubMed: {name} → '{search_term}' ({result['count']} results)")
        pubs = []
        if result["ids"]:
            pubs, info = pubmed_fetch_summaries(result["ids"], search_term=search_term,
                                                verify_affiliation=False, faculty_name=name,
                                                return_info=True)
            if info["orcid"]:
                faculty_member["pubmed_orcid"] = info["orcid"]
            pubs = _upgrade_with_computed_authors(faculty_member, pubs, search_term,
                                                  known_orcid=info["orcid"])
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
        pmids, count = fetch_pmids_by_author_id(author_id, max_results=300)
        print(f"    MyNCBI: found {count} PMIDs on bibliography page")
        if pmids:
            # The faculty member's own curated list — trusted, so no UNC
            # re-verification (it would drop papers from prior institutions).
            pubs, info = pubmed_fetch_summaries(pmids, search_term=name, verify_affiliation=False,
                                                faculty_name=name, return_info=True)
            pubs = _filter_recent_pubs(pubs)
            if info["orcid"]:
                faculty_member["pubmed_orcid"] = info["orcid"]
            faculty_member.update({
                "pubmed_search": f"MyNCBI:{author_id}", "pubmed_count": count,
                "pubmed_verified": len(pubs), "pubmed_ambiguous": False, "publications": pubs
            })
            time.sleep(0.4)
            return faculty_member
        print(f"    MyNCBI bibliography empty — falling back to name search")
        slug_parts = [p for p in author_id.split(".") if p and not p.isdigit()]
        if len(slug_parts) >= 2:
            clean_hint = f"{slug_parts[1].capitalize()} {slug_parts[0][0].upper()}"
            print(f"    Slug-derived fallback search: '{clean_hint}'")
        else:
            clean_hint = None

    # ── 3. Name-based PubMed search (primary path for most faculty) ──────────
    profile_orcid = None
    if clean_hint and clean_hint.startswith("ORCID:"):
        initial_term = clean_hint
        # An ORCID iD on the faculty member's own profile page is trusted identity.
        profile_orcid = _normalize_orcid(clean_hint.replace("ORCID:", ""))
    elif clean_hint:
        canonical = build_pubmed_search_string(name)
        if clean_hint != canonical:
            print(f"    Using hint '{clean_hint}' for {name}")
        initial_term = clean_hint
    else:
        initial_term = build_pubmed_search_string(name)

    print(f"    PubMed: {name} → '{initial_term}'")
    search_term, result, recent_recruit = _pubmed_name_search_with_fallbacks(name, initial_term)

    target_lastname = clean_name_for_pubmed(name)
    flag_reason = None
    pubs = []
    anchor_orcid = profile_orcid
    if recent_recruit:
        # Found only by dropping the UNC filter — could be a recent recruit, or
        # someone else with the same name. Don't publish unverified papers.
        faculty_member["pubmed_recent_recruit"] = True
        faculty_member["pubmed_unverified_pmids"] = result["ids"][:50]
        flag_reason = "papers found only outside UNC (recent recruit?)"
    elif result["ids"]:
        pubs, info = pubmed_fetch_summaries(
            result["ids"], search_term=search_term, verify_affiliation=True,
            faculty_name=name, known_orcid=profile_orcid,
            departments=_faculty_departments(faculty_member), return_info=True)
        anchor_orcid = profile_orcid or info["orcid"]
        if info["orcid_conflict"]:
            pubs = _hide_unverified(faculty_member, pubs,
                                    "papers from two different people (ORCID) match this name")
            flag_reason = faculty_member["pubmed_flag_reason"]
        else:
            if anchor_orcid:
                faculty_member["pubmed_orcid"] = anchor_orcid
            pubs = _upgrade_with_computed_authors(
                faculty_member, pubs, search_term, target_lastname, known_orcid=anchor_orcid,
                dept_strict=info.get("resolved_by_department", False))

    # ── 4. ORCID fallback — only when name search was weak ────────────────────
    name_search_weak = (
        len(pubs) == 0 or
        (len(pubs) < 2 and target_lastname.split()[-1].lower() in AMBIGUOUS_LASTNAMES)
    )
    already_have_id_source = faculty_member.get("pubmed_search", "").startswith(
        ("MyNCBI", "ORCID", "OVERRIDE", "ComputedAuthors"))

    try_orcid = (name_search_weak and not already_have_id_source
                 and (flag_reason is None or recent_recruit))
    if try_orcid:
        orcid_id, orcid_given, orcid_family = search_orcid_by_name(name)
        time.sleep(0.2)
        if orcid_id:
            print(f"    ORCID fallback: found {orcid_id} for {orcid_given} {orcid_family}")
            orcid_pmids, orcid_dois = fetch_pmids_via_orcid(orcid_id)  # uses rolling window default
            time.sleep(0.2)
            if not orcid_pmids and orcid_dois:
                print(f"    ORCID: resolving {min(len(orcid_dois), 3)} DOIs...")
                orcid_pmids = resolve_dois_to_pmids(orcid_dois, max_dois=3)
            if orcid_pmids:
                # An ORCID record found by NAME search may be someone else with
                # the same name. Accept its papers only if at least one passes
                # the strict UNC check; then papers carrying that iD also count.
                orcid_pubs = pubmed_fetch_summaries(orcid_pmids, search_term=name,
                                                    verify_affiliation=True,
                                                    faculty_name=name,
                                                    known_orcid=orcid_id,
                                                    require_strict_anchor=True,
                                                    departments=_faculty_departments(faculty_member))
                if len(orcid_pubs) > len(pubs):
                    print(f"    ORCID: {len(orcid_pubs)} papers (better than name search's {len(pubs)})")
                    for k in ("pubmed_unverified_pmids", "pubmed_flag_reason",
                              "pubmed_recent_recruit"):
                        faculty_member.pop(k, None)
                    faculty_member.update({
                        "pubmed_search": f"ORCID:{orcid_id}",
                        "pubmed_orcid": orcid_id,
                        "pubmed_count": len(orcid_pmids),
                        "pubmed_verified": len(orcid_pubs),
                        "pubmed_ambiguous": False,
                        "publications": orcid_pubs,
                    })
                    time.sleep(PUBMED_SLEEP)
                    return faculty_member
                elif not orcid_pubs:
                    print(f"    ORCID: none of {len(orcid_pmids)} papers verified at UNC — not used")

    # Low-confidence: single-initial search on a common surname with few papers.
    term_parts = search_term.replace("ComputedAuthors:", "").split()
    primary_last = target_lastname.split()[-1].lower() if target_lastname else ""
    single_initial = len(term_parts) >= 2 and len(term_parts[-1]) == 1
    low_confidence = (single_initial and primary_last in AMBIGUOUS_LASTNAMES
                      and 0 < len(pubs) < 3 and not already_have_id_source
                      and not anchor_orcid)
    if low_confidence:
        pubs = _hide_unverified(faculty_member, pubs, "common surname + single initial, few papers")
        flag_reason = faculty_member["pubmed_flag_reason"]
    elif flag_reason and "pubmed_flag_reason" not in faculty_member:
        faculty_member["pubmed_flag_reason"] = flag_reason

    faculty_member.update({
        "pubmed_search": faculty_member.get("pubmed_search") or search_term,
        "pubmed_count": faculty_member.get("pubmed_count") or result["count"],
        "pubmed_verified": len(pubs),
        "pubmed_ambiguous": (result["count"] > 0 and len(pubs) == 0) or bool(flag_reason),
        "publications": pubs,
    })
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

# ---- Safety-guard helpers ----
# A full production run must have at least DROP_THRESHOLD of the previous
# committed count AND at least MIN_FULL_RUN_FACULTY in absolute terms.
# The absolute floor exists because the ratio check alone can be walked
# down: Nov 2026 runs went 2,751 → 604 → 378, each one compared only to the
# already-shrunken file before it. ~2,750 was the healthy pre-block total;
# lower the floor deliberately if the real faculty count ever falls that far.
DROP_THRESHOLD = 0.6
MIN_FULL_RUN_FACULTY = 1500


def _read_total(path):
    try:
        with open(path) as fh:
            d = json.load(fh)
        return int(d.get("total_faculty") or len(d.get("faculty", [])))
    except Exception:
        return 0


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2)


def _check_run_size(new_count, baseline, label):
    if new_count < MIN_FULL_RUN_FACULTY:
        return False, (f"{label}: this run has only {new_count} faculty, below the "
                       f"absolute floor of {MIN_FULL_RUN_FACULTY} (MIN_FULL_RUN_FACULTY). "
                       f"Previous committed file had {baseline}.")
    if baseline > 50 and new_count < baseline * DROP_THRESHOLD:
        return False, (f"{label}: previous file had {baseline} faculty; this run only has "
                       f"{new_count} ({new_count/baseline:.0%}). Looks like a pipeline "
                       f"failure, not real attrition.")
    return True, ""


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

    # Baseline for the safety guard — read ONCE, before this run writes anything.
    raw_baseline = _read_total(raw_output_path)

    all_faculty = []

    # ---- Step 1: Scrape faculty pages ----
    print("\n=== Step 1: Scraping faculty pages ===")
    api_counts = {}
    for dept in departments:
        print(f"\n[{dept['name']}]")
        urls = dept.get("urls") or [dept.get("url")]
        urls = [u for u in urls if u]
        dept_faculty = []

        # PRIMARY: WordPress REST API (not behind the med.unc.edu bot-check).
        # If it returns people, the HTML listing pages are skipped entirely —
        # they're blocked anyway, and merging them would re-add nav junk.
        api = dept.get("api")
        if api and api.get("base"):
            print(f"  API: {api['base']}/wp-json/wp/v2/{api.get('type', 'ud_entry')}")
            dept_faculty = scrape_faculty_via_wp_rest(
                api["base"], dept["name"],
                post_type=api.get("type", "ud_entry"),
                api_base=api["base"],
                exclude_paths=api.get("exclude_paths"),
                division_role=api.get("division_role", False),
            )
            api_counts[dept["name"]] = len(dept_faculty)
            if dept_faculty:
                all_faculty.extend(dept_faculty)
                time.sleep(0.5)
                continue
            print(f"  ⚠ API returned no faculty for {dept['name']} — falling back to HTML pages")
            # Mark this site as already tried so the HTML fallback below
            # doesn't call the same API a second time.
            if not hasattr(scrape_faculty_from_page, "_wp_rest_cache"):
                scrape_faculty_from_page._wp_rest_cache = {}
            p_ = urllib.parse.urlparse(api["base"])
            scrape_faculty_from_page._wp_rest_cache[
                f"{p_.netloc}/{p_.path.strip('/').split('/')[0]}"] = True

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
                    # Was 20 — Lineberger's directory now runs past 20 pages,
                    # and the cap silently dropped ~200 members (A–F) in the
                    # Nov 2026 test. Loop still stops at the first empty page.
                    if page > 80:
                        break

        all_faculty.extend(dept_faculty)
        if len(urls) > 1:
            print(f"  Total from {len(urls)} pages: {len(dept_faculty)} faculty")
        time.sleep(0.5)

    print(f"\nTotal faculty scraped: {len(all_faculty)}")
    if api_counts:
        empty = [n for n, c in api_counts.items() if c == 0]
        print(f"  API-sourced departments: {len(api_counts) - len(empty)}/{len(api_counts)} returned faculty")
        if empty:
            print(f"  ⚠ API returned 0 for: {', '.join(empty)}")

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

    # Second pass: same person, name written with/without a middle name or
    # initial ("Nigel Key" vs "Nigel S. Key", "Janice Hwang" vs "Janice Jin
    # Hwang"). The API names often differ from Lineberger's, and the Nov 2026
    # full test left ~90 such pairs as separate entries. Merge when first
    # name + last name match, middle parts don't conflict, and the Jr/Sr/III
    # suffix is the same (keeps e.g. "Will Roper" and "Will Roper Jr" apart).
    def _loose_key(name):
        toks = re.sub(r"[^a-z ]", " ", html_lib.unescape(name).lower()).split()
        suffix = tuple(t for t in toks if t in ("jr", "sr", "ii", "iii", "iv"))
        toks = [t for t in toks if t not in ("jr", "sr", "ii", "iii", "iv")]
        if len(toks) < 2:
            return None, None
        firsts = [t for t in toks[:-1] if len(t) > 1]
        if not firsts:
            return None, None
        i = toks.index(firsts[0])
        middle = [t[0] for t in toks[i + 1:-1]]
        return (firsts[0], toks[-1], suffix), middle

    loose_seen = {}
    merged_loose = []
    loose_merges = 0
    for f in all_faculty:
        key, middle = _loose_key(f["name"])
        target = loose_seen.get(key) if key else None
        if target is not None:
            t_mid = _loose_key(target["name"])[1]
            # conflicting middle initials → different people, don't merge
            if middle and t_mid and middle[0] != t_mid[0]:
                target = None
        if target is None:
            if key and key not in loose_seen:
                loose_seen[key] = f
            merged_loose.append(f)
            continue
        for d in f.get("departments", [f["department"]]):
            if d not in target["departments"]:
                target["departments"].append(d)
        if f.get("pubmed_hint") and not target.get("pubmed_hint"):
            target["pubmed_hint"] = f["pubmed_hint"]
        # keep the fuller name (more middle detail) for display/search
        if len(f["name"]) > len(target["name"]):
            target["name"] = f["name"]
        loose_merges += 1
    if loose_merges:
        print(f"  Merged {loose_merges} name-variant duplicates (middle name/initial differences)")
    all_faculty = merged_loose
    print(f"  {len(all_faculty)} unique faculty after name-variant merge")
    print(f"  {len(all_faculty)} unique faculty after deduplication")

    # ---- Step 2: Profile page visits ----
    print("\n=== Step 2: Checking profiles for PubMed search strings ===")
    trainees_removed = []
    # Circuit breaker: once a host has returned this many 403s in a row, stop
    # requesting its profile pages for the rest of the run. Under the Nov 2026
    # bot-check every med.unc.edu profile 403s, and retrying them all cost
    # ~45 min per run for nothing. Blocked people are KEPT (see below).
    BLOCK_TRIP = 8
    consecutive_403 = {}
    blocked_hosts = set()
    profiles_skipped_blocked = 0
    profiles_skipped_have_hint = 0
    profiles_skipped_offsite = 0
    PROFILE_HOSTS = {"www.med.unc.edu", "med.unc.edu", "unclineberger.org", "www.unclineberger.org"}

    def _host(u):
        return urllib.parse.urlparse(u).netloc.lower()

    def _fetch_profile(u, **kw):
        h = _host(u)
        if h in blocked_hosts:
            return None, True
        body = fetch_url(u, **kw)
        if LAST_STATUS["code"] == 403:
            consecutive_403[h] = consecutive_403.get(h, 0) + 1
            if consecutive_403[h] >= BLOCK_TRIP:
                blocked_hosts.add(h)
                print(f"  ⛔ {h}: {BLOCK_TRIP} profile 403s in a row — skipping its "
                      f"remaining profile pages this run (bot-check)")
            return None, True
        consecutive_403[h] = 0
        return body, False

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
                probe, _blocked = _fetch_profile(candidate, retries=1, delay=0.5)
                if probe:
                    profile_url = candidate
                    f["profile_url"] = profile_url
                    print(f"  {f['name']}: constructed profile URL → {candidate}")
                time.sleep(0.2)

        from_api = str(f.get("source", "")).startswith("wp_rest")

        # API entries were already credential/trainee-filtered from their
        # structured fields. If the API entry also carried a PubMed/ORCID link,
        # the profile page adds nothing — skip it.
        # (Not for Lineberger: its profile page is still needed for the
        # clinical-department filter below, and it isn't behind the bot-check.)
        if profile_url and from_api and f.get("pubmed_hint") and "unclineberger.org" not in profile_url:
            profiles_skipped_have_hint += 1
            continue

        # API entries sometimes link off-site (REX / UNC Health physician
        # pages, LinkedIn, uncmedicalcenter.org). Those pages aren't UNC SOM
        # profiles: no PubMed hint to find, and is_trainee_profile() wrongly
        # rejected 8 Rad Onc faculty on them in the Nov 2026 test. Skip them.
        if profile_url and from_api and _host(profile_url) not in PROFILE_HOSTS:
            profiles_skipped_offsite += 1
            continue

        if profile_url:
            profile_html, was_blocked = _fetch_profile(profile_url, retries=2, delay=1.0)
            if was_blocked:
                # Can't see the profile. Keep the person: API entries passed the
                # API credential filter, and is_trainee_profile(None) has always
                # meant "keep" for HTML entries too. They just get no curated
                # PubMed hint and fall back to name search in Step 3.
                profiles_skipped_blocked += 1
                continue
            time.sleep(0.3)

            if "unclineberger.org" in profile_url:
                if not is_lineberger_clinical(profile_html):
                    print(f"  {f['name']} ({f['department']}): non-clinical Lineberger member — excluding")
                    f["_exclude"] = True
                    trainees_removed.append(f["name"])
                    continue

            # API entries were already credential/trainee-filtered from
            # structured fields, which is more reliable than scanning page text.
            if not from_api and is_trainee_profile(profile_html):
                print(f"  {f['name']} ({f['department']}): excluded (no faculty credentials)")
                f["_exclude"] = True
                trainees_removed.append(f["name"])
                continue

            ps = scrape_profile_for_pubmed_string(profile_url, html=profile_html)
            if ps:
                print(f"  {f['name']}: found '{ps}'")
                f["pubmed_hint"] = ps
        else:
            # No profile_url and none could be constructed — previously this
            # meant the credential check was skipped entirely and the entry
            # was auto-kept, which is how unlinked nav/junk text (plain-text
            # strings with no <a> tag, e.g. "General Obstetrics", "Vice
            # Chair") ended up in the final faculty list unfiltered. A real
            # faculty member scraped from a UNC SOM directory page should
            # almost always have (or be able to construct) a profile link,
            # so treat the absence of one as unverifiable and exclude.
            print(f"  {f['name']} ({f.get('department')}): excluded (no profile link — unverifiable)")
            f["_exclude"] = True
            trainees_removed.append(f["name"])
            continue

    if trainees_removed:
        all_faculty = [f for f in all_faculty if not f.get("_exclude")]
        print(f"  Removed {len(trainees_removed)} excluded: {', '.join(trainees_removed)}")
    if profiles_skipped_have_hint:
        print(f"  Skipped {profiles_skipped_have_hint} profile pages (PubMed/ORCID link already in API data)")
    if profiles_skipped_offsite:
        print(f"  Skipped {profiles_skipped_offsite} off-site profile links (not UNC SOM / Lineberger pages)")
    if profiles_skipped_blocked:
        print(f"  Kept {profiles_skipped_blocked} faculty whose profile page was blocked (403) — no PubMed hint")

    # ---- Safety guard (raw file). The Nov 1 2026 run committed a raw file
    # with 603 of ~2,750 faculty because only the enrichment step had a guard.
    # Full runs only — a --dept test run is expected to be small.
    if not dept_filter:
        ok, msg = _check_run_size(len(all_faculty), raw_baseline, "faculty_raw")
        if not ok:
            fail_path = raw_output_path.replace(".json", "_FAILED_RUN.json")
            _write_json(fail_path, {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "total_faculty": len(all_faculty),
                "departments": [d["name"] for d in departments],
                "faculty": all_faculty,
            })
            print(f"\n⚠️  REFUSING TO WRITE {raw_output_path}")
            print(f"  {msg}")
            print(f"  Suspect output written to {fail_path} for inspection instead.")
            print(f"  {raw_output_path} was left untouched.")
            sys.exit(1)

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
    # Baseline for the safety guard — read ONCE, before anything in this run
    # writes to output_path. (Bug fixed Nov 2026: the guard used to re-read
    # output_path at the END, after the every-100 checkpoint had already
    # overwritten it with this run's own data — so the Nov 8 run compared
    # 604 against 600 and passed, replacing 2,751 faculty with 604.)
    output_baseline = _read_total(output_path)
    checkpoint_path = output_path.replace(".json", "_checkpoint.json")

    with open(raw_input_path) as f:
        raw = json.load(f)

    all_faculty = raw["faculty"]
    departments_in_raw = raw.get("departments", [])

    # Optionally filter to a single department. Load the config so we can match
    # against department short names (e.g. "ENT" → "Otolaryngology...") — without
    # this, a filter like "ENT" matches nothing because it's not a word in the
    # full department name. This bug silently produced 0-faculty enrichment runs.
    if dept_filter:
        dept_short_map = {}
        try:
            with open(config_path) as cf:
                cfg = json.load(cf)
            dept_short_map = {d["name"]: d.get("short", "") for d in cfg["departments"]}
        except Exception:
            pass

        def _dept_ok(dept_name):
            return dept_matches(dept_filter, dept_name, dept_short_map.get(dept_name, ""))

        all_faculty = [f for f in all_faculty
                       if any(_dept_ok(d) for d in f.get("departments", [f.get("department", "")]))]
        departments_in_raw = [d for d in departments_in_raw if _dept_ok(d)]
        print(f"Filter '{dept_filter}': {len(all_faculty)} faculty in {len(departments_in_raw)} department(s)")

    # ---- Resume support (version-aware) ----
    # If a prior run wrote results to output_path, we can skip faculty already
    # enriched — but ONLY if they were enriched by the CURRENT pipeline version.
    # This prevents the stale-data bug: a scheduled full run after a code change
    # must re-enrich everyone, while a timed-out run resuming minutes later
    # (same version) correctly skips completed work.
    already_done = {}
    stale_skipped = 0
    # Read the last committed output, then any checkpoint left by a run that
    # timed out (checkpoint entries win — they're newer).
    for resume_path in (output_path, checkpoint_path):
        if not os.path.exists(resume_path):
            continue
        try:
            with open(resume_path) as f:
                prev = json.load(f)
            for pf in prev.get("faculty", []):
                if "pubmed_verified" not in pf and "publications" not in pf:
                    continue
                if pf.get("pipeline_version") == PIPELINE_VERSION:
                    already_done[pf["name"].lower().strip()] = pf
                elif resume_path == output_path:
                    stale_skipped += 1
        except Exception as e:
            print(f"Resume: could not read {resume_path} ({e}) — ignoring it")
    if already_done:
        print(f"Resume: {len(already_done)} faculty already enriched "
              f"by current pipeline v{PIPELINE_VERSION} — will skip those")
    if stale_skipped:
        print(f"Resume: {stale_skipped} faculty have OLD pipeline version "
              f"— will RE-ENRICH with current logic")

    # ---- Step 3: PubMed enrichment ----
    print("\n=== Step 3: Enriching with PubMed ===")
    for i, f in enumerate(all_faculty):
        name_key = f["name"].lower().strip()
        # Skip only if enriched by the CURRENT pipeline version
        if name_key in already_done:
            prev = already_done[name_key]
            for k in ("pubmed_search", "pubmed_count", "pubmed_verified",
                      "pubmed_ambiguous", "publications", "nih_grants",
                      "pubmed_recent_recruit", "pipeline_version",
                      "pubmed_orcid", "pubmed_unverified_pmids", "pubmed_flag_reason"):
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
        # crash or timeout doesn't lose everything. Written to a SEPARATE
        # file — never to output_path, which the safety guard protects.
        if (i + 1) % 100 == 0:
            checkpoint = {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "total_faculty": len(all_faculty),
                "departments": departments_in_raw,
                "faculty": all_faculty,
                "_checkpoint": f"{i+1}/{len(all_faculty)}",
            }
            _write_json(checkpoint_path, checkpoint)
            print(f"    💾 checkpoint saved at {i+1}/{len(all_faculty)} → {checkpoint_path}")

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

    # ---- Safety guard: refuse to silently overwrite good data with a
    # catastrophically shrunken run. This is what let the 2026-07-12 run
    # replace ~2,900+ previously-enriched faculty with just 114 after a
    # Step 2 exclusion bug wrongly dropped almost everyone — without this
    # guard, that bad output gets committed and the prior good data is gone.
    # Only applies to full (no dept_filter) runs, since a filtered run is
    # expected to have far fewer faculty than the full committed dataset.
    if not dept_filter:
        ok, msg = _check_run_size(len(all_faculty), output_baseline, "faculty")
        if not ok:
            fail_path = output_path.replace(".json", "_FAILED_RUN.json")
            _write_json(fail_path, {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "total_faculty": len(all_faculty),
                "departments": departments_in_raw,
                "faculty": all_faculty,
            })
            print(f"\n⚠️  REFUSING TO WRITE {output_path}")
            print(f"  {msg}")
            print(f"  Suspect output written to {fail_path} for inspection instead.")
            print(f"  {output_path} was left untouched.")
            sys.exit(1)

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
    if os.path.exists(checkpoint_path):
        os.remove(checkpoint_path)  # run finished — checkpoint no longer needed


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
