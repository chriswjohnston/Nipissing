#!/usr/bin/env python3
"""Daily: keep every township plan, and each term's strategic plan. No AI.

1. The township's "Strategies & Plans" page lists its standing plans
   (strategic plan, asset management plans and policy, accessibility plan,
   energy plan, community safety plan). Every PDF linked there is copied the
   day it appears and recorded in data/canonical/plans.json, with the date
   it was first seen and, once the township removes it, the date it went.
   terms.html uses this list: a new strategic plan posted for a term shows
   on that term's page without anyone editing terms.json.
2. The strategic plan named in terms.json is copied too (term_summary.py).

What still needs a person: how far each action in a plan has got. That
comes from staff reports to council, read and condensed into terms.json.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import doc_sources as ds
from archive import is_township_pdf, mirror

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "canonical" / "plans.json"
PAGE = "https://nipissingtownship.com/explore-nipissing-township/strategies-plans/"
TODAY = date.today().isoformat()


def kind_of(text: str) -> str:
    t = text.lower()
    for k, pat in [("Strategic plan", r"strategic.plan"), ("Asset management", r"asset.management"),
                   ("Accessibility", r"accessib"), ("Energy", r"conservation|energy|demand.management"),
                   ("Community safety", r"safety|cswb|well.being")]:
        if re.search(pat, t):
            return k
    return "Other plan"


def years_of(text: str):
    ys = [int(y) for y in re.findall(r"\b(20\d\d)\b", text)]
    return (min(ys), max(ys)) if ys else (None, None)


def scrape() -> list:
    try:
        r = ds.polite_get(PAGE, timeout=60)
    except Exception as e:
        print(f"  plans page: {e}")
        return []
    if r.status_code != 200:
        print(f"  plans page: HTTP {r.status_code}")
        return []
    out = []
    for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S | re.I):
        url = urljoin(PAGE, unescape(href)).replace("http://", "https://", 1)
        if is_township_pdf(url):
            label = re.sub(r"<[^>]+>|\s+", " ", unescape(text)).strip(" :")
            name = re.sub(r"[-_]+", " ", unquote(Path(urlparse(url).path).stem))
            out.append({"url": url, "title": label if len(label) > 3 else name})
    return out


def main() -> None:
    data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {
        "_about": "Plans listed on the township's Strategies & Plans page, copied daily by scripts/archive_plans.py. "
                  "listed=false means the township has since removed it; the copy kept here stays."}
    plans = {p["url"]: p for p in data.get("plans", [])}
    found = scrape()
    if found:                                   # an empty scrape (site down) changes nothing
        seen = {f["url"] for f in found}
        for f in found:
            p = plans.setdefault(f["url"], {"url": f["url"], "first_seen": TODAY})
            p["title"] = f["title"]
            p["kind"] = kind_of(f["title"] + " " + f["url"])
            p["from"], p["to"] = years_of(f["title"] + " " + unquote(f["url"]))
            p["listed"] = True
            p.pop("removed_seen", None)
        for url, p in plans.items():
            if url not in seen and p.get("listed"):
                p["listed"] = False
                p["removed_seen"] = TODAY
                print(f"  no longer listed: {p['title']}")
    for p in plans.values():
        if not p.get("archived"):
            got = mirror(p["url"], "plans", p.get("from"))
            if got:
                p["archived"] = got
                print(f"  kept: {p['title']}")
    data["plans"] = sorted(plans.values(), key=lambda p: (p.get("kind", ""), -(p.get("from") or 0)))
    data["checked"] = TODAY
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  {len(plans)} plans, {sum(1 for p in plans.values() if p.get('archived'))} kept")

    # The strategic plan named in terms.json (no AI; see term_summary.py).
    import term_summary
    sys.argv.append("--archive-only")
    term_summary.main()


if __name__ == "__main__":
    main()
