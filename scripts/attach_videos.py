#!/usr/bin/env python3
"""
attach_videos.py — final YouTube matching pass across council and every
township board, run after all the record updaters.

update_meetings.py and update_boards.py already attach videos whose title
names the body and the date. That misses most of the channel's history:

  * Combined sessions: "Council Meeting including Committee of Adjustment
    March 29, 2022" belongs to both council and the committee.
  * Next-day uploads: older recordings with no date in the title ("Council
    Meeting", "Recreation Committee Meeting") were published the morning
    after, so the upload date is one day late.
  * Untitled streams: "Township of Nipissing's Zoom Meeting" names neither
    the body nor the date. The previous fallback only tried boards, never
    council.

Rules, in order, each only ever filling an EMPTY video_url:
  1. Title names the body and the date            -> exact match
  2. Title names the body, no date                -> upload day, else the day before
  3. Untitled stream                               -> the one meeting (any body)
                                                      held that day, else the day
                                                      before; skipped if more than
                                                      one meeting is open
External boards (library, health unit, DNSSAB) are never matched — they are
not streamed on the township channel. Reads the cached video list only; the
updaters refresh it earlier in the run.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import youtube

ROOT = Path(__file__).resolve().parents[1]
MEETINGS_FILE = ROOT / "data" / "canonical" / "meetings.json"
BOARDS_FILE = ROOT / "data" / "canonical" / "boards.json"
TODAY = date.today()

# Boards never streamed on the township channel: no untitled-stream guesses.
NO_UNTITLED = {"pnc"}

# Untitled uploads that are not meetings.
NOT_MEETINGS = ("tender", "open house", "town hall", "strategic plan", "public open")


def prev_day(iso: str) -> str:
    return (date.fromisoformat(iso) - timedelta(days=1)).isoformat()


def open_meeting(m: Dict[str, Any]) -> bool:
    if m.get("video_url") or m.get("cancelled") or m.get("is_future"):
        return False
    try:
        return date.fromisoformat(m["date"]) <= TODAY
    except (KeyError, ValueError):
        return False


def attach(m: Dict[str, Any], vids: List[Dict[str, Any]], body: str, how: str) -> None:
    vids = sorted(vids, key=lambda v: v.get("published") or "9999")
    m["video_url"] = vids[0]["url"]
    if len(vids) > 1:
        m["videos"] = [{"url": v["url"], "title": v.get("title", "")} for v in vids]
    extra = f" (+{len(vids) - 1} part/upload)" if len(vids) > 1 else ""
    print(f"  {body:10} {m['date']} {m.get('meeting_type', ''):8} <- {how}: {vids[0]['title'][:60]}{extra}")


def main() -> None:
    print("=" * 60)
    print("Nipissing attach_videos.py")
    print("=" * 60)
    cache = json.loads((ROOT / youtube.CACHE).read_text(encoding="utf-8")) \
        if (ROOT / youtube.CACHE).exists() else {}
    meetings_payload = json.loads(MEETINGS_FILE.read_text(encoding="utf-8"))
    council = meetings_payload["meetings"] if isinstance(meetings_payload, dict) else meetings_payload
    boards_payload = json.loads(BOARDS_FILE.read_text(encoding="utf-8"))

    by_body: Dict[str, List[Dict[str, Any]]] = {"council": council}
    for b in boards_payload.get("boards", []):
        if not b.get("external"):
            by_body[b["id"]] = b.get("meetings", [])

    vids = [v for v in cache.values() if v.get("url") and v.get("title")]
    for v in vids:
        v["_bodies"] = youtube.classify_bodies(v["title"])
    used: Dict[str, set] = {body: {m.get("video_url") for m in ms if m.get("video_url")}
                            for body, ms in by_body.items()}
    for ms in by_body.values():
        for m in ms:
            for x in m.get("videos") or []:
                used.setdefault(m.get("body_id") or "council", set()).add(x.get("url"))
    filled = {"titled": 0, "upload-day": 0, "untitled": 0}

    # Rules 1 + 2: labelled videos
    for body, ms in by_body.items():
        labelled = [v for v in vids if body in v["_bodies"]]
        if not labelled:
            continue
        for m in ms:
            if not open_meeting(m):
                continue
            free = [v for v in labelled if v["url"] not in used[body]]
            same_day = [x for x in ms if x.get("date") == m["date"]]
            exact = [v for v in free if v.get("title_date") == m["date"]]
            how = "titled"
            if not exact:
                exact = [v for v in free if not v.get("title_date")
                         and v.get("local_date") in (m["date"], (date.fromisoformat(m["date"])
                                                                 + timedelta(days=1)).isoformat())]
                how = "upload-day"
            if len(same_day) > 1 and exact:
                want_special = m.get("meeting_type") == "Special"
                exact = [v for v in exact if bool(v.get("special")) == want_special]
            if not exact:
                continue
            if how == "upload-day":
                # Prefer an upload on the day itself over the day after.
                today_ = [v for v in exact if v.get("local_date") == m["date"]]
                exact = today_ or exact
            attach(m, exact, body, how)
            used[body].update(v["url"] for v in exact)
            filled[how] += 1

    # Rule 3: untitled streams, across all bodies (council included)
    all_used = set().union(*used.values()) if used else set()
    untitled: Dict[str, List[Dict[str, Any]]] = {}
    for v in vids:
        t = v["title"].lower()
        if v["_bodies"] or not v.get("local_date") or v["url"] in all_used:
            continue
        if "zoom meeting" not in t or any(w in t for w in NOT_MEETINGS):
            continue
        untitled.setdefault(v["local_date"], []).append(v)

    def open_on(day: str) -> List[Tuple[str, Dict[str, Any]]]:
        return [(body, m) for body, ms in by_body.items() for m in ms
                if body not in NO_UNTITLED and m.get("date") == day and open_meeting(m)]

    for day in sorted(untitled):
        cands = open_on(day)
        when = "same day"
        if not cands and prev_day(day) not in untitled:
            cands, when = open_on(prev_day(day)), "day before"
        if len(cands) == 1:
            body, m = cands[0]
            attach(m, untitled[day], body, f"untitled stream, {when}")
            filled["untitled"] += 1
        elif len(cands) > 1:
            print(f"  ? {day}: untitled stream, {len(cands)} meetings open "
                  f"({', '.join(b for b, _ in cands)}) — left unmatched")

    if any(filled.values()):
        MEETINGS_FILE.write_text(json.dumps(meetings_payload, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
        BOARDS_FILE.write_text(json.dumps(boards_payload, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    print(f"Filled: {filled}")
    missing = {body: sum(1 for m in ms if open_meeting(m) and m["date"] >= "2022")
               for body, ms in by_body.items()}
    print(f"Still without video (2022+): {missing}")


if __name__ == "__main__":
    main()
