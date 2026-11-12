#!/usr/bin/env python3
"""
git_sync.py — commit the queue under the cross-process lock, then push it coalesced.

Usage (run from anywhere):
  python scripts/git_sync.py "brief(my-id): completed" my-id
  python scripts/git_sync.py --sweep [queue-root]   # commit+push everything dirty (timer job)
  python scripts/git_sync.py --push-pending ROOT --lock-fd N   # the detached pusher (internal)

A named write commits only its briefs and pushes in the background; `--sweep` commits every
uncommitted file and pushes before returning, within SWEEP_BUDGET seconds. A queue root that is
not the top of its own git repo is never synced here.

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

from _concurrency import (  # noqa: E402
    clone_state_path,
    exclusive_lock,
    repo_lock,
    shared_lock,
    try_lock,
)
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
    out = _git(
        root,
        "ls-files",
        "-z",
        "--modified",
        "--others",
        "--exclude-standard",
        "--",
        *paths,
        capture_output=True,
        text=True,
    ).stdout
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
    out = _git(
        root,
        "diff",
        "--cached",
        "--name-only",
        "-z",
        "--diff-filter=ACMR",
        capture_output=True,
        text=True,
    ).stdout
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


FETCH_MAX_AGE = 45.0  # under the 60 s sweep interval, so every timed sweep fetches
FETCH_TIMEOUT = 20.0  # a hung fetch must not spend the sweep's whole budget


def _fetch_head(root: pathlib.Path) -> pathlib.Path | None:
    """The clone's FETCH_HEAD; None when git cannot name it, so the caller fetches."""
    git = root / ".git"
    if git.is_dir():
        return git / "FETCH_HEAD"
    out = _git(
        root,
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "FETCH_HEAD",
        capture_output=True,
        text=True,
    ).stdout.strip()
    return pathlib.Path(out) if out else None


def _timeout(deadline: float | None, cap: float | None = None) -> float | None:
    """Seconds a subprocess may run: what is left before `deadline` (monotonic), at most `cap`."""
    limits = [cap] if cap is not None else []
    if deadline is not None:
        limits.append(max(0.0, deadline - time.monotonic()))
    return min(limits) if limits else None


def _fetch_if_stale(root: pathlib.Path) -> None:
    """Fetch origin/main unless the last fetch is under FETCH_MAX_AGE old."""
    fetch_head = _fetch_head(root)
    try:
        if fetch_head and time.time() - fetch_head.stat().st_mtime < FETCH_MAX_AGE:
            return
    except OSError:
        pass  # never fetched
    try:
        _git(root, "fetch", "-q", "origin", "main", capture_output=True, timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"  (fetch timed out after {FETCH_TIMEOUT:.0f}s; using origin/main as last fetched.)")


def _shadowing_briefs(root: pathlib.Path) -> list[str]:
    """New brief files whose id is already filed in another state dir, here or on origin/main.

    Reads origin/main as last fetched, so a rival pushed since is not seen until the next sweep.
    """
    # Only briefs origin gained since our history diverged are rivals; an older origin path
    # we already moved away locally (committed but unpushed) is not.
    remote = _git(
        root,
        "diff",
        "--relative",
        "--name-only",
        "--diff-filter=A",
        "HEAD...origin/main",
        capture_output=True,
        text=True,
    )
    moved_away = set(
        _tracked(root, "--deleted")
    )  # the old half of an in-flight move is not a rival
    filed = [
        pathlib.PurePath(p)
        for p in (*_tracked(root), *remote.stdout.splitlines())
        if p not in moved_away
    ]
    shadows = []
    for new in _tracked(root, "--others", "--exclude-standard"):
        path = pathlib.PurePath(new)
        if (
            path.parent.name in TASK_DIRS
            and path.suffix == ".md"
            and any(
                f.name == path.name and f.parent.name in TASK_DIRS and f.parent != path.parent
                for f in filed
            )
        ):
            shadows.append(new)
    return shadows


def _stage_named_briefs(
    root: pathlib.Path, ids: Sequence[str], refused: Sequence[str] = ()
) -> None:
    for brief_id in ids:
        staged = _git(
            root,
            "add",
            "--all",
            "--",
            _brief_pathspec(brief_id),
            *refused,
            capture_output=True,
            text=True,
        )
        if staged.returncode != 0:
            print(f"  (warning: no brief file matched '{brief_id}'; nothing staged for it)")


