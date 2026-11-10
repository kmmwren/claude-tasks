#!/usr/bin/env python3
"""
git_sync.py — commit + push the queue under the cross-process lock.

Shared commit helper for every writer that touches the queue working tree (add_task.py
for new briefs, transition.py for status changes). Routing all commits through here means
concurrent writers take turns on the git index via the same mutex, instead of colliding
on .git/index.lock and silently dropping commits. Each writer commits only the briefs it names.

Usage (run from anywhere):
  python scripts/git_sync.py "brief(my-id): completed" my-id

Exit codes: 0 — committed+pushed or safely deferred; 1 — bad usage.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from typing import Any

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _concurrency import repo_lock  # noqa: E402
from _paths import TASK_DIRS, resolve_root  # noqa: E402


def _git(root: pathlib.Path, *args: str, **kw: Any) -> "subprocess.CompletedProcess[Any]":
    return subprocess.run(["git", "-C", str(root), *args], **kw)


def owns_repo(root: pathlib.Path) -> bool:
    """True when `root` is the top of its own git repo, so `add --all` stages only the queue."""
    top = _git(root, "rev-parse", "--show-toplevel", capture_output=True, text=True)
    return top.returncode == 0 and pathlib.Path(top.stdout.strip()).resolve() == root.resolve()


def _brief_pathspec(brief_id: str) -> str:
    return f":(glob)**/{brief_id}.md"


def _tracked(root: pathlib.Path, *flags: str) -> list[str]:
    out = _git(root, "ls-files", "-z", *flags, capture_output=True, text=True).stdout
    return [path for path in out.split("\0") if path]


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


def _short_log(root: pathlib.Path, rev_range: str) -> str:
    out = _git(root, "log", "--format=%h %s", rev_range, capture_output=True, text=True).stdout
    return "; ".join(out.splitlines()) or "none"


def _report_divergence(root: pathlib.Path, why: str) -> None:
    print(f"  (push deferred: {why}. Local commits kept, unpushed: {_short_log(root, 'origin/main..HEAD')}. "
          f"Remote commits not integrated: {_short_log(root, 'HEAD..origin/main')}. "
          "Resolve in a worktree, rebase onto origin/main and push.)")


LOG_HEADING = "## Execution log"


def _merge_log_conflicts(text: str) -> str | None:
    """Keep both sides of every conflict hunk, but only when each hunk sits inside the
    `## Execution log` section (append-only dated lines, so the union loses nothing).

    Returns None when a hunk is outside that section, contains a heading or the markers are
    malformed, so the caller aborts and defers as before.
    """
    out: list[str] = []
    lines = text.splitlines(keepends=True)
    in_log = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("<<<<<<< "):
            if not in_log:
                return None
            ours: list[str] = []
            theirs: list[str] = []
            side = ours
            i += 1
            while i < len(lines) and not lines[i].startswith(">>>>>>> "):
                if lines[i].startswith("||||||| "):
                    return None
                if lines[i].startswith("======="):
                    side = theirs
                else:
                    side.append(lines[i])
                i += 1
            if i >= len(lines):
                return None
            i += 1
            if any(ln.startswith("#") for ln in ours + theirs):
                return None
            out.extend(ours)
            out.extend(ln for ln in theirs if ln not in ours)
            continue
        if line.startswith("## "):
            in_log = line.rstrip("\n") == LOG_HEADING
        out.append(line)
        i += 1
    return "".join(out)


def _resolve_log_conflicts(wt: pathlib.Path) -> bool:
    """Resolve every unmerged path by keeping both Execution-log sides; all-or-nothing."""
    unmerged = _git(wt, "diff", "--name-only", "--diff-filter=U", capture_output=True, text=True).stdout.split()
    if not unmerged:
        return False
    merged: dict[str, str] = {}
    for rel in unmerged:
        stages = _git(wt, "ls-files", "-u", "--", rel, capture_output=True, text=True).stdout
        if len({ln.split()[2] for ln in stages.splitlines()}) != 3:
            return False  # delete/add conflicts are not append races
        try:
            result = _merge_log_conflicts((wt / rel).read_text())
        except (OSError, UnicodeDecodeError):
            return False
        if result is None:
            return False
        merged[rel] = result
    for rel, text in merged.items():
        (wt / rel).write_text(text)
        _git(wt, "add", "--", rel, capture_output=True, text=True)
    return True


def _rebase_resolving_logs(wt: pathlib.Path, max_steps: int = 50) -> bool:
    """Rebase onto origin/main, resolving Execution-log-only conflicts at each replayed commit.

    False when a conflict outside the log stopped it; the caller aborts the rebase.
    """
    env = {**os.environ, "GIT_EDITOR": "true"}
    if _git(wt, "rebase", "origin/main", capture_output=True, text=True, env=env).returncode == 0:
        return True
    for _ in range(max_steps):
        if not _resolve_log_conflicts(wt):
            return False
        if _git(wt, "rebase", "--continue", capture_output=True, text=True, env=env).returncode == 0:
            return True
    return False


def _integrate_remote(root: pathlib.Path) -> bool:
    """Rebase the local commits onto origin/main in a scratch worktree, push, then advance `root`.

    Nothing is stashed and the checkout's uncommitted edits are never touched: `reset --keep`
    refuses to move when an edit overlaps an incoming change.
    """
    _git(root, "fetch", "-q", "origin", "main", capture_output=True, text=True)
    with tempfile.TemporaryDirectory(prefix="git-sync-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        _git(root, "worktree", "add", "-q", "--detach", str(wt), "HEAD", capture_output=True, text=True)
        try:
            if not _rebase_resolving_logs(wt):
                conflicts = _git(wt, "diff", "--name-only", "--diff-filter=U",
                                 capture_output=True, text=True).stdout.split()
                _git(wt, "rebase", "--abort", capture_output=True, text=True)
                _report_divergence(root, f"rebase conflict in {', '.join(conflicts)}")
                return False
            if _git(wt, "push", "-q", "origin", "HEAD:main", capture_output=True, text=True).returncode != 0:
                _report_divergence(root, "origin moved again while rebasing")
                return False
        finally:
            _git(root, "worktree", "remove", "--force", str(wt), capture_output=True, text=True)
    _git(root, "fetch", "-q", "origin", "main", capture_output=True, text=True)
    if _git(root, "reset", "--keep", "origin/main", capture_output=True, text=True).returncode != 0:
        print("  (pushed, but the checkout was not advanced: uncommitted edits overlap incoming changes; "
              "commit or move them, then run: git reset --keep origin/main)")
    return True


def sync_commit(message: str, root: pathlib.Path | None = None,
                ids: Sequence[str] = (), paths: Sequence[str] = ()) -> bool:
    """Commit and push only what the caller names, under the cross-process lock.

    `ids` stage every file `<id>.md` at any depth, deletions and moves included; `paths` stage
    explicit pathspecs (`.` when the caller owns the whole tree). Anything else dirty in the
    checkout stays unstaged. Returns False if deferred; a deferred caller re-runs with the same args.
    """
    root = resolve_root() if root is None else root
    with repo_lock(root) as acquired:
        if not acquired:
            print("  (sync deferred: lock held; your files are safe on disk. "
                  "Re-run git_sync.py with the same message and ids to commit them.)")
            return False
        shadows = _shadowing_briefs(root)
        for path in shadows:
            print(f"  (refused: {path} would re-create an id already filed in another state; "
                  "sharpen the existing brief instead. Left untracked.)")
        refused = [f":(exclude){p}" for p in shadows]
        _stage_named_briefs(root, ids, refused)
        if paths:
            _git(root, "add", "--all", "--", *paths, *refused)
        # Tolerate "nothing to commit": a concurrent holder may have already swept us in.
        _git(root, "commit", "-q", "-m", message, capture_output=True, text=True)
        if _git(root, "push", "-q", "origin", "main").returncode != 0:
            return _integrate_remote(root)
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
