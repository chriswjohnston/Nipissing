#!/usr/bin/env python3
"""
update_bylaws.py

Minutes-first updater for:
- data/canonical/bylaws.json
- data/canonical/resolutions.json

Workflow:
1. Read canonical meetings.json
2. Parse minutes PDFs to extract:
   - by-laws passed
   - resolutions carried / defeated
3. Use agenda package PDFs to extract standalone PDFs for:
   - by-laws found in minutes
   - resolutions found in minutes
4. Merge with existing canonical data
5. Optionally enrich from the Township by-laws page for direct links

This intentionally avoids OCR in CI for now.
Package extraction works best on text-extractable PDFs.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import fitz  # PyMuPDF
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://nipissingtownship.com"
BYLAWS_PAGE = f"{BASE_URL}/municipal-information/by-laws/"

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIR = ROOT / "data" / "canonical"
RUNTIME_DIR = ROOT / "data" / "runtime"
DOCS_DIR = ROOT / "docs"
DOWNLOAD_CACHE_DIR = RUNTIME_DIR / "pdf_cache"
EXTRACTED_BYLAWS_DIR = DOCS_DIR / "files" / "bylaws"
EXTRACTED_RESOLUTIONS_DIR = DOCS_DIR / "files" / "resolutions"

MEETINGS_FILE = CANONICAL_DIR / "meetings.json"
BYLAWS_FILE = CANONICAL_DIR / "bylaws.json"
RESOLUTIONS_FILE = CANONICAL_DIR / "resolutions.json"

HEADERS = {
    "User-Agent": "nipissing-public-records/1.0 (bylaw updater)"
}

NUMBER_TITLE_RE = re.compile(r"^\s*(\d{4}[\-–]\d{1,3}|\d{3,4})\s+(.+?)\s*$")
BYLAW_NUM_RE = re.compile(r"(\d{4}[\-–]\d{1,3})")
RES_NUM_RE = re.compile(r"(R\d{4}-\d+)", re.IGNORECASE)

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
        doc = fitz.open(str(pdf_path))
        text = []
        for page in doc:
            text.append(page.get_text())
        doc.close()
        return "\n".join(text)
    except Exception as e:
        print(f"  WARN: PDF read failed {pdf_path.name}: {e}")
        return ""


def extract_pdf_page_texts(pdf_path: Path) -> List[str]:
    try:
        doc = fitz.open(str(pdf_path))
        pages = [page.get_text() for page in doc]
        doc.close()
        return pages
    except Exception as e:
        print(f"  WARN: page text extraction failed {pdf_path.name}: {e}")
        return []


def relative_docs_url(path: Path) -> str:
    return path.relative_to(DOCS_DIR).as_posix()


def write_page_range_pdf(source_pdf: Path, start_page: int, end_page: int, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    src = fitz.open(str(source_pdf))
    out = fitz.open()
    out.insert_pdf(src, from_page=start_page, to_page=end_page)
    out.save(str(output_path))
    out.close()
    src.close()


# ---------------------------------------------------------------------
# Step 1: scrape by-laws page (secondary enrichment only)
# ---------------------------------------------------------------------

def scrape_bylaws_page() -> List[Dict[str, Any]]:
    print("\n═══ Step 1: By-Laws Page ═══")
    soup = fetch_page(BYLAWS_PAGE)

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

        title = clean_title(m.group(2))
        full_url = urljoin(BASE_URL, href)
        pdf_url = full_url if full_url.lower().endswith((".pdf", ".doc", ".docx")) else None
        page_url = None if pdf_url else full_url

        bylaws.append({
            "number": number,
            "year": parse_year(number, href=full_url),
            "title": title,
            "date_passed": None,
            "pdf_url": pdf_url,
            "page_url": page_url,
            "source": "bylaws_page",
            "status": "approved",
            "votes": None,
            "meeting_date": None,
            "agenda_package_url": None,
            "minutes_url": None,
            "summary": None,
        })

    print(f"  Found {len(bylaws)} by-law(s) on listing page")
    return bylaws


# ---------------------------------------------------------------------
# Step 2: read meetings from canonical
# ---------------------------------------------------------------------

def load_meetings() -> List[Dict[str, Any]]:
    payload = load_json(MEETINGS_FILE, {"meetings": []})
    if isinstance(payload, list):
        return payload
    return payload.get("meetings", [])


# ---------------------------------------------------------------------
# Step 3: parse by-laws and resolutions from minutes
# ---------------------------------------------------------------------

def parse_bylaws_from_minutes(text: str, meeting: Dict[str, Any]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    blocks = re.split(r'(R\d{4}-\d+)', text)
    for i in range(1, len(blocks), 2):
        res_number = blocks[i]
        body = blocks[i + 1] if i + 1 < len(blocks) else ""

        bm = re.search(
            r'(?:pass|adopt)\s+By[\-\s]?Law\s*(?:No\.?\s*|Number\s*)?(\d{4}[\-–]\d{1,3})',
            body,
            re.IGNORECASE
        )
        if not bm:
            continue

        bylaw_num = normalize_number(bm.group(1))

        mm = re.match(r'\s*([A-Z]\.\s*\w+)\s*,\s*([A-Z]\.\s*\w+)', body)
        mover = mm.group(1).strip() if mm else None
        seconder = mm.group(2).strip() if mm else None

        tm = re.search(
            r'being\s+a\s+By[\-\s]?Law\s+(?:to\s+)?(.+?)(?:\.\s*$|\.\s*Read\s|;\s*Read\s)',
            body,
            re.IGNORECASE | re.DOTALL
        )
        title = None
        if tm:
            title = clean_title(tm.group(1))
            if len(title) > 180:
                title = title[:177] + "..."

        status = "approved" if re.search(r'\bCarried\b', body, re.IGNORECASE) else "pending"
        votes = f"Moved by {mover}, Seconded by {seconder}" if mover and seconder else None

        results.append({
            "number": bylaw_num,
            "year": parse_year(bylaw_num),
            "title": title or f"By-Law {bylaw_num}",
            "date_passed": meeting["date"],
            "pdf_url": None,
            "page_url": None,
            "source": "minutes",
            "status": status,
            "votes": votes,
            "meeting_date": meeting["date"],
            "agenda_package_url": meeting.get("package_url"),
            "minutes_url": meeting.get("minutes_url"),
            "summary": None,
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
    if "receive" in t or "receive the correspondence" in t:
        return "Receive"
    if "support" in t:
        return "Support"
    if "approve" in t:
        return "Approval"
    return "General"


def parse_resolutions_from_minutes(text: str, meeting: Dict[str, Any]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    blocks = re.split(r'(R\d{4}-\d+)', text)
    for i in range(1, len(blocks), 2):
        res_number = blocks[i].upper()
        body = blocks[i + 1] if i + 1 < len(blocks) else ""

        mm = re.match(r'\s*([A-Z]\.\s*\w+)\s*,\s*([A-Z]\.\s*\w+)', body)
        mover = mm.group(1).strip() if mm else None
        seconder = mm.group(2).strip() if mm else None

        motion_match = re.search(
            r':\s*(.*?)(?:\bCarried\b|\bDefeated\b|\bLost\b|\bWithdrawn\b)',
            f"{res_number}{body}",
            re.IGNORECASE | re.DOTALL
        )
        motion_text = motion_match.group(1).strip() if motion_match else body.strip()
        motion_text = re.sub(r'\s+', ' ', motion_text)

        if not motion_text:
            continue

        if re.search(r'\bDefeated\b|\bLost\b', body, re.IGNORECASE):
            status = "defeated"
        elif re.search(r'\bWithdrawn\b', body, re.IGNORECASE):
            status = "withdrawn"
        elif re.search(r'\bCarried\b', body, re.IGNORECASE):
            status = "carried"
        else:
            status = "unknown"

        bylaw_match = re.search(
            r'By[\-\s]?Law\s*(?:No\.?\s*|Number\s*)?(\d{4}[\-–]\d{1,3})',
            motion_text,
            re.IGNORECASE
        )
        bylaw_number = normalize_number(bylaw_match.group(1)) if bylaw_match else None

        title = motion_text
        if len(title) > 140:
            title = title[:137] + "..."

        votes = f"Moved by {mover}, Seconded by {seconder}" if mover and seconder else None

        results.append({
            "number": res_number,
            "title": title,
            "motion_text": motion_text,
            "meeting_date": meeting["date"],
            "minutes_url": meeting.get("minutes_url"),
            "status": status,
            "votes": votes,
            "mover": mover,
            "seconder": seconder,
            "is_bylaw": bool(bylaw_number),
            "bylaw_number": bylaw_number,
            "category": categorize_resolution(motion_text),
            "pdf_url": None,
            "agenda_package_url": meeting.get("package_url"),
        })

    return results


def parse_all_minutes(meetings: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    print("\n═══ Step 2: Parsing Minutes ═══")
    all_bylaws: List[Dict[str, Any]] = []
    all_resolutions: List[Dict[str, Any]] = []

    for meeting in meetings:
        if not meeting.get("minutes_url"):
            continue

        pdf_path = download_pdf(meeting["minutes_url"], DOWNLOAD_CACHE_DIR / "minutes")
        if not pdf_path:
            continue

        text = extract_pdf_text(pdf_path)
        if not text or len(text.strip()) < 100:
            print(f"  WARN: no text from minutes {meeting['date']}")
            continue

        bylaws = parse_bylaws_from_minutes(text, meeting)
        resolutions = parse_resolutions_from_minutes(text, meeting)

        if bylaws:
            labels = ", ".join(b["number"] for b in bylaws)
            print(f"  {meeting['display_date']}: {len(bylaws)} by-law(s) — {labels}")

        if resolutions:
            print(f"  {meeting['display_date']}: {len(resolutions)} resolution(s)")

        all_bylaws.extend(bylaws)
        all_resolutions.extend(resolutions)

    print(f"  Total by-laws from minutes: {len(all_bylaws)}")
    print(f"  Total resolutions from minutes: {len(all_resolutions)}")
    return all_bylaws, all_resolutions


# ---------------------------------------------------------------------
# Step 4: extract standalone PDFs from agenda packages
# ---------------------------------------------------------------------

def find_target_start_pages(page_texts: List[str], targets: List[str]) -> Dict[str, int]:
    starts: Dict[str, int] = {}
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
    prefix: str = ""
) -> Dict[str, str]:
    """
    Return { target_number: relative_docs_url }
    """
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

    ordered = sorted(start_map.items(), key=lambda x: x[1])
    exported: Dict[str, str] = {}

    for i, (target, start_page) in enumerate(ordered):
        next_start = ordered[i + 1][1] if i + 1 < len(ordered) else len(page_texts)
        end_page = max(start_page, next_start - 1)

        safe_target = target.replace("/", "-")
        output_path = output_dir / f"{safe_target}.pdf"

        try:
            write_page_range_pdf(package_path, start_page, end_page, output_path)
            exported[target] = relative_docs_url(output_path)
        except Exception as e:
            print(f"  WARN: could not export {target} from {meeting['date']}: {e}")

    return exported


def enrich_from_packages(
    meetings: List[Dict[str, Any]],
    bylaws: List[Dict[str, Any]],
    resolutions: List[Dict[str, Any]]
) -> None:
    print("\n═══ Step 3: Extracting PDFs from Agenda Packages ═══")

    bylaws_by_meeting: Dict[str, List[str]] = {}
    for b in bylaws:
        if b.get("meeting_date") and b.get("number"):
            bylaws_by_meeting.setdefault(b["meeting_date"], []).append(normalize_number(b["number"]))

    resolutions_by_meeting: Dict[str, List[str]] = {}
    for r in resolutions:
        if r.get("meeting_date") and r.get("number"):
            resolutions_by_meeting.setdefault(r["meeting_date"], []).append(resolution_key(r))

    bylaw_pdf_map: Dict[str, str] = {}
    resolution_pdf_map: Dict[str, str] = {}

    for meeting in meetings:
        meeting_date = meeting.get("date")
        if not meeting.get("package_url") or not meeting_date:
            continue

        # By-laws
        bylaw_targets = sorted(set(bylaws_by_meeting.get(meeting_date, [])))
        if bylaw_targets:
            exported = export_targets_from_package(
                meeting,
                bylaw_targets,
                EXTRACTED_BYLAWS_DIR
            )
            bylaw_pdf_map.update(exported)
            if exported:
                print(f"  {meeting_date}: extracted {len(exported)} by-law PDF(s)")

        # Resolutions
        resolution_targets = sorted(set(resolutions_by_meeting.get(meeting_date, [])))
        if resolution_targets:
            exported = export_targets_from_package(
                meeting,
                resolution_targets,
                EXTRACTED_RESOLUTIONS_DIR
            )
            resolution_pdf_map.update(exported)
            if exported:
                print(f"  {meeting_date}: extracted {len(exported)} resolution PDF(s)")

    # Attach exported PDFs
    for b in bylaws:
        num = normalize_number(b.get("number", ""))
        if num in bylaw_pdf_map:
            b["pdf_url"] = bylaw_pdf_map[num]

    for r in resolutions:
        num = resolution_key(r)
        if num in resolution_pdf_map:
            r["pdf_url"] = resolution_pdf_map[num]


# ---------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------

def merge_bylaw(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(existing)

    for key in (
        "title", "pdf_url", "page_url", "votes", "meeting_date",
        "minutes_url", "agenda_package_url", "summary"
    ):
        if not out.get(key) and new.get(key):
            out[key] = new[key]

    if out.get("title", "").startswith("By-Law ") and new.get("title") and not new["title"].startswith("By-Law "):
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


def merge_bylaws(existing: List[Dict[str, Any]], incoming_lists: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
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
            if num in idx:
                idx[num] = merge_bylaw(idx[num], b)
            else:
                idx[num] = deepcopy(b)

    out = list(idx.values())
    out.sort(key=lambda b: (b.get("year") or 0, b.get("number", "")), reverse=True)
    return out


def merge_resolution(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(existing)
    for key in (
        "title", "motion_text", "minutes_url", "meeting_date",
        "status", "votes", "mover", "seconder", "is_bylaw",
        "bylaw_number", "category", "pdf_url", "agenda_package_url"
    ):
        if not out.get(key) and new.get(key) is not None:
            out[key] = new[key]
    return out


def merge_resolutions(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    idx: Dict[str, Dict[str, Any]] = {
        resolution_key(r): deepcopy(r)
        for r in existing
        if isinstance(r, dict) and r.get("number")
    }

    for r in incoming:
        num = resolution_key(r)
        if not num:
            continue
        if num in idx:
            idx[num] = merge_resolution(idx[num], r)
        else:
            idx[num] = deepcopy(r)

    out = list(idx.values())
    out.sort(key=lambda r: (r.get("meeting_date") or "", r.get("number") or ""), reverse=True)
    return out


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("Nipissing update_bylaws.py")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    meetings = load_meetings()
    print(f"Meetings loaded: {len(meetings)}")

    bylaws_payload = load_json(BYLAWS_FILE, {"last_updated": None, "source": BYLAWS_PAGE, "bylaws": []})
    resolutions_payload = load_json(RESOLUTIONS_FILE, {"last_updated": None, "resolutions": []})

    existing_bylaws = bylaws_payload.get("bylaws", []) if isinstance(bylaws_payload, dict) else bylaws_payload
    existing_resolutions = resolutions_payload.get("resolutions", []) if isinstance(resolutions_payload, dict) else resolutions_payload

    print(f"Existing bylaws: {len(existing_bylaws)}")
    print(f"Existing resolutions: {len(existing_resolutions)}")

    page_bylaws = scrape_bylaws_page()
    minutes_bylaws, minutes_resolutions = parse_all_minutes(meetings)

    # package extraction uses only numbers already found in minutes
    enrich_from_packages(meetings, minutes_bylaws, minutes_resolutions)

    final_bylaws = merge_bylaws(existing_bylaws, [page_bylaws, minutes_bylaws])
    final_resolutions = merge_resolutions(existing_resolutions, minutes_resolutions)

    bylaws_payload = {
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "source": BYLAWS_PAGE,
        "bylaws": final_bylaws,
    }
    resolutions_payload = {
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "resolutions": final_resolutions,
    }

    save_json(BYLAWS_FILE, bylaws_payload)
    save_json(RESOLUTIONS_FILE, resolutions_payload)

    print(f"\nSaved {len(final_bylaws)} by-law(s) -> {BYLAWS_FILE}")
    print(f"Saved {len(final_resolutions)} resolution(s) -> {RESOLUTIONS_FILE}")


if __name__ == "__main__":
    main()
