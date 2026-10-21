#!/usr/bin/env python3
"""
audit_site.py — a health check of everything nipissing.news shows.

Read-only. Looks for, per body:
  * meetings with no minutes / agenda (past meetings only; recent meetings get
    a grace period because minutes are approved at the next meeting);
  * meetings from 2022 on with minutes but no AI summary;
  * duplicate documents on a meeting (same file at another URL: http/https,
    a re-upload in another month's folder, WordPress "-1" copies);
  * extra documents that repeat the meeting's own agenda/minutes/package;
  * archived copies referenced but missing from the repo;
  * meetings without a video, for bodies that stream;
and for by-laws: no document at all.

Writes data/runtime/audit.json and prints a summary.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List

from doc_tidy import doc_key

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "data" / "canonical"
DOCS = ROOT / "docs"
OUT = ROOT / "data" / "runtime" / "audit.json"

MIN_YEAR = 2022
MINUTES_GRACE = timedelta(days=45)
# Bodies whose meetings are streamed to the township's YouTube channel.
STREAMED = {"council", "adjustment", "museum", "cemetery", "recreation"}
PRIMARY = ("agenda_url", "minutes_url", "package_url")


def load(name: str) -> Any:
    return json.loads((CANON / name).read_text(encoding="utf-8"))


def archived_paths(m: Dict[str, Any]) -> List[str]:
    out = [m.get(k) for k in ("agenda_archived", "minutes_archived", "package_archived")]
    out += [d.get("archived") for d in m.get("extra_docs") or []]
    return [p for p in out if p and not p.startswith("http")]


def audit_meeting(body: str, m: Dict[str, Any], today: date) -> Dict[str, Any]:
    issues: Dict[str, Any] = {}
    d = m.get("date") or ""
    try:
        when = date.fromisoformat(d[:10])
    except ValueError:
        return {"bad_date": d}
    if m.get("cancelled") or when > today:
        return issues
    has_min = bool(m.get("minutes_url") or m.get("minutes_archived"))
    if not has_min and today - when > MINUTES_GRACE:
        issues["no_minutes"] = True
    if not (m.get("agenda_url") or m.get("agenda_archived") or m.get("package_url")):
        issues["no_agenda"] = True
    if when.year >= MIN_YEAR and has_min and not m.get("summary"):
        issues["no_summary"] = True
    if body in STREAMED and when.year >= MIN_YEAR and not m.get("video_url"):
        issues["no_video"] = True

    extras = m.get("extra_docs") or []
    keys = Counter(doc_key(x.get("url") or "") for x in extras)
    dups = sorted(k for k, n in keys.items() if n > 1)
    if dups:
        issues["duplicate_docs"] = {"count": sum(keys[k] - 1 for k in dups), "files": dups}
    prim = {doc_key(m.get(f) or "") for f in PRIMARY if m.get(f)}
    rep = [x.get("label") for x in extras if doc_key(x.get("url") or "") in prim]
    if rep:
        issues["extra_repeats_primary"] = rep
    missing = [p for p in archived_paths(m) if not (DOCS / p).exists()]
    if missing:
        issues["archive_file_missing"] = missing
    return issues


def main() -> None:
    today = date.today()
    meetings = load("meetings.json")["meetings"]
    boards = load("boards.json")["boards"]
    bodies: Dict[str, List[Dict[str, Any]]] = {"council": meetings}
    for b in boards:
        bodies[b["id"]] = b.get("meetings") or []

    report: Dict[str, Any] = {"checked": today.isoformat(), "bodies": {}, "bylaws": {}}
    print(f"{'body':45} {'mtgs':>4} " + " ".join(f"{h:>6}" for h in
          ("no-min", "no-agd", "no-sum", "no-vid", "dupdoc", "repeat", "nofile")))
    for body, ms in bodies.items():
        rows = {}
        c = Counter()
        for m in ms:
            iss = audit_meeting(body, m, today)
            if iss:
                rows[f"{m.get('date')} {m.get('meeting_type') or ''}".strip()] = iss
                for k, v in iss.items():
                    c[k] += v["count"] if k == "duplicate_docs" else 1
        report["bodies"][body] = {"meetings": len(ms), "totals": dict(c), "issues": rows}
        print(f"{body:45} {len(ms):>4} " + " ".join(f"{c[k]:>6}" for k in (
            "no_minutes", "no_agenda", "no_summary", "no_video", "duplicate_docs",
            "extra_repeats_primary", "archive_file_missing")))

    bylaws = load("bylaws.json")["bylaws"]
    nodoc = [b["number"] for b in bylaws if not (b.get("pdf_url") or b.get("extracted_pdf"))]
    by_year = defaultdict(list)
    for n in nodoc:
        by_year[n[:4]].append(n)
    report["bylaws"] = {"total": len(bylaws), "no_document": len(nodoc),
                        "no_document_by_year": {y: sorted(v) for y, v in sorted(by_year.items())}}
    print(f"\nby-laws: {len(bylaws)} total, {len(nodoc)} with no document: "
          + ", ".join(f"{y}: {len(v)}" for y, v in sorted(by_year.items())))
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    write_step_summary(report)


def write_step_summary(report: Dict[str, Any]) -> None:
    """A table on the run's summary page (GitHub Actions)."""
    import os
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    cols = [("no_minutes", "No minutes"), ("no_agenda", "No agenda"), ("no_summary", "No summary"),
            ("no_video", "No video"), ("duplicate_docs", "Duplicate docs"),
            ("archive_file_missing", "Archive file missing")]
    lines = ["## Site audit", "", "| Body | Meetings | " + " | ".join(c[1] for c in cols) + " |",
             "|---|---:|" + "---:|" * len(cols)]
    for body, v in report["bodies"].items():
        t = v["totals"]
        lines.append(f"| {body} | {v['meetings']} | " + " | ".join(str(t.get(c[0], 0) or "") for c in cols) + " |")
    b = report["bylaws"]
    lines += ["", f"By-laws: {b['total']} total, {b['no_document']} with no document.",
              "", "Details: `data/runtime/audit.json`"]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
