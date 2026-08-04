"""
Build "research nodes" — co-authorship collaborator lists for each faculty
member, based on shared publications already in faculty.json.

This is a pure local computation (no PubMed/ORCID API calls) that runs over
the already-enriched faculty.json and adds a `collaborators` field to each
person: their top 5 most-frequent co-authors *within our own database*
(i.e. other UNC faculty we've scraped and enriched), ranked by how many
shared publications appear in both people's publication lists.

It also adds a `student_coauthors` field (JBG Research Day medical-student
roster cross-reference) — see attach_student_coauthors() below. This piggybacks
on the same identity-dedup map as the faculty collaborator computation, so a
faculty member with leftover duplicate entries (e.g. "Clara Lee" and
"Clara Lee, ,") gets one consistent student-match count instead of it being
split (or double-counted) across the duplicates.

Scope note: this only surfaces collaborations between two people who are
both in faculty.json. It cannot show a UNC researcher's external
collaborators at other institutions, since we don't have their data.

Usage:
    python scraper/build_collaborators.py --input data/faculty.json --output data/faculty.json
    python scraper/build_collaborators.py --input data/faculty.json --output data/faculty.json \
        --students scraper/student_researchers.json
"""

import json
import os
import argparse
import re
import itertools
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


# Matches trailing credential letters sometimes appended to an author string
# (not PubMed's own AuthorList format, but defensive in case the student
# roster or a fallback source includes them), e.g. "James Hardy, BS"
_CREDENTIAL_SUFFIX = re.compile(
    r",?\s*\b(BS|BA|MD|PhD|MS|MPH|DO|PharmD|RN|NP)\b\.?$", re.IGNORECASE
)


def _student_match_key(name):
    """
    Normalize a name to a 'first last' matching key for student-roster
    cross-referencing: strip credential suffixes, accents, punctuation,
    drop middle names/initials, lowercase. Deliberately coarser than
    _name_dedup_key (which is for merging near-identical faculty name
    variants) — this needs to collapse "James Hardy" / "James R. Hardy" /
    "Hardy, James" down to the same key. Returns None if fewer than 2
    tokens remain (can't safely match on a single token).
    """
    if not name:
        return None
    name = _CREDENTIAL_SUFFIX.sub("", name)
    key = _name_dedup_key(name)
    tokens = [t for t in key.split(" ") if t]
    if len(tokens) < 2:
        return None
    return f"{tokens[0]} {tokens[-1]}"


def _is_initials_only(token):
    """True for bare-initial tokens like 'J' or 'J.' — too ambiguous to
    match against a student roster (PubMed's esummary fallback format is
    'Hardy J', not a full first name)."""
    return len(re.sub(r"\.", "", token)) <= 1


