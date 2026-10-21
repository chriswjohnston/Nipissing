#!/usr/bin/env python3
"""
bylaw_sources.py — the township website is the primary source for each
by-law's document; the agenda-package extract (package_extract.py) is the
fallback.

For every by-law number found on the site (inventory_bylaws.survey/match):
  * the main document is the township's own PDF, chosen in this order:
      linked from the By-Laws & Enforcement page > linked from any page >
      filename leading with the number > newest upload;
    WordPress duplicates ("…-1.pdf") of the same file are ignored;
  * other genuinely different files are kept as extra documents, labelled
    (township consolidation, map, earlier upload);
  * by-laws on the site but missing from the archive are added (2020-20
    Zoning, 2017-04 Building, zoning amendments …).
If the main document changes, the previous one (and its archived copy) is
kept as an extra document. Nothing is ever removed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

BYLAWS_PAGE_TITLE = re.compile(r"by-?laws?\s*&\s*enforcement", re.I)


def _name(url: str) -> str:
    return Path(unquote(url)).name


def _base(url: str) -> str:
    """Filename without WordPress's duplicate suffix: 2022-42-Dog-1.pdf -> 2022-42-Dog."""
    return re.sub(r"-\d$", "", Path(unquote(url)).stem).lower()


def _rank(num: str, f: Dict[str, Any]):
    pages = [p.split(" | ")[0] for p in (f.get("found_on") or [])]
    return (
        any(BYLAWS_PAGE_TITLE.search(p) for p in pages),
        bool(pages),
        _name(f["url"]).startswith(num),
        "map-only" not in _name(f["url"]).lower() and "consolidated" not in _name(f["url"]).lower(),
        f.get("uploaded") or "",
    )


def _label(f: Dict[str, Any]) -> str:
    n = _name(f["url"]).lower()
    if "consolidated" in n:
        return "Township consolidation"
    if "map" in n:
        return "Map"
    up = f.get("uploaded")
    return f"Earlier upload{f' ({up})' if up else ''}: {_name(f['url'])}"


def _title_for_new(num: str, files: List[Dict[str, Any]]) -> str:
    for f in files:
        for t in (f.get("link_texts") or []) + [f.get("title") or ""]:
            t = re.sub(r"\s+", " ", t).strip(" -–")
            if t and t.lower() not in (num, f"{num}.pdf") and len(t) > len(num) + 3:
                return t
    return f"By-Law {num}"


def apply(by_number: Dict[str, List[Dict[str, Any]]], bylaws: List[Dict[str, Any]]) -> Dict[str, int]:
    stats = {"primary_set": 0, "primary_changed": 0, "extras_added": 0, "added": 0}
    index = {b.get("number"): b for b in bylaws}

    for num, files in sorted(by_number.items()):
        # one file per distinct document (drop WordPress -1/-2 duplicates)
        seen, distinct = set(), []
        for f in sorted(files, key=lambda f: _rank(num, f), reverse=True):
            if _base(f["url"]) in seen:
                continue
            seen.add(_base(f["url"]))
            distinct.append(f)
        main, rest = distinct[0], distinct[1:]

        b = index.get(num)
        if b is None:
            m = re.match(r"(\d{4})-", num)
            b = {"number": num, "year": int(m.group(1)) if m else None,
                 "title": _title_for_new(num, [main] + rest), "status": "approved",
                 "source": "township_site", "date_passed": None, "votes": None,
                 "meeting_date": None, "pdf_url": None}
            bylaws.append(b)
            index[num] = b
            stats["added"] += 1
            print(f"  added {num}: {b['title']}")

        live_bases = {_base(f["url"]) for f in distinct}
        old = b.get("pdf_url") or ""
        if old != main["url"]:
            # The same file re-uploaded elsewhere on the site is not a separate
            # version; only a genuinely different old document is kept.
            if old.startswith("http") and _base(old) not in live_bases:
                # keep the previous township document and its archived copy
                kind = "Earlier township consolidation" if "consolidated" in old.lower() else "Earlier version"
                prev = {"label": f"{kind}: {_name(old)}", "url": old}
                if b.get("pdf_url_archived"):
                    prev["archived"] = b["pdf_url_archived"]
                b.setdefault("extra_docs", []).append(prev)
                stats["primary_changed"] += 1
            b["pdf_url"] = main["url"]
            b.pop("pdf_url_archived", None)
            stats["primary_set"] += 1
        b["pdf_source"] = "township_site"
        pages = [p.split(" | ") for p in (main.get("found_on") or [])]
        if pages and not b.get("page_url"):
            b["page_url"] = pages[0][1] if len(pages[0]) > 1 else None

        have = {d.get("url") for d in b.get("extra_docs") or []} | {b.get("pdf_url")}
        have_bases = {_base(u) for u in have if u}
        for f in rest:
            if f["url"] not in have and _base(f["url"]) not in have_bases:
                b.setdefault("extra_docs", []).append({"label": _label(f), "url": f["url"]})
                stats["extras_added"] += 1
    return stats
