#!/usr/bin/env python3
"""
package_extract.py — pull each by-law out of its council agenda package.

Most by-laws are only published as a few pages inside a 100+ page agenda
package, and most packages are scans with no text layer. For every council
meeting that passed by-laws, this:

  1. gets the package (our archived copy first, else the township's),
  2. reads the top of every page — text layer, or OCR (Tesseract) for scans —
     to find where each by-law starts and ends,
  3. saves those pages as docs/files/bylaws/<number>.pdf, plus the page text
     as docs/files/bylaws/text/<number>.txt (search, summaries, consolidation).

Finding the pages:
  * start: the first page whose top carries the by-law's own heading
    ("BY-LAW NO. 2024-12"), skipping agenda/listing pages that name several
    by-laws;
  * end: the page before the next document starts (another by-law, a staff
    report, minutes, correspondence), keeping schedules that name this by-law;
    after the signing page ("read a first, second and third time") only
    schedules continue it. Capped at 40 pages.

Each package is processed once and remembered in
data/runtime/package_extraction.json; it is redone only if the set of
by-laws for that meeting changes. Newest meetings first, within a time
budget, so new by-laws are handled immediately and the 2022+ backlog is
worked through over a few runs.

The extracted pages are the version presented to council. If council changed
a by-law at the meeting, the minutes record it; the site says so.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
OUT_DIR = DOCS_DIR / "files" / "bylaws"
TEXT_DIR = OUT_DIR / "text"
STATE_FILE = ROOT / "data" / "runtime" / "package_extraction.json"

MIN_YEAR = 2022            # matches the site and archive cutoffs
BUDGET_SECONDS = 15 * 60   # per run; the rest continues next run
MAX_PAGES = 40
TOP_FRACTION = 0.30        # share of a page read for headings
OCR_WORKERS = 4            # GitHub runners have 4 cores
# Bump when detection improves: packages done by an older version that had
# by-laws not found are processed again.
EXTRACTOR_VERSION = 2
HEADERS = {"User-Agent": "Mozilla/5.0 (nipissing public records; by-law extraction)"}

# "BY-LAW NO. 2024-12", "By-law Number 2024 - 12", OCR's "BY—LAW #2024-012"
HEAD_RE = re.compile(
    r"BY\s*[-–—]?\s*LAW\s*(?:NO\.?|NUMBER|#)?\s*[:.]?\s*(20\d\d)\s*[-–—_ ]\s*0*(\d{1,3})\b", re.I)
NEW_DOC_RE = re.compile(
    r"^\s*(?:the\s+)?corporation\s+of\s+the\s+(?:township|municipality)|"
    r"^\s*(?:staff\s+)?report\b|^\s*memo(?:randum)?\b|^\s*minutes\s+of\b|"
    r"^\s*to\s*:\s*(?:mayor|council|members)|^\s*correspondence\b|^\s*request\s+for\s+decision",
    re.I | re.M)
SIGNED_RE = re.compile(r"first,?\s+second\s+and\s+third\s+time|third\s+time\s+and\s+finally\s+passed", re.I)
SCHEDULE_RE = re.compile(r"\bschedule\b|\bappendix\b", re.I)
LISTING_RE = re.compile(r"\bagenda\b", re.I)


def norm(year: str, num: str) -> str:
    return f"{int(year)}-{int(num):02d}"


def norm_number(n: str) -> str:
    m = re.match(r"\s*(20\d\d)\s*[-–]\s*0*(\d{1,3})\s*$", n or "")
    return norm(m.group(1), m.group(2)) if m else (n or "").strip()


# ── reading pages ─────────────────────────────────────────────────────────────

def have_tesseract() -> bool:
    return shutil.which("tesseract") is not None


def ocr_png(png: bytes) -> str:
    try:
        # One thread per tesseract: several run side by side, and each one
        # grabbing every core (its default) made pages crawl and time out on
        # GitHub's 2-core runners (a 121-page package took 27 minutes and
        # came back nearly empty).
        r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "3"],
                           input=png, capture_output=True, timeout=60,
                           env={**os.environ, "OMP_THREAD_LIMIT": "1"})
        return r.stdout.decode("utf-8", "ignore")
    except Exception:
        return ""


def page_top_text(page, ocr: bool) -> Tuple[str, str]:
    """(text of the top of the page, method). Text layer first, OCR for scans."""
    import pymupdf as fitz
    full = page.get_text()
    if len(full.strip()) >= 40:
        clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                         page.rect.y0 + page.rect.height * TOP_FRACTION)
        return page.get_text(clip=clip), "text"
    if not ocr:
        return "", "none"
    clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                     page.rect.y0 + page.rect.height * TOP_FRACTION)
    pix = page.get_pixmap(dpi=130, colorspace=fitz.csGRAY, clip=clip)
    return ocr_png(pix.tobytes("png")), "ocr"


def page_top_job(page, ocr: bool):
    """Like page_top_text, but returns a callable so OCR can run in a pool."""
    import pymupdf as fitz
    full = page.get_text()
    clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                     page.rect.y0 + page.rect.height * TOP_FRACTION)
    if len(full.strip()) >= 40:
        txt = page.get_text(clip=clip)
        return lambda: (txt, "text")
    if not ocr:
        return lambda: ("", "none")
    png = page.get_pixmap(dpi=110, colorspace=fitz.csGRAY, clip=clip).tobytes("png")
    return lambda: (ocr_png(png), "ocr")


def page_full_text(page, ocr: bool) -> str:
    import pymupdf as fitz
    full = page.get_text()
    if len(full.strip()) >= 40 or not ocr:
        return full
    pix = page.get_pixmap(dpi=200, colorspace=fitz.csGRAY)
    return ocr_png(pix.tobytes("png"))


# ── finding a by-law's pages ──────────────────────────────────────────────────

# OCR slips inside numbers: "2O26-26", "2026-l2", "2O24 - I2".
def _fix_digits(text: str) -> str:
    return re.sub(r"(?<=[\d\s-])[OoQD](?=[\d\s-]*\d)|(?<=\d)[OoQD]",
                  "0", re.sub(r"(?<=[\d-])[lI|](?=[\d\s]|$)|(?<=[\s-])[lI|](?=\d)", "1", text or ""))


def heads(text: str) -> List[str]:
    return [norm(y, n) for y, n in HEAD_RE.findall(_fix_digits(text))]


START_RE = re.compile(r"corporation\s+of\s+the\s+(?:township|municipality)", re.I)
# The resolution that passes a by-law ("THAT we pass By-Law Number …,
# being a By-Law to …") sits just before it; it is not the by-law.
RESOLUTION_RE = re.compile(r"moved\s+by|seconded\s+by|\bR\s*20\d\d\s*-\s*\d", re.I)
BEING_RE = re.compile(r"being\s+a\s+by[\s-]*law", re.I)


def start_score(text: str) -> int:
    """How much a page top looks like the first page of a by-law."""
    t = (text or "")[:600]
    return (2 * bool(START_RE.search(t)) + 2 * bool(BEING_RE.search(t))
            + bool(HEAD_RE.search(_fix_digits(t[:250]))))


def own_heading(text: str) -> Optional[str]:
    """The by-law a page belongs to: the first by-law number near the top.
    "BY-LAW NUMBER 2024-12 Being a by-law to Amend By-Law Number 2019-41"
    belongs to 2024-12 — later numbers are references, not headings."""
    hs = heads((text or "")[:350])
    return hs[0] if hs else None


def find_ranges(tops: List[str], full, targets: List[str]) -> Dict[str, Tuple[int, int]]:
    """tops: top-of-page text for every page (finding starts).
    full(i): whole-page text, read lazily while walking a by-law's pages."""
    out: Dict[str, Tuple[int, int]] = {}
    for target in targets:
        # Candidates lead with this by-law's number and aren't listing pages
        # (agenda, minutes). Of those, the one that looks most like a by-law's
        # first page wins (staff reports that mention the number lose):
        # "THE CORPORATION OF THE TOWNSHIP … BY-LAW NO. … Being a by-law to …"
        cands = [i for i, t in enumerate(tops)
                 if own_heading(t) == target and not LISTING_RE.search(t[:300])
                 and not RESOLUTION_RE.search(t[:400]) and len(set(heads(t))) <= 3]
        if not cands:
            continue
        best = max(start_score(tops[i]) for i in cands)
        start = next(i for i in cands if start_score(tops[i]) == best)
        end = start
        signed = bool(SIGNED_RE.search(full(start)))
        for j in range(start + 1, min(len(tops), start + MAX_PAGES)):
            top = tops[j]
            first = own_heading(top)
            mentions_self = target in set(heads(top))
            if first and first != target and NEW_DOC_RE.search(top[:250]):
                break                                   # next by-law starts here
            if NEW_DOC_RE.search(top[:400]) and not mentions_self:
                break                                   # next report/minutes/etc.
            if signed and not (SCHEDULE_RE.search(top[:400]) or mentions_self):
                break                                   # past the signing page
            end = j
            if SIGNED_RE.search(full(j)):
                signed = True
        out[target] = (start, end)
    return out


