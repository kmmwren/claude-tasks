#!/usr/bin/env python3
"""
git_sync.py — commit the queue under the cross-process lock, then push it coalesced.

Shared commit helper for every writer that touches the queue working tree (add_task.py
for new briefs, transition.py for status changes). Routing all commits through here means
concurrent writers take turns on the git index via the same mutex, instead of colliding
on .git/index.lock and silently dropping commits. Each writer commits only the briefs it names.

The commit is synchronous; the push is not. A writer marks the clone dirty and returns. At
most one detached pusher runs per clone: it waits for in-flight writers to land, pushes once,
and pushes again only if a write marked the clone dirty meanwhile. A burst of writes from
concurrent sessions therefore costs one or two `git push` processes, not one per write.

Usage (run from anywhere):
  python scripts/git_sync.py "brief(my-id): completed" my-id
  python scripts/git_sync.py --sweep [queue-root]   # commit+push everything dirty (timer job)
  python scripts/git_sync.py --push-pending ROOT --lock-fd N   # the detached pusher (internal)

`--sweep` is the same helper over the whole tree: it catches files a session edited by hand,
which no scripted writer could name. A queue root that is not the top of its own git repo
(a project's `.tasks/`) is never synced here; its repo ships it.

Exit codes: 0 — committed (push in the background) or safely deferred; 1 — bad usage.
"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from typing import Any

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _concurrency import file_lock, lock_path, repo_lock, try_lock  # noqa: E402
from _paths import TASK_DIRS, resolve_root  # noqa: E402


def _git(root: pathlib.Path, *args: str, **kw: Any) -> "subprocess.CompletedProcess[Any]":
    return subprocess.run(["git", "-C", str(root), *args], **kw)


def owns_repo(root: pathlib.Path) -> bool:
    """True when `root` is the top of its own git repo, so `add --all` stages only the queue."""
    top = _git(root, "rev-parse", "--show-toplevel", capture_output=True, text=True)
    return top.returncode == 0 and pathlib.Path(top.stdout.strip()).resolve() == root.resolve()


# High-signal credential shapes only: a sweep stages whole files, so one that carries a key stays local.
_SECRET = re.compile(
    r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)


def _split_z(output: str) -> list[str]:
    return [p for p in output.split("\0") if p]


def _secret_bearing(root: pathlib.Path, paths: Sequence[str]) -> list[str]:
    """Changed or new files under `paths` that hold a credential."""
    out = _git(root, "ls-files", "-z", "--modified", "--others", "--exclude-standard", "--", *paths,
               capture_output=True, text=True).stdout
    found = []
    for rel in _split_z(out):
        try:
            if _SECRET.search((root / rel).read_text(errors="ignore")):
                found.append(rel)
        except OSError:
            continue  # deleted or unreadable: nothing to leak
    return found


def _staged_secret_bearing(root: pathlib.Path) -> list[str]:
    """Files already in the index whose staged content holds a credential: `git commit` takes them all."""
    out = _git(root, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR",
               capture_output=True, text=True).stdout
    staged = (
        (rel, _git(root, "show", f":{rel}", capture_output=True).stdout.decode(errors="ignore"))
        for rel in _split_z(out)
    )
    return [rel for rel, content in staged if _SECRET.search(content)]


def _unstage(root: pathlib.Path, rels: Sequence[str]) -> None:
    """Drop `rels` from the pending commit; the files stay on disk."""
    if rels:
        _git(root, "reset", "-q", "--", *(f":(literal){rel}" for rel in rels))


def _brief_pathspec(brief_id: str) -> str:
    return f":(glob)**/{brief_id}.md"


def _tracked(root: pathlib.Path, *flags: str) -> list[str]:
    out = _git(root, "ls-files", "-z", *flags, capture_output=True, text=True).stdout
    return [path for path in out.split("\0") if path]


FETCH_MAX_AGE = 60.0  # seconds a fetched origin/main stays fresh enough for the shadow check


def _fetch_head(root: pathlib.Path) -> pathlib.Path:
    git = root / ".git"
    if git.is_dir():
        return git / "FETCH_HEAD"
    out = _git(root, "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD",
               capture_output=True, text=True).stdout.strip()
    return pathlib.Path(out)


def _fetch_if_stale(root: pathlib.Path) -> None:
    """Fetch origin/main unless the last fetch is under FETCH_MAX_AGE old, so a burst fetches once."""
    try:
        if time.time() - _fetch_head(root).stat().st_mtime < FETCH_MAX_AGE:
            return
    except OSError:
        pass  # never fetched
    _git(root, "fetch", "-q", "origin", "main", capture_output=True, text=True)


def _shadowing_briefs(root: pathlib.Path) -> list[str]:
    """New brief files whose id is already filed in another state dir, here or on origin/main.

    A writer holding a stale view (a brief closed or moved meanwhile) would otherwise
    re-create the id in its old folder, leaving it in two states at once. The origin/main view
    may be up to FETCH_MAX_AGE old: a rival pushed inside that window is not seen.
    """
    _fetch_if_stale(root)
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
            if _git(wt, "rebase", "origin/main", capture_output=True, text=True).returncode != 0:
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


# Per-clone state of the coalesced pusher, all beside the sync lock inside .git.
PUSH_LOCK = ".tasks-push.lock"  # held by the one running pusher
WRITERS_LOCK = ".tasks-writers.lock"  # shared by every writer between commit start and dirty mark
DIRTY = ".tasks-push.dirty"  # a commit landed that no push has started from yet
PUSH_LOG = ".tasks-push.log"  # the detached pusher's output: divergence reports land here
SETTLE_MAX = 10.0  # longest a pusher waits for in-flight writers before pushing anyway
PUSH_WAIT = 120.0  # longest a `wait=True` caller waits for a running pusher


def _mark_dirty(root: pathlib.Path) -> None:
    lock_path(root, DIRTY).touch()


def _is_dirty(root: pathlib.Path) -> bool:
    return lock_path(root, DIRTY).exists()


def _take_dirty(root: pathlib.Path) -> bool:
    """Clear the dirty mark; True if it was set. Cleared before pushing, so a later mark means a later commit."""
    try:
        lock_path(root, DIRTY).unlink()
    except FileNotFoundError:
        return False
    return True


def _push(root: pathlib.Path) -> bool:
    """Push main; on rejection rebase onto origin under the queue lock and push again."""
    if _git(root, "push", "-q", "origin", "main").returncode == 0:
        return True
    with repo_lock(root) as acquired:
        if not acquired:
            print("  (push deferred: lock held; the next write or sweep pushes these commits.)")
            return False
        return _integrate_remote(root)


def _drain(root: pathlib.Path) -> bool:
    """Push until no commit is waiting; the caller holds the push lock. Returns the last push's result."""
    pushed = True
    while True:
        # Let writers already committing land first, so one push carries the whole burst.
        with file_lock(lock_path(root, WRITERS_LOCK), timeout=SETTLE_MAX, poll=0.05):
            pass
        if not _take_dirty(root):
            return pushed
        pushed = _push(root)


