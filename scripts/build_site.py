#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "canonical"
SITE = ROOT / "site"
DOCS = ROOT / "docs"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def ensure_docs_dir() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)


def copy_site_files() -> None:
    if not SITE.exists():
        raise FileNotFoundError(f"site/ directory not found: {SITE}")

    copied = []
    for file in SITE.iterdir():
        if file.is_file():
            shutil.copy2(file, DOCS / file.name)
            copied.append(file.name)

    print(f"Copied {len(copied)} site file(s) to {DOCS}: {', '.join(sorted(copied))}")


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
