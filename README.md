# nipissing.news — Township of Nipissing public record

An independent, permanent public record of the Township of Nipissing's council:
meetings, minutes, agenda packages, videos, resolutions, by-laws, the laws in
force today, and the township's finances. The township removes documents from
its own site as they age; this archive keeps a copy of each one.

The site is static. GitHub Pages serves it straight from the root of `main`
(Settings → Pages → Deploy from a branch → `main` / `(root)`), so whatever is
committed to `main` is live. There is no build step.

## Pages

| Page | What it shows | Data it reads |
|---|---|---|
| `index.html` | Latest meeting, recent decisions, recently amended laws | meetings, resolutions, bylaws, laws_in_effect |
| `council.html` | Every meeting, with minutes, package and video | meetings |
| `meeting-detail.html?id=` | One meeting: documents, video, summary, what was in the package | meetings, package_index |
| `decisions.html` | All resolutions and by-laws, searchable | resolutions, bylaws |
| `resolution-detail.html?id=` | One resolution, connected decisions, the papers council had | resolutions, package_index |
| `bylaw-detail.html?id=` | One by-law: file, status, what it amends or repeals | bylaws, bylaw_families |
| `laws.html` | The standing laws, by topic | laws_in_effect |
| `law.html?id=` | One law as it reads today (consolidated, clean copy or outline), printable | laws_in_effect, consolidations |
| `terms.html` | Council terms, elections, strategic plans | terms, council_terms, plans |
| `finances.html` | Taxes, spending, reserves, debt since 2009; budgets; financial documents | financial_series, financials |
| `about.html` | What this is and how it works | — |
| `bylaws.html` | Redirect for old links | — |

`shared.js` and `shared.css` are used by every page. When you change either,
bump the `?v=` on their `<script>`/`<link>` tags so browsers fetch the new copy.

## Data

`data/canonical/` — what the pages read. Built by the updaters unless noted.

- `meetings.json`, `resolutions.json`, `bylaws.json`, `boards.json`
- `package_index.json` — contents of each agenda package (text in `data/packages/<date>.txt.gz`)
- `bylaw_families.json`, `consolidations.json` — laws and their amendments; consolidated texts
- `laws_in_effect.json` — **hand-edited**: which by-laws are the standing laws, by topic
- `council_terms.json`, `terms.json` — **hand-edited**: councillors, elections, term notes
- `financials.json` — documents (built) plus budget figures, council pay and comparators (**hand-edited**, each with its source)
- `financial_series.json` — figures by year from the Province's Financial Information Return (`data/financial/fir/`)
- `plans.json` — the township's Strategies & Plans page, copied daily

`data/runtime/` — updater state, caches and logs; the site doesn't read them.

`docs/files/` — the archive of documents (PDFs). Agenda packages too large for
the repository are in the GitHub Release **document-archive**.

## The daily update

`.github/workflows/update-records.yml` runs every day (and on demand from the
Actions tab). Each updater gets 30 minutes; one that fails or runs over is
skipped and its stored data kept. In order:

1. `update_meetings` — meetings, minutes, agendas and packages from the township site
2. `import_documents` — files uploaded to `inbox/`
3. `financials`, `financial_series` — financial documents and FIR figures
4. `recover_documents` — finds lost files in the Wayback Machine
5. `generate_summaries` — meeting summaries
6. `update_bylaws` — by-laws and resolutions
7. `package_index`, `consolidate_bylaws` — read packages; build law texts
8. `update_adjustment`, `update_boards`, `update_library`, `update_healthunit`, `update_dnssab` — committee and board records
9. `attach_videos` — YouTube recordings
10. `doc_tidy`, `archive_plans`, `check_freshness`, `audit_site`, `build_sitemap`

Then `validate_data.py` checks the data and the run commits the changes.
If the township site is down, the township scrapers are skipped for that run.

Other workflows: **Import inbox** (files an upload to `inbox/` immediately),
**Term summary** (end-of-term summary), **Inventory by-laws** (a one-click
survey of every by-law PDF on the township site; changes nothing).

## Adding documents by hand

Upload PDFs to `inbox/` and keep the township's original file name. See
`inbox/README.md`.

## Running locally

```sh
pip install requests beautifulsoup4 pdfminer.six pymupdf
python scripts/update_meetings.py      # or any updater
python scripts/validate_data.py
python -m http.server 8000             # then open http://localhost:8000
```

Summaries and law outlines need `ANTHROPIC_API_KEY`; videos need `YOUTUBE_API_KEY`;
scanned package pages need `tesseract-ocr`.
