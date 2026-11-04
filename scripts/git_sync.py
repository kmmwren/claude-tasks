#!/usr/bin/env python3
"""
git_sync.py — commit + push the queue under the cross-process lock.

Shared commit helper for every writer that touches the queue working tree (add_task.py
for new briefs, transition.py for status changes). Routing all commits through here means
concurrent writers take turns on the git index via the same mutex, instead of colliding
on .git/index.lock and silently dropping commits.

Usage (run from anywhere):
  python scripts/git_sync.py "brief(my-id): completed"

Exit codes: 0 — committed+pushed or safely deferred; 1 — bad usage.
"""
import pathlib
import subprocess
import sys
from typing import Any

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _concurrency import repo_lock  # noqa: E402
from _paths import resolve_root  # noqa: E402


def _git(root: pathlib.Path, *args: str, **kw: Any) -> "subprocess.CompletedProcess[Any]":
    return subprocess.run(["git", "-C", str(root), *args], **kw)


def sync_commit(message: str, root: pathlib.Path | None = None) -> bool:
    """Commit all queue changes under the cross-process lock and push.

    Stages the whole working tree with `git add --all` (respecting .gitignore), so
    new top-level files and dirs are committed rather than silently left dirty. It is a
    self-healing sweep — any file written to disk but not yet committed gets picked up;
    unique brief filenames mean we never grab another session's in-flight work (that
    session is blocked on the same lock). .gitignore is the guard for generated/secret
    files (view/*.html, the lock, .env, …).

    Returns True if the commit+push succeeded, False if deferred (lock timeout or remote
    divergence). Deferred means the changes are safe on disk; the next call sweeps them in.
    """
    root = resolve_root() if root is None else root
    with repo_lock(root) as acquired:
        if not acquired:
            print("  (sync deferred: lock held; changes are safe on disk and will be "
                  "committed by the next writer that acquires the lock)")
            return False
        _git(root, "add", "--all")
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
        print("Usage: python scripts/git_sync.py '<commit-message>'", file=sys.stderr)
        sys.exit(1)
    sync_commit(argv[0])


if __name__ == "__main__":
    main()  # pragma: no cover
