#!/usr/bin/env python3
"""
git_sync.py — commit the queue under the cross-process lock, then push it coalesced.

Writers commit synchronously and mark the clone dirty; one detached pusher per clone pushes once
for the whole burst. `--sweep` is the timer's net: it commits what no writer named and pushes before it exits.
"""

import contextlib
import datetime
import enum
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from typing import Any

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _advance import advance_to_origin  # noqa: E402
from _concurrency import NETWORK_TIMEOUT, Deadline, HeldLock, repo_lock  # noqa: E402
from _credentials import carries_credential  # noqa: E402
from _default_branch import default_branch  # noqa: E402
from _log_conflict import Rebase  # noqa: E402
from _paths import is_brief, resolve_root  # noqa: E402
from _push_state import PushState  # noqa: E402
from done_guard import DoneBackstop, scope_of  # noqa: E402
from host_land import land_host_queue  # noqa: E402


def _git(root: pathlib.Path, *args: str, **kw: Any) -> "subprocess.CompletedProcess[Any]":
    return subprocess.run(["git", "-C", str(root), *args], **kw)


def owns_repo(root: pathlib.Path) -> bool:
    """True when `root` is the top of its own git repo, so `add --all` stages only the queue."""
    top = _git(root, "rev-parse", "--show-toplevel", capture_output=True, encoding="utf-8")
    return top.returncode == 0 and pathlib.Path(top.stdout.strip()).resolve() == root.resolve()


def host_repo_name(root: pathlib.Path) -> str | None:
    """The name of the git repo that holds `root`, None when `root` is in no repo."""
    top = _git(root, "rev-parse", "--show-toplevel", capture_output=True, encoding="utf-8")
    return pathlib.Path(top.stdout.strip()).name if top.returncode == 0 else None


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
        encoding="utf-8",
    ).stdout
    found = []
    for rel in _split_z(out):
        try:
            if carries_credential((root / rel).read_text(errors="ignore", encoding="utf-8")):
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
        encoding="utf-8",
    ).stdout
    staged = (
        (rel, _git(root, "show", f":{rel}", capture_output=True).stdout.decode(errors="ignore"))
        for rel in _split_z(out)
    )
    return [rel for rel, content in staged if carries_credential(content)]


def _unstage(root: pathlib.Path, rels: Sequence[str]) -> None:
    """Drop `rels` from the pending commit; the files stay on disk."""
    if rels:
        _git(root, "reset", "-q", "--", *(f":(literal){rel}" for rel in rels))


def _brief_pathspec(brief_id: str) -> str:
    return f":(glob)**/{brief_id}.md"


def _tracked(root: pathlib.Path, *flags: str) -> list[str]:
    out = _git(root, "ls-files", "-z", *flags, capture_output=True, encoding="utf-8").stdout
    return [path for path in out.split("\0") if path]


FETCH_MAX_AGE = 45.0  # under the 60 s sweep interval, so every timed sweep fetches


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
        encoding="utf-8",
    ).stdout.strip()
    return pathlib.Path(out) if out else None


def _fetch_if_stale(root: pathlib.Path) -> None:
    """Fetch origin's default branch unless the last fetch is under FETCH_MAX_AGE old."""
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
            f"  (fetch timed out after {NETWORK_TIMEOUT:.0f}s; using the remote branch as last fetched.)"
        )


