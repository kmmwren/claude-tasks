#!/usr/bin/env python3
"""
prioritise.py — rank the ready/ queue, dependency-aware: "what should I do next?"

The score is deliberately generic and transparent (no personal "leverage" tuning):

  due urgency  +  importance  +  quick-win (low effort)  +  lead-time bonus
  +  unlock bonus (briefs that unblock others rank higher)
  +  optional config tag weights (define your own priority tags in tasks.toml)

Dependencies are real: a brief lists `blockers:` (ids or external text) and/or
`depends:<id>` tags. A dependency counts as MET only once its target brief is `done`, so
the queue self-unblocks as work completes. A brief is *actionable* when it is `autonomy:
full`, unblocked, and runnable in the current context.

Usage:
  python scripts/prioritise.py                 # ranked table (all of ready/, flagged)
  python scripts/prioritise.py --actionable    # only the auto-actionable ones
  python scripts/prioritise.py --top 5 --json  # machine-readable, top 5
  python scripts/prioritise.py --context cloud # exclude briefs needing local/other-repo access
"""
import argparse
import datetime
import json
import pathlib
import re
from typing import Any

from _paths import require_queue, resolve_root, strip_comment
from config import load_config

EFFORT_RANK = {"xs": 40, "s": 30, "m": 20, "l": 10, "xl": 5, "": 15}
EFFORT_DAYS = {"xs": 0.125, "s": 0.5, "m": 2, "l": 7, "xl": 14}
UNLOCK_WEIGHT = 150
INDEX_DIRS = ("ready", "in-progress", "parked", "done")

# A context excludes briefs declaring a requires-* field it cannot satisfy.
CONTEXT_EXCLUSIONS = {
    # cloud: only this repo is available; no local-machine files, no other repos
    "cloud": {"requires-local", "requires-repo"},
}


