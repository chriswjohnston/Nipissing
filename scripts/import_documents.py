#!/usr/bin/env python3
"""
import_documents.py — file documents someone already has into the record.

The township deleted many documents before this archive kept a copy, and
residents (Chris among them) saved some of them. Put the PDFs in inbox/ (on
GitHub: open the inbox folder, "Add file" > "Upload files") or pass paths:

    python scripts/import_documents.py path/to/file.pdf ...

Each file is matched to the township address it came from by its file name
("CAPITAL-BUDGET-2024-2034-FORECAST.pdf" -> the capital forecast presented
on April 16, 2024). The match fills the missing copy everywhere that address
appears: meeting documents, by-laws, the Finances list. A file that matches
nothing is kept as a financial document when its name looks financial,
otherwise left in inbox/ and reported, so nothing is filed by guesswork.
Files from inbox/ are removed once filed (the copy lives in docs/files).
Runs daily, before financials.py; does nothing when inbox/ is empty.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from archive import store_bytes_in_repo

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "data" / "canonical"
INBOX = ROOT / "inbox"
LOG = ROOT / "data" / "runtime" / "import_log.json"
TODAY = date.today().isoformat()
FIN_RE = re.compile(r"financ|budget|capital|forecast|audit|kpmg|asset.management|tax|levy|remunerat|reserve|-fs\b|xfs", re.I)


def key(name: str) -> str:
    """Comparable file name: no upload prefix, no '__hash', no '-1' copy suffix."""
    n = unquote(Path(name).name).lower()
    n = re.sub(r"^[0-9a-f]{8}-", "", n)                 # chat-upload prefix
    n = re.sub(r"__[0-9a-f]{8}(?=\.pdf$)", "", n)       # our archive suffix
    n = re.sub(r"[-_ ]\d(?=\.pdf$)", "", n)             # WordPress "-1" re-upload
    return re.sub(r"[^a-z0-9.]+", "-", n)


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def slots() -> Dict[str, List[Dict[str, Any]]]:
    """file-name key -> every place in the data that names that file without a copy."""
    out: Dict[str, List[Dict[str, Any]]] = {}

    def add(url: Optional[str], holder: Dict[str, Any], field: str, category: str, year, file: Path, payload):
        if url and str(url).startswith("http"):
            out.setdefault(key(urlparse(url).path), []).append(
                {"url": url, "holder": holder, "field": field, "category": category,
                 "year": year, "file": file, "payload": payload})

    meetings_path = CANON / "meetings.json"
    meetings = load(meetings_path, {"meetings": []})
    for m in meetings["meetings"]:
        y = m.get("year") or int(str(m.get("date", "0"))[:4] or 0)
        add(m.get("minutes_url"), m, "minutes_archived", "meetings", y, meetings_path, meetings)
        add(m.get("agenda_url"), m, "agenda_archived", "meetings", y, meetings_path, meetings)
        for d in m.get("extra_docs") or []:
            add(d.get("url"), d, "archived", "meetings", y, meetings_path, meetings)
    boards_path = CANON / "boards.json"
    boards = load(boards_path, {"boards": []})
    for b in boards["boards"]:
        for m in b.get("meetings", []):
            y = m.get("year") or int(str(m.get("date", "0"))[:4] or 0)
            add(m.get("minutes_url"), m, "minutes_archived", f"boards/{b['id']}", y, boards_path, boards)
            for d in m.get("extra_docs") or []:
                add(d.get("url"), d, "archived", f"boards/{b['id']}", y, boards_path, boards)
    bylaws_path = CANON / "bylaws.json"
    bylaws = load(bylaws_path, {"bylaws": []})
    for b in bylaws["bylaws"]:
        add(b.get("pdf_url"), b, "pdf_url_archived", "bylaws", b.get("year"), bylaws_path, bylaws)
        for d in b.get("extra_docs") or []:
            add(d.get("url"), d, "archived", "bylaws", b.get("year"), bylaws_path, bylaws)
    fin_path = CANON / "financials.json"
    fin = load(fin_path, {"documents": []})
    for d in fin.get("documents", []):
        add(d.get("url"), d, "archived", "financial", d.get("year"), fin_path, fin)
    plans_path = CANON / "plans.json"
    plans = load(plans_path, {"plans": []})
    for p in plans.get("plans", []):
        add(p.get("url"), p, "archived", "plans", p.get("from"), plans_path, plans)
    # Addresses known only as deleted (archive_misses.json): file under financial or meetings.
    for url in load(ROOT / "data" / "runtime" / "archive_misses.json", []):
        out.setdefault(key(urlparse(url).path), []).append(
            {"url": url, "holder": None, "field": None, "category": "financial" if FIN_RE.search(url) else "recovered",
             "year": int(m.group(1)) if (m := re.search(r"/uploads/(20\d\d)/", url)) else None,
             "file": None, "payload": None})
    return out


def main() -> None:
    files = [Path(a) for a in sys.argv[1:]] or sorted(p for p in INBOX.glob("*") if p.suffix.lower() == ".pdf")  # README.md stays
    if not files:
        print("  inbox empty")
        return
    index = slots()
    log = load(LOG, [])
    dirty: Dict[Path, Any] = {}
    fin_path = CANON / "financials.json"
    for f in files:
        data = f.read_bytes()
        if not data[:5].startswith(b"%PDF"):
            print(f"  {f.name}: not a PDF, left in place")
            continue
        places = index.get(key(f.name), [])
        # One copy per document, wherever it's referenced: an existing copy
        # if any place already has one, else one new file.
        filed_at = next((s["holder"][s["field"]] for s in places
                         if s["holder"] is not None and s["holder"].get(s["field"])), None)
        if places and not filed_at:
            first = places[0]
            year = int(m.group(1)) if (m := re.search(r"/uploads/(20\d\d)/", first["url"])) else first["year"]
            filed_at = store_bytes_in_repo(first["url"], "financial" if FIN_RE.search(f.name) else first["category"],
                                           year, data)
        for s in places:
            if not filed_at:
                break
            if s["holder"] is not None and s["holder"].get(s["field"]):
                continue
            got = filed_at
            if s["holder"] is not None:
                s["holder"][s["field"]] = got
                s["holder"].pop("gone", None)
                s["holder"]["provided"] = TODAY
                dirty[s["file"]] = s["payload"]
            print(f"  {f.name}: filed for {s['url']}")
        if not places:
            if FIN_RE.search(f.name):
                year = int(m.group(1)) if (m := re.search(r"(20[12]\d)", f.name)) else None
                pseudo = f"provided:{key(f.name)}"           # no known township address
                got = store_bytes_in_repo(pseudo, "financial", year, data, filename=key(f.name))
                fin = dirty.get(fin_path) or load(fin_path, {"documents": []})
                if got and not any(d.get("archived") == got for d in fin.setdefault("documents", [])):
                    fin["documents"].append({"url": "", "title": re.sub(r"[-_]+", " ", Path(key(f.name)).stem),
                                             "year": year, "archived": got, "provided": TODAY,
                                             "first_seen": TODAY})
                    dirty[fin_path] = fin
                filed_at = got
                print(f"  {f.name}: no township address known; kept as a financial document")
            else:
                print(f"  {f.name}: no match; left in inbox/ for a person to look at")
        log.append({"date": TODAY, "file": f.name, "filed": filed_at, "matches": len(places)})
        if filed_at and f.parent.resolve() == INBOX.resolve():
            f.unlink()
    for path, payload in dirty.items():
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + ("\n" if path.name in ("financials.json", "plans.json") else ""),
                        encoding="utf-8")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(log[-500:], indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
