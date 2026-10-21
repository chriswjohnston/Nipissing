#!/usr/bin/env python3
"""
joint_boards.py — shared plumbing for external joint boards (Health Unit,
DNSSAB): bodies that Nipissing is represented on but does not run.

Each committee is stored as its own entry in boards.json (so a committee and
the main board meeting on the same day never collide), tied together by a
"group" id the site uses for filtering. Records are carry-forward: a meeting
or document that disappears from the source is never removed here.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from archive import mirror_record

ROOT = Path(__file__).resolve().parents[1]
BOARDS_FILE = ROOT / "data" / "canonical" / "boards.json"
TODAY = date.today()
DOC_FIELDS = ("agenda_url", "minutes_url", "package_url")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return deepcopy(default)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]


def display_date(dt: date) -> str:
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def new_meeting(body: Dict[str, Any], dt: date, meeting_type: str = "Board") -> Dict[str, Any]:
    return {
        "date": dt.isoformat(),
        "display_date": display_date(dt),
        "year": dt.year,
        "meeting_type": meeting_type,
        "title": body["name"],
        "board_id": body["id"],
        "board_name": body["name"],
        "body": body["short"],
        "body_id": body["id"],
        "group": body["group"],
        "source_kind": "board",
        "external": True,
        "all_agendas": True,
        "agenda_url": None,
        "minutes_url": None,
        "package_url": None,
        "extra_docs": [],
        "summary": None,
        "events": [],
        "cancelled": False,
        "rescheduled": False,
        "postponed": False,
        "is_future": dt > TODAY,
        "video_url": None,
    }


def add_doc(m: Dict[str, Any], kind: str, url: str, label: str) -> None:
    """Put a document on a meeting: primary slot if free, else extra_docs."""
    if not url:
        return
    field = f"{kind}_url" if kind in ("agenda", "minutes", "package") else None
    if field and not m.get(field):
        m[field] = url
        return
    if field and m.get(field) == url:
        return
    if any(d.get("url") == url for d in m.get("extra_docs", [])):
        return
    m.setdefault("extra_docs", []).append({"label": label or "Document", "url": url})


class Store:
    """Loads boards.json, hands out board entries for one group, merges
    scraped meetings into stored ones (carry-forward), mirrors, saves."""

    def __init__(self, group: str):
        self.group = group
        self.payload = load_json(BOARDS_FILE, {"boards": []})
        self.payload.setdefault("boards", [])
        self.scraped: Dict[str, Dict[str, Dict[str, Any]]] = {}   # body id -> key -> meeting
        self.bodies: Dict[str, Dict[str, Any]] = {}

    def body(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if body["id"] in self.bodies:      # one shared dict per body
            return self.bodies[body["id"]]
        self.bodies[body["id"]] = body
        self.scraped.setdefault(body["id"], {})
        return body

    def meeting(self, body: Dict[str, Any], dt: date, meeting_type: str = "Board") -> Dict[str, Any]:
        key = f"{dt.isoformat()}|{meeting_type}"
        ms = self.scraped.setdefault(body["id"], {})
        if key not in ms:
            ms[key] = new_meeting(body, dt, meeting_type)
        return ms[key]

    def save(self, category_prefix: str) -> None:
        boards = self.payload["boards"]
        for bid, body in self.bodies.items():
            entry = next((b for b in boards if b.get("id") == bid), None)
            if entry is None:
                entry = {"id": bid, "meetings": []}
                boards.append(entry)
            entry.update({
                "name": body["name"], "short_name": body["short"], "url": body["url"],
                "description": body.get("description", ""), "group": self.group,
                "external": True,
            })
            if body.get("members"):
                entry["members"] = body["members"]
            for k in ("resolutions", "page_docs"):
                if body.get(k):
                    old = {json.dumps(x, sort_keys=True) for x in entry.get(k, [])}
                    entry[k] = (entry.get(k) or []) + [
                        x for x in body[k] if json.dumps(x, sort_keys=True) not in old]

            stored = {f"{m['date']}|{m.get('meeting_type', 'Board')}": m
                      for m in entry.get("meetings", [])}
            for key, sm in self.scraped.get(bid, {}).items():
                old = stored.get(key)
                if old is None:
                    stored[key] = sm
                    continue
                for f in DOC_FIELDS:   # newer link wins; the old one (and its copy) stays
                    if sm.get(f) and sm[f] != old.get(f):
                        arch_f = f.replace("_url", "_archived")
                        if old.get(f):
                            prev = {"label": f"Earlier {f.split('_')[0]} version", "url": old[f]}
                            if old.get(arch_f):
                                prev["archived"] = old[arch_f]
                            if not any(d.get("url") == old[f] for d in old.get("extra_docs", [])):
                                old.setdefault("extra_docs", []).append(prev)
                        old[f] = sm[f]
                        old.pop(arch_f, None)
                for d in sm.get("extra_docs", []):
                    add_doc(old, "extra", d["url"], d.get("label"))
                for flag in ("cancelled", "rescheduled", "postponed"):
                    old[flag] = bool(sm.get(flag))
                for f in ("location", "source_url"):
                    if sm.get(f):
                        old[f] = sm[f]
            for m in stored.values():
                try:
                    m["is_future"] = date.fromisoformat(m["date"]) > TODAY
                except ValueError:
                    pass
                m["external"] = True
                m["all_agendas"] = True
                m["group"] = self.group
                mirror_record(m, f"{category_prefix}/{bid}")
            entry["meetings"] = sorted(stored.values(), key=lambda x: x["date"], reverse=True)

        self.payload["last_updated"] = TODAY.isoformat()
        save_json(BOARDS_FILE, self.payload)
        for bid in self.bodies:
            entry = next(b for b in boards if b["id"] == bid)
            ms = entry["meetings"]
            print(f"  {bid}: {len(ms)} meeting(s) | agendas {sum(1 for m in ms if m.get('agenda_url'))}"
                  f" | packages {sum(1 for m in ms if m.get('package_url'))}"
                  f" | minutes {sum(1 for m in ms if m.get('minutes_url'))}"
                  f" | future {sum(1 for m in ms if m.get('is_future'))}")
