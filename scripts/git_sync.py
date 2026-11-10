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
from _log_merge import merge_log_appends  # noqa: E402
from _paths import TASK_DIRS, resolve_root  # noqa: E402


def _git(root: pathlib.Path, *args: str, **kw: Any) -> "subprocess.CompletedProcess[Any]":
    return subprocess.run(["git", "-C", str(root), *args], **kw)


def owns_repo(root: pathlib.Path) -> bool:
    """True when `root` is the top of its own git repo, so `add --all` stages only the queue."""
    top = _git(root, "rev-parse", "--show-toplevel", capture_output=True, text=True)
    return top.returncode == 0 and pathlib.Path(top.stdout.strip()).resolve() == root.resolve()


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


def _shadowing_briefs(root: pathlib.Path) -> list[str]:
    """New brief files whose id is already filed in another state dir, here or on origin/main.

    A writer holding a stale view (a brief closed or moved meanwhile) would otherwise
    re-create the id in its old folder, leaving it in two states at once.
    """
    _git(root, "fetch", "-q", "origin", "main", capture_output=True, text=True)
    # Only briefs origin gained since our history diverged are rivals; an older origin path
    # we already moved away locally (committed but unpushed) is not.
    remote = _git(root, "diff", "--relative", "--name-only", "--diff-filter=A", "HEAD...origin/main",
                  capture_output=True, text=True)
    moved_away = set(_tracked(root, "--deleted"))  # the old half of an in-flight move is not a rival
    filed = [pathlib.PurePath(p) for p in (*_tracked(root), *remote.stdout.splitlines())
             if p not in moved_away]
    shadows = []
    for new in _tracked(root, "--others", "--exclude-standard"):
        path = pathlib.PurePath(new)
        if path.parent.name in TASK_DIRS and path.suffix == ".md" and any(
                f.name == path.name and f.parent.name in TASK_DIRS and f.parent != path.parent
                for f in filed):
            shadows.append(new)
    return shadows


def _stage_named_briefs(root: pathlib.Path, ids: Sequence[str], refused: Sequence[str] = ()) -> None:
    for brief_id in ids:
        staged = _git(root, "add", "--all", "--", _brief_pathspec(brief_id), *refused,
                      capture_output=True, text=True)
        if staged.returncode != 0:
            print(f"  (warning: no brief file matched '{brief_id}'; nothing staged for it)")


def _stage_text(root: pathlib.Path, stage: int, path: str) -> str | None:
    """A conflicted file's content at merge stage 1 (base), 2 (upstream) or 3 (replayed commit)."""
    shown = _git(root, "show", f":{stage}:{path}", capture_output=True, text=True)
    return shown.stdout if shown.returncode == 0 else None


def _resolve_log_conflict(root: pathlib.Path, path: str) -> bool:
    """Resolve a conflicted brief as the union of both sides' log appends; False if it is not one."""
    pure = pathlib.PurePath(path)
    if pure.suffix != ".md" or pure.parent.name not in TASK_DIRS:
        return False
    base, ours, theirs = (_stage_text(root, stage, path) for stage in (1, 2, 3))
    if base is None or ours is None or theirs is None:
        return False
    merged = merge_log_appends(base, ours, theirs)
    if merged is None:
        return False
    (_toplevel(root) / path).write_text(merged)
    return _git(root, "add", "--", f":(top){path}", capture_output=True,
                text=True).returncode == 0


def _toplevel(root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(_git(root, "rev-parse", "--show-toplevel",
                             capture_output=True, text=True).stdout.strip())


def _conflicted(root: pathlib.Path) -> list[str]:
    """Unmerged paths relative to the repo top, including any outside the queue folder."""
    out: str = _git(root, "diff", "--name-only", "--diff-filter=U",
                    capture_output=True, text=True).stdout
    return out.splitlines()


def _rebase_in_progress(root: pathlib.Path) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        found: str = _git(root, "rev-parse", "--git-path", name,
                          capture_output=True, text=True).stdout.strip()
        if found and (root / found).exists():
            return True
    return False


def _pull_rebase(root: pathlib.Path) -> bool:
    """Rebase local commits onto origin/main, resolving execution-log-only conflicts.

    Any other conflict aborts the rebase (the local commits stay intact) and is reported on
    stderr by file name, so a stuck queue is never silent.
    """
    if _git(root, "pull", "--rebase", "--autostash", "-q", "origin", "main").returncode == 0:
        return True
    while _rebase_in_progress(root):
        conflicted = _conflicted(root)
        unresolved = [p for p in conflicted if not _resolve_log_conflict(root, p)]
        if not conflicted or unresolved:
            _git(root, "rebase", "--abort", capture_output=True, text=True)
            names = ", ".join(unresolved) or "none reported by git"
            print(f"WARNING: queue sync cannot rebase onto origin/main; unresolved conflict in: "
                  f"{names}. Resolve by hand in {root}; nothing will publish until then.",
                  file=sys.stderr)
            return False
        cont = _git(root, "-c", "core.editor=true", "rebase", "--continue",
                    capture_output=True, text=True)
        if cont.returncode == 0:
            return True
    return False


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
        shadows = _shadowing_briefs(root)
        for path in shadows:
            print(f"  (refused: {path} would re-create an id already filed in another state; "
                  "sharpen the existing brief instead. Left untracked.)")
        refused = [f":(exclude){p}" for p in shadows]
        _git(root, "add", "--ignore-removal", ".", *refused)
        moves = _completed_moves(root)
        if moves:
            _git(root, "add", "--", *moves)
        _stage_named_briefs(root, ids, refused)
        # Tolerate "nothing to commit": a concurrent holder may have already swept us in.
        _git(root, "commit", "-q", "-m", message, capture_output=True, text=True)
        if _git(root, "push", "-q", "origin", "main").returncode != 0:
            if not _pull_rebase(root):
                print("  (push deferred: remote diverged; committed locally — "
                      "next sync will push)")
                return False
            _git(root, "push", "-q", "origin", "main")
    return True


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    # A flag in the message slot would otherwise be committed and pushed as the message.
    if not argv or argv[0].startswith("-"):
        print("Usage: python scripts/git_sync.py '<commit-message>' [brief-id ...]",
              file=sys.stderr)
        sys.exit(0 if argv and argv[0] in ("-h", "--help") else 1)
    sync_commit(argv[0], ids=argv[1:])


if __name__ == "__main__":
    main()  # pragma: no cover
