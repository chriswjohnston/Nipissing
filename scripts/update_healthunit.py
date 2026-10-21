#!/usr/bin/env python3
"""
update_healthunit.py

North Bay Parry Sound District Health Unit — Board of Health and its
committees.

Source (one page, years in collapsible sections):
https://www.myhealthunit.ca/about-us/boardofhealth/meeting-schedules-agendas-minutes-reports-and-resolutions/

Captured: agendas and minutes for the Board of Health and every committee,
Medical Officer of Health reports (attached to the Board of Health meeting
they were presented at), the resolutions table, and the published schedule.

The page says earlier years are "available upon request" — i.e. once a year
rolls off the page it is gone — so everything is mirrored and carried
forward. Nipissing has no seat of its own: it is represented by the member
appointed for the municipalities it is grouped with.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

import doc_sources as ds
from joint_boards import Store, add_doc, slug

URL = ("https://www.myhealthunit.ca/about-us/boardofhealth/"
       "meeting-schedules-agendas-minutes-reports-and-resolutions/")
GROUP = "healthunit"
DESCRIPTION = ("North Bay Parry Sound District Health Unit. Nipissing is represented "
               "through the member appointed for its group of municipalities.")

# Known bodies (filename prefix -> body). Anything else gets its own entry.
BODIES = {
    "boh": {"id": "healthunit", "name": "Health Unit: Board of Health", "short": "Health Unit"},
    "fp": {"id": "healthunit-finance", "name": "Health Unit: Finance and Property Committee",
           "short": "Health Unit F&P"},
    "ppler": {"id": "healthunit-personnel",
              "name": "Health Unit: Personnel Policy, Employee/Labour Relations Committee",
              "short": "Health Unit Personnel"},
}
NAME_TO_PREFIX = [
    ("finance", "fp"),
    ("personnel", "ppler"),
    ("labour", "ppler"),
    ("board of health", "boh"),
]

ISO_IN_NAME = re.compile(r"(?<!\d)(20\d\d)-(\d{2})-(\d{2})(?!\d)")
SECTION_WORDS = [
    ("schedule", "schedule"),
    ("agenda", "agenda"),
    ("minute", "minutes"),
    ("medical officer", "moh"),
    ("moh", "moh"),
    ("resolution", "resolutions"),
]


def body_for(text: str, filename: str, store: Store) -> Dict[str, Any]:
    prefix = (filename.split("-")[0] or "").lower()
    if prefix in BODIES:
        key = prefix
    else:
        t = (text or "").lower()
        key = next((p for w, p in NAME_TO_PREFIX if w in t), None)
    if not key and "committee" not in (text or "").lower():
        key = "boh"          # schedule lines and links that name no committee
    if key:
        b = dict(BODIES[key])
    else:
        name = re.sub(r"^.*?\d{4}\s*[-–—:]\s*", "", text or "").strip() or "Committee"
        b = {"id": f"healthunit-{slug(name)}", "name": f"Health Unit: {name}",
             "short": f"Health Unit {name.split()[0]}"}
    b.update(group=GROUP, url=URL, description=DESCRIPTION)
    return store.body(b)


def date_of(text: str, href: str) -> Optional[date]:
    m = ISO_IN_NAME.search(ds.filename_of(href))
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return ds.find_date(text)


def kind_from(section: Optional[str], text: str, href: str) -> str:
    n = f"{text} {ds.filename_of(href)}".lower()
    if "minute" in n:
        return "minutes"
    if "moh" in n or "medical officer" in n:
        return "moh"
    if "agenda" in n:
        return "agenda"
    return section or "other"


def heading_text(el: Tag) -> Optional[str]:
    if el.name in ("h1", "h2", "h3", "h4", "h5", "h6", "summary", "button", "legend"):
        t = el.get_text(" ", strip=True)
        return t if 0 < len(t) < 120 else None
    return None


def parse(html: str, store: Store) -> None:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    section: Optional[str] = None
    counts = {"agenda": 0, "minutes": 0, "moh": 0, "resolutions": 0, "schedule": 0}
    boh = body_for("Board of Health", "boh", store)
    resolutions: List[Dict[str, Any]] = []

    for el in main.find_all(True):
        h = heading_text(el)
        if h:
            low = h.lower()
            hit = next((sec for w, sec in SECTION_WORDS if w in low), None)
            if hit:
                section = hit
            continue

        # Resolutions table rows: date | number | title | topic (link somewhere)
        if el.name == "tr" and section == "resolutions":
            cells = [c.get_text(" ", strip=True) for c in el.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            dt = ds.find_date(cells[0]) or ds.find_date(" ".join(cells))
            if not dt:
                continue
            a = el.find("a", href=True)
            url = urljoin(URL, a["href"]) if a else None
            res = {"date": dt.isoformat(), "number": cells[1] if len(cells) > 1 else "",
                   "title": cells[2] if len(cells) > 2 else "",
                   "topic": cells[3] if len(cells) > 3 else "", "url": url}
            resolutions.append(res)
            if url:
                m = store.meeting(boh, dt)
                add_doc(m, "extra", url, f"Resolution {res['number']}: {res['title']}".strip(": "))
            counts["resolutions"] += 1
            continue

        if el.name == "a" and el.get("href"):
            href = urljoin(URL, el["href"])
            text = el.get_text(" ", strip=True)
            if not re.search(r"\.pdf($|\?)", href, re.I) or section == "resolutions":
                continue
            dt = date_of(text, href)
            if not dt:
                if section in ("schedule", None):
                    boh.setdefault("page_docs", []).append({"label": text or ds.filename_of(href),
                                                           "url": href})
                continue
            kind = kind_from(section, text, href)
            if kind == "moh":
                m = store.meeting(boh, dt)
                add_doc(m, "extra", href, f"Medical Officer of Health report: {text}")
            else:
                body = body_for(text, ds.filename_of(href), store)
                m = store.meeting(body, dt)
                add_doc(m, kind if kind in ("agenda", "minutes") else "extra", href,
                        text or ds.filename_of(href))
            counts[kind if kind in counts else "agenda"] += 1
            continue

        # Published schedule: short dated lines naming a body
        if section == "schedule" and el.name in ("li", "p", "td"):
            text = el.get_text(" ", strip=True)
            if 0 < len(text) < 160 and not el.find("a"):
                dt = ds.find_date(text)
                if dt:
                    m = store.meeting(body_for(text, "", store), dt)
                    if "cancel" in text.lower():
                        m["cancelled"] = True
                    counts["schedule"] += 1

    if resolutions:
        boh["resolutions"] = resolutions
    print("  found: " + ", ".join(f"{k} {v}" for k, v in counts.items()))


def main() -> None:
    print("=" * 60)
    print("Nipissing update_healthunit.py")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    store = Store(GROUP)
    # First run (2026-10-21) created one-meeting bodies named after dates from
    # schedule lines ("healthunit-april-22-2026"). Remove them; the schedule
    # now files those dates under the Board of Health.
    bogus = re.compile(r"^healthunit-[a-z]+-\d{1,2}-\d{4}$")
    before = len(store.payload["boards"])
    store.payload["boards"] = [b for b in store.payload["boards"]
                               if not bogus.match(b.get("id", ""))]
    if before != len(store.payload["boards"]):
        print(f"  removed {before - len(store.payload['boards'])} date-named body entries")
    try:
        r = requests.get(URL, headers=ds.HEADERS, timeout=60)
        r.raise_for_status()
        parse(r.text, store)
    except Exception as e:
        print(f"  ✗ Health Unit page failed ({e}) — keeping stored data")
        return
    store.save("boards/healthunit")


if __name__ == "__main__":
    main()
