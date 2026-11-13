#!/usr/bin/env python3
"""
git_sync.py — commit the queue under the cross-process lock, then push it coalesced.

Writers commit synchronously and mark the clone dirty; one detached pusher per clone pushes once
for the whole burst. `--sweep` is the timer's net: it commits what no writer named and pushes before it exits.
"""

import contextlib
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from typing import Any

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _concurrency import Deadline, HeldLock, repo_lock  # noqa: E402
from _paths import TASK_DIRS, resolve_root  # noqa: E402
from _push_state import PushState  # noqa: E402


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
NETWORK_TIMEOUT = 20.0  # a hung remote must not spend the sweep's whole budget


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


def _fetch_if_stale(root: pathlib.Path) -> None:
    """Fetch origin/main unless the last fetch is under FETCH_MAX_AGE old."""
    fetch_head = _fetch_head(root)
    try:
        if fetch_head and time.time() - fetch_head.stat().st_mtime < FETCH_MAX_AGE:
            return
    except OSError:
        pass  # never fetched
    try:
        _fetch_origin(root, Deadline.in_(NETWORK_TIMEOUT))
    except subprocess.TimeoutExpired:
        print(
            f"  (fetch timed out after {NETWORK_TIMEOUT:.0f}s; using origin/main as last fetched.)"
        )


