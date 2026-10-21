#!/usr/bin/env python3

import json
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from archive import mirror_record
import youtube


URL = "https://nipissingtownship.com/council/council-meeting-dates-agendas-minutes/"
# The pre-redesign address still served the same page in Oct 2026; kept as a
# fallback in case the new one moves again.
FALLBACK_URLS = ["https://nipissingtownship.com/council-meeting-dates-agendas-minutes/"]
# Minutes for 2023–2025 (agendas/packages/videos for those years were deleted
# from the site; we hold archived copies). Rows hold two meetings each; special
# meetings are labelled "September 2, 2025 – Special Meeting".
PREVIOUS_MINUTES_URL = "https://nipissingtownship.com/council/previous-council-minutes/"
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
    if href.startswith("http://"):
        href = "https://" + href[7:]        # the site links both; same file
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


# Labels that mark a navigation/index link rather than a real supplementary
# document. These leak into extra_docs when the listing page is parsed and one
# meeting record accidentally absorbs links belonging to many other meetings.
GENERIC_EXTRA_LABELS = {"agenda", "minutes", "agenda package", "package"}


def sanitize_extra_docs(meetings: List[Dict[str, Any]]) -> None:
    """Strip index-scrape noise from every meeting's extra_docs, in place.

    An entry is dropped when it:
      - duplicates ANY meeting's primary agenda/minutes/package URL,
      - uses a generic nav label (Agenda / Minutes / Agenda Package), or
      - is a mailto/non-http link.
    Genuine supplementary docs (budgets, financial statements, correspondence)
    are kept, and no meeting records are removed. Idempotent.
    """
    primary: set = set()
    for m in meetings:
        for f in ("agenda_url", "minutes_url", "package_url"):
            u = (m.get(f) or "").strip()
            if u:
                primary.add(u)

    for m in meetings:
        cleaned = []
        for d in m.get("extra_docs", []) or []:
            url = (d.get("url") or "").strip()
            label = (d.get("label") or "").strip()
            if not url or "mailto:" in url or not url.lower().startswith("http"):
                continue
            if url in primary:
                continue
            if label.lower() in GENERIC_EXTRA_LABELS:
                continue
            cleaned.append({"label": label or "Document", "url": url})
        m["extra_docs"] = cleaned


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

    # Text seen since the last meeting started, within the current line.
    # "Special Meeting*" sits in its own <strong> just BEFORE the date, so the
    # date's own text never contained the word "special".
    pending: List[str] = []
    # True from a date until the meeting's first link or the end of the line:
    # a "– Special Meeting" suffix in that window belongs to this meeting.
    just_started = [False]

    def start_meeting(month: str, day: str, year: str, context_text: str = "") -> None:
        nonlocal current
        flush()

        dt = datetime.strptime(
            f"{month.capitalize()} {int(day)}, {year}",
            "%B %d, %Y"
        )

        meeting_type, cancelled = flags_from_context(context_text)
        if meeting_type == "Regular" and "special" in " ".join(pending).lower():
            meeting_type = "Special"
        pending.clear()
        just_started[0] = True

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
            if (current and just_started[0] and not DATE_RE.search(text)
                    and "cancel" in text.lower()):
                current["cancelled"] = True      # "Meeting Cancelled" in its own cell
            if (current and just_started[0] and not DATE_RE.search(text)
                    and "special" in text.lower()):
                current["meeting_type"] = "Special"
            last_end = 0
            found = False
            for dm in DATE_RE.finditer(text):
                pending.append(text[last_end:dm.start()])
                start_meeting(dm.group(1), dm.group(2), dm.group(3), text)
                last_end = dm.end()
                found = True
            if not found and not (current and just_started[0]):
                # Text inside a meeting's own window (after its date, before
                # its first link) was consumed above; anything else may be a
                # prefix like "Special Meeting*" for the NEXT date.
                pending.append(text)

        elif isinstance(node, Tag):
            if node.name in ("br", "li", "tr", "p"):
                pending.clear()          # new line: earlier text can't apply
                just_started[0] = False
            if node.name == "br":
                return
            if node.name == "a":
                just_started[0] = False
                pending.clear()          # a link ends the previous meeting's text
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
            pending.clear()
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


