"""
Hand-checked evaluation of publication-to-faculty linking.

Three steps:

  1. sample  — pick the faculty to hand-check (once; saved to scraper/eval/sample.json)
       python scraper/eval_links.py sample data/faculty.json

  2. build   — write/extend the labeling sheet scraper/eval/labels.csv with the
               candidate papers from one or more pipeline outputs. Existing labels
               are kept; only new (faculty, PMID) rows are appended.
       python scraper/eval_links.py build data/faculty.json data/faculty_test.json

     Open labels.csv in Excel/Sheets and fill the `label` column:
         Y = this paper is by this faculty member
         N = it's by someone else
         ? = can't tell (ignored in scoring)
     Title + journal usually settle it; the PubMed link shows affiliations.

  3. score   — precision / recall of each output against the labels
       python scraper/eval_links.py score data/faculty.json data/faculty_test.json

Rows are included per faculty member as: a sample of the papers the outputs
DISAGREE on (up to DISAGREE_CAP, balanced across sides), plus up to
AGREED_SAMPLE papers that all outputs show. Disagreements are what decide
which version is better; the agreed sample estimates absolute precision.
Rerunning build after a new pipeline run adds rows for new disagreements.

Recall here is relative: the share of papers labeled Y (from any version)
that a given output shows. Papers no version found can't be counted.
"""
import argparse
import csv
import json
import os
import random
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_DIR = os.path.join(HERE, "eval")
SAMPLE_PATH = os.path.join(EVAL_DIR, "sample.json")
LABELS_PATH = os.path.join(EVAL_DIR, "labels.csv")

N_RANDOM = 50
N_HARD = 20
AGREED_SAMPLE = 4      # papers every output shows, per faculty member
DISAGREE_CAP = 10      # papers the outputs disagree on, per faculty member
SINGLE_SAMPLE = 4      # per faculty member covered by only one output so far
SEED = 20261125

# Known hard cases from the Radiology test run (Nov 2026) — always included
# when present in the data.
HARD_NAMED = [
    "Li Wang", "Gang Li", "Yuli Jiang", "Cherie M. Kuzmiak", "Carolina Guimaraes",
    "Yoon Seong Choi", "Mingxia Liu", "Emily J. Werder", "Hong Yuan", "Zhengwang Wu",
    "Dan Hu",
]
COMMON_SURNAMES = {
    "smith", "lee", "kim", "wang", "chen", "johnson", "brown", "jones", "taylor",
    "williams", "wilson", "moore", "harris", "martin", "thompson", "garcia", "martinez",
    "anderson", "clark", "lewis", "robinson", "walker", "young", "hall", "allen", "wright",
    "scott", "hill", "green", "adams", "baker", "nelson", "carter", "mitchell", "perez",
    "roberts", "turner", "phillips", "campbell", "parker", "evans", "edwards", "zhang",
    "liu", "li", "wu", "patel", "nguyen",
}
FIELDS = ["faculty", "department", "pmid", "year", "journal", "title", "authors",
          "pubmed_url", "shown_by", "label", "notes"]


