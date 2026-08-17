#!/usr/bin/env python3
"""
init_queue.py — scaffold a fresh task queue (backs the /tasks-init command).

Creates the lifecycle folders, a tasks.toml (which is what makes a directory a *queue*),
the brief template, and a .gitignore. Idempotent only with --force, so you can't silently
clobber an existing queue.

Usage:
  python scripts/init_queue.py ~/tasks --name my-project
  python scripts/init_queue.py ./.tasks --name my-repo --force
"""
import argparse
import os
import pathlib
import sys

from _paths import CONFIG_FILE, TASK_DIRS, is_queue

QUEUE_TEMPLATE = """\
---
id: # short-kebab-case, used as the filename
title: # one-line human title
created: # YYYY-MM-DD
updated: # YYYY-MM-DD
status: ready # inbox (raw, un-enriched) | ready | in-progress | done | parked
type: # todo | project | research | decision
importance: # 1 (critical) | 2 (high) | 3 (medium) | 4 (low)
autonomy: # full | needs-input | blocked
estimated-effort: # xs (<30min) | s (<2h) | m (<1day) | l (<1week) | xl (>1week)
due: # OPTIONAL YYYY-MM-DD hard deadline. Empty = no deadline.
domain: # OPTIONAL category. Define the allowed set in tasks.toml [domains].
tags: [] # OPTIONAL free-form labels, e.g. [security, depends:other-brief-id].
parent: # OPTIONAL brief id this is a sub-task of (epic/project). Empty for top-level.
assignee: # OPTIONAL who (person or agent) has claimed this. Empty = unclaimed. Advisory, not a lock.
source: # who created it: user | claude.
blockers: [] # list of brief ids or external blockers
related: [] # list of brief ids
requires-repo: # OPTIONAL repo slug, when the brief needs a repo other than this one.
requires-local: # OPTIONAL true, when the brief needs files only on a local machine.
---

## Goal
<!-- One paragraph. The desired END STATE, not "do X". "X exists / Y is true". -->

## Context
<!-- What a zero-context agent needs. Link repos, PRs, docs, prior briefs. -->

## Success criteria
<!-- Bulleted, concrete, checkable. Use - [ ] so the board shows progress. -->

## Constraints
<!-- What must the agent NOT do? Budget, scope, dependencies, style. -->

## Notes / open questions
<!-- Half-formed thoughts, investigations, deferred decisions. -->

## Execution log
<!-- YYYY-MM-DD: what happened. Use `ESCALATED — <reason>` when human input was
     needed mid-execution on an autonomy:full brief. -->
"""

TASKS_TOML = """\
# claude-tasks queue config. All fields optional; these are the defaults made explicit.
name = "{name}"

# Allowed values for a brief's `domain:` field. Free to leave empty.
domains = []

# Timezone for any date logic.
timezone = "UTC"
"""

GITIGNORE = """\
# generated board view
view/*.html
# sync lock + atomic-write temp files
.tasks-sync.lock
.*.tmp
# python
.venv/
__pycache__/
"""


def init_queue(root: str | os.PathLike[str], name: str | None = None, force: bool = False) -> pathlib.Path:
    """Scaffold a queue at `root`. Refuses an existing queue unless `force`."""
    root = pathlib.Path(root).expanduser()
    if is_queue(root) and not force:
        raise SystemExit(f"{root} is already a task queue (has {CONFIG_FILE}). Use --force to reinitialise.")
    for d in (*TASK_DIRS, "view"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for d in TASK_DIRS:
        keep = root / d / ".keep"
        if not keep.exists():
            keep.write_text("")
    template = root / "_template.md"
    if force or not template.exists():
        template.write_text(QUEUE_TEMPLATE)
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE)
    (root / CONFIG_FILE).write_text(TASKS_TOML.format(name=name or root.name))
    return root


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Scaffold a new task queue.")
    ap.add_argument("root", help="where to create the queue (e.g. ~/tasks or ./.tasks)")
    ap.add_argument("--name", default=None, help="project name (default: the directory name)")
    ap.add_argument("--force", action="store_true", help="reinitialise even if a queue exists")
    a = ap.parse_args(argv)
    root = init_queue(a.root, name=a.name, force=a.force)
    print(f"Initialised task queue at {root}")


if __name__ == "__main__":
    main(sys.argv[1:])  # pragma: no cover
