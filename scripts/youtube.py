#!/usr/bin/env python3
"""
Shared YouTube matcher for the Township channel.

Every run reads the channel RSS feed (only the ~15 most recent uploads) and
merges each entry into a permanent cache, data/runtime/youtube_cache.json,
keyed by video ID. Both update_meetings.py and update_boards.py read from it,
so a video is never lost just because it scrolled out of the feed.

Each cached video records:
  url, title, published (UTC ISO), local_date (America/Toronto YYYY-MM-DD),
  title_date (date parsed from the title, or null), body (see classify_body),
  special (True if "special" is in the title)

Two ways a video gets matched to a meeting:
  1. Labelled: body known from the title, and title_date equals the meeting date.
  2. Unlabelled fallback (boards only): titles like
     "Township of Nipissing's Zoom Meeting" carry no body or date, so they are
     matched on the date they streamed — but only when exactly one body that
     isn't council met that day and still lacks a video.
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

CHANNEL_ID = "UC2XSMZqRNHbwVppelfKcEXw"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
CACHE = Path("data/runtime/youtube_cache.json")
# Records when a full API backfill has completed, so later runs only read the
# newest page(s) instead of the whole channel history every time.
BACKFILL_STATE = Path("data/runtime/youtube_backfill.json")
_BACKFILL_DONE_THIS_RUN = False
LEGACY_CACHE = Path("data/runtime/youtube_videos.json")  # date-keyed, council only
LOCAL_TZ = ZoneInfo("America/Toronto")

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

# Title formats seen so far:
#   "Township of Nipissing Council Meeting Sept 01.2026"
#   "Township of Nipissing Recreation Committee Meeting Sept 14.2026"
#   "Nipissing Museum Board Meeting - September 2, 2026"
#   "Council Meeting June 16, 2026"
#   "Township of Nipissing's Zoom Meeting"   (no body, no date)
TITLE_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
    r"\s*(\d{1,2})(?:st|nd|rd|th)?\s*[,./-]?\s*(\d{4})\b",
    re.IGNORECASE,
)
TITLE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# Order matters: the first keyword found decides the body.
BODY_KEYWORDS = [
    ("museum", "museum"),
    ("recreation", "recreation"),
    ("cemetery", "cemetery"),
    # Not tracked on the site, but labelling it keeps these videos out of
    # the untitled-stream fallback.
    ("adjustment", "adjustment"),
    # Police board (PNC OPP Detachment Board) — rarely streamed, but labelled
    # so it can never be picked up by the untitled-stream fallback.
    ("detachment", "pnc"),
    ("police", "pnc"),
]
NOT_COUNCIL = ("committee", "board", "adjustment", "conservation", "library", "planning")


def _load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def title_date(title: str) -> Optional[str]:
    """ISO date parsed from a video title, or None."""
    m = TITLE_DATE_RE.search(title or "")
    if m:
        try:
            return datetime(int(m.group(3)), MONTHS[m.group(1)[:3].lower()],
                            int(m.group(2))).strftime("%Y-%m-%d")
        except (KeyError, ValueError):
            return None
    m = TITLE_ISO_RE.search(title or "")
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)),
                            int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def classify_body(title: str) -> Optional[str]:
    """'council', 'museum', 'recreation', 'cemetery', or None if unlabelled."""
    tl = (title or "").lower()
    for kw, body in BODY_KEYWORDS:
        if kw in tl:
            return body
    if "council" in tl and not any(w in tl for w in NOT_COUNCIL):
        return "council"
    return None


def _local_date(published: str) -> Optional[str]:
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


def _video_id(url: str) -> str:
    m = re.search(r"[?&]v=([\w-]{6,})", url or "")
    return m.group(1) if m else (url or "")


def _import_legacy(cache: Dict[str, Dict[str, Any]]) -> None:
    """One-time import of the short-lived date-keyed council cache."""
    legacy = _load(LEGACY_CACHE, {})
    for iso, v in legacy.items():
        url = v.get("url", "")
        vid = _video_id(url)
        if vid and vid not in cache:
            cache[vid] = {
                "url": url,
                "title": v.get("title", ""),
                "published": None,
                "local_date": None,
                "title_date": iso,
                "body": "council",
                "special": bool(v.get("special")),
            }


def _record(title: str, url: str, published: Optional[str]) -> Dict[str, Any]:
    return {
        "url": url,
        "title": title,
        "published": published or None,
        "local_date": _local_date(published) if published else None,
        "title_date": title_date(title),
        "body": classify_body(title),
        "special": "special" in (title or "").lower(),
    }


def backfill_from_api(cache: Dict[str, Dict[str, Any]]) -> int:
    """
    Optional: page through the channel's whole upload history with the
    YouTube Data API (needs a free API key in the YOUTUBE_API_KEY secret).
    The RSS feed only ever shows the 15 newest uploads, so anything that
    scrolled off before the cache existed can only be recovered this way.
    Costs ~1 quota unit per 50 videos (daily free quota is 10,000).
    """
    global _BACKFILL_DONE_THIS_RUN
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key or _BACKFILL_DONE_THIS_RUN:
        return 0
    state = _load(BACKFILL_STATE, {})
    complete = bool(state.get("complete"))
    playlist = "UU" + CHANNEL_ID[2:]          # the channel's "uploads" playlist
    added, token, pages = 0, None, 0
    try:
        while pages < 40:
            params = {"part": "snippet,contentDetails", "playlistId": playlist,
                      "maxResults": 50, "key": key}
            if token:
                params["pageToken"] = token
            r = requests.get("https://www.googleapis.com/youtube/v3/playlistItems",
                             params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            pages += 1
            page_new = 0
            for item in data.get("items", []):
                sn = item.get("snippet", {})
                vid = (sn.get("resourceId") or {}).get("videoId")
                if not vid or vid in cache:
                    continue
                if sn.get("title") in ("Private video", "Deleted video"):
                    continue  # unplayable placeholders in the uploads playlist
                published = (item.get("contentDetails") or {}).get("videoPublishedAt") \
                    or sn.get("publishedAt")
                cache[vid] = _record(sn.get("title", ""),
                                     f"https://www.youtube.com/watch?v={vid}", published)
                added += 1
                page_new += 1
            token = data.get("nextPageToken")
            if not token:
                # Reached the oldest upload: the history is fully cached.
                if not complete:
                    _save(BACKFILL_STATE, {"complete": datetime.now(timezone.utc)
                                           .strftime("%Y-%m-%d")})
                break
            if complete and page_new == 0:
                break  # incremental mode: nothing new on this page
        _BACKFILL_DONE_THIS_RUN = True
        mode = "incremental" if complete else "full history"
        print(f"  API backfill ({mode}): {added} older video(s) added ({pages} page(s))")
    except Exception as ex:
        # Never log the URL: it contains the key.
        print(f"  Warning: YouTube API backfill failed ({type(ex).__name__})")
    return added


def refresh() -> Dict[str, Dict[str, Any]]:
    """Read the RSS feed, merge into the cache, save, and return the cache."""
    cache: Dict[str, Dict[str, Any]] = _load(CACHE, {})
    _import_legacy(cache)
    before = len(cache)
    print(f"YouTube: {before} cached video(s); reading RSS ...")

    try:
        resp = requests.get(RSS_URL, timeout=20)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        entries = root.findall("atom:entry", NS)
        print(f"  Feed returned {len(entries)} entries")

        for e in entries:
            title = (e.findtext("atom:title", default="", namespaces=NS) or "").strip()
            link = e.find("atom:link", NS)
            url = link.get("href", "") if link is not None else ""
            vid = e.findtext("yt:videoId", default="", namespaces=NS) or _video_id(url)
            published = e.findtext("atom:published", default="", namespaces=NS)
            if not vid or not url:
                continue

            rec = _record(title, url, published)
            new = vid not in cache
            # Refresh metadata (titles sometimes get edited after a stream);
            # the URL for a given video ID never changes.
            cache[vid] = rec
            tag = "+" if new else "="
            print(f"    {tag} [{rec['body'] or 'unlabelled'}] "
                  f"title_date={rec['title_date']} streamed={rec['local_date']}  {title}")

    except Exception as ex:
        # Transient: keep the cache, never wipe it.
        print(f"  Warning: could not read YouTube RSS: {ex}")

    backfill_from_api(cache)
    # Re-classify everything with the current rules (e.g. a new body keyword).
    for v in cache.values():
        if v.get("title"):            # legacy entries may lack a title
            v["body"] = classify_body(v["title"])
            v["title_date"] = title_date(v["title"]) or v.get("title_date")
    _save(CACHE, cache)
    print(f"  {len(cache)} cached ({len(cache) - before} new)")
    return cache


def labelled(cache: Dict[str, Dict[str, Any]], body: str) -> Dict[str, List[Dict[str, Any]]]:
    """{iso_date: [video, ...]} for videos whose title names this body and a date."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for v in cache.values():
        if v.get("body") != body:
            continue
        # "Township of Nipissing Special Council Meeting" has no date in the
        # title; the day it streamed is the meeting day.
        day = v.get("title_date") or v.get("local_date")
        if day:
            out.setdefault(day, []).append(v)
    return out