def load_faculty(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    fac = data if isinstance(data, list) else data.get("faculty", [])
    return {f["name"].lower().strip(): f for f in fac}


def label_name(path):
    name = os.path.splitext(os.path.basename(path))[0]
    return {"faculty": "production", "faculty_test": "test"}.get(name, name)


def surname(name):
    name = re.sub(r",.*$", "", name)
    name = re.sub(r"\s+(jr|sr|ii|iii|iv)\.?$", "", name, flags=re.I)
    toks = name.split()
    return toks[-1].strip(".").lower() if toks else ""


def _ascii(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def short_authors(authors, faculty_name):
    """First 3 … last 2, plus the author(s) sharing the faculty surname, marked **."""
    sn = surname(_ascii(faculty_name))
    marked, keep = [], set(range(min(3, len(authors)))) | set(range(max(0, len(authors) - 2), len(authors)))
    for i, a in enumerate(authors):
        if sn and sn in _ascii(a).lower().split():
            keep.add(i)
    last = -1
    for i in sorted(keep):
        if last >= 0 and i > last + 1:
            marked.append("…")
        a = authors[i]
        if sn and sn in _ascii(a).lower().split():
            a = f"**{a}** ({i + 1}/{len(authors)})"
        marked.append(a)
        last = i
    return "; ".join(marked)


def cmd_sample(args):
    fac = load_faculty(args.production)
    keys = sorted(fac)
    rng = random.Random(SEED)
    with_pubs = [k for k in keys if fac[k].get("publications")]
    chosen = {}
    for name in HARD_NAMED:
        k = name.lower()
        if k in fac:
            chosen[k] = "hard: Radiology test case"
    hard_pool = [k for k in keys if k not in chosen and
                 (len(surname(fac[k]["name"])) <= 2 or surname(fac[k]["name"]) in COMMON_SURNAMES)]
    rng.shuffle(hard_pool)
    for k in hard_pool:
        if sum(1 for v in chosen.values() if v.startswith("hard")) >= N_HARD:
            break
        chosen[k] = "hard: common or two-letter surname"
    pool = [k for k in with_pubs if k not in chosen]
    rng.shuffle(pool)
    for k in pool[:N_RANDOM]:
        chosen[k] = "random"
    os.makedirs(EVAL_DIR, exist_ok=True)
    out = [{"name": fac[k]["name"], "department": fac[k].get("department", ""), "why": why}
           for k, why in chosen.items()]
    with open(SAMPLE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(out)} faculty to {SAMPLE_PATH} "
          f"({sum(1 for o in out if o['why'] == 'random')} random, "
          f"{sum(1 for o in out if o['why'] != 'random')} hard)")


def read_labels():
    if not os.path.exists(LABELS_PATH):
        return []
    # Excel on Windows may re-save the CSV as cp1252 instead of UTF-8.
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(LABELS_PATH, encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    sys.exit(f"Couldn't read {LABELS_PATH} — save it as CSV UTF-8.")


def cmd_build(args):
    with open(SAMPLE_PATH, encoding="utf-8") as f:
        sample = json.load(f)
    outputs = [(label_name(p), load_faculty(p)) for p in args.outputs]
    rows = read_labels()
    have = {(r["faculty"].lower(), r["pmid"]) for r in rows}
    rng = random.Random(SEED)
    added = 0
    for s in sample:
        key = s["name"].lower()
        shown = {}   # pmid -> set of output labels
        info = {}    # pmid -> pub dict
        present = [lbl for lbl, fac in outputs if key in fac]
        for lbl, fac in outputs:
            f = fac.get(key)
            if not f:
                continue
            for p in f.get("publications") or []:
                if p.get("pmid"):
                    shown.setdefault(p["pmid"], set()).add(lbl)
                    info.setdefault(p["pmid"], p)
        agreed = [m for m, by in shown.items() if len(by) == len(present)]
        disagree = [m for m, by in shown.items() if len(by) < len(present)]
        if len(present) == 1:
            # Only one version covers this person so far: sample its papers.
            pick = sorted(agreed)
            rng.shuffle(pick)
            pick = pick[:SINGLE_SAMPLE]
        else:
            # Balance the disagreement sample across "only in A" / "only in B".
            groups = {}
            for m in sorted(disagree):
                groups.setdefault(frozenset(shown[m]), []).append(m)
            per_group = max(1, DISAGREE_CAP // max(1, len(groups)))
            dpick = []
            for g in groups.values():
                rng.shuffle(g)
                dpick += g[:per_group]
            apick = sorted(agreed)
            rng.shuffle(apick)
            pick = dpick + apick[:AGREED_SAMPLE]
        for m in pick:
            if (key, m) in have:
                continue
            p = info[m]
            rows.append({
                "faculty": s["name"], "department": s["department"], "pmid": m,
                "year": p.get("year", ""), "journal": p.get("journal", ""),
                "title": p.get("title", ""),
                "authors": short_authors(p.get("authors") or [], s["name"]),
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{m}/",
                "shown_by": "", "label": "", "notes": "",
            })
            have.add((key, m))
            added += 1
    # Refresh shown_by for every row against the outputs given now
    for r in rows:
        key = r["faculty"].lower()
        by = [lbl for lbl, fac in outputs if key in fac and
              any(p.get("pmid") == r["pmid"] for p in fac[key].get("publications") or [])]
        r["shown_by"] = ";".join(by)
    rows.sort(key=lambda r: (r["faculty"].lower(), r["label"] != "", r["pmid"]))
    os.makedirs(EVAL_DIR, exist_ok=True)
    with open(LABELS_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    todo = sum(1 for r in rows if not r["label"].strip())
    print(f"{LABELS_PATH}: {len(rows)} rows ({added} new, {todo} still to label)")


def cmd_score(args):
    rows = [r for r in read_labels() if r["label"].strip().upper() in ("Y", "N")]
    if not rows:
        sys.exit("No Y/N labels yet in labels.csv.")
    labels = {(r["faculty"].lower(), r["pmid"]): r["label"].strip().upper() == "Y" for r in rows}
    true_by_fac = {}
    for (fk, m), ok in labels.items():
        if ok:
            true_by_fac.setdefault(fk, set()).add(m)
    labeled_fac = {fk for fk, _ in labels}
    loaded = [(path, load_faculty(path)) for path in args.outputs]
    if not args.all_faculty:
        # Same faculty for every output, so the numbers are comparable.
        for _, fac in loaded:
            labeled_fac &= set(fac)

    print(f"{len(labels)} labeled papers; scoring {len(labeled_fac)} faculty "
          f"{'(each output on its own faculty)' if args.all_faculty else 'present in every output'}\n")
    print(f"{'output':28} {'faculty':>7} {'shown':>6} {'correct':>8} {'wrong':>6} "
          f"{'precision':>10} {'recall':>7}")
    for path, fac in loaded:
        n_fac = shown_l = correct = wrong = true_total = true_shown = 0
        wrong_rows = []
        for fk in labeled_fac:
            f = fac.get(fk)
            if not f:
                continue
            n_fac += 1
            shown = {p.get("pmid") for p in f.get("publications") or []}
            for m in shown:
                if (fk, m) in labels:
                    shown_l += 1
                    if labels[(fk, m)]:
                        correct += 1
                    else:
                        wrong += 1
                        wrong_rows.append((f["name"], m))
            t = true_by_fac.get(fk, set())
            true_total += len(t)
            true_shown += len(t & shown)
        prec = correct / shown_l if shown_l else float("nan")
        rec = true_shown / true_total if true_total else float("nan")
        print(f"{label_name(path):28} {n_fac:7} {shown_l:6} {correct:8} {wrong:6} "
              f"{prec:10.1%} {rec:7.1%}")
        if args.show_wrong and wrong_rows:
            for name, m in wrong_rows:
                print(f"      wrong: {name}  PMID {m}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("sample")
    a.add_argument("production")
    b = sub.add_parser("build")
    b.add_argument("outputs", nargs="+")
    c = sub.add_parser("score")
    c.add_argument("outputs", nargs="+")
    c.add_argument("--show-wrong", action="store_true", help="list wrong papers per output")
    c.add_argument("--all-faculty", action="store_true",
                   help="score each output on all labeled faculty it contains "
                        "(default: only faculty present in every output)")
    args = ap.parse_args()
    {"sample": cmd_sample, "build": cmd_build, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    main()
