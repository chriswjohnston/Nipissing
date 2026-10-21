#!/usr/bin/env python3
"""
recover_documents.py — bounded, one-time attempt to recover deleted minutes.

The township's June 2026 relaunch deleted older minutes before the archive
had copies of some of them (2024 recreation and cemetery minutes, a few
council meetings). The Wayback Machine sometimes saved individual PDFs, so
this asks it once for each meeting that has a township minutes link but no
archived copy.

Bounded on purpose:
  * each URL is tried once; only throttling/timeouts are retried
  * 10 minutes per run at most
  * stops entirely after doc_sources.RECOVERY_UNTIL; delete this file then
    (remove its line from .github/workflows/update-records.yml too)

Recovered files are archived like any other, so the summary step can read
them. Results are remembered in data/runtime/recovery.json.
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import doc_sources as ds
from archive import ARCHIVE_MIN_YEAR, is_township_pdf, store_bytes_in_repo

ROOT = Path(__file__).resolve().parents[1]
MEETINGS_FILE = ROOT / "data" / "canonical" / "meetings.json"
BOARDS_FILE = ROOT / "data" / "canonical" / "boards.json"
STATE_FILE = ROOT / "data" / "runtime" / "recovery.json"
BUDGET_SECONDS = 10 * 60
TODAY = date.today().isoformat()


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def main() -> None:
    print("=" * 60)
    print("Nipissing recover_documents.py")
    print("=" * 60)
    if not ds.recovery_active():
        print(f"Recovery ended {ds.RECOVERY_UNTIL}. This script can be deleted.")
        return

    meetings_payload = load(MEETINGS_FILE, {"meetings": []})
    boards_payload = load(BOARDS_FILE, {"boards": []})
    state = load(STATE_FILE, {})

    # (category, meeting) for every council / township-board meeting whose
    # minutes are a township PDF with no archived copy.
    todo = [("meetings", m) for m in meetings_payload.get("meetings", [])]
    for b in boards_payload.get("boards", []):
        if not b.get("external"):
            todo += [(f"boards/{b['id']}", m) for m in b.get("meetings", [])]
    todo = [(cat, m) for cat, m in todo
            if is_township_pdf(m.get("minutes_url")) and not m.get("minutes_archived")
            and int(m.get("year") or 0) >= ARCHIVE_MIN_YEAR]
    pending = [(c, m) for c, m in todo if state.get(m["minutes_url"], {}).get("outcome")
               not in ("recovered", "notfound", "notpdf")]
    print(f"{len(todo)} meeting(s) missing archived minutes; {len(pending)} not yet tried")

    deadline = time.monotonic() + BUDGET_SECONDS
    recovered = changed = 0
    for cat, m in pending:
        if time.monotonic() > deadline:
            print("  out of time — continuing next run")
            break
        url = m["minutes_url"]
        data, outcome = ds.wayback_fetch(url, "2")      # "2" = nearest capture
        state[url] = {"outcome": "recovered" if data else outcome, "checked": TODAY}
        if data:
            got = store_bytes_in_repo(url, cat, int(m["year"]), data)
            if got:
                m["minutes_archived"] = got
                recovered += 1
                changed += 1
                print(f"  recovered {cat} {m['date']}")
        else:
            print(f"  not in Wayback ({outcome}): {cat} {m['date']}")

    if changed:
        MEETINGS_FILE.write_text(json.dumps(meetings_payload, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
        BOARDS_FILE.write_text(json.dumps(boards_payload, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    gone = sum(1 for v in state.values() if v["outcome"] in ("notfound", "notpdf"))
    print(f"Recovered {recovered} this run | {gone} confirmed unrecoverable overall")


if __name__ == "__main__":
    main()
