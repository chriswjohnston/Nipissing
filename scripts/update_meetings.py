#!/usr/bin/env python3
"""
update_meetings.py

Forward-looking council meetings updater for the Nipissing public records repo.

What this does:
- Scrapes the current council meetings page
- Captures scheduled future meetings even if agenda/minutes/package links do not exist yet
- Preserves existing historical records already in canonical data
- Preserves existing extra_docs / summary / video_url fields where the scraper does not have better data
- Matches YouTube videos using the channel RSS feed and dated council-meeting titles

This script intentionally stays simpler than the old historical scraper.
It updates the modern canonical JSON instead of rebuilding the whole archive from scratch.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

SOURCE_URL = "https://nipissingtownship.com/council-meeting-dates-agendas-minutes/"
YOUTUBE_CHANNEL_ID = "UC2XSMZqRNHbwVppelfKcEXw"
YOUTUBE_RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIR = ROOT / "data" / "canonical"
RUNTIME_DIR = ROOT / "data" / "runtime"

MEETINGS_FILE = CANONICAL_DIR / "meetings.json"
YOUTUBE_STATE_FILE = RUNTIME_DIR / "youtube_state.json"

HEADERS = {
    "User-Agent": "nipissing-public-records/1.0 (civic archive updater)"
}

DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)

SKIP_VIDEO_WORDS = (
    "committee",
    "adjustment",
    "conservation",
    "museum",
    "recreation",
    "cemetery",
)

# ---------------------------------------------------------------------
# Basic helpers
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


def normalize_absolute_url(href: str) -> str:
    return urljoin("https://nipissingtownship.com", href.strip())


def meeting_key(meeting: Dict[str, Any]) -> Tuple[str, str]:
    return (
        meeting.get("date", ""),
        meeting.get("meeting_type", "Regular") or "Regular",
    )


def classify_link(label: str, href: str) -> str:
    text = f"{label} {href}".lower()

    if "package" in text:
        return "package"
    if "minute" in text:
        return "minutes"
    if "agenda" in text:
        return "agenda"
    return "other"


def clean_label(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = text.strip("()[]")
    return text or "Document"


def unique_extra_docs(docs: List[Dict[str, str]]) -> List[Dict[str, str]]:
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


# ---------------------------------------------------------------------
# YouTube RSS
# ---------------------------------------------------------------------

def fetch_youtube_videos(existing_state: Dict[str, Any]) -> Dict[str, str]:
    """
    Return mapping:
      "Month D, YYYY" -> youtube_url

    We only keep videos that appear to be council meetings with a date in title.
    """
    prior = existing_state.get("_youtube_videos", {})
    videos = {
        item["date"]: item["url"]
        for item in prior.values()
        if isinstance(item, dict) and item.get("date") and item.get("url")
    }

    print(f"Fetching YouTube RSS... ({len(videos)} previously saved)")
    try:
        resp = requests.get(YOUTUBE_RSS_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(resp.content)

        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)

            if title_el is None or link_el is None:
                continue

            title = (title_el.text or "").strip()
            url = (link_el.get("href") or "").strip()
            title_l = title.lower()

            if "council meeting" not in title_l:
                continue
            if any(word in title_l for word in SKIP_VIDEO_WORDS):
                continue

            m = DATE_RE.search(title)
            if not m:
                continue

            dt = datetime.strptime(
                f"{m.group(1).capitalize()} {int(m.group(2))}, {m.group(3)}",
                "%B %d, %Y",
            ).date()
            display = f"{dt.strftime('%B')} {dt.day}, {dt.year}"

            if display not in videos:
                videos[display] = url
                print(f"  YouTube: {display} -> {url}")

    except Exception as e:
        print(f"  Could not fetch YouTube RSS: {e}")

    existing_state["_youtube_videos"] = {
        d: {"date": d, "url": u}
        for d, u in videos.items()
    }
    save_json(YOUTUBE_STATE_FILE, existing_state)

    print(f"  Found {len(videos)} dated video(s) total")
    return videos


# ---------------------------------------------------------------------
# Meeting page scraping
# ---------------------------------------------------------------------

def flags_from_context(context_text: str) -> Tuple[str, bool]:
    ctx = (context_text or "").lower()
    meeting_type = "Special" if "special" in ctx else "Regular"
    cancelled = "cancel" in ctx
    return meeting_type, cancelled


def extract_meetings_from_content(content: Tag) -> List[Dict[str, Any]]:
    """
    Parse the visible council page in DOM order.

    Handles rows where:
    - the date and links are in the same element
    - the date is in one inline chunk and links follow in the same parent
    - extra docs appear beside agenda/minutes/package
    """
    meetings: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    def flush_current() -> None:
        nonlocal current
        if current is None:
            return

        extra_docs = unique_extra_docs(current.pop("extra_docs_raw", []))

        meetings.append({
            "date": current["date"],
            "display_date": current["display_date"],
            "year": current["year"],
            "meeting_type": current["meeting_type"],
            "title": "",
            "agenda_url": current.get("agenda_url"),
            "minutes_url": current.get("minutes_url"),
            "package_url": current.get("package_url"),
            "extra_docs": extra_docs,
            "video_url": None,
            "summary": None,
            "cancelled": current.get("cancelled", False),
        })
        current = None

    def start_meeting(dt: date, context_text: str) -> None:
        nonlocal current
        flush_current()

        meeting_type, cancelled = flags_from_context(context_text)
        current = {
            "date": dt.isoformat(),
            "display_date": format_display_date(dt),
            "year": dt.year,
            "meeting_type": meeting_type,
            "agenda_url": None,
            "minutes_url": None,
            "package_url": None,
            "extra_docs_raw": [],
            "cancelled": cancelled,
        }

    def attach_links_from_element(el: Tag) -> None:
        nonlocal current
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
            else:
                current["extra_docs_raw"].append({
                    "label": label,
                    "url": href,
                })

    # Prefer row-like elements first
    candidates = content.find_all(["p", "li", "div", "tr"])

    for el in candidates:
        text = " ".join(el.stripped_strings)
        if not text:
            continue

        dm = DATE_RE.search(text)

        if dm:
            dt = datetime.strptime(
                f"{dm.group(1).capitalize()} {int(dm.group(2))}, {dm.group(3)}",
                "%B %d, %Y",
            ).date()
            start_meeting(dt, text)
            attach_links_from_element(el)
            continue

        # no new date, but if this row still has links and we already have a current meeting,
        # treat them as belonging to the current row
        if current is not None and el.find("a", href=True):
            attach_links_from_element(el)

    flush_current()

    # Merge duplicates by date + meeting_type
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for m in meetings:
        k = meeting_key(m)
        if k not in merged:
            merged[k] = deepcopy(m)
            continue

        old = merged[k]
        for field in ("agenda_url", "minutes_url", "package_url"):
            if not old.get(field) and m.get(field):
                old[field] = m[field]

        old["extra_docs"] = unique_extra_docs(
            (old.get("extra_docs") or []) + (m.get("extra_docs") or [])
        )
        old["cancelled"] = old.get("cancelled", False) or m.get("cancelled", False)

    out = list(merged.values())
    out.sort(key=lambda x: x["date"], reverse=True)
    return out

    def process_container(el: Tag) -> None:
        nonlocal current

        text = " ".join(el.stripped_strings)
        if not text:
            return

        dm = DATE_RE.search(text)
        if dm:
            dt = datetime.strptime(
                f"{dm.group(1).capitalize()} {int(dm.group(2))}, {dm.group(3)}",
                "%B %d, %Y",
            ).date()
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

    # Prefer line-like containers first.
    containers = content.find_all(["p", "li", "tr", "div"])
    seen_dates = set()

    for el in containers:
        text = " ".join(el.stripped_strings)
        if not text:
            continue
        if DATE_RE.search(text) or el.find("a", href=True):
            before_len = len(meetings)
            current_before = None if current is None else current.get("date")
            process_container(el)

            # If a same-date line reappears later, let it update the current object
            # but do not produce duplicates in final output.
            # We'll de-dupe after the parse.
            if current is not None and current.get("date") != current_before and current["date"] in seen_dates:
                # same meeting date encountered again, still okay; final merge handles duplicates
                pass

            if len(meetings) > before_len:
                seen_dates.add(meetings[-1]["date"])

    flush_current()

    # Deduplicate same date + meeting type by merging links/extra_docs
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for m in meetings:
        k = meeting_key(m)
        if k not in merged:
            merged[k] = deepcopy(m)
            continue

        old = merged[k]
        for field in ("agenda_url", "minutes_url", "package_url"):
            if not old.get(field) and m.get(field):
                old[field] = m[field]
        old["extra_docs"] = unique_extra_docs((old.get("extra_docs") or []) + (m.get("extra_docs") or []))
        old["cancelled"] = old.get("cancelled", False) or m.get("cancelled", False)

    out = list(merged.values())
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def scrape_current_meetings() -> List[Dict[str, Any]]:
    print(f"Fetching council meetings page: {SOURCE_URL}")
    resp = requests.get(SOURCE_URL, headers=HEADERS, timeout=30)
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

    meetings = extract_meetings_from_content(content)
    print(f"  Scraped {len(meetings)} meeting record(s) from current page")
    return meetings


# ---------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------

def merge_meeting(existing: Dict[str, Any], scraped: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge one scraped meeting into one existing canonical meeting.
    New scrape can improve agenda/minutes/package.
    Existing canonical preserves summary, extra_docs, historical youtube, etc.
    """
    out = deepcopy(existing)

    # Always keep latest basics from scrape
    for field in ("display_date", "year", "meeting_type", "cancelled", "title"):
        if scraped.get(field) not in (None, ""):
            out[field] = scraped[field]

    # Prefer scraped URLs when present
    for field in ("agenda_url", "minutes_url", "package_url"):
        if scraped.get(field):
            out[field] = scraped[field]
        else:
            out.setdefault(field, existing.get(field))

    # Preserve / merge extra docs
    merged_extra = unique_extra_docs(
        (existing.get("extra_docs") or []) + (scraped.get("extra_docs") or [])
    )
    out["extra_docs"] = merged_extra

    # Preserve summary
    if not out.get("summary"):
        out["summary"] = existing.get("summary")

    # Preserve video for now; later youtube step can improve it
    if not out.get("video_url"):
        out["video_url"] = existing.get("video_url")

    # Preserve any other known fields if present
    for field in ("source_kind", "body", "body_id"):
        if existing.get(field) and not out.get(field):
            out[field] = existing[field]

    return out


