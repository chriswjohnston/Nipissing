#!/usr/bin/env python3
"""
package_index.py — what is inside each agenda package, page by page. No AI.

Staff reports, quotes, capital forecasts and draft resolutions only exist
inside the agenda package: the minutes say "received the staff report", the
report itself is page 2 of a 69-page PDF. (The August 11, 2026 update on the
landfill cards, saying they were mailed in June and 1,792 were used in July,
is one: nothing outside the package shows it.) This reads every package and
records its contents:

  data/canonical/package_index.json   per meeting: the documents in the
      package, each with its kind (report, resolution, by-law, minutes,
      correspondence, other), title, pages, and for reports the author and
      date. Pages that follow a draft resolution (quotes, forecasts, letters)
      are filed as that resolution's backing papers.
  data/packages/<date>.txt.gz         the package's text, page by page
      (form feed between pages), for search and for the projects pass.

Text comes from the PDF's text layer, or OCR for scanned pages. A package
is read once; re-read only when the extractor improves (INDEX_VERSION).
25 minutes per run; the backlog continues the next day.
"""
from __future__ import annotations

import gzip
import json
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import package_extract as pe

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "data" / "canonical"
OUT = CANON / "package_index.json"
TEXT_DIR = ROOT / "data" / "packages"
INDEX_VERSION = 1
BUDGET_SECONDS = int(__import__("os").environ.get("PACKAGE_INDEX_SECONDS", 25 * 60))
MIN_YEAR = 2022

NUM = r"(\d{4})\s?-\s?(\d{1,3})"
RES_RE = re.compile(r"RESOLUTION", re.I)
RES_NUM_RE = re.compile(r"NUMBER\s*[:.]?\s*[-—=_ ]*R\s?" + NUM, re.I)
REPORT_RE = re.compile(r"REPORT\s+TO\s+COUNCIL|STAFF\s+REPORT|INFORMATION\s+REPORT|REPORT\s+TO\s+THE\s+COUNCIL", re.I)
MEMO_RE = re.compile(r"\bRe\s*:|\bSubject\s*:", re.I)
BYLAW_RE = re.compile(r"BY-?\s?LAW\s+(?:NO\.?|NUMBER)\s*" + NUM, re.I)
BEING_RE = re.compile(r"being\s+a\s+by-?\s?law", re.I)
# "MINUTES", "Museum Board Minutes", "Minutes of the ... meeting"; not "The minutes of the Council meeting shall".
MINUTES_RE = re.compile(r"^\W{0,40}(?:MINUTES\b|Minutes\s+(?:of|for)\b|(?:[A-Z][\w&'.-]*\s+){1,8}(?:Meeting\s+)?Minutes\b)")
CORR_RE = re.compile(r"\bCORRESPONDENCE\b")
AGENDA_RE = re.compile(r"^\W{0,60}(?:\S+\s+){0,12}AGENDA\b", re.I)


def page_texts(pdf: Path, ocr: bool) -> List[str]:
    import pymupdf
    doc = pymupdf.open(pdf)
    texts: List[Optional[str]] = []
    jobs = []
    for i, page in enumerate(doc):
        t = page.get_text()
        if len(t.strip()) >= 40 or not ocr:
            texts.append(t)
        else:
            texts.append(None)
            jobs.append((i, page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY).tobytes("png")))
    if jobs:
        with ThreadPoolExecutor(max(2, __import__('os').cpu_count() or 2)) as ex:
            for (i, _), t in zip(jobs, ex.map(lambda j: pe.ocr_png(j[1]), jobs)):
                texts[i] = t
    return [t or "" for t in texts]