class _UnionFind:
    """Minimal union-find (disjoint set) with path compression, used to
    merge faculty_list indices that resolve to the same real person."""
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def build_identity_map(faculty_list, name_aliases=None):
    """
    Map every faculty_list index to a canonical identity key, and pick one
    representative index per canonical key (the one with the cleanest name)
    to use for display. This is what prevents leftover duplicate faculty
    entries (same person, slightly different name formatting) from showing
    up as separate nodes in the collaborator network.

    Matching, most to least trusted:
      0. Manual alias list (name_aliases), if provided — see
         load_name_aliases(). This is the ONLY mechanism for resolving
         same-initial name collisions like 'Joshua Zeidner' vs 'J Zeidner'.
         An earlier version tried to auto-detect these by requiring the two
         records to share a publication PMID, but that turned out to be
         unreliable in both directions: it produced false merges (two
         different colleagues who genuinely co-authored a paper together
         — the very relationship this feature is meant to surface — look
         identical to 'same person, two name spellings' under a shared-PMID
         test), AND it missed real duplicates whose own publication lists
         happened not to overlap (e.g. enriched via two different PubMed
         paths). There's no reliable automatic signal for this case, so it
         needs a human to confirm it once via the alias file.
      1. Same non-empty profile_url — the strongest automatic signal, two
         records pointing at the literal same profile page.
      2. Exact normalized name match (punctuation/accent/case differences).
      3. Loose first+last-name match, gated on the two records sharing at
         least one department in common (checking the full `departments`
         list, not just the primary `department` string — duplicate
         entries usually arise BECAUSE someone was scraped from two
         different department pages, e.g. their home department and
         Lineberger, so requiring the single primary department to match
         exactly blocks exactly the cases this tier exists to catch).

    Returns: (index_to_key, key_to_representative_index, dupes_found)
    """
    name_aliases = name_aliases or {}
    n = len(faculty_list)
    uf = _UnionFind(n)

    def _depts(f):
        ds = f.get("departments") or [f.get("department", "")]
        return set(d for d in ds if d)

    alias_buckets = defaultdict(list)
    exact_buckets = defaultdict(list)
    loose_buckets = defaultdict(list)
    url_buckets = defaultdict(list)

    for i, f in enumerate(faculty_list):
        name = f.get("name", "")
        url = (f.get("profile_url") or "").strip()
        exact_key = _name_dedup_key(name)

        # Every entry buckets under its resolved canonical name — defaulting
        # to its own name if it has no explicit alias. This matters because
        # the canonical spelling itself (e.g. 'Benjamin Garrett Vincent')
        # needs to land in the same bucket as its aliased short forms (e.g.
        # 'Benjamin Vincent' -> 'Benjamin Garrett Vincent') for the union to
        # actually connect them — only bucketing the aliased side leaves the
        # canonical-spelling record in a bucket of its own.
        alias_target = name_aliases.get(exact_key, name)
        alias_buckets[_name_dedup_key(alias_target)].append(i)

        exact_buckets[exact_key].append(i)
        loose_buckets[_name_loose_key(name)].append(i)
        if url:
            url_buckets[url].append(i)

    dupes_found = []

    def _merge_bucket(bucket, match_kind, require_dept_overlap=False):
        for indices in bucket.values():
            if len(indices) < 2:
                continue
            for a, b in itertools.combinations(indices, 2):
                if require_dept_overlap:
                    if not (_depts(faculty_list[a]) & _depts(faculty_list[b])):
                        continue
                if uf.find(a) != uf.find(b):
                    uf.union(a, b)
                    dupes_found.append((faculty_list[a]["name"], faculty_list[b]["name"], match_kind))

    _merge_bucket(alias_buckets, "manual alias")
    _merge_bucket(url_buckets, "profile_url")
    _merge_bucket(exact_buckets, "exact")
    _merge_bucket(loose_buckets, "loose (shared dept)", require_dept_overlap=True)

    # Pick a representative index per merged group. If any member of the
    # group has an explicit alias pointing at a canonical name, trust that
    # over the auto-picked "cleanest" name — the user told us what it
    # should be.
    group_to_indices = defaultdict(list)
    for i in range(n):
        group_to_indices[uf.find(i)].append(i)

    index_to_key = {}
    key_to_best_index = {}
    for root, indices in group_to_indices.items():
        alias_names = {
            name_aliases[_name_dedup_key(faculty_list[i]["name"])]
            for i in indices
            if _name_dedup_key(faculty_list[i]["name"]) in name_aliases
        }
        if alias_names:
            canonical_name = sorted(alias_names, key=len, reverse=True)[0]
            best = indices[0]
            key = _name_dedup_key(canonical_name) + f"#{root}"
            faculty_list[best]["_display_name_override"] = canonical_name
        else:
            best = max(indices, key=lambda i: _name_quality(faculty_list[i]["name"]))
            key = _name_dedup_key(faculty_list[best]["name"]) + f"#{best}"
        key_to_best_index[key] = best
        for i in indices:
            index_to_key[i] = key

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


def attach_collaborators(faculty_list, top_n=5, name_aliases=None):
    """
    Compute and attach a `collaborators` field to every faculty dict in place.
    Duplicate entries for the same real person (leftover from before the
    scrape-side dedup fix, e.g. 'Clara Lee' vs 'Clara Lee, ,') are collapsed
    onto one canonical identity before counting, so they can't show up as
    two separate nodes for the same collaborator in the network view.

    Returns (stats, index_to_key, key_to_best_index) — the identity map is
    handed back so attach_student_coauthors() can reuse it instead of
    recomputing (and potentially disagreeing on) faculty identity merges.
    """
    index_to_key, key_to_best_index, dupes_found = build_identity_map(faculty_list, name_aliases=name_aliases)
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
                "name": faculty_list[key_to_best_index[other_key]].get("_display_name_override")
                        or faculty_list[key_to_best_index[other_key]]["name"],
                "shared_papers": count,
            }
            for other_key, count in ranked
        ]
        with_collaborators += 1

    stats = {
        "total_faculty": len(faculty_list),
        "faculty_with_collaborators": with_collaborators,
        "shared_pmid_count": shared_pmid_count,
        "duplicate_identities_merged": len(dupes_found),
    }
    return stats, index_to_key, key_to_best_index