def fm(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    d: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                d[k.strip()] = strip_comment(v)
    return d


def _parse_tags(brief: dict[str, Any]) -> set[str]:
    raw = (brief.get("tags", "") or "").strip().strip("[]")
    return {t.strip() for t in raw.split(",") if t.strip()}


def parse_deps(brief: dict[str, Any]) -> set[str]:
    """Dependency ids: every token in `blockers:` plus the target of each `depends:<id>` tag."""
    deps = set(re.findall(r"[\w-]+", brief.get("blockers", "") or ""))
    for tag in _parse_tags(brief):
        if tag.startswith("depends:"):
            deps.add(tag[len("depends:"):])
    return deps


def is_runnable_in_context(brief: dict[str, Any], context: str) -> bool:
    """False if the brief declares a requires-* field the context cannot satisfy."""
    for field in CONTEXT_EXCLUSIONS.get(context, set()):
        val = (brief.get(field, "") or "").strip()
        if val and val.lower() not in ("false", "[]"):
            return False
    return True


def lead_time_urgent(brief: dict[str, Any], today: datetime.date) -> bool:
    """True if the effort lead time means this must START now even though the due date is 'soon'."""
    due_str = brief.get("due", "")
    if not due_str:
        return False
    try:
        due_date = datetime.date.fromisoformat(due_str)
    except ValueError:
        return False
    days_until_due = (due_date - today).days
    effort_days = EFFORT_DAYS.get(brief.get("estimated-effort", ""), 0.5)
    return 0 <= days_until_due <= effort_days


def score(brief: dict[str, Any], today: datetime.date, *, unlocks: int = 0,
          tag_weights: dict[str, int] | None = None) -> int:
    due_score = 0
    if brief.get("due"):
        try:
            days = (datetime.date.fromisoformat(brief["due"]) - today).days
            due_score = 1000 if days < 0 else 500 if days <= 3 else 200 if days <= 7 else 50
        except ValueError:
            pass
    try:
        imp = int(brief.get("importance", "3"))
    except ValueError:
        imp = 3
    importance_score = (5 - imp) * 100
    effort_score = EFFORT_RANK.get(brief.get("estimated-effort", ""), 15)
    lead_bonus = 300 if lead_time_urgent(brief, today) else 0
    tags = _parse_tags(brief)
    tag_score = sum(w for t, w in (tag_weights or {}).items() if t in tags)
    return due_score + importance_score + effort_score + lead_bonus + tag_score + unlocks * UNLOCK_WEIGHT


def index_queue(root: pathlib.Path) -> tuple[dict[str, str], dict[str, int]]:
    """Map every brief id to its status folder, and count how many briefs depend on each id."""
    status_by_id: dict[str, str] = {}
    unlocks: dict[str, int] = {}
    for d in INDEX_DIRS:
        for f in (root / d).glob("*.md"):
            b = fm(f.read_text())
            status_by_id[b.get("id", f.stem)] = d
            for dep in parse_deps(b):
                unlocks[dep] = unlocks.get(dep, 0) + 1
    return status_by_id, unlocks


def is_blocked(brief: dict[str, Any], done_ids: set[str]) -> tuple[bool, list[str]]:
    """A brief is blocked if autonomy is blocked, or any dependency is not yet `done`."""
    if brief.get("autonomy") == "blocked":
        return True, []
    unmet = sorted(d for d in parse_deps(brief) if d not in done_ids)
    return bool(unmet), unmet


def rank(root: pathlib.Path, today: datetime.date | None = None, context: str = "",
         tag_weights: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Every ready/ brief, scored and annotated (_score, _blocked, _unlocks, _actionable, …)."""
    today = today or datetime.date.today()
    status_by_id, unlocks = index_queue(root)
    done_ids = {bid for bid, st in status_by_id.items() if st == "done"}
    if tag_weights is None:
        tag_weights = load_config(root).get("priority", {}).get("tag_weights", {})
    out: list[dict[str, Any]] = []
    for f in sorted((root / "ready").glob("*.md")):
        b: dict[str, Any] = dict(fm(f.read_text()))
        bid = b.get("id", f.stem)
        blocked, unmet = is_blocked(b, done_ids)
        n_unlocks = unlocks.get(bid, 0)
        b["_score"] = score(b, today, unlocks=n_unlocks, tag_weights=tag_weights)
        b["_unlocks"] = n_unlocks
        b["_lead_urgent"] = lead_time_urgent(b, today)
        b["_blocked"] = blocked
        b["_unmet"] = unmet
        b["_file"] = f.name
        b["_actionable"] = (
            b.get("autonomy") == "full" and not blocked and is_runnable_in_context(b, context)
        )
        out.append(b)
    out.sort(key=lambda b: (-b["_score"], b.get("estimated-effort", "z")))
    return out


def _row(b: dict[str, Any]) -> str:
    flags = ""
    if not b["_actionable"]:
        if b["_blocked"]:
            flags += f" ⛔blocked({', '.join(b['_unmet']) or b.get('autonomy', '')})"
        elif b.get("autonomy") != "full":
            flags += f" 🙋{b.get('autonomy', '')}"
    if b["_lead_urgent"]:
        flags += " ⏱lead"
    if b["_unlocks"]:
        flags += f" →{b['_unlocks']}"
    return (f"{b['_score']:>5}  {b.get('importance', '?'):<2} "
            f"{b.get('due', '') or '-':<10} {b.get('estimated-effort', '') or '-':<3} "
            f"{b.get('id', '?')}{flags}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Rank the ready queue (dependency-aware).")
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--actionable", action="store_true", help="only auto-actionable briefs")
    ap.add_argument("--context", default="", help="execution context, e.g. 'cloud'")
    a = ap.parse_args(argv)

    root = require_queue(resolve_root())
    items = rank(root, context=a.context)
    if a.actionable:
        items = [b for b in items if b["_actionable"]]
    if a.top:
        items = items[: a.top]

    if a.json:
        print(json.dumps([{
            "id": b.get("id"), "score": b["_score"], "title": b.get("title", ""),
            "importance": b.get("importance"), "due": b.get("due", ""),
            "effort": b.get("estimated-effort", ""), "file": b["_file"],
            "blocked": b["_blocked"], "unmet": b["_unmet"], "unlocks": b["_unlocks"],
            "lead_urgent": b["_lead_urgent"], "actionable": b["_actionable"],
        } for b in items], indent=2))
        return
    if not items:
        print("No briefs to show (try without --actionable, or add some to ready/).")
        return
    print(f"{'score':>5}  {'P':<2} {'due':<10} {'eff':<3} id")
    for b in items:
        print(_row(b))


if __name__ == "__main__":
    main()  # pragma: no cover
