#!/usr/bin/env python3
"""
archive.py — shared document mirror for the Nipissing public records repo.

The Township periodically deletes agendas, minutes and agenda packages from its
live site. This module mirrors those PDFs so they survive deletion, and records a
durable URL alongside the original (live) township URL in canonical data. The
frontend can fall back to the mirror whenever the live link 404s.

Two storage backends, chosen by file size/type:

  * Small documents (minutes, agendas, misc) are committed into
    docs/files/archive/ as normal files. GitHub Pages serves these directly, so
    they are reachable at https://nipissing.ca/files/archive/...
    Git LFS is deliberately NOT used: Pages cannot serve LFS-backed files (it
    serves the pointer text, not the PDF).

  * Agenda packages are large (often 5-50 MB). Committing them all would exceed
    the Pages 1 GB site limit and risk the 100 MB per-file block, so they are
    uploaded as GitHub Release assets instead (free, up to 2 GB each, stable
    public URLs, and they do not count against repo size). This needs a token:
    GITHUB_TOKEN in Actions (with `permissions: contents: write`), or a PAT in
    GH_PAT locally. With no token, packages are skipped and logged — nothing
    crashes, and the run still completes.

Guarantees:
  * Idempotent      — a document already mirrored is never re-fetched/re-uploaded.
  * Non-destructive — a mirrored file is never deleted, even if the township
                      later removes the source.
"""

from __future__ import annotations

import hashlib
import os
import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
ARCHIVE_DIR = DOCS_DIR / "files" / "archive"

# Content-policy gates (match the front-end cutoffs):
#   - nothing before the PDF era is mirrored
#   - agendas are only mirrored once they matter on the site (2026+)
ARCHIVE_MIN_YEAR = 2022
AGENDA_MIN_YEAR = 2026

# Tombstone of URLs that returned 404, so a deleted document is fetched once,
# recorded as gone, and never retried (this is what stops the 404 storm).
# Lives under data/runtime (committed, not the gitignored pdf_cache), so the
# memory persists across runs. Delete the file or an entry to retry.
MISSES_FILE = ROOT / "data" / "runtime" / "archive_misses.json"

TOWNSHIP_HOST = "nipissingtownship.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Hard ceiling for the in-repo path — stay safely under GitHub's 100 MB block.
INREPO_MAX_BYTES = 90 * 1024 * 1024

GITHUB_API = "https://api.github.com"
RELEASE_TAG = "document-archive"  # one rolling release holds all mirrored packages


# ── filename / detection helpers ──────────────────────────────────────────────

def _safe_name(url: str) -> str:
    """Stable, collision-resistant filename derived from the URL."""
    base = Path(unquote(urlparse(url).path)).name or "document.pdf"
    base = re.sub(r"[^\w\-.]", "_", base)
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{base[:-4]}__{digest}.pdf"


def is_township_pdf(url: Optional[str]) -> bool:
    """True only for absolute township-hosted PDFs (skips local/relative paths)."""
    if not url:
        return False
    p = urlparse(url)
    return (
        p.scheme in ("http", "https")
        and TOWNSHIP_HOST in p.netloc
        and p.path.lower().endswith(".pdf")
    )