def merge_duplicate_faculty(faculty_list, index_to_key, key_to_best_index):
    """
    Collapse duplicate raw faculty entries (same real person, scraped from
    multiple department pages) into ONE record per canonical identity — this
    is what makes the frontend show one card per person instead of one card
    per department page they were scraped from.

    Must be called AFTER attach_collaborators()/attach_student_coauthors(),
    since those already compute the correct per-canonical-key results and
    write the SAME values onto every duplicate entry — this function just
    picks one of those (identical) values rather than recomputing anything.

    Per merged person:
      - departments: union of every department seen across their duplicate
        entries, in first-seen order
      - department_profiles: [{department, profile_url}] — each department
        paired with the specific profile URL it was scraped from, so the
        frontend can render a separate clickable link per department
        instead of collapsing to one URL (a real person can have a
        genuinely different bio page per department, e.g. a Hematology
        page and a separate Lineberger page)
      - publications: union across all duplicate entries, deduped by PMID
        (different name-variant PubMed searches can turn up slightly
        different but overlapping results — union is more complete, not
        just deduped)
      - pubmed_count / pubmed_verified: recomputed as len(merged
        publications) so the badge count always matches what's actually
        listed on the card, rather than carrying over one duplicate's
        possibly-stale count
      - collaborators / has_student_coauthor / student_coauthor_count /
        student_coauthors: copied as-is from any one duplicate (they're
        already identical across the group)

    Returns a new list of merged faculty dicts (does not mutate faculty_list).
    """
    groups = defaultdict(list)
    for i, key in index_to_key.items():
        groups[key].append(i)

    merged_list = []
    for key, indices in groups.items():
        best_i = key_to_best_index[key]
        best = faculty_list[best_i]

        departments = []
        dept_profiles = {}
        role = ""
        for i in indices:
            f = faculty_list[i]
            f_depts = f.get("departments") or [f.get("department", "")]
            f_depts = [d for d in f_depts if d]
            url = (f.get("profile_url") or "").strip()
            for d in f_depts:
                if d not in departments:
                    departments.append(d)
                if url and d not in dept_profiles:
                    dept_profiles[d] = url
            if not role and f.get("role"):
                role = f["role"]

        pubs_merged = []
        seen_pmids = set()
        for i in indices:
            for p in faculty_list[i].get("publications") or []:
                pmid = p.get("pmid")
                if pmid:
                    if pmid in seen_pmids:
                        continue
                    seen_pmids.add(pmid)
                pubs_merged.append(p)

        # At least one duplicate being "ambiguous" only still matters if the
        # merge didn't actually resolve any publications — once we have real
        # merged publications, the ambiguity concern is moot.
        any_ambiguous = any(faculty_list[i].get("pubmed_ambiguous") for i in indices)

        merged_list.append({
            "name": best.get("_display_name_override") or best["name"],
            "profile_url": best.get("profile_url", ""),
            "department": departments[0] if departments else "",
            "departments": departments,
            "department_profiles": [
                {"department": d, "profile_url": dept_profiles.get(d, "")}
                for d in departments
            ],
            "role": role,
            "pipeline_version": best.get("pipeline_version", ""),
            "pubmed_search": best.get("pubmed_search", ""),
            "pubmed_count": len(pubs_merged),
            "pubmed_verified": len(pubs_merged),
            "pubmed_ambiguous": any_ambiguous and len(pubs_merged) == 0,
            "publications": pubs_merged,
            "collaborators": best.get("collaborators", []),
            "has_student_coauthor": best.get("has_student_coauthor", False),
            "student_coauthor_count": best.get("student_coauthor_count", 0),
            "student_coauthors": best.get("student_coauthors", []),
        })

    return merged_list