# ── package handling ──────────────────────────────────────────────────────────

def package_source(meeting: Dict[str, Any]) -> Optional[str]:
    arch = meeting.get("package_archived")
    if arch:
        return arch if arch.startswith("http") else str(DOCS_DIR / arch)
    return meeting.get("package_url")


def fetch_package(src: str, dest: Path) -> bool:
    if not src.startswith("http"):
        if Path(src).exists():
            shutil.copyfile(src, dest)
            return True
        return False
    try:
        r = requests.get(src, headers=HEADERS, timeout=180)
        r.raise_for_status()
    except Exception as e:
        print(f"    package download failed: {e}")
        return False
    if not r.content[:5].startswith(b"%PDF"):
        return False
    dest.write_bytes(r.content)
    return True


def process_meeting(meeting: Dict[str, Any], targets: List[str], ocr: bool,
                    workdir: Path) -> Dict[str, Any]:
    import pymupdf as fitz
    src = package_source(meeting)
    entry: Dict[str, Any] = {"targets": targets, "checked": date.today().isoformat(),
                             "package": src, "bylaws": {}}
    if not src:
        entry["status"] = "no-package"
        return entry
    pdf_path = workdir / "package.pdf"
    if not fetch_package(src, pdf_path):
        entry["status"] = "failed"          # retried next run
        return entry
    doc = fitz.open(str(pdf_path))
    # Page tops: render serially (PyMuPDF), OCR in parallel (Tesseract is a
    # separate process, so threads give real concurrency).
    from concurrent.futures import ThreadPoolExecutor
    jobs, tops, methods = [], [], set()
    for page in doc:
        jobs.append(page_top_job(page, ocr))
    with ThreadPoolExecutor(max_workers=OCR_WORKERS) as ex:
        for t, how in ex.map(lambda j: j(), jobs):
            tops.append(t)
            methods.add(how)
    cache: Dict[int, str] = {}

    def full(i: int) -> str:
        if i not in cache:
            cache[i] = page_full_text(doc[i], ocr)
        return cache[i]

    ranges = find_ranges(tops, full, targets)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    for target, (s, e) in ranges.items():
        out = fitz.open()
        out.insert_pdf(doc, from_page=s, to_page=e)
        path = OUT_DIR / f"{target}.pdf"
        out.save(str(path), garbage=4, deflate=True)
        out.close()
        text = "\n\n".join(full(i) for i in range(s, e + 1))
        (TEXT_DIR / f"{target}.txt").write_text(text, encoding="utf-8")
        entry["bylaws"][target] = {
            "pdf": path.relative_to(DOCS_DIR).as_posix(),
            "text": (TEXT_DIR / f"{target}.txt").relative_to(DOCS_DIR).as_posix(),
            "pages": [s + 1, e + 1],
            "method": "ocr" if "ocr" in methods else "text",
        }
    doc.close()
    entry["missing"] = sorted(set(targets) - set(ranges))
    entry["version"] = EXTRACTOR_VERSION
    if entry["missing"]:
        # Show what the page tops said wherever a missing by-law is mentioned,
        # so a miss can be diagnosed from the log.
        for i, t in enumerate(tops):
            flat = " ".join(_fix_digits(t).split())
            if any(re.search(re.escape(n).replace("\\-", r"\s*[-–]\s*0*"), flat) for n in entry["missing"]):
                print(f"    p{i + 1} mentions {', '.join(entry['missing'])}: {flat[:140]}")
    entry["status"] = "done" if ocr or "none" not in methods else "needs-ocr"
    return entry


