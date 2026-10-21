#!/usr/bin/env python3
"""
update_library.py

Powassan & District Union Public Library Board updater.

The library is a joint board of Powassan, Nipissing, Chisholm and Restoule.
Nipissing appoints members to it, so its meetings belong in the public record
alongside the township's own boards.

Sources:
- Board page (Wix): https://www.powassanlibrary.com/board-members
    * board members grouped by municipality
    * the published meeting schedule (future dates)
    * links to the two public Google Drive folders
- Google Drive (public folders, read with an API key):
    * Agendas folder
    * Minutes folder

What this does:
- Lists both Drive folders (and any subfolders) with the Drive API v3
- Downloads each new or changed PDF once, works out its meeting date, and
  mirrors it into docs/files/archive/boards/library/<year>/ (agenda packages
  go to the rolling GitHub Release, like township packages)
- Merges everything into the "library" board in data/canonical/boards.json
- Never deletes: files removed from Drive stay in the archive, and a file the
  library replaces in place (same Drive file, new content) keeps its earlier
  version as an extra document

Dates: filenames are inconsistent ("04 Agenda April , 2026.pdf" has no day),
so the date comes from the filename when it holds a full date, otherwise from
the first page of the PDF ("Agenda for Monday, Sept 21, 2026"). A file whose
date can't be found is logged and skipped; add it to MANUAL_DATES.

State: data/runtime/library_drive.json remembers every Drive file version
already processed, so a normal run makes two folder listings and downloads
nothing. It is permanent state — do not delete it.

Requires GOOGLE_API_KEY (repo secret). Without it the Drive step is skipped
and stored data is kept; the board page is still scraped.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from archive import INREPO_MAX_BYTES, store_bytes_in_repo, store_bytes_to_release

ROOT = Path(__file__).resolve().parents[1]
BOARDS_FILE = ROOT / "data" / "canonical" / "boards.json"
STATE_FILE = ROOT / "data" / "runtime" / "library_drive.json"

TODAY = date.today()

BOARD = {
    "id": "library",
    "name": "Powassan & District Union Public Library Board",
    "short_name": "Library Board",
    "url": "https://www.powassanlibrary.com/board-members",
    "description": (
        "Joint library board for Powassan, Nipissing, Chisholm and Restoule. "
        "Nipissing Township appoints members to the board."
    ),
}

# Folder IDs as linked from the board page. The page is re-read each run and
# any folder it links to is used instead, so a moved folder is picked up.
DEFAULT_FOLDERS = {
    "agenda": "1Z6dnutXqtE9px7DQC1Jt6zG4O0M1cK4n",
    "minutes": "1ZGer-dtqzuCqskaGW_sbzwlD-A4cka2E",
}

# Drive file id -> "YYYY-MM-DD", for files whose date can't be detected.
MANUAL_DATES: Dict[str, str] = {}

# Drive file ids that are not meeting documents and should be ignored.
IGNORE_FILE_IDS: set = set()

HOME_MUNICIPALITY = "Township of Nipissing"

DRIVE_API = "https://www.googleapis.com/drive/v3"
API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

PDF_MIME = "application/pdf"
FOLDER_MIME = "application/vnd.google-apps.folder"
GDOC_MIMES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.spreadsheet",
}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# Full or abbreviated month, optional period, day with optional suffix, year.
DATE_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\.?"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    re.IGNORECASE,
)
DRIVE_FOLDER_RE = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([\w-]+)")
MUNICIPALITY_RE = re.compile(r"^(Township|Municipality|Town|City|Village) of\s+\S", re.I)


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


def format_display_date(dt: date) -> str:
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def plausible(dt: date) -> bool:
    return 2010 <= dt.year <= TODAY.year + 2


def find_date(text: str) -> Optional[date]:
    """First plausible full date in the text."""
    for m in DATE_RE.finditer(text or ""):
        try:
            dt = date(int(m.group(3)), MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
        except ValueError:
            continue
        if plausible(dt):
            return dt
    return None


def clean_text(s: str) -> str:
    s = (s or "").replace("​", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def slugify(name: str) -> str:
    base = re.sub(r"\.pdf$", "", name or "document", flags=re.I)
    base = re.sub(r"[^\w\-]+", "_", base).strip("_")
    return base[:60] or "document"


def drive_view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def classify(name: str, folder_kind: str) -> str:
    n = (name or "").lower()
    if "package" in n:
        return "package"
    if "minute" in n:
        return "minutes"
    if "agenda" in n:
        return "agenda"
    return folder_kind


def new_meeting(dt: date) -> Dict[str, Any]:
    return {
        "date": dt.isoformat(),
        "display_date": format_display_date(dt),
        "year": dt.year,
        "meeting_type": "Board",
        "title": BOARD["name"],
        "board_id": BOARD["id"],
        "board_name": BOARD["name"],
        "body": BOARD["short_name"],   # short: shown in calendar badges
        "body_id": BOARD["id"],
        "source_kind": "board",
        "external": True,
        "agenda_url": None,
        "minutes_url": None,
        "package_url": None,
        "extra_docs": [],
        "summary": None,
        "events": [],
        "cancelled": False,
        "rescheduled": False,
        "postponed": False,
        "is_future": dt > TODAY,
        "video_url": None,
    }


# ---------------------------------------------------------------------
# Board page (members, schedule, folder links)
# ---------------------------------------------------------------------

def scrape_board_page() -> Dict[str, Any]:
    """Returns {"members": [...], "schedule": [date...], "folders": {...}}.
    Each part is empty when it can't be found, so callers keep stored data."""
    print(f"Scraping {BOARD['url']} ...")
    r = requests.get(BOARD["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    folders: Dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        m = DRIVE_FOLDER_RE.search(a["href"])
        if not m:
            continue
        label = clean_text(a.get_text(" ", strip=True)).lower()
        if "agenda" in label:
            folders["agenda"] = m.group(1)
        elif "minute" in label:
            folders["minutes"] = m.group(1)

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [clean_text(x) for x in soup.get_text("\n").split("\n")]
    lines = [x for x in lines if x]

    # Members: from the "Board Members" heading to the first "Meeting" heading.
    members: List[Dict[str, str]] = []
    try:
        start = next(i for i, x in enumerate(lines) if "board members" in x.lower())
        end = next(i for i in range(start + 1, len(lines)) if "meeting" in lines[i].lower())
        municipality = None
        for line in lines[start + 1:end]:
            if MUNICIPALITY_RE.match(line):
                municipality = line
                continue
            if not municipality or len(line) > 80:
                continue
            name, role = line, None
            rm = re.match(r"^(.*?)\s*[-–—]\s*(Chair|Vice[ -]?Chair|Secretary|Treasurer)\s*$", line, re.I)
            if rm:
                name, role = rm.group(1).strip(), rm.group(2).strip()
            members.append({"name": name, "role": role, "municipality": municipality})
    except StopIteration:
        pass

    # Schedule: dated lines after the first "Meeting" heading, before the
    # statutory-holiday list (which has no years, but stop there anyway).
    schedule: List[date] = []
    in_sched = False
    for line in lines:
        low = line.lower()
        if "meeting" in low and ("schedule" in low or "remainder" in low):
            in_sched = True
            continue
        if in_sched and "statutory" in low:
            break
        if in_sched:
            dt = find_date(line)
            if dt and dt not in schedule:
                schedule.append(dt)

    print(f"  {len(members)} member(s), {len(schedule)} scheduled date(s), "
          f"{len(folders)} Drive folder link(s)")
    return {"members": members, "schedule": schedule, "folders": folders}


# ---------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------

def drive_get(path: str, params: Dict[str, Any], stream: bool = False) -> requests.Response:
    params = dict(params, key=API_KEY)
    r = requests.get(f"{DRIVE_API}/{path}", params=params, headers=HEADERS,
                     timeout=120, stream=stream)
    r.raise_for_status()
    return r


def list_folder(folder_id: str, depth: int = 0) -> List[Dict[str, Any]]:
    """All non-folder files under a folder, recursing into subfolders."""
    files: List[Dict[str, Any]] = []
    token = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken, files(id, name, mimeType, md5Checksum, "
                      "modifiedTime, createdTime, size)",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if token:
            params["pageToken"] = token
        data = drive_get("files", params).json()
        for f in data.get("files", []):
            if f.get("mimeType") == FOLDER_MIME:
                if depth < 3:
                    files.extend(list_folder(f["id"], depth + 1))
            else:
                files.append(f)
        token = data.get("nextPageToken")
        if not token:
            return files


def download(f: Dict[str, Any]) -> Optional[bytes]:
    try:
        if f.get("mimeType") in GDOC_MIMES:
            data = drive_get(f"files/{f['id']}/export", {"mimeType": PDF_MIME}).content
        else:
            data = drive_get(f"files/{f['id']}", {"alt": "media",
                                                  "supportsAllDrives": "true"}).content
    except Exception as e:
        print(f"  ✗ download failed {f.get('name')}: {e}")
        return None
    if not data[:5].startswith(b"%PDF"):
        print(f"  ✗ not a PDF, skipped: {f.get('name')}")
        return None
    return data


def first_page_text(data: bytes) -> str:
    try:
        import pymupdf as fitz  # pymupdf
        doc = fitz.open(stream=data, filetype="pdf")
        return doc[0].get_text() if len(doc) else ""
    except Exception as e:
        print(f"  pdf text failed: {e}")
        return ""


def version_of(f: Dict[str, Any]) -> str:
    return f.get("md5Checksum") or f.get("modifiedTime") or ""


def wanted(f: Dict[str, Any]) -> bool:
    if f["id"] in IGNORE_FILE_IDS:
        return False
    mime = f.get("mimeType", "")
    name = f.get("name", "")
    if name.startswith("."):
        return False  # .DS_Store and friends
    return mime == PDF_MIME or mime in GDOC_MIMES or name.lower().endswith(".pdf")


def resolve_date(f: Dict[str, Any], data: bytes) -> Tuple[Optional[date], str]:
    if f["id"] in MANUAL_DATES:
        return date.fromisoformat(MANUAL_DATES[f["id"]]), "manual"
    from_name = find_date(f.get("name", ""))
    from_pdf = find_date(first_page_text(data)[:3000])
    if from_name and from_pdf and from_name != from_pdf:
        print(f"  ! date mismatch for {f['name']}: filename {from_name}, "
              f"PDF {from_pdf} — using filename")
    if from_name:
        return from_name, "filename"
    if from_pdf:
        return from_pdf, "pdf"
    return None, "none"


def archive_bytes(f: Dict[str, Any], kind: str, dt: date, data: bytes) -> Optional[str]:
    fname = f"{slugify(f.get('name'))}__{f['id'][:10]}_{version_of(f)[:8] or 'v'}.pdf"
    fname = re.sub(r"[^\w\-.]", "_", fname)
    src = drive_view_url(f["id"])
    if kind == "package":
        got = store_bytes_to_release(src, data, asset_name=f"library_{fname}")
        if got:
            return got
        if len(data) > INREPO_MAX_BYTES:
            return None
    return store_bytes_in_repo(src, "boards/library", dt.year, data, filename=fname)


def sync_drive(folders: Dict[str, str], state: Dict[str, Any]) -> int:
    """Process new/changed Drive files into state['files']. Returns count changed."""
    files_state: Dict[str, Any] = state.setdefault("files", {})
    changed = 0
    for folder_kind, folder_id in folders.items():
        print(f"Listing Drive folder ({folder_kind}) {folder_id} ...")
        try:
            listing = list_folder(folder_id)
        except Exception as e:
            print(f"  ✗ listing failed ({e}) — keeping stored data")
            continue
        print(f"  {len(listing)} file(s)")
        for f in listing:
            if not wanted(f):
                continue
            ver = version_of(f)
            entry = files_state.get(f["id"])
            if entry and entry.get("version") == ver:
                done = bool(entry.get("archived"))
                undated = entry.get("date") is None and f["id"] not in MANUAL_DATES
                if done or undated:
                    # Already handled (or waiting on a MANUAL_DATES entry).
                    entry["last_seen"] = TODAY.isoformat()
                    entry["name"] = f.get("name")
                    continue

            print(f"  processing {f.get('name')}")
            data = download(f)
            if data is None:
                continue  # transient or not a PDF: retried next run
            dt, how = resolve_date(f, data)
            kind = classify(f.get("name", ""), folder_kind)
            if dt is None:
                print(f"  ! no date found for {f['name']} ({f['id']}) — add to MANUAL_DATES")
                files_state[f["id"]] = {
                    "name": f.get("name"), "kind": kind, "version": ver, "date": None,
                    "archived": None, "last_seen": TODAY.isoformat(),
                }
                changed += 1
                continue
            archived = archive_bytes(f, kind, dt, data)
            history = list((entry or {}).get("history", []))
            if entry and entry.get("version") != ver and entry.get("archived"):
                history.append({k: entry.get(k) for k in
                                ("version", "archived", "modified", "name", "date")})
                print(f"  ↻ replaced in Drive: {f['name']} (earlier version kept)")
            files_state[f["id"]] = {
                "name": f.get("name"),
                "kind": kind,
                "folder": folder_kind,
                "version": ver,
                "date": dt.isoformat(),
                "date_source": how,
                "modified": f.get("modifiedTime"),
                "created": f.get("createdTime"),
                "archived": archived,
                "first_seen": (entry or {}).get("first_seen") or TODAY.isoformat(),
                "last_seen": TODAY.isoformat(),
                "history": history,
            }
            changed += 1
            print(f"    → {kind} for {dt} (date from {how})")
    return changed


# ---------------------------------------------------------------------
# Merge into boards.json
# ---------------------------------------------------------------------

LABELS = {"agenda": "Agenda", "minutes": "Minutes", "package": "Agenda Package"}


def apply_files(meetings: Dict[str, Dict[str, Any]], files_state: Dict[str, Any]) -> None:
    """Attach every known Drive file (current and removed) to its meeting."""
    groups: Dict[Tuple[str, str], List[Tuple[str, Dict[str, Any]]]] = {}
    for fid, e in files_state.items():
        if e.get("date"):
            groups.setdefault((e["date"], e["kind"]), []).append((fid, e))

    for (iso, kind), entries in groups.items():
        m = meetings.get(iso)
        if m is None:
            m = meetings[iso] = new_meeting(date.fromisoformat(iso))
        # Primary = most recently modified file of this kind for the date.
        entries.sort(key=lambda x: x[1].get("modified") or "", reverse=True)
        extras = m.get("extra_docs") or []
        for i, (fid, e) in enumerate(entries):
            url = drive_view_url(fid)
            if i == 0:
                m[f"{kind}_url"] = url
                if e.get("archived"):
                    m[f"{kind}_archived"] = e["archived"]
                m.setdefault("drive_files", {})[kind] = {
                    "id": fid, "name": e.get("name"), "posted": e.get("created"),
                    "modified": e.get("modified"),
                }
            else:
                extras.append({"label": e.get("name") or LABELS[kind], "url": url,
                               "archived": e.get("archived")})
            for h in e.get("history", []):
                if not h.get("archived"):
                    continue
                when = (h.get("modified") or "")[:10]
                extras.append({
                    "label": f"{LABELS[kind]} (earlier version{', ' + when if when else ''})",
                    "url": f"{url}?version={h.get('version', '')[:8]}",
                    "archived": h["archived"],
                })
        seen, out = set(), []
        for d in extras:
            k = (d.get("url") or "").lower()
            if k and k not in seen:
                seen.add(k)
                out.append({x: y for x, y in d.items() if y is not None})
        m["extra_docs"] = out


def main() -> None:
    print("=" * 60)
    print("Nipissing update_library.py")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    payload = load_json(BOARDS_FILE, {"boards": []})
    state = load_json(STATE_FILE, {"files": {}})

    boards = payload.setdefault("boards", [])
    board = next((b for b in boards if b.get("id") == BOARD["id"]), None)
    if board is None:
        board = {"id": BOARD["id"], "meetings": []}
        boards.append(board)
    board.update({
        "name": BOARD["name"],
        "short_name": BOARD["short_name"],
        "url": BOARD["url"],
        "description": BOARD["description"],
        "external": True,
    })

    page: Dict[str, Any] = {"members": [], "schedule": [], "folders": {}}
    try:
        page = scrape_board_page()
    except Exception as e:
        print(f"  ✗ board page failed ({e}) — keeping stored members/schedule")

    if page["members"]:
        board["members"] = page["members"]
        board["members_checked"] = TODAY.isoformat()
    folders = dict(DEFAULT_FOLDERS, **page["folders"])
    board["drive_folders"] = {k: f"https://drive.google.com/drive/folders/{v}"
                              for k, v in folders.items()}

    if API_KEY:
        n = sync_drive(folders, state)
        print(f"Drive: {n} file(s) new or changed")
    else:
        print("GOOGLE_API_KEY not set — skipping Drive, keeping stored documents")

    meetings = {m["date"]: m for m in board.get("meetings", []) if m.get("date")}
    for dt in page["schedule"]:
        meetings.setdefault(dt.isoformat(), new_meeting(dt))
    apply_files(meetings, state.get("files", {}))

    for m in meetings.values():
        try:
            m["is_future"] = date.fromisoformat(m["date"]) > TODAY
        except ValueError:
            pass
        m.setdefault("video_url", None)
        m["external"] = True
        m["body"] = BOARD["short_name"]
        m["board_name"] = BOARD["name"]

    board["meetings"] = sorted(meetings.values(), key=lambda x: x["date"], reverse=True)
    payload["last_updated"] = TODAY.isoformat()

    save_json(STATE_FILE, state)
    save_json(BOARDS_FILE, payload)

    ms = board["meetings"]
    print(f"Saved library board -> {BOARDS_FILE}")
    print(f"  meetings: {len(ms)} | agendas: {sum(1 for m in ms if m.get('agenda_url'))} | "
          f"packages: {sum(1 for m in ms if m.get('package_url'))} | "
          f"minutes: {sum(1 for m in ms if m.get('minutes_url'))} | "
          f"future: {sum(1 for m in ms if m.get('is_future'))}")
    nip = [x["name"] for x in board.get("members", []) if x.get("municipality") == HOME_MUNICIPALITY]
    if nip:
        print(f"  Nipissing appointees: {', '.join(nip)}")


if __name__ == "__main__":
    main()