def load_name_aliases(path):
    """
    Load a manual name-alias file: a JSON object mapping known duplicate
    name variants to one canonical display name, e.g.:

        {
          "j zeidner": "Joshua F. Zeidner",
          "joshua zeidner": "Joshua F. Zeidner",
          "benjamin vincent": "Benjamin Garrett Vincent"
        }

    Keys are matched after the same normalization used everywhere else
    (accents/punctuation/case stripped), so 'J. Zeidner', 'j zeidner', and
    'J Zeidner' all resolve the same way — you don't need an entry for
    every possible formatting variant, just one per name variant you've
    actually seen.

    This exists because same-initial name collisions (e.g. 'Joshua
    Zeidner' vs 'J Zeidner') can't be reliably resolved automatically —
    see build_identity_map()'s docstring for why. Returns {} if the file
    doesn't exist (this feature is opt-in, not required).
    """
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    return {_name_dedup_key(k): v for k, v in raw.items() if not k.startswith("_")}


def load_student_roster(path):
    """
    Load the JBG Research Day medical-student roster (a flat JSON list of
    display names, e.g. student_researchers.json). Returns a dict mapping
    _student_match_key(name) -> the cleanest display-name variant seen for
    that key, or {} if the file doesn't exist (this feature is opt-in).
    """
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    idx = {}
    for name in raw:
        key = _student_match_key(name)
        if not key:
            continue
        # Prefer the more complete-looking variant if two roster entries
        # collapse to the same key (e.g. "Adelaide Cooke" vs
        # "Adelaide Rosalie Cooke" both -> "adelaide cooke").
        if key not in idx or _name_quality(name) > _name_quality(idx[key]):
            idx[key] = name
    return idx


def attach_student_coauthors(faculty_list, student_idx, index_to_key, key_to_best_index):
    """
    Compute and attach `has_student_coauthor`, `student_coauthor_count`, and
    `student_coauthors` to every faculty dict in place, by cross-referencing
    each publication's `authors` list (requires scrape.py's author-list
    capture — publications enriched before that will simply have no
    `authors` field and contribute no matches) against student_idx.

    Reuses the same canonical-identity map as attach_collaborators() so a
    faculty member's publications spread across duplicate leftover entries
    are matched as one person, and every duplicate entry for that person
    ends up with the same (correct, non-double-counted) result — the same
    guarantee attach_collaborators() gives for the faculty-to-faculty graph.
    """
    # canonical_key -> {student_match_key: shared_paper_count}
    key_matches = defaultdict(lambda: defaultdict(int))
    # canonical_key -> set of PMIDs already counted, so a paper independently
    # found by TWO duplicate raw entries for the same real person (e.g. one
    # entry scraped from a Hematology page, one from Lineberger, each doing
    # its own PubMed search and both correctly turning up the same paper)
    # only increments the shared-paper count once, not once per duplicate
    # entry it happens to appear on.
    counted_pmids = defaultdict(set)
    skipped_ambiguous = 0
    faculty_missing_author_data = 0

    for i, f in enumerate(faculty_list):
        key = index_to_key[i]
        pubs = f.get("publications", []) or []
        if pubs and not any("authors" in p for p in pubs):
            faculty_missing_author_data += 1
        for pub in pubs:
            pmid = pub.get("pmid")
            has_student_match_in_this_pub = False
            for a in pub.get("authors", []) or []:
                first_tok = a.split()[0] if a.split() else ""
                if _is_initials_only(first_tok):
                    skipped_ambiguous += 1
                    continue
                mkey = _student_match_key(a)
                if mkey and mkey in student_idx:
                    if pmid and pmid in counted_pmids[key]:
                        # Already counted this exact paper for this person
                        # via a different duplicate entry — skip to avoid
                        # double-counting the same real publication.
                        continue
                    key_matches[key][mkey] += 1
                    has_student_match_in_this_pub = True
            if pmid and has_student_match_in_this_pub:
                counted_pmids[key].add(pmid)

    flagged = 0
    for i, f in enumerate(faculty_list):
        key = index_to_key[i]
        matches = key_matches.get(key)
        if matches:
            flagged += 1
            f["has_student_coauthor"] = True
            f["student_coauthor_count"] = len(matches)
            f["student_coauthors"] = [
                {"student": student_idx[mkey], "shared_papers": count}
                for mkey, count in sorted(matches.items(), key=lambda kv: -kv[1])
            ]
        else:
            f["has_student_coauthor"] = False
            f["student_coauthor_count"] = 0
            f["student_coauthors"] = []

    return {
        "faculty_flagged": flagged,
        "skipped_ambiguous_authors": skipped_ambiguous,
        "faculty_missing_author_data": faculty_missing_author_data,
    }