def one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def report_fields(text: str) -> Dict[str, str]:
    t = re.sub(r"[ \t]+", " ", text)
    out = {}
    m = re.search(r"\bRe\s*:\s*(.+?)(?:\n\s*\n|\n(?=[A-Z][a-z]+:)|Background|Purpose|Summary|$)", t, re.S)
    if not m:
        m = re.search(r"\bSubject\s*:\s*(.+?)(?:\n\s*\n|$)", t, re.S)
    if m:
        out["title"] = one_line(m.group(1))[:160]
    if "title" not in out:      # "STAFF REPORT" then the title on its own line
        m = re.search(r"(?:STAFF\s+REPORT|REPORT\s+TO\s+COUNCIL)[ \t|:.\-—]*\n+\s*([^\n]{6,160})", t, re.I)
        if m and not re.match(r"\s*(From|Date|To|Re)\s*:", m.group(1), re.I):
            out["title"] = one_line(m.group(1)).strip(" |")
    if "title" not in out:      # "2023 Budget Update — Staff Report — Page 2" / "Page 2 - Staff Report - X"
        m = re.search(r"^\W*(?:Page\s+\d+\s*[-—–]\s*Staff\s+Report\s*[-—–]\s*([^\n]{4,120})|([^\n]{4,120}?)\s*[-—–]\s*Staff\s+Report\s*[-—–]\s*Page)", t, re.I | re.M)
        if m:
            out["title"] = one_line(m.group(1) or m.group(2))
    m = re.search(r"\bFrom\s*:\s*(.+?)(?:\n\s*(?:To|Re|Date|Subject)\s*:|\n\s*\n)", t, re.S | re.I)
    if m:
        who = re.split(r"The Corporation|Telephone|www\.|\d{3}-\d{3}|\b(?:Re|Date|To|Subject)\s*:", m.group(1), flags=re.I)[0]
        out["from"] = one_line(who).strip(" ,;")[:120]
    m = re.search(r"\bDate\s*:\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})", t, re.I)
    if m:
        out["date"] = one_line(m.group(1))
    return out


def tidy_title(t: str) -> str:
    """Drop what runs on from the next heading: "X BACKGROUND/OVERVIEW", "X Dear Council,"."""
    t = re.split(r"\s+(?:BACKGROUN\w*|OVERVIEW|ISSUE\s*:|PURPOSE\s*:?|Dear\s+Council|Date\s*:|-{3,})", t or "")[0]
    return t.strip(" |:-—.")


def classify(text: str, first: bool) -> Optional[Dict[str, Any]]:
    """A new document starting on this page, or None (the page continues the last one)."""
    top = pe._fix_digits(text[:700])
    flat = one_line(top)
    # "Page 2 - Staff Report - NOHFC Application": a report's later page.
    if re.match(r"\W{0,10}Page\s+\d+\b", flat) or re.search(r"Staff\s+Report\s*[-—–]\s*Page\s+\d", flat[:120], re.I):
        return None
    # The agenda lists "Staff Reports." as an item; it is not one.
    if first and re.search(r"\bAGENDA\b", flat[:500]):
        return {"kind": "agenda", "title": "Agenda"}
    if REPORT_RE.search(flat[:500]) or (MEMO_RE.search(flat[:600]) and re.search(r"\bFrom\s*:", flat[:600])
                                       and re.search(r"\bDate\s*:", flat[:600]) and not re.search(r"\bSent\s*:", flat[:600])):
        f = report_fields(text)
        if f.get("title"):
            f["title"] = tidy_title(f["title"])
        return {"kind": "report", **f}
    # The township's own draft resolutions (other councils' resolutions in
    # the correspondence are part of the correspondence).
    if RES_RE.search(flat[:400]) and re.search(r"\bDATE\s*:", flat[:400], re.I) \
            and (RES_NUM_RE.search(flat[:500]) or re.search(r"NIPIS\s?SING", flat[:400], re.I)):
        m = RES_NUM_RE.search(flat[:500])
        return {"kind": "resolution", "number": f"R{m.group(1)}-{m.group(2)}" if m else None,
                "title": one_line(re.split(r"\bTHAT\b|\bWHEREAS\b", flat, maxsplit=1, flags=re.I)[-1])[:160] if re.search(r"\bTHAT\b|\bWHEREAS\b", flat, re.I) else ""}
    m = BYLAW_RE.search(flat[:300])
    if m and BEING_RE.search(flat[:600]):
        return {"kind": "bylaw", "number": f"{m.group(1)}-{int(m.group(2)):02d}",
                "title": one_line(re.split(BEING_RE, flat, maxsplit=1)[-1])[:160]}
    if CORR_RE.search(flat[:300]):
        return {"kind": "correspondence", "title": "Correspondence"}
    if MINUTES_RE.search(flat[:200]) and not re.search(r"Page\s+\d", flat[:80]):
        return {"kind": "minutes", "title": one_line(flat[:120])}
    if first and AGENDA_RE.search(flat[:200]):
        return {"kind": "agenda", "title": "Agenda"}
    return None


