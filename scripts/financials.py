#!/usr/bin/env python3
"""
financials.py — keep every township financial document, daily. No AI.

The township removes older documents from its site (most of the 2022-2025
budget presentations and capital forecasts were already gone by October
2026), so this runs every day and saves a copy of anything financial the
moment it appears. It builds data/canonical/financials.json, the list the
Financials page shows.

Where it looks:
  1. The township's financial pages (audited statements, tax information,
     property assessment): every PDF linked there.
  2. The township's media library (WordPress), searched for financial words:
     finds files that aren't linked from any page, and files staff
     re-uploaded under a new address after deleting the old one.
  3. Meeting documents (meetings.json extra_docs) with financial titles:
     budget presentations, capital forecasts, KPMG reports, the asset
     management plan. archive.py already copies these; this lists them and,
     for ones deleted before a copy was made, tries the media library and
     the Wayback Machine (bounded, like recover_documents.py).
  4. The Province's Financial Information Return (FIR), the standard return
     every Ontario municipality files each year: the yearly open-data files
     are filtered to Nipissing's rows and kept in data/financial/fir/. They
     give a consistent history back to 2009, for the charts.

Nothing is ever deleted. Figures shown on the page come from
data/canonical/financials.json "figures", entered by hand from the audited
statements (each with its source page) and checked against the FIR.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
from datetime import date
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urljoin, urlparse

import requests

import doc_sources as ds
from archive import is_township_pdf, mirror, store_bytes_in_repo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "canonical" / "financials.json"
MEETINGS = ROOT / "data" / "canonical" / "meetings.json"
FIR_DIR = ROOT / "data" / "financial" / "fir"
STATE = ROOT / "data" / "runtime" / "financials_state.json"
TODAY = date.today().isoformat()
BUDGET_SECONDS = 20 * 60

PAGES = [
    "https://nipissingtownship.com/financial/",
    "https://nipissingtownship.com/financial/audited-financial-statements/",
    "https://nipissingtownship.com/financial/tax-information/",
    "https://nipissingtownship.com/financial/property-assessment/",
]
MEDIA_TERMS = ["financial", "budget", "capital", "forecast", "audited", "KPMG", "tax",
               "asset management", "levy", "remuneration", "statement"]
FIN_RE = re.compile(r"financ|budget|capital|forecast|audit|KPMG|asset.management|tax.rate|levy|"
                    r"remunerat|statement.of.(?:accounts|remun)|\bFS\b|-FS|xFS|reserve|user.fee", re.I)

# The FIR open-data files: one zip a year, every municipality.
FIR_INDEX = "https://efis.fma.csc.gov.on.ca/fir/MultiYearReport/MYCIndex.html"
FIR_ZIP = "https://efis.fma.csc.gov.on.ca/fir/MultiYearReport/fir_data_{year}.zip"
FIR_FIRST_YEAR = 2009                   # full accrual accounting from 2009; earlier years don't compare
MUNICIPALITY = re.compile(r"^\s*nipissing\s+tp\s*$", re.I)   # "Nipissing Tp" in the FIR


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def kind_of(title: str, url: str) -> str:
    t = f"{title} {unquote(url)}".lower()
    if re.search(r"audited|-fs\b|xfs|financial.statements?(?!.presentation)", t) and "presentation" not in t:
        return "Audited financial statements"
    if re.search(r"capital", t):
        return "Capital budget and forecast"
    if re.search(r"budget", t):
        return "Budget"
    if re.search(r"kpmg|financial.(?:indicator|analysis|overview)|statement.presentation", t):
        return "Auditor’s presentation"
    if re.search(r"asset.management", t):
        return "Asset management plan"
    if re.search(r"tax.rate|tax-rate|levy", t):
        return "Tax rates"
    if re.search(r"remunerat|statement.of", t):
        return "Council remuneration"
    if re.search(r"user.fee", t):
        return "User fees"
    if re.search(r"collection", t):
        return "Policy"
    return "Other financial document"


def year_of(title: str, url: str) -> Optional[int]:
    """The year the document is about (2024 statements, 2025 budget), not the upload year."""
    name = unquote(Path(urlparse(url).path).name)
    for text in (title, name):
        m = re.search(r"\b(20[12]\d)\b", text or "")
        if m:
            return int(m.group(1))
    m = re.search(r"/uploads/(20\d\d)/", url)
    return int(m.group(1)) if m else None


def title_from(url: str) -> str:
    name = unquote(Path(urlparse(url).path).stem)
    name = re.sub(r"[-_]+", " ", name)
    return re.sub(r"\s+\d$", "", name).strip()


def links_on(page: str) -> List[Dict[str, str]]:
    try:
        r = ds.polite_get(page, timeout=60)
    except Exception as e:
        print(f"  {page}: {e}")
        return []
    if r.status_code != 200:
        print(f"  {page}: HTTP {r.status_code}")
        return []
    out = []
    for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S | re.I):
        url = urljoin(page, unescape(href)).replace("http://", "https://", 1)
        if is_township_pdf(url):
            label = re.sub(r"<[^>]+>|\s+", " ", unescape(text)).strip(" :")
            out.append({"url": url, "title": label or title_from(url), "found_on": page})
    return out


def main() -> None:
    print("=" * 60)
    print("Nipissing financials.py")
    print("=" * 60)
    deadline = time.monotonic() + BUDGET_SECONDS
    data = load(OUT, {})
    data.setdefault("_about", "Financial documents kept by the archive (built daily by scripts/financials.py) "
                              "and the figures the Financials page charts (entered by hand; every figure names its source).")
    docs: Dict[str, Dict[str, Any]] = {(d.get("url") or d.get("archived")): d for d in data.get("documents", [])}
    state = load(STATE, {})

    def add(url: str, title: str, **extra) -> Dict[str, Any]:
        d = docs.get(url)
        if not d:
            d = docs[url] = {"url": url, "title": title or title_from(url), "kind": kind_of(title, url),
                             "year": year_of(title, url), "first_seen": TODAY}
            print(f"  new: {d['kind']}, {d['title']}")
        for k, v in extra.items():
            if v and not d.get(k):
                d[k] = v
        if not d.get("archived") and not d.get("gone"):
            got = mirror(url, "financial", d.get("year"))
            if got:
                d["archived"] = got
        return d

    # 1. The financial pages.
    for page in PAGES:
        for link in links_on(page):
            add(link["url"], link["title"], found_on=link["found_on"])

    # 2. The media library: unlinked files and re-uploads.
    media: List[Dict[str, Any]] = []
    for term in MEDIA_TERMS:
        if time.monotonic() > deadline:
            break
        media += [m for m in ds.wp_media_search(term, max_pages=5)
                  if m.get("mime") == "application/pdf" and FIN_RE.search(f"{m['title']} {m['url']}")]
    by_name: Dict[str, str] = {}
    for m in media:
        add(m["url"], unescape(m["title"]), uploaded=m.get("date"))
        by_name.setdefault(Path(urlparse(m["url"]).path).name.lower(), m["url"])

    # 3. Financial documents presented at meetings.
    meetings = load(MEETINGS, {"meetings": []})
    meetings_changed = False
    for m in meetings.get("meetings", []):
        for doc in m.get("extra_docs") or []:
            url, label = doc.get("url") or "", doc.get("label") or ""
            if not FIN_RE.search(f"{label} {url}"):
                continue
            d = docs.get(url) or {"url": url, "title": re.sub(r"\*+", "", label).strip(), "kind": kind_of(label, url),
                                  "year": year_of(label, url), "first_seen": TODAY}
            docs[url] = d
            d.setdefault("meetings", [])
            if m["date"] not in d["meetings"]:
                d["meetings"].append(m["date"])
                d["meetings"].sort()
            if doc.get("archived") and not d.get("archived"):
                d["archived"] = doc["archived"]
            if d.get("archived") or d.get("gone"):
                continue
            # Deleted before a copy was kept: the same file re-uploaded elsewhere?
            same = by_name.get(Path(urlparse(url).path).name.lower())
            if same and same != url:
                again = docs.get(same) or add(same, d["title"])
                if again.get("archived"):
                    d["archived"] = again["archived"]
                    d["recovered_from"] = same
                    print(f"  recovered (re-uploaded): {d['title']}")
                    continue
            # The Wayback Machine, once per URL while recovery runs.
            if ds.recovery_active() and is_township_pdf(url) and url not in state and time.monotonic() < deadline:
                data_bytes, outcome = ds.wayback_fetch(url, "2")
                state[url] = {"outcome": "recovered" if data_bytes else outcome, "checked": TODAY}
                if data_bytes:
                    got = store_bytes_in_repo(url, "financial", d.get("year"), data_bytes)
                    if got:
                        d["archived"] = got
                        d["recovered_from"] = "Wayback Machine"
                        print(f"  recovered (Wayback): {d['title']}")
                        continue
            if state.get(url, {}).get("outcome") in ("notfound", "notpdf"):
                d["gone"] = True
    # Financial schedules attached to by-laws (the asset management plan as
    # adopted by By-law 2025-32), and plans that are financial in nature.
    for b in load(ROOT / "data" / "canonical" / "bylaws.json", {"bylaws": []}).get("bylaws", []):
        for doc in b.get("extra_docs") or []:
            if doc.get("archived") and FIN_RE.search(doc.get("label") or "") and not re.match(r"Earlier", doc.get("label") or ""):
                d = docs.setdefault(doc["url"], {"url": doc["url"], "title": doc.get("label"), "kind": kind_of(doc.get("label") or "", doc["url"]),
                                                 "year": year_of(doc.get("label") or "", "") or b.get("year"), "first_seen": TODAY})
                d.update({"archived": doc["archived"], "bylaw": b["number"]})
    for plan in load(ROOT / "data" / "canonical" / "plans.json", {"plans": []}).get("plans", []):
        if plan.get("kind") == "Asset management":
            d = docs.setdefault(plan["url"], {"url": plan["url"], "title": plan.get("title"), "kind": "Asset management plan",
                                              "year": plan.get("from"), "first_seen": TODAY})
            for k in ("archived", "as_adopted"):
                if plan.get(k):
                    d[k] = plan[k]

    # Copy recovered files back onto the meetings, so meeting pages link them too.
    for m in meetings.get("meetings", []):
        for doc in m.get("extra_docs") or []:
            d = docs.get(doc.get("url") or "")
            if d and d.get("archived") and not doc.get("archived"):
                doc["archived"] = d["archived"]
                meetings_changed = True
    if meetings_changed:
        MEETINGS.write_text(json.dumps(meetings, indent=2, ensure_ascii=False), encoding="utf-8")

    # 4. The FIR: one small file per year with Nipissing's rows only.
    fir_years(deadline)

    data["documents"] = sorted(docs.values(), key=lambda d: (-(d.get("year") or 0), d.get("kind", ""), d.get("title", "")))
    data["fir_years"] = sorted(int(p.stem) for p in FIR_DIR.glob("*.csv")) if FIR_DIR.exists() else []
    data["updated"] = TODAY
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    kept = sum(1 for d in docs.values() if d.get("archived"))
    print(f"  {len(docs)} financial documents, {kept} kept, {sum(1 for d in docs.values() if d.get('gone'))} gone")


def fir_years(deadline: float) -> None:
    """Nipissing's rows from each year's FIR file. A year is fetched once,
    and the latest two again each month (late filings and corrections)."""
    FIR_DIR.mkdir(parents=True, exist_ok=True)
    this_year = date.today().year
    for year in range(this_year - 1, FIR_FIRST_YEAR - 1, -1):
        out = FIR_DIR / f"{year}.csv"
        fresh = out.exists() and (year < this_year - 2 or
                                  time.time() - out.stat().st_mtime < 30 * 86400)
        if fresh or time.monotonic() > deadline:
            continue
        try:
            r = requests.get(FIR_ZIP.format(year=year), headers=ds.HEADERS, timeout=(30, 600))
        except Exception as e:
            print(f"  FIR {year}: {e}")
            continue
        if r.status_code != 200 or r.content[:2] != b"PK":
            print(f"  FIR {year}: not published (HTTP {r.status_code})")
            continue
        rows, header, head = [], None, None
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for name in z.namelist():
                if not name.lower().endswith(".csv"):
                    continue
                with z.open(name) as f:
                    reader = csv.reader(io.TextIOWrapper(f, encoding="latin-1", newline=""))
                    head = next(reader, None)
                    for row in reader:
                        if any(MUNICIPALITY.match(c) for c in row[:12]):
                            header = header or head
                            rows.append(row)
        if not rows:
            print(f"  FIR {year}: no rows for Nipissing Tp (columns: {head})")
            continue
        with out.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        print(f"  FIR {year}: {len(rows)} rows kept")


if __name__ == "__main__":
    main()
