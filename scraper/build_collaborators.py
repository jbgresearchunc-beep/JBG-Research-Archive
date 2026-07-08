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
from collections import defaultdict, Counter


def build_collaborator_index(faculty_list):
    """
    Build a PMID -> [faculty indices] reverse index, then derive pairwise
    shared-publication counts from it.

    Returns: dict of faculty_index -> Counter({other_faculty_index: shared_count})
    """
    pmid_to_indices = defaultdict(list)

    for i, f in enumerate(faculty_list):
        for pub in f.get("publications", []) or []:
            pmid = pub.get("pmid")
            if pmid:
                pmid_to_indices[pmid].append(i)

    # For each PMID shared by 2+ of our faculty, increment a pairwise counter
    # for every pair of co-authors on that paper.
    pair_counts = defaultdict(lambda: defaultdict(int))

    shared_pmid_count = 0
    for pmid, indices in pmid_to_indices.items():
        if len(indices) < 2:
            continue
        shared_pmid_count += 1
        # NOTE: no minimum-shared-papers threshold and no down-weighting for
        # large multi-author consortium papers yet — deliberately simple for
        # this first pass. If a paper with many UNC co-authors turns out to
        # dominate everyone's top-5 list, the fix is to weight each paper's
        # contribution by 1/len(indices) (or cap len(indices) per paper)
        # before incrementing — see the commented alternative below.
        for a in indices:
            for b in indices:
                if a != b:
                    pair_counts[a][b] += 1
                    # Alternative down-weighted version (not used yet):
                    # pair_counts[a][b] += 1.0 / (len(indices) - 1)

    return pair_counts, shared_pmid_count


def attach_collaborators(faculty_list, top_n=5):
    """
    Compute and attach a `collaborators` field to every faculty dict in place.
    """
    pair_counts, shared_pmid_count = build_collaborator_index(faculty_list)

    with_collaborators = 0
    for i, f in enumerate(faculty_list):
        counts = pair_counts.get(i)
        if not counts:
            f["collaborators"] = []
            continue

        # Sort by shared paper count descending, take top N
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
        f["collaborators"] = [
            {
                "name": faculty_list[j]["name"],
                "shared_papers": count,
            }
            for j, count in ranked
        ]
        with_collaborators += 1

    return {
        "total_faculty": len(faculty_list),
        "faculty_with_collaborators": with_collaborators,
        "shared_pmid_count": shared_pmid_count,
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

    with open(args.output, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Written to {args.output}")


if __name__ == "__main__":
    main()
