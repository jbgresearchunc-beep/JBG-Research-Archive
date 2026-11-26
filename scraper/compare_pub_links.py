"""
Compare publication-to-faculty linking between two faculty JSON files —
typically production (data/faculty.json) vs a test run (data/faculty_test.json)
after changing verification logic. Read-only.

    python scraper/compare_pub_links.py                       # defaults below
    python scraper/compare_pub_links.py OLD.json NEW.json --top 30

Only faculty present in BOTH files are compared (a department-filtered test
run covers one department). Prints a summary, then the faculty whose lists
changed most, with the titles of removed/added papers so you can spot-check.
"""
import argparse
import json
import sys


def load(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    fac = data if isinstance(data, list) else data.get("faculty", [])
    return {f["name"].lower().strip(): f for f in fac}


def pubs_by_pmid(f):
    return {p.get("pmid"): p for p in (f.get("publications") or []) if p.get("pmid")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old", nargs="?", default="data/faculty.json")
    ap.add_argument("new", nargs="?", default="data/faculty_test.json")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    old, new = load(args.old), load(args.new)
    common = sorted(set(old) & set(new))
    if not common:
        sys.exit("No faculty in common between the two files.")

    rows = []
    tot_old = tot_new = tot_removed = tot_added = 0
    newly_hidden = newly_shown = orcid = 0
    reasons = {}
    for key in common:
        o, n = old[key], new[key]
        po, pn = pubs_by_pmid(o), pubs_by_pmid(n)
        removed = [po[k] for k in po.keys() - pn.keys()]
        added = [pn[k] for k in pn.keys() - po.keys()]
        tot_old += len(po)
        tot_new += len(pn)
        tot_removed += len(removed)
        tot_added += len(added)
        if po and not pn:
            newly_hidden += 1
        if pn and not po:
            newly_shown += 1
        if n.get("pubmed_orcid"):
            orcid += 1
        if n.get("pubmed_flag_reason"):
            reasons[n["pubmed_flag_reason"]] = reasons.get(n["pubmed_flag_reason"], 0) + 1
        if removed or added:
            rows.append((len(removed) + len(added), n, o, removed, added))

    print(f"Compared {len(common)} faculty  ({args.old} → {args.new})")
    print(f"  Publications shown:   {tot_old} → {tot_new}  "
          f"(removed {tot_removed}, added {tot_added})")
    print(f"  Faculty with changed lists: {len(rows)}")
    print(f"  Lists now hidden (had papers, now flagged/empty): {newly_hidden}")
    print(f"  Lists now shown (had none before):               {newly_shown}")
    print(f"  Faculty with an ORCID iD learned:                {orcid}")
    if reasons:
        print("  Flag reasons:")
        for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {c:4}  {r}")

    rows.sort(key=lambda r: -r[0])
    print(f"\nTop {min(args.top, len(rows))} changes — spot-check these:\n")
    for _, n, o, removed, added in rows[:args.top]:
        print(f"■ {n['name']}  [{(n.get('department') or '')}]  "
              f"{len(pubs_by_pmid(o))} → {len(pubs_by_pmid(n))}  "
              f"via {n.get('pubmed_search', '')}"
              + (f"  ⚠ {n['pubmed_flag_reason']}" if n.get("pubmed_flag_reason") else ""))
        for label, items in (("−", removed), ("+", added)):
            for p in sorted(items, key=lambda p: p.get("year") or "", reverse=True)[:5]:
                print(f"    {label} {p.get('year', '')} PMID {p.get('pmid')}: "
                      f"{(p.get('title') or '')[:90]}")
            if len(items) > 5:
                print(f"    {label} … and {len(items) - 5} more")
        print()


if __name__ == "__main__":
    main()