def _short_log(root: pathlib.Path, rev_range: str) -> str:
    out = _git(root, "log", "--format=%h %s", rev_range, capture_output=True, text=True).stdout
    return "; ".join(out.splitlines()) or "none"


def _report_divergence(root: pathlib.Path, why: str) -> None:
    print(
        f"  (push deferred: {why}. Local commits kept, unpushed: {_short_log(root, 'origin/main..HEAD')}. "
        f"Remote commits not integrated: {_short_log(root, 'HEAD..origin/main')}. "
        "Resolve in a worktree, rebase onto origin/main and push.)"
    )


def _report_timeout(root: pathlib.Path, what: str) -> None:
    print(
        f"  (push deferred: git {what} timed out. Local commits kept, unpushed: "
        f"{_short_log(root, 'origin/main..HEAD')}. The next sweep retries.)"
    )


def _integrate_remote(root: pathlib.Path, deadline: float | None = None) -> bool:
    """Rebase the local commits onto origin/main in a scratch worktree, push, then advance `root`.

    Nothing is stashed and the checkout's uncommitted edits are never touched: `reset --keep`
    refuses to move when an edit overlaps an incoming change.
    """
    try:
        _git(
            root,
            "fetch",
            "-q",
            "origin",
            "main",
            capture_output=True,
            text=True,
            timeout=_timeout(deadline, FETCH_TIMEOUT),
        )
    except subprocess.TimeoutExpired:
        _report_timeout(root, "fetch")
        return False
    with tempfile.TemporaryDirectory(prefix="git-sync-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        _git(
            root,
            "worktree",
            "add",
            "-q",
            "--detach",
            str(wt),
            "HEAD",
            capture_output=True,
            text=True,
        )
        try:
            if _git(wt, "rebase", "origin/main", capture_output=True, text=True).returncode != 0:
                conflicts = _git(
                    wt, "diff", "--name-only", "--diff-filter=U", capture_output=True, text=True
                ).stdout.split()
                _git(wt, "rebase", "--abort", capture_output=True, text=True)
                _report_divergence(root, f"rebase conflict in {', '.join(conflicts)}")
                return False
            pushed = _git(
                wt,
                "push",
                "-q",
                "origin",
                "HEAD:main",
                capture_output=True,
                text=True,
                timeout=_timeout(deadline),
            )
            if pushed.returncode != 0:
                _report_divergence(root, "origin moved again while rebasing")
                return False
        except subprocess.TimeoutExpired:
            _report_timeout(root, "push")
            return False
        finally:
            _git(root, "worktree", "remove", "--force", str(wt), capture_output=True, text=True)
    try:
        _git(
            root,
            "fetch",
            "-q",
            "origin",
            "main",
            capture_output=True,
            text=True,
            timeout=_timeout(deadline, FETCH_TIMEOUT),
        )
    except subprocess.TimeoutExpired:
        print(
            "  (pushed, but the checkout was not advanced: fetch timed out; "
            "the next sweep catches up.)"
        )
        return True
    if _git(root, "reset", "--keep", "origin/main", capture_output=True, text=True).returncode != 0:
        print(
            "  (pushed, but the checkout was not advanced: uncommitted edits overlap incoming changes; "
            "commit or move them, then run: git reset --keep origin/main)"
        )
    return True


# Per-clone state of the coalesced pusher, all beside the sync lock inside .git.
PUSH_LOCK = ".tasks-push.lock"  # held by the one running pusher
WRITERS_LOCK = ".tasks-writers.lock"  # shared by every writer between commit start and dirty mark
DIRTY = ".tasks-push.dirty"  # main is ahead of origin and no push has started from it yet
PUSH_LOG = ".tasks-push.log"  # the detached pusher's output: divergence reports land here
SETTLE_MAX = 10.0  # longest a pusher waits for in-flight writers before pushing anyway
PUSH_WAIT = 90.0  # longest a synchronous push waits for a running pusher
# A sweep's whole run, under dispatch's 120 s flush timeout; the env var lets tests shrink it.
SWEEP_BUDGET = float(os.environ.get("CLAUDE_TASKS_SWEEP_BUDGET", 110.0))
PUSH_RESERVE = 40.0  # of that budget, kept for the settle wait and the push itself


def _ahead_of_origin(root: pathlib.Path) -> bool:
    """True when main carries commits origin/main lacks; True too when that cannot be read."""
    ahead = _git(root, "rev-list", "--count", "origin/main..HEAD", capture_output=True, text=True)
    return ahead.returncode != 0 or ahead.stdout.strip() != "0"


