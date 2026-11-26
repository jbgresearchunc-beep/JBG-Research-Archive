"""
Offline tests for PubMed author verification in scrape.py.
Run:  python scraper/test_author_verification.py
No network needed — builds PubMed-style XML in memory.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape as s  # noqa: E402


def author(last, fore, initials, affs=(), orcid=None):
    aff_xml = "".join(f"<AffiliationInfo><Affiliation>{a}</Affiliation></AffiliationInfo>" for a in affs)
    orcid_xml = f'<Identifier Source="ORCID">{orcid}</Identifier>' if orcid else ""
    return (f'<Author ValidYN="Y"><LastName>{last}</LastName><ForeName>{fore}</ForeName>'
            f"<Initials>{initials}</Initials>{orcid_xml}{aff_xml}</Author>")


def article(pmid, *authors, title="A paper", article_affs=()):
    art_aff = "".join(f"<Affiliation>{a}</Affiliation>" for a in article_affs)
    return (f'<PubmedArticle><MedlineCitation><PMID Version="1">{pmid}</PMID><Article>'
            f"<Journal><Title>J</Title><ISOAbbreviation>J</ISOAbbreviation>"
            f"<JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal>"
            f"<ArticleTitle>{title}</ArticleTitle>"
            f'<AuthorList CompleteYN="Y">{"".join(authors)}</AuthorList>{art_aff}'
            f"</Article></MedlineCitation></PubmedArticle>")


UNC = "Department of Medicine, University of North Carolina at Chapel Hill, Chapel Hill, NC, USA."
DUKE = "Department of Medicine, Duke University School of Medicine, Durham, North Carolina, USA."
OTHER = "Dana-Farber Cancer Institute, Boston, MA, USA."
ORCID_A = "https://orcid.org/0000-0002-1825-0097"
ORCID_B = "0000-0001-5109-3700"


def run(name, arts, **kw):
    identity = s.build_author_identity(name, kw.pop("search_term", ""))
    records = [s._evaluate_article(a, identity) for a in arts]
    pubs, info = s.select_verified_publications(records, identity, **kw)
    return sorted(p["pmid"] for p in pubs), info


FAILS = []


def check(label, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"\n     got  {got}\n     want {want}"))
    if not ok:
        FAILS.append(label)


# ── Affiliation matching ──────────────────────────────────────────────────
AFFS = {
    UNC: True,
    DUKE: False,
    "Wake Forest School of Medicine, Winston-Salem, North Carolina": False,
    "RTI International, Research Triangle Park, North Carolina": False,
    "Department of Functional Genomics, Karolinska Institutet": False,
    "UNC Charlotte, Charlotte, NC": False,
    "University of North Carolina at Greensboro, Greensboro, NC": False,
    "University of North Carolina, Wilmington": False,
    "Universidad Nacional de Córdoba (UNC), Córdoba, Argentina": False,
    "North Carolina State University, Raleigh, NC": False,
    "Lineberger Comprehensive Cancer Center, Chapel Hill": True,
    "UNC School of Medicine": True,
    "UNC Health Rex, Raleigh, NC": True,
    "Gillings School of Global Public Health": True,
    "Univ. of N.C., Chapel Hill": True,
}
for aff, want in AFFS.items():
    check(f"affiliation {want!s:5} | {aff[:60]}", s.affiliation_is_unc(aff), want)

# ── Same surname at UNC, different first initial ───────────────────────────
pmids, _ = run("William Y. Kim", [
    article("1", author("Kim", "William Y", "WY", [UNC])),
    article("2", author("Kim", "Jiyoung", "J", [UNC])),             # different UNC Kim
    article("3", author("Kim", "William K", "WK", [UNC])),          # middle initial conflicts
    article("4", author("Kim", "William", "W", [UNC])),             # no middle → accept
])
check("UNC colleague with same surname rejected; middle-initial conflict rejected", pmids, ["1", "4"])

# ── Target at Duke, someone else at UNC ───────────────────────────────────
pmids, info = run("Brent A. Hanks", [
    article("10", author("Hanks", "Brent A", "BA", [DUKE]), author("Smith", "Jo", "J", [UNC])),
    article("11", author("Hanks", "Brent A", "BA", [UNC])),
])
check("Duke-affiliated target rejected even when a co-author is at UNC", pmids, ["11"])
check("reason recorded", info["rejected"], {"target-not-UNC": 1})

# ── Goes by middle name ───────────────────────────────────────────────────
pmids, _ = run("E. Claire Dees", [
    article("20", author("Dees", "E Claire", "EC", [UNC])),
    article("21", author("Dees", "Claire", "C", [UNC])),
    article("22", author("Dees", "Robert", "R", [UNC])),
])
check("goes-by-middle-name accepted under either initial", pmids, ["20", "21"])

# ── Nickname ───────────────────────────────────────────────────────────────
pmids, _ = run("Bill Stevens", [article("30", author("Stevens", "William R", "WR", [UNC]))])
check("nickname Bill → W accepted", pmids, ["30"])

# ── Compound / particle / apostrophe surnames ─────────────────────────────
pmids, _ = run("Luis E. Borges Espinosa", [
    article("40", author("Borges Espinosa", "Luis E", "LE", [UNC])),
    article("41", author("Espinosa", "Luis E", "LE", [UNC])),
])
check("two-part surname matched in both indexings", pmids, ["40", "41"])
pmids, _ = run("Aravinda M. de Silva", [article("42", author("de Silva", "Aravinda M", "AM", [UNC]))])
check("particle surname", pmids, ["42"])
pmids, _ = run("Meghan O’Leary", [article("43", author("O'Leary", "Meghan", "M", [UNC]))])
check("apostrophe surname", pmids, ["43"])
pmids, _ = run("Jacquelyn Baskin-Miller", [
    article("44", author("Baskin-Miller", "Jacquelyn", "J", [UNC])),
    article("45", author("Miller", "James", "J", [UNC])),
])
check("hyphenated surname: half alone doesn't match", pmids, ["44"])

# ── Two-letter surnames (were mis-parsed as initials) ──────────────────────
check("two-letter surname kept", s.build_author_identity("Danny T.Y. Wu")["surnames"], {"wu"})
pmids, _ = run("Yun Li", [article("46", author("Li", "Yun", "Y", [UNC])),
                          article("47", author("Li", "Xiao", "X", [UNC]))])
check("two-letter surname papers verified", pmids, ["46"])
check("given name that looks like a particle ('Di Wu')", s.build_author_identity("Di Wu")["first_initials"], {"d"})
check("search-term initials still stripped", s._surname_tokens("Hanks BA"), {"hanks"})

# ── Full first names must be compatible, not just initials ────────────────
pmids, _ = run("Yuli Jiang", [
    article("80", author("Jiang", "Yuli", "Y", [UNC])),
    article("81", author("Jiang", "Yukang", "Y", [UNC])),
    article("82", author("Jiang", "Yue", "Y", [UNC])),
])
check("Yukang/Yue rejected for Yuli (same initial, different name)", pmids, ["80"])
for name, fore, inits, want in [
    ("Theodore Yanagihara", "Ted K", "TK", True), ("Dan Hu", "Daniel", "D", True),
    ("Anwar Hossain", "Mohammad Anwar", "MA", True), ("S. Rahima Benhabbour", "Soumya Rahima", "SR", True),
    ("A. Sidney Barritt", "Alfred S", "AS", True), ("Yen-Yu Ian Shih", "Yen-Yu Ian", "YYI", True),
    ("Jacquelyn Patterson", "Jackie K", "JK", True), ("Joshua D. Garcia", "Jessica", "J", False),
    ("Kelly Brown", "Kevin D", "KD", False), ("Li Wang", "Limei", "L", False),
]:
    got = s.name_matches_identity(s._block_initials(fore, inits), s.build_author_identity(name), fore)
    check(f"first-name check: {name} vs {fore!r}", got is not False, want)
check("HTML entities decoded in names", s._letters("Hendr&#xe9;e"), "hendree")

# ── ORCID anchor ───────────────────────────────────────────────────────────
arts = [
    article("50", author("Chen", "Li", "L", [UNC], orcid=ORCID_A)),
    article("51", author("Chen", "Li", "L", [UNC], orcid=ORCID_A)),
    article("52", author("Chen", "Li", "L", [UNC], orcid=ORCID_B)),    # other UNC Li Chen
    article("53", author("Chen", "Li", "L", [OTHER], orcid=ORCID_A)),  # prior institution
    article("54", author("Chen", "Li", "L", [UNC])),                   # no ORCID on paper
]
pmids, info = run("Li Chen", arts)
check("ORCID anchor learned from 2+ strict papers", info["orcid"], "0000-0002-1825-0097")
check("different ORCID rejected; anchor ORCID recovers prior-institution paper", pmids, ["50", "51", "53", "54"])

pmids, info = run("Li Chen", arts + [article("55", author("Chen", "Li", "L", [UNC], orcid=ORCID_B))])
check("two ORCIDs each on 2+ strict papers → conflict flagged", info["orcid_conflict"], True)

pmids, _ = run("Li Chen", [article("56", author("Chen", "Li", "L", [OTHER], orcid=ORCID_A))],
               known_orcid="0000-0002-1825-0097", require_strict_anchor=True)
check("require_strict_anchor: ORCID-only papers with no UNC paper are rejected", pmids, [])

# ── Old records without per-author affiliations ───────────────────────────
pmids, _ = run("Gary Pielak", [article("60", author("Pielak", "Gary J", "GJ"), article_affs=[UNC])])
check("article-level UNC accepted for uncommon surname", pmids, ["60"])
pmids, _ = run("Patrick Smith", [article("61", author("Smith", "Patrick", "P"), article_affs=[UNC])])
check("article-level UNC rejected for common surname", pmids, [])

# ── Author list still captured, AuthorList not treated as an author ───────
ident = s.build_author_identity("Brent A. Hanks")
pub, rec = s._evaluate_article(article("70", author("Doe", "Jane", "J", [UNC]),
                                       author("Hanks", "Brent A", "BA", [UNC])), ident)
check("authors list order preserved", pub["authors"], ["Jane Doe", "Brent A Hanks"])

# ── Hint sanitizing: hints must contain the faculty member's surname ───────
for hint, name, want in [
    ("pregnant women", "David J. Weber", None),
    ("Munich Germany", "J. Niklas Ulrich", None),
    ("nez JC", "Jose Martinez", None),
    ("Leary", "Meghan O’Leary", None),
    ("clinical trials", "Yun Li", None),            # short surname must match a whole token
    ("Stouffer GA", "George Stouffer", "Stouffer George"),
    ("Wu DTY", "Danny T.Y. Wu", "Wu Danny"),
    ("Baskin Miller J", "Jacquelyn Baskin-Miller", "Baskin Miller J"),
    ("ORCID:0000-0002-1825-0097", "Li Chen", "ORCID:0000-0002-1825-0097"),
]:
    check(f"hint {hint!r} for {name} → {want!r}", s.sanitize_hint(hint, name), want)


# ── Department tiebreak for two same-name UNC people ──────────────────────
RAD = "Department of Radiology, University of North Carolina at Chapel Hill, Chapel Hill, NC."
BRIC = "Biomedical Research Imaging Center, University of North Carolina, Chapel Hill, NC."
PHARM = "Eshelman School of Pharmacy, University of North Carolina at Chapel Hill."
dept_arts = [
    article("90", author("Wang", "Li", "L", [RAD], orcid=ORCID_A)),
    article("91", author("Wang", "Li", "L", [BRIC], orcid=ORCID_A)),
    article("92", author("Wang", "Li", "L", [RAD], orcid=ORCID_A)),
    article("93", author("Wang", "Li", "L", [PHARM], orcid=ORCID_B)),
    article("94", author("Wang", "Li", "L", [PHARM], orcid=ORCID_B)),
    article("95", author("Wang", "Li", "L", [RAD])),     # no ORCID, right department
    article("96", author("Wang", "Li", "L", [PHARM])),   # no ORCID, other department
]
pmids, info = run("Li Wang", dept_arts, dept_patterns=s.department_patterns(["Radiology"]))
check("dept tiebreak: Radiology ORCID group kept, pharmacy Li Wang dropped",
      (pmids, info["resolved_by_department"], info["orcid_conflict"]),
      (["90", "91", "92", "95"], True, False))
pmids, info = run("Li Wang", dept_arts, dept_patterns=s.department_patterns(["Medicine — Cardiology"]))
check("dept tiebreak: neither group in department → still a conflict",
      info["orcid_conflict"], True)
check("department patterns", [p.pattern for p in s.department_patterns(
      ["Medicine — Gastroenterology & Hepatology", "Lineberger Comprehensive Cancer Center"])],
      [r"\bgastroe", r"\bhepatol", r"\blineber", r"\blineberger"])

# ── End-to-end enrichment with mocked network ──────────────────────────────
import time as _time  # noqa: E402
s.PUBMED_SLEEP = 0
_time.sleep = lambda *_: None
ARTS = {}


def add(pmid, *authors):
    ARTS[pmid] = article(pmid, *authors)


def mock_records(pmids, identity):
    return [s._evaluate_article(ARTS[p], identity) for p in pmids if p in ARTS]


s._pubmed_fetch_records_batch = mock_records
s.search_orcid_by_name = lambda name: (None, None, None)


def enrich(name, search_ids, *, unc_ids=None, ca=None, hint=None, only_term=None,
           dept="Radiology"):
    """search_ids: what the UNC-filtered search returns; unc_ids=[] simulates
    nothing at UNC so the no-affiliation fallback returns search_ids."""
    def fake_search(term, affiliation="University of North Carolina", max_results=300):
        if only_term and term != only_term:
            ids = []
        elif affiliation:
            ids = search_ids if unc_ids is None else unc_ids
        else:
            ids = search_ids
        return {"count": len(ids), "ids": list(ids)}
    s.pubmed_search = fake_search
    # ca: a list (one cluster for any seed) or a dict seed → cluster
    def fake_ca(n, seed_pmid=None):
        cl = ca.get(seed_pmid) if isinstance(ca, dict) else ca
        return (list(cl), len(cl)) if cl else ([], 0)
    s.fetch_pmids_via_computed_authors = fake_ca
    f = {"name": name, "department": dept, "departments": [dept]}
    s.enrich_faculty_with_pubmed(f, pubmed_string=hint)
    return f


# A: strict verification + CA expansion with ORCID-recovered prior-institution paper
add("100", author("Kim", "William Y", "WY", [UNC], orcid=ORCID_A))
add("101", author("Kim", "William Y", "WY", [UNC], orcid=ORCID_A))
add("102", author("Kim", "William Y", "WY", [DUKE]))                    # Duke
add("103", author("Kim", "Jae", "J", [UNC]))                            # other UNC Kim
add("104", author("Kim", "William Y", "WY", [OTHER], orcid=ORCID_A))    # prior inst., in CA
add("105", author("Kim", "William Y", "WY", [UNC]))                     # only in CA
f = enrich("William Y. Kim", ["100", "101", "102", "103"], ca=["100", "101", "104", "105"])
check("E2E A: CA expansion keeps strict + ORCID papers",
      sorted(p["pmid"] for p in f["publications"]), ["100", "101", "104", "105"])
check("E2E A: ORCID recorded", f.get("pubmed_orcid"), "0000-0002-1825-0097")
check("E2E A: not flagged", f["pubmed_ambiguous"], False)

# B: CA returns a fragment of the person's papers → union, not replace;
#    cluster papers by a different UNC person (other initials) are still rejected
add("110", author("Chen", "Li", "L", [UNC]))
add("111", author("Chen", "Li", "L", [UNC]))
add("112", author("Chen", "Li", "L", [UNC]))
add("113", author("Chen", "Li", "L", [UNC]))
add("114", author("Chen", "Xu", "X", [UNC]))
f = enrich("Li Chen", ["110", "111", "112"], ca=["110", "113", "114"])
check("E2E B: CA fragment unioned with verified list; wrong-initials paper rejected",
      sorted(p["pmid"] for p in f["publications"]), ["110", "111", "112", "113"])

# B2: newest paper isn't clustered yet → older seed used; second fragment pooled
def art_year(pmid, year, *authors):
    ARTS[pmid] = article(pmid, *authors).replace("<Year>2024</Year>", f"<Year>{year}</Year>")


THIS_YEAR = s.datetime.utcnow().year
art_year("180", THIS_YEAR, author("Kuzmiak", "Cherie M", "CM", [UNC]))
art_year("181", THIS_YEAR - 1, author("Kuzmiak", "Cherie M", "CM", [UNC]))
art_year("182", THIS_YEAR - 2, author("Kuzmiak", "Cherie M", "CM", [UNC]))
art_year("183", THIS_YEAR - 2, author("Kuzmiak", "Cherie M", "CM", [UNC]))
art_year("184", THIS_YEAR - 3, author("Kuzmiak", "Cherie M", "CM", [UNC]))
art_year("185", THIS_YEAR - 3, author("Kuzmiak", "Cherie M", "CM", [UNC]))
art_year("186", THIS_YEAR - 3, author("Kuzmiak", "Cherie M", "CM", [UNC]))
check("seeds: last year's paper first, this year's last",
      s._pick_ca_seeds([{"pmid": "180", "year": str(THIS_YEAR)},
                        {"pmid": "181", "year": str(THIS_YEAR - 1)},
                        {"pmid": "182", "year": str(THIS_YEAR - 2)}]), ["181", "182", "180"])
f = enrich("Cherie M. Kuzmiak", ["180", "181", "182", "183"],
           ca={"181": ["181", "184"], "182": ["182", "183", "185", "186"]})
check("E2E B2: two CA fragments pooled from two seeds",
      sorted(p["pmid"] for p in f["publications"]),
      ["180", "181", "182", "183", "184", "185", "186"])

# C: found only outside UNC → flagged, nothing shown
add("120", author("Newman", "Ada B", "AB", [OTHER]))
f = enrich("Ada B. Newman", ["120"], unc_ids=[])
check("E2E C: recent recruit hidden + flagged",
      (f["publications"], f["pubmed_ambiguous"], f.get("pubmed_recent_recruit")), ([], True, True))

# D: two different ORCID identities pass strict → hide
add("130", author("Lee", "Jin", "J", [UNC], orcid=ORCID_A))
add("131", author("Lee", "Jin", "J", [UNC], orcid=ORCID_A))
add("132", author("Lee", "Jin", "J", [UNC], orcid=ORCID_B))
add("133", author("Lee", "Jin", "J", [UNC], orcid=ORCID_B))
f = enrich("Jin Lee", ["130", "131", "132", "133"])
check("E2E D: mixed identities hidden + flagged",
      (f["publications"], f["pubmed_ambiguous"], len(f.get("pubmed_unverified_pmids", []))),
      ([], True, 4))

# D2: same, but the department settles it
for pm, aff, oc in [("190", RAD, ORCID_A), ("191", BRIC, ORCID_A), ("192", RAD, ORCID_A),
                    ("193", PHARM, ORCID_B), ("194", PHARM, ORCID_B)]:
    add(pm, author("Wang", "Gang", "G", [aff], orcid=oc))
add("195", author("Wang", "Gang", "G", [PHARM]))   # other Gang Wang, no ORCID, in CA output
f = enrich("Gang Wang", ["190", "191", "192", "193", "194"], ca=["190", "195"])
check("E2E D2: department resolves same-name UNC people",
      (sorted(p["pmid"] for p in f["publications"]), f["pubmed_ambiguous"]),
      (["190", "191", "192"], False))

# E: common surname + single-initial search + 1 paper → hide
add("140", author("Smith", "Patrick", "P", [UNC]))
f = enrich("Patrick Smith", ["140"], only_term="Smith P")
check("E2E E: low-confidence hidden",
      (f["publications"], f["pubmed_ambiguous"], f.get("pubmed_flag_reason")),
      ([], True, "common surname + single initial, few papers"))

# F: clean single-person case, no CA → shown, not flagged
add("150", author("Pielak", "Gary J", "GJ", [UNC]))
add("151", author("Pielak", "Gary J", "GJ", [UNC]))
f = enrich("Gary Pielak", ["150", "151"])
check("E2E F: normal case shown",
      (sorted(p["pmid"] for p in f["publications"]), f["pubmed_ambiguous"]), (["150", "151"], False))


# G/H: ORCID name-search fallback must include a strictly verified UNC paper
add("160", author("Okafor", "Nneka", "N", [UNC], orcid=ORCID_B))
add("161", author("Okafor", "Nneka", "N", [OTHER], orcid=ORCID_B))
add("170", author("Okafor", "Nneka", "N", [OTHER], orcid=ORCID_A))
add("171", author("Okafor", "Nneka", "N", [OTHER], orcid=ORCID_A))
s.search_orcid_by_name = lambda name: ("0000-0001-5109-3700", "Nneka", "Okafor")
s.fetch_pmids_via_orcid = lambda oid, since_year=None: (["160", "161"], [])
f = enrich("Nneka Okafor", [])
check("E2E G: ORCID fallback with a UNC anchor paper accepted",
      sorted(p["pmid"] for p in f["publications"]), ["160", "161"])
s.search_orcid_by_name = lambda name: ("0000-0002-1825-0097", "Nneka", "Okafor")
s.fetch_pmids_via_orcid = lambda oid, since_year=None: (["170", "171"], [])
f = enrich("Nneka Okafor", [])
check("E2E H: same-name ORCID record with no UNC paper rejected", f["publications"], [])
s.search_orcid_by_name = lambda name: (None, None, None)

# I: plain author names must NOT be quoted — quoting disables PubMed's
# truncation, so "Patel K"[Author] missed 'Patel KP' (PMID 36508317, Kunal P
# Patel, UNC PCCM) and "Flower Kori"[Author] missed 'Flower, Kori B'.
check("Search: 'Patel K' unquoted", s._author_clause("Patel K"), "Patel K[Author]")
check("Search: 'Flower Kori' unquoted", s._author_clause("Flower Kori"), "Flower Kori[Author]")
check("Search: apostrophe surname unquoted", s._author_clause("O'Shea TM"), "O'Shea TM[Author]")
check("Search: particle surname unquoted", s._author_clause("Van Le L"), "Van Le L[Author]")
check("Search: non-plain term stays quoted", s._author_clause("Stürmer T"), '"Stürmer T"[Author]')
ident = s.build_author_identity("Kunal Patel", "Patel K")
rec = s._evaluate_article(article("36508317",
        author("Mainardi", "Anne S", "AS", [OTHER]),
        author("Patel", "Kunal P", "KP", ["Division of Pulmonary and Critical Care Medicine, "
               "University of North Carolina at Chapel Hill, Chapel Hill, North Carolina."])), ident)[1]
check("Kunal P Patel (middle initial) passes strict check", s._strict_pass(rec, ident)[0], True)
for wrong_fore, ini in [("Kinjal", "K"), ("Kripa", "K"), ("Kamal", "K")]:
    rec = s._evaluate_article(article("1", author("Patel", wrong_fore, ini, [UNC])), ident)[1]
    check(f"{wrong_fore} Patel rejected for Kunal Patel", s._strict_pass(rec, ident)[0], False)

# J: one person with two ORCID records is not a same-name conflict when every
# paper under both iDs carries the faculty member's first + middle initials.
ORCID_C = "0000-0003-1415-9265"
arts = [article(str(300 + i), author("Ting", "Jenny P Y", "JPY", [UNC], orcid=o))
        for i, o in enumerate([ORCID_A, ORCID_A, ORCID_C, ORCID_C])]
arts.append(article("305", author("Ting", "Jenny P", "JP", [UNC], orcid=ORCID_C)))
pubs, info = run("Jenny P. Y. Ting", arts, search_term="Ting JPY")
check("Two ORCIDs, same full initials (JPY/JP) → one person, all kept",
      (len(pubs), info["orcid_conflict"]), (5, False))
arts = [article(str(310 + i), author("Richter", "Margaret", "M", [UNC], orcid=o))
        for i, o in enumerate([ORCID_A, ORCID_A, ORCID_C, ORCID_C])]
pubs, info = run("Margaret Richter", arts, search_term="Richter Margaret")
check("Two ORCIDs, no middle initial → still a conflict", info["orcid_conflict"], True)
arts = [article(str(320 + i), author("Ting", fore, ini, [UNC], orcid=o))
        for i, (fore, ini, o) in enumerate([("Jenny P Y", "JPY", ORCID_A), ("Jenny P Y", "JPY", ORCID_A),
                                            ("Jenny M", "JM", ORCID_C), ("Jenny M", "JM", ORCID_C)])]
pubs, info = run("Jenny P. Y. Ting", arts, search_term="Ting J")
check("Two ORCIDs, other iD has different middle initial → not merged",
      ORCID_C.replace("-", "") in str(info) and info["orcid_conflict"] is False and len(pubs) == 4, False)

# K: HTML entities in titles / author names are decoded for display
pub, _ = s._evaluate_article(article("400", author("Ch&#xe9;ry", "Samantha L", "SL", [UNC]),
                                     title="(3&#x3b1;,5&#x3b1;)-THP and CX3CL1"),
                             s.build_author_identity("Samantha Chery", ""))
check("Entities decoded in title", pub["title"], "(3\u03b1,5\u03b1)-THP and CX3CL1")
check("Entities decoded in author name", pub["authors"][0], "Samantha L Ch\u00e9ry")

# L: MyNCBI bibliographies are trusted, but a paper that doesn't list the
# faculty member at all (Shawn Hingtgen's list held two Duke papers by others)
# is dropped.
_orig_fetch = s._pubmed_fetch_records_batch
s._pubmed_fetch_records_batch = lambda pmids, identity: [s._evaluate_article(a, identity) for a in [
    article("500", author("Hingtgen", "Shawn D", "SD", [OTHER])),
    article("501", author("Floyd", "Scott R", "SR", [DUKE]), author("Oldham", "Mark", "M", [DUKE]))]]
pubs = s.pubmed_fetch_summaries(["500", "501"], verify_affiliation=False, search_term="Hingtgen SD",
                                faculty_name="Shawn D. Hingtgen", require_name_on_paper=True)
check("MyNCBI: paper without the faculty member dropped, own paper kept (any affiliation)",
      [p["pmid"] for p in pubs], ["500"])
s._pubmed_fetch_records_batch = _orig_fetch

# M: MyNCBI bibliography pages are read past page 1 (50 per page)
_orig_url = s.fetch_url
pages = {"": [str(10000000 + i) for i in range(50)], "?page=2": ["20000001", "20000002"]}
s.fetch_url = lambda url, *a, **k: "".join(f'<a href="/pubmed/{p}/">x</a>'
                                           for p in pages.get(url.split("/public/")[-1], []))
ids, n = s.fetch_pmids_by_author_id("some.one.1", max_results=300)
check("MyNCBI: page 2 read, stops when a page is short", (n, ids[-1]), (52, "20000002"))
s.fetch_url = _orig_url

# N: initials-only author blocks ('K D Allen') are ambiguous when two UNC
# faculty share the surname + first initial (Katherine Allen / Kelli Allen).
s.set_unc_roster(["Katherine Allen", "Kelli Allen", "Evan S. Dellon", "Elisabeth Dellon",
                  "Dan Reuland", "Daniel S. Reuland", "Patrick J. Smith", "Patrick Carlsen Smith",
                  "Di Wu", "Danny T.Y. Wu", "E. Claire Dees", "Elizabeth Claire Dees",
                  "Jacquelyn Patterson", "J. Megan Patterson"])
def _one(name, fore, ini, st=""):
    ident = s.build_author_identity(name, st)
    rec = s._evaluate_article(article("600", author(name.split()[-1], fore, ini, [UNC])), ident)[1]
    return s._strict_pass(rec, ident)[0]
check("Initials-only 'K D Allen' rejected for Katherine Allen", _one("Katherine Allen", "K D", "KD"), False)
check("Initials-only 'K D Allen' rejected for Kelli Allen too (no middle known)", _one("Kelli Allen", "K D", "KD"), False)
check("Spelled-out 'Kelli D Allen' still accepted", _one("Kelli Allen", "Kelli D", "KD"), True)
check("'E S Dellon' accepted for Evan S. Dellon (middle initial matches)", _one("Evan S. Dellon", "E S", "ES"), True)
check("Duplicate record Dan / Daniel Reuland is not a rival", _one("Dan Reuland", "D S", "DS"), True)
check("'Patrick Smith' (no middle) rejected: fits two UNC Patrick Smiths", _one("Patrick J. Smith", "Patrick", "P"), False)
check("'Patrick J Smith' accepted for Patrick J. Smith", _one("Patrick J. Smith", "Patrick J", "PJ"), True)
check("'D Wu' rejected for Di Wu (Danny T.Y. Wu also fits)", _one("Di Wu", "D", "D"), False)
check("'Di Wu' spelled out accepted", _one("Di Wu", "Di", "D"), True)
check("E. Claire / Elizabeth Claire Dees = one person, 'E Claire Dees' kept", _one("E. Claire Dees", "E Claire", "EC"), True)
check("'Jacquelyn K Patterson' kept (J. Megan Patterson doesn't fit)", _one("Jacquelyn Patterson", "Jacquelyn K", "JK"), True)
s.set_unc_roster([])

# O: per-author UNC positions are recorded so student matching can require
# the student's own block to be at UNC
pub, _ = s._evaluate_article(article("700", author("Wood", "Brian", "B", [OTHER]),
                                     author("Desai", "Shivani", "S", [UNC]),
                                     author("Dellon", "Evan S", "ES", [UNC])),
                             s.build_author_identity("Evan S. Dellon", ""))
check("UNC author positions recorded", pub["unc_author_idx"], [1, 2])
import build_collaborators as bc
fl = [{"name": "Evan S. Dellon", "publications": [pub]}]
idx = {bc._student_match_key("Brian Wood"): "Brian Wood", bc._student_match_key("Shivani Desai"): "Shivani Desai"}
key_map = {0: "dellon"}
bc.attach_student_coauthors(fl, idx, key_map, {"dellon": 0})
check("Student matched only where the author was at UNC",
      [x["student"] for x in fl[0]["student_coauthors"]], ["Shivani Desai"])

print()
print(f"{len(FAILS)} failure(s)" if FAILS else "All tests passed.")
sys.exit(1 if FAILS else 0)
