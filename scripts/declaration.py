"""
declaration.py: write what an open brief waits on, in place, without moving it.

check_deps reports a ready, in-progress or parked brief with no `blocked_by:` as undeclared;
`transition.py declare` is the fix. `[]` records the judgement that the brief waits on nothing.
A ready brief that does wait belongs in blocked/, so that case is refused and pointed at `move`.

Stdlib only; every git write goes through git_sync.sync_commit.
"""

import argparse
import datetime
import pathlib
import sys

from _concurrency import atomic_write, repo_lock
from deps import parse_list
from git_sync import sync_commit
from transition import _append_log, _check_blockers, _update_frontmatter, find_brief

# Where check_deps reports an undeclared brief, so where a declaration is written in place.
DECLARABLE_STATUSES = ("ready", "in-progress", "parked")


def _require_declarable(brief_id: str, status: str, blocked_by: list[str]) -> None:
    if status not in DECLARABLE_STATUSES:
        raise ValueError(
            f"{brief_id!r} is in {status}/; only {', '.join(DECLARABLE_STATUSES)} briefs are declared in place"
        )
    if blocked_by and status == "ready":
        raise ValueError(
            f"{brief_id!r} waits on {blocked_by}; use `move {brief_id} blocked --blocked-by "
            f"{','.join(blocked_by)}` so it leaves ready/"
        )


def declare_brief(
    brief_id: str,
    blocked_by: list[str],
    root: pathlib.Path,
    log: str | None = None,
    today: str | None = None,
) -> pathlib.Path:
    """Write the brief's `blocked_by` under the queue lock and log it; the brief stays in its folder."""
    path = find_brief(brief_id, root)
    if path is None:
        raise ValueError(f"no brief with id {brief_id!r}")
    _require_declarable(brief_id, path.parent.name, blocked_by)
    _check_blockers(brief_id, blocked_by, root)
    today = today or datetime.date.today().isoformat()
    message = log or f"declared blocked_by: [{', '.join(blocked_by)}]"
    with repo_lock(root) as acquired:
        if not acquired:
            raise RuntimeError(
                f"timed out waiting for the queue lock; {brief_id!r} was NOT declared"
            )
        if not path.exists():
            raise ValueError(f"no brief with id {brief_id!r} (moved by another actor)")
        text = _update_frontmatter(path.read_text(), path.parent.name, today, blocked_by=blocked_by)
        atomic_write(path, _append_log(text, today, message))
    return path


def run_declare(a: argparse.Namespace, root: pathlib.Path) -> None:
    """The `transition.py declare` command: declare, commit, and say what was written."""
    blocked_by = [] if a.no_deps else parse_list(a.blocked_by)
    if not (a.no_deps or blocked_by):
        sys.exit("error: --blocked-by needs at least one id or external:<reason>")
    try:
        path = declare_brief(a.brief_id, blocked_by, root, log=a.log, today=a.today)
    except ValueError as e:
        sys.exit(f"error: {e}")
    if not a.no_sync:
        sync_commit(f"brief({a.brief_id}): declare dependencies", root, ids=[a.brief_id])
    print(f"{a.brief_id} declared blocked_by: [{', '.join(blocked_by)}]: {path}")
