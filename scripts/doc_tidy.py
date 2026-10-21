#!/usr/bin/env python3
"""
doc_tidy.py — one entry per document on a meeting.

The township's site links the same file in several ways, and carry-forward
keeps every one: http:// and https://, the original upload and a re-upload in
another month's folder (the June 2026 relaunch re-uploaded everything to
/2026/05/), and WordPress "-1" copies. They are the same document.

tidy(meeting) merges extra documents that share a file name (case, scheme,
upload folder and "-N" suffix ignored), keeping the newest upload's URL and
an archived copy, and drops extras that repeat the meeting's own agenda,
minutes or package. When two archived copies of the "same" name are both in
the repo and differ in content, both are kept (a genuinely revised file).

Run on its own, it tidies every meeting in meetings.json and boards.json.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "data" / "canonical"
DOCS = ROOT / "docs"
PRIMARY = ("agenda_url", "minutes_url", "package_url")
UPLOAD_RE = re.compile(r"/wp-content/uploads/(\d{4})/(\d{2})/")


def https(url: str) -> str:
    url = (url or "").strip()
    return "https://" + url[7:] if url.startswith("http://") else url


def doc_key(url: str) -> str:
    """Same document regardless of scheme, upload folder or -N copy suffix."""
    if not url:
        return ""
    p = urlsplit(https(url))
    name = unquote(Path(p.path).name).lower()
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"-\d$", "", stem)
    if "nipissingtownship.com" in p.netloc:
        return f"township:{stem}.{ext}"
    # other hosts (eScribe, Drive): query strings identify the file
    return f"{p.netloc}{p.path}?{p.query}".lower()


def _upload(url: str) -> str:
    m = UPLOAD_RE.search(url or "")
    return f"{m.group(1)}{m.group(2)}" if m else ""


def _content(path: str) -> bytes | None:
    if not path or path.startswith("http"):
        return None
    f = DOCS / path
    return f.read_bytes() if f.exists() else None


def tidy(m: Dict[str, Any]) -> int:
    """Merge duplicate extra docs on one meeting, in place. Returns removed count."""
    extras = m.get("extra_docs") or []
    if not extras:
        return 0
    primary = {doc_key(m.get(f) or "") for f in PRIMARY if m.get(f)}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for d in extras:
        k = doc_key(d.get("url") or "")
        if not k or k in primary:
            continue
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(d)

    out: List[Dict[str, Any]] = []
    for k in order:
        docs = groups[k]
        # newest upload, then https, wins the URL
        docs.sort(key=lambda d: (_upload(d.get("url")), (d.get("url") or "").startswith("https")),
                  reverse=True)
        # split into genuinely different contents (when we can tell)
        variants: List[Dict[str, Any]] = []
        seen: List[bytes] = []
        for d in docs:
            body = _content(d.get("archived"))
            if body is not None and seen and body not in seen:
                v = dict(d, url=https(d.get("url")))
                v["label"] = f"{d.get('label') or 'Document'} (earlier version)"
                variants.append(v)
                seen.append(body)
                continue
            if body is not None and body not in seen:
                seen.append(body)
            if not variants:
                keep = {"label": d.get("label") or "Document", "url": https(d.get("url"))}
                variants.append(keep)
            if not variants[0].get("archived") and d.get("archived") \
                    and (body is None or body == seen[0]):
                variants[0]["archived"] = d["archived"]
        # keep the most descriptive label among merged entries
        labels = [d.get("label") or "" for d in docs]
        variants[0]["label"] = max(labels, key=len) or "Document"
        # say so when a file is a draft and its label doesn't
        if "draft" in k and "draft" not in variants[0]["label"].lower():
            variants[0]["label"] += " (draft)"
        out.extend(variants)
    removed = len(extras) - len(out)
    m["extra_docs"] = out
    return removed


def main() -> None:
    total = 0
    for name, getter in (("meetings.json", lambda d: [d["meetings"]]),
                         ("boards.json", lambda d: [b.get("meetings") or [] for b in d["boards"]])):
        path = CANON / name
        data = json.loads(path.read_text(encoding="utf-8"))
        n = 0
        for ms in getter(data):
            for m in ms:
                n += tidy(m)
        if n:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  {name}: {n} duplicate document link(s) merged")
        total += n


if __name__ == "__main__":
    main()
