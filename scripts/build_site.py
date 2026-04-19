#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "canonical"
SITE = ROOT  # HTML files now served from repo root
DOCS = ROOT / "docs"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def ensure_docs_dir() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)


def copy_site_files() -> None:
    extensions = {".html", ".css", ".js"}
    copied = []
    for file in ROOT.iterdir():
        if file.is_file() and file.suffix in extensions:
            shutil.copy2(file, DOCS / file.name)
            copied.append(file.name)

    # Also copy CNAME if present
    cname = ROOT / "CNAME"
    if cname.exists():
        shutil.copy2(cname, DOCS / "CNAME")
        copied.append("CNAME")

    print(f"Copied {len(copied)} site file(s) to {DOCS}: {', '.join(sorted(copied))}")


def slug_from_display_date(display_date: str) -> str:
    """
    Convert 'Mar 17, 2026' -> 'march-17-2026'
    """
    m = re.match(r"^([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4})$", display_date.strip())
    if not m:
        return ""
    month_map = {
        "jan": "january", "feb": "february", "mar": "march", "apr": "april",
        "may": "may", "jun": "june", "jul": "july", "aug": "august",
        "sep": "september", "oct": "october", "nov": "november", "dec": "december",
    }
    month = month_map.get(m.group(1).strip().lower()[:3], m.group(1).strip().lower())
    day = str(int(m.group(2)))
    year = m.group(3)
    return f"{month}-{day}-{year}"


def merge_summaries_into_meetings(meetings: dict[str, Any], summaries: dict[str, str]) -> dict[str, Any]:
    out = {"meetings": []}
    for m in meetings.get("meetings", []):
        item = dict(m)
        year = str(item.get("year", "")) or str(item.get("date", ""))[:4]
        display_date = item.get("display_date") or ""
        slug = slug_from_display_date(display_date)
        summary_key = f"{year}/{slug}" if year and slug else None
        if summary_key and not item.get("summary"):
            item["summary"] = summaries.get(summary_key)
        out["meetings"].append(item)
    return out


def build_compatibility_data() -> None:
    meetings = load_json(DATA / "meetings.json", {"meetings": []})
    bylaws = load_json(DATA / "bylaws.json", {"bylaws": []})
    resolutions = load_json(DATA / "resolutions.json", {"resolutions": []})
    boards = load_json(DATA / "boards.json", {"boards": []})
    summaries = load_json(DATA / "summaries.json", {})

    if isinstance(meetings, list):
        meetings = {"meetings": meetings}
    if isinstance(bylaws, list):
        bylaws = {"bylaws": bylaws}
    if isinstance(resolutions, list):
        resolutions = {"resolutions": resolutions}
    if isinstance(boards, list):
        boards = {"boards": boards}

    meetings = merge_summaries_into_meetings(meetings, summaries)

    write_json(DOCS / "council-data.json", meetings)
    write_json(DOCS / "bylaws-data.json", bylaws)
    write_json(DOCS / "resolutions-data.json", resolutions)
    write_json(DOCS / "boards-data.json", boards)
    write_json(DOCS / "summaries.json", summaries)

    write_json(DOCS / "data" / "meetings.json", meetings)
    write_json(DOCS / "data" / "bylaws.json", bylaws)
    write_json(DOCS / "data" / "resolutions.json", resolutions)
    write_json(DOCS / "data" / "boards.json", boards)
    write_json(DOCS / "data" / "summaries.json", summaries)

    print("Wrote frontend JSON into docs/")


def verify_required_outputs() -> None:
    required = [
        DOCS / "index.html",
        DOCS / "shared.css",
        DOCS / "shared.js",
        DOCS / "council-data.json",
        DOCS / "bylaws-data.json",
        DOCS / "resolutions-data.json",
        DOCS / "boards-data.json",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise RuntimeError(
            "Build finished but required output files are missing:\n- "
            + "\n- ".join(missing)
        )


def main() -> None:
    print("ROOT =", ROOT)
    print("DATA =", DATA)
    print("SITE =", SITE)
    print("DOCS =", DOCS)

    ensure_docs_dir()
    copy_site_files()
    build_compatibility_data()
    verify_required_outputs()

    print("✓ docs/ built successfully")


if __name__ == "__main__":
    main()
