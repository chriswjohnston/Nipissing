#!/usr/bin/env python3
"""Write the yearly summary of a council term for terms.html.

Runs once a year (and on demand) from .github/workflows/term-summary.yml.
For the current term, and a term that ended in the last year, it gives
Claude the term's council decisions (resolutions and by-laws, routine items
left out) and the meeting summaries, and asks for a short neutral overview
and the term's key decisions, each tied to record numbers. Record numbers
that don't exist are dropped. The result is saved as the term's "summary"
in data/canonical/terms.json, with the date it was written.

It also archives the term's strategic plan PDF, if it isn't already.

Counts on the page are not written here: the page computes them daily.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List

import requests

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "data" / "canonical"
TERMS = CANON / "terms.json"
MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 6000
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
PROMPT_VERSION = 1

# Same routine wording the Decisions page hides (shared.js ROUTINE_RULES).
ROUTINE = [
    r"\bminutes\b[^.]{0,160}\b(?:be\s+)?(?:adopted|approved)\b",
    r"\badopt(?:s|ed)?\s+the\s+minutes\b",
    r"\bagenda\b[^.]{0,80}\b(?:be\s+)?(?:adopted|approved)\b",
    r"\b(?:meeting|council)\s+(?:be\s+|is\s+)?adjourn",
    r"^(?:that\s+)?(?:we|council)\s+(?:receives?|accepts?)\s+the\s+correspondence",
    r"\bconfirm\s+the\s+proceedings\s+of\s+council\b",
    r"\bstatement\s+of\s+accounts\b",
]


def load(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def routine(text: str) -> bool:
    return any(re.search(p, text or "", re.I) for p in ROUTINE)


def term_records(term: Dict[str, Any]) -> Dict[str, Any]:
    start, end = term["start"], term["end"]
    within = lambda d: bool(d) and start <= d[:10] <= end
    res = [r for r in load(CANON / "resolutions.json", {}).get("resolutions", [])
           if within(r.get("meeting_date")) and not r.get("fragment")]
    bls = [b for b in load(CANON / "bylaws.json", {}).get("bylaws", [])
           if within(b.get("date_passed") or b.get("meeting_date"))]
    mtgs = [m for m in load(CANON / "meetings.json", {}).get("meetings", [])
            if within(m.get("date")) and m.get("summary")]
    passing = {r.get("bylaw_number") for r in res
               if r.get("bylaw_number") and re.search(r"\bpass(?:es|ed)?\s+(?:the\s+)?by-?law\b", r.get("motion_text") or "", re.I)}
    lines, ids = [], set()
    for r in sorted(res, key=lambda r: (r.get("meeting_date", ""), r["number"])):
        if r.get("bylaw_number") in passing and re.search(r"\bpass", r.get("motion_text") or "", re.I):
            continue                                    # the by-law line covers it
        text = re.sub(r"\s+", " ", r.get("motion_text") or "")
        if routine(text):
            continue
        ids.add(r["number"])
        lines.append(f"{r['meeting_date']} {r['number']} [{r.get('status', '')}] {text[:600]}")
    for b in sorted(bls, key=lambda b: (b.get("date_passed") or b.get("meeting_date") or "", b["number"])):
        if routine(b.get("title", "")):
            continue
        ids.add(b["number"])
        lines.append(f"{(b.get('date_passed') or b.get('meeting_date'))[:10]} By-law {b['number']}: {b.get('common_name') or b.get('title')}")
    sums = [f"## {m['date']}\n{re.sub(r'(?m)^# .*$', '', m['summary']).strip()[:1500]}" for m in sorted(mtgs, key=lambda m: m["date"])]
    return {"decisions": lines, "ids": ids, "summaries": sums}


PROMPT = """You are writing a factual summary of one Ontario township council's term for an independent public archive, Nipissing Public Record.
Readers are residents. Be neutral: report what council decided, with no praise, criticism, speculation or adjectives about performance. Plain language, short sentences, sentence case.

Term: {label} ({start} to {end}). Records available up to {through}.

Below are the term's council decisions (routine items such as adopting minutes have been removed), then plain-language summaries of the meetings.
Each decision line starts with its date and record number: a resolution (R2024-090) or "By-law 2024-20".

Return only JSON, in this shape:
{{
  "overview": "2 to 4 sentences on what the term's decisions were mostly about. No numbers that you would have to count yourself.",
  "key_decisions": [
    {{"title": "A short heading in sentence case, under 9 words", "text": "One or two sentences on what council decided and when.", "records": ["R2024-090", "2024-20"], "date": "YYYY-MM-DD"}}
  ]
}}

Rules for key_decisions:
- 8 to 12 items: the decisions with the largest effect on residents, money or township services. Prefer final decisions over procedural steps.
- "records" must use only record numbers that appear in the decision lines below (write by-laws as "2024-20", resolutions as "R2024-090").
- Order them by date.

DECISIONS
{decisions}

MEETING SUMMARIES
{summaries}
"""


def call_claude(prompt: str) -> Dict[str, Any]:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": MAX_TOKENS, "messages": [{"role": "user", "content": prompt}]},
        timeout=(30, 600),
    )
    if r.status_code != 200:
        raise RuntimeError(f"API {r.status_code}: {r.text[:400]}")
    text = "".join(b.get("text", "") for b in r.json().get("content", []))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"no JSON in response: {text[:200]!r}")
    return json.loads(m.group(0))


def archive_plan(term: Dict[str, Any]) -> None:
    plan = term.get("strategic_plan") or {}
    if not plan.get("url") or plan.get("archived"):
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from archive import mirror
        got = mirror(plan["url"], "terms", int(term["start"][:4]))
        if got:
            plan["archived"] = got
            print(f"  archived strategic plan: {got}")
    except Exception as e:
        print(f"  strategic plan not archived ({e})")


def main() -> None:
    data = load(TERMS, {"terms": []})
    today = date.today()
    force = "--force" in sys.argv
    changed = False
    for term in data.get("terms", []):
        if term.get("upcoming") or today.isoformat() < term["start"]:
            continue
        ended_long_ago = term["end"] < (today - timedelta(days=400)).isoformat()
        if ended_long_ago and not force:
            continue
        print(f"\n═══ {term['label']} ═══")
        archive_plan(term)
        changed = True
        if not API_KEY:
            print("  no ANTHROPIC_API_KEY; summary not written")
            continue
        rec = term_records(term)
        if not rec["decisions"]:
            print("  no decisions in range")
            continue
        through = min(today.isoformat(), term["end"])
        out = call_claude(PROMPT.format(label=term["label"], start=term["start"], end=term["end"], through=through,
                                        decisions="\n".join(rec["decisions"]), summaries="\n\n".join(rec["summaries"])))
        items = []
        for k in out.get("key_decisions") or []:
            recs = [str(x).replace("By-law ", "").strip() for x in k.get("records") or []]
            recs = [x for x in recs if x in rec["ids"]]
            if not recs or not k.get("title") or not k.get("text"):
                continue
            items.append({"title": k["title"].strip(), "text": k["text"].strip(), "records": recs, "date": str(k.get("date") or "")[:10]})
        term["summary"] = {
            "overview": (out.get("overview") or "").strip(),
            "key_decisions": items,
            "generated": today.isoformat(),
            "records_through": through,
            "model": MODEL,
            "prompt_version": PROMPT_VERSION,
        }
        print(f"  summary written: {len(items)} key decisions")
    if changed:
        TERMS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
