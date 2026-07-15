"""
Diagnostic (read-only): scan data/faculty.json for likely duplicate-person
entries that the automatic matching in build_collaborators.py did NOT
already merge, so you can review them and add confirmed cases to
scraper/name_aliases.json.

This does NOT modify faculty.json or auto-merge anything -- it only prints
a report. Two signals are used, most to least trustworthy:

  1. PROFILE URL SLUG MATCH (high confidence)
     Two records whose profile_url ends in the same slug, e.g.:
       .../medicine/hematology/people/benjamin-vincent/
       .../directory/benjamin-vincent/
     Different domain, different department, same slug -- this is what
     caught the real Benjamin Vincent / Benjamin Garrett Vincent case.
     UNC's own systems use the same slug for the same person across
     department and Lineberger listings, so this is strong evidence.

  2. SAME SURNAME + SAME FIRST INITIAL (low confidence, needs your judgment)
     Flagged for review only -- this is exactly the class of case that
     produced false positives before (e.g. Sameer Prasada / Sudhir
     Prasada are different people). Don't add these to the alias file
     without actually checking -- e.g. by looking up both profile pages.

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


def _url_slug(url):
    """Last non-empty path segment of a profile URL, lowercased."""
    if not url:
        return None
    parts = [p for p in url.rstrip("/").split("/") if p]
    return parts[-1].lower() if parts else None


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

    # ---- Signal 1: profile URL slug match across different identities ----
    slug_buckets = defaultdict(list)
    for i, f in enumerate(faculty_list):
        slug = _url_slug(f.get("profile_url"))
        if slug:
            slug_buckets[slug].append(i)

    print("=" * 70)
    print("HIGH CONFIDENCE — same profile URL slug, not already merged")
    print("=" * 70)
    slug_candidates = []
    for slug, indices in slug_buckets.items():
        keys = {index_to_key[i] for i in indices}
        if len(keys) < 2:
            continue  # already resolved to one identity, or only one record
        slug_candidates.append((slug, indices))

    if not slug_candidates:
        print("(none found)\n")
    for slug, indices in slug_candidates:
        names = [faculty_list[i]["name"] for i in indices]
        print(f"  slug: {slug!r}")
        for i in indices:
            f = faculty_list[i]
            print(f"    {f['name']!r:35s} | {f.get('department')!r:45s} | {f.get('profile_url')}")
        # Suggest the longest/most complete name as canonical
        canonical = max(names, key=lambda n: len(re.sub(r'[^\w\s]', '', n).split()))
        print(f"  Suggested alias entries (add to name_aliases.json):")
        for n in names:
            if n != canonical:
                print(f'    "{bc._name_dedup_key(n)}": "{canonical}",')
        print()

    # ---- Signal 2: same surname + first initial, not already merged ----
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
        keys = {index_to_key[i] for i in indices}
        if len(keys) < 2:
            continue
        # de-dup by identity (only show one representative per already-merged group)
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
        for i in indices:
            f = faculty_list[i]
            print(f"    {f['name']!r:35s} | {f.get('department')!r:45s} | {f.get('profile_url')}")
        print()

    print(f"\nSummary: {len(slug_candidates)} high-confidence group(s), "
          f"{len(initial_candidates)} low-confidence group(s) to review.")


if __name__ == "__main__":
    main()
