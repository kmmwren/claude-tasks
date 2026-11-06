#!/usr/bin/env python3
"""
append_log.py — append a dated execution-log line to a brief without touching the main checkout.

Subagents are refused writes in the queue's main checkout (block-main-checkout-writes), and
`transition.py move --log` only logs as part of a state change. This fetches origin/main into a
temporary worktree, appends the line there, commits, pushes and removes the worktree, so the
main checkout is never written.

Usage:
  python scripts/append_log.py <brief-id> "<line>" [--root DIR]

Exit codes: 0 logged; 1 unknown brief; 2 git failure.
"""
import argparse
import datetime
import pathlib
import subprocess
import sys
import tempfile

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _paths import resolve_root  # noqa: E402
from git_sync import _git  # noqa: E402
from transition import BRIEF_DIRS, _append_log  # noqa: E402

PUSH_ATTEMPTS = 4
IDENTITY = ("-c", "user.name=claude-tasks", "-c", "user.email=noreply@anthropic.com")


def _run(repo: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return _git(repo, *args, capture_output=True, text=True)


def _queue_prefix(root: pathlib.Path) -> pathlib.Path:
    """The queue's path inside its repo (empty when the queue is the repo top)."""
    top = pathlib.Path(_run(root, "rev-parse", "--show-toplevel").stdout.strip())
    return root.resolve().relative_to(top.resolve())


def _find(queue: pathlib.Path, brief_id: str) -> pathlib.Path | None:
    for d in (*BRIEF_DIRS, "inbox"):
        path = queue / d / f"{brief_id}.md"
        if path.exists():
            return path
    return None


def _try_push(wt: pathlib.Path, queue: pathlib.Path, brief_id: str, line: str, today: str) -> str | None:
    """Append, commit and push in `wt`; return the pushed sha, or None when the push lost a race."""
    path = _find(queue, brief_id)
    if path is None:
        raise ValueError(f"no brief with id {brief_id!r}")
    path.write_text(_append_log(path.read_text(), today, line))
    _run(wt, "add", str(path.relative_to(wt)))
    commit = _run(wt, *IDENTITY, "commit", "-m", f"brief({brief_id}): log line")
    if commit.returncode != 0:
        raise RuntimeError(f"commit failed: {commit.stdout.strip()} {commit.stderr.strip()}")
    if _run(wt, "push", "origin", "HEAD:main").returncode != 0:
        return None
    return _run(wt, "rev-parse", "HEAD").stdout.strip()


def append_log(brief_id: str, line: str, root: pathlib.Path, today: str | None = None) -> str:
    """Append `line` to the brief's Execution log on origin/main; return the pushed commit sha."""
    today = today or datetime.date.today().isoformat()
    prefix = _queue_prefix(root)
    with tempfile.TemporaryDirectory(prefix="append-log-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        for _ in range(PUSH_ATTEMPTS):
            if _run(root, "fetch", "origin", "main").returncode != 0:
                raise RuntimeError("git fetch origin main failed")
            added = _run(root, "worktree", "add", "--detach", str(wt), "origin/main")
            if added.returncode != 0:
                raise RuntimeError(f"worktree add failed: {added.stderr.strip()}")
            try:
                sha = _try_push(wt, wt / prefix, brief_id, line, today)
            finally:
                _run(root, "worktree", "remove", "--force", str(wt))
            if sha:
                return sha
        raise RuntimeError(f"push to origin/main still rejected after {PUSH_ATTEMPTS} attempts")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Append a dated execution-log line to a brief.")
    ap.add_argument("brief_id")
    ap.add_argument("line")
    ap.add_argument("--root", default=None)
    a = ap.parse_args(argv)
    root = pathlib.Path(a.root) if a.root else resolve_root()
    try:
        sha = append_log(a.brief_id, a.line, root)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"logged to {a.brief_id} ({sha[:9]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
