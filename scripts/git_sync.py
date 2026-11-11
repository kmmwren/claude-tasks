#!/usr/bin/env python3
"""
git_sync.py — commit the queue under the cross-process lock, then push it coalesced.

Shared commit helper for every writer that touches the queue working tree (add_task.py
for new briefs, transition.py for status changes). Routing all commits through here means
concurrent writers take turns on the git index via the same mutex, instead of colliding
on .git/index.lock and silently dropping commits. Each writer commits only the briefs it names.

The commit is synchronous; the push is not. A writer marks the clone dirty and returns. One
detached pusher per clone (a try-lock nobody waits on) lets the burst go quiet, pushes once, and
pushes again only if a write marked the clone dirty meanwhile, so N writes cost one push, not N.
`origin/main` is fetched only when a push is rejected, or (for the shadow check) when the last
fetch is over a minute old. A bulk writer skips the per-write sync (`--no-sync`), then commits
and pushes once with `--sweep`.

Usage (run from anywhere):
  python scripts/git_sync.py "brief(my-id): completed" my-id
  python scripts/git_sync.py --sweep [queue-root]   # commit+push everything dirty, now (timer job)
  python scripts/git_sync.py --push-pending ROOT --lock-fd N   # the detached pusher (internal)

`--sweep` is the same helper over the whole tree: it catches files a session edited by hand,
which no scripted writer could name. A queue root that is not the top of its own git repo
(a project's `.tasks/`) is never synced here; its repo ships it.

Exit codes: 0 — committed (push queued) or safely deferred; 1 — bad usage.
"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from typing import Any, NamedTuple

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _concurrency import lock_path, repo_lock, try_lock  # noqa: E402
from _paths import TASK_DIRS, resolve_root  # noqa: E402

FETCH_FRESH = 60.0  # seconds a fetch stays good for the shadow check
# Per-clone pusher state, beside the sync lock inside .git.
PUSH_LOCK = ".tasks-push.lock"  # try-locked by the one running pusher; nobody waits on it
DIRTY = ".tasks-push.dirty"  # a commit landed that no push has started from yet; its mtime is the last write
PUSH_LOG = ".tasks-push.log"  # the detached pusher's output, shown to the next writer


def _setting(name: str, default: float) -> float:
    return float(os.environ.get(f"CLAUDE_TASKS_{name}", default))


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


class _Listing(NamedTuple):
    """The checkout as one `ls-files` call reports it: what is tracked, what is gone, what is new."""

    tracked: list[str]
    deleted: list[str]
    untracked: list[str]


def _list_checkout(root: pathlib.Path) -> _Listing:
    out = _git(root, "ls-files", "-z", "-t", "--cached", "--deleted", "--others", "--exclude-standard",
               capture_output=True, text=True).stdout
    by_tag: dict[str, list[str]] = {"H": [], "R": [], "?": []}
    for entry in _split_z(out):
        by_tag.setdefault(entry[0], []).append(entry[2:])
    return _Listing(by_tag["H"], by_tag["R"], by_tag["?"])


def _fetch_origin(root: pathlib.Path) -> None:
    """Refresh origin/main unless a fetch ran within FETCH_FRESH seconds, so a burst of writes fetches once."""
    try:
        age = time.time() - lock_path(root, "FETCH_HEAD").stat().st_mtime
    except FileNotFoundError:
        age = FETCH_FRESH
    if age >= FETCH_FRESH:
        _git(root, "fetch", "-q", "origin", "main", capture_output=True, text=True)


def _shadowing_briefs(root: pathlib.Path) -> list[str]:
    """New brief files whose id is already filed in another state dir, here or on origin/main.

    A writer holding a stale view (a brief closed or moved meanwhile) would otherwise
    re-create the id in its old folder, leaving it in two states at once.
    """
    listing = _list_checkout(root)
    new_briefs = [pathlib.PurePath(n) for n in listing.untracked]
    new_briefs = [n for n in new_briefs if n.parent.name in TASK_DIRS and n.suffix == ".md"]
    if not new_briefs:
        return []  # nothing new to shadow: no fetch, no diff
    _fetch_origin(root)
    # Only briefs origin gained since our history diverged are rivals; an older origin path
    # we already moved away locally (committed but unpushed) is not.
    remote = _git(root, "diff", "--relative", "--name-only", "--diff-filter=A", "HEAD...origin/main",
                  capture_output=True, text=True)
    moved_away = set(listing.deleted)  # the old half of an in-flight move is not a rival
    filed = [pathlib.PurePath(p) for p in (*listing.tracked, *remote.stdout.splitlines())
             if p not in moved_away]
    return [str(n) for n in new_briefs
            if any(f.name == n.name and f.parent.name in TASK_DIRS and f.parent != n.parent
                   for f in filed)]


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


def _unpushed(root: pathlib.Path) -> bool:
    """True when local main is ahead of origin/main, or that cannot be told."""
    ahead = _git(root, "rev-list", "--count", "origin/main..main", capture_output=True, text=True)
    return ahead.returncode != 0 or int(ahead.stdout) > 0


def _push(root: pathlib.Path) -> bool:
    """Push main if there is anything to push; on rejection fetch and rebase under the queue lock."""
    if not _unpushed(root):
        return True
    if _git(root, "push", "-q", "origin", "main", capture_output=True, text=True).returncode == 0:
        return True
    with repo_lock(root) as acquired:
        if not acquired:
            print("  (push deferred: lock held; the next write or sweep pushes these commits.)")
            return False
        return _integrate_remote(root)


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


def _since_last_write(root: pathlib.Path) -> float:
    try:
        return time.time() - lock_path(root, DIRTY).stat().st_mtime
    except FileNotFoundError:
        return float("inf")


def _await_quiet(root: pathlib.Path) -> None:
    """Let a burst of writes finish so one push carries it; never longer than PUSH_HOLD seconds."""
    quiet = _setting("PUSH_QUIET", 3.0)
    deadline = time.monotonic() + _setting("PUSH_HOLD", 30.0)
    while _since_last_write(root) < quiet and time.monotonic() < deadline:
        time.sleep(min(0.1, quiet))


def _drain(root: pathlib.Path) -> None:
    """Push, once per quiet spell, until no commit is waiting."""
    while _is_dirty(root):
        _await_quiet(root)
        if _take_dirty(root):
            _push(root)


def _run_pusher(root: pathlib.Path, fd: int) -> None:
    """Drain while holding the push lock `fd`; take it again if a write slipped in as it was released."""
    held: int | None = fd
    while held is not None:
        try:
            _drain(root)
        finally:
            os.close(held)
        held = try_lock(lock_path(root, PUSH_LOCK)) if _is_dirty(root) else None


def _report_last_push(root: pathlib.Path) -> None:
    """Show what the previous detached pusher reported (a divergence, say), then clear it."""
    log = lock_path(root, PUSH_LOG)
    report = log.read_text().strip() if log.exists() else ""
    if report:
        print(f"  (previous background push reported:)\n{report}")
        log.write_text("")


def _spawn_pusher(root: pathlib.Path, fd: int) -> None:
    """Hand the held push lock to a detached pusher, so the writer returns at once and the pusher outlives it."""
    try:
        with open(lock_path(root, PUSH_LOG), "a") as log:
            subprocess.Popen(
                [sys.executable, str(SCRIPTS_DIR / "git_sync.py"), "--push-pending", str(root),
                 "--lock-fd", str(fd)],
                pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    finally:
        os.close(fd)  # the child's copy keeps the lock held


def request_push(root: pathlib.Path) -> None:
    """Start a detached pusher unless one is running; a running one sees the dirty mark when it finishes."""
    _report_last_push(root)
    fd = try_lock(lock_path(root, PUSH_LOCK))
    if fd is not None:
        _spawn_pusher(root, fd)


def push_now(root: pathlib.Path) -> bool:
    """Push in this process: for callers that need origin current (dispatch, the sweep)."""
    _report_last_push(root)
    _take_dirty(root)
    return _push(root)


def sync_commit(message: str, root: pathlib.Path | None = None,
                ids: Sequence[str] = (), paths: Sequence[str] = (), wait: bool = False) -> bool:
    """Commit only what the caller names under the cross-process lock, then queue the push.

    `ids` stage every file `<id>.md` at any depth, deletions and moves included; `paths` stage
    explicit pathspecs (`.` when the caller owns the whole tree). Anything else dirty in the
    checkout stays unstaged. True means committed and a push is queued (the detached pusher
    sends it once the burst of writes goes quiet); `wait=True` pushes before returning and
    reports the push. Returns False if deferred; a deferred caller re-runs with the same args.
    """
    root = resolve_root() if root is None else root
    if not owns_repo(root):
        return False  # a project queue lives inside another repo, which ships it
    if not _commit(message, root, ids, paths):
        return False
    _mark_dirty(root)
    if wait:
        return push_now(root)
    request_push(root)
    return True


def sweep(root: pathlib.Path | None = None) -> bool:
    """Commit and push now every uncommitted change in the queue: the net under writers that never call sync_commit."""
    return sync_commit("queue: sync uncommitted briefs", root, paths=(".",), wait=True)


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
