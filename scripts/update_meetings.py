#!/usr/bin/env python3

import json
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from archive import mirror_record


URL = "https://nipissingtownship.com/council-meeting-dates-agendas-minutes/"
OUT = Path("data/canonical/meetings.json")

# YouTube: township channel RSS (only returns the ~15 most recent uploads,
# so every match is also cached in data/runtime so nothing ages out unmatched).
YOUTUBE_CHANNEL_ID = "UC2XSMZqRNHbwVppelfKcEXw"
YOUTUBE_RSS = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
YOUTUBE_CACHE = Path("data/runtime/youtube_videos.json")

# Videos for other bodies posted to the same channel — never attach to council.
YOUTUBE_EXCLUDE = (
    "committee", "adjustment", "conservation", "museum",
    "recreation", "library", "board", "planning", "public meeting",
)

DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)

# Tolerant title-date matcher. Current township format:
#   "Township of Nipissing Council Meeting Sept 01.2026"
# Also accepts "September 1, 2026", "Sept. 1st, 2026", "Sep 1 2026",
# "Sept 01-2026", "Sept 01/2026". ISO dates (2026-09-01) handled separately.
YT_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
    r"\s*(\d{1,2})(?:st|nd|rd|th)?\s*[,./-]?\s*(\d{4})\b",
    re.IGNORECASE,
)
YT_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return "https://nipissingtownship.com" + href


