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
- Attaches YouTube recordings via the shared youtube.py cache

Page layout (2026): each board page lists meetings in a WordPress table, one
row per meeting (date in the first cell, document links after it). The
cemetery page splits Agendas and Minutes into two tables; rows are joined on
date. Only dated table rows are parsed — other links on the page (by-laws,
registration forms, contact email) are stored once at board level as
page_docs, never on a meeting. If a page ever loses its tables, the older
paragraph walker is used as a fallback.

History: on 2026-06-23 this file was accidentally overwritten with a copy of
update_meetings.py, which froze boards.json. Restored from commit a835a9a.
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

from archive import mirror_record
import youtube

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIR = ROOT / "data" / "canonical"

BOARDS_FILE = CANONICAL_DIR / "boards.json"
MEETINGS_FILE = CANONICAL_DIR / "meetings.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def fetch_html(url: str, timeout: int = 30) -> str:
    """Fetch a page, tolerating the township's trailing-slash changes.
    Tries the URL as given, then with the slash toggled. Raises only if all fail."""
    candidates = [url, url.rstrip("/") if url.endswith("/") else url + "/"]
    last_exc: Optional[Exception] = None
    for candidate in candidates:
        try:
            resp = requests.get(candidate, headers=HEADERS,
                                timeout=timeout, allow_redirects=True)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.HTTPError as e:
            last_exc = e
            if e.response is not None and e.response.status_code == 404:
                continue          # try the other slash form
            raise                 # 500/etc. is a real failure, surface it
    raise last_exc


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
        "url": "https://nipissingtownship.com/services/museum/",
        "description": "Preservation and display of the history and heritage of Nipissing Township.",
    },
    {
        "id": "cemetery",
        "name": "Cemetery Committee",
        "url": "https://nipissingtownship.com/services/cemetery/",
        "description": "Administration of local cemeteries in Nipissing Township.",
    },
    {
        # Joint board with Powassan and Callander; Nipissing hosts the files.
        # 2025-26 meetings are in tables, 2024 is a bulleted list.
        "id": "pnc",
        "name": "PNC OPP Detachment Board",
        "url": "https://nipissingtownship.com/services/pnc-detachment-board/",
        "description": (
            "Powassan Nipissing Callander O.P.P. Detachment Board (local police "
            "services board), established under the Community Safety and Policing Act."
        ),
        "list_meetings": True,
        "all_agendas": True,
    },
]

DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)

# Records that are NOT meetings and must never be carried forward.
# (board_id, date) -> reason. A tombstone only removes a record that has no
# agenda, minutes or package, so a real meeting can never be deleted by it.
TOMBSTONES: Dict[Tuple[str, str], str] = {
    # The old museum-services page listed public events; the previous
    # paragraph walker saved each event date as a board meeting.
    ("museum", "2026-07-02"): "museum event, not a board meeting",
    ("museum", "2026-07-04"): "museum event (Butter Making)",
    ("museum", "2026-07-18"): "museum event (Ice Cream Making / Heritage Day)",
    ("museum", "2026-08-02"): "museum event (Heritage Day Craft Fair)",
    ("museum", "2026-08-13"): "museum event (Rope Coaster Craft)",
    ("museum", "2026-08-15"): "museum event (Rope Basket Making)",
    ("museum", "2026-05-06"): "placeholder; the May meeting was held May 13",
}


