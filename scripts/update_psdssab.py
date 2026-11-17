#!/usr/bin/env python3
"""
update_psdssab.py

District of Parry Sound Social Services Administration Board (PSDSSAB) —
board meetings. Nipissing Township is in the Parry Sound District, so this
is the social services board that serves it.

Source (one page, one collapsible table per year, newest year first):
https://www.psdssab.org/board-and-administration/minutes-agendas/

Each table's first row is a header: Meeting Date | Agenda | Board Package |
Approved Minutes | CAO Report. Columns are found by those header names, so a
reordered or extra column does not break the parse. Special meetings carry
"(Special)" in the date cell; skipped months say "No Meeting" in every cell.

Records are carry-forward (joint_boards.Store): a meeting or document that
disappears from the page is never removed here. Files live on the
cdn.govstack.com CDN and are mirrored by archive.py like the Health Unit's.
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

import doc_sources as ds
from joint_boards import Store, add_doc

URL = "https://www.psdssab.org/board-and-administration/minutes-agendas/"
GROUP = "psdssab"
BODY = {
    "id": "psdssab",
    "name": "PSDSSAB Board",
    "short": "PSDSSAB",
    "group": GROUP,
    "url": URL,
    "description": ("District of Parry Sound Social Services Administration Board. "
                    "Nipissing Township is in the Parry Sound District and is served "
                    "by this board."),
}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
# The page has a typo ("October 12. 2023"), so accept . or , before the year.
DATE_RE = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})\s*[.,]?\s*(20\d\d)\b", re.I)
# Fewer meetings than this means the page changed shape: keep stored data.
MIN_EXPECTED = 20


def parse_date(text: str) -> Optional[date]:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
    except ValueError:
        return None


def column_kinds(header_cells: List[Tag]) -> Dict[int, str]:
    kinds: Dict[int, str] = {}
    for i, c in enumerate(header_cells):
        t = c.get_text(" ", strip=True).lower()
        if "agenda" in t:
            kinds[i] = "agenda"
        elif "package" in t:
            kinds[i] = "package"
        elif "minute" in t:
            kinds[i] = "minutes"
        elif "cao" in t:
            kinds[i] = "cao"
    return kinds


def parse(html: str, store: Store) -> int:
    soup = BeautifulSoup(html, "html.parser")
    body = store.body(dict(BODY))
    meetings = 0
    skipped: List[Dict[str, Any]] = []     # "No Meeting" rows that still carry a CAO report

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        head = rows[0].find_all(["th", "td"])
        if not head or "meeting date" not in head[0].get_text(" ", strip=True).lower():
            continue                        # the site-search widget has tables too
        kinds = column_kinds(head)
        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            label = cells[0].get_text(" ", strip=True)
            dt = parse_date(label)
            if not dt:
                continue
            row_text = " ".join(c.get_text(" ", strip=True).lower() for c in cells[1:])
            links = {k: [] for k in ("agenda", "package", "minutes", "cao")}
            for i, kind in kinds.items():
                if i < len(cells):
                    for a in cells[i].find_all("a", href=True):
                        links[kind].append((urljoin(URL, a["href"]),
                                            a.get_text(" ", strip=True)))
            if "no meeting" in row_text and not any(links[k] for k in ("agenda", "package", "minutes")):
                if links["cao"]:
                    skipped.append({"date": dt, "cao": links["cao"]})
                continue
            special = "special" in label.lower()
            m = store.meeting(body, dt, "Special" if special else "Board")
            m["title"] = ("PSDSSAB Special Board Meeting" if special else "PSDSSAB Board Meeting")
            for kind in ("agenda", "package", "minutes"):
                for href, text in links[kind]:
                    add_doc(m, kind, href, text or kind.title())
            for href, text in links["cao"]:
                add_doc(m, "extra", href, text or "CAO Report")
            meetings += 1

    # A CAO report published for a month with no meeting (July/August 2024)
    # is attached to the next meeting that did happen.
    dated = sorted(
        (date.fromisoformat(k.split("|")[0]), k) for k in store.scraped.get(body["id"], {}))
    for s in skipped:
        nxt = next((k for d, k in dated if d > s["date"]), None)
        if nxt:
            for href, text in s["cao"]:
                add_doc(store.scraped[body["id"]][nxt], "extra", href, text or "CAO Report")
    return meetings


def main() -> None:
    print("=" * 60)
    print("Nipissing update_psdssab.py")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    store = Store(GROUP)
    try:
        r = requests.get(URL, headers=ds.HEADERS, timeout=60)
        r.raise_for_status()
        found = parse(r.text, store)
    except Exception as e:
        print(f"  ✗ PSDSSAB page failed ({e}) — keeping stored data")
        return
    print(f"  found {found} meeting(s) on the page")
    if found < MIN_EXPECTED:
        print(f"  ✗ expected at least {MIN_EXPECTED}; page layout may have changed — "
              "keeping stored data")
        return
    store.save("boards/psdssab")


if __name__ == "__main__":
    main()