def merge_canonical(existing_meetings: List[Dict[str, Any]], scraped_meetings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    existing_index = {meeting_key(m): deepcopy(m) for m in existing_meetings}
    scraped_index = {meeting_key(m): deepcopy(m) for m in scraped_meetings}

    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # Start with scraped meetings, enriched by existing
    for k, scraped in scraped_index.items():
        if k in existing_index:
            merged[k] = merge_meeting(existing_index[k], scraped)
        else:
            new_meeting = deepcopy(scraped)
            new_meeting.setdefault("title", "")
            new_meeting.setdefault("video_url", None)
            new_meeting.setdefault("summary", None)
            new_meeting.setdefault("cancelled", False)
            new_meeting["extra_docs"] = unique_extra_docs(new_meeting.get("extra_docs") or [])
            merged[k] = new_meeting

    # Carry forward older existing meetings not visible on live page anymore
    for k, old in existing_index.items():
        if k not in merged:
            merged[k] = deepcopy(old)

    out = list(merged.values())
    out.sort(key=lambda x: (x.get("date") or ""), reverse=True)
    return out


def attach_youtube(meetings: List[Dict[str, Any]], videos_by_display_date: Dict[str, str]) -> None:
    for meeting in meetings:
        if meeting.get("video_url"):
            continue
        display_full = meeting.get("display_date", "")
        try:
            parsed = datetime.strptime(display_full, "%b %d, %Y").date()
            full_display = f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
        except Exception:
            full_display = ""

        if full_display and full_display in videos_by_display_date:
            meeting["video_url"] = videos_by_display_date[full_display]


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    current_payload = load_json(MEETINGS_FILE, {"meetings": []})
    if isinstance(current_payload, list):
        current_payload = {"meetings": current_payload}

    existing_meetings = current_payload.get("meetings", [])
    youtube_state = load_json(YOUTUBE_STATE_FILE, {})

    print("=" * 60)
    print("Nipissing update_meetings.py")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Existing canonical meetings: {len(existing_meetings)}")
    print("=" * 60)

    videos_by_display_date = fetch_youtube_videos(youtube_state)
    scraped_meetings = scrape_current_meetings()
    merged_meetings = merge_canonical(existing_meetings, scraped_meetings)
    attach_youtube(merged_meetings, videos_by_display_date)

    payload = {
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "source": SOURCE_URL,
        "meetings": merged_meetings,
    }

    save_json(MEETINGS_FILE, payload)

    print(f"Saved {len(merged_meetings)} meeting(s) -> {MEETINGS_FILE}")
    print(f"  with agenda:  {sum(1 for m in merged_meetings if m.get('agenda_url'))}")
    print(f"  with minutes: {sum(1 for m in merged_meetings if m.get('minutes_url'))}")
    print(f"  with package: {sum(1 for m in merged_meetings if m.get('package_url'))}")
    print(f"  with video:   {sum(1 for m in merged_meetings if m.get('video_url'))}")
    print(f"  with extras:  {sum(1 for m in merged_meetings if m.get('extra_docs'))}")


if __name__ == "__main__":
    main()