def _shadowing_briefs(root: pathlib.Path) -> list[str]:
    """New brief files whose id is already filed in another state dir, here or on origin's default branch.

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
        f"HEAD...origin/{default_branch(root)}",
        capture_output=True,
        encoding="utf-8",
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
        if is_brief(path) and any(
            f.name == path.name and is_brief(f) and f.parent != path.parent for f in filed
        ):
            shadows.append(new)
    return shadows


def _diff_names(root: pathlib.Path, rev_range: str, kind: str) -> list[str]:
    """Paths of one change kind (A or D) in `rev_range`; renames are split so a move shows as both."""
    out = _git(
        root,
        "diff",
        "--relative",
        "--no-renames",
        "--name-only",
        f"--diff-filter={kind}",
        rev_range,
        capture_output=True,
        encoding="utf-8",
    )
    names: list[str] = out.stdout.splitlines()
    return names


def _rival_briefs(root: pathlib.Path) -> list[str]:
    """Briefs the unpushed commits add whose id origin already files in another state dir.

    The commit-time check sees only the state fetched then; a rival pushed since rebases cleanly
    (different paths), so the push must refuse it once origin is refreshed.
    """
    remote = f"origin/{default_branch(root)}"
    listed = _git(
        root, "ls-tree", "-r", "--name-only", remote, capture_output=True, encoding="utf-8"
    ).stdout.splitlines()
    moved_away = set(_diff_names(root, f"{remote}...HEAD", "D"))  # the old half of our own move
    filed = [pathlib.PurePath(p) for p in listed if p not in moved_away and is_brief(p)]
    added = (pathlib.PurePath(p) for p in _diff_names(root, f"{remote}...HEAD", "A") if is_brief(p))
    return [
        str(new)
        for new in added
        if any(f.name == new.name and f.parent != new.parent for f in filed)
    ]


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
            encoding="utf-8",
        )
        if staged.returncode != 0:
            print(f"  (warning: no brief file matched '{brief_id}'; nothing staged for it)")


def _short_log(root: pathlib.Path, rev_range: str) -> str:
    out = _git(
        root, "log", "--format=%h %s", rev_range, capture_output=True, encoding="utf-8"
    ).stdout
    return "; ".join(out.splitlines()) or "none"


def _report_divergence(root: pathlib.Path, why: str, parked: str = "") -> None:
    branch = default_branch(root)
    print(
        f"  (push deferred: {why}. {parked + '. ' if parked else ''}"
        f"Local commits kept, unpushed: {_short_log(root, f'origin/{branch}..HEAD')}. "
        f"Remote commits not integrated: {_short_log(root, f'HEAD..origin/{branch}')}. "
        f"Resolve on a detached worktree, never the checked-out {branch}: {_recovery_steps(root)}.)"
    )


def _recovery_steps(root: pathlib.Path) -> str:
    """Rebasing the default branch itself from a second worktree leaves `root`'s index stale, so stay detached."""
    wt = f"{root}-recover"
    branch = default_branch(root)
    return (
        f"git -C {root} worktree add --detach {wt} HEAD; cd {wt}; git rebase origin/{branch}; "
        f"git push origin HEAD:{branch}; git -C {root} merge --ff-only origin/{branch}; "
        f"git -C {root} worktree remove {wt}"
    )


def _fetch_origin(root: pathlib.Path, deadline: Deadline) -> None:
    _git(
        root,
        "fetch",
        "-q",
        "origin",
        default_branch(root),
        capture_output=True,
        encoding="utf-8",
        timeout=deadline.cap(NETWORK_TIMEOUT),
    )


@contextlib.contextmanager
def scratch_worktree(root: pathlib.Path, ref: str = "HEAD") -> Iterator[pathlib.Path]:
    """A throwaway detached worktree of `ref`, removed on exit, so a write is built and pushed off the checkout."""
    with tempfile.TemporaryDirectory(prefix="git-sync-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        _git(
            root,
            "worktree",
            "add",
            "-q",
            "--detach",
            str(wt),
            ref,
            capture_output=True,
            encoding="utf-8",
        )
        try:
            yield wt
        finally:
            _git(
                root,
                "worktree",
                "remove",
                "--force",
                str(wt),
                capture_output=True,
                encoding="utf-8",
            )


def _wip_branch() -> str:
    """The branch an unresolvable divergence is parked on: one per host and day, so reruns reuse it."""
    host = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname().split(".")[0]) or "host"
    return f"wip/queue-sync-{host}-{datetime.date.today().isoformat()}"


def _park_local_commits(root: pathlib.Path, deadline: Deadline) -> str:
    """Push the local commits to the wip branch so origin holds them; say where they went.

    Only this sync writes the branch and the local default branch only grows, so a forced push is a fast-forward
    of the same history or a safe replace. A failed push leaves the commits local, and says so.
    """
    branch = _wip_branch()
    try:
        parked = _git(
            root,
            "push",
            "-q",
            "--force",
            "origin",
            f"HEAD:refs/heads/{branch}",
            capture_output=True,
            encoding="utf-8",
            timeout=deadline.cap(NETWORK_TIMEOUT),
        )
    except subprocess.TimeoutExpired:
        return f"ALERT: could not park the local commits on origin/{branch} (push timed out)"
    if parked.returncode != 0:
        return (
            f"ALERT: could not park the local commits on origin/{branch}: {parked.stderr.strip()}"
        )
    return f"ALERT: local commits parked on origin/{branch} for a human to merge"


