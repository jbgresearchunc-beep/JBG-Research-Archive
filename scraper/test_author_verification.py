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

# ── ORCID anchor ───────────────────────────────────────────────────────────
arts = [
    article("50", author("Chen", "Li", "L", [UNC], orcid=ORCID_A)),
    article("51", author("Chen", "Li", "L", [UNC], orcid=ORCID_A)),
    article("52", author("Chen", "Lin", "L", [UNC], orcid=ORCID_B)),   # other UNC "Chen L"
    article("53", author("Chen", "Li", "L", [OTHER], orcid=ORCID_A)),  # prior institution
    article("54", author("Chen", "Li", "L", [UNC])),                   # no ORCID on paper
]
pmids, info = run("Li Chen", arts)
check("ORCID anchor learned from 2+ strict papers", info["orcid"], "0000-0002-1825-0097")
check("different ORCID rejected; anchor ORCID recovers prior-institution paper", pmids, ["50", "51", "53", "54"])

pmids, info = run("Li Chen", arts + [article("55", author("Chen", "Lin", "L", [UNC], orcid=ORCID_B))])
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


def enrich(name, search_ids, *, unc_ids=None, ca=None, hint=None, only_term=None):
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
    s.fetch_pmids_via_computed_authors = (lambda n, seed_pmid=None:
                                          (list(ca), len(ca)) if ca else ([], 0))
    f = {"name": name}
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

# B: CA cluster belongs to someone else (doesn't contain our verified papers)
add("110", author("Chen", "Li", "L", [UNC]))
add("111", author("Chen", "Li", "L", [UNC]))
add("112", author("Chen", "Li", "L", [UNC]))
add("113", author("Chen", "Li", "L", [UNC]))
f = enrich("Li Chen", ["110", "111", "112"], ca=["110", "113"])
check("E2E B: low-overlap CA cluster rejected, verified list kept",
      sorted(p["pmid"] for p in f["publications"]), ["110", "111", "112"])

# C: found only outside UNC → flagged, nothing shown
add("120", author("Newman", "Ada B", "AB", [OTHER]))
f = enrich("Ada B. Newman", ["120"], unc_ids=[])
check("E2E C: recent recruit hidden + flagged",
      (f["publications"], f["pubmed_ambiguous"], f.get("pubmed_recent_recruit")), ([], True, True))

# D: two different ORCID identities pass strict → hide
add("130", author("Lee", "Jin", "J", [UNC], orcid=ORCID_A))
add("131", author("Lee", "Jin", "J", [UNC], orcid=ORCID_A))
add("132", author("Lee", "Jun", "J", [UNC], orcid=ORCID_B))
add("133", author("Lee", "Jun", "J", [UNC], orcid=ORCID_B))
f = enrich("Jin Lee", ["130", "131", "132", "133"])
check("E2E D: mixed identities hidden + flagged",
      (f["publications"], f["pubmed_ambiguous"], len(f.get("pubmed_unverified_pmids", []))),
      ([], True, 4))

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

print()
print(f"{len(FAILS)} failure(s)" if FAILS else "All tests passed.")
sys.exit(1 if FAILS else 0)
