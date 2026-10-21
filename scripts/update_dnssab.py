#!/usr/bin/env python3
"""
update_dnssab.py

District of Nipissing Social Services Administration Board (DNSSAB) — board
and committee meetings, from its eScribe portal:
https://pub-dnssab.escribemeetings.com/

eScribe pages render with JavaScript, but the calendar is fed by a JSON
endpoint (MeetingsCalendarView.aspx/GetCalendarMeetings) that the same
portal software exposes for every municipality using it. This asks it for
each year from 2022 on and records every meeting with all of its published
documents (agenda, agenda package, minutes, and anything else attached).

If the endpoint ever changes shape, the run logs the raw response keys and
keeps all stored data. Nipissing has no seat of its own on DNSSAB: it is
represented through the members appointed for its group of municipalities.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

import archive
import doc_sources as ds
from joint_boards import Store, add_doc, slug

BASE = "https://pub-dnssab.escribemeetings.com/"
CALENDAR_API = urljoin(BASE, "MeetingsCalendarView.aspx/GetCalendarMeetings")
GROUP = "dnssab"
FIRST_YEAR = 2022
# Bump to rebuild the DNSSAB group from eScribe (safe: eScribe keeps the full
# history). 2 = NDHC Board split out; the first runs merged NDHC and DNSSAB
# meetings held on the same day.
SCHEMA = 2
DESCRIPTION = ("District of Nipissing Social Services Administration Board: social "
               "housing, Ontario Works, child care and paramedic services. Nipissing is "
               "represented through the members appointed for its group of municipalities.")




def g(d: Dict[str, Any], *names: str) -> Any:
    """Case-insensitive field lookup (eScribe versions differ in casing)."""
    low = {k.lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in low and low[n.lower()] not in (None, ""):
            return low[n.lower()]
    return None


def parse_dt(value: Any) -> Optional[date]:
    if not value:
        return None
    s = str(value)
    m = re.search(r"/Date\((-?\d+)", s)           # ASP.NET "/Date(1700000000000)/"
    if m:
        return datetime.utcfromtimestamp(int(m.group(1)) / 1000).date()
    for rx, order in ((r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", "ymd"),
                      (r"(\d{1,2})/(\d{1,2})/(\d{4})", "mdy")):
        m = re.search(rx, s)
        if m:
            a, b, c = (int(x) for x in m.groups())
            try:
                return date(a, b, c) if order == "ymd" else date(c, a, b)
            except ValueError:
                pass
    return ds.find_date(s)


def body_for(name: str, store: Store) -> Dict[str, Any]:
    clean = re.sub(r"\b(DNSSAB|District of Nipissing Social Services Administration Board)\b",
                   "", name or "", flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip(" -–:")
    low = clean.lower()
    if "ndhc" in low or "housing corporation" in low:
        b = {"id": "dnssab-ndhc", "short": "NDHC",
             "name": "NDHC Board (Nipissing District Housing Corporation)"}
    elif "committee" in low:
        b = {"id": f"dnssab-{slug(re.sub(r'(?i)special|meeting', '', clean))}",
             "name": f"DNSSAB: {clean}", "short": f"DNSSAB {clean.split()[0]}"}
    else:
        b = {"id": "dnssab", "name": "DNSSAB Board", "short": "DNSSAB"}
    b.update(group=GROUP, url=BASE, description=DESCRIPTION, schema=SCHEMA)
    return store.body(b)


def kind_for(doc: Dict[str, Any]) -> str:
    t = f"{g(doc, 'Type') or ''} {g(doc, 'Title', 'Name') or ''}".lower()
    if "minute" in t:
        return "minutes"
    if "package" in t or "packet" in t:
        return "package"
    if "agenda" in t:
        return "agenda"
    return "extra"


def fetch_year(session: requests.Session, year: int) -> Optional[List[Dict[str, Any]]]:
    body = {"calendarStartDate": f"{year}-01-01", "calendarEndDate": f"{year}-12-31"}
    r = session.post(CALENDAR_API, json=body, timeout=60,
                     headers={"Content-Type": "application/json; charset=utf-8",
                              "X-Requested-With": "XMLHttpRequest",
                              "Referer": f"{BASE}?Year={year}"})
    r.raise_for_status()
    data = r.json()
    items = data.get("d", data) if isinstance(data, dict) else data
    if isinstance(items, str):          # some versions double-encode
        import json
        items = json.loads(items)
    if not isinstance(items, list):
        print(f"  ! unexpected response for {year}: keys {list(data)[:10]}")
        return None
    return items


STATS = {"ok": 0, "refused": 0, "other": 0}
_warmed: set = set()


def _meeting_page(m: Dict[str, Any]) -> Optional[str]:
    mid = re.search(r"[?&]Id=([0-9a-fA-F-]{36})", m.get("source_url") or "")
    return f"{BASE}Meeting.aspx?Id={mid.group(1)}&Agenda=Agenda&lang=English" if mid else None


def fetch_escribe(session: requests.Session, url: str, page: Optional[str]) -> Optional[bytes]:
    """eScribe refuses FileStream.ashx requests that don't come from a portal
    page (403 seen 2026-10-21). Open the meeting page first, then ask for the
    file with that page as Referer and browser-like headers."""
    if page and page not in _warmed:
        try:
            session.get(page, timeout=60)
        except Exception:
            pass
        _warmed.add(page)
    try:
        r = session.get(url, timeout=120, headers={
            **ds.HEADERS, "Referer": page or BASE,
            "Accept": "application/pdf,application/octet-stream,*/*;q=0.8"})
    except Exception as e:
        STATS["other"] += 1
        print(f"  archive: {url} failed: {e}")
        return None
    if r.content[:5].startswith(b"%PDF"):
        return r.content
    if r.status_code == 403:
        STATS["refused"] += 1
        if STATS["refused"] <= 3:     # enough to diagnose without flooding the log
            print(f"  archive: 403 for {url} (page {page}); "
                  f"server={r.headers.get('Server')} cf-ray={r.headers.get('CF-RAY')} "
                  f"body={r.content[:120]!r}")
    else:
        STATS["other"] += 1
        print(f"  archive: HTTP {r.status_code} {r.headers.get('Content-Type')} for {url}")
    return None


def _store(url: str, data: bytes, category: str, year: int) -> Optional[str]:
    if len(data) > archive.EXTERNAL_INREPO_MAX_BYTES:
        return archive.store_bytes_to_release(url, data)
    return archive.store_bytes_in_repo(url, category, year, data)


def mirror_escribe(session: requests.Session, m: Dict[str, Any], category: str) -> None:
    year = int(m.get("year") or 0)
    if year and year < archive.ARCHIVE_MIN_YEAR:
        return
    page = _meeting_page(m)
    for kind in ("agenda", "package", "minutes"):
        url, dst = m.get(f"{kind}_url"), f"{kind}_archived"
        if url and not m.get(dst) and archive.is_external_doc(url):
            data = fetch_escribe(session, url, page)
            if data:
                got = _store(url, data, category, year)
                if got:
                    m[dst] = got
                    STATS["ok"] += 1
    for d in m.get("extra_docs") or []:
        url = d.get("url")
        if url and not d.get("archived") and archive.is_external_doc(url):
            data = fetch_escribe(session, url, page)
            if data:
                got = _store(url, data, category, year)
                if got:
                    d["archived"] = got
                    STATS["ok"] += 1


def main() -> None:
    print("=" * 60)
    print("Nipissing update_dnssab.py")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    store = Store(GROUP)
    old = [b for b in store.payload["boards"] if b.get("group") == GROUP]
    if old and any(b.get("schema") != SCHEMA for b in old):
        if any(m.get("summary") or m.get("agenda_archived") or m.get("minutes_archived")
               for b in old for m in b.get("meetings", [])):
            print("  ! DNSSAB records have summaries/archives — not rebuilding automatically")
        else:
            store.payload["boards"] = [b for b in store.payload["boards"]
                                       if b.get("group") != GROUP]
            print(f"  rebuilding DNSSAB group from eScribe (schema {SCHEMA}): "
                  f"dropped {len(old)} stored entries")
    body_for("Board Meeting", store)     # always present, even before data
    session = requests.Session()
    session.headers.update(ds.HEADERS)
    try:
        session.get(f"{BASE}?Year={date.today().year}", timeout=60)   # cookies
    except Exception as e:
        print(f"  (portal page failed: {e})")

    ok_years, total, docs = 0, 0, 0
    for year in range(FIRST_YEAR, date.today().year + 2):
        try:
            items = fetch_year(session, year)
        except Exception as e:
            print(f"  ✗ {year}: {e}")
            continue
        if items is None:
            continue
        ok_years += 1
        if items and year == FIRST_YEAR:
            print(f"  fields seen: {sorted(items[0].keys())}")
        for it in items:
            name = g(it, "MeetingName", "Name", "Title") or "Board Meeting"
            dt = parse_dt(g(it, "StartDate", "MeetingDate", "Start", "FormattedStart"))
            if not dt:
                print(f"  ! no date: {name}")
                continue
            mtype = "Special" if "special" in name.lower() else "Board"
            body = body_for(name, store)
            m = store.meeting(body, dt, mtype)
            m["title"] = f"DNSSAB: {name}" if "dnssab" not in name.lower() else name
            page = g(it, "Url", "MeetingUrl")
            if page:
                m["source_url"] = urljoin(BASE, page)
            loc = g(it, "Location")
            if loc:
                m["location"] = re.sub(r"<[^>]+>", " ", str(loc)).strip()
            if "cancel" in name.lower() or g(it, "IsCancelled", "Cancelled"):
                m["cancelled"] = True
            for doc in g(it, "MeetingDocumentLink", "Documents", "MeetingDocuments") or []:
                url = g(doc, "Url", "Link")
                if not url:
                    continue
                url = urljoin(BASE, url)
                fmt = (g(doc, "Format") or "").lower()
                title = g(doc, "Title", "Name", "Type") or "Document"
                if "html" in fmt or "meeting.aspx" in url.lower():
                    label = title if "html" in title.lower() else f"{title} (web page)"
                    add_doc(m, "extra", url, label)
                    continue
                add_doc(m, kind_for(doc), url, title)
                docs += 1
            total += 1
        print(f"  {year}: {len(items)} meeting(s)")

    if ok_years == 0:
        print("  ✗ calendar endpoint returned nothing usable — keeping stored data")
        return   # nothing saved, so a pending rebuild is simply retried next run
    print(f"  {total} meeting(s), {docs} document link(s)")
    store.save("boards/dnssab", mirror=lambda m, cat: mirror_escribe(session, m, cat))
    print(f"  archive: {STATS['ok']} document(s) mirrored this run, "
          f"{STATS['refused']} refused, {STATS['other']} other failures")


if __name__ == "__main__":
    main()