def run(meetings: List[Dict[str, Any]], bylaws: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Process what's new or pending; returns the full state (all meetings)."""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    ocr = have_tesseract()
    if ocr:
        try:
            v = subprocess.run(["tesseract", "--version"], capture_output=True, text=True).stdout.split("\n")[0]
            print(f"  OCR: {v}")
        except Exception:
            pass
    if not ocr:
        print("  (Tesseract not installed: scanned packages are left for a later run)")

    by_meeting: Dict[str, List[str]] = {}
    for b in bylaws:
        d = (b.get("meeting_date") or "")[:10]
        if b.get("number") and d and int(d[:4]) >= MIN_YEAR and b.get("status") != "defeated":
            by_meeting.setdefault(d, []).append(norm_number(b["number"]))
    meeting_by_date = {m.get("date"): m for m in meetings}

    deadline = time.monotonic() + BUDGET_SECONDS
    done = found = 0
    with tempfile.TemporaryDirectory() as tmp:
        for d in sorted(by_meeting, reverse=True):              # newest first
            targets = sorted(set(by_meeting[d]))
            prev = state.get(d, {})
            if prev.get("targets") == targets and prev.get("status") in ("done", "no-package") \
                    and not (prev.get("missing") and prev.get("version", 1) < EXTRACTOR_VERSION):
                continue
            if prev.get("status") == "needs-ocr" and not ocr and prev.get("targets") == targets:
                continue
            m = meeting_by_date.get(d)
            if not m:
                continue
            if time.monotonic() > deadline:
                print("  out of time — remaining packages continue next run")
                break
            t0 = time.monotonic()
            entry = process_meeting(m, targets, ocr, Path(tmp))
            if entry["status"] not in ("done",) and prev.get("bylaws"):
                entry["bylaws"] = prev["bylaws"]    # a failed fetch keeps earlier finds
            state[d] = entry
            done += 1
            found += len(entry["bylaws"])
            miss = f", not found: {', '.join(entry.get('missing', []))}" if entry.get("missing") else ""
            print(f"  {d}: {entry['status']} — {len(entry['bylaws'])}/{len(targets)} by-law(s) "
                  f"in {time.monotonic() - t0:.0f}s{miss}")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    pending = sum(1 for d, t in by_meeting.items()
                  if state.get(d, {}).get("status") not in ("done", "no-package"))
    print(f"  packages processed this run: {done}, by-laws extracted: {found}, "
          f"meetings still pending: {pending}")
    return state


def apply(state: Dict[str, Any], bylaws: List[Dict[str, Any]]) -> int:
    """Attach extracted PDFs/text to by-law records. A standalone PDF from the
    township's by-law page stays the main document; the extract is added."""
    found = {}
    for d, entry in state.items():
        for num, info in (entry.get("bylaws") or {}).items():
            found[num] = dict(info, meeting_date=d.split("#")[0])   # "<date>#index": found by package_index.py
    n = 0
    for b in bylaws:
        num = norm_number(b.get("number", ""))
        info = found.get(num)
        if not info:
            # An extract the current extractor no longer finds was wrong pages
            # (e.g. the resolution passing the by-law): drop it. These are our
            # own cut-outs, never township documents.
            stale = f"files/bylaws/{num}.pdf"
            if b.get("extracted_pdf") == stale or b.get("pdf_url") == stale:
                for k in ("extracted_pdf", "extracted_text", "extracted_from"):
                    b.pop(k, None)
                if b.get("pdf_url") == stale:
                    b["pdf_url"] = None
                for f in (DOCS_DIR / stale, TEXT_DIR / f"{num}.txt"):
                    f.unlink(missing_ok=True)
                print(f"  removed stale extract for {num}")
            continue
        b["extracted_pdf"] = info["pdf"]
        b["extracted_text"] = info["text"]
        b["extracted_from"] = {"meeting_date": info["meeting_date"], "pages": info["pages"],
                               "method": info["method"]}
        if info.get("presented"):
            b["extracted_from"]["presented"] = info["presented"]
        pdf = b.get("pdf_url") or ""
        if not pdf or pdf.startswith("files/bylaws/"):
            b["pdf_url"] = info["pdf"]
        n += 1
    return n
