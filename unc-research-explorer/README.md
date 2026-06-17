# UNC SOM Research Explorer

A student-built tool for finding research opportunities at the UNC School of Medicine. Built and maintained by the JGB Research Society.

## What it does

- Scrapes faculty names and roles from UNC SOM department pages weekly
- Enriches each faculty member with recent publications from PubMed
- Optionally adds active NIH grant information from NIH RePORTER
- Presents everything in a searchable, filterable web interface

## Setup (one-time, ~15 minutes)

### 1. Fork this repo

Click **Fork** in the top right on GitHub.

### 2. Enable GitHub Pages

Go to your repo → **Settings** → **Pages** → Source: **Deploy from a branch** → Branch: `main` / folder: `/ (root)`.

Your site will be live at `https://YOUR-ORG.github.io/unc-research-explorer/`

### 3. Enable GitHub Actions

Go to **Actions** and enable workflows if prompted. The scraper runs automatically every Sunday.

### 4. Run the first scrape manually

Go to **Actions** → **Scrape UNC SOM Faculty Data** → **Run workflow**.

This takes 30–60 minutes for all departments. You can filter to a single department to test faster:
- Set `department_filter` to e.g. `Plastic` to only scrape Plastic Surgery
- Check `skip_nih` to skip the NIH RePORTER step and finish faster

### 5. Update the GitHub link in the footer

Edit `index.html` and replace `YOUR-ORG` in the footer with your GitHub username/org.

---

## Running locally

```bash
# Scrape all departments (takes ~45 min)
python3 scraper/scrape.py

# Scrape one department only (for testing)
python3 scraper/scrape.py --dept "Plastic"

# Skip NIH step
python3 scraper/scrape.py --skip-nih

# Serve the frontend
python3 -m http.server 8000
# then open http://localhost:8000
```

No dependencies beyond the Python standard library.

---

## Maintenance

**Adding a department:** Edit `scraper/departments.json` and add an entry. The scraper will pick it up on the next run.

**A department page changed its URL or HTML structure:** Edit the URL in `departments.json`. If the scraper can't find faculty names, you may need to add a custom extraction in `scraper/scrape.py` — see the `scrape_faculty_from_page` function.

**A faculty name has too many false positives on PubMed:** Find the faculty member in `data/faculty.json` and note their `pubmed_search` field. You can override it by adding a `pubmed_hint` field manually, or by updating the profile page scraping logic to find the curated string the faculty member has listed on their UNC profile.

---

## How it works

```
Every Sunday (GitHub Actions cron)
  └── For each department URL in departments.json
        └── Scrape faculty names + profile links
              └── Check each profile for a curated PubMed search string
                    └── Query PubMed E-utilities API (name + UNC affiliation)
                          └── Fetch article titles, journals, years
                                └── Query NIH RePORTER for active grants
                                      └── Write data/faculty.json
                                            └── Commit & push → GitHub Pages auto-redeploys
```

---

## Known limitations

- **Name disambiguation:** Common names (e.g. "John Smith") may pull in publications from other institutions even with the UNC affiliation filter. Faculty who list a curated PubMed search string on their profile page are more accurate.
- **New faculty:** Will appear once the scraper runs after they are added to a department page.
- **Department page structure:** Each department has slightly different HTML. The scraper uses heuristics that work for most UNC SOM pages but may miss some faculty on unusual page layouts.

---

Built by JGB Research Society · UNC School of Medicine