def unlabelled(cache: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """{local_stream_date: [video, ...]} for videos with no body in the title."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for v in cache.values():
        if v.get("body") is None and v.get("local_date"):
            out.setdefault(v["local_date"], []).append(v)
    return out


def attach_labelled(meetings: List[Dict[str, Any]], cache: Dict[str, Dict[str, Any]],
                    body: str) -> int:
    """
    Fill empty video_url on meetings from title-labelled videos for `body`.
    Never overwrites an existing link; skips cancelled meetings. When a Regular
    and a Special meeting share a date, matches on the 'special' flag.
    Returns the number of meetings filled.
    """
    by_date = labelled(cache, body)
    used = {m.get("video_url") for m in meetings if m.get("video_url")}
    filled = 0
    for m in meetings:
        if m.get("video_url") or m.get("cancelled"):
            continue
        candidates = [v for v in by_date.get(m.get("date"), []) if v["url"] not in used]
        if not candidates:
            continue
        same_day = sum(1 for x in meetings if x.get("date") == m.get("date"))
        if same_day > 1:
            want_special = m.get("meeting_type") == "Special"
            candidates = [v for v in candidates if bool(v.get("special")) == want_special]
        if not candidates:
            continue
        # Several uploads for one meeting (live stream + re-upload, or a
        # stream that restarted): use the earliest, keep them all in videos[].
        candidates.sort(key=lambda v: v.get("published") or "9999")
        m["video_url"] = candidates[0]["url"]
        if len(candidates) > 1:
            m["videos"] = [{"url": v["url"], "title": v.get("title", "")} for v in candidates]
        used.update(v["url"] for v in candidates)
        filled += 1
        extra = f" (+{len(candidates) - 1} more part/upload)" if len(candidates) > 1 else ""
        print(f"  video attached ({body}): {m['date']} {m.get('meeting_type', '')}{extra}")
    print(f"  {filled} {body} meeting(s) gained a video")
    return filled


def attach_unlabelled(meetings_by_body: Dict[str, List[Dict[str, Any]]],
                      cache: Dict[str, Dict[str, Any]],
                      council_dates: Optional[set] = None) -> int:
    """
    Fallback for untitled streams ("Township of Nipissing's Zoom Meeting").
    For each stream date, attach the video only when exactly ONE non-council
    body has a non-cancelled meeting that day with no video, exactly one
    unlabelled video streamed that day, and council did not also meet that day.
    Anything ambiguous is logged and left alone.
    """
    council_dates = council_dates or set()
    used = {m.get("video_url") for ms in meetings_by_body.values()
            for m in ms if m.get("video_url")}
    filled = 0
    for day, vids in sorted(unlabelled(cache).items()):
        vids = [v for v in vids if v["url"] not in used]
        if not vids:
            continue
        open_meetings = [(body, m) for body, ms in meetings_by_body.items()
                         for m in ms
                         if m.get("date") == day and not m.get("video_url")
                         and not m.get("cancelled")]
        if day in council_dates or len(vids) != 1 or len(open_meetings) != 1:
            if open_meetings or day in council_dates:
                print(f"  unlabelled video on {day} left unmatched "
                      f"({len(vids)} video(s), {len(open_meetings)} open meeting(s), "
                      f"council that day: {day in council_dates})")
            continue
        body, m = open_meetings[0]
        m["video_url"] = vids[0]["url"]
        used.add(m["video_url"])
        filled += 1
        print(f"  video attached ({body}, by stream date): {day}  [{vids[0]['title']}]")
    print(f"  {filled} meeting(s) gained a video from untitled streams")
    return filled