def apply_tombstones(boards: List[Dict[str, Any]]) -> None:
    for b in boards:
        kept = []
        for m in b.get("meetings", []):
            reason = TOMBSTONES.get((b.get("id", ""), m.get("date", "")))
            has_docs = any(m.get(f) for f in ("agenda_url", "minutes_url", "package_url"))
            if reason and not has_docs:
                print(f"  removed {b['id']} {m['date']}: {reason}")
                continue
            if reason and has_docs:
                print(f"  tombstone ignored for {b['id']} {m['date']}: record has documents")
            kept.append(m)
        b["meetings"] = kept


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
            "cancelled": "cancel" in ctx,
            "rescheduled": "reschedul" in ctx,
            "postponed": "postpon" in ctx,
            "is_future": dt > TODAY,
        }

    def process_segment(text: str, links: List[Tag]) -> None:
        """Process one logical line (br-separated segment or standalone element)."""
        nonlocal current
        dm = DATE_RE.search(text)
        if dm:
            start_meeting(parse_date_match(dm), text)

        if current is None:
            return

        for a in links:
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

    def split_by_br(el: Tag) -> List[Tuple[str, List[Tag]]]:
        """
        Split a tag's contents on <br> boundaries.
        Returns list of (text, [anchor_tags]) per segment.
        """
        segments: List[Tuple[str, List[Tag]]] = []
        current_text_parts: List[str] = []
        current_links: List[Tag] = []

        for child in el.children:
            if isinstance(child, NavigableString):
                current_text_parts.append(str(child))
            elif isinstance(child, Tag):
                if child.name == "br":
                    segments.append((" ".join(current_text_parts), current_links))
                    current_text_parts = []
                    current_links = []
                elif child.name == "a" and child.get("href"):
                    current_text_parts.append(child.get_text(" ", strip=True))
                    current_links.append(child)
                else:
                    # Recurse into inline elements (span, strong, em, etc.)
                    current_text_parts.append(child.get_text(" ", strip=True))
                    current_links.extend(child.find_all("a", href=True))

        # Don't forget the last segment after the final <br> (or if no <br> at all)
        segments.append((" ".join(current_text_parts), current_links))
        return segments

    # Walk top-level block containers
    containers = content.find_all(["p", "li", "div", "tr"])
    for el in containers:
        # Check if this element has <br> tags — if so, split it
        if el.find("br"):
            for seg_text, seg_links in split_by_br(el):
                seg_text = re.sub(r"\s+", " ", seg_text).strip()
                if seg_text or seg_links:
                    process_segment(seg_text, seg_links)
        else:
            # Original behaviour for non-br elements
            text = re.sub(r"\s+", " ", " ".join(el.stripped_strings)).strip()
            if not text:
                continue
            if DATE_RE.search(text) or el.find("a", href=True):
                process_segment(text, list(el.find_all("a", href=True)))

    flush_current()

    # Merge duplicates
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
        old["extra_docs"] = dedupe_docs(
            (old.get("extra_docs") or []) + (m.get("extra_docs") or [])
        )
        old["cancelled"] = old.get("cancelled", False) or m.get("cancelled", False)
        old["rescheduled"] = old.get("rescheduled", False) or m.get("rescheduled", False)
        old["postponed"] = old.get("postponed", False) or m.get("postponed", False)

    out = list(merged.values())
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def _new_meeting(dt: date, board: Dict[str, str], context_text: str) -> Dict[str, Any]:
    ctx = (context_text or "").lower()
    return {
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
        "extra_docs": [],
        "summary": None,
        "events": [],
        "cancelled": "cancel" in ctx,
        "rescheduled": "reschedul" in ctx,
        "postponed": "postpon" in ctx,
        "is_future": dt > TODAY,
    }


def dated_rows(content: Tag) -> List[Tuple[date, Tag]]:
    """Table rows whose first cell holds a meeting date."""
    rows = []
    for tr in content.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        dm = DATE_RE.search(cells[0].get_text(" ", strip=True))
        if dm:
            rows.append((parse_date_match(dm), tr))
    return rows


