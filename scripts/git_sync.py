#!/usr/bin/env python3
"""
git_sync.py — commit + push the queue under the cross-process lock.

Shared commit helper for every writer that touches the queue working tree (add_task.py
for new briefs, transition.py for status changes). Routing all commits through here means
concurrent writers take turns on the git index via the same mutex, instead of colliding
on .git/index.lock and silently dropping commits.

Usage (run from anywhere):
  python scripts/git_sync.py "brief(my-id): completed" my-id

Exit codes: 0 — committed+pushed or safely deferred; 1 — bad usage.
"""
import pathlib
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _concurrency import repo_lock  # noqa: E402
from _paths import resolve_root  # noqa: E402


def _git(root: pathlib.Path, *args: str, **kw: Any) -> "subprocess.CompletedProcess[Any]":
    return subprocess.run(["git", "-C", str(root), *args], **kw)


def _brief_pathspec(brief_id: str) -> str:
    return f":(glob)*/{brief_id}.md"


def _tracked(root: pathlib.Path, *flags: str) -> list[str]:
    out = _git(root, "ls-files", "-z", *flags, capture_output=True, text=True).stdout
    return [path for path in out.split("\0") if path]


def _completed_moves(root: pathlib.Path) -> list[str]:
    """Deleted brief paths whose filename is still tracked elsewhere: a move, not a loss."""
    deleted = set(_tracked(root, "--deleted"))
    surviving = {pathlib.PurePath(p).name for p in _tracked(root) if p not in deleted}
    return [p for p in deleted if p.endswith(".md") and pathlib.PurePath(p).name in surviving]


def _stage_named_briefs(root: pathlib.Path, ids: Sequence[str]) -> None:
    for brief_id in ids:
        staged = _git(root, "add", "--all", "--", _brief_pathspec(brief_id),
                      capture_output=True, text=True)
        if staged.returncode != 0:
            print(f"  (warning: no brief file matched '{brief_id}'; nothing staged for it)")


def sync_commit(message: str, root: pathlib.Path | None = None,
                ids: Sequence[str] = ()) -> bool:
    """Commit queue changes under the cross-process lock and push.

    New and modified files and completed moves are swept in, so a deferred write still
    lands; any other deletion is staged only for the briefs named in `ids`, so a brief
    another tool removed is never lost in this writer's commit. Returns False if deferred.
    """
    root = resolve_root() if root is None else root
    with repo_lock(root) as acquired:
        if not acquired:
            print("  (sync deferred: lock held; new files and completed moves are safe on "
                  "disk and will be committed by the next writer that acquires the lock)")
            return False
        _git(root, "add", "--ignore-removal", ".")
        moves = _completed_moves(root)
        if moves:
            _git(root, "add", "--", *moves)
        _stage_named_briefs(root, ids)
        # Tolerate "nothing to commit": a concurrent holder may have already swept us in.
        _git(root, "commit", "-q", "-m", message, capture_output=True, text=True)
        if _git(root, "push", "-q", "origin", "main").returncode != 0:
            if _git(root, "pull", "--rebase", "-q", "origin", "main").returncode != 0:
                _git(root, "rebase", "--abort", capture_output=True, text=True)
                print("  (push deferred: remote diverged; committed locally — "
                      "next sync will push)")
                return False
            _git(root, "push", "-q", "origin", "main")
    return True


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("Usage: python scripts/git_sync.py '<commit-message>' [brief-id ...]",
              file=sys.stderr)
        sys.exit(1)
    sync_commit(argv[0], ids=argv[1:])


if __name__ == "__main__":
    main()  # pragma: no cover
