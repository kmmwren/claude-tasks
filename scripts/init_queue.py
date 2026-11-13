#!/usr/bin/env python3
"""
init_queue.py — scaffold a fresh task queue (backs the /tasks-init command).

Creates the lifecycle folders, a tasks.toml (which is what makes a directory a *queue*),
the brief template, and a .gitignore. Idempotent only with --force, so you can't silently
clobber an existing queue.

With --session, the queue is session-linked: one long-running Claude Code session owns it,
the session is bound to it (so resolve_root finds it), and fold_session.py folds its open
briefs into --fold-to when the session ends.

Usage:
  python scripts/init_queue.py ~/tasks --name my-project
  python scripts/init_queue.py ./.tasks --name my-repo --force
  python scripts/init_queue.py ./.tasks --name panaseer-lite --prefix LITE
  python scripts/init_queue.py ~/q/s1 --session "$CLAUDE_CODE_SESSION_ID" --fold-to ~/tasks

It also scaffolds requirements.md, the register a large project's requirements are extracted
into (see the requirements-extract skill). An existing register is never overwritten.
"""

import argparse
import json
import os
import pathlib
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from _concurrency import atomic_write
from _paths import CONFIG_FILE, TASK_DIRS, binding_path, is_queue, require_queue
from requirements_register import REGISTER_FILE, stub

QUEUE_TEMPLATE = """\
---
id: # short-kebab-case, used as the filename
title: # one-line human title
created: # YYYY-MM-DD
updated: # YYYY-MM-DD
status: ready # inbox (raw, un-enriched) | ready | in-progress | blocked | done | parked
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
blockers: [] # list of brief ids or external blockers (original form; blocked_by is preferred)
blocks: [] # OPTIONAL ids of briefs this one blocks (the inverse of their blockers:); each open one raises this brief's rank.
blocked_by: [] # required while status is blocked: brief ids or external:<reason>; cleared on unblock.
clears: # OPTIONAL fleet-blocker id (or [ids]) this brief clears; while its record is active or unverified, prioritise.py ranks the brief first. Needs tasks.toml `fleet_blockers_dir`.
milestone: # OPTIONAL id from the project's milestones file; only checked if tasks.toml sets `milestones`.
related: [] # list of brief ids
requirements: [] # OPTIONAL requirement ids from requirements.md this brief covers, e.g. [LITE-R001].
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

# OPTIONAL. Path (relative to the directory holding this queue) of a milestones file the
# project owns. Unset, the queue has no milestones and `milestone:` is ignored.
# milestones = "docs/milestones.toml"

# OPTIONAL. Directory of fleet-blocker records, one <id>.json each (id, summary, status
# active|cleared|unverified, ...). A brief whose `clears:` names an active or unverified
# blocker ranks first. Unset, the feature is off. Relative paths resolve beside the queue.
# fleet_blockers_dir = "fleet-blockers"
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


@dataclass(frozen=True)
class SessionLink:
    """The session that owns a queue, and the queue its open briefs fold into."""

    id: str
    fold_to: pathlib.Path

    def toml(self) -> str:
        fold_to = json.dumps(str(self.fold_to))
        return (
            f'\n[session]\nid = {json.dumps(self.id)}\nlifecycle = "session"\nfold_to = {fold_to}\n'
        )

    def bind(self, root: pathlib.Path, env: Mapping[str, str] | None = None) -> pathlib.Path:
        path = binding_path(self.id, env)
        atomic_write(path, f"root = {json.dumps(str(root.resolve()))}\n")
        return path


def default_prefix(name: str) -> str:
    """Requirement id prefix from a project name: initials of a multi-word name, else its first 4 letters."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
    raw = "".join(w[0] for w in words) if len(words) > 1 else "".join(words)[:4]
    return raw.upper().lstrip("0123456789") or "REQ"


def init_queue(
    root: str | os.PathLike[str],
    name: str | None = None,
    force: bool = False,
    session: SessionLink | None = None,
    env: Mapping[str, str] | None = None,
    prefix: str | None = None,
) -> pathlib.Path:
    """Scaffold a queue at `root`. Refuses an existing queue unless `force`."""
    root = pathlib.Path(root).expanduser()
    if is_queue(root) and not force:
        raise SystemExit(
            f"{root} is already a task queue (has {CONFIG_FILE}). Use --force to reinitialise."
        )
    project = name or root.name
    chosen = (prefix or default_prefix(project)).upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]*", chosen):
        raise SystemExit(
            f"--prefix must be letters and digits starting with a letter, got {chosen!r}"
        )
    if session is not None:
        require_queue(session.fold_to)
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
    register = root / REGISTER_FILE
    if not register.exists():
        register.write_text(stub(project, chosen))
    config = TASKS_TOML.format(name=project)
    (root / CONFIG_FILE).write_text(config + (session.toml() if session else ""))
    if session is not None:
        session.bind(root, env)
    return root


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Scaffold a new task queue.")
    ap.add_argument("root", help="where to create the queue (e.g. ~/tasks or ./.tasks)")
    ap.add_argument("--name", default=None, help="project name (default: the directory name)")
    ap.add_argument("--prefix", default=None, help="requirement id prefix (default: from the name)")
    ap.add_argument("--force", action="store_true", help="reinitialise even if a queue exists")
    ap.add_argument("--session", default=None, help="Claude Code session id that owns this queue")
    ap.add_argument(
        "--fold-to", default=None, help="queue the open briefs fold into at session end"
    )
    a = ap.parse_args(argv)
    if bool(a.session) != bool(a.fold_to):
        ap.error("--session and --fold-to go together")
    session = (
        SessionLink(a.session, pathlib.Path(a.fold_to).expanduser().resolve())
        if a.session
        else None
    )
    root = init_queue(a.root, name=a.name, force=a.force, session=session, prefix=a.prefix)
    print(f"Initialised task queue at {root}")


if __name__ == "__main__":
    main(sys.argv[1:])  # pragma: no cover