def _mark_dirty(root: pathlib.Path) -> None:
    clone_state_path(root, DIRTY).touch()


def _is_dirty(root: pathlib.Path) -> bool:
    return clone_state_path(root, DIRTY).exists()


def _take_dirty(root: pathlib.Path) -> bool:
    """Clear the dirty mark; True if it was set. Cleared before pushing, so a later mark means a later commit."""
    try:
        clone_state_path(root, DIRTY).unlink()
    except FileNotFoundError:
        return False
    return True


def _push(root: pathlib.Path, deadline: float | None = None) -> bool:
    """Push main; on rejection rebase onto origin under the queue lock and push again.

    Past `deadline` the push is abandoned and its commits are left for the next sweep.
    """
    try:
        if _git(root, "push", "-q", "origin", "main", timeout=_timeout(deadline)).returncode == 0:
            return True
    except subprocess.TimeoutExpired:
        _report_timeout(root, "push")
        return False
    with repo_lock(root, timeout=_timeout(deadline, 30.0) or 0.0) as acquired:
        if not acquired:
            print("  (push deferred: lock held; the next write or sweep pushes these commits.)")
            return False
        return _integrate_remote(root, deadline)


def _drain(root: pathlib.Path, deadline: float | None = None) -> bool:
    """Push until no commit is waiting or `deadline` passes; the caller holds the push lock.

    Returns the last push's result; a mark still set at the deadline counts as a failure.
    """
    pushed = True
    while True:
        # Let writers already committing land first, so one push carries the whole burst.
        settle = _timeout(deadline, SETTLE_MAX)
        with exclusive_lock(clone_state_path(root, WRITERS_LOCK), timeout=settle, poll=0.05):
            pass
        if deadline is not None and time.monotonic() >= deadline:
            return pushed and not _is_dirty(root)
        if not _take_dirty(root):
            return pushed
        pushed = _push(root, deadline)


def _release(fd: int) -> None:
    os.close(fd)  # closing the last fd on the lock's open file releases the flock


def _run_pusher(root: pathlib.Path, fd: int | None) -> None:
    """Drain while holding the push lock `fd`; re-take it if a write slipped in as it was released."""
    while fd is not None:
        try:
            _drain(root)
        finally:
            _release(fd)
        fd = try_lock(clone_state_path(root, PUSH_LOCK)) if _is_dirty(root) else None


def _report_last_push(root: pathlib.Path) -> None:
    """Show the writer what the previous detached pusher reported, then clear it; the caller holds the push lock."""
    log = clone_state_path(root, PUSH_LOG)
    report = log.read_text(errors="replace").strip() if log.exists() else ""
    if report:
        print(f"  (previous background push reported:)\n{report}")
        log.write_text("")


def _spawn_pusher(root: pathlib.Path, fd: int) -> None:
    """Hand the held push lock to a detached pusher process, so the caller returns at once."""
    try:
        _report_last_push(root)
        with open(clone_state_path(root, PUSH_LOG), "a") as log:
            subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "git_sync.py"),
                    "--push-pending",
                    str(root),
                    "--lock-fd",
                    str(fd),
                ],
                pass_fds=(fd,),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    finally:
        _release(fd)


def request_push(root: pathlib.Path) -> None:
    """Start a detached pusher unless one is running; a running one picks up the dirty mark."""
    fd = try_lock(clone_state_path(root, PUSH_LOCK))
    if fd is None:
        return
    try:
        _spawn_pusher(root, fd)
    except OSError as e:
        # The commit has landed and stays dirty, so the next write or sweep pushes it.
        print(f"  (push deferred: could not start the background pusher: {e})")


def push_now(root: pathlib.Path, wait: float = PUSH_WAIT, deadline: float | None = None) -> bool:
    """Push synchronously, after any running pusher, for callers that need origin current.

    `wait` bounds the wait for a running pusher; past it the push is left to that pusher.
    `deadline` (monotonic) bounds the pushing itself.
    """
    with exclusive_lock(clone_state_path(root, PUSH_LOCK), timeout=wait) as acquired:
        if not acquired:
            print("  (push deferred: another pusher still running; it will push these commits.)")
            return False
        _report_last_push(root)
        _mark_dirty(root)
        pushed = _drain(root, deadline)
    if _is_dirty(root):  # a write that found the lock held just before it was released
        request_push(root)
    return pushed


