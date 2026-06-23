#!/usr/bin/env python3
"""
backfill_from_cache.py — one-time archive backfill from the local PDF cache.

data/runtime/pdf_cache holds minutes and agenda packages downloaded over time,
INCLUDING documents the Township has since deleted from its live site. A normal
run can't recover those (the live URLs 404); the cached copy is the only one
left. This walks the canonical records, matches each to a cached file by the
same filename rule the downloader used, and mirrors the LOCAL copy into the
archive with no network fetch.

  * Minutes  → committed in-repo (docs/files/archive/...). No token needed.
  * Packages → uploaded to the GitHub Release. Needs a token + repo in env:
                 GH_PAT=<token> GITHUB_REPOSITORY=<owner/repo> \
                 python scripts/backfill_from_cache.py
               Without those, packages are skipped (minutes still backfill).

Idempotent and non-destructive: anything already archived is left untouched.
After running, commit docs/files and data/canonical as usual.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from archive import (
    ROOT,
    store_bytes_in_repo,
    store_bytes_to_release,
)

CANONICAL = ROOT / "data" / "canonical"
CACHE = ROOT / "data" / "runtime" / "pdf_cache"
MINUTES_CACHE = CACHE / "minutes"
PACKAGES_CACHE = CACHE / "packages"


def cache_name(url: str) -> str:
    """Reproduce download_pdf()'s cache filename from a URL."""
    return re.sub(r"[^\w\-\.]", "_", url.split("/")[-1])


def find_cached(url: Optional[str], folder: Path) -> Optional[Path]:
    if not url:
        return None
    p = folder / cache_name(url)
    return p if p.exists() and p.stat().st_size > 0 else None


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def backfill_meeting(rec: dict, category: str,
                     used_min: set, used_pkg: set) -> bool:
    changed = False
    year = rec.get("year")

    # minutes → in-repo (offline, no token)
    if not rec.get("minutes_archived"):
        cached = find_cached(rec.get("minutes_url"), MINUTES_CACHE)
        if cached:
            used_min.add(cached.name)
            got = store_bytes_in_repo(rec["minutes_url"], category, year, cached.read_bytes())
            if got:
                rec["minutes_archived"] = got
                print(f"  ✓ minutes  {rec.get('display_date', '?')}")
                changed = True

    # package → Releases (needs token)
    if not rec.get("package_archived"):
        cached = find_cached(rec.get("package_url"), PACKAGES_CACHE)
        if cached:
            used_pkg.add(cached.name)
            got = store_bytes_to_release(rec["package_url"], cached.read_bytes())
            if got:
                rec["package_archived"] = got
                print(f"  ✓ package  {rec.get('display_date', '?')}")
                changed = True

    return changed


def report_orphans(folder: Path, used: set, label: str) -> None:
    if not folder.exists():
        return
    files = [p.name for p in folder.iterdir() if p.is_file() and p.stat().st_size > 0]
    orphans = sorted(set(files) - used)
    print(f"\n{label}: {len(files)} cached, {len(files) - len(orphans)} matched, "
          f"{len(orphans)} unmatched")
    for name in orphans[:25]:
        print(f"    ? {name}")
    if len(orphans) > 25:
        print(f"    … and {len(orphans) - 25} more")
    if orphans:
        print(f"  (unmatched = no canonical record points at that URL; "
              f"usually safe to ignore, but worth a glance)")


def main() -> None:
    if not CACHE.exists():
        print(f"No cache folder at {CACHE} — nothing to backfill.")
        return

    print("=" * 60)
    print("Backfill archive from local PDF cache")
    print(f"Cache: {CACHE}")
    print("=" * 60)

    used_minutes: set = set()
    used_packages: set = set()

    # ---- meetings.json
    mfile = CANONICAL / "meetings.json"
    meetings = load(mfile)
    n = 0
    print("\nCouncil meetings:")
    for rec in meetings.get("meetings", []):
        if backfill_meeting(rec, "meetings", used_minutes, used_packages):
            n += 1
    save(mfile, meetings)
    print(f"  backfilled {n} council meeting(s)")

    # ---- boards.json
    bfile = CANONICAL / "boards.json"
    boards = load(bfile)
    nb = 0
    print("\nBoard meetings:")
    for board in boards.get("boards", []):
        cat = f"boards/{board['id']}"
        for rec in board.get("meetings", []):
            if backfill_meeting(rec, cat, used_minutes, used_packages):
                nb += 1
    save(bfile, boards)
    print(f"  backfilled {nb} board meeting(s)")

    # Orphan report (which cached files no canonical record claimed)
    report_orphans(MINUTES_CACHE, used_minutes, "Minutes cache")
    report_orphans(PACKAGES_CACHE, used_packages, "Packages cache")

    print("\nDone. Review the diff, then commit docs/files and data/canonical.")
    print("If packages were skipped, re-run with GH_PAT and GITHUB_REPOSITORY set.")


if __name__ == "__main__":
    main()
