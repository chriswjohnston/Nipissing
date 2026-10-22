"""Fill a by-law's missing date_passed from its own PDF.

By-laws listed only on the township's by-laws page come without a date, but
the document itself ends with "READ A FIRST, SECOND AND THIRD TIME AND PASSED
THIS 13th DAY OF SEPTEMBER, 2011". This reads that line from our archived
copy: the text layer first, then OCR (Tesseract) for scanned documents.

Only fills dates that are missing; never changes one already recorded.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pymupdf

import package_extract

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"

MONTHS = {m[:3]: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}

# "passed this 13th day of September, 2011", "third time this 5TH DAY OF
# JANUARY, 2021", "PASSED THIS THE 17" DAY OF DECEMBER, 2013", OCR's "DAV",
# and a month split by a scan ("Sept mber").
PASSED_RE = re.compile(
    r"(?:passed|third\s+time)\s+(?:and\s+passed\s+)?(?:this|on)\s+(?:the\s+)?"
    r"(\d{1,2})\s*(?:st|nd|rd|th|\")?\s+da[yv]\s+of\s+([a-z][a-z ]{2,11}?)\s*,?\s*((?:19|20)\d\d)",
    re.IGNORECASE)


def _date_from_text(text: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", text or "")
    found = None
    for day, month, year in PASSED_RE.findall(text):
        mon = MONTHS.get(re.sub(r"[^a-z]", "", month.lower())[:3])
        d = int(day)
        if mon and 1 <= d <= 31:
            found = f"{year}-{mon:02d}-{d:02d}"   # the last match is the final reading
    return found


def _pdf_path(b: Dict[str, Any]) -> Optional[Path]:
    for rel in (b.get("pdf_url_archived"), b.get("extracted_pdf")):
        if rel and not str(rel).startswith("http"):
            p = DOCS_DIR / str(rel).lstrip("/").removeprefix("docs/")
            if p.exists():
                return p
    return None


def date_from_pdf(path: Path, ocr: bool) -> Optional[str]:
    try:
        doc = pymupdf.open(path)
    except Exception:
        return None
    found = _date_from_text(" ".join(page.get_text() for page in doc))
    if found or not ocr:
        return found
    # Scanned: OCR from the last page back; the passing line is near the end.
    for i in range(doc.page_count - 1, max(-1, doc.page_count - 7), -1):
        png = doc[i].get_pixmap(dpi=220).tobytes("png")
        found = _date_from_text(package_extract.ocr_png(png))
        if found:
            return found
    return None


def fill(bylaws: List[Dict[str, Any]]) -> Dict[str, int]:
    ocr = package_extract.have_tesseract()
    stats = {"filled": 0, "not_found": 0, "no_pdf": 0}
    for b in bylaws:
        if b.get("date_passed"):
            continue
        path = _pdf_path(b)
        if not path:
            stats["no_pdf"] += 1
            continue
        found = date_from_pdf(path, ocr)
        # A by-law can be passed early the next year (2020-51: January 2021).
        year = b.get("year")
        if found and (not year or int(found[:4]) - int(year) in (0, 1)):
            b["date_passed"] = found
            b["date_passed_source"] = "pdf"
            stats["filled"] += 1
            print(f"  {b.get('number')}: passed {found} (from the PDF)")
        else:
            stats["not_found"] += 1
    return stats


if __name__ == "__main__":
    import json
    p = Path(__file__).resolve().parents[1] / "data" / "canonical" / "bylaws.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    print(fill(data["bylaws"]))
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
