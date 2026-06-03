#!/usr/bin/env python3
"""
add_task.py — let any Claude session add a well-formed brief to the queue.

Fills out a brief from flags so Claude can self-capture follow-ups, discovered work, and
improvements without hand-writing the file. Defaults to inbox/ (raw) unless given enough
to be ready/. Stdlib only.

Examples:
  python scripts/add_task.py --title "Rotate the leaked API key" \
     --goal "The leaked key is rotated and the app still works." \
     --domain security --importance 1 --autonomy needs-input --due 2026-06-05 --ready --commit

  echo "idea: a hook that blocks committing secrets" | \
     python scripts/add_task.py --stdin --title "Pre-commit secret scanner"
"""
import argparse
import datetime
import re
import sys

from _concurrency import atomic_write, reserve_name
from _paths import TASK_DIRS, require_queue, resolve_root
from git_sync import sync_commit as _sync_commit


def slug(s: str, maxwords: int = 6) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", s.lower())
    words = [w for w in re.split(r"[\s-]+", s) if w]
    return "-".join(words[:maxwords]) or "task"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Add a brief to the tasks queue")
    ap.add_argument("--title", required=True)
    ap.add_argument("--goal", default="")
    ap.add_argument("--context", default="")
    ap.add_argument("--criteria", action="append", default=[], help="repeatable success criterion")
    ap.add_argument("--type", default="todo", choices=["todo", "project", "research", "decision"])
    ap.add_argument("--importance", default="3", choices=["1", "2", "3", "4"])
    ap.add_argument("--autonomy", default="needs-input", choices=["full", "needs-input", "blocked"])
    ap.add_argument("--effort", default="")
    ap.add_argument("--due", default="")
    ap.add_argument("--domain", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--parent", default="")
    ap.add_argument("--source", default="claude", choices=["claude", "user"])
    ap.add_argument("--ready", action="store_true", help="file in ready/ (enriched) instead of inbox/")
    ap.add_argument(
        "--parked",
        action="store_true",
        help="file in parked/ (status: parked) — work intentionally suspended awaiting external input",
    )
    ap.add_argument("--stdin", action="store_true", help="append stdin to Context")
    ap.add_argument("--commit", action="store_true", help="git add+commit+push the new brief")
    ap.add_argument("--root", default=None, help="queue root (default: resolved per _paths)")
    a = ap.parse_args(argv)

    root = require_queue(a.root if a.root else resolve_root())
    today = datetime.date.today().isoformat()
    if a.parked:
        status = "parked"
    elif a.ready:
        status = "ready"
    else:
        status = "inbox"
    # Atomically claim a unique filename BEFORE writing, so two sessions capturing the
    # same title at the same instant can't overwrite each other (O_CREAT|O_EXCL).
    nid = reserve_name(root / status, slug(a.title), [root / d for d in TASK_DIRS])
    context = a.context
    if a.stdin and not sys.stdin.isatty():
        context = (context + "\n\n" + sys.stdin.read().strip()).strip()
    tags = "[" + ", ".join(t.strip() for t in a.tags.split(",") if t.strip()) + "]"

    crit = "\n".join(f"- [ ] {c}" for c in a.criteria) if a.criteria else "# TODO"
    fm = f"""---
id: {nid}
title: {a.title}
created: {today}
updated: {today}
status: {status}
type: {a.type}
importance: {a.importance}
autonomy: {a.autonomy}
estimated-effort: {a.effort}
due: {a.due}
domain: {a.domain}
tags: {tags}
parent: {a.parent}
source: {a.source}
blockers: []
related: []
---

## Goal

{a.goal or "# TODO"}

## Context

{context or "# TODO"}

## Success criteria

{crit}

## Constraints

# none recorded

## Notes / open questions

_none_

## Execution log

{today}: captured by {a.source} via add_task.py.
"""
    # Fill the reserved (empty) file atomically: a concurrent committer never sees a
    # half-written brief, it sees either nothing-yet or the complete file.
    path = root / status / f"{nid}.md"
    atomic_write(path, fm)
    print(f"{status}/{nid}.md")

    if a.commit:
        _sync_commit(f"brief({nid}): captured ({a.source})", root)


if __name__ == "__main__":
    main()  # pragma: no cover
