#!/usr/bin/env python3
"""
append_log.py — append a dated line to a brief's Execution log without touching the checkout.

`transition.py move --log` writes a log line only as part of a state change, and a write
into the queue's main checkout is refused for subagents. This commits the line from a
temporary worktree cut from origin/main, pushes it (re-cutting on a lost race), and
removes the worktree, so the main checkout's working tree, index and HEAD are never written.

Usage:
  python scripts/append_log.py <brief_id> "<line>" [--root <queue>]
"""
import argparse
import datetime
import pathlib
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

from _concurrency import atomic_write
from _paths import TASK_DIRS, require_queue, resolve_root
from git_sync import _git
from transition import _append_log

PUSH_ATTEMPTS = 5
REMOTE_MAIN = "origin/main"


def _run(root: pathlib.Path, *args: str) -> str:
    """Run git in `root`, returning stdout; a failure raises with git's own message."""
    done = _git(root, *args, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {done.stderr.strip()}")
    return str(done.stdout).strip()


@contextmanager
def temp_worktree(root: pathlib.Path) -> Iterator[pathlib.Path]:
    """A detached worktree of the queue repo at origin/main, removed on exit."""
    _run(root, "fetch", "-q", "origin", "main")
    with tempfile.TemporaryDirectory(prefix="append-log-") as tmp:
        tree = pathlib.Path(tmp) / "tree"
        _run(root, "worktree", "add", "-q", "--detach", str(tree), REMOTE_MAIN)
        try:
            yield tree
        finally:
            _git(root, "worktree", "remove", "--force", str(tree), capture_output=True)
            _git(root, "worktree", "prune", capture_output=True)


def find_in_tree(brief_id: str, tree: pathlib.Path, queue_rel: pathlib.Path) -> pathlib.Path:
    """The brief file for `brief_id` under any queue folder of the worktree."""
    for folder in TASK_DIRS:
        path = tree / queue_rel / folder / f"{brief_id}.md"
        if path.is_file():
            return path
    raise ValueError(f"no brief {brief_id!r} in {', '.join(TASK_DIRS)} on {REMOTE_MAIN}")


def _commit_line(tree: pathlib.Path, brief: pathlib.Path, line: str, today: str) -> None:
    atomic_write(brief, _append_log(brief.read_text(), today, line))
    _run(tree, "add", "--", str(brief))
    _run(tree, "commit", "-q", "-m", f"brief({brief.stem}): log")


def append_log(brief_id: str, line: str, root: pathlib.Path, today: str | None = None) -> None:
    """Commit `line` to the brief on origin/main, retrying when the push loses a race."""
    today = today or datetime.date.today().isoformat()
    top = pathlib.Path(_run(root, "rev-parse", "--show-toplevel"))
    queue_rel = root.resolve().relative_to(top.resolve())
    with temp_worktree(root) as tree:
        for _ in range(PUSH_ATTEMPTS):
            _commit_line(tree, find_in_tree(brief_id, tree, queue_rel), line, today)
            if _git(tree, "push", "-q", "origin", "HEAD:main", capture_output=True).returncode == 0:
                return
            _run(tree, "fetch", "-q", "origin", "main")
            _run(tree, "reset", "-q", "--hard", REMOTE_MAIN)
    raise RuntimeError(f"push to origin/main still rejected after {PUSH_ATTEMPTS} attempts")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Append a line to a brief's Execution log.")
    ap.add_argument("brief_id")
    ap.add_argument("line")
    ap.add_argument("--root", default=None, help="queue root (default: resolved per _paths)")
    ap.add_argument("--today", default=None, help="override date (testing)")
    a = ap.parse_args(argv)
    if "\n" in a.line or "\r" in a.line:
        print("error: line must be a single line (no newlines)", file=sys.stderr)
        sys.exit(1)
    try:
        root = require_queue(a.root) if a.root else resolve_root()
        append_log(a.brief_id, a.line, root, today=a.today)
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"{a.brief_id}: logged")


if __name__ == "__main__":
    main()  # pragma: no cover
