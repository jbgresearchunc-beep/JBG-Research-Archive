"""
Diagnostic (read-only): scan data/faculty.json for likely duplicate-person
entries that the automatic matching in build_collaborators.py did NOT
already merge, so you can review them and add confirmed cases to
scraper/name_aliases.json.

This does NOT modify faculty.json or auto-merge anything -- it only prints
a report. Three signals are used, most to least trustworthy:

  1. PROFILE URL SLUG MATCH (high confidence)
     Two records whose profile_url ends in the same slug, e.g.:
       .../medicine/hematology/people/benjamin-vincent/
       .../directory/benjamin-vincent/
     Different domain, different department, same slug -- this is what
     caught the real Benjamin Vincent / Benjamin Garrett Vincent case.
     UNC's own systems use the same slug for the same person across
     department and Lineberger listings, so this is strong evidence.
     Ready-to-paste alias entries are printed directly.

  2. LINEBERGER CROSS-LISTING, FULL NAME MATCH (medium confidence)
     Two records with the same first+last name (not just initial -- a
     dropped/added middle name or initial is the only difference) where
     at least one is in Lineberger Comprehensive Cancer Center. This is
     grounded in a specific, verified fact about this dataset: Lineberger
     members are scraped separately from their home department page, and
     the two listings often use different URL slug conventions (so signal
     1 misses them), but the underlying cross-listing mechanism is the
     same one that produced the Vincent duplicate. Ready-to-paste alias
     entries are printed directly, but it's still worth a quick skim --
     this is evidence-based, not a guarantee.

  3. SAME SURNAME + SAME FIRST INITIAL (low confidence, needs your judgment)
     Everything else. Flagged for review only -- this is exactly the class
     of case that produced false positives before (e.g. Sameer Prasada /
     Sudhir Prasada are different people, and in this dataset 'Joseph J.
     Eron' / 'Joseph Eron [Lineberger]' are the same person while
     'Jaquelyn Eron' [Plastic Surgery] sharing the same bucket is someone
     else entirely). Don't add these without actually checking.

Only prints candidates that aren't already resolved by the current
build_identity_map() (profile_url exact match / exact name / loose+dept
/ existing aliases), so re-running this after adding aliases will shrink
the list.

Usage:
    python scraper/find_duplicate_candidates.py --input data/faculty.json --aliases scraper/name_aliases.json
"""
import json
import argparse
import re
from collections import defaultdict

import build_collaborators as bc

LINEBERGER = "Lineberger Comprehensive Cancer Center"


def _url_slug(url):
    """Last non-empty path segment of a profile URL, lowercased."""
    if not url:
        return None
    parts = [p for p in url.rstrip("/").split("/") if p]
    return parts[-1].lower() if parts else None


def _depts(f):
    ds = f.get("departments") or [f.get("department", "")]
    return set(d for d in ds if d)


def _print_group(indices, faculty_list, suggest_alias=False):
    names = [faculty_list[i]["name"] for i in indices]
    for i in indices:
        f = faculty_list[i]
        print(f"    {f['name']!r:35s} | {f.get('department')!r:45s} | {f.get('profile_url')}")
    if suggest_alias:
        canonical = max(names, key=lambda n: len(re.sub(r'[^\w\s]', '', n).split()))
        print(f"  Suggested alias entries (add to name_aliases.json):")
        for n in names:
            if n != canonical:
                print(f'    "{bc._name_dedup_key(n)}": "{canonical}",')
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/faculty.json")
    parser.add_argument("--aliases", default="scraper/name_aliases.json")
    args = parser.parse_args()

    with open(args.input) as f:
        data = json.load(f)
    faculty_list = data["faculty"]
    print(f"Loaded {len(faculty_list)} faculty from {args.input}")

    name_aliases = bc.load_name_aliases(args.aliases)
    print(f"Loaded {len(name_aliases)} existing alias(es) from {args.aliases}\n")

    # Figure out what's already resolved so we don't re-flag it
    index_to_key, _, _ = bc.build_identity_map(faculty_list, name_aliases=name_aliases)

    # ---- Tier 1: profile URL slug match across different identities ----
    slug_buckets = defaultdict(list)
    for i, f in enumerate(faculty_list):
        slug = _url_slug(f.get("profile_url"))
        if slug:
            slug_buckets[slug].append(i)

    print("=" * 70)
    print("HIGH CONFIDENCE — same profile URL slug, not already merged")
    print("=" * 70)
    slug_candidates = []
    already_flagged = set()  # indices already shown in a higher tier
    for slug, indices in slug_buckets.items():
        keys = {index_to_key[i] for i in indices}
        if len(keys) < 2:
            continue
        slug_candidates.append((slug, indices))
        already_flagged.update(indices)

    if not slug_candidates:
        print("(none found)\n")
    for slug, indices in slug_candidates:
        print(f"  slug: {slug!r}")
        _print_group(indices, faculty_list, suggest_alias=True)

    # ---- Tier 2: full name match + Lineberger cross-listing ----
    loose_buckets = defaultdict(list)
    for i, f in enumerate(faculty_list):
        k = bc._name_loose_key(f.get("name", ""))
        loose_buckets[k].append(i)

    print("=" * 70)
    print("MEDIUM CONFIDENCE — same first+last name, Lineberger cross-listing")
    print("=" * 70)
    loose_candidates = []
    for k, indices in loose_buckets.items():
        keys = {index_to_key[i] for i in indices}
        if len(keys) < 2:
            continue
        has_lineberger = any(LINEBERGER in _depts(faculty_list[i]) for i in indices)
        if not has_lineberger:
            continue
        loose_candidates.append((k, indices))
        already_flagged.update(indices)

    if not loose_candidates:
        print("(none found)\n")
    for k, indices in loose_candidates:
        _print_group(indices, faculty_list, suggest_alias=True)

    # ---- Tier 3: same surname + first initial, not caught above ----
    def _surname_initial_key(name):
        key = bc._name_dedup_key(name)
        tokens = [t for t in key.split(" ") if t]
        if len(tokens) < 2:
            return None
        return f"{tokens[0][0]} {tokens[-1]}"

    initial_buckets = defaultdict(list)
    for i, f in enumerate(faculty_list):
        k = _surname_initial_key(f.get("name", ""))
        if k:
            initial_buckets[k].append(i)

    print("=" * 70)
    print("LOW CONFIDENCE — same surname + first initial, not already merged")
    print("(review each one individually -- these are often just different")
    print(" people, e.g. 'Sameer Prasada' vs 'Sudhir Prasada')")
    print("=" * 70)
    initial_candidates = []
    for k, indices in initial_buckets.items():
        # Skip entirely if every record in this bucket was already shown above
        if all(i in already_flagged for i in indices):
            continue
        keys = {index_to_key[i] for i in indices}
        if len(keys) < 2:
            continue
        seen_keys = set()
        rep_indices = []
        for i in indices:
            if index_to_key[i] not in seen_keys:
                seen_keys.add(index_to_key[i])
                rep_indices.append(i)
        if len(rep_indices) < 2:
            continue
        initial_candidates.append((k, rep_indices))

    if not initial_candidates:
        print("(none found)\n")
    for k, indices in initial_candidates:
        _print_group(indices, faculty_list, suggest_alias=False)

    print(f"\nSummary: {len(slug_candidates)} high-confidence, "
          f"{len(loose_candidates)} medium-confidence, "
          f"{len(initial_candidates)} low-confidence group(s) to review.")


if __name__ == "__main__":
    main()