def _shadowing_briefs(root: pathlib.Path) -> list[str]:
    """New brief files whose id is already filed in another state dir, here or on origin/main.

    A writer with a stale view would re-create a moved or closed id; a write never fetches, so a rival
    pushed since the last sweep is seen only by the next one.
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


def _fetch_origin(root: pathlib.Path, deadline: Deadline) -> None:
    _git(
        root,
        "fetch",
        "-q",
        "origin",
        "main",
        capture_output=True,
        text=True,
        timeout=deadline.cap(NETWORK_TIMEOUT),
    )


@contextlib.contextmanager
def _scratch_worktree(root: pathlib.Path) -> Iterator[pathlib.Path]:
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
            yield wt
        finally:
            _git(root, "worktree", "remove", "--force", str(wt), capture_output=True, text=True)


def _rebase_and_push(root: pathlib.Path, wt: pathlib.Path, deadline: Deadline) -> bool:
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
        timeout=deadline.cap(NETWORK_TIMEOUT),
    )
    if pushed.returncode != 0:
        _report_divergence(root, "origin moved again while rebasing")
    return pushed.returncode == 0


def _advance_checkout(root: pathlib.Path, deadline: Deadline) -> None:
    try:
        _fetch_origin(root, deadline)
    except subprocess.TimeoutExpired:
        print(
            "  (pushed, but the checkout was not advanced: the fetch timed out; "
            "run: git fetch origin main && git reset --keep origin/main)"
        )
        return
    if _git(root, "reset", "--keep", "origin/main", capture_output=True, text=True).returncode != 0:
        print(
            "  (pushed, but the checkout was not advanced: uncommitted edits overlap incoming changes; "
            "commit or move them, then run: git reset --keep origin/main)"
        )


def _integrate_remote(root: pathlib.Path, deadline: Deadline) -> bool:
    """Rebase the local commits onto origin/main in a scratch worktree, push, then advance `root`.

    Nothing is stashed and the checkout's uncommitted edits are never touched: `reset --keep`
    refuses to move when an edit overlaps an incoming change.
    """
    _fetch_origin(root, deadline)
    with _scratch_worktree(root) as wt:
        if not _rebase_and_push(root, wt, deadline):
            return False
    _advance_checkout(root, deadline)
    return True


SETTLE_MAX = 10.0  # longest a pusher waits for in-flight writers before pushing anyway
PUSH_WAIT = 90.0  # longest a synchronous push waits for a running pusher
SWEEP_BUDGET = 110.0  # a sweep's whole run, under dispatch's 120 s flush timeout
PUSH_RESERVE = 40.0  # of that budget, the most one drain may spend settling, fetching and pushing
REBASE_LOCK_WAIT = 30.0  # longest a rejected push waits for the queue lock before deferring


def _ahead_of_origin(root: pathlib.Path) -> bool:
    """True when main carries commits origin/main lacks; True too when that cannot be read."""
    ahead = _git(root, "rev-list", "--count", "origin/main..HEAD", capture_output=True, text=True)
    return ahead.returncode != 0 or ahead.stdout.strip() != "0"


def _push_main(root: pathlib.Path, deadline: Deadline) -> bool:
    pushed = _git(root, "push", "-q", "origin", "main", timeout=deadline.cap(NETWORK_TIMEOUT))
    if pushed.returncode == 0:
        return True
    with repo_lock(root, timeout=deadline.cap(REBASE_LOCK_WAIT)) as acquired:
        if not acquired:
            print("  (push deferred: lock held; the next write or sweep pushes these commits.)")
            return False
        return _integrate_remote(root, deadline)


def _push(root: pathlib.Path, deadline: Deadline) -> bool:
    """Push main, rebasing onto origin under the queue lock if it is rejected.

    A timeout is a failed push that restores the dirty mark, so the next sweep pushes again.
    """
    try:
        return _push_main(root, deadline)
    except subprocess.TimeoutExpired as e:
        PushState(root).mark_dirty()
        print(f"  (push timed out after {e.timeout:.0f}s; the next sweep pushes these commits.)")
        return False


def _drain(root: pathlib.Path) -> bool:
    """Push until no commit is waiting, within PUSH_RESERVE; the caller holds the push lock. False once a push fails."""
    state, deadline = PushState(root), Deadline.in_(PUSH_RESERVE)
    while True:
        # Let writers already committing land first, so one push carries the whole burst.
        state.settle_writers(deadline.cap(SETTLE_MAX))
        if not state.take_dirty():
            return True
        if not _push(root, deadline):
            return False


def _run_pusher(root: pathlib.Path, lock: HeldLock | None) -> None:
    """Drain while holding the push lock; re-take it if a write slipped in as it was released."""
    state = PushState(root)
    while lock is not None:
        with lock:
            drained = _drain(root)
        lock = state.try_lock_pusher() if drained and state.is_dirty() else None


def _report_last_push(state: PushState) -> None:
    """Show the writer what the previous detached pusher reported; the caller holds the push lock."""
    report = state.take_report()
    if report:
        print(f"  (previous background push reported:)\n{report}")


def _spawn_pusher(state: PushState, lock: HeldLock) -> None:
    """Hand the held push lock to a detached pusher process, so the caller returns at once."""
    _report_last_push(state)
    with open(state.log_path, "a") as log:
        subprocess.Popen(
            [
                sys.executable,
                str(SCRIPTS_DIR / "git_sync.py"),
                "--push-pending",
                str(state.root),
                "--lock-fd",
                str(lock.fd),
            ],
            pass_fds=(lock.fd,),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def request_push(root: pathlib.Path) -> None:
    """Start a detached pusher unless one is running; a running one picks up the dirty mark."""
    state = PushState(root)
    lock = state.try_lock_pusher()
    if lock is None:
        return
    with lock:
        try:
            _spawn_pusher(state, lock)
        except OSError as e:
            # The commit has landed and stays dirty, so the next write or sweep pushes it.
            print(f"  (push deferred: could not start the background pusher: {e})")


def push_now(root: pathlib.Path, wait: float = PUSH_WAIT) -> bool:
    """Push synchronously, after any running pusher, for callers that need origin current.

    `wait` bounds the wait for a running pusher; past it the push is left to that pusher.
    """
    state = PushState(root)
    with state.pusher_lock(wait) as acquired:
        if not acquired:
            print("  (push deferred: another pusher still running; it will push these commits.)")
            return False
        _report_last_push(state)
        state.mark_dirty()
        pushed = _drain(root)
    if pushed and state.is_dirty():  # a write that found the lock held just before it was released
        request_push(root)
    return pushed


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
    state = PushState(root)
    with state.writing():
        if not _commit(message, root, ids, paths):
            return False
        # An idle sweep, or a commit a running pusher already carried, needs no push.
        if _ahead_of_origin(root):
            state.mark_dirty()
    return True


def sync_commit(
    message: str,
    root: pathlib.Path | None = None,
    ids: Sequence[str] = (),
    paths: Sequence[str] = (),
) -> bool:
    """Commit only the briefs `ids` and pathspecs `paths` name, under the lock; the push runs detached.

    False means deferred, and the caller re-runs with the same args.
    """
    root = resolve_root() if root is None else root
    if not _commit_for_push(message, root, ids, paths):
        return False
    if PushState(root).is_dirty():
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
    deadline = Deadline.in_(SWEEP_BUDGET)
    if owns_repo(root):
        _fetch_if_stale(root)
    if not _commit_for_push("queue: sync uncommitted briefs", root, (), (".",)):
        return False
    # Ahead, not dirty: a running pusher may already have taken the mark and still be pushing.
    if not _ahead_of_origin(root):
        return True
    # Time the fetch and commit locks spent comes off the wait, so dispatch never kills a push.
    return push_now(root, wait=deadline.reserving(PUSH_RESERVE).cap(PUSH_WAIT))


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--sweep":
        sweep(pathlib.Path(argv[1]).expanduser() if len(argv) > 1 else None)
        return
    if argv[:1] == ["--push-pending"] and argv[2:3] == ["--lock-fd"]:
        _run_pusher(pathlib.Path(argv[1]), HeldLock(int(argv[3])))
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
