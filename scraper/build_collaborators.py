"""
Build "research nodes" — co-authorship collaborator lists for each faculty
member, based on shared publications already in faculty.json.

This is a pure local computation (no PubMed/ORCID API calls) that runs over
the already-enriched faculty.json and adds a `collaborators` field to each
person: their top 5 most-frequent co-authors *within our own database*
(i.e. other UNC faculty we've scraped and enriched), ranked by how many
shared publications appear in both people's publication lists.

Scope note: this only surfaces collaborations between two people who are
both in faculty.json. It cannot show a UNC researcher's external
collaborators at other institutions, since we don't have their data.

Usage:
    python scraper/build_collaborators.py --input data/faculty.json --output data/faculty.json
"""

import json
import argparse
import re
import unicodedata
from collections import defaultdict, Counter


def _name_dedup_key(name):
    """
    Normalize a name for identity matching: strip accents, lowercase,
    remove periods/commas/quote marks (straight and curly), collapse
    whitespace. Mirrors scraper/scrape.py's _name_dedup_key so the two
    stay consistent — this is what lets us recognize 'Clara Lee' and
    'Clara Lee, ,' as the same person even if faculty.json still has both
    as separate entries from before the scrape-side dedup fix.
    """
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"[\u2018\u2019\u201c\u201d'\".,]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _name_quality(name):
    """Prefer the cleanest, most complete name variant for display —
    more tokens (e.g. keeps a middle initial) beats a shorter/junkier
    string, and raw punctuation junk doesn't win just by being longer."""
    cleaned = re.sub(r"[^\w\s\-]", "", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    tokens = [t for t in cleaned.split() if t]
    return (len(tokens), len(cleaned))


def _name_loose_key(name):
    """
    Looser key: first token + last token only, dropping any middle
    initials/names. Catches same-person variants where a middle initial
    was inconsistently scraped, e.g. 'Culley Carson' vs 'Culley C. Carson'.
    Only used paired with a department match (see build_identity_map) —
    first+last alone isn't unique enough for common names.
    """
    key = _name_dedup_key(name)
    tokens = [t for t in key.split(" ") if t]
    if len(tokens) < 2:
        return key
    return f"{tokens[0]} {tokens[-1]}"


def build_identity_map(faculty_list):
    """
    Map every faculty_list index to a canonical identity key, and pick one
    representative index per canonical key (the one with the cleanest name)
    to use for display. This is what prevents leftover duplicate faculty
    entries (same person, slightly different name formatting) from showing
    up as separate nodes in the collaborator network.

    Two-tier matching, same logic as the scrape-side dedup fix:
      1. Exact normalized name match (punctuation/accent/case differences)
      2. Loose first+last-name match, gated on matching department (catches
         middle-initial variants without risking merging two different
         real people who share a first+last name in different departments)

    Returns: (index_to_key, key_to_representative_index, dupes_found)
    """
    index_to_key = {}
    key_to_best_index = {}      # exact key -> index
    loose_to_key = {}           # (loose key, department) -> exact key it resolved to
    dupes_found = []

    for i, f in enumerate(faculty_list):
        name = f.get("name", "")
        dept = f.get("department", "")
        exact_key = _name_dedup_key(name)
        loose_key = (_name_loose_key(name), dept)

        if exact_key in key_to_best_index:
            resolved_key = exact_key
            match_kind = "exact"
        elif loose_key in loose_to_key:
            resolved_key = loose_to_key[loose_key]
            match_kind = "loose (same dept)"
        else:
            resolved_key = exact_key
            match_kind = None

        index_to_key[i] = resolved_key

        if match_kind is None:
            key_to_best_index[resolved_key] = i
            loose_to_key[loose_key] = resolved_key
        else:
            existing_i = key_to_best_index[resolved_key]
            if _name_quality(name) > _name_quality(faculty_list[existing_i]["name"]):
                key_to_best_index[resolved_key] = i
            dupes_found.append((faculty_list[existing_i]["name"], name, match_kind))

    return index_to_key, key_to_best_index, dupes_found


def build_collaborator_index(faculty_list, index_to_key):
    """
    Build a PMID -> set of canonical identity keys reverse index (using
    keys, not raw indices, so duplicate entries for the same real person
    collapse into a single appearance per paper), then derive pairwise
    shared-publication counts between canonical identities.

    Returns: dict of canonical_key -> Counter({other_canonical_key: shared_count})
    """
    pmid_to_keys = defaultdict(set)

    for i, f in enumerate(faculty_list):
        key = index_to_key[i]
        for pub in f.get("publications", []) or []:
            pmid = pub.get("pmid")
            if pmid:
                pmid_to_keys[pmid].add(key)

    # For each PMID shared by 2+ distinct people in our database, increment
    # a pairwise counter for every pair of co-authors on that paper.
    pair_counts = defaultdict(lambda: defaultdict(int))

    shared_pmid_count = 0
    for pmid, keys in pmid_to_keys.items():
        if len(keys) < 2:
            continue
        shared_pmid_count += 1
        # NOTE: no minimum-shared-papers threshold and no down-weighting for
        # large multi-author consortium papers yet — deliberately simple for
        # this first pass. If a paper with many UNC co-authors turns out to
        # dominate everyone's top-5 list, the fix is to weight each paper's
        # contribution by 1/len(keys) (or cap len(keys) per paper) before
        # incrementing — see the commented alternative below.
        for a in keys:
            for b in keys:
                if a != b:
                    pair_counts[a][b] += 1
                    # Alternative down-weighted version (not used yet):
                    # pair_counts[a][b] += 1.0 / (len(keys) - 1)

    return pair_counts, shared_pmid_count


def attach_collaborators(faculty_list, top_n=5):
    """
    Compute and attach a `collaborators` field to every faculty dict in place.
    Duplicate entries for the same real person (leftover from before the
    scrape-side dedup fix, e.g. 'Clara Lee' vs 'Clara Lee, ,') are collapsed
    onto one canonical identity before counting, so they can't show up as
    two separate nodes for the same collaborator in the network view.
    """
    index_to_key, key_to_best_index, dupes_found = build_identity_map(faculty_list)
    if dupes_found:
        print(f"  Found {len(dupes_found)} duplicate faculty entr{'y' if len(dupes_found)==1 else 'ies'} "
              f"(same person, different name formatting) — merging for collaborator counting:")
        for old, new, kind in dupes_found[:20]:
            print(f"    [{kind}] {old!r} / {new!r}")
        if len(dupes_found) > 20:
            print(f"    ... and {len(dupes_found) - 20} more")

    pair_counts, shared_pmid_count = build_collaborator_index(faculty_list, index_to_key)

    with_collaborators = 0
    for i, f in enumerate(faculty_list):
        key = index_to_key[i]
        counts = pair_counts.get(key)
        if not counts:
            f["collaborators"] = []
            continue

        # Sort by shared paper count descending, take top N
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
        f["collaborators"] = [
            {
                "name": faculty_list[key_to_best_index[other_key]]["name"],
                "shared_papers": count,
            }
            for other_key, count in ranked
        ]
        with_collaborators += 1

    return {
        "total_faculty": len(faculty_list),
        "faculty_with_collaborators": with_collaborators,
        "shared_pmid_count": shared_pmid_count,
        "duplicate_identities_merged": len(dupes_found),
    }


def main():
    parser = argparse.ArgumentParser(description="Build research collaborator networks")
    parser.add_argument("--input", default="data/faculty.json")
    parser.add_argument("--output", default="data/faculty.json")
    parser.add_argument("--top-n", type=int, default=5)
    args = parser.parse_args()

    with open(args.input) as f:
        data = json.load(f)

    faculty_list = data.get("faculty", [])
    print(f"Loaded {len(faculty_list)} faculty from {args.input}")

    stats = attach_collaborators(faculty_list, top_n=args.top_n)

    print(f"Shared publications found (2+ of our faculty as co-authors): {stats['shared_pmid_count']}")
    print(f"Faculty with at least one collaborator: {stats['faculty_with_collaborators']} / {stats['total_faculty']}")
    if stats["duplicate_identities_merged"]:
        print(f"Duplicate identities merged during collaborator counting: {stats['duplicate_identities_merged']}")

    with open(args.output, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Written to {args.output}")


if __name__ == "__main__":
    main()