def trim_for_publishing(faculty_list, max_pubs=0, max_authors=0):
    """
    Shrink the published faculty.json without losing anything the site shows.

    Every visitor to the site downloads this file, so its size is page-load
    time. The uncapped file is ~13.6 MB (~3.6 MB gzipped), and the two things
    driving that are (a) prolific faculty carrying up to 238 publications and
    (b) consortium papers carrying up to 634 author names.

    BOTH caps default to off. This function exists as an escape hatch for if
    the payload ever becomes a real problem, not as normal behaviour — the
    default path here is a no-op.

    Capping authors was tried and reverted. Capping at 12 saved only ~0.55 MB
    gzipped, but 31% of publications have more than 12 authors, and on those
    papers the faculty member whose card you are reading was cut from their
    own author list 48% of the time. It also cut the LAST author on every
    long paper — the senior/PI position, which is the most informative slot
    on the page for a student sizing up a mentor. Medical students appear in
    the middle of author lists rather than at the ends, so the cap hid
    precisely the people this site exists to surface.

    Publications are likewise uncapped: the full verified list is the
    substance of what a student is looking for, and it backs the site's
    keyword search, so capping it would silently make older work
    unsearchable.

    When max_authors IS set, the extras are dropped and the true total is
    recorded as `n_authors` so the frontend can say how many are hidden
    rather than implying a short author list.

    IMPORTANT: call this AFTER attach_collaborators() and
    attach_student_coauthors(). Both read the complete publication and author
    lists, and the student cross-reference in particular needs every author of
    every paper — trimming first would silently drop student matches on
    long-author-list papers, which is exactly the kind of silent failure this
    pipeline has been bitten by before.

    `pubmed_verified` is deliberately NOT recalculated: it reports how many
    publications were verified for that person, which stays true even though
    only the most recent max_pubs are shipped to the browser.
    """
    pubs_dropped = 0
    authors_dropped = 0
    for f in faculty_list:
        pubs = f.get("publications") or []
        if len(pubs) > max_pubs:
            pubs_dropped += len(pubs) - max_pubs
            pubs = pubs[:max_pubs]
            f["publications"] = pubs
        for p in pubs:
            authors = p.get("authors") or []
            if len(authors) > max_authors:
                authors_dropped += len(authors) - max_authors
                p["n_authors"] = len(authors)
                p["authors"] = authors[:max_authors]
    return {"publications_trimmed": pubs_dropped, "authors_trimmed": authors_dropped}