def clean_label(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip(" ()")


def classify_link(label: str, url: str) -> str:
    text = f"{label} {url}".lower()
    if "agenda package" in text or "package" in text:
        return "package"
    if "minutes" in text or "minute" in text:
        return "minutes"
    if "agenda" in text:
        return "agenda"
    return "extra"


def meeting_key(m: Dict[str, Any]) -> Tuple[str, str]:
    return (m["date"], m.get("meeting_type", "Regular"))


def unique_extra_docs(docs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for d in docs:
        label = clean_label(d.get("label", ""))
        url = (d.get("url") or "").strip()
        if not url:
            continue
        key = (label, url)
        if key not in seen:
            seen.add(key)
            out.append({"label": label, "url": url})
    return out


def flags_from_context(text: str) -> Tuple[str, bool]:
    t = (text or "").lower()
    return (
        "Special" if "special" in t else "Regular",
        "cancel" in t,
    )


# ── YouTube ──────────────────────────────────────────────────────

def youtube_title_date(title: str) -> Optional[str]:
    """Return ISO date (YYYY-MM-DD) parsed from a video title, or None."""
    m = YT_DATE_RE.search(title)
    if m:
        month = MONTHS.get(m.group(1)[:3].lower())
        try:
            return datetime(int(m.group(3)), month, int(m.group(2))).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return None
    m = YT_ISO_RE.search(title)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def fetch_youtube_videos() -> Dict[str, Dict[str, str]]:
    """
    Read the channel RSS feed and merge council-meeting videos into the
    persistent cache. Returns {iso_date: {"url", "title", "special"}}.
    Logs every title it sees so a future naming change is obvious in the
    Actions log instead of failing silently.
    """
    cache = load_json(YOUTUBE_CACHE, {})
    before = len(cache)
    print(f"Fetching YouTube RSS ... ({before} cached)")

    try:
        resp = requests.get(YOUTUBE_RSS, timeout=20)
        resp.raise_for_status()
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(resp.content)
        entries = root.findall("atom:entry", ns)
        print(f"  Feed returned {len(entries)} entries")

        for entry in entries:
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            if title_el is None or link_el is None:
                continue
            title = (title_el.text or "").strip()
            url = link_el.get("href", "")
            tl = title.lower()

            if "council" not in tl:
                print(f"    skip (not council): {title}")
                continue
            if any(w in tl for w in YOUTUBE_EXCLUDE):
                print(f"    skip (other body):  {title}")
                continue

            iso = youtube_title_date(title)
            if not iso:
                print(f"    skip (no date):     {title}")
                continue

            if iso not in cache:
                cache[iso] = {"url": url, "title": title, "special": "special" in tl}
                print(f"    + {iso} -> {url}  [{title}]")
            else:
                print(f"    = {iso} already cached")

    except Exception as e:
        # Transient: keep using the cache, never wipe it.
        print(f"  Warning: could not fetch YouTube RSS: {e}")

    save_json(YOUTUBE_CACHE, cache)
    print(f"  {len(cache)} cached video(s) ({len(cache) - before} new)")
    return cache


def attach_videos(meetings: List[Dict[str, Any]], videos: Dict[str, Dict[str, str]]) -> None:
    """Fill video_url only where empty — never overwrite an existing link."""
    filled = 0
    for m in meetings:
        if m.get("video_url") or m.get("cancelled"):
            continue
        v = videos.get(m.get("date"))
        if not v:
            continue
        # If a Regular and a Special meeting share a date, match on type.
        is_special = m.get("meeting_type") == "Special"
        same_day = [x for x in meetings if x.get("date") == m.get("date")]
        if len(same_day) > 1 and bool(v.get("special")) != is_special:
            continue
        m["video_url"] = v["url"]
        filled += 1
        print(f"  video attached: {m['date']} ({m.get('meeting_type')})")
    print(f"  {filled} meeting(s) gained a video")


# ── Township scrape ──────────────────────────────────────────────

def extract_meetings(content: Tag) -> List[Dict[str, Any]]:
    meetings_raw: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    def flush() -> None:
        nonlocal current
        if current:
            meetings_raw.append(dict(current))
        current = None

    def start_meeting(month: str, day: str, year: str, context_text: str = "") -> None:
        nonlocal current
        flush()

        dt = datetime.strptime(
            f"{month.capitalize()} {int(day)}, {year}",
            "%B %d, %Y"
        )

        meeting_type, cancelled = flags_from_context(context_text)

        current = {
            "date": dt.strftime("%Y-%m-%d"),
            "display_date": f"{dt.strftime('%b')} {dt.day}, {dt.year}",
            "year": dt.year,
            "meeting_type": meeting_type,
            "agenda_url": None,
            "minutes_url": None,
            "package_url": None,
            "extra_docs_raw": [],
            "cancelled": cancelled,
        }

    def walk(node: Any) -> None:
        nonlocal current

        if isinstance(node, NavigableString):
            text = str(node)
            for dm in DATE_RE.finditer(text):
                start_meeting(dm.group(1), dm.group(2), dm.group(3), text)

        elif isinstance(node, Tag):
            if node.name == "a":
                if current:
                    href = node.get("href")
                    label = clean_label(node.get_text(" ", strip=True))

                    if href:
                        url = normalize_url(href)
                        kind = classify_link(label, url)

                        if kind == "agenda" and not current["agenda_url"]:
                            current["agenda_url"] = url
                        elif kind == "minutes" and not current["minutes_url"]:
                            current["minutes_url"] = url
                        elif kind == "package" and not current["package_url"]:
                            current["package_url"] = url
                        else:
                            current["extra_docs_raw"].append({
                                "label": label,
                                "url": url,
                            })
                return

            for child in node.children:
                walk(child)

    for el in content.find_all(["p", "li", "div", "tr"]):
        text = el.get_text(" ", strip=True)
        if DATE_RE.search(text) or el.find("a"):
            walk(el)

    flush()

    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for m in meetings_raw:
        k = meeting_key(m)
        if k not in merged:
            merged[k] = deepcopy(m)
            continue

        old = merged[k]
        for field in ("agenda_url", "minutes_url", "package_url"):
            if not old.get(field) and m.get(field):
                old[field] = m[field]

        old["extra_docs_raw"] += m.get("extra_docs_raw", [])

    out: List[Dict[str, Any]] = []
    for m in merged.values():
        out.append({
            "date": m["date"],
            "display_date": m["display_date"],
            "year": m["year"],
            "meeting_type": m["meeting_type"],
            "title": "",
            "agenda_url": m.get("agenda_url"),
            "minutes_url": m.get("minutes_url"),
            "package_url": m.get("package_url"),
            "extra_docs": unique_extra_docs(m.get("extra_docs_raw", [])),
            "video_url": None,
            "summary": None,
            "cancelled": m.get("cancelled", False),
        })

    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def merge_meetings(scraped: List[Dict[str, Any]], existing: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    existing_map = {meeting_key(m): deepcopy(m) for m in existing if isinstance(m, dict) and m.get("date")}
    final_map: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for m in scraped:
        k = meeting_key(m)
        if k in existing_map:
            old = existing_map[k]

            merged = deepcopy(old)
            merged["display_date"] = m.get("display_date", old.get("display_date"))
            merged["year"] = m.get("year", old.get("year"))
            merged["meeting_type"] = m.get("meeting_type", old.get("meeting_type", "Regular"))
            merged["title"] = old.get("title", "") or m.get("title", "")
            merged["agenda_url"] = m.get("agenda_url") or old.get("agenda_url")
            merged["minutes_url"] = m.get("minutes_url") or old.get("minutes_url")
            merged["package_url"] = m.get("package_url") or old.get("package_url")
            merged["video_url"] = old.get("video_url")
            merged["summary"] = old.get("summary")
            merged["cancelled"] = m.get("cancelled", old.get("cancelled", False))
            merged["extra_docs"] = unique_extra_docs(
                (old.get("extra_docs") or []) + (m.get("extra_docs") or [])
            )
            final_map[k] = merged
        else:
            final_map[k] = m

    for k, old in existing_map.items():
        if k not in final_map:
            final_map[k] = old

    final = list(final_map.values())
    final.sort(key=lambda x: x["date"], reverse=True)
    return final


def main() -> None:
    print("Fetching meetings...")

    res = requests.get(URL, timeout=30)
    res.raise_for_status()

    soup = BeautifulSoup(res.text, "html.parser")
    content = (
        soup.find("div", class_=re.compile(r"entry-content|post-content"))
        or soup.find("article")
        or soup.find("main")
        or soup.body
    )

    if content is None:
        raise RuntimeError("Could not find page content to parse")

    scraped_meetings = extract_meetings(content)

    payload = load_json(OUT, {"last_updated": None, "source": URL, "meetings": []})

    if isinstance(payload, list):
        existing_meetings = payload
        payload = {"last_updated": None, "source": URL, "meetings": payload}
    else:
        existing_meetings = payload.get("meetings", [])

    final_meetings = merge_meetings(scraped_meetings, existing_meetings)

    # Attach YouTube recordings (fills empty video_url only).
    attach_videos(final_meetings, fetch_youtube_videos())

    # Mirror township PDFs (minutes/agendas in-repo, packages to Releases)
    # before saving, so archived_* paths land in canonical data.
    for m in final_meetings:
        mirror_record(m, "meetings")

    payload["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    payload["source"] = URL
    payload["meetings"] = final_meetings

    save_json(OUT, payload)

    print(f"Saved {len(final_meetings)} meetings")


if __name__ == "__main__":
    main()