def _basename(url: Optional[str]) -> str:
    return (url or "").rstrip("/").split("/")[-1].lower()


def _named_special(m: Dict[str, Any]) -> bool:
    return any("special" in _basename(m.get(f)) for f in ("minutes_url", "agenda_url"))


def _fill(target: Dict[str, Any], src: Dict[str, Any], skip=("meeting_type", "cancelled")) -> None:
    for f, v in src.items():
        if f in skip:
            continue
        if f == "extra_docs":
            target["extra_docs"] = unique_extra_docs((target.get("extra_docs") or []) + (v or []))
        elif not target.get(f) and v:
            target[f] = v


def relabel_specials(meetings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Rule 1 — document names decide the type. A "Regular" record whose minutes
    or agenda filename says "special" is a special meeting. If a Special record
    already exists that day with no minutes or the same minutes, the two are
    one meeting and are merged. Also collapses same-day Regular+Special pairs
    that point at identical minutes (phantom duplicates, e.g. 2024-01-02).
    """
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for m in meetings:
        by_day.setdefault(m.get("date"), []).append(m)

    out: List[Dict[str, Any]] = []
    for day, ms in by_day.items():
        regs = [m for m in ms if m.get("meeting_type", "Regular") == "Regular"]
        specs = [m for m in ms if m.get("meeting_type") == "Special"]
        others = [m for m in ms if m not in regs and m not in specs]
        if len(regs) == 1 and len(specs) <= 1:
            r = regs[0]
            sp = specs[0] if specs else None
            same_minutes = sp and r.get("minutes_url") and r.get("minutes_url") == sp.get("minutes_url")
            if _named_special(r):
                if sp is None:
                    r["meeting_type"] = "Special"
                    print(f"  {day}: relabelled Regular -> Special (document names)")
                    regs, specs = [], [r]
                elif not sp.get("minutes_url") or same_minutes:
                    _fill(sp, r)
                    print(f"  {day}: merged mislabelled Regular into Special")
                    regs = []
            elif same_minutes:
                # Identical docs, nothing says special: the Special is the phantom.
                _fill(r, sp)
                print(f"  {day}: dropped phantom Special (same minutes as Regular)")
                specs = []
        out.extend(regs + specs + others)
    return out


# Fields that belong to the regular evening meeting when a closed special
# session was held earlier the same day.
REGULAR_SIDE_FIELDS = ("package_url", "package_archived", "video_url")


def split_mixed_specials(meetings: List[Dict[str, Any]],
                         page_keys: set) -> List[Dict[str, Any]]:
    """
    Rule 2 — when a page lists BOTH a Regular and a Special meeting on a day
    but we only hold the Special record, documents that belong to the regular
    meeting (package, video, an agenda not named "special") move to a new
    Regular record. Its minutes come from the previous-minutes page.
    """
    have = {(m.get("date"), m.get("meeting_type", "Regular")) for m in meetings}
    for m in list(meetings):
        d = m.get("date")
        if m.get("meeting_type") != "Special":
            continue
        if (d, "Regular") not in page_keys or (d, "Regular") in have:
            continue
        reg = {
            "date": d,
            "display_date": m.get("display_date"),
            "year": m.get("year"),
            "meeting_type": "Regular",
            "title": "",
            "agenda_url": None, "minutes_url": None, "package_url": None,
            "extra_docs": [], "video_url": None, "summary": None, "cancelled": False,
        }
        for f in REGULAR_SIDE_FIELDS:
            if m.get(f):
                reg[f] = m.pop(f)
        if m.get("agenda_url") and "special" not in _basename(m.get("agenda_url")):
            reg["agenda_url"] = m.pop("agenda_url")
            if m.get("agenda_archived"):
                reg["agenda_archived"] = m.pop("agenda_archived")
        m.setdefault("video_url", None)
        m.setdefault("package_url", None)
        meetings.append(reg)
        have.add((d, "Regular"))
        print(f"  {d}: split regular-meeting documents off the Special record")
    return meetings


def fill_from_previous_minutes(meetings: List[Dict[str, Any]],
                               previous: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rule 3 — the previous-minutes page only fills gaps: minutes where a
    record has none, and meetings we don't hold at all. Never overwrites."""
    idx = {meeting_key(m): m for m in meetings}
    added = filled = 0
    for p in previous:
        k = meeting_key(p)
        if k in idx:
            if not idx[k].get("minutes_url") and p.get("minutes_url"):
                idx[k]["minutes_url"] = p["minutes_url"]
                filled += 1
        else:
            meetings.append(p)
            idx[k] = p
            added += 1
            print(f"  previous-minutes page: added {k[0]} {k[1]}")
    print(f"  previous-minutes page: {added} meeting(s) added, {filled} minutes link(s) filled")
    return meetings


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

    # Older runs missed the "Special Meeting*" label and stored special
    # meetings as Regular (e.g. 2026-06-30, 2026-08-20). When the page now
    # lists a date ONLY as Special, fold the stale Regular record into it
    # (keeping its summary, video, archived copies) instead of carrying a
    # duplicate. Dates with a genuine Regular + Special pair are untouched.
    scraped_keys = {meeting_key(m) for m in scraped}
    for (d, t) in list(scraped_keys):
        if t != "Special" or (d, "Regular") in scraped_keys:
            continue
        stale = existing_map.pop((d, "Regular"), None)
        if not stale:
            continue
        target = final_map[(d, "Special")]
        for f, v in stale.items():
            if f in ("meeting_type", "cancelled"):
                continue
            if f == "extra_docs":
                target["extra_docs"] = unique_extra_docs((target.get("extra_docs") or []) + (v or []))
            elif not target.get(f) and v:
                target[f] = v
        print(f"  {d}: folded stale Regular record into Special")

    for k, old in existing_map.items():
        if k not in final_map:
            final_map[k] = old

    final = list(final_map.values())
    final.sort(key=lambda x: x["date"], reverse=True)
    return final


def main() -> None:
    print("Fetching meetings...")

    res = None
    for url in [URL] + FALLBACK_URLS:
        try:
            res = requests.get(url, timeout=30)
            res.raise_for_status()
            break
        except requests.RequestException as e:
            print(f"  WARN: {url} failed ({e})")
            res = None
    if res is None:
        raise RuntimeError("Council meetings page unreachable at all known URLs")

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

    existing_meetings = relabel_specials(existing_meetings)
    final_meetings = merge_meetings(scraped_meetings, existing_meetings)

    previous_meetings: List[Dict[str, Any]] = []
    try:
        prev = requests.get(PREVIOUS_MINUTES_URL, timeout=30)
        prev.raise_for_status()
        psoup = BeautifulSoup(prev.text, "html.parser")
        pcontent = (psoup.find("div", class_=re.compile(r"entry-content|post-content"))
                    or psoup.find("article") or psoup.find("main") or psoup.body)
        if pcontent is not None:
            previous_meetings = extract_meetings(pcontent)
        print(f"  previous-minutes page: {len(previous_meetings)} meeting(s) listed")
    except requests.RequestException as e:
        print(f"  WARN: previous-minutes page unavailable ({e})")

    page_keys = {meeting_key(m) for m in scraped_meetings + previous_meetings}
    final_meetings = split_mixed_specials(final_meetings, page_keys)
    final_meetings = fill_from_previous_minutes(final_meetings, previous_meetings)
    final_meetings = relabel_specials(final_meetings)
    final_meetings.sort(key=lambda x: (x["date"], x.get("meeting_type") == "Special"), reverse=True)

    # Strip index-scrape noise from extra_docs — cleans junk carried forward in
    # canonical and prevents the merge from re-bloating it on future runs.
    sanitize_extra_docs(final_meetings)

    # Attach YouTube recordings from the shared cache (fills empty video_url
    # only; never overwrites). Council videos are always title-labelled.
    youtube.attach_labelled(final_meetings, youtube.refresh(), "council")

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