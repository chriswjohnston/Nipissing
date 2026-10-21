#!/usr/bin/env python3
"""
consolidate_bylaws.py — "how the law reads today" for amended by-laws.

For each family in data/canonical/bylaw_families.json (a principal by-law and
the by-laws that amend it), gathers the text of every document and asks
Claude to apply the amendments in order, producing an UNOFFICIAL consolidated
text in which every changed passage is marked with the by-law that changed
it. The site shows it next to links to the original documents.

Text for each by-law, in order of preference:
  docs/files/bylaws/text/<number>.txt (agenda-package extract) >
  our archived PDF > the township PDF; scanned PDFs are OCR'd.
Texts are cached in docs/files/bylaws/text/ for search and reuse.

Regenerated only when a source text, the family, or the prompt changes
(input hash). On any failure the previous consolidation is kept.
Output: data/canonical/consolidations.json
"""

from __future__ import annotations

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
BYLAWS = ROOT / "data" / "canonical" / "bylaws.json"
OUT = ROOT / "data" / "canonical" / "consolidations.json"
TEXT_DIR = DOCS / "files" / "bylaws" / "text"

MODEL = "claude-sonnet-5-5"     # accuracy matters more than cost here
MAX_TOKENS = 32000
PROMPT_VERSION = 2
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HEADERS = {"User-Agent": "Mozilla/5.0 (nipissing public records)"}

PROMPT = """You are preparing an UNOFFICIAL CONSOLIDATION of a municipal by-law of the
Township of Nipissing, Ontario, for a public records website. Residents will read it to
understand what the law says today. Accuracy matters more than polish.

You are given the principal by-law and the by-laws that amend it, oldest first. Apply each
amendment in order and produce the full text as it reads after all of them.

Rules:
- Reproduce the principal by-law's wording faithfully, including schedules and tables
  (use Markdown tables). Fix obvious OCR errors only where the intended word is certain.
- Apply every amendment exactly as written. Do not paraphrase amended wording.
- Immediately after every passage that an amendment added, replaced or changed, add a
  marker on its own line: {{amended: <number> | <what changed, a few words>}}
  For a deleted provision keep its number and write: "[Deleted]" followed by
  {{deleted: <number>}}
- The principal document may itself be a consolidation that already includes some
  amendments. If so, do not apply those again; list them in "already_incorporated".
- If an amendment's instruction is ambiguous or refers to text you cannot find, do not
  guess: leave the original text, add {{unclear: <number> | <short reason>}}, and list
  it in "uncertainties".
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
  "uncertainties": ["<plain-language notes>"],
  "summary": "<2-3 sentences: what this law does, in plain language>"
}
</notes>

{documents}
"""


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def ocr_pdf(pdf: Path) -> str:
    import fitz
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
    import fitz
    tmp.write_bytes(data)
    doc = fitz.open(str(tmp))
    text = "\n\n".join(p.get_text() for p in doc)
    if len(text.strip()) < 200 * max(1, len(doc)) // 4:     # mostly scanned
        text = ocr_pdf(tmp)
    return text


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


def call_claude(prompt: str) -> Dict[str, Any]:
    """Streams the response (long outputs can take several minutes)."""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": MAX_TOKENS, "stream": True,
              "messages": [{"role": "user", "content": prompt}]},
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
    print("=" * 60)
    print("Nipissing consolidate_bylaws.py")
    print("=" * 60)
    families = load(FAMILIES, {}).get("families", [])
    bylaws = {b["number"]: b for b in load(BYLAWS, {}).get("bylaws", [])}
    out = load(OUT, {"consolidations": {}})
    store = out.setdefault("consolidations", {})

    for fam in families:
        fid = fam["id"]
        members = [fam["principal"]] + list(fam.get("amendments", []))
        texts, missing = {}, []
        for n in members:
            b = bylaws.get(n)
            t = bylaw_text(b) if b else None
            if t and t.strip():
                texts[n] = t
            else:
                missing.append(n)
        entry = store.get(fid, {})
        entry.update({
            "id": fid, "name": fam["name"], "also_known_as": fam.get("also_known_as", []),
            "principal": fam["principal"], "amendments": fam.get("amendments", []),
            "missing_text": missing,
        })
        store[fid] = entry
        if missing:
            print(f"  {fid}: no text yet for {', '.join(missing)} — consolidation waits")
            continue
        if not fam.get("consolidate"):
            continue
        digest = hashlib.sha256(json.dumps(
            [PROMPT_VERSION, MODEL, members, [texts[n] for n in members]]).encode()).hexdigest()
        if entry.get("input_hash") == digest and entry.get("consolidated_markdown"):
            print(f"  {fid}: unchanged")
            continue
        if not API_KEY:
            print(f"  {fid}: ANTHROPIC_API_KEY not set — skipping")
            continue
        docs = []
        for i, n in enumerate(members):
            b = bylaws[n]
            role = "PRINCIPAL BY-LAW" if i == 0 else f"AMENDMENT {i}"
            docs.append(f"=== {role}: By-law {n} — {b.get('title', '')} "
                        f"(passed {b.get('date_passed') or 'date unknown'}) ===\n{texts[n]}")
        try:
            result = call_claude(PROMPT.replace("{documents}", "\n\n".join(docs)))
        except Exception as e:
            msg = str(e)[:500]
            print(f"  ✗ {fid}: {msg} — keeping previous consolidation")
            print(f"::warning title=Consolidation failed: {fid}::{msg}")
            entry["last_error"] = {"date": date.today().isoformat(), "error": msg}
            continue
        entry.pop("last_error", None)
        entry.update({
            "consolidated_markdown": result.get("consolidated_markdown", ""),
            "changes": result.get("changes", []),
            "already_incorporated": result.get("already_incorporated", []),
            "uncertainties": result.get("uncertainties", []),
            "summary": result.get("summary", ""),
            "generated": date.today().isoformat(), "model": MODEL, "input_hash": digest,
        })
        print(f"  {fid}: consolidated ({len(entry['changes'])} change(s), "
              f"{len(entry['uncertainties'])} uncertainty(ies))")

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
