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

# Files whose name carries the wrong date (checked against the page: the
# "September 23 - Board of Health" entry links boh-minutes-2026-08-23.pdf).
DATE_FIXES = {"boh-minutes-2026-08-23.pdf": date(2026, 9, 23)}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
MONTH_YEAR = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(20\d\d)\b", re.I)
RES_NO = re.compile(r"^(20\d\d)/(\d{1,2})/\d+")

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
    fixed = DATE_FIXES.get(ds.filename_of(href).lower())
    if fixed:
        return fixed
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
            # "Resolution Date" is a month ("April 2026"); the number is
            # year/month/sequence. Never read a date out of the title ("Year-Ended
            # December 31, 2025" is not when it passed).
            ym = None
            mm = MONTH_YEAR.search(cells[0])
            if mm:
                ym = (int(mm.group(2)), MONTHS[mm.group(1).lower()])
            else:
                rn = RES_NO.match(cells[1] if len(cells) > 1 else "")
                if rn:
                    ym = (int(rn.group(1)), int(rn.group(2)))
            if not ym:
                continue
            a = el.find("a", href=True)
            url = urljoin(URL, a["href"]) if a else None
            res = {"month": f"{ym[0]}-{ym[1]:02d}", "number": cells[1] if len(cells) > 1 else "",
                   "title": cells[2] if len(cells) > 2 else "",
                   "topic": cells[3] if len(cells) > 3 else "", "url": url}
            resolutions.append(res)
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
        attach_resolutions(store, boh, resolutions)
    print("  found: " + ", ".join(f"{k} {v}" for k, v in counts.items()))


def attach_resolutions(store: Store, boh: Dict[str, Any], resolutions: List[Dict[str, Any]]) -> None:
    """A linked resolution goes on the Board of Health meeting of its month
    (scraped or stored); with no meeting that month it stays in the list only."""
    stored = next((b for b in store.payload["boards"] if b.get("id") == boh["id"]), {})
    dates = {k.split("|")[0] for k in store.scraped.get(boh["id"], {})}
    dates |= {m["date"] for m in stored.get("meetings", []) if not _resolution_only(m)}
    for res in resolutions:
        if not res.get("url"):
            continue
        same = sorted(d for d in dates if d.startswith(res["month"]))
        if not same:
            continue
        m = store.meeting(boh, date.fromisoformat(same[-1]))
        add_doc(m, "extra", res["url"], f"Resolution {res['number']}: {res['title']}".strip(": "))


def _resolution_only(m: Dict[str, Any]) -> bool:
    return (not any(m.get(k) for k in ("agenda_url", "minutes_url", "package_url", "video_url"))
            and bool(m.get("extra_docs"))
            and all((d.get("label") or "").startswith("Resolution ") for d in m["extra_docs"]))


def drop_misdated(store: Store) -> None:
    """Remove stored meetings that only existed because of a wrong date: a
    resolution dated from its title, or a file listed in DATE_FIXES."""
    fixed = set(DATE_FIXES)
    for b in store.payload["boards"]:
        if b.get("group") != GROUP:
            continue
        # resolutions used to carry a (wrong) day; the list is rebuilt by month
        if b.get("resolutions"):
            b["resolutions"] = [r for r in b["resolutions"] if "month" in r]
        keep = []
        for m in b.get("meetings", []):
            docs = [m.get(k) for k in ("agenda_url", "minutes_url", "package_url")]
            docs += [d.get("url") for d in m.get("extra_docs") or []]
            docs = [ds.filename_of(u).lower() for u in docs if u]
            if _resolution_only(m) or (docs and all(d in fixed for d in docs)
                                       and not m.get("video_url")):
                print(f"  removed misdated record {b['id']} {m['date']}")
                continue
            keep.append(m)
        b["meetings"] = keep


def main() -> None:
    print("=" * 60)
    print("Nipissing update_healthunit.py")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    store = Store(GROUP)
    drop_misdated(store)
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
