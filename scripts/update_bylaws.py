#!/usr/bin/env python3
"""
update_bylaws.py

Minutes-first updater for:
- data/canonical/bylaws.json
- data/canonical/resolutions.json
- data/canonical/council_terms.json

Council term stats show two slots — "current" and "last" — which
advance automatically when a new term begins. Term boundaries are
computed from a small set of known historical anchor points plus
Ontario's fixed 4-year election cycle (October, every 4 years from
2018: 2022, 2026, 2030, …).

The first meeting of a new term is typically the third Tuesday of
November following the election. We use the actual first meeting date
found in meetings.json rather than guessing, so the transition happens
precisely when real data arrives — not on election day.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import fitz  # PyMuPDF
import requests
from bs4 import BeautifulSoup

BASE_URL    = "https://nipissingtownship.com"
BYLAWS_PAGE = f"{BASE_URL}/municipal-information/by-laws/"

ROOT               = Path(__file__).resolve().parents[1]
CANONICAL_DIR      = ROOT / "data" / "canonical"
RUNTIME_DIR        = ROOT / "data" / "runtime"
DOCS_DIR           = ROOT / "docs"
DOWNLOAD_CACHE_DIR = RUNTIME_DIR / "pdf_cache"
EXTRACTED_BYLAWS_DIR      = DOCS_DIR / "files" / "bylaws"
EXTRACTED_RESOLUTIONS_DIR = DOCS_DIR / "files" / "resolutions"

MEETINGS_FILE      = CANONICAL_DIR / "meetings.json"
BYLAWS_FILE        = CANONICAL_DIR / "bylaws.json"
RESOLUTIONS_FILE   = CANONICAL_DIR / "resolutions.json"
COUNCIL_TERMS_FILE = CANONICAL_DIR / "council_terms.json"

HEADERS = {"User-Agent": "nipissing-public-records/1.0 (bylaw updater)"}

NUMBER_TITLE_RE = re.compile(r"^\s*(\d{4}[\-–]\d{1,3}|\d{3,4})\s+(.+?)\s*$")
BYLAW_NUM_RE    = re.compile(r"(\d{4}[\-–]\d{1,3})")
RES_NUM_RE      = re.compile(r"(R\d{4}-\d+)", re.IGNORECASE)

# ─────────────────────────────────────────────────────────────────────
# ELECTION CYCLE
#
# Ontario municipal elections are held every 4 years in October.
# The base year is 2018. This list of known first-meeting dates covers
# the historical record; everything after the last entry is derived
# automatically from the meetings.json data as real meetings arrive.
#
# KNOWN_TERM_STARTS maps election_year -> first meeting date of that
# council term (the inaugural meeting in November of the election year).
# ─────────────────────────────────────────────────────────────────────

ELECTION_BASE_YEAR   = 2018
ELECTION_CYCLE_YEARS = 4

# Known first-meeting dates for each term, keyed by election year.
# Add a new entry here ONLY if the auto-detection from meetings.json
# fails for some reason. Under normal operation this dict only needs
# to grow when a historical term is added to the archive.
KNOWN_TERM_STARTS: Dict[int, str] = {
    2018: "2018-11-06",
    2022: "2022-11-15",
    # 2026 and beyond: auto-detected from meetings.json
}

# Known last-meeting dates (last regular meeting before election).
# The last meeting of any term is always in October of the election year.
KNOWN_TERM_ENDS: Dict[int, str] = {
    2018: "2022-10-25",   # last meeting of the 2018-elected council
    2022: "2026-10-20",   # last scheduled meeting of the 2022-elected council
    # 2026 and beyond: will be detected once meetings.json has that data
}


def election_years_up_to(today: date) -> List[int]:
    """Return all election years from base up to and including this term."""
    years = []
    y = ELECTION_BASE_YEAR
    # Go two cycles past today so we always have a future placeholder
    while y <= today.year + ELECTION_CYCLE_YEARS * 2:
        years.append(y)
        y += ELECTION_CYCLE_YEARS
    return years


def find_first_meeting_after(target_date: str, meetings: List[Dict[str, Any]]) -> Optional[str]:
    """
    Return the date string of the earliest meeting in meetings.json
    whose date is >= target_date and which has published minutes
    (minutes_url is set), indicating the term has actually started.
    """
    candidates = [
        m["date"] for m in meetings
        if m.get("date", "") >= target_date
        and m.get("minutes_url")
    ]
    return min(candidates) if candidates else None


def find_last_meeting_before(cutoff_date: str, meetings: List[Dict[str, Any]]) -> Optional[str]:
    """
    Return the date string of the latest meeting in meetings.json
    whose date is <= cutoff_date and which has published minutes.
    """
    candidates = [
        m["date"] for m in meetings
        if m.get("date", "") <= cutoff_date
        and m.get("minutes_url")
    ]
    return max(candidates) if candidates else None


def build_term_definitions(
    meetings: List[Dict[str, Any]],
    today: date,
) -> List[Dict[str, Any]]:
    """
    Construct the full list of council terms, newest first.

    For each election year in the cycle:
    - start  = first meeting with published minutes on/after Nov 1 of that year
               (falling back to KNOWN_TERM_STARTS if not yet in meetings.json)
    - end    = last meeting with published minutes on/before Oct 31 of
               (election_year + 4), i.e. the last meeting before the next election
               (falling back to KNOWN_TERM_ENDS)
    - first_meeting_of_next_term = the start of the following term

    Terms that haven't started yet (no meetings data) are excluded.
    """
    all_election_years = election_years_up_to(today)
    terms: List[Dict[str, Any]] = []

    for i, election_year in enumerate(all_election_years):
        # Term starts in November of the election year
        nov_1 = f"{election_year}-11-01"

        # Try to find the actual first meeting from real data
        start = (
            KNOWN_TERM_STARTS.get(election_year)
            or find_first_meeting_after(nov_1, meetings)
        )

        if not start:
            # This term hasn't started yet — skip it
            continue

        # Term ends just before the next election (Oct 31 of election_year + 4)
        next_election_year = election_year + ELECTION_CYCLE_YEARS
        oct_31_next        = f"{next_election_year}-10-31"

        end = (
            KNOWN_TERM_ENDS.get(election_year)
            or find_last_meeting_before(oct_31_next, meetings)
        )
        # If no end found, term is still ongoing
        if end and date.fromisoformat(end) >= today:
            end = None   # treat as ongoing until the date actually passes

        # first_meeting_of_next_term — used to decide when "current" advances
        next_nov_1 = f"{next_election_year}-11-01"
        next_start = (
            KNOWN_TERM_STARTS.get(next_election_year)
            or find_first_meeting_after(next_nov_1, meetings)
        )

        terms.append({
            "id":    f"{election_year}-{election_year + ELECTION_CYCLE_YEARS}",
            "label": f"{election_year}–{election_year + ELECTION_CYCLE_YEARS} Council",
            "start": start,
            "end":   end,
            "first_meeting_of_next_term": next_start,
        })

    # Return newest first
    terms.sort(key=lambda t: t["start"], reverse=True)
    return terms


# ─────────────────────────────────────────────────────────────────────
# Slot assignment
#
# "current" = the term whose first_meeting_of_next_term has NOT yet
#             arrived (today < first_meeting_of_next_term, or it's None).
# "last"    = the term immediately before current.
#
# The transition from old "current" to new "current" happens on the
# day the new term's first meeting appears in meetings.json with
# published minutes — not on election day.
# ─────────────────────────────────────────────────────────────────────

def assign_slots(
    terms: List[Dict[str, Any]],
    today: date,
) -> Dict[str, Optional[Dict[str, Any]]]:
    slots: Dict[str, Optional[Dict[str, Any]]] = {"current": None, "last": None}

    for term in terms:  # newest first
        next_start = term.get("first_meeting_of_next_term")

        if next_start is None or today < date.fromisoformat(next_start):
            if slots["current"] is None:
                slots["current"] = term
        else:
            if slots["current"] is not None and slots["last"] is None:
                slots["last"] = term
            # Anything older falls off the two-slot display

    return slots


# ─────────────────────────────────────────────────────────────────────
# Resolution stats
# ─────────────────────────────────────────────────────────────────────

def compute_term_stats(
    resolutions: List[Dict[str, Any]],
    term_start: str,
    term_end: Optional[str],
) -> Dict[str, Any]:
    in_term = [
        r for r in resolutions
        if r.get("meeting_date")
        and r["meeting_date"] >= term_start
        and (term_end is None or r["meeting_date"] <= term_end)
    ]

    counts   = Counter(r.get("status", "unknown") for r in in_term)
    carried  = counts.get("carried",  0)
    defeated = counts.get("defeated", 0)
    deferred = counts.get("deferred", 0)
    total    = len(in_term)

    meeting_dates = sorted({r["meeting_date"] for r in in_term if r.get("meeting_date")})

    return {
        "total":            total,
        "carried":          carried,
        "defeated":         defeated,
        "deferred":         deferred,
        "meetings_counted": len(meeting_dates),
        "last_meeting":     meeting_dates[-1] if meeting_dates else None,
    }


def write_term_stats(
    resolutions: List[Dict[str, Any]],
    meetings: List[Dict[str, Any]],
) -> None:
    print("\n═══ Step 5: Council Term Stats ═══")

    today = date.today()
    terms = build_term_definitions(meetings, today)
    slots = assign_slots(terms, today)

    # Load any existing council_terms.json so we can preserve static entries.
    # A term marked "static": true has manually verified counts that the
    # scraper cannot reliably reproduce (e.g. pre-PDF web-based minutes).
    # Static entries are never overwritten by computed stats.
    existing_terms_payload = load_json(COUNCIL_TERMS_FILE, {})
    existing_by_id: Dict[str, Dict[str, Any]] = {}
    for entry in existing_terms_payload.get("all_terms", []):
        if isinstance(entry, dict) and entry.get("id"):
            existing_by_id[entry["id"]] = entry

    output: Dict[str, Any] = {
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "current": None,
        "last":    None,
        "all_terms": [],
    }

    for slot_name in ("current", "last"):
        term = slots[slot_name]
        if term is None:
            continue

        term_id  = term.get("id", "")
        existing = existing_by_id.get(term_id, {})

        if existing.get("static"):
            # Preserve the manually-set stats exactly; only refresh the slot label
            entry = {**existing, "slot": slot_name}
            print(
                f"  [{slot_name}] {term['label']} — using static values "
                f"({existing.get('total', '?')} resolutions, "
                f"{existing.get('meetings_counted', '?')} meetings)"
            )
        else:
            # Compute fresh from resolutions.json
            stats = compute_term_stats(
                resolutions,
                term_start=term["start"],
                term_end=term["end"],
            )
            entry = {**term, **stats, "slot": slot_name}
            end_label = term["end"] or "present"
            print(
                f"  [{slot_name}] {term['label']} "
                f"({term['start']} – {end_label}): "
                f"{stats['total']} resolutions | "
                f"{stats['carried']} carried | "
                f"{stats['defeated']} defeated | "
                f"{stats['deferred']} deferred"
            )

        output[slot_name] = entry
        output["all_terms"].append(entry)

    save_json(COUNCIL_TERMS_FILE, output)
    print(f"  Saved → {COUNCIL_TERMS_FILE}")


# ─────────────────────────────────────────────────────────────────────
# Helpers (unchanged from original)
# ─────────────────────────────────────────────────────────────────────

def normalize_number(number: str) -> str:
    return (number or "").replace("–", "-").strip()


def clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    if title.startswith("- "):
        title = title[2:].strip()
    return title


def parse_year(number: str, href: str = "") -> Optional[int]:
    number = normalize_number(number)
    m = re.match(r"^(\d{4})-\d{1,3}$", number)
    if m:
        return int(m.group(1))
    um = re.search(r"/(\d{4})[-–]", href or "")
    if um:
        year = int(um.group(1))
        if 1990 <= year <= 2035:
            return year
    return None


def bylaw_key(bylaw: Dict[str, Any]) -> str:
    return normalize_number(bylaw.get("number", ""))


def resolution_key(resolution: Dict[str, Any]) -> str:
    return (resolution.get("number") or "").upper().strip()


def fetch_page(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def download_pdf(url: str, folder: Path) -> Optional[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    filename = re.sub(r"[^\w\-\.]", "_", url.split("/")[-1])
    path = folder / filename
    if path.exists():
        return path
    try:
        print(f"  ↓ {filename}")
        resp = requests.get(url, headers=HEADERS, timeout=120)
        resp.raise_for_status()
        path.write_bytes(resp.content)
        return path
    except Exception as e:
        print(f"  WARN: download failed {filename}: {e}")
        return None


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        doc  = fitz.open(str(pdf_path))
        text = [page.get_text() for page in doc]
        doc.close()
        return "\n".join(text)
    except Exception as e:
        print(f"  WARN: PDF read failed {pdf_path.name}: {e}")
        return ""


def extract_pdf_page_texts(pdf_path: Path) -> List[str]:
    try:
        doc   = fitz.open(str(pdf_path))
        pages = [page.get_text() for page in doc]
        doc.close()
        return pages
    except Exception as e:
        print(f"  WARN: page text extraction failed {pdf_path.name}: {e}")
        return []


def relative_docs_url(path: Path) -> str:
    return path.relative_to(DOCS_DIR).as_posix()


def write_page_range_pdf(
    source_pdf: Path, start_page: int, end_page: int, output_path: Path
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    src = fitz.open(str(source_pdf))
    out = fitz.open()
    out.insert_pdf(src, from_page=start_page, to_page=end_page)
    out.save(str(output_path))
    out.close()
    src.close()


# ─────────────────────────────────────────────────────────────────────
# Step 1: scrape by-laws page
# ─────────────────────────────────────────────────────────────────────

def scrape_bylaws_page() -> List[Dict[str, Any]]:
    print("\n═══ Step 1: By-Laws Page ═══")
    soup    = fetch_page(BYLAWS_PAGE)
    content = (
        soup.find("div", class_=re.compile(r"entry-content|post-content"))
        or soup.find("article")
        or soup.find("main")
        or soup.body
    )
    if content is None:
        return []

    bylaws: List[Dict[str, Any]] = []
    seen = set()

    for a in content.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = a.get("href", "").strip()
        if not text:
            continue
        m = NUMBER_TITLE_RE.match(text)
        if not m:
            continue
        number = normalize_number(m.group(1))
        if number in seen:
            continue
        seen.add(number)

        title    = clean_title(m.group(2))
        full_url = urljoin(BASE_URL, href)
        pdf_url  = full_url if full_url.lower().endswith((".pdf", ".doc", ".docx")) else None
        page_url = None if pdf_url else full_url

        bylaws.append({
            "number":             number,
            "year":               parse_year(number, href=full_url),
            "title":              title,
            "date_passed":        None,
            "pdf_url":            pdf_url,
            "page_url":           page_url,
            "source":             "bylaws_page",
            "status":             "approved",
            "votes":              None,
            "meeting_date":       None,
            "agenda_package_url": None,
            "minutes_url":        None,
            "summary":            None,
        })

    print(f"  Found {len(bylaws)} by-law(s) on listing page")
    return bylaws


# ─────────────────────────────────────────────────────────────────────
# Step 2: read meetings from canonical
# ─────────────────────────────────────────────────────────────────────

def load_meetings() -> List[Dict[str, Any]]:
    payload = load_json(MEETINGS_FILE, {"meetings": []})
    if isinstance(payload, list):
        return payload
    return payload.get("meetings", [])


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


# ─────────────────────────────────────────────────────────────────────
# Step 3: parse by-laws and resolutions from minutes
# ─────────────────────────────────────────────────────────────────────

def parse_bylaws_from_minutes(
    text: str, meeting: Dict[str, Any]
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    blocks = re.split(r'(R\d{4}-\d+)', text)

    for i in range(1, len(blocks), 2):
        res_number = blocks[i]
        body       = blocks[i + 1] if i + 1 < len(blocks) else ""

        bm = re.search(
            r'(?:pass|adopt)\s+By[\-\s]?Law\s*(?:No\.?\s*|Number\s*)?(\d{4}[\-–]\d{1,3})',
            body, re.IGNORECASE,
        )
        if not bm:
            continue

        bylaw_num = normalize_number(bm.group(1))
        mm        = re.match(r'\s*([A-Z]\.\s*\w+)\s*,\s*([A-Z]\.\s*\w+)', body)
        mover     = mm.group(1).strip() if mm else None
        seconder  = mm.group(2).strip() if mm else None

        tm = re.search(
            r'being\s+a\s+By[\-\s]?Law\s+(?:to\s+)?(.+?)(?:\.\s*$|\.\s*Read\s|;\s*Read\s)',
            body, re.IGNORECASE | re.DOTALL,
        )
        title = None
        if tm:
            title = clean_title(tm.group(1))
            if len(title) > 180:
                title = title[:177] + "..."

        status = "approved" if re.search(r'\bCarried\b', body, re.IGNORECASE) else "pending"
        votes  = f"Moved by {mover}, Seconded by {seconder}" if mover and seconder else None

        results.append({
            "number":             bylaw_num,
            "year":               parse_year(bylaw_num),
            "title":              title or f"By-Law {bylaw_num}",
            "date_passed":        meeting["date"],
            "pdf_url":            None,
            "page_url":           None,
            "source":             "minutes",
            "status":             status,
            "votes":              votes,
            "meeting_date":       meeting["date"],
            "agenda_package_url": meeting.get("package_url"),
            "minutes_url":        meeting.get("minutes_url"),
            "summary":            None,
        })

    return results


def categorize_resolution(text: str) -> str:
    t = text.lower()
    if "by-law number" in t or "by-law no" in t or "pass by-law" in t:
        return "By-Law"
    if "authorize" in t:
        return "Authorization"
    if "appoint" in t:
        return "Appointments"
    if "receive" in t:
        return "Receive"
    if "support" in t:
        return "Support"
    if "approve" in t:
        return "Approval"
    return "General"


def parse_resolutions_from_minutes(
    text: str, meeting: Dict[str, Any]
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    blocks = re.split(r'(R\d{4}-\d+)', text)

    for i in range(1, len(blocks), 2):
        res_number = blocks[i].upper()
        body       = blocks[i + 1] if i + 1 < len(blocks) else ""

        mm       = re.match(r'\s*([A-Z]\.\s*\w+)\s*,\s*([A-Z]\.\s*\w+)', body)
        mover    = mm.group(1).strip() if mm else None
        seconder = mm.group(2).strip() if mm else None

        motion_match = re.search(
            r':\s*(.*?)(?:\bCarried\b|\bDefeated\b|\bLost\b|\bWithdrawn\b|\bDeferred\b)',
            f"{res_number}{body}", re.IGNORECASE | re.DOTALL,
        )
        motion_text = motion_match.group(1).strip() if motion_match else body.strip()
        motion_text = re.sub(r'\s+', ' ', motion_text)
        if not motion_text:
            continue

        if re.search(r'\bDefeated\b|\bLost\b', body, re.IGNORECASE):
            status = "defeated"
        elif re.search(r'\bWithdrawn\b', body, re.IGNORECASE):
            status = "withdrawn"
        elif re.search(r'\bDeferred\b', body, re.IGNORECASE):
            status = "deferred"
        elif re.search(r'\bCarried\b', body, re.IGNORECASE):
            status = "carried"
        else:
            status = "unknown"

        bylaw_match  = re.search(
            r'By[\-\s]?Law\s*(?:No\.?\s*|Number\s*)?(\d{4}[\-–]\d{1,3})',
            motion_text, re.IGNORECASE,
        )
        bylaw_number = normalize_number(bylaw_match.group(1)) if bylaw_match else None
        title        = motion_text[:137] + "..." if len(motion_text) > 140 else motion_text
        votes        = f"Moved by {mover}, Seconded by {seconder}" if mover and seconder else None

        results.append({
            "number":             res_number,
            "title":              title,
            "motion_text":        motion_text,
            "meeting_date":       meeting["date"],
            "minutes_url":        meeting.get("minutes_url"),
            "status":             status,
            "votes":              votes,
            "mover":              mover,
            "seconder":           seconder,
            "is_bylaw":           bool(bylaw_number),
            "bylaw_number":       bylaw_number,
            "category":           categorize_resolution(motion_text),
            "pdf_url":            None,
            "agenda_package_url": meeting.get("package_url"),
        })

    return results


def parse_all_minutes(
    meetings: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    print("\n═══ Step 2: Parsing Minutes ═══")
    all_bylaws:      List[Dict[str, Any]] = []
    all_resolutions: List[Dict[str, Any]] = []

    # Deduplicate by minutes_url — same-day Regular+Special meetings that
    # share a URL (e.g. Jan 7 2025, May 9 2024, Jan 2 2024) would otherwise
    # be parsed twice, producing duplicate resolution records that the merge
    # step quietly drops, making the carried count appear lower than it is.
    seen_urls: set = set()

    for meeting in meetings:
        minutes_url = meeting.get("minutes_url")
        if not minutes_url:
            continue

        # Skip web page URLs — only process actual PDF files.
        # Older minutes (pre-2023) are HTML pages that PyMuPDF cannot read.
        # Those are handled via static entries in council_terms.json.
        if not minutes_url.lower().endswith(".pdf"):
            continue

        if minutes_url in seen_urls:
            continue
        seen_urls.add(minutes_url)

        pdf_path = download_pdf(minutes_url, DOWNLOAD_CACHE_DIR / "minutes")
        if not pdf_path:
            continue
        text = extract_pdf_text(pdf_path)
        if not text or len(text.strip()) < 100:
            print(f"  WARN: no text from minutes {meeting['date']}")
            continue

        bylaws      = parse_bylaws_from_minutes(text, meeting)
        resolutions = parse_resolutions_from_minutes(text, meeting)

        if bylaws:
            print(f"  {meeting['display_date']}: {len(bylaws)} by-law(s) — "
                  f"{', '.join(b['number'] for b in bylaws)}")
        if resolutions:
            print(f"  {meeting['display_date']}: {len(resolutions)} resolution(s)")

        all_bylaws.extend(bylaws)
        all_resolutions.extend(resolutions)

    print(f"  Total by-laws from minutes:      {len(all_bylaws)}")
    print(f"  Total resolutions from minutes:  {len(all_resolutions)}")
    return all_bylaws, all_resolutions


# ─────────────────────────────────────────────────────────────────────
# Step 4: extract standalone PDFs from agenda packages
# ─────────────────────────────────────────────────────────────────────

def find_target_start_pages(
    page_texts: List[str], targets: List[str]
) -> Dict[str, int]:
    starts:        Dict[str, int] = {}
    upper_targets = [t.upper() for t in targets]
    for idx, page_text in enumerate(page_texts):
        up = (page_text or "").upper()
        for target in upper_targets:
            if target in up and target not in starts:
                starts[target] = idx
    return starts


def export_targets_from_package(
    meeting: Dict[str, Any],
    targets: List[str],
    output_dir: Path,
) -> Dict[str, str]:
    if not meeting.get("package_url") or not targets:
        return {}
    package_path = download_pdf(meeting["package_url"], DOWNLOAD_CACHE_DIR / "packages")
    if not package_path:
        return {}
    page_texts = extract_pdf_page_texts(package_path)
    if not page_texts:
        return {}
    start_map = find_target_start_pages(page_texts, targets)
    if not start_map:
        return {}

    ordered  = sorted(start_map.items(), key=lambda x: x[1])
    exported: Dict[str, str] = {}

    for i, (target, start_page) in enumerate(ordered):
        next_start  = ordered[i + 1][1] if i + 1 < len(ordered) else len(page_texts)
        end_page    = max(start_page, next_start - 1)
        safe_target = target.replace("/", "-")
        output_path = output_dir / f"{safe_target}.pdf"
        try:
            write_page_range_pdf(package_path, start_page, end_page, output_path)
            exported[target] = relative_docs_url(output_path)
        except Exception as e:
            print(f"  WARN: could not export {target} from {meeting['date']}: {e}")

    return exported


def enrich_from_packages(
    meetings:    List[Dict[str, Any]],
    bylaws:      List[Dict[str, Any]],
    resolutions: List[Dict[str, Any]],
) -> None:
    print("\n═══ Step 3: Extracting PDFs from Agenda Packages ═══")

    bylaws_by_meeting:      Dict[str, List[str]] = {}
    resolutions_by_meeting: Dict[str, List[str]] = {}

    for b in bylaws:
        if b.get("meeting_date") and b.get("number"):
            bylaws_by_meeting.setdefault(b["meeting_date"], []).append(
                normalize_number(b["number"])
            )
    for r in resolutions:
        if r.get("meeting_date") and r.get("number"):
            resolutions_by_meeting.setdefault(r["meeting_date"], []).append(
                resolution_key(r)
            )

    bylaw_pdf_map:      Dict[str, str] = {}
    resolution_pdf_map: Dict[str, str] = {}

    for meeting in meetings:
        meeting_date = meeting.get("date")
        if not meeting.get("package_url") or not meeting_date:
            continue

        bylaw_targets = sorted(set(bylaws_by_meeting.get(meeting_date, [])))
        if bylaw_targets:
            exported = export_targets_from_package(
                meeting, bylaw_targets, EXTRACTED_BYLAWS_DIR
            )
            bylaw_pdf_map.update(exported)
            if exported:
                print(f"  {meeting_date}: extracted {len(exported)} by-law PDF(s)")

        res_targets = sorted(set(resolutions_by_meeting.get(meeting_date, [])))
        if res_targets:
            exported = export_targets_from_package(
                meeting, res_targets, EXTRACTED_RESOLUTIONS_DIR
            )
            resolution_pdf_map.update(exported)
            if exported:
                print(f"  {meeting_date}: extracted {len(exported)} resolution PDF(s)")

    for b in bylaws:
        num = normalize_number(b.get("number", ""))
        if num in bylaw_pdf_map:
            b["pdf_url"] = bylaw_pdf_map[num]

    for r in resolutions:
        num = resolution_key(r)
        if num in resolution_pdf_map:
            r["pdf_url"] = resolution_pdf_map[num]


# ─────────────────────────────────────────────────────────────────────
# Merge logic (unchanged from original)
# ─────────────────────────────────────────────────────────────────────

def merge_bylaw(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(existing)
    for key in (
        "title", "pdf_url", "page_url", "votes", "meeting_date",
        "minutes_url", "agenda_package_url", "summary",
    ):
        if not out.get(key) and new.get(key):
            out[key] = new[key]
    if (out.get("title", "").startswith("By-Law ")
            and new.get("title")
            and not new["title"].startswith("By-Law ")):
        out["title"] = new["title"]
    if out.get("status") == "pending" and new.get("status") in ("approved", "defeated"):
        out["status"] = new["status"]
    if not out.get("date_passed") and new.get("date_passed"):
        out["date_passed"] = new["date_passed"]
    if not out.get("year") and new.get("year"):
        out["year"] = new["year"]
    if not out.get("source") and new.get("source"):
        out["source"] = new["source"]
    return out


def merge_bylaws(
    existing:       List[Dict[str, Any]],
    incoming_lists: List[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    idx: Dict[str, Dict[str, Any]] = {
        bylaw_key(b): deepcopy(b)
        for b in existing
        if isinstance(b, dict) and b.get("number")
    }
    for group in incoming_lists:
        for b in group:
            num = bylaw_key(b)
            if not num:
                continue
            idx[num] = merge_bylaw(idx[num], b) if num in idx else deepcopy(b)

    out = list(idx.values())
    out.sort(key=lambda b: (b.get("year") or 0, b.get("number", "")), reverse=True)
    return out


def merge_resolution(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(existing)
    for key in (
        "title", "motion_text", "minutes_url", "meeting_date",
        "status", "votes", "mover", "seconder", "is_bylaw",
        "bylaw_number", "category", "pdf_url", "agenda_package_url",
    ):
        if not out.get(key) and new.get(key) is not None:
            out[key] = new[key]
    return out


def merge_resolutions(
    existing: List[Dict[str, Any]],
    incoming: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    idx: Dict[str, Dict[str, Any]] = {
        resolution_key(r): deepcopy(r)
        for r in existing
        if isinstance(r, dict) and r.get("number")
    }
    for r in incoming:
        num = resolution_key(r)
        if not num:
            continue
        idx[num] = merge_resolution(idx[num], r) if num in idx else deepcopy(r)

    out = list(idx.values())
    out.sort(
        key=lambda r: (r.get("meeting_date") or "", r.get("number") or ""),
        reverse=True,
    )
    return out


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("Nipissing update_bylaws.py")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    meetings = load_meetings()
    print(f"Meetings loaded: {len(meetings)}")

    bylaws_payload      = load_json(BYLAWS_FILE,      {"last_updated": None, "source": BYLAWS_PAGE, "bylaws": []})
    resolutions_payload = load_json(RESOLUTIONS_FILE, {"last_updated": None, "resolutions": []})

    existing_bylaws      = bylaws_payload.get("bylaws", [])           if isinstance(bylaws_payload, dict)      else bylaws_payload
    existing_resolutions = resolutions_payload.get("resolutions", []) if isinstance(resolutions_payload, dict) else resolutions_payload

    print(f"Existing bylaws:      {len(existing_bylaws)}")
    print(f"Existing resolutions: {len(existing_resolutions)}")

    page_bylaws                         = scrape_bylaws_page()
    minutes_bylaws, minutes_resolutions = parse_all_minutes(meetings)

    enrich_from_packages(meetings, minutes_bylaws, minutes_resolutions)

    final_bylaws      = merge_bylaws(existing_bylaws, [page_bylaws, minutes_bylaws])
    final_resolutions = merge_resolutions(existing_resolutions, minutes_resolutions)

    # Step 5: council term stats — fully automatic, no manual updates needed
    write_term_stats(final_resolutions, meetings)

    save_json(BYLAWS_FILE, {
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "source":       BYLAWS_PAGE,
        "bylaws":       final_bylaws,
    })
    save_json(RESOLUTIONS_FILE, {
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "resolutions":  final_resolutions,
    })

    print(f"\nSaved {len(final_bylaws)} by-law(s)      → {BYLAWS_FILE}")
    print(f"Saved {len(final_resolutions)} resolution(s) → {RESOLUTIONS_FILE}")


if __name__ == "__main__":
    main()
