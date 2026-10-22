# Inbox: documents to add to the record

Upload PDFs here (on GitHub: **Add file → Upload files**, then commit to `main`).

The next daily update (or a manual run of **Update records**) files each one:

- If its file name matches a document the township posted (for example `CAPITAL-BUDGET-2024-2034-FORECAST.pdf`), the copy is attached everywhere that document is listed: the meeting it was presented at, the by-law, the Finances page. Keep the township's original file name.
- A financial file with no known address is kept as a financial document.
- Anything else stays here and is listed in `data/runtime/import_log.json` for a person to look at.

Filed files are removed from this folder; the copy lives under `docs/files/archive/`.
