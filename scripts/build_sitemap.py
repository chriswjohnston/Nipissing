#!/usr/bin/env python3
"""Daily: sitemap.xml and robots.txt, so search engines can list every record.

Every page here is built in the browser from an address like
bylaw-detail.html?id=2021-50, which no crawler would find by following
links from the home page alone. The sitemap names each one: the main
pages, every law, by-law, resolution and past meeting (council and boards).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "data" / "canonical"
SITE = "https://nipissing.news/"
TODAY = date.today().isoformat()


def load(name: str, default):
    try:
        return json.loads((CANON / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def main() -> None:
    urls = []   # (path, lastmod or None, priority)
    for page, pr in [("", "1.0"), ("council.html", "0.9"), ("laws.html", "0.9"), ("decisions.html", "0.9"),
                     ("terms.html", "0.7"), ("about.html", "0.5")]:
        urls.append((page, TODAY if pr != "0.5" else None, pr))

    bylaws = load("bylaws.json", {}).get("bylaws", [])
    when = {b["number"]: (b.get("date_passed") or b.get("meeting_date") or "")[:10] for b in bylaws}
    for law in load("consolidations.json", {}).get("consolidations", {}).values():
        urls.append((f"law.html?id={quote(law['id'])}", law.get("generated"), "0.8"))
    for b in bylaws:
        urls.append((f"bylaw-detail.html?id={quote(str(b['number']))}", when.get(b["number"]) or None, "0.6"))
    for r in load("resolutions.json", {}).get("resolutions", []):
        if not r.get("fragment"):
            urls.append((f"resolution-detail.html?id={quote(r['number'])}", (r.get("meeting_date") or "")[:10] or None, "0.5"))
    for m in load("meetings.json", {}).get("meetings", []):
        if m.get("date") and m["date"] <= TODAY and not m.get("cancelled"):
            urls.append((f"meeting-detail.html?date={m['date']}&body=council", m["date"], "0.6"))
    for b in load("boards.json", {}).get("boards", []):
        for m in b.get("meetings", []):
            if m.get("date") and m["date"] <= TODAY and not m.get("cancelled"):
                special = "&type=Special" if m.get("meeting_type") == "Special" else ""
                urls.append((f"meeting-detail.html?date={m['date']}&body={quote(b['id'])}{special}", m["date"], "0.4"))

    seen, rows = set(), []
    for path, mod, pr in urls:
        if path in seen:
            continue
        seen.add(path)
        rows.append(f"  <url><loc>{escape(SITE + path)}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") +
                    f"<priority>{pr}</priority></url>")
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + \
          "\n".join(rows) + "\n</urlset>\n"
    (ROOT / "sitemap.xml").write_text(xml, encoding="utf-8")
    (ROOT / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {SITE}sitemap.xml\n", encoding="utf-8")
    print(f"  sitemap.xml: {len(rows)} addresses")


if __name__ == "__main__":
    main()
