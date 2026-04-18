#!/usr/bin/env python3
"""
generate_summaries.py

Generate AI summaries for:
- council meetings in data/canonical/meetings.json
- board / committee meetings in data/canonical/boards.json

Rules:
- only summarize meetings from 2026 onward
- only summarize meetings that have minutes_url
- skip meetings that already have a summary
- write summaries directly back into canonical JSON
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests
from pdfminer.high_level import extract_text_to_fp
from pdfminer.layout import LAParams

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIR = ROOT / "data" / "canonical"

MEETINGS_FILE = CANONICAL_DIR / "meetings.json"
BOARDS_FILE = CANONICAL_DIR / "boards.json"

MIN_YEAR = 2026


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_pdf_text(url: str, max_chars: int = 120000) -> str:
    if not url or not url.lower().endswith(".pdf"):
        raise ValueError("Not a PDF URL")

    r = requests.get(url, timeout=120)
    r.raise_for_status()

    buf = io.BytesIO(r.content)
    out = io.StringIO()
    extract_text_to_fp(
        buf,
        out,
        laparams=LAParams(),
        output_type="text",
        codec="utf-8"
    )
    return out.getvalue()[:max_chars]


def clean_source_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def looks_unusable(text: str) -> bool:
    if not text:
        return True
    if len(text.strip()) < 800:
        return True
    bad_markers = [
        "No /Root object",
        "not really a pdf",
        "unable to extract",
        "corrupt",
    ]
    tl = text.lower()
    return any(marker.lower() in tl for marker in bad_markers)


def meeting_label(meeting: Dict[str, Any]) -> str:
    return meeting.get("display_date") or meeting.get("date") or "Unknown meeting"


def board_label(board_id: str) -> str:
    mapping = {
        "recreation": "Recreation Committee",
        "museum": "Museum Board",
        "cemetery": "Cemetery Committee",
    }
    return mapping.get(board_id, board_id.title())


def build_prompt(meeting: Dict[str, Any], source_text: str) -> str:
    body = meeting.get("body") or meeting.get("board_name") or "Council"
    date_text = meeting.get("display_date") or meeting.get("date") or ""

    return f"""
Summarize this Township of Nipissing {body} meeting in plain language for a public archive.

Meeting date: {date_text}
Meeting body: {body}

Return markdown using exactly this structure:

# {body} Meeting Summary

**{date_text}**

## Key Decisions
- 3 to 8 bullets
- include motions passed or defeated
- include major approvals, by-laws, spending, staffing, procurement, grants, public-service changes, project decisions

## Main Topics
- 2 to 6 bullets or short paragraphs
- what was discussed in practical plain language

## Notable Items
- 1 to 5 bullets
- include important deadlines, cost figures, implementation dates, next-step impacts, policy implications, or resident-facing changes

If there is a clear next regular meeting date in the minutes, add this at the end:
---
*Next regular meeting: ...*

Rules:
- plain language
- factual and neutral
- do not invent details
- keep it concise but useful
- if something was defeated, say so clearly
- prefer specific numbers and dates when present

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
        "model": "claude-haiku-4-5",
        "max_tokens": 1400,
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": prompt
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


# ---------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------

def should_summarize(meeting: Dict[str, Any]) -> Tuple[bool, str]:
    year = meeting.get("year")
    if not isinstance(year, int) or year < MIN_YEAR:
        return False, "before minimum year"

    if not meeting.get("minutes_url"):
        return False, "no minutes_url"

    if meeting.get("summary"):
        return False, "already has summary"

    if meeting.get("cancelled"):
        return False, "cancelled"

    return True, "ok"


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    meetings_payload = load_json(MEETINGS_FILE, {"meetings": []})
    boards_payload = load_json(BOARDS_FILE, {"boards": []})

    if isinstance(meetings_payload, list):
        meetings_payload = {"meetings": meetings_payload}
    if not isinstance(meetings_payload, dict):
        meetings_payload = {"meetings": []}

    if not isinstance(boards_payload, dict):
        boards_payload = {"boards": []}

    council_meetings = meetings_payload.get("meetings", [])
    boards = boards_payload.get("boards", [])

    candidates: List[Tuple[str, Dict[str, Any], Tuple[int, ...]]] = []

    # council meetings
    for idx, meeting in enumerate(council_meetings):
        ok, _ = should_summarize(meeting)
        if ok:
            candidates.append(("council", meeting, (idx,)))

    # board meetings
    for b_idx, board in enumerate(boards):
        meetings = board.get("meetings", [])
        for m_idx, meeting in enumerate(meetings):
            # normalize body fields for prompt/UI consistency
            if not meeting.get("body"):
                meeting["body"] = board.get("name") or board_label(board.get("id", "board"))
            if not meeting.get("board_name"):
                meeting["board_name"] = board.get("name") or board_label(board.get("id", "board"))
            if not meeting.get("body_id"):
                meeting["body_id"] = board.get("id")

            ok, _ = should_summarize(meeting)
            if ok:
                candidates.append(("board", meeting, (b_idx, m_idx)))

    print(f"Council meetings loaded: {len(council_meetings)}")
    print(f"Boards loaded: {len(boards)}")
    print(f"Meetings missing summaries with minutes posted: {len(candidates)}")

    if not candidates:
        print("Nothing to do.")
        return

    generated = 0
    skipped = 0

    for kind, meeting, pointer in candidates:
        label = meeting_label(meeting)
        minutes_url = meeting.get("minutes_url")

        print(f"\n--- Generating summary for {label} ---")
        print(f"Type: {kind}")
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

            if kind == "council":
                meetings_payload["meetings"][pointer[0]]["summary"] = summary
                save_json(MEETINGS_FILE, meetings_payload)
            else:
                b_idx, m_idx = pointer
                boards_payload["boards"][b_idx]["meetings"][m_idx]["summary"] = summary
                save_json(BOARDS_FILE, boards_payload)

            generated += 1
            print("Saved summary")

            time.sleep(1.5)

        except Exception as e:
            print(f"Failed for {label}: {e}")
            skipped += 1

    print("\nDone.")
    print(f"Generated: {generated}")
    print(f"Skipped/failed: {skipped}")


if __name__ == "__main__":
    main()
