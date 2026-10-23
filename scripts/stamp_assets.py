#!/usr/bin/env python3
"""
stamp_assets.py — keep browsers from using an old shared.css / shared.js.

Every page loads shared.css?v=… and shared.js?v=…. When either file
changes, the ?v= must change too or visitors keep a cached copy that no
longer matches the page. This sets ?v= to the first 10 characters of each
file's SHA-256, so it changes exactly when the file does.

    python scripts/stamp_assets.py          # rewrite the pages
    python scripts/stamp_assets.py --check  # exit 1 if any page is stale
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("shared.css", "shared.js")


def stamps() -> dict:
    return {a: hashlib.sha256((ROOT / a).read_bytes()).hexdigest()[:10] for a in ASSETS}


def main() -> int:
    check = "--check" in sys.argv
    want = stamps()
    stale = []
    for page in sorted(ROOT.glob("*.html")):
        text = page.read_text(encoding="utf-8")
        new = text
        for asset, v in want.items():
            new = re.sub(rf'({re.escape(asset)})\?v=[^"\'\s>]*', rf"\g<1>?v={v}", new)
        if new != text:
            stale.append(page.name)
            if not check:
                page.write_text(new, encoding="utf-8")
    if check and stale:
        print(f"Out-of-date shared.css/shared.js stamps in: {', '.join(stale)}. "
              "Run: python scripts/stamp_assets.py")
        return 1
    print(f"  asset stamps: {'updated ' + ', '.join(stale) if stale else 'all current'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
