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
import contextlib
import errno
import fcntl
import os
import pathlib
import time
from collections.abc import Iterable, Iterator

import _compat  # noqa: F401  - version guard; must run before the annotations below evaluate

TASK_DIRS = ("inbox", "ready", "in-progress", "done", "parked")


def lock_path(root: pathlib.Path) -> pathlib.Path:
    """Where the sync lock lives. Inside .git so it is per-clone and never committed.

    Falls back to the repo root for worktrees (where .git is a file, not a dir) and
    for non-git callers (tests), so the lock always has a real directory to live in.
    """
    git = root / ".git"
    return (git if git.is_dir() else root) / ".tasks-sync.lock"


@contextlib.contextmanager
def repo_lock(root: pathlib.Path, timeout: float = 30.0, poll: float = 0.1) -> Iterator[bool]:
    """Serialize git index operations across processes.

    Blocks (polling, non-blocking flock) until the lock is free or `timeout` seconds
    pass. Yields True if the lock was acquired, False on timeout — callers treat a
    timeout as "the brief is safe on disk; sync was deferred" rather than an error,
    because the file write already succeeded before the lock was ever requested.
    """
    path = lock_path(root)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    raise  # pragma: no cover - defensive: only EAGAIN/EACCES mean "held"
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
