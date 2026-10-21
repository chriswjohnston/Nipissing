#!/usr/bin/env python3
"""
doc_sources.py — shared helpers for finding documents the township no
longer links to.

  * WordPress media index (wp-json/wp/v2/media): lists every file still on
    the township server, including ones removed from all pages.
  * Wayback Machine CDX index: lists every archived copy of a URL pattern,
    which is the only route to files that were actually deleted.
  * Date parsing for filenames and PDF text ("March-19-2024", "13 May 2025",
    "Sept 21, 2026").

Used by update_adjustment.py; written to be reusable for council and by-law
recovery later.
"""

from __future__ import annotations

import re
import time
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

WP_BASE = "https://nipissingtownship.com"

_MONTH = (r"(january|february|march|april|may|june|july|august|september|october|"
          r"november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)")
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
MDY_RE = re.compile(_MONTH + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)
DMY_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r"\.?,?\s+(\d{4})\b", re.I)


def _norm(text: str) -> str:
    """Filenames use -, _ and %20 as spaces."""
    t = (text or "").replace("%20", " ")
    t = re.sub(r"[-_/]+", " ", t)
    return re.sub(r"\s+", " ", t)


def _mk(y: str, mon: str, d: str) -> Optional[date]:
    try:
        dt = date(int(y), MONTHS[mon.lower()[:3]], int(d))
    except (ValueError, KeyError):
        return None
    return dt if 2010 <= dt.year <= date.today().year + 2 else None


def find_dates(text: str) -> List[date]:
    """All plausible dates in order of appearance (MDY and DMY)."""
    t = _norm(text)
    found = []
    for m in MDY_RE.finditer(t):
        dt = _mk(m.group(3), m.group(1), m.group(2))
        if dt:
            found.append((m.start(), dt))
    for m in DMY_RE.finditer(t):
        dt = _mk(m.group(3), m.group(2), m.group(1))
        if dt:
            found.append((m.start(), dt))
    found.sort(key=lambda x: x[0])
    out: List[date] = []
    for _, dt in found:
        if dt not in out:
            out.append(dt)
    return out


def find_date(text: str) -> Optional[date]:
    ds = find_dates(text)
    return ds[0] if ds else None


def filename_of(url: str) -> str:
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


def pdf_text(data: bytes, pages: int = 1) -> str:
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=data, filetype="pdf")
        return "\n".join(doc[i].get_text() for i in range(min(pages, len(doc))))
    except Exception as e:
        print(f"  pdf text failed: {e}")
        return ""


def download_pdf(url: str, timeout: int = 120) -> Optional[bytes]:
    """PDF bytes, or None (404 / not a PDF / transient error)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code == 404:
            return None
        r.raise_for_status()
    except Exception as e:
        print(f"  download failed {url}: {e}")
        return None
    return r.content if r.content[:5].startswith(b"%PDF") else None


# ── WordPress media index ─────────────────────────────────────────────────────

def wp_media_search(term: str, base: str = WP_BASE, max_pages: int = 30) -> List[Dict[str, Any]]:
    """Every media item matching `term`: {id, date, url, mime, size, title}."""
    out: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        try:
            r = requests.get(f"{base}/wp-json/wp/v2/media", headers=HEADERS, timeout=60,
                             params={"search": term, "per_page": 100, "page": page,
                                     "_fields": "id,date,source_url,mime_type,title,media_details"})
        except Exception as e:
            print(f"  media index failed ({term} p{page}): {e}")
            break
        if r.status_code == 400:     # past the last page
            break
        if r.status_code != 200:
            print(f"  media index HTTP {r.status_code} ({term} p{page})")
            break
        items = r.json()
        if not items:
            break
        for it in items:
            md = it.get("media_details") or {}
            out.append({
                "id": it.get("id"),
                "date": (it.get("date") or "")[:10],
                "url": (it.get("source_url") or "").replace("http://", "https://", 1),
                "mime": it.get("mime_type"),
                "size": md.get("filesize"),
                "title": (it.get("title") or {}).get("rendered", ""),
            })
        total = int(r.headers.get("X-WP-TotalPages", "0") or 0)
        if total and page >= total:
            break
    return out


# ── Wayback Machine ───────────────────────────────────────────────────────────

def wayback_files(url_prefix: str, name_regex: str) -> List[Dict[str, str]]:
    """Archived PDFs under url_prefix whose URL matches name_regex.
    One entry per original URL (newest good capture): {original, timestamp}."""
    params = {
        "url": url_prefix,
        "matchType": "prefix",
        "output": "json",
        "fl": "original,timestamp,statuscode,mimetype",
        "filter": ["statuscode:200", f"original:{name_regex}"],
        "collapse": "urlkey",
    }
    for attempt in range(3):
        try:
            r = requests.get("https://web.archive.org/cdx/search/cdx", params=params,
                             headers=HEADERS, timeout=120)
            r.raise_for_status()
            rows = r.json()
            break
        except Exception as e:
            print(f"  wayback CDX attempt {attempt + 1} failed: {e}")
            time.sleep(10)
    else:
        return []
    out = []
    for row in rows[1:] if rows else []:
        original, ts = row[0], row[1]
        if not original.lower().split("?")[0].endswith(".pdf"):
            continue
        out.append({"original": original.replace("http://", "https://", 1)
                    .replace(":80/", "/"), "timestamp": ts})
    return out


def wayback_pages(domain: str, url_regex: str, limit: int = 80) -> List[Dict[str, str]]:
    """HTML captures of pages on `domain` whose URL matches url_regex, at most
    one per page per month: [{original, timestamp}], oldest first."""
    params = {
        "url": domain, "matchType": "domain", "output": "json",
        "fl": "original,timestamp",
        "filter": ["statuscode:200", "mimetype:text/html", f"original:{url_regex}"],
        "collapse": "timestamp:6",
    }
    try:
        r = requests.get("https://web.archive.org/cdx/search/cdx", params=params,
                         headers=HEADERS, timeout=180)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"  wayback page index failed: {e}")
        return []
    out = [{"original": row[0], "timestamp": row[1]} for row in (rows[1:] if rows else [])]
    if len(out) > limit:            # spread the sample across the whole period
        step = len(out) / limit
        out = [out[int(i * step)] for i in range(limit)]
    return out


def wayback_page_html(original: str, timestamp: str) -> Optional[str]:
    try:
        r = requests.get(f"https://web.archive.org/web/{timestamp}id_/{original}",
                         headers=HEADERS, timeout=120)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  wayback page fetch failed {original} @{timestamp}: {e}")
        return None


def wayback_download(original: str, timestamp: str) -> Optional[bytes]:
    """The archived bytes (id_ = raw file, no Wayback toolbar)."""
    return download_pdf(f"https://web.archive.org/web/{timestamp}id_/{original}", timeout=180)


def unique(seq: Iterable[Any]) -> List[Any]:
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
