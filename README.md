# Nipissing Public Records starter scripts

This folder contains a starter rebuild of the new repo architecture.

## Layout

- `data/canonical/` – source-of-truth JSON
- `data/runtime/` – mutable runtime state like YouTube matches
- `scripts/` – updaters, validation, and build steps
- `site/` – static HTML/CSS/JS templates
- `docs/` – generated output

## Run order

```bash
python scripts/update_meetings.py
python scripts/update_bylaws.py
python scripts/update_boards.py
python scripts/validate_data.py
python scripts/build_site.py
```

## Notes

- `update_meetings.py` is the most complete updater and is designed to preserve pre-2024 canonical history while refreshing 2024+.
- `update_bylaws.py` refreshes the by-law listing conservatively and preserves canonical resolutions.
- `update_boards.py` refreshes board links conservatively and removes obviously bad AI-summary text from public-facing data.
- `build_site.py` keeps your current front-end templates working by emitting the legacy JSON filenames into `docs/`.
