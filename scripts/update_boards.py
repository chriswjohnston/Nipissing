#!/usr/bin/env python3
"""
update_boards.py

Forward-looking board / committee meetings updater for the Nipissing public records repo.

Sources:
- Recreation Committee
- Museum Board
- Cemetery Committee

What this does:
- Scrapes the current Township pages
- Captures meeting dates even when links are missing
- Normalizes all board meetings into one canonical boards.json file
- Preserves existing summaries/events/history already in canonical data
- Avoids historical rebuild complexity from the old scraper
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIR = ROOT / "data" / "canonical"

BOARDS_FILE = CANONICAL_DIR / "boards.json"

HEADERS = {
    "User-Agent": "nipissing-public-records/1.0 (board updater)"
}

TODAY = date.today()

BOARDS = [
    {
        "id": "recreation",
        "name": "Recreation Committee",
        "url": "https://nipissingtownship.com/services/recreation/",
        "description": "Management of recreational programming and the Community Centre at 2381 Highway 654.",
    },
    {
        "id": "museum",
        "name": "Museum Board",
        "url": "https://nipissingtownship.com/services/museum-services-and-information/",
        "description": "Preservation and display of the history and heritage of Nipissing Township.",
    },
    {
        "id": "cemetery",
        "name": "Cemetery Committee",
        "url": "https://nipissingtownship.com/services/cemetery/",
        "description": "Administration of local cemeteries in Nipissing Township.",
    },
]

DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return deepcopy(default)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_absolute_url(href: str) -> str:
    return urljoin("https://nipissingtownship.com", href.strip())


def clean_label(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = text.strip("()[]")
    return text or "Document"


def parse_date_match(match: re.Match[str]) -> date:
    return datetime.strptime(
        f"{match.group(1).capitalize()} {int(match.group(2))}, {match.group(3)}",
        "%B %d, %Y",
    ).date()


def format_display_date(dt: date) -> str:
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def classify_link(label: str, href: str) -> str:
    text = f"{label} {href}".lower()
    if "package" in text:
        return "package"
    if "minute" in text:
        return "minutes"
    if "agenda" in text:
        return "agenda"
    return "other"


def dedupe_docs(docs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for doc in docs:
        label = clean_label(doc.get("label", ""))
        url = (doc.get("url") or "").strip()
        if not url:
            continue
        key = (label, url)
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "url": url})
    return out


def board_meeting_key(meeting: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        meeting.get("board_id", ""),
        meeting.get("date", ""),
        meeting.get("meeting_type", "Board") or "Board",
    )


# ---------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------

def extract_meetings_from_content(content: Tag, board: Dict[str, str]) -> List[Dict[str, Any]]:
    """
    DOM-order parser for board pages.
    Captures date lines plus any links following them in the same container.
    """
    meetings: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    def flush_current() -> None:
        nonlocal current
        if current is None:
            return

        current["extra_docs"] = dedupe_docs(current.pop("extra_docs_raw", []))
        meetings.append(current)
        current = None

    def start_meeting(dt: date, context_text: str) -> None:
        nonlocal current
        flush_current()

        ctx = (context_text or "").lower()
        cancelled = "cancel" in ctx
        rescheduled = "reschedul" in ctx
        postponed = "postpon" in ctx

        current = {
            "date": dt.isoformat(),
            "display_date": format_display_date(dt),
            "year": dt.year,
            "meeting_type": "Board",
            "title": board["name"],
            "board_id": board["id"],
            "board_name": board["name"],
            "body": board["name"],
            "body_id": board["id"],
            "source_kind": "board",
            "agenda_url": None,
            "minutes_url": None,
            "package_url": None,
            "extra_docs_raw": [],
            "summary": None,
            "events": [],
            "cancelled": cancelled,
            "rescheduled": rescheduled,
            "postponed": postponed,
            "is_future": dt > TODAY,
        }

    def process_container(el: Tag) -> None:
        nonlocal current

        text = " ".join(el.stripped_strings)
        if not text:
            return

        dm = DATE_RE.search(text)
        if dm:
            dt = parse_date_match(dm)
            start_meeting(dt, text)

        if current is None:
            return

        for a in el.find_all("a", href=True):
            href = normalize_absolute_url(a["href"])
            label = clean_label(a.get_text(" ", strip=True))
            kind = classify_link(label, href)

            if kind == "agenda" and not current["agenda_url"]:
                current["agenda_url"] = href
            elif kind == "minutes" and not current["minutes_url"]:
                current["minutes_url"] = href
            elif kind == "package" and not current["package_url"]:
                current["package_url"] = href
            elif kind == "other":
                current["extra_docs_raw"].append({"label": label, "url": href})

    containers = content.find_all(["p", "li", "div", "tr"])

    for el in containers:
        text = " ".join(el.stripped_strings)
        if not text:
            continue
        if DATE_RE.search(text) or el.find("a", href=True):
            process_container(el)

    flush_current()

    # Merge duplicates by board+date+type
    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for m in meetings:
        k = board_meeting_key(m)
        if k not in merged:
            merged[k] = deepcopy(m)
            continue

        old = merged[k]
        for field in ("agenda_url", "minutes_url", "package_url"):
            if not old.get(field) and m.get(field):
                old[field] = m[field]

        old["extra_docs"] = dedupe_docs((old.get("extra_docs") or []) + (m.get("extra_docs") or []))
        old["cancelled"] = old.get("cancelled", False) or m.get("cancelled", False)
        old["rescheduled"] = old.get("rescheduled", False) or m.get("rescheduled", False)
        old["postponed"] = old.get("postponed", False) or m.get("postponed", False)

    out = list(merged.values())
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def scrape_board(board: Dict[str, str]) -> List[Dict[str, Any]]:
    print(f"Scraping {board['name']} ...")
    resp = requests.get(board["url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    content = (
        soup.find("div", class_=re.compile(r"entry-content|post-content"))
        or soup.find("article")
        or soup.find("main")
        or soup.body
    )
    if content is None:
        return []

    meetings = extract_meetings_from_content(content, board)
    print(
        f"  {len(meetings)} meeting(s) | "
        f"{sum(1 for m in meetings if m.get('minutes_url'))} with minutes | "
        f"{sum(1 for m in meetings if m.get('is_future'))} future"
    )
    return meetings


# ---------------------------------------------------------------------
# Merge canonical data
# ---------------------------------------------------------------------

def merge_board_meeting(existing: Dict[str, Any], scraped: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(existing)

    for field in (
        "display_date",
        "year",
        "meeting_type",
        "title",
        "board_id",
        "board_name",
        "body",
        "body_id",
        "source_kind",
        "cancelled",
        "rescheduled",
        "postponed",
        "is_future",
    ):
        if scraped.get(field) not in (None, ""):
            out[field] = scraped[field]

    for field in ("agenda_url", "minutes_url", "package_url"):
        if scraped.get(field):
            out[field] = scraped[field]

    out["extra_docs"] = dedupe_docs(
        (existing.get("extra_docs") or []) + (scraped.get("extra_docs") or [])
    )

    if not out.get("summary"):
        out["summary"] = existing.get("summary")

    if not out.get("events"):
        out["events"] = existing.get("events", [])

    return out


def merge_canonical(existing_payload: Dict[str, Any], scraped_by_board: List[Dict[str, Any]]) -> Dict[str, Any]:
    existing_boards = existing_payload.get("boards", [])

    existing_index: Dict[str, Dict[str, Any]] = {}
    for board in existing_boards:
        existing_index[board["id"]] = deepcopy(board)

    scraped_index: Dict[str, Dict[str, Any]] = {}
    for board in scraped_by_board:
        scraped_index[board["id"]] = deepcopy(board)

    result_boards: List[Dict[str, Any]] = []

    all_ids = sorted(set(existing_index.keys()) | set(scraped_index.keys()))
    for board_id in all_ids:
        old_board = existing_index.get(board_id)
        new_board = scraped_index.get(board_id)

        if old_board and new_board:
            merged_board = deepcopy(old_board)
            merged_board["name"] = new_board.get("name", merged_board.get("name"))
            merged_board["url"] = new_board.get("url", merged_board.get("url"))
            merged_board["description"] = new_board.get("description", merged_board.get("description"))

            old_meetings = {board_meeting_key(m): deepcopy(m) for m in old_board.get("meetings", [])}
            new_meetings = {board_meeting_key(m): deepcopy(m) for m in new_board.get("meetings", [])}

            merged_meetings: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

            for k, scraped_m in new_meetings.items():
                if k in old_meetings:
                    merged_meetings[k] = merge_board_meeting(old_meetings[k], scraped_m)
                else:
                    nm = deepcopy(scraped_m)
                    nm.setdefault("summary", None)
                    nm.setdefault("events", [])
                    nm["extra_docs"] = dedupe_docs(nm.get("extra_docs") or [])
                    merged_meetings[k] = nm

            for k, old_m in old_meetings.items():
                if k not in merged_meetings:
                    merged_meetings[k] = deepcopy(old_m)

            merged_board["meetings"] = sorted(
                list(merged_meetings.values()),
                key=lambda x: x.get("date", ""),
                reverse=True,
            )
            result_boards.append(merged_board)

        elif new_board:
            nb = deepcopy(new_board)
            nb["meetings"] = sorted(nb.get("meetings", []), key=lambda x: x.get("date", ""), reverse=True)
            result_boards.append(nb)

        elif old_board:
            result_boards.append(deepcopy(old_board))

    return {
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "source": "board pages",
        "boards": result_boards,
    }


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    current_payload = load_json(BOARDS_FILE, {"boards": []})

    print("=" * 60)
    print("Nipissing update_boards.py")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Existing canonical boards: {len(current_payload.get('boards', []))}")
    print("=" * 60)

    scraped_boards = []
    for board in BOARDS:
        meetings = scrape_board(board)
        scraped_boards.append({
            "id": board["id"],
            "name": board["name"],
            "url": board["url"],
            "description": board["description"],
            "meetings": meetings,
        })

    merged_payload = merge_canonical(current_payload, scraped_boards)
    save_json(BOARDS_FILE, merged_payload)

    total_meetings = sum(len(b.get("meetings", [])) for b in merged_payload["boards"])
    total_minutes = sum(
        1 for b in merged_payload["boards"] for m in b.get("meetings", [])
        if m.get("minutes_url")
    )
    total_future = sum(
        1 for b in merged_payload["boards"] for m in b.get("meetings", [])
        if m.get("is_future")
    )

    print(f"Saved boards -> {BOARDS_FILE}")
    print(f"  boards:   {len(merged_payload['boards'])}")
    print(f"  meetings: {total_meetings}")
    print(f"  minutes:  {total_minutes}")
    print(f"  future:   {total_future}")


if __name__ == "__main__":
    main()
