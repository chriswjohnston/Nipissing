#!/usr/bin/env python3
"""
consolidate_bylaws.py — "how the law reads today", for every law in effect.

For each family in data/canonical/bylaw_families.json (a principal by-law and
the by-laws that amend it), gathers the text of every document and asks
Claude to apply the amendments in order, producing an UNOFFICIAL consolidated
text in which every changed passage is marked with the by-law that changed
it. The site shows it next to links to the original documents.

Every other law listed in laws_in_effect.json gets a page too, so all laws
read the same way ("What it does", then "How it reads today"):
  * a by-law with amendments found from titles ("amend By-law 2015-23") is
    consolidated like a family;
  * a by-law with none gets a clean text of the by-law as passed;
  * a very long by-law (the Zoning By-law) gets "What it does" and an outline
    of its parts only; the page links the original for the full text.

Text for each by-law, in order of preference:
  docs/files/bylaws/text/<number>.txt (agenda-package extract) >
  our archived PDF > the township PDF; scanned PDFs are OCR'd.
Texts are cached in docs/files/bylaws/text/ for search and reuse.

Regenerated only when a source text, the family, or the prompt changes
(input hash). On any failure the previous consolidation is kept.
Output: data/canonical/consolidations.json
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
FAMILIES = ROOT / "data" / "canonical" / "bylaw_families.json"
LAWS = ROOT / "data" / "canonical" / "laws_in_effect.json"
BYLAWS = ROOT / "data" / "canonical" / "bylaws.json"
OUT = ROOT / "data" / "canonical" / "consolidations.json"
TEXT_DIR = DOCS / "files" / "bylaws" / "text"

MODEL = "claude-sonnet-5-5"     # accuracy matters more than cost here
MAX_TOKENS = 32000
PROMPT_VERSION = 4
SUMMARY_PROMPT_VERSION = 1
LONG_PAGES = 40                    # longer than this: outline only, not the full text
BUDGET_SECONDS = 25 * 60           # the workflow caps each step at 30 minutes; the rest waits for the next run

# Same as shared.js AMEND_RE: "amend By-law 2021-50", "Amend Zoning By-Law Number 2020-20".
AMEND_RE = re.compile(r"\bamend(?:s|ing)?\b[^.]{0,80}?\b(?:by-?law|bl)\s*(?:no\.?|number|#)?\s*"
                      r"(\d{4}-\d{1,3}|\d{3,4})\b", re.I)
MAX_PDF_BYTES = 25 * 1024 * 1024   # API limit is 32 MB per request
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HEADERS = {"User-Agent": "Mozilla/5.0 (nipissing public records)"}

PROMPT = """You are preparing an UNOFFICIAL CONSOLIDATION of a municipal by-law of the
Township of Nipissing, Ontario, for a public records website. Residents will read it to
understand what the law says today. Accuracy matters more than polish.

You are given the principal by-law and the by-laws that amend it, oldest first, as PDFs
(with extracted text only where no PDF is available). Read tables from the page layout:
keep every column aligned, and leave a cell empty where the original is empty.
Copies taken from council agenda packages may be marked DRAFT; that is how the township
publishes its by-laws. Treat them as the passed by-law and do not mention the mark. Apply each
amendment in order and produce the full text as it reads after all of them.

Rules:
- Reproduce the principal by-law's wording faithfully, including schedules and tables
  (use Markdown tables). Fix obvious OCR errors only where the intended word is certain.
- Apply every amendment exactly as written. Do not paraphrase amended wording.
- Immediately after every passage that an amendment added, replaced or changed, add a
  marker on its own line: {{amended: <number> | <what changed, a few words>}}
  where <number> is always the AMENDING by-law's number, e.g. 2024-12 (never a section).
  For a deleted provision keep its number and write: "[Deleted]" followed by
  {{deleted: <number>}}
- The principal document may itself be a consolidation that already includes some
  amendments. If so, do not apply those again; list them in "already_incorporated".