def main():
    parser = argparse.ArgumentParser(description="Build research collaborator networks")
    parser.add_argument("--input", default="data/faculty.json")
    parser.add_argument("--output", default="data/faculty.json")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--aliases", default="scraper/name_aliases.json",
                         help="Manual name-alias file for resolving name collisions "
                              "that can't be detected automatically (see load_name_aliases)")
    parser.add_argument("--students", default=None,
                         help="Path to student_researchers.json (JBG Research Day roster). "
                              "If omitted, student co-author flagging is skipped entirely "
                              "and existing has_student_coauthor/student_coauthors fields "
                              "(if any) are left untouched.")
    parser.add_argument("--max-pubs", type=int, default=0,
                         help="Cap publications per faculty in the published JSON "
                              "(newest first). Default 0 = no cap: a faculty member's "
                              "full verified publication list is valuable to students "
                              "and also feeds the site's keyword search, so we ship all "
                              "of it. Set a number only if payload size becomes a problem.")
    parser.add_argument("--max-authors", type=int, default=0,
                         help="Cap authors per publication in the published JSON, "
                              "recording the true total as n_authors. Default 0 = "
                              "no cap; capping cuts the senior author and often the "
                              "faculty member themself. Set only if payload size "
                              "becomes a real problem.")
    parser.add_argument("--indent", type=int, default=0,
                         help="Pretty-print the output JSON with this indent. "
                              "Default 0 (compact) — the file is browser-read only "
                              "and indenting adds ~50%% to every visitor's download.")
    parser.add_argument("--keep-duplicates", action="store_true",
                         help="Skip merge_duplicate_faculty() and leave one raw entry per "
                              "scraped department page (old behavior) instead of collapsing "
                              "to one card per real person. Off by default.")
    args = parser.parse_args()

    with open(args.input) as f:
        data = json.load(f)

    faculty_list = data.get("faculty", [])
    print(f"Loaded {len(faculty_list)} faculty from {args.input}")

    name_aliases = load_name_aliases(args.aliases)
    if name_aliases:
        print(f"Loaded {len(name_aliases)} name alias(es) from {args.aliases}")

    stats, index_to_key, key_to_best_index = attach_collaborators(
        faculty_list, top_n=args.top_n, name_aliases=name_aliases
    )

    print(f"Shared publications found (2+ of our faculty as co-authors): {stats['shared_pmid_count']}")
    print(f"Faculty with at least one collaborator: {stats['faculty_with_collaborators']} / {stats['total_faculty']}")
    if stats["duplicate_identities_merged"]:
        print(f"Duplicate identities merged during collaborator counting: {stats['duplicate_identities_merged']}")

    if args.students:
        student_idx = load_student_roster(args.students)
        print(f"Loaded {len(student_idx)} student(s) from {args.students}")
        student_stats = attach_student_coauthors(faculty_list, student_idx, index_to_key, key_to_best_index)
        print(f"Faculty flagged with a matched student co-author: {student_stats['faculty_flagged']}")
        if student_stats["skipped_ambiguous_authors"]:
            print(f"Skipped {student_stats['skipped_ambiguous_authors']} initials-only author "
                  f"tokens (too ambiguous to match, e.g. 'Hardy J')")
        if student_stats["faculty_missing_author_data"]:
            print(f"WARNING: {student_stats['faculty_missing_author_data']} faculty had publications "
                  f"with no 'authors' field — re-run scrape.py's enrich step with the "
                  f"author-list capture (pipeline_version 2026.07.14-author-lists-v1 or later) "
                  f"before student matches can be found for them.")
    else:
        print("No --students roster given — skipping student co-author flagging "
              "(existing has_student_coauthor fields, if any, left as-is).")

    if args.keep_duplicates:
        print("--keep-duplicates set — leaving one raw entry per scraped department "
              "page (not collapsing to one card per real person).")
        # Drop the internal display-name-override scratch field before writing
        # — it's only used during this computation, not part of the schema.
        for f in faculty_list:
            f.pop("_display_name_override", None)
        data["faculty"] = faculty_list
        data["total_faculty"] = len(faculty_list)
    else:
        merged_list = merge_duplicate_faculty(faculty_list, index_to_key, key_to_best_index)
        print(f"Merged {len(faculty_list)} raw scraped entries into "
              f"{len(merged_list)} unique faculty records "
              f"({len(faculty_list) - len(merged_list)} duplicate entries collapsed).")
        data["faculty"] = merged_list
        data["total_faculty"] = len(merged_list)

    # Trim last — collaborator counting and student matching above both need
    # the complete publication and author lists.
    if args.max_pubs or args.max_authors:
        trim_stats = trim_for_publishing(
            data["faculty"],
            max_pubs=args.max_pubs or 10**9,
            max_authors=args.max_authors or 10**9,
        )
        print(f"Trimmed for publishing: dropped {trim_stats['publications_trimmed']} "
              f"older publication(s) beyond --max-pubs={args.max_pubs} and "
              f"{trim_stats['authors_trimmed']} author name(s) beyond "
              f"--max-authors={args.max_authors} (true totals preserved as n_authors)")

    # Written compact, not indent=2. This file is only ever read by the
    # browser, and pretty-printing 2,666 records adds roughly 50% pure
    # whitespace to something every visitor downloads (and that gets
    # committed to git every week). Use --indent 2 if you need to eyeball it.
    with open(args.output, "w") as f:
        if args.indent:
            json.dump(data, f, indent=args.indent)
        else:
            json.dump(data, f, separators=(",", ":"))
    size_mb = os.path.getsize(args.output) / 1e6
    print(f"Written to {args.output} ({size_mb:.1f} MB)")
    # Uncompressed size. GitHub Pages serves this gzipped, which cuts it by
    # roughly 70%, so the number that matters to a visitor is ~0.3x this.
    # Threshold is set well above the normal uncapped size (~13 MB) so it
    # only fires if something has genuinely gotten out of hand.
    if size_mb > 25:
        print(f"⚠ {args.output} is {size_mb:.1f} MB uncompressed — every site visitor "
              f"downloads this file. Consider --max-pubs / --max-authors.")


if __name__ == "__main__":
    main()