def extract_meetings_from_tables(rows: List[Tuple[date, Tag]],
                                 board: Dict[str, str]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for dt, tr in rows:
        text = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))
        m = merged.get(dt.isoformat())
        if m is None:
            m = merged[dt.isoformat()] = _new_meeting(dt, board, text)
        else:  # same date in a second table (cemetery: Agendas + Minutes)
            fresh = _new_meeting(dt, board, text)
            for flag in ("cancelled", "rescheduled", "postponed"):
                m[flag] = m[flag] or fresh[flag]
        for a in tr.find_all("a", href=True):
            href = normalize_absolute_url(a["href"])
            if not href.lower().startswith("http"):
                continue
            label = clean_label(a.get_text(" ", strip=True))
            kind = classify_link(label, href)
            if kind in ("agenda", "minutes", "package"):
                field = f"{kind}_url"
                if not m[field]:
                    m[field] = href
                    continue
            m["extra_docs"].append({"label": label, "url": href})
    out = list(merged.values())
    for m in out:
        m["extra_docs"] = dedupe_docs(m["extra_docs"])
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def extract_meetings_from_list_items(content: Tag, board: Dict[str, str],
                                     skip_dates: set) -> Tuple[List[Dict[str, Any]], List[Tag]]:
    """Meetings written as bullet points ("September 23, 2024 – Agenda | Minutes").
    Only <li> elements outside tables whose own text holds a date are used,
    and only the links inside that <li>. Returns (meetings, li_elements)."""
    merged: Dict[str, Dict[str, Any]] = {}
    used: List[Tag] = []
    for li in content.find_all("li"):
        if li.find_parent("table") or li.find_parent("nav"):
            continue
        text = re.sub(r"\s+", " ", li.get_text(" ", strip=True))
        dm = DATE_RE.search(text)
        if not dm or len(text) > 300:
            continue
        dt = parse_date_match(dm)
        if dt.isoformat() in skip_dates:
            continue
        m = merged.get(dt.isoformat())
        if m is None:
            m = merged[dt.isoformat()] = _new_meeting(dt, board, text)
        used.append(li)
        for a in li.find_all("a", href=True):
            href = normalize_absolute_url(a["href"])
            if not href.lower().startswith("http"):
                continue
            label = clean_label(a.get_text(" ", strip=True))
            kind = classify_link(label, href)
            if kind in ("agenda", "minutes", "package") and not m[f"{kind}_url"]:
                m[f"{kind}_url"] = href
            else:
                m["extra_docs"].append({"label": label, "url": href})
    out = list(merged.values())
    for m in out:
        m["extra_docs"] = dedupe_docs(m["extra_docs"])
    return out, used


def page_level_docs(content: Tag, rows: List[Tuple[date, Tag]]) -> List[Dict[str, str]]:
    """PDF links on the page that are NOT inside a dated meeting row
    (by-laws, forms). mailto/tel links are dropped."""
    in_rows = {id(a) for _, tr in rows for a in tr.find_all("a", href=True)}
    docs = []
    for a in content.find_all("a", href=True):
        if id(a) in in_rows:
            continue
        href = normalize_absolute_url(a["href"])
        if href.lower().startswith("http") and ".pdf" in href.lower():
            docs.append({"label": clean_label(a.get_text(" ", strip=True)), "url": href})
    return dedupe_docs(docs)


def _url_key(url: str) -> str:
    """Compare URLs ignoring http/https (older records stored http://)."""
    return re.sub(r"^https?://", "", (url or "").strip().lower())


def sanitize_board_extra_docs(board: Dict[str, Any]) -> None:
    """Strip page-level and junk links that older runs attached to meetings.
    Drops mailto/non-http links, anything duplicating a meeting's primary
    agenda/minutes/package URL, and anything listed in the board's page_docs.
    Genuine per-meeting extras (e.g. 'Additional Information') are kept."""
    meetings = board.get("meetings", [])
    primary = {_url_key(m.get(f)) for m in meetings
               for f in ("agenda_url", "minutes_url", "package_url") if m.get(f)}

    # A document attached to more than one meeting is a page-level link that
    # leaked in (by-laws, registration forms) — no real meeting document is
    # shared between meetings. Move it to page_docs.
    seen: Dict[str, int] = {}
    for m in meetings:
        for k in {_url_key(d.get("url")) for d in m.get("extra_docs") or []}:
            seen[k] = seen.get(k, 0) + 1
    leaked = [d for m in meetings for d in m.get("extra_docs") or []
              if seen.get(_url_key(d.get("url")), 0) > 1
              and (d.get("url") or "").lower().startswith("http")]
    if leaked:
        board["page_docs"] = dedupe_docs((board.get("page_docs") or []) + leaked)

    page = {_url_key(d["url"]) for d in board.get("page_docs", [])}
    for m in meetings:
        kept = []
        for d in m.get("extra_docs") or []:
            url = (d.get("url") or "").strip()
            k = _url_key(url)
            if not url.lower().startswith("http") or k in primary or k in page:
                continue
            kept.append(d)
        m["extra_docs"] = kept


