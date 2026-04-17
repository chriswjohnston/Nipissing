#!/usr/bin/env python3

import json
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, NavigableString, Tag


URL = "https://nipissingtownship.com/council-meeting-dates-agendas-minutes/"
OUT = Path("data/canonical/meetings.json")

DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)


# ----------------------------
# Helpers
# ----------------------------

def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def normalize_url(href):
    if href.startswith("http"):
        return href
    return "https://nipissingtownship.com" + href


def clean_label(text):
    return re.sub(r"\s+", " ", text or "").strip(" ()")


def classify_link(label, url):
    text = f"{label} {url}".lower()
    if "agenda package" in text:
        return "package"
    if "minutes" in text:
        return "minutes"
    if "agenda" in text:
        return "agenda"
    return "extra"


def format_display_date(dt):
    return dt.strftime("%b %-d, %Y") if hasattr(dt, "strftime") else dt


def meeting_key(m):
    return (m["date"], m.get("meeting_type", "Regular"))


def unique_extra_docs(docs):
    seen = set()
    out = []
    for d in docs:
        key = (d["label"], d["url"])
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def flags_from_context(text):
    t = text.lower()
    return (
        "Special" if "special" in t else "Regular",
        "cancel" in t
    )


# ----------------------------
# Core scraper
# ----------------------------

def extract_meetings(content: Tag) -> List[Dict[str, Any]]:
    meetings_raw = []
    current = None

    def flush():
        nonlocal current
        if current:
            meetings_raw.append(dict(current))
        current = None

    def start_meeting(month, day, year):
        nonlocal current
        flush()

        dt = datetime.strptime(
            f"{month.capitalize()} {int(day)}, {year}",
            "%B %d, %Y"
        )

        meeting_type, cancelled = flags_from_context(month)

        current = {
            "date": dt.strftime("%Y-%m-%d"),
            "display_date": dt.strftime("%b %-d, %Y"),
            "year": dt.year,
            "meeting_type": meeting_type,
            "agenda_url": None,
            "minutes_url": None,
            "package_url": None,
            "extra_docs_raw": [],
            "cancelled": cancelled,
        }

    def walk(node):
        nonlocal current

        if isinstance(node, NavigableString):
            text = str(node)
            last = 0

            for dm in DATE_RE.finditer(text):
                start_meeting(dm.group(1), dm.group(2), dm.group(3))
                last = dm.end()

        elif isinstance(node, Tag):

            if node.name == "a":
                if current:
                    href = node.get("href")
                    label = clean_label(node.get_text())

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
        if DATE_RE.search(el.get_text(" ", strip=True)) or el.find("a"):
            walk(el)

    flush()

    # merge duplicates
    merged = {}

    for m in meetings_raw:
        k = meeting_key(m)
        if k not in merged:
            merged[k] = m
            continue

        old = merged[k]

        for field in ("agenda_url", "minutes_url", "package_url"):
            if not old.get(field) and m.get(field):
                old[field] = m[field]

        old["extra_docs_raw"] += m.get("extra_docs_raw", [])

    out = []

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


# ----------------------------
# Main
# ----------------------------

def main():
    print("Fetching meetings...")

    res = requests.get(URL)
    soup = BeautifulSoup(res.text, "html.parser")

    content = soup.find("main") or soup

    meetings = extract_meetings(content)

    existing = load_json(OUT, [])

    existing_map = {meeting_key(m): m for m in existing}

    final = []

    for m in meetings:
        key = meeting_key(m)
        if key in existing_map:
            old = existing_map[key]

            m["summary"] = old.get("summary")
            m["video_url"] = old.get("video_url")

        final.append(m)

    save_json(OUT, final)

    print(f"Saved {len(final)} meetings")


if __name__ == "__main__":
    main()
