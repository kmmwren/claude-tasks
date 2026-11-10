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
import pathlib
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from typing import Any

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _concurrency import repo_lock  # noqa: E402
from _log_merge import merge_log_appends  # noqa: E402
from _paths import TASK_DIRS, resolve_root  # noqa: E402


def _git(root: pathlib.Path, *args: str, **kw: Any) -> "subprocess.CompletedProcess[Any]":
    return subprocess.run(["git", "-C", str(root), *args], **kw)


def _toplevel(root: pathlib.Path) -> pathlib.Path | None:
    top = _git(root, "rev-parse", "--show-toplevel", capture_output=True, text=True)
    return pathlib.Path(top.stdout.strip()) if top.returncode == 0 else None


def owns_repo(root: pathlib.Path) -> bool:
    """True when `root` is the top of its own git repo, so `add --all` stages only the queue."""
    top = _toplevel(root)
    return top is not None and top.resolve() == root.resolve()


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


def _stage_text(root: pathlib.Path, stage: int, path: str) -> str | None:
    """A conflicted file's content at merge stage 1 (base), 2 (upstream) or 3 (replayed commit)."""
    shown = _git(root, "show", f":{stage}:{path}", capture_output=True, text=True,
                 encoding="utf-8")
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
    top = _toplevel(root)
    if merged is None or top is None:
        return False
    (top / path).write_text(merged, encoding="utf-8")
    return _git(root, "add", "--", f":(top){path}", capture_output=True,
                text=True).returncode == 0


def _conflicted(root: pathlib.Path) -> list[str]:
    """Unmerged paths relative to the repo top, including any outside the queue folder."""
    out: str = _git(root, "diff", "-z", "--name-only", "--diff-filter=U",
                    capture_output=True, text=True).stdout
    return [path for path in out.split("\0") if path]


def _rebase_in_progress(root: pathlib.Path) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        found: str = _git(root, "rev-parse", "--git-path", name,
                          capture_output=True, text=True).stdout.strip()
        if found and (root / found).exists():
            return True
    return False


def _rebase_keeping_log_union(wt: pathlib.Path) -> list[str] | None:
    """Rebase `wt` onto origin/main, resolving conflicts that are only Execution-log appends.

    None on success; otherwise the paths git could not merge (empty when it named none).
    """
    run = _git(wt, "rebase", "origin/main", capture_output=True, text=True)
    while run.returncode != 0:
        if not _rebase_in_progress(wt):
            return []
        conflicted = _conflicted(wt)
        unresolved = [path for path in conflicted if not _resolve_log_conflict(wt, path)]
        if unresolved or not conflicted:
            return unresolved
        run = _git(wt, "-c", "core.editor=true", "rebase", "--continue",
                   capture_output=True, text=True)
    return None


def _short_log(root: pathlib.Path, rev_range: str) -> str:
    out = _git(root, "log", "--format=%h %s", rev_range, capture_output=True, text=True).stdout
    return "; ".join(out.splitlines()) or "none"


def _report_divergence(root: pathlib.Path, why: str) -> None:
    print(f"  (push deferred: {why}. Local commits kept, unpushed: {_short_log(root, 'origin/main..HEAD')}. "
          f"Remote commits not integrated: {_short_log(root, 'HEAD..origin/main')}. "
          "Resolve in a worktree, rebase onto origin/main and push.)")


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
            stuck = _rebase_keeping_log_union(wt)
            if stuck is not None:
                _git(wt, "rebase", "--abort", capture_output=True, text=True)
                names = ", ".join(stuck) or "none reported by git"
                print(f"WARNING: queue sync cannot rebase onto origin/main; unresolved conflict in: "
                      f"{names}. Resolve by hand in {root}; nothing will publish until then.",
                      file=sys.stderr)
                _report_divergence(root, f"rebase conflict in {names}")
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