def _load_misses() -> set:
    try:
        return set(json.loads(MISSES_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_misses() -> None:
    MISSES_FILE.parent.mkdir(parents=True, exist_ok=True)
    MISSES_FILE.write_text(json.dumps(sorted(_MISSES), indent=2), encoding="utf-8")


_MISSES = _load_misses()


def _download(url: str) -> Optional[bytes]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=120, allow_redirects=True)
        if r.status_code == 404:
            # Gone for good — record it so we never retry this URL again.
            print(f"  archive: 404, tombstoned (won't retry) {url}")
            _MISSES.add(url)
            _save_misses()
            return None
        r.raise_for_status()
        data = r.content
        if not data[:5].startswith(b"%PDF"):
            print(f"  archive: not a PDF, skipping {url}")
            return None
        return data
    except Exception as e:
        # Transient (timeout/5xx): do NOT tombstone, so it can retry next run.
        print(f"  archive: download failed {url}: {e}")
        return None


# ── in-repo backend (served by GitHub Pages) ──────────────────────────────────

def store_bytes_in_repo(url: str, category: str, year, data: bytes) -> Optional[str]:
    """Archive already-in-hand PDF bytes into the in-repo mirror.
    Used by both the live archiver and the offline cache backfill."""
    if not data or not data[:5].startswith(b"%PDF"):
        return None
    bucket = str(year) if year else "undated"
    out_dir = ARCHIVE_DIR / category / bucket
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / _safe_name(url)
    rel = target.relative_to(DOCS_DIR).as_posix()

    if target.exists() and target.stat().st_size > 0:
        return rel  # already mirrored — never overwrite, never delete
    if len(data) > INREPO_MAX_BYTES:
        print(f"  archive: {len(data)//1024//1024} MB too big for in-repo "
              f"(use a package/Releases path): {url}")
        return None
    target.write_bytes(data)
    print(f"  archive: committed docs/{rel} ({len(data)//1024} KB)")
    return rel


def _archive_in_repo(url: str, category: str, year) -> Optional[str]:
    bucket = str(year) if year else "undated"
    target = ARCHIVE_DIR / category / bucket / _safe_name(url)
    if target.exists() and target.stat().st_size > 0:
        return target.relative_to(DOCS_DIR).as_posix()  # skip download
    data = _download(url)
    if data is None:
        return None
    return store_bytes_in_repo(url, category, year, data)


# ── GitHub Releases backend (for large packages) ──────────────────────────────

_release_cache: dict = {}


def _gh_repo() -> Optional[str]:
    return os.environ.get("GITHUB_REPOSITORY")  # "owner/repo", auto-set in Actions


def _gh_token() -> Optional[str]:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_PAT")


def _get_or_create_release(repo: str, token: str) -> Optional[dict]:
    if RELEASE_TAG in _release_cache:
        return _release_cache[RELEASE_TAG]
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    r = requests.get(f"{GITHUB_API}/repos/{repo}/releases/tags/{RELEASE_TAG}", headers=h)
    if r.status_code == 200:
        rel = r.json()
    elif r.status_code == 404:
        r = requests.post(f"{GITHUB_API}/repos/{repo}/releases", headers=h, json={
            "tag_name": RELEASE_TAG,
            "name": "Document Archive",
            "body": "Agenda packages preserved from the Township of Nipissing site.",
        })
        if r.status_code not in (200, 201):
            print(f"  archive: could not create release: {r.status_code} {r.text[:140]}")
            return None
        rel = r.json()
    else:
        print(f"  archive: release lookup failed: {r.status_code} {r.text[:140]}")
        return None
    _release_cache[RELEASE_TAG] = rel
    return rel


def store_bytes_to_release(url: str, data: bytes) -> Optional[str]:
    """Upload already-in-hand PDF bytes to the rolling Release as an asset.
    Used by both the live archiver and the offline cache backfill."""
    repo, token = _gh_repo(), _gh_token()
    if not (repo and token):
        print(f"  archive: no token/repo in env — skipping package {url}")
        return None
    rel = _get_or_create_release(repo, token)
    if not rel:
        return None

    asset_name = _safe_name(url)
    for asset in rel.get("assets", []):
        if asset["name"] == asset_name:
            return asset["browser_download_url"]  # already uploaded

    if not data or not data[:5].startswith(b"%PDF"):
        return None

    upload_url = rel["upload_url"].split("{")[0]
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/pdf"}
    r = requests.post(f"{upload_url}?name={asset_name}", headers=h, data=data)
    if r.status_code in (200, 201):
        dl = r.json()["browser_download_url"]
        rel.setdefault("assets", []).append(
            {"name": asset_name, "browser_download_url": dl}
        )
        print(f"  archive: uploaded package → Releases ({len(data)//1024//1024} MB)")
        return dl
    print(f"  archive: package upload failed: {r.status_code} {r.text[:140]}")
    return None


def _archive_to_release(url: str) -> Optional[str]:
    # Cheap existence check before downloading the (large) package.
    repo, token = _gh_repo(), _gh_token()
    if not (repo and token):
        print(f"  archive: no token/repo in env — skipping package {url}")
        return None
    rel = _get_or_create_release(repo, token)
    if rel:
        asset_name = _safe_name(url)
        for asset in rel.get("assets", []):
            if asset["name"] == asset_name:
                return asset["browser_download_url"]

    data = _download(url)
    if data is None:
        return None
    return store_bytes_to_release(url, data)


# ── public API ────────────────────────────────────────────────────────────────

def mirror(url: Optional[str], category: str, year=None, *, is_package: bool = False
           ) -> Optional[str]:
    """Mirror one township PDF; return a durable URL/path, or None.

    Small docs → committed under docs/ (repo-relative path, Pages-served).
    Packages   → GitHub Release asset (absolute URL).
    """
    if not is_township_pdf(url):
        return None
    if url in _MISSES:
        return None  # known-gone (404 tombstone) — don't retry
    return _archive_to_release(url) if is_package else _archive_in_repo(url, category, year)


def mirror_record(rec: dict, category: str) -> dict:
    """Stamp archived_* fields onto a meeting/board record in place.

    Re-uses an existing mirror if its file is still present, so reruns are cheap
    and nothing already saved is touched.
    """
    year = rec.get("year") or 0
    if year and year < ARCHIVE_MIN_YEAR:
        return rec  # pre-PDF era — not mirrored at all

    pairs = [
        ("agenda_url",  "agenda_archived",  False, AGENDA_MIN_YEAR),
        ("minutes_url", "minutes_archived", False, ARCHIVE_MIN_YEAR),
        ("package_url", "package_archived", True,  ARCHIVE_MIN_YEAR),
    ]
    for src, dst, is_pkg, min_year in pairs:
        if year and year < min_year:
            continue  # e.g. agendas before 2026 are intentionally skipped
        if rec.get(dst):
            # packages live in Releases (can't stat); in-repo docs we verify exist
            if is_pkg or (DOCS_DIR / rec[dst]).exists():
                continue
        got = mirror(rec.get(src), category, year, is_package=is_pkg)
        if got:
            rec[dst] = got

    for doc in rec.get("extra_docs", []) or []:
        if doc.get("archived"):
            continue
        got = mirror(doc.get("url"), category, year, is_package=False)
        if got:
            doc["archived"] = got
    return rec