def _release(fd: int) -> None:
    os.close(fd)  # closing the last fd on the lock's open file releases the flock


def _run_pusher(root: pathlib.Path, fd: int | None) -> None:
    """Drain while holding the push lock `fd`; re-take it if a write slipped in as it was released."""
    while fd is not None:
        try:
            _drain(root)
        finally:
            _release(fd)
        fd = try_lock(lock_path(root, PUSH_LOCK)) if _is_dirty(root) else None


def _report_last_push(root: pathlib.Path) -> None:
    """Show the writer what the previous detached pusher reported, then clear it; the caller holds the push lock."""
    log = lock_path(root, PUSH_LOG)
    report = log.read_text().strip() if log.exists() else ""
    if report:
        print(f"  (previous background push reported:)\n{report}")
        log.write_text("")


def _spawn_pusher(root: pathlib.Path, fd: int) -> None:
    """Hand the held push lock to a detached pusher process, so the caller returns at once."""
    try:
        _report_last_push(root)
        with open(lock_path(root, PUSH_LOG), "a") as log:
            subprocess.Popen(
                [sys.executable, str(SCRIPTS_DIR / "git_sync.py"), "--push-pending", str(root),
                 "--lock-fd", str(fd)],
                pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    finally:
        _release(fd)


def request_push(root: pathlib.Path) -> bool:
    """Start a detached pusher unless one is running; a running one picks up the dirty mark."""
    fd = try_lock(lock_path(root, PUSH_LOCK))
    if fd is not None:
        _spawn_pusher(root, fd)
    return True


def push_now(root: pathlib.Path) -> bool:
    """Push synchronously, after any running pusher, for callers that need origin current."""
    with file_lock(lock_path(root, PUSH_LOCK), timeout=PUSH_WAIT) as acquired:
        if not acquired:
            print("  (push deferred: another pusher still running; it will push these commits.)")
            return False
        _report_last_push(root)
        _mark_dirty(root)
        pushed = _drain(root)
    if _is_dirty(root):  # a write that found the lock held just before it was released
        request_push(root)
    return pushed


def wait_for_push(root: pathlib.Path, timeout: float = 60.0, poll: float = 0.05) -> bool:
    """Block until no pusher runs and no commit awaits a push; False if `timeout` passed first.

    For callers and tests that must observe origin after a detached push.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fd = None if _is_dirty(root) else try_lock(lock_path(root, PUSH_LOCK))
        if fd is not None:
            _release(fd)
            if not _is_dirty(root):
                return True
        time.sleep(poll)
    return False


def _commit(message: str, root: pathlib.Path, ids: Sequence[str], paths: Sequence[str]) -> bool:
    """Stage what the caller names and commit it under the queue lock; False if the lock timed out."""
    with repo_lock(root) as acquired:
        if not acquired:
            print("  (sync deferred: lock held; your files are safe on disk. "
                  "Re-run git_sync.py with the same message and ids to commit them.)")
            return False
        shadows = _shadowing_briefs(root)
        for path in shadows:
            print(f"  (refused: {path} would re-create an id already filed in another state; "
                  "sharpen the existing brief instead. Left untracked.)")
        staged_leaks = _staged_secret_bearing(root)
        _unstage(root, staged_leaks)
        leaks = list(dict.fromkeys([*staged_leaks, *(_secret_bearing(root, paths) if paths else [])]))
        if leaks:  # a count, not names: the paths come from scanning credential-bearing files
            print(f"  (refused: {len(leaks)} held back for looking like a credential. "
                  "Left uncommitted; `git status` lists them. Remove the secret, then re-sync.)")
        refused = [f":(exclude){p}" for p in (*shadows, *leaks)]
        _stage_named_briefs(root, ids, refused)
        if paths:
            _git(root, "add", "--all", "--", *paths, *refused)
        # Tolerate "nothing to commit": a concurrent holder may have already swept us in.
        _git(root, "commit", "-q", "-m", message, capture_output=True, text=True)
    return True


def sync_commit(message: str, root: pathlib.Path | None = None,
                ids: Sequence[str] = (), paths: Sequence[str] = (), wait: bool = False) -> bool:
    """Commit only what the caller names under the cross-process lock, then push coalesced.

    `ids` stage every file `<id>.md` at any depth, deletions and moves included; `paths` stage
    explicit pathspecs (`.` when the caller owns the whole tree). Anything else dirty in the
    checkout stays unstaged. The push runs detached and True means committed; `wait=True`
    pushes before returning and reports the push. Returns False if deferred; a deferred caller
    re-runs with the same args.
    """
    root = resolve_root() if root is None else root
    if not owns_repo(root):
        return False  # a project queue lives inside another repo, which ships it
    # Shared while committing, so a pusher waiting for exclusive access waits for the whole burst.
    with file_lock(lock_path(root, WRITERS_LOCK), shared=True):
        if not _commit(message, root, ids, paths):
            return False
        _mark_dirty(root)
    return push_now(root) if wait else request_push(root)


def sweep(root: pathlib.Path | None = None) -> bool:
    """Commit and push every uncommitted change in the queue: the net under writers that never call sync_commit."""
    return sync_commit("queue: sync uncommitted briefs", root, paths=(".",))


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--sweep":
        sweep(pathlib.Path(argv[1]).expanduser() if len(argv) > 1 else None)
        return
    if argv[:1] == ["--push-pending"] and argv[2:3] == ["--lock-fd"]:
        _run_pusher(pathlib.Path(argv[1]), int(argv[3]))
        return
    # A flag in the message slot would otherwise be committed and pushed as the message.
    if not argv or argv[0].startswith("-"):
        print("Usage: python scripts/git_sync.py '<commit-message>' [brief-id ...]",
              file=sys.stderr)
        sys.exit(0 if argv and argv[0] in ("-h", "--help") else 1)
    sync_commit(argv[0], ids=argv[1:])


if __name__ == "__main__":
    main()  # pragma: no cover
