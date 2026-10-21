#!/usr/bin/env python3
"""
check_freshness.py — tells you when a source stops producing new meetings.

Scrapers fail quietly when a site changes: the run still succeeds and the
stored data is kept, so nothing looks wrong. This compares each body's most
recent captured meeting (past or scheduled) with how often it normally meets,
and flags any body that has gone quiet for longer than expected.

Warnings are printed as GitHub Actions annotations, so they appear on the
run's summary page without opening the log. The run itself never fails.
Report also saved to data/runtime/freshness.json.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "data" / "canonical"
OUT = ROOT / "data" / "runtime" / "freshness.json"
TODAY = date.today()

# Days without any captured meeting (past or scheduled) before warning.
# Generous: allows summer breaks and irregular schedules.
LIMITS = {
    "council": 45,
    "recreation": 90,
    "museum": 120,
    "cemetery": 400,
    "pnc": 180,
    "adjustment": 150,
    "library": 120,
    "healthunit": 150,
    "dnssab": 90,
    "dnssab-ndhc": 120,
}
DEFAULT_LIMIT = 365   # committees of joint boards, anything new


def main() -> None:
    bodies = {}
    council = json.loads((CANON / "meetings.json").read_text(encoding="utf-8"))
    bodies["council"] = council.get("meetings", council) if isinstance(council, dict) else council
    for b in json.loads((CANON / "boards.json").read_text(encoding="utf-8")).get("boards", []):
        bodies[b["id"]] = b.get("meetings", [])

    report, warnings = {}, 0
    for body, ms in sorted(bodies.items()):
        dates = sorted(m["date"] for m in ms if m.get("date") and not m.get("cancelled"))
        past = [d for d in dates if d <= TODAY.isoformat()]
        future = [d for d in dates if d > TODAY.isoformat()]
        latest = dates[-1] if dates else None
        limit = LIMITS.get(body, DEFAULT_LIMIT)
        age = (TODAY - date.fromisoformat(latest)).days if latest else None
        stale = latest is None or (not future and age is not None and age > limit)
        report[body] = {"last_meeting": past[-1] if past else None,
                        "next_meeting": future[0] if future else None,
                        "days_quiet": max(age, 0) if age is not None else None,
                        "limit_days": limit, "stale": stale}
        if stale:
            warnings += 1
            print(f"::warning title=No new {body} meetings::"
                  f"Last {body} meeting captured: {latest or 'never'} "
                  f"({age} days ago, expected within {limit}). Check its source page "
                  f"and scraper.")
        else:
            nxt = f", next {future[0]}" if future else ""
            print(f"  ok  {body:45} last {past[-1] if past else '—'}{nxt}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"checked": TODAY.isoformat(), "bodies": report}, indent=2),
                   encoding="utf-8")
    print(f"{warnings} source(s) gone quiet")


if __name__ == "__main__":
    main()
