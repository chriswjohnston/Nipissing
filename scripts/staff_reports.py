#!/usr/bin/env python3
"""
staff_reports.py — each staff report in an agenda package as its own PDF.

package_index.py finds the reports (kind "report") and their pages. This
cuts those pages out of the package into docs/files/archive/reports/<year>/,
so a meeting page can link straight to the report instead of page 15 of a
package. Scanned pages are re-rendered at 150 dpi in greyscale with a
searchable text layer (Tesseract), which keeps each page near 100 KB;
pages with their own text are copied as they are.

Writes data/canonical/staff_reports.json:
  {"reports": {"<date>#<first>-<last>": {"file": "files/archive/…pdf", "pages": n}}}
Keyed by page range, so a report re-filed by new rules gets a new cut; cuts
no longer in the index are deleted. 20 minutes per run; the rest next day.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import package_extract as pe

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "data" / "canonical"
INDEX = CANON / "package_index.json"
OUT = CANON / "staff_reports.json"
DOCS = ROOT / "docs"
REPORT_DIR = DOCS / "files" / "archive" / "reports"
BUDGET_SECONDS = int(os.environ.get("STAFF_REPORTS_SECONDS", 20 * 60))


def slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-")
    return s[:60].rstrip("-") or "staff-report"


def cut(doc, first: int, last: int, dest: Path, ocr: bool) -> None:
    import pymupdf as fitz
    out = fitz.open()
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(first - 1, last):
            page = doc[i]
            # A scan: one picture covering the page (often with an OCR layer).
            area = page.rect.width * page.rect.height
            scanned = any(abs(fitz.Rect(im["bbox"])) > 0.6 * area for im in page.get_image_info())
            if not scanned:
                out.insert_pdf(doc, from_page=i, to_page=i)
                continue
            pix = page.get_pixmap(dpi=150, colorspace=fitz.csGRAY)
            img = Path(tmp) / f"{i}.jpg"
            img.write_bytes(pix.tobytes("jpeg", jpg_quality=60))
            if ocr:
                base = Path(tmp) / f"{i}"
                r = subprocess.run(["tesseract", str(img), str(base), "pdf"],
                                   capture_output=True, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
                if r.returncode == 0 and base.with_suffix(".pdf").exists():
                    out.insert_pdf(fitz.open(str(base.with_suffix(".pdf"))))
                    continue
            p = out.new_page(width=page.rect.width, height=page.rect.height)
            p.insert_image(p.rect, filename=str(img))
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(dest), garbage=4, deflate=True)


def main() -> None:
    meetings = {m["date"]: m for m in json.loads((CANON / "meetings.json").read_text(encoding="utf-8"))["meetings"]}
    packages = json.loads(INDEX.read_text(encoding="utf-8")).get("packages", {})
    state = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    state.setdefault("_about", "Staff reports cut out of agenda packages (scripts/staff_reports.py). "
                               "Key: <meeting date>#<first page>-<last page> in the package.")
    done = state.setdefault("reports", {})

    wanted = {}
    for d, entry in packages.items():
        for it in entry.get("items", []):
            if it.get("kind") == "report":
                wanted[f"{d}#{it['pages'][0]}-{it['pages'][1]}"] = (d, it)

    # Drop cuts for page ranges the index no longer has.
    for key in [k for k in done if k not in wanted]:
        f = DOCS / done.pop(key)["file"]
        if f.exists():
            f.unlink()

    todo = sorted({d for k, (d, _) in wanted.items() if k not in done}, reverse=True)
    only = [a for a in sys.argv[1:] if re.match(r"\d{4}-\d\d-\d\d$", a)]
    if only:
        todo = [d for d in todo if d in only]
    print(f"  {len(todo)} package(s) with staff reports to cut")
    ocr = pe.have_tesseract()
    deadline = time.monotonic() + BUDGET_SECONDS
    import pymupdf as fitz
    with tempfile.TemporaryDirectory() as tmp:
        for d in todo:
            if time.monotonic() > deadline:
                print("  out of time — the rest continue next run")
                break
            src = packages[d].get("package") or pe.package_source(meetings.get(d, {}))
            local = next((Path(a) for a in sys.argv[1:] if a.endswith(".pdf") and d in a), None)
            pdf = local or Path(tmp) / f"{d}.pdf"
            if not local and not (src and pe.fetch_package(src, pdf)):
                print(f"  {d}: package not available")
                continue
            doc = fitz.open(str(pdf))
            for key, (dd, it) in wanted.items():
                if dd != d or key in done:
                    continue
                first, last = it["pages"]
                if last > len(doc):
                    continue
                name = f"{d}_p{first}-{last}_{slug(it.get('title') or 'staff-report')}.pdf"
                dest = REPORT_DIR / d[:4] / name
                cut(doc, first, last, dest, ocr)
                done[key] = {"file": dest.relative_to(DOCS).as_posix(), "pages": last - first + 1}
                print(f"  {d} p{first}-{last}: {dest.stat().st_size // 1024} KB  {it.get('title') or ''}")
            doc.close()
            if not local:
                pdf.unlink(missing_ok=True)
            OUT.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  staff reports kept: {len(done)} of {len(wanted)}")


if __name__ == "__main__":
    main()