def _rebase_and_push(root: pathlib.Path, wt: pathlib.Path, deadline: Deadline) -> bool:
    branch = default_branch(root)
    rebased = Rebase.onto_origin(wt, branch).run()
    if not rebased.completed:
        _report_divergence(root, rebased.reason, _park_local_commits(root, deadline))
        return False
    pushed = _git(
        wt,
        "push",
        "-q",
        "origin",
        f"HEAD:{branch}",
        capture_output=True,
        encoding="utf-8",
        timeout=deadline.cap(NETWORK_TIMEOUT),
    )
    if pushed.returncode != 0:
        _report_divergence(root, "origin moved again while rebasing")
    return pushed.returncode == 0


def _advance_checkout(root: pathlib.Path, deadline: Deadline) -> None:
    branch = default_branch(root)
    try:
        _fetch_origin(root, deadline)
    except subprocess.TimeoutExpired:
        print(
            "  (pushed, but the checkout was not advanced: the fetch timed out; "
            f"run: git fetch origin {branch} && git reset --keep origin/{branch})"
        )
        return
    if not advance_to_origin(root, branch):
        print(
            "  (pushed, but the checkout was not advanced: uncommitted edits overlap incoming changes; "
            f"commit or move them, then run: git reset --keep origin/{branch})"
        )


def _integrate_remote(root: pathlib.Path, deadline: Deadline) -> bool:
    """Rebase the local commits onto origin's default branch in a scratch worktree, push, then advance `root`.

    Nothing is stashed and the checkout's uncommitted edits are never touched: `reset --keep`
    refuses to move when an edit overlaps an incoming change.
    """
    _fetch_origin(root, deadline)
    rivals = _rival_briefs(root)
    if rivals:
        why = (
            f"{', '.join(rivals)} would duplicate a brief id origin already files in another state"
        )
        _report_divergence(root, why, _park_local_commits(root, deadline))
        return False
    with scratch_worktree(root) as wt:
        if not _rebase_and_push(root, wt, deadline):
            return False
    _advance_checkout(root, deadline)
    return True


SETTLE_MAX = 10.0  # longest a pusher waits for in-flight writers before pushing anyway
PUSH_WAIT = 90.0  # longest a synchronous push waits for a running pusher


def _sweep_time_scale(raw: str | None) -> float:
    """Lets a subprocess test run the real sweep on a seconds-long budget; a bad value warns and keeps the real one."""
    try:
        scale = float(raw or "1")
    except ValueError:
        scale = 0.0
    if scale > 0:
        return scale
    print(f"git_sync: ignoring CLAUDE_TASKS_SWEEP_TIME_SCALE={raw!r}, using 1", file=sys.stderr)
    return 1.0


_TIME_SCALE = _sweep_time_scale(os.environ.get("CLAUDE_TASKS_SWEEP_TIME_SCALE"))
SWEEP_BUDGET = 110.0 * _TIME_SCALE  # a sweep's whole run, under dispatch's 120 s flush timeout
PUSH_RESERVE = (
    40.0 * _TIME_SCALE
)  # of that budget, the most one drain may spend settling, fetching and pushing
REBASE_LOCK_WAIT = 30.0  # longest a rejected push waits for the queue lock before deferring


def _ahead_of_origin(root: pathlib.Path) -> bool:
    """True when the checkout carries commits origin lacks; True too when that cannot be read."""
    ahead = _git(
        root,
        "rev-list",
        "--count",
        f"origin/{default_branch(root)}..HEAD",
        capture_output=True,
        encoding="utf-8",
    )
    return ahead.returncode != 0 or ahead.stdout.strip() != "0"


def _push_main(root: pathlib.Path, deadline: Deadline) -> bool:
    pushed = _git(
        root, "push", "-q", "origin", default_branch(root), timeout=deadline.cap(NETWORK_TIMEOUT)
    )
    if pushed.returncode == 0:
        return True
    with repo_lock(root, timeout=deadline.cap(REBASE_LOCK_WAIT)) as acquired:
        if not acquired:
            print("  (push deferred: lock held; the next write or sweep pushes these commits.)")
            return False
        return _integrate_remote(root, deadline)