def scrape_board(board: Dict[str, str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    print(f"Scraping {board['name']} ...")
    soup = BeautifulSoup(fetch_html(board["url"]), "html.parser")
    content = (
        soup.find("div", class_=re.compile(r"entry-content|post-content"))
        or soup.find("article")
        or soup.find("main")
        or soup.body
    )
    if content is None:
        return [], []

    rows = dated_rows(content)
    if rows:
        meetings = extract_meetings_from_tables(rows, board)
        if board.get("list_meetings"):
            extra, lis = extract_meetings_from_list_items(
                content, board, {m["date"] for m in meetings})
            if extra:
                print(f"  {len(extra)} more meeting(s) from bulleted lists")
            meetings = sorted(meetings + extra, key=lambda x: x["date"], reverse=True)
            # Links inside those list items are meeting docs, not page docs.
            in_lists = {normalize_absolute_url(a["href"])
                        for li in lis for a in li.find_all("a", href=True)}
            page_docs = [d for d in page_level_docs(content, rows)
                         if d["url"] not in in_lists]
        else:
            page_docs = page_level_docs(content, rows)
    else:
        print("  no dated table rows found — using paragraph fallback")
        meetings = extract_meetings_from_content(content, board)
        page_docs = []
    print(
        f"  {len(meetings)} meeting(s) | "
        f"{sum(1 for m in meetings if m.get('minutes_url'))} with minutes | "
        f"{sum(1 for m in meetings if m.get('is_future'))} future | "
        f"{sum(1 for m in meetings if m.get('cancelled'))} cancelled"
    )
    return meetings, page_docs


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
        "all_agendas",
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
            # Accumulate: a page link seen once stays filtered off meetings
            # even after the township removes it from the page.
            merged_board["page_docs"] = dedupe_docs(
                (old_board.get("page_docs") or []) + (new_board.get("page_docs") or [])
            )

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
        try:
            meetings, page_docs = scrape_board(board)
        except Exception as e:
            print(f"  ✗ {board['name']} failed ({e}) — keeping stored canonical data")
            continue  # merge_canonical carries the existing board forward untouched
        scraped_boards.append({
            "id": board["id"],
            "name": board["name"],
            "url": board["url"],
            "description": board["description"],
            "page_docs": page_docs,
            "meetings": meetings,
        })
        if board.get("all_agendas"):
            for m in meetings:
                m["all_agendas"] = True

    merged_payload = merge_canonical(current_payload, scraped_boards)

    apply_tombstones(merged_payload["boards"])

    for b in merged_payload["boards"]:
        sanitize_board_extra_docs(b)
        for m in b.get("meetings", []):
            # Carried-forward records keep a stale is_future otherwise.
            try:
                m["is_future"] = date.fromisoformat(m["date"]) > TODAY
            except (KeyError, ValueError):
                pass
            m.setdefault("video_url", None)
            # Minutes exist, so the meeting happened. Older runs leaked the
            # word "Cancelled" from another row onto the first meeting of a
            # table (e.g. recreation 2026-01-19, 2025-01-06).
            if m.get("cancelled") and m.get("minutes_url"):
                print(f"  un-cancelled {b['id']} {m['date']}: has minutes")
                m["cancelled"] = False

    # YouTube: title-labelled videos first, then untitled streams
    # ("Township of Nipissing's Zoom Meeting") matched by stream date.
    # External boards (e.g. the library board, written by update_library.py)
    # are never streamed on the township channel; keep them out of matching so
    # an untitled township stream can't be attached to them by date.
    cache = youtube.refresh()
    by_body = {b["id"]: b.get("meetings", []) for b in merged_payload["boards"]
               if not b.get("external")}
    for body_id, meetings in by_body.items():
        youtube.attach_labelled(meetings, cache, body_id)
    council = load_json(MEETINGS_FILE, {"meetings": []})
    council_list = council if isinstance(council, list) else council.get("meetings", [])
    youtube.attach_unlabelled(by_body, cache, {m.get("date") for m in council_list})

    # Mirror township PDFs (minutes/agendas in-repo, packages to Releases)
    # before saving, so archived_* paths land in canonical data.
    for b in merged_payload["boards"]:
        if b.get("external"):
            continue  # joint boards are mirrored by their own scripts (DNSSAB needs a session)
        for m in b.get("meetings", []):
            mirror_record(m, f"boards/{b['id']}")

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
