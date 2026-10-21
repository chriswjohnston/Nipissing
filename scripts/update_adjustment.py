#!/usr/bin/env python3
"""
update_adjustment.py

Committee of Adjustment (consents and minor variances).

The township page only lists the current year, and older documents have
been removed from it, so this pulls from three places:

  1. The live page — meetings under date headings, each followed by
     Agenda / Minutes / Public Notice / Planning Report / Decision links.
  2. The WordPress media index — finds earlier agendas, minutes, notices and
     decisions that are still on the server but no longer linked.
  3. The Wayback Machine — the only copy of files that were deleted.

Every document found is mirrored. Agendas are kept for all years (they are
the main public record of what the committee heard), unlike council where
agendas are only shown from 2026.

Dates: filenames mislead ("Minutes-COA-July-15-2025.pdf" holds the June 10
minutes), so agendas and minutes are dated from the PDF itself: the filename
date is used only if it also appears on the first page.

State: data/runtime/adjustment_docs.json remembers every file already
examined, so each one is downloaded once. Permanent state — do not delete.
Runs before update_boards.py, which attaches YouTube recordings by date.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, Tag

import doc_sources as ds
from archive import ARCHIVE_MIN_YEAR, mirror_record, store_bytes_in_repo
from update_boards import (_new_meeting, clean_label, dedupe_docs, fetch_html,
                           normalize_absolute_url)

ROOT = Path(__file__).resolve().parents[1]
BOARDS_FILE = ROOT / "data" / "canonical" / "boards.json"
STATE_FILE = ROOT / "data" / "runtime" / "adjustment_docs.json"
TODAY = date.today()

BOARD = {
    "id": "adjustment",
    "name": "Committee of Adjustment",
    "url": "https://nipissingtownship.com/municipal-information/committee-of-adjustment/",
    "description": (
        "Hears applications for consent (severances) and minor variances under "
        "the Planning Act. Made up of Council members and public appointees."
    ),
}
CATEGORY = "boards/adjustment"

# Media-index searches. "agenda" is broad on purpose: early COA agendas were
# named "AGENDA-Feb-8-2022.pdf" with no COA marker; each is confirmed by
# reading its first page before it is accepted.
MEDIA_TERMS = ["COA", "Committee of Adjustment", "Adjustment", "Consent",
               "Variance", "agenda"]
# Files that are COA material by name (notices, reports, decisions).
COA_NAME_RE = re.compile(r"(\bCOA\b|adjustment|consent|variance|\b[AC][\s_-]?20\d\d[\s_-]?\d{1,2}\b)", re.I)
# Skip large files when checking "agenda" hits: COA agendas are small,
# council agenda packages are not.
MAX_CHECK_BYTES = 8 * 1024 * 1024
WAYBACK_PREFIX = "nipissingtownship.com/wp-content/uploads/"
WAYBACK_REGEX = r".*([Cc][Oo][Aa]|[Aa]djustment|[Cc]onsent|[Vv]ariance).*"
WAYBACK_EVERY_DAYS = 7

# file URL -> "YYYY-MM-DD" for documents whose date can't be detected.
MANUAL_DATES: Dict[str, str] = {}


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


def https(url: str) -> str:
    return re.sub(r"^http://", "https://", (url or "").strip())


def kind_of(name: str) -> str:
    n = (name or "").lower()
    if "minute" in n:
        return "minutes"
    if "agenda" in n:
        return "agenda"
    return "extra"


def pretty(url: str) -> str:
    stem = re.sub(r"\.pdf$", "", ds.filename_of(url), flags=re.I)
    stem = re.sub(r"[-_]+", " ", stem)
    stem = re.sub(r"\s+\d$", "", stem)          # WordPress "-1" duplicate suffix
    return stem.strip()


def extra_label(link_text: str, url: str) -> str:
    text = clean_label(link_text)
    name = pretty(url)
    if not text or text == "Document":
        return name
    return f"{text}: {name}" if name.lower() != text.lower() else text


# ---------------------------------------------------------------------
# 1. Live page
# ---------------------------------------------------------------------

def scrape_page() -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, str]]]:
    """Meetings keyed by ISO date, plus page-level docs (by-laws, forms)."""
    print(f"Scraping {BOARD['name']} ...")
    soup = BeautifulSoup(fetch_html(BOARD["url"]), "html.parser")
    content = (soup.find("div", class_=re.compile(r"entry-content|post-content"))
               or soup.find("article") or soup.find("main") or soup.body)
    meetings: Dict[str, Dict[str, Any]] = {}
    page_docs: List[Dict[str, str]] = []
    current: Optional[Dict[str, Any]] = None

    # Walk in document order: short dated text starts a meeting, every link
    # after it belongs to that meeting until the next date.
    for el in content.find_all(True):
        if el.name == "a" and el.get("href"):
            href = https(normalize_absolute_url(el["href"]))
            if not href.lower().startswith("http") or "mailto:" in href:
                continue
            text = el.get_text(" ", strip=True)
            if current is None:
                if ".pdf" in href.lower():
                    page_docs.append({"label": clean_label(text), "url": href})
                continue
            k = kind_of(text)
            if k in ("agenda", "minutes") and href.lower().endswith(".pdf") \
                    and not current[f"{k}_url"]:
                current[f"{k}_url"] = href
            else:
                current["extra_docs"].append({"label": extra_label(text, href), "url": href})
            continue
        if el.name not in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "strong", "b", "td"):
            continue
        own = el.get_text(" ", strip=True)
        for a in el.find_all("a"):
            own = own.replace(a.get_text(" ", strip=True), " ")
        own = re.sub(r"\s+", " ", own).strip()
        if not own or len(own) > 60:
            continue
        dt = ds.find_date(own)
        if dt:
            current = meetings.get(dt.isoformat())
            if current is None:
                current = meetings[dt.isoformat()] = _new_meeting(dt, BOARD, own)

    for m in meetings.values():
        m["extra_docs"] = dedupe_docs(m["extra_docs"])
    print(f"  page: {len(meetings)} meeting(s), {len(page_docs)} page document(s)")
    return meetings, dedupe_docs(page_docs)


# ---------------------------------------------------------------------
# 2 + 3. Media index and Wayback
# ---------------------------------------------------------------------

def date_for(url: str, text: str) -> Tuple[Optional[date], str]:
    """Meeting date of an agenda or minutes."""
    if url in MANUAL_DATES:
        return date.fromisoformat(MANUAL_DATES[url]), "manual"
    from_name = ds.find_date(ds.filename_of(url))
    in_text = ds.find_dates(text[:3000])
    if from_name and from_name in in_text:
        return from_name, "filename+pdf"
    if in_text:
        if from_name:
            print(f"    ! filename says {from_name}, PDF says {in_text[0]} — using PDF")
        return in_text[0], "pdf"
    return from_name, "filename" if from_name else "none"


def candidate_dates(url: str, text: str) -> List[str]:
    """Notices / reports / decisions: every date they mention. A notice's
    filename carries the notice date, not the hearing date, so the document
    joins whichever of these dates turns out to be a meeting."""
    if url in MANUAL_DATES:
        return [MANUAL_DATES[url]]
    found = ds.find_dates(ds.filename_of(url)) + ds.find_dates(text[:6000])
    return [d.isoformat() for d in ds.unique(found)]


def examine(url: str, data: bytes, source: str, state: Dict[str, Any]) -> None:
    """Classify + date one PDF and archive it. Records the outcome in state."""
    name = ds.filename_of(url)
    kind = kind_of(name)
    text = ds.pdf_text(data, pages=2)
    # Named files are COA material; an unnamed "AGENDA-<date>.pdf" must say
    # Committee of Adjustment in its header (council agendas mention the
    # committee as an agenda item, so the whole page is not enough).
    header = re.sub(r"\s+", " ", text).strip()[:150].lower()
    is_coa_header = "adjustment" in header and "council" not in header
    if not COA_NAME_RE.search(name) and not is_coa_header:
        state[url] = {"status": "not-coa", "checked": TODAY.isoformat()}
        return
    entry: Dict[str, Any] = {"kind": kind, "source": source, "checked": TODAY.isoformat()}
    if kind == "extra":
        cands = candidate_dates(url, text)
        entry.update(status="ok" if cands else "undated", candidates=cands)
        year = int(cands[0][:4]) if cands else 0
        shown = cands[0] if cands else "NO DATE"
        how = "candidates"
    else:
        dt, how = date_for(url, text)
        entry.update(status="ok" if dt else "undated",
                     date=dt.isoformat() if dt else None, date_source=how)
        year = dt.year if dt else 0
        shown = dt or "NO DATE"
    if year >= ARCHIVE_MIN_YEAR:
        entry["archived"] = store_bytes_in_repo(url, CATEGORY, year, data)
    state[url] = entry
    print(f"    → {kind} {shown} ({how}, {source}) {name}")


def backfill(state: Dict[str, Any], meta: Dict[str, Any]) -> None:
    # Media index (files still on the server)
    candidates: Dict[str, Dict[str, Any]] = {}
    for term in MEDIA_TERMS:
        items = ds.wp_media_search(term)
        print(f"  media index '{term}': {len(items)} item(s)")
        for it in items:
            if it["url"].lower().endswith(".pdf"):
                candidates[it["url"]] = it
    for url, it in sorted(candidates.items()):
        if url in state:
            continue
        name = ds.filename_of(url)
        named_coa = bool(COA_NAME_RE.search(name))
        if not named_coa:
            # Only plain "AGENDA-<date>.pdf"-style files are worth checking.
            if kind_of(name) != "agenda" or (it.get("size") or 0) > MAX_CHECK_BYTES:
                state[url] = {"status": "skipped", "checked": TODAY.isoformat()}
                continue
        data = ds.download_pdf(url)
        if data is None:
            continue  # retried next run
        if not named_coa and len(data) > MAX_CHECK_BYTES:
            state[url] = {"status": "skipped", "checked": TODAY.isoformat()}
            continue
        examine(url, data, "media-index", state)

    # Wayback (deleted files) — weekly, it is slow
    last = meta.get("wayback_checked")
    if last and date.fromisoformat(last) > TODAY - timedelta(days=WAYBACK_EVERY_DAYS):
        print(f"  wayback: checked {last}, next check in a few days")
        return
    files = ds.wayback_files(WAYBACK_PREFIX, WAYBACK_REGEX)
    print(f"  wayback: {len(files)} archived COA-like PDF(s)")
    new = 0
    for f in files:
        url = f["original"]
        if url in state or url in candidates:
            continue
        data = ds.wayback_download(url, f["timestamp"])
        if data is None:
            continue
        examine(url, data, f"wayback {f['timestamp'][:8]}", state)
        new += 1
    print(f"  wayback: {new} recovered file(s) examined")
    meta["wayback_checked"] = TODAY.isoformat()


def apply_backfill(meetings: Dict[str, Dict[str, Any]], state: Dict[str, Any]) -> List[str]:
    """Attach examined documents. Agenda/minutes create meetings; notices,
    reports and decisions only join a meeting that exists. Returns unplaced."""
    unplaced = []
    docs = [(u, e) for u, e in state.items() if e.get("status") == "ok"]
    for url, e in sorted(docs, key=lambda x: (x[1]["kind"] == "extra", x[0])):
        if e["kind"] == "extra":
            iso = next((c for c in e.get("candidates", []) if c in meetings), None)
            if iso is None:
                unplaced.append(ds.filename_of(url))
                continue
        else:
            iso = e["date"]
        m = meetings.get(iso)
        if m is None:
            m = meetings[iso] = _new_meeting(date.fromisoformat(iso), BOARD, "")
        archived = e.get("archived")
        k = e["kind"]
        known = {https(m.get(f"{x}_url") or "") for x in ("agenda", "minutes")} | \
                {https(d.get("url") or "") for d in m.get("extra_docs", [])}
        if k in ("agenda", "minutes") and not m.get(f"{k}_url"):
            m[f"{k}_url"] = url
            if archived:
                m[f"{k}_archived"] = archived
        elif url not in known:
            doc = {"label": extra_label("", url) if k == "extra" else pretty(url), "url": url}
            if archived:
                doc["archived"] = archived
            m.setdefault("extra_docs", []).append(doc)
        # Archived copy for a page link that matched a backfilled file
        for x in ("agenda", "minutes"):
            if https(m.get(f"{x}_url") or "") == url and archived and not m.get(f"{x}_archived"):
                m[f"{x}_archived"] = archived
        for d in m.get("extra_docs", []):
            if https(d.get("url") or "") == url and archived and not d.get("archived"):
                d["archived"] = archived
        if e.get("source", "").startswith("wayback"):
            m["recovered_from_wayback"] = True
    return unplaced


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("Nipissing update_adjustment.py")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    payload = load_json(BOARDS_FILE, {"boards": []})
    raw_state = load_json(STATE_FILE, {"files": {}, "meta": {}})
    state, meta = raw_state.setdefault("files", {}), raw_state.setdefault("meta", {})

    boards = payload.setdefault("boards", [])
    board = next((b for b in boards if b.get("id") == BOARD["id"]), None)
    if board is None:
        board = {"id": BOARD["id"], "meetings": [], "page_docs": []}
        boards.append(board)
    board.update({k: BOARD[k] for k in ("name", "url", "description")})

    meetings = {m["date"]: m for m in board.get("meetings", []) if m.get("date")}

    try:
        page_meetings, page_docs = scrape_page()
        board["page_docs"] = dedupe_docs((board.get("page_docs") or []) + page_docs)
        for iso, pm in page_meetings.items():
            old = meetings.get(iso)
            if old is None:
                meetings[iso] = pm
                continue
            for f in ("agenda_url", "minutes_url"):
                if pm.get(f):
                    old[f] = pm[f]
            old["extra_docs"] = dedupe_docs((old.get("extra_docs") or []) +
                                            pm.get("extra_docs", []))
    except Exception as e:
        print(f"  ✗ page failed ({e}) — keeping stored meetings")

    try:
        backfill(state, meta)
    except Exception as e:
        print(f"  ✗ backfill failed ({e}) — will retry next run")
    unplaced = apply_backfill(meetings, state)

    for m in meetings.values():
        m["all_agendas"] = True
        try:
            m["is_future"] = date.fromisoformat(m["date"]) > TODAY
        except ValueError:
            pass
        m.setdefault("video_url", None)
        m["extra_docs"] = _dedupe_keep(m.get("extra_docs") or [])
        mirror_record(m, CATEGORY)

    board["meetings"] = sorted(meetings.values(), key=lambda x: x["date"], reverse=True)
    save_json(STATE_FILE, raw_state)
    save_json(BOARDS_FILE, payload)

    ms = board["meetings"]
    years = {}
    for m in ms:
        years[m["date"][:4]] = years.get(m["date"][:4], 0) + 1
    print(f"Saved {BOARD['name']} -> {BOARDS_FILE}")
    print(f"  meetings by year: {dict(sorted(years.items()))}")
    print(f"  agendas: {sum(1 for m in ms if m.get('agenda_url'))} | "
          f"minutes: {sum(1 for m in ms if m.get('minutes_url'))} | "
          f"recovered from Wayback: {sum(1 for m in ms if m.get('recovered_from_wayback'))}")
    undated = [u for u, e in state.items() if e.get("status") == "undated"]
    for u in undated:
        print(f"  ! undated (add to MANUAL_DATES): {u}")
    for u in unplaced:
        print(f"  ? no matching meeting for: {u}")


def _dedupe_keep(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """dedupe_docs drops the 'archived' key; keep it."""
    seen, out = set(), []
    for d in docs:
        k = https(d.get("url") or "")
        if k and k not in seen:
            seen.add(k)
            out.append(d)
    return out


if __name__ == "__main__":
    main()
