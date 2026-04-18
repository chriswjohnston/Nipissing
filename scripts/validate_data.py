#!/usr/bin/env python3
"""
validate_data.py

Validation for canonical Nipissing public-records data.

Fails the workflow on:
- malformed JSON / missing files
- wrong top-level structure
- duplicate primary keys
- missing required identifiers
- clearly invalid internal references

Warns on:
- missing optional fields
- past meetings with no documents
- file paths that look local but do not exist
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse
from datetime import datetime, date

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIR = ROOT / "data" / "canonical"
DOCS_DIR = ROOT / "docs"

MEETINGS_FILE = CANONICAL_DIR / "meetings.json"
BYLAWS_FILE = CANONICAL_DIR / "bylaws.json"
RESOLUTIONS_FILE = CANONICAL_DIR / "resolutions.json"
BOARDS_FILE = CANONICAL_DIR / "boards.json"


errors: List[str] = []
warnings: List[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def load_json(path: Path) -> Any:
    if not path.exists():
        err(f"Missing file: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        err(f"Could not parse JSON: {path} ({e})")
        return None


def expect_dict(obj: Any, label: str) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        err(f"{label} must be a JSON object")
        return {}
    return obj


def expect_list(obj: Any, label: str) -> List[Any]:
    if not isinstance(obj, list):
        err(f"{label} must be a list")
        return []
    return obj


def is_url(value: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    if value.startswith(("http://", "https://")):
        return True
    return False


def is_docs_relative_path(value: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    return not value.startswith(("http://", "https://", "/"))


def validate_docs_reference(path_str: str, label: str) -> None:
    if is_docs_relative_path(path_str):
        abs_path = DOCS_DIR / path_str
        if not abs_path.exists():
            warn(f"{label} points to missing docs file: {path_str}")
    elif not is_url(path_str):
        warn(f"{label} is not a valid URL/path: {path_str}")


def parse_iso_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def dupes(values: List[Tuple], label: str) -> None:
    counts = Counter(values)
    repeated = [k for k, v in counts.items() if v > 1]
    if repeated:
        preview = ", ".join(str(x) for x in repeated[:10])
        err(f"Duplicate {label}: {preview}")


def validate_meetings(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    meetings = expect_list(payload.get("meetings"), "meetings.json -> meetings")

    keys = []
    today = date.today()

    for i, m in enumerate(meetings):
        if not isinstance(m, dict):
            err(f"meetings[{i}] must be an object")
            continue

        date_val = m.get("date")
        meeting_type = m.get("meeting_type", "Regular")

        if not date_val:
            err(f"meetings[{i}] missing date")
            continue

        if not parse_iso_date(date_val):
            err(f"meetings[{i}] has invalid date: {date_val}")

        keys.append((date_val, meeting_type))

        if not m.get("display_date"):
            warn(f"meeting {date_val} missing display_date")

        if not isinstance(m.get("year"), int):
            warn(f"meeting {date_val} has non-integer year")

        for field in ("agenda_url", "minutes_url", "package_url", "video_url"):
            value = m.get(field)
            if value:
                validate_docs_reference(value, f"meeting {date_val} field {field}")

        extra_docs = m.get("extra_docs", [])
        if extra_docs is None:
            extra_docs = []
        if not isinstance(extra_docs, list):
            err(f"meeting {date_val} extra_docs must be a list")
            extra_docs = []

        extra_keys = []
        for j, doc in enumerate(extra_docs):
            if not isinstance(doc, dict):
                err(f"meeting {date_val} extra_docs[{j}] must be an object")
                continue
            label = doc.get("label")
            url = doc.get("url")
            if not label:
                warn(f"meeting {date_val} extra_docs[{j}] missing label")
            if not url:
                err(f"meeting {date_val} extra_docs[{j}] missing url")
            else:
                validate_docs_reference(url, f"meeting {date_val} extra_docs[{j}]")
            extra_keys.append((label, url))

        dupes(extra_keys, f"extra_docs for meeting {date_val}")

        d = parse_iso_date(date_val)
        if d and d < today and not m.get("cancelled"):
            if not any(m.get(f) for f in ("agenda_url", "minutes_url", "package_url")):
                warn(f"past meeting {date_val} has no agenda/minutes/package links")

    dupes(keys, "meetings (date, meeting_type)")
    return meetings


def validate_bylaws(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    bylaws = expect_list(payload.get("bylaws"), "bylaws.json -> bylaws")
    numbers = []

    for i, b in enumerate(bylaws):
        if not isinstance(b, dict):
            err(f"bylaws[{i}] must be an object")
            continue

        number = b.get("number")
        if not number:
            err(f"bylaws[{i}] missing number")
            continue

        numbers.append(number)

        if not b.get("title"):
            warn(f"bylaw {number} missing title")

        if b.get("pdf_url"):
            validate_docs_reference(b["pdf_url"], f"bylaw {number} pdf_url")
        if b.get("page_url"):
            validate_docs_reference(b["page_url"], f"bylaw {number} page_url")
        if b.get("minutes_url"):
            validate_docs_reference(b["minutes_url"], f"bylaw {number} minutes_url")
        if b.get("agenda_package_url"):
            validate_docs_reference(b["agenda_package_url"], f"bylaw {number} agenda_package_url")

        if b.get("date_passed") and not parse_iso_date(b["date_passed"]):
            warn(f"bylaw {number} has invalid date_passed: {b['date_passed']}")

    dupes(numbers, "by-law numbers")
    return bylaws


def validate_resolutions(payload: Dict[str, Any], bylaw_numbers: set[str]) -> List[Dict[str, Any]]:
    resolutions = expect_list(payload.get("resolutions"), "resolutions.json -> resolutions")
    numbers = []

    for i, r in enumerate(resolutions):
        if not isinstance(r, dict):
            err(f"resolutions[{i}] must be an object")
            continue

        number = r.get("number")
        if not number:
            err(f"resolutions[{i}] missing number")
            continue

        numbers.append(number)

        if not r.get("meeting_date"):
            warn(f"resolution {number} missing meeting_date")
        elif not parse_iso_date(r["meeting_date"]):
            warn(f"resolution {number} has invalid meeting_date: {r['meeting_date']}")

        if not r.get("motion_text"):
            warn(f"resolution {number} missing motion_text")

        if r.get("minutes_url"):
            validate_docs_reference(r["minutes_url"], f"resolution {number} minutes_url")
        if r.get("agenda_package_url"):
            validate_docs_reference(r["agenda_package_url"], f"resolution {number} agenda_package_url")
        if r.get("pdf_url"):
            validate_docs_reference(r["pdf_url"], f"resolution {number} pdf_url")

        if r.get("is_bylaw") and r.get("bylaw_number"):
            if r["bylaw_number"] not in bylaw_numbers:
                warn(f"resolution {number} references missing by-law {r['bylaw_number']}")

    dupes(numbers, "resolution numbers")
    return resolutions


def validate_boards(payload: Dict[str, Any]) -> None:
    boards = expect_list(payload.get("boards"), "boards.json -> boards")

    board_ids = []
    all_board_keys = []

    for i, board in enumerate(boards):
        if not isinstance(board, dict):
            err(f"boards[{i}] must be an object")
            continue

        board_id = board.get("id")
        if not board_id:
            err(f"boards[{i}] missing id")
            continue

        board_ids.append(board_id)

        if not board.get("name"):
            warn(f"board {board_id} missing name")

        meetings = board.get("meetings", [])
        if not isinstance(meetings, list):
            err(f"board {board_id} meetings must be a list")
            continue

        for j, m in enumerate(meetings):
            if not isinstance(m, dict):
                err(f"board {board_id} meetings[{j}] must be an object")
                continue

            date_val = m.get("date")
            if not date_val:
                err(f"board {board_id} meeting[{j}] missing date")
                continue

            all_board_keys.append((board_id, date_val, m.get("meeting_type", "Board")))

            if not parse_iso_date(date_val):
                warn(f"board {board_id} meeting {date_val} has invalid date")

            for field in ("agenda_url", "minutes_url", "package_url"):
                value = m.get(field)
                if value:
                    validate_docs_reference(value, f"board {board_id} meeting {date_val} field {field}")

            extra_docs = m.get("extra_docs", [])
            if extra_docs and isinstance(extra_docs, list):
                for k, doc in enumerate(extra_docs):
                    if isinstance(doc, dict) and doc.get("url"):
                        validate_docs_reference(doc["url"], f"board {board_id} meeting {date_val} extra_docs[{k}]")

    dupes(board_ids, "board ids")
    dupes(all_board_keys, "board meetings (board_id, date, meeting_type)")


def main() -> int:
    meetings_payload = expect_dict(load_json(MEETINGS_FILE), "meetings.json")
    bylaws_payload = expect_dict(load_json(BYLAWS_FILE), "bylaws.json")
    resolutions_payload = expect_dict(load_json(RESOLUTIONS_FILE), "resolutions.json")
    boards_payload = expect_dict(load_json(BOARDS_FILE), "boards.json")

    meetings = validate_meetings(meetings_payload)
    bylaws = validate_bylaws(bylaws_payload)
    bylaw_numbers = {b.get("number") for b in bylaws if isinstance(b, dict) and b.get("number")}
    validate_resolutions(resolutions_payload, bylaw_numbers)
    validate_boards(boards_payload)

    print("Validation summary")
    print(f"  errors: {len(errors)}")
    print(f"  warnings: {len(warnings)}")

    if warnings:
        print("\nWarnings:")
        for msg in warnings:
            print(f"  - {msg}")

    if errors:
        print("\nErrors:")
        for msg in errors:
            print(f"  - {msg}")
        return 1

    print("\nAll validation checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