- If an amendment's instruction is ambiguous or refers to text you cannot find, do not
  guess: leave the original text, add {{unclear: <number> | <short reason>}}, and list
  it in "uncertainties".
- Tables: reproduce only what you can read reliably. If a column or table is too
  garbled in the source to reproduce faithfully, leave it out and write in its place
  "[This table/column could not be read reliably from the source; see the original
  by-law.]" rather than reconstructing it.
- Maps and images: write "[Map: <title>. See the original by-law.]"
- Leave out signature blocks, "read a first, second and third time" lines and page
  headers/footers.

Answer in exactly two parts and nothing else.

Part 1, the full consolidated text in Markdown, with markers, between these tags:
<consolidated>
...
</consolidated>

Part 2, a JSON object between these tags:
<notes>
{
  "changes": [{"bylaw": "<number>", "where": "<section/schedule>", "change": "<plain-language summary>"}],
  "already_incorporated": ["<numbers>"],
  "reader_notes": ["<at most 3 short notes a resident needs to read this correctly, in plain language, e.g. that maps are not reproduced or that a table was left out; no technical detail about OCR or how you worked>"],
  "uncertainties": ["<technical review notes for the site editor: anything you could not resolve, OCR problems, judgement calls>"],
  "summary": "<2-3 sentences: what this law does, in plain language>"
}
</notes>

{documents}
"""

# Added when a by-law has no amendments: the same rules give a clean text.
SINGLE_NOTE = """
This by-law has NOT been amended. There are no amendments to apply: reproduce the by-law's
text as passed, cleanly, with no markers. "changes" and "already_incorporated" are empty lists.
"""

SUMMARY_PROMPT = """You are describing a long municipal by-law of the Township of Nipissing, Ontario, for a
public records website. Residents will read your description to understand what the law does
and find their way around the original document. Accuracy matters more than polish. Plain
language, short sentences, no legal advice.

The by-law follows as a PDF{amend_note}.

Answer with a JSON object between <notes> tags and nothing else:
<notes>
{{
  "summary": "<2-4 sentences: what this law does, in plain language>",
  "contents": [{{"part": "<section or part title as it appears, with its number>", "about": "<one plain sentence>"}}],
  "reader_notes": ["<at most 3 short notes a resident needs, in plain language>"],
  "uncertainties": ["<technical review notes for the site editor>"]
}}
</notes>
List the main parts in "contents" in document order (8 to 20 items), not every subsection.
"""


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def ocr_pdf(pdf: Path) -> str:
    import pymupdf as fitz
    if not shutil.which("tesseract"):
        return ""
    out = []
    for page in fitz.open(str(pdf)):
        png = page.get_pixmap(dpi=200, colorspace=fitz.csGRAY).tobytes("png")
        r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "3"],
                           input=png, capture_output=True, timeout=120)
        out.append(r.stdout.decode("utf-8", "ignore"))
    return "\n\n".join(out)


def pdf_text(data: bytes, tmp: Path) -> str:
    import pymupdf as fitz
    tmp.write_bytes(data)
    doc = fitz.open(str(tmp))
    text = "\n\n".join(p.get_text() for p in doc)
    if len(text.strip()) < 200 * max(1, len(doc)) // 4:     # mostly scanned
        text = ocr_pdf(tmp)
    return text


def bylaw_pdf(b: Dict[str, Any]) -> Optional[bytes]:
    """The by-law's PDF for the model to read directly (layout, tables)."""
    cands: List[Any] = []
    for key in ("pdf_url_archived", "pdf_url", "extracted_pdf"):
        v = b.get(key) or ""
        cands.append(v if v.startswith("http") else (DOCS / v if v else None))
    for src in cands:
        try:
            if isinstance(src, Path):
                if src.exists():
                    data = src.read_bytes()
                else:
                    continue
            elif src:
                r = requests.get(src, headers=HEADERS, timeout=120)
                if r.status_code != 200:
                    continue
                data = r.content
            else:
                continue
            if data[:5].startswith(b"%PDF") and len(data) <= MAX_PDF_BYTES:
                return data
        except Exception as e:
            print(f"  {b['number']}: could not read {src}: {e}")
    return None


