#!/usr/bin/env python3
"""
add_task.py — let any Claude session add a well-formed brief to the queue.

Fills out a brief from flags so Claude can self-capture follow-ups, discovered work, and
improvements without hand-writing the file. Defaults to inbox/ (raw); a ready/ brief
requires --goal, --criteria and --context so it never carries placeholder stubs.

A capture whose fingerprint (normalised title plus the sources it cites) matches a brief already
in the queue is a repeat: --on-duplicate merge (default) adds it to that brief as a log line and,
when it carries new context, a note; refuse files nothing and exits 1; file captures it anyway.
Either way the output says which brief it matched. Stdlib only.

Examples:
  python scripts/add_task.py --title "Rotate the leaked API key" \
     --goal "The leaked key is rotated and the app still works." \
     --context "Key leaked in PR 123; rotate in the vault." --criteria "Old key rejected by the API" \
     --domain security --importance 1 --autonomy needs-input --due 2026-06-05 --ready --commit

  echo "idea: a hook that blocks committing secrets" | \
     python scripts/add_task.py --stdin --title "Pre-commit secret scanner"
"""

import argparse
import datetime
import pathlib
import re
import sys

from _concurrency import atomic_write, require_lock, reserve_name
from _paths import TASK_DIRS, require_queue, resolve_root
from fingerprint import Fingerprint, Located, find_duplicate
from git_sync import sync_commit as _sync_commit
from readiness import (
    PLACEHOLDER_CONTEXT,
    missing_ready_fields,
    placeholder_criterion,
    placeholder_goal,
)

# The CLI flag that supplies each field a ready brief needs.
FLAGS = {"goal": "--goal", "criteria": "--criteria/--success", "context": "--context"}
NOTES = re.compile(r"(## Notes / open questions\n\n)(?:_none_\n*)?")
ON_DUPLICATE = ("merge", "refuse", "file")


def slug(s: str, maxwords: int = 6) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", s.lower())
    words = [w for w in re.split(r"[\s-]+", s) if w]
    return "-".join(words[:maxwords]) or "task"


def render_brief(nid: str, a: argparse.Namespace, status: str, context: str, today: str) -> str:
    tags = "[" + ", ".join(t.strip() for t in a.tags.split(",") if t.strip()) + "]"
    crit = (
        "\n".join(f"- [ ] {c}" for c in a.criteria) if a.criteria else f"- [ ] {placeholder_criterion(a.title)}"
    )
    return f"""---
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

{a.goal or placeholder_goal(a.title)}

## Context

{context or PLACEHOLDER_CONTEXT}

## Success criteria

{crit}

## Constraints

# none recorded

## Notes / open questions

_none_

## Execution log

{today}: captured by {a.source} via add_task.py.
"""


def where(found: Located) -> str:
    return f"{found.folder}/{found.id}.md"


def add_note(text: str, note: str) -> str:
    """`note` first under Notes / open questions, replacing the `_none_` placeholder."""
    if not NOTES.search(text):
        return f"{text.rstrip()}\n\n## Notes / open questions\n\n{note}\n"
    return NOTES.sub(lambda m: f"{m[1]}{note}\n\n", text, count=1)


def merged_text(text: str, source: str, context: str, today: str) -> str:
    noted = add_note(text, context) if context and context not in text else text
    return f"{noted.rstrip()}\n{today}: repeat capture by {source} merged here.\n"


def merge_into(root: pathlib.Path, found: Located, source: str, context: str, today: str) -> None:
    with require_lock(root):
        atomic_write(found.path, merged_text(found.path.read_text(), source, context, today))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Add a brief to the tasks queue")
    ap.add_argument("--title", required=True, type=str.strip)
    ap.add_argument("--goal", default="")
    ap.add_argument("--context", default="")
    ap.add_argument(
        "--criteria",
        "--success",
        dest="criteria",
        action="append",
        default=[],
        help="repeatable success criterion (alias --success)",
    )
    ap.add_argument("--type", default="todo", choices=["todo", "project", "research", "decision"])
    ap.add_argument("--importance", default="3", choices=["1", "2", "3", "4"])
    ap.add_argument("--autonomy", default="needs-input", choices=["full", "needs-input", "blocked"])
    ap.add_argument("--effort", default="")
    ap.add_argument("--due", default="")
    ap.add_argument("--domain", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--parent", default="")
    ap.add_argument("--source", default="claude", choices=["claude", "user"])
    ap.add_argument(
        "--status",
        choices=["inbox", "ready", "parked"],
        default=None,
        help="lifecycle state to file the brief in (default: inbox)",
    )
    ap.add_argument("--ready", action="store_true", help="alias for --status ready")
    ap.add_argument(
        "--on-duplicate",
        choices=ON_DUPLICATE,
        default="merge",
        help="a capture matching an existing brief: merge into it (default), refuse, or file anyway",
    )
    ap.add_argument("--stdin", action="store_true", help="append stdin to Context")
    ap.add_argument("--commit", action="store_true", help="accepted for old callers: committing is the default")
    ap.add_argument("--no-sync", action="store_true", help="skip the git commit/push")
    ap.add_argument("--root", default=None, help="queue root (default: resolved per _paths)")
    a = ap.parse_args(argv)

    status = a.status or ("ready" if a.ready else "inbox")
    a.criteria = [c.strip() for c in a.criteria if c.strip()]
    context = a.context.strip()
    if a.stdin and not sys.stdin.isatty():
        context = (context + "\n\n" + sys.stdin.read().strip()).strip()
    if status == "ready":
        missing = [FLAGS[f] for f in missing_ready_fields(a.goal, a.criteria, context)]
        if missing:
            ap.error(
                f"a ready brief needs {', '.join(missing)}; a brief must not carry placeholder stubs "
                "(omit --ready to file it in inbox/ instead)"
            )
    root = require_queue(a.root if a.root else resolve_root())
    today = datetime.date.today().isoformat()
    repeat = None if a.on_duplicate == "file" else find_duplicate(
        root, Fingerprint.of_text(render_brief("pending", a, status, context, today))
    )
    if repeat and a.on_duplicate == "refuse":
        sys.exit(f"duplicate of {where(repeat)}")
    if repeat:
        merge_into(root, repeat, a.source, context, today)
        print(f"merged into {where(repeat)}")
        if not a.no_sync:
            _sync_commit(f"brief({repeat.id}): repeat capture merged ({a.source})", root, ids=[repeat.id])
        return
    # Atomically claim a unique filename BEFORE writing, so two sessions capturing the
    # same title at the same instant can't overwrite each other (O_CREAT|O_EXCL).
    nid = reserve_name(root / status, slug(a.title), [root / d for d in TASK_DIRS])
    fm = render_brief(nid, a, status, context, today)
    # Fill the reserved (empty) file atomically: a concurrent committer never sees a
    # half-written brief, it sees either nothing-yet or the complete file.
    path = root / status / f"{nid}.md"
    atomic_write(path, fm)
    print(f"{status}/{nid}.md")

    if not a.no_sync:
        _sync_commit(f"brief({nid}): captured ({a.source})", root, ids=[nid])


if __name__ == "__main__":
    main()  # pragma: no cover