def segment(pages: List[str]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for i, text in enumerate(pages):
        start = classify(text, i == 0)
        if start:
            # A letterhead page with nothing else, then the report: one document.
            if items and items[-1].get("kind") == "other" and items[-1]["pages"] == [i, i] and start["kind"] == "report":
                items.pop()
                start["pages"] = [i, i + 1]
            else:
                start["pages"] = [i + 1, i + 1]
            items.append(start)
        elif items:
            items[-1]["pages"][1] = i + 1
            # A report whose Re: line is on its second page (letterhead first)
            if items[-1]["kind"] == "report" and not items[-1].get("title"):
                items[-1].update({k: v for k, v in report_fields(text).items() if k not in items[-1]})
        else:
            items.append({"kind": "other", "pages": [i + 1, i + 1], "title": one_line(text[:100])})
    # Pages after a draft resolution (until the next document) are its backing papers.
    for it in items:
        if it["kind"] == "resolution" and it["pages"][1] > it["pages"][0]:
            it["papers"] = [it["pages"][0] + 1, it["pages"][1]]
    return items


def resegment() -> None:
    """Re-file every package from its saved text (after the rules change); no download, no OCR."""
    index = json.loads(OUT.read_text(encoding="utf-8"))
    for d, entry in index.get("packages", {}).items():
        f = TEXT_DIR / f"{d}.txt.gz"
        if f.exists():
            entry["items"] = segment(gzip.open(f, "rt", encoding="utf-8").read().split("\f"))
    OUT.write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"  re-filed {len(index.get('packages', {}))} packages")


def main() -> None:
    if "--resegment" in sys.argv:
        resegment()
        return
    meetings = json.loads((CANON / "meetings.json").read_text(encoding="utf-8"))["meetings"]
    index = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    index.setdefault("_about", "Contents of each council agenda package (scripts/package_index.py). Pages are 1-based. Text: data/packages/<date>.txt.gz.")
    pk = index.setdefault("packages", {})
    ocr = pe.have_tesseract()
    deadline = time.monotonic() + BUDGET_SECONDS
    todo = [m for m in sorted(meetings, key=lambda m: m["date"], reverse=True)
            if int(m["date"][:4]) >= MIN_YEAR and pe.package_source(m)
            and pk.get(m["date"], {}).get("version") != INDEX_VERSION]
    only = [a for a in sys.argv[1:] if re.match(r"\d{4}-\d\d-\d\d$", a)]
    if only:
        todo = [m for m in meetings if m["date"] in only]
    print(f"  {len(todo)} package(s) to read")
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for m in todo:
            if time.monotonic() > deadline:
                print("  out of time — the rest continue next run")
                break
            t0 = time.monotonic()
            dest = Path(tmp) / f"{m['date']}.pdf"
            src = pe.package_source(m)
            local = next((Path(a) for a in sys.argv[1:] if a.endswith(".pdf") and m["date"] in a), None)
            if local:
                dest = local
            elif not pe.fetch_package(src, dest):
                print(f"  {m['date']}: package not available")
                continue
            pages = page_texts(dest, ocr)
            blank = sum(1 for t in pages if len(t.strip()) < 40)
            if blank > len(pages) * 0.3:
                # OCR timed out or failed on many pages: don't file a half-read
                # package as done; it's tried again next run.
                print(f"  {m['date']}: {blank} of {len(pages)} pages unreadable — will retry")
                continue
            if not any(len(p.strip()) > 40 for p in pages):
                print(f"  {m['date']}: no text (OCR unavailable?) — later")
                continue
            with gzip.open(TEXT_DIR / f"{m['date']}.txt.gz", "wt", encoding="utf-8") as f:
                f.write("\f".join(pages))
            items = segment(pages)
            pk[m["date"]] = {"package": src, "pages": len(pages), "items": items,
                             "version": INDEX_VERSION, "read": date.today().isoformat()}
            reports = [i for i in items if i["kind"] == "report"]
            print(f"  {m['date']}: {len(pages)} pages, {len(items)} documents, {len(reports)} report(s) "
                  f"in {time.monotonic() - t0:.0f}s" + "".join(f"\n      report p{r['pages'][0]}: {r.get('title', '?')}" for r in reports))
            OUT.write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
    OUT.write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
