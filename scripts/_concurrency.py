#!/usr/bin/env python3
"""
_concurrency.py — make the task queue safe for many Claude sessions at once.

The queue is a single git working tree shared by every local session plus the cloud
agents. Two things in the old write path raced:

  1. The git index. Git allows exactly ONE add/commit/rebase per working tree at a
     time. Concurrent `add_task --commit` calls collided on `.git/index.lock`; some
     commits silently failed and their briefs were left written-to-disk-but-never-
     committed (invisible to the board and to cloud agents, lingering forever).
  2. Single output files. `write_text()` truncates then writes, so two processes
     regenerating index.md / board.html — or two briefs that hashed to the same
     filename — could clobber each other with no error at all.

This module fixes both with stdlib only:

  * repo_lock()    — a cross-process mutex (fcntl.flock) so only one process touches
                     the git index at a time. The OTHER half of the write — creating
                     the brief file — stays lock-free, because unique filenames never
                     contend, so a slow push never blocks a quick capture.
  * atomic_write() — write to a temp file then os.replace() (atomic on POSIX), so a
                     reader/committer never sees a half-written or truncated file.
  * reserve_name() — claim a unique filename with O_CREAT|O_EXCL, so two simultaneous
                     captures of the same title can't overwrite one another.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import contextlib
import dataclasses
import errno
import fcntl
import os
import pathlib
import time
from collections.abc import Iterable, Iterator

TASK_DIRS = ("inbox", "ready", "in-progress", "blocked", "done", "parked")


def clone_state_path(root: pathlib.Path, name: str) -> pathlib.Path:
    """Where per-clone state file `name` (a lock, a mark, a log) lives.

    Inside the clone's common git dir, so it is never committed and every worktree of the
    clone shares it. Falls back to the queue root for non-git callers (tests).
    """
    git = _common_git_dir(root)
    return (git if git is not None else root) / name


def _common_git_dir(root: pathlib.Path) -> pathlib.Path | None:
    """The git dir shared by all worktrees of `root`'s clone, read from disk without spawning git."""
    git = root / ".git"
    if git.is_dir():
        return git
    try:
        gitdir = git.read_text().removeprefix("gitdir:").strip()
    except OSError:
        return None
    worktree_dir = (root / gitdir).resolve()
    try:
        return (worktree_dir / (worktree_dir / "commondir").read_text().strip()).resolve()
    except OSError:
        return worktree_dir


def lock_path(root: pathlib.Path) -> pathlib.Path:
    """Where the queue's sync lock lives."""
    return clone_state_path(root, ".tasks-sync.lock")


def _flock_within(fd: int, op: int, timeout: float, poll: float) -> bool:
    """Take flock `op` on `fd`, polling until `timeout` seconds pass; True once held."""
    deadline = Deadline.in_(timeout)
    while True:
        try:
            fcntl.flock(fd, op | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES):
                raise  # pragma: no cover - defensive: only EAGAIN/EACCES mean "held"
            if deadline.expired():
                return False
            time.sleep(poll)


@contextlib.contextmanager
def _held_flock(path: pathlib.Path, op: int, timeout: float, poll: float) -> Iterator[bool]:
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        acquired = _flock_within(fd, op, timeout, poll)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def exclusive_lock(
    path: pathlib.Path, timeout: float = 30.0, poll: float = 0.1
) -> contextlib.AbstractContextManager[bool]:
    """Hold an exclusive flock on `path`; yields False if `timeout` passed first."""
    return _held_flock(path, fcntl.LOCK_EX, timeout, poll)


def shared_lock(
    path: pathlib.Path, timeout: float = 30.0, poll: float = 0.1
) -> contextlib.AbstractContextManager[bool]:
    """Hold a shared flock on `path`, alongside other sharers; yields False if `timeout` passed first."""
    return _held_flock(path, fcntl.LOCK_SH, timeout, poll)


class HeldLock:
    """An exclusive flock owned through its open file descriptor, which a child process can inherit."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._held = True

    def release(self) -> None:
        """Close the descriptor, freeing the flock once no child holds a copy; idempotent."""
        if self._held:
            self._held = False
            os.close(self.fd)

    def __enter__(self) -> "HeldLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def try_lock(path: pathlib.Path) -> HeldLock | None:
    """Take an exclusive flock on `path` without waiting; None if another process holds it."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    if _flock_within(fd, fcntl.LOCK_EX, 0.0, 0.0):
        return HeldLock(fd)
    os.close(fd)
    return None


@dataclasses.dataclass(frozen=True)
class Deadline:
    """A point in monotonic time that a run of waits and subprocesses must finish before."""

    at: float

    @classmethod
    def in_(cls, seconds: float) -> "Deadline":
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        """Seconds left before the deadline, never negative."""
        return max(0.0, self.at - time.monotonic())

    def expired(self) -> bool:
        return time.monotonic() >= self.at

    def cap(self, seconds: float) -> float:
        """`seconds`, or what is left of the deadline if that is less."""
        return min(seconds, self.remaining())

    def reserving(self, seconds: float) -> "Deadline":
        """The same deadline moved `seconds` earlier, for work that must finish before it."""
        return Deadline(self.at - seconds)


def repo_lock(
    root: pathlib.Path, timeout: float = 30.0, poll: float = 0.1
) -> contextlib.AbstractContextManager[bool]:
    """Serialize git index operations across processes.

    Yields True if the lock was acquired, False on timeout. Callers treat a timeout as
    "the brief is safe on disk; sync was deferred" rather than an error, because the
    file write already succeeded before the lock was ever requested.
    """
    return exclusive_lock(lock_path(root), timeout=timeout, poll=poll)


@contextlib.contextmanager
def require_lock(root: pathlib.Path, timeout: float = 30.0) -> Iterator[None]:
    """repo_lock for a read-modify-write that must not run unlocked: a timeout raises instead."""
    with repo_lock(root, timeout) as acquired:
        if not acquired:
            raise TimeoutError(
                f"queue lock at {root} not acquired within {timeout}s; nothing written"
            )
        yield


def atomic_write(path: pathlib.Path, text: str) -> None:
    """Write `text` to `path` so concurrent readers/writers never see a partial file.

    Writes a sibling temp file (same dir, so os.replace stays on one filesystem) then
    atomically renames it into place, fsyncing first so the content is durable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def reserve_name(target_dir: pathlib.Path, base: str, search_dirs: Iterable[pathlib.Path]) -> str:
    """Atomically claim a unique `<name>.md` in target_dir; return the bare name.

    Uses O_CREAT|O_EXCL so two processes choosing the same slug at the same instant
    can't both win — the loser bumps to `<base>-2`, `<base>-3`, … `search_dirs` keeps
    ids globally unique across all queue folders (a brief shouldn't share an id with
    one already filed elsewhere). The reserved file starts empty; the caller fills it
    with atomic_write so a committer never captures a half-written brief.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    others = [pathlib.Path(d) for d in search_dirs if pathlib.Path(d) != target_dir]
    n, name = 1, base
    while True:
        if any((d / f"{name}.md").exists() for d in others):
            n += 1
            name = f"{base}-{n}"
            continue
        try:
            fd = os.open(target_dir / f"{name}.md", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            n += 1
            name = f"{base}-{n}"
            continue
        os.close(fd)
        return name