def wait_for_push(root: pathlib.Path, timeout: float = 60.0, poll: float = 0.05) -> bool:
    """Block until no pusher runs and none is pending; False if `timeout` passed first.

    True does not mean origin carries HEAD: a failed push leaves its commits for the next sweep.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fd = None if _is_dirty(root) else try_lock(clone_state_path(root, PUSH_LOCK))
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
            print(
                "  (sync deferred: lock held; your files are safe on disk. "
                "Re-run git_sync.py with the same message and ids to commit them.)"
            )
            return False
        shadows = _shadowing_briefs(root)
        for path in shadows:
            print(
                f"  (refused: {path} would re-create an id already filed in another state; "
                "sharpen the existing brief instead. Left untracked.)"
            )
        staged_leaks = _staged_secret_bearing(root)
        _unstage(root, staged_leaks)
        leaks = list(
            dict.fromkeys([*staged_leaks, *(_secret_bearing(root, paths) if paths else [])])
        )
        if leaks:  # a count, not names: the paths come from scanning credential-bearing files
            print(
                f"  (refused: {len(leaks)} held back for looking like a credential. "
                "Left uncommitted; `git status` lists them. Remove the secret, then re-sync.)"
            )
        refused = [f":(exclude){p}" for p in (*shadows, *leaks)]
        _stage_named_briefs(root, ids, refused)
        if paths:
            _git(root, "add", "--all", "--", *paths, *refused)
        # Tolerate "nothing to commit": a concurrent holder may have already swept us in.
        _git(root, "commit", "-q", "-m", message, capture_output=True, text=True)
    return True


def _commit_for_push(
    message: str, root: pathlib.Path, ids: Sequence[str], paths: Sequence[str]
) -> bool:
    """Commit and mark the clone dirty; False if deferred or the queue belongs to another repo."""
    if not owns_repo(root):
        return False  # a project queue lives inside another repo, which ships it
    # Shared while committing, so a pusher waiting for exclusive access waits for the whole burst.
    with shared_lock(clone_state_path(root, WRITERS_LOCK)):
        if not _commit(message, root, ids, paths):
            return False
        # An idle sweep, or a commit a running pusher already carried, needs no push.
        if _ahead_of_origin(root):
            _mark_dirty(root)
    return True


def sync_commit(
    message: str,
    root: pathlib.Path | None = None,
    ids: Sequence[str] = (),
    paths: Sequence[str] = (),
) -> bool:
    """Commit only what the caller names under the cross-process lock; the push runs detached.

    `ids` stage every file `<id>.md` at any depth, deletions and moves included; `paths` stage
    explicit pathspecs (`.` when the caller owns the whole tree); nothing else is staged.
    True means committed; False means deferred, and the caller re-runs with the same args.
    """
    root = resolve_root() if root is None else root
    if not _commit_for_push(message, root, ids, paths):
        return False
    if _is_dirty(root):
        request_push(root)
    return True


def sync_commit_and_push(
    message: str, root: pathlib.Path, ids: Sequence[str] = (), paths: Sequence[str] = ()
) -> bool:
    """sync_commit, then push before returning; True only once origin carries the commit."""
    return _commit_for_push(message, root, ids, paths) and push_now(root)


def sweep(root: pathlib.Path | None = None) -> bool:
    """Commit every uncommitted change and push before returning: the net under every writer.

    It is the one caller that refreshes origin/main, and dispatch relies on origin being current after it.
    True means nothing is left to push; False means deferred or the push failed.
    """
    root = resolve_root() if root is None else root
    deadline = time.monotonic() + SWEEP_BUDGET
    if owns_repo(root):
        _fetch_if_stale(root)
    if not _commit_for_push("queue: sync uncommitted briefs", root, (), (".",)):
        return False
    # Ahead, not dirty: a running pusher may already have taken the mark and still be pushing.
    if not _ahead_of_origin(root):
        return True
    # Time the fetch and commit locks spent comes off the wait, so dispatch never kills a push.
    reserve = min(PUSH_RESERVE, SWEEP_BUDGET / 2)
    wait = min(PUSH_WAIT, max(0.0, deadline - time.monotonic() - reserve))
    return push_now(root, wait=wait, deadline=deadline)


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
        print(
            "Usage: python scripts/git_sync.py '<commit-message>' [brief-id ...]", file=sys.stderr
        )
        sys.exit(0 if argv and argv[0] in ("-h", "--help") else 1)
    sync_commit(argv[0], ids=argv[1:])


if __name__ == "__main__":
    main()  # pragma: no cover
