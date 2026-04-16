#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests
from pdfminer.high_level import extract_text

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "canonical"

MEETINGS_FILE = DATA / "meetings.json"
SUMMARIES_FILE = DATA / "summaries.json"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-3-5-sonnet-latest"

HEADERS = {
    "User-Agent": "NipissingPublicRecords/1.0"
}

TODAY = date.today().isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def slug_from_display_date(display_date: str) -> str:
    """
    'Mar 17, 2026' -> 'march-17-2026'
    """
    m = re.match(r"^([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4})$", (display_date or "").strip())
    if not m:
        return ""

    month_map = {
        "jan": "january",
        "feb": "february",
        "mar": "march",
        "apr": "april",
        "may": "may",
        "jun": "june",
        "jul": "july",
        "aug": "august",
        "sep": "september",
        "oct": "october",
        "nov": "november",
        "dec": "december",
    }
    month = month_map.get(m.group(1).lower()[:3], m.group(1).lower())
    day = str(int(m.group(2)))
    year = m.group(3)
    return f"{month}-{day}-{year}"


def summary_key_for_meeting(meeting: dict[str, Any]) -> str | None:
    year = str(meeting.get("year") or str(meeting.get("date", ""))[:4]).strip()
    display_date = (meeting.get("display_date") or "").strip()

    if not year or not display_date:
        return None

    slug = slug_from_display_date(display_date)
    if not slug:
        return None

    return f"{year}/{slug}"


def should_summarize(meeting: dict[str, Any], summaries: dict[str, str]) -> tuple[bool, str]:
    if meeting.get("cancelled"):
        return False, "cancelled"

    meeting_date = str(meeting.get("date", ""))
    if meeting_date and meeting_date > TODAY:
        return False, "future"

    if not meeting.get("minutes_url"):
        return False, "no minutes"

    key = summary_key_for_meeting(meeting)
    if not key:
        return False, "no summary key"

    existing = summaries.get(key)
    if existing and str(existing).strip():
        return False, "already summarized"

    return True, key


def fetch_pdf_text(url: str) -> str:
    if not url.lower().endswith(".pdf"):
        raise RuntimeError("Not a PDF URL")

    r = requests.get(url, headers=HEADERS, timeout=120)
    r.raise_for_status()

    pdf_bytes = io.BytesIO(r.content)
    text = extract_text(pdf_bytes) or ""
    return text.strip()
    r = requests.get(url, headers=HEADERS, timeout=120)
    r.raise_for_status()

    pdf_bytes = io.BytesIO(r.content)
    text = extract_text(pdf_bytes) or ""
    return text.strip()


def clean_source_text(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def looks_unusable(text: str) -> bool:
    if not text or len(text) < 1200:
        return True

    lower = text.lower()
    bad_markers = [
        "obj",
        "endobj",
        "stream",
        "endstream",
        "xref",
        "/type /page",
    ]
    score = sum(1 for marker in bad_markers if marker in lower)
    return score >= 3


def build_prompt(meeting: dict[str, Any], source_text: str) -> str:
    meeting_label = meeting.get("display_date") or meeting.get("date") or "Unknown date"
    meeting_type = meeting.get("meeting_type") or "Regular"

    return f"""
You are writing a public-facing plain-language summary of a Township council meeting.

Meeting:
- Date: {meeting_label}
- Type: {meeting_type}
- Municipality: Township of Nipissing, Ontario

Instructions:
- Write in clear plain language for residents
- Focus on decisions, votes, spending, by-laws, major projects, and public-interest items
- Do not invent facts
- Do not mention uncertainty unless the minutes truly are unclear
- Use markdown
- Keep it concise but useful
- Use this exact structure:

# Nipissing Township Council Meeting Summary
**{meeting_label}**

## Key Decisions
- 3 to 6 bullet points

## Main Topics
- 2 to 5 bullet points or short paragraphs

## Notable Items
- short plain-language notes if there are any important amounts, deadlines, project approvals, staffing, procurement, taxation, or public service impacts

If there is a clear next regular meeting date in the minutes, add:
---
*Next regular Council meeting: ...*

Source minutes text:
{source_text[:120000]}
""".strip()


def call_anthropic(prompt: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    payload = {
        "model": "claude-3-5-sonnet-latest",
        "max_tokens": 1200,
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt}
                ]
            }
        ],
    }

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=payload,
        timeout=180,
    )

    if r.status_code != 200:
        print("Anthropic error:", r.status_code, r.text)
        r.raise_for_status()

    data = r.json()

    parts = data.get("content", [])
    text_parts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    out = "\n".join(text_parts).strip()

    if not out:
        raise RuntimeError("Anthropic returned no text content")

    return out


def main() -> None:
    meetings = load_json(MEETINGS_FILE, {"meetings": []})
    summaries = load_json(SUMMARIES_FILE, {})

    if isinstance(meetings, list):
        meetings = {"meetings": meetings}
    if not isinstance(summaries, dict):
        summaries = {}

    candidates: list[tuple[dict[str, Any], str]] = []
    for meeting in meetings.get("meetings", []):
        ok, detail = should_summarize(meeting, summaries)
        if ok:
            candidates.append((meeting, detail))

    print(f"Meetings loaded: {len(meetings.get('meetings', []))}")
    print(f"Existing summaries: {len(summaries)}")
    print(f"Meetings missing summaries with minutes posted: {len(candidates)}")

    if not candidates:
        print("Nothing to do.")
        return

    generated = 0
    skipped = 0

    for meeting, key in candidates:
        label = meeting.get("display_date") or meeting.get("date")
        minutes_url = meeting.get("minutes_url")

        print(f"\n--- Generating summary for {label} ---")
        print(f"Key: {key}")
        print(f"Minutes: {minutes_url}")

        try:
            text = fetch_pdf_text(minutes_url)
            text = clean_source_text(text)

            if looks_unusable(text):
                print("Skipped: extracted text is too short or looks unusable")
                skipped += 1
                continue

            prompt = build_prompt(meeting, text)
            summary = call_anthropic(prompt)

            summaries[key] = summary
            generated += 1

            save_json(SUMMARIES_FILE, summaries)
            print("Saved summary")

            # be polite to rate limits
            time.sleep(1.5)

        except Exception as e:
            print(f"Failed for {label}: {e}")
            skipped += 1

    print("\nDone.")
    print(f"Generated: {generated}")
    print(f"Skipped/failed: {skipped}")
    print(f"Summaries total: {len(summaries)}")


if __name__ == "__main__":
    main()