def bylaw_text(b: Dict[str, Any]) -> Optional[str]:
    """Text of a by-law's main document. The township PDF (our archived copy
    first) when there is one; otherwise the agenda-package extract."""
    num = b["number"]
    cached = TEXT_DIR / f"{num}.txt"
    sources: List[Any] = []
    for key in ("pdf_url_archived", "pdf_url"):
        v = b.get(key) or ""
        if v.startswith("http"):
            sources.append(v)
        elif v and not v.startswith("files/bylaws/"):     # in-repo archived copy
            sources.append(DOCS / v)
    if not sources and b.get("extracted_text") and (DOCS / b["extracted_text"]).exists():
        return (DOCS / b["extracted_text"]).read_text(encoding="utf-8")
    for src in sources:
        try:
            if isinstance(src, Path):
                if not src.exists():
                    continue
                data = src.read_bytes()
            else:
                r = requests.get(src, headers=HEADERS, timeout=120)
                if r.status_code != 200 or not r.content[:5].startswith(b"%PDF"):
                    continue
                data = r.content
            text = pdf_text(data, Path("/tmp") / f"bylaw-{num}.pdf")
            if text.strip():
                TEXT_DIR.mkdir(parents=True, exist_ok=True)
                cached.write_text(text, encoding="utf-8")
                return text
        except Exception as e:
            print(f"  {num}: could not read {src}: {e}")
    if cached.exists():                    # last good text, if the source is gone
        return cached.read_text(encoding="utf-8")
    return None


def pdf_pages(data: Optional[bytes]) -> int:
    if not data:
        return 0
    try:
        import pymupdf as fitz
        return fitz.open(stream=data, filetype="pdf").page_count
    except Exception:
        return 0


