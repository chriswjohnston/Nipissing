#!/usr/bin/env python3
"""
inventory_bylaws.py — find every by-law PDF on the township website.

The township's by-laws page links only some by-laws; others sit on service
pages (cemetery, zoning, committee of adjustment) or only in the media
library. This sweeps the whole site and compares it with our archive.

Sources (WordPress REST API, public):
  * media library: every uploaded PDF (filename + title + upload date)
  * pages and posts: every PDF link in their content, with link text and the
    page it is on (context for "which by-law is this")

A file is tied to a by-law number when its filename, title or link text
holds one ("2020-52-Amend-2019-41…", "By-law No. 2024-12"). The first
number is the file's own; later ones are references (amends/repeals).

Writes data/runtime/bylaw_inventory.json and prints a summary. Read-only
toward canonical data: nothing in bylaws.json changes.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

import doc_sources as ds

ROOT = Path(__file__).resolve().parents[1]
BYLAWS_FILE = ROOT / "data" / "canonical" / "bylaws.json"
OUT = ROOT / "data" / "runtime" / "bylaw_inventory.json"
BASE = "https://nipissingtownship.com"

# YYYY-N(NN) not followed by -DD (dates) and not preceded by R (resolutions).
NUM_RE = re.compile(r"(?<![\dRr])((?:19|20)\d\d)\s*[-_–]\s*0*(\d{1,3})(?![\d])(?!\s*[-_]\s*\d{1,2}\b)")
BYLAW_WORD = re.compile(r"by[\s_-]*law|bylaw|\bBL\b", re.I)


def numbers_in(text: str) -> List[str]:
    t = unquote(text or "").replace("%20", " ")
    out = []
    for y, n in NUM_RE.findall(t):
        num = f"{int(y)}-{int(n):02d}"
        if num not in out:
            out.append(num)
    return out


def fetch_all(kind: str, fields: str) -> List[Dict[str, Any]]:
    """Every item of a WP REST collection (media/pages/posts)."""
    items: List[Dict[str, Any]] = []
    for page in range(1, 200):
        try:
            r = ds.polite_get(f"{BASE}/wp-json/wp/v2/{kind}", timeout=60,
                              params={"per_page": 100, "page": page, "_fields": fields})
        except Exception as e:
            print(f"  {kind} page {page} failed: {e}")
            break
        if r.status_code == 400:
            break
        if r.status_code != 200:
            print(f"  {kind} page {page}: HTTP {r.status_code}")
            break
        batch = r.json()
        if not batch:
            break
        items += batch
        total = int(r.headers.get("X-WP-TotalPages", "0") or 0)
        if total and page >= total:
            break
    print(f"  {kind}: {len(items)} item(s)")
    return items


def main() -> None:
    print("=" * 60)
    print("Nipissing inventory_bylaws.py")
    print("=" * 60)
    files: Dict[str, Dict[str, Any]] = {}     # url -> info

    def add(url: str, **info) -> None:
        url = url.replace("http://", "https://", 1).split("#")[0]
        f = files.setdefault(url, {"url": url, "found_on": [], "link_texts": []})
        for k, v in info.items():
            if k == "found_on" and v and v not in f["found_on"]:
                f["found_on"].append(v)
            elif k == "link_text" and v and v not in f["link_texts"]:
                f["link_texts"].append(v)
            elif k not in ("found_on", "link_text") and v and not f.get(k):
                f[k] = v

    for m in fetch_all("media", "id,date,source_url,mime_type,title"):
        if (m.get("mime_type") or "").endswith("pdf") or (m.get("source_url") or "").lower().endswith(".pdf"):
            add(m["source_url"], title=unescape((m.get("title") or {}).get("rendered", "")),
                uploaded=(m.get("date") or "")[:10], in_media_library=True)

    for kind in ("pages", "posts"):
        for p in fetch_all(kind, "id,link,title,content"):
            html = (p.get("content") or {}).get("rendered", "")
            page_title = unescape((p.get("title") or {}).get("rendered", ""))
            for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
                href = urljoin(BASE, a["href"])
                if ".pdf" not in href.lower():
                    continue
                add(href, found_on=f"{page_title} | {p.get('link')}",
                    link_text=" ".join(a.get_text(" ", strip=True).split()))

    archive = json.loads(BYLAWS_FILE.read_text(encoding="utf-8"))["bylaws"]
    arch = {b["number"]: b for b in archive}

    by_number: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in files.values():
        name = Path(unquote(f["url"])).name
        texts = [name, f.get("title", "")] + f["link_texts"]
        nums = []
        for t in texts:
            for n in numbers_in(t):
                if n not in nums:
                    nums.append(n)
        if not nums:
            continue
        # Count it as a by-law file when something says "by-law", or the
        # filename starts with the number ("2020-52-Amend-…"), or the number
        # is a by-law we already know.
        says_bylaw = any(BYLAW_WORD.search(t or "") for t in texts)
        leads = re.match(r"\s*(?:19|20)\d\d[-_ ]\d", name) is not None
        if not (says_bylaw or leads or nums[0] in arch):
            continue
        f["numbers"] = nums
        by_number[nums[0]].append(f)

    report: Dict[str, Any] = {"checked": date.today().isoformat(),
                              "site_pdfs_scanned": len(files), "bylaws": {}}
    for num in sorted(set(by_number) | set(arch)):
        a = arch.get(num, {})
        cands = by_number.get(num, [])
        report["bylaws"][num] = {
            "in_archive": num in arch,
            "archive_title": a.get("common_name") or a.get("title"),
            "archive_pdf": a.get("pdf_url"),
            "archive_source": a.get("source"),
            "site_files": [{k: c.get(k) for k in ("url", "title", "uploaded", "in_media_library",
                                                  "found_on", "link_texts", "numbers")}
                           for c in cands],
        }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    b = report["bylaws"]
    on_site = {n for n, v in b.items() if v["site_files"]}
    print(f"\nSite PDFs scanned: {len(files)}; by-law numbers with a site PDF: {len(on_site)}")
    print(f"  archive by-laws: {len(arch)}")
    print(f"  archive by-laws with a site PDF:            {len(on_site & set(arch))}")
    print(f"    …that the archive does not link yet:      "
          f"{sum(1 for n in on_site & set(arch) if not (arch[n].get('pdf_url') or '').startswith('http'))}")
    print(f"  archive by-laws with no site PDF:           {len(set(arch) - on_site)}")
    print(f"  site by-laws missing from the archive:      {len(on_site - set(arch))}")
    print(f"  by-laws with more than one site PDF:        "
          f"{sum(1 for n in on_site if len(b[n]['site_files']) > 1)}")
    for n in sorted(on_site - set(arch))[:40]:
        f = b[n]["site_files"][0]
        print(f"    new: {n}  {Path(unquote(f['url'])).name}")


if __name__ == "__main__":
    main()