def _push(root: pathlib.Path, deadline: Deadline) -> bool:
    """Push the default branch, rebasing onto origin under the queue lock if it is rejected.

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
    with open(state.log_path, "a", encoding="utf-8") as log:
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
        _commit_locked(message, root, ids, paths)
    return True


def _commit_locked(
    message: str, root: pathlib.Path, ids: Sequence[str], paths: Sequence[str]
) -> None:
    """Stage and commit what the caller names; the caller holds the queue lock."""
    shadows = _shadowing_briefs(root)
    for path in shadows:
        print(
            f"  (refused: {path} would re-create an id already filed in another state; "
            "sharpen the existing brief instead. Left untracked.)"
        )
    staged_leaks = _staged_secret_bearing(root)
    _unstage(root, staged_leaks)
    leaks = list(dict.fromkeys([*staged_leaks, *(_secret_bearing(root, paths) if paths else [])]))
    if leaks:  # a count, not names: the paths come from scanning credential-bearing files
        print(
            f"  (refused: {len(leaks)} held back for looking like a credential. "
            "Left uncommitted; `git status` lists them. Remove the secret, then re-sync.)"
        )
    DoneBackstop(root).run(scope_of(ids, paths))
    refused = [f":(exclude){p}" for p in (*shadows, *leaks)]
    _stage_named_briefs(root, ids, refused)
    if paths:
        _git(root, "add", "--all", "--", *paths, *refused)
    # Tolerate "nothing to commit": a concurrent holder may have already swept us in.
    _git(root, "commit", "-q", "-m", message, capture_output=True, encoding="utf-8")


class SyncResult(enum.Enum):
    """Where a write went: the queue's own main, a wip branch of its host repo, or nowhere yet."""

    COMMITTED = "committed"  # on the queue's own main; the push is a separate step
    DEFERRED = (
        "deferred"  # not recorded (lock held, a git step failed, no repo); files stay on disk
    )
    HOSTED = "hosted"  # a queue inside another repo, landed on that repo's origin wip branch


def _commit_for_push(
    message: str,
    root: pathlib.Path,
    ids: Sequence[str],
    paths: Sequence[str],
    lock_held: bool = False,
) -> SyncResult:
    """Commit and mark the clone dirty; a queue inside another repo is landed on that repo's wip branch.

    `lock_held` means the caller already holds the queue lock around its own writes, so one hold
    covers write and commit; taking it again would wait on itself.
    """
    if not owns_repo(root):
        landed = land_host_queue(root, message, lock_held=lock_held)
        return SyncResult.HOSTED if landed else SyncResult.DEFERRED
    state = PushState(root)
    with state.writing():
        if lock_held:
            _commit_locked(message, root, ids, paths)
        elif not _commit(message, root, ids, paths):
            return SyncResult.DEFERRED
        # An idle sweep, or a commit a running pusher already carried, needs no push.
        if _ahead_of_origin(root):
            state.mark_dirty()
    return SyncResult.COMMITTED


def sync_commit(
    message: str,
    root: pathlib.Path | None = None,
    ids: Sequence[str] = (),
    paths: Sequence[str] = (),
    *,
    lock_held: bool = False,
) -> SyncResult:
    """Commit only the briefs `ids` and pathspecs `paths` name, under the lock; the push runs detached.

    DEFERRED means the caller re-runs with the same args; HOSTED means a queue inside another repo
    was landed on that repo's wip branch, with nothing recorded in its checkout.
    """
    root = resolve_root() if root is None else root
    result = _commit_for_push(message, root, ids, paths, lock_held)
    if result is SyncResult.COMMITTED and PushState(root).is_dirty():
        request_push(root)
    return result


def sync_commit_and_push(
    message: str, root: pathlib.Path, ids: Sequence[str] = (), paths: Sequence[str] = ()
) -> bool:
    """sync_commit, then push before returning; True only once origin carries the commit."""
    match _commit_for_push(message, root, ids, paths):
        case SyncResult.COMMITTED:
            return push_now(root)
        case SyncResult.HOSTED:
            return True  # the landing pushed the wip branch itself
        case _:
            return False


def sweep(root: pathlib.Path | None = None) -> bool:
    """Commit every uncommitted change and push before returning: the net under every writer.

    It is the one caller that refreshes origin, and dispatch relies on origin being current after it.
    True means nothing is left to push; False means deferred or the push failed.
    """
    root = resolve_root() if root is None else root
    deadline = Deadline.in_(SWEEP_BUDGET)
    if owns_repo(root):
        _fetch_if_stale(root)
    committed = _commit_for_push("queue: sync uncommitted briefs", root, (), (".",))
    if committed is not SyncResult.COMMITTED:
        return committed is SyncResult.HOSTED  # a hosted queue has no main of its own to push
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