def all_families(bylaws: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The hand-made families, then one for every other law in effect."""
    fams = [dict(f) for f in load(FAMILIES, {}).get("families", [])]
    covered = {n for f in fams for n in [f["principal"], *f.get("amendments", [])]}
    when = lambda n: (bylaws.get(n, {}).get("date_passed") or bylaws.get(n, {}).get("meeting_date") or "", n)
    amends: Dict[str, List[str]] = {}
    for b in bylaws.values():
        if b.get("repeals") or b.get("status") == "defeated":
            continue                                   # a replacement is not an amendment
        m = AMEND_RE.search(b.get("title") or "")
        if m and m.group(1) != b["number"]:
            amends.setdefault(m.group(1), []).append(b["number"])
    for topic in load(LAWS, {}).get("topics", []):
        for law in topic.get("laws", []):
            n = str(law["bylaw"])
            b = bylaws.get(n)
            if n in covered or not b or str(b.get("status", "")).lower() == "repealed":
                continue
            covered.add(n)
            fams.append({"id": f"law-{n}", "name": law["name"], "also_known_as": [],
                         "principal": n, "amendments": sorted(amends.get(n, []), key=when),
                         "consolidate": True, "auto": True})
    return fams


def call_claude(content: List[Dict[str, Any]], want_text: bool = True) -> Dict[str, Any]:
    """Streams the response (long outputs can take several minutes)."""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": MAX_TOKENS, "stream": True,
              "messages": [{"role": "user", "content": content}]},
        timeout=(30, 300), stream=True,
    )
    if r.status_code != 200:
        raise RuntimeError(f"API {r.status_code}: {r.text[:400]}")
    parts, stop = [], None
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        ev = json.loads(line[5:])
        t = ev.get("type")
        if t == "content_block_delta" and ev["delta"].get("type") == "text_delta":
            parts.append(ev["delta"]["text"])
        elif t == "message_delta":
            stop = ev.get("delta", {}).get("stop_reason") or stop
        elif t == "error":
            raise RuntimeError(f"API stream error: {ev.get('error')}")
    text = "".join(parts)
    if stop == "max_tokens":
        raise RuntimeError("output hit the length limit; consolidation incomplete")
    if not want_text:
        m = re.search(r"<notes>\s*(\{.*\})\s*</notes>", text, re.S)
        if not m:
            raise RuntimeError(f"no notes in response: {text[:200]!r}")
        return json.loads(m.group(1))
    body = re.search(r"<consolidated>\s*(.*?)\s*</consolidated>", text, re.S)
    if not body or len(body.group(1)) < 200:
        raise RuntimeError(f"no consolidated text in response (stop={stop}, {len(text)} chars): {text[:200]!r}")
    notes: Dict[str, Any] = {}
    m = re.search(r"<notes>\s*(\{.*\})\s*</notes>", text, re.S)
    if m:
        try:
            notes = json.loads(m.group(1))
        except Exception as e:
            notes = {"uncertainties": [f"(notes could not be read: {e})"]}
    notes["consolidated_markdown"] = body.group(1)
    return notes


def main() -> None:
    import time
    print("=" * 60)
    print("Nipissing consolidate_bylaws.py")
    print("=" * 60)
    bylaws = {b["number"]: b for b in load(BYLAWS, {}).get("bylaws", [])}
    families = all_families(bylaws)
    out = load(OUT, {"consolidations": {}})
    store = out.setdefault("consolidations", {})
    # A law taken off laws_in_effect.json loses its generated page.
    wanted = {f["id"] for f in families}
    for fid in [k for k, v in store.items() if v.get("auto") and k not in wanted]:
        del store[fid]
        print(f"  {fid}: no longer listed — removed")
    deadline = time.monotonic() + BUDGET_SECONDS
    out_of_time = False

    for fam in families:
        fid = fam["id"]
        members = [fam["principal"]] + list(fam.get("amendments", []))
        # A very long by-law (Zoning) is described, not reproduced: only its
        # own document is needed, not every amendment's.
        principal_pdf = bylaw_pdf(bylaws[fam["principal"]]) if fam["principal"] in bylaws else None
        long_doc = pdf_pages(principal_pdf) > LONG_PAGES
        texts, missing = {}, []
        for n in members:
            b = bylaws.get(n)
            t = bylaw_text(b) if b else None
            if t and t.strip():
                texts[n] = t
            elif not (long_doc and n != fam["principal"]):
                missing.append(n)
        entry = store.get(fid, {})
        entry.update({
            "id": fid, "name": fam["name"], "also_known_as": fam.get("also_known_as", []),
            "principal": fam["principal"], "amendments": fam.get("amendments", []),
            "missing_text": missing,
        })
        if fam.get("auto"):
            entry["auto"] = True
        store[fid] = entry
        if missing:
            print(f"  {fid}: no text yet for {', '.join(missing)} — waits")
            continue
        if not fam.get("consolidate"):
            continue
        pdfs = {n: (principal_pdf if n == fam["principal"] else (None if long_doc else bylaw_pdf(bylaws[n])))
                for n in members}
        mode = "outline" if long_doc else ("consolidated" if fam.get("amendments") else "clean")
        if mode == "outline":
            # Only the principal is read; amendments are listed on the page.
            pdfs = {n: (pdfs[n] if n == fam["principal"] else None) for n in members}
        elif sum(len(v) for v in pdfs.values() if v) > MAX_PDF_BYTES:    # keep the request under the limit
            for n in sorted(pdfs, key=lambda n: -len(pdfs[n] or b""))[1:]:
                pdfs[n] = None
        version = SUMMARY_PROMPT_VERSION if mode == "outline" else PROMPT_VERSION
        digest = hashlib.sha256(json.dumps(
            [version, mode, MODEL, members, [texts.get(n) for n in members],
             [hashlib.sha256(pdfs[n]).hexdigest() if pdfs[n] else None for n in members]]
        ).encode()).hexdigest()
        done = entry.get("consolidated_markdown") or (mode == "outline" and entry.get("contents"))
        if entry.get("input_hash") == digest and done:
            print(f"  {fid}: unchanged")
            continue
        # Made before law pages had modes, from these same documents: keep it.
        legacy = hashlib.sha256(json.dumps(
            [PROMPT_VERSION, MODEL, members, [texts.get(n) for n in members],
             [hashlib.sha256(pdfs[n]).hexdigest() if pdfs[n] else None for n in members]]
        ).encode()).hexdigest()
        if entry.get("consolidated_markdown") and entry.get("mode") is None and entry.get("input_hash") == legacy:
            entry["mode"] = mode
            entry["input_hash"] = digest
            print(f"  {fid}: unchanged (kept)")
            continue
        if not API_KEY:
            print(f"  {fid}: ANTHROPIC_API_KEY not set — skipping")
            continue
        if time.monotonic() > deadline:
            out_of_time = True
            continue
        p = bylaws[fam["principal"]]
        head0 = (f"=== PRINCIPAL BY-LAW: By-law {p['number']} — {p.get('title', '')} "
                 f"(passed {p.get('date_passed') or p.get('meeting_date') or 'date unknown'}) ===")
        if mode == "outline":
            amend_note = (f". It has since been amended by By-law {', '.join(fam['amendments'])}; "
                          "describe the by-law itself, the amendments are listed separately") if fam.get("amendments") else ""
            content = [{"type": "text", "text": SUMMARY_PROMPT.format(amend_note=amend_note)}, {"type": "text", "text": head0}]
            if pdfs[p["number"]]:
                content.append({"type": "document", "title": f"By-law {p['number']}",
                                "source": {"type": "base64", "media_type": "application/pdf",
                                           "data": base64.b64encode(pdfs[p["number"]]).decode()}})
            else:
                content[-1]["text"] += "\n" + texts[p["number"]]
        else:
            prompt = PROMPT.replace("{documents}", (SINGLE_NOTE if mode == "clean" else "") + "The documents follow.")
            content = [{"type": "text", "text": prompt}]
            for i, n in enumerate(members):
                b = bylaws[n]
                role = "PRINCIPAL BY-LAW" if i == 0 else f"AMENDMENT {i}"
                head = (f"=== {role}: By-law {n} — {b.get('title', '')} "
                        f"(passed {b.get('date_passed') or b.get('meeting_date') or 'date unknown'}) ===")
                if pdfs[n]:
                    content.append({"type": "text", "text": head})
                    content.append({"type": "document", "title": f"By-law {n}",
                                    "source": {"type": "base64", "media_type": "application/pdf",
                                               "data": base64.b64encode(pdfs[n]).decode()}})
                else:
                    content.append({"type": "text", "text": f"{head}\n{texts[n]}"})
        print(f"  {fid} ({mode}): sending " + ", ".join(f"{n} ({'PDF' if pdfs[n] else 'text'})" for n in members))
        try:
            result = call_claude(content, want_text=(mode != "outline"))
        except Exception as e:
            msg = str(e)[:500]
            print(f"  ✗ {fid}: {msg} — keeping previous version")
            print(f"::warning title=Law page failed: {fid}::{msg}")
            entry["last_error"] = {"date": date.today().isoformat(), "error": msg}
            continue
        entry.pop("last_error", None)
        entry.update({
            "mode": mode,
            "consolidated_markdown": result.get("consolidated_markdown", ""),
            "contents": result.get("contents", []) if mode == "outline" else [],
            "pages": pdf_pages(bylaw_pdf(p)) if mode == "outline" else None,
            "changes": result.get("changes", []) if mode == "consolidated" else [],
            "already_incorporated": result.get("already_incorporated", []),
            "uncertainties": result.get("uncertainties", []),
            "reader_notes": result.get("reader_notes", [])[:3],
            "summary": result.get("summary", ""),
            "generated": date.today().isoformat(), "model": MODEL, "input_hash": digest,
        })
        print(f"  {fid}: done ({mode}; {len(entry['changes'])} change(s), "
              f"{len(entry['uncertainties'])} uncertainty(ies))")
        # Save as we go: a later failure or the time limit keeps what's done.
        OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    if out_of_time:
        print("  out of time — the remaining laws continue next run")
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
