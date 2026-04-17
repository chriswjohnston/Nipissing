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

    payload["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    payload["source"] = URL
    payload["meetings"] = final_meetings

    save_json(OUT, payload)

    print(f"Saved {len(final_meetings)} meetings")


if __name__ == "__main__":
    main()
