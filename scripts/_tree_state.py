"""_tree_state: a cheap fingerprint of what a commit would carry, to tell whether a lock-free scan went stale.

`git_sync` scans the changed briefs before it takes the queue lock and, once it holds the lock, compares
this fingerprint instead of scanning again. It names the commit heads and every changed path with its
stat, so any write, move or stage since the scan makes the two fingerprints differ. Stdlib only.
"""

import os
import pathlib
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass

Stamp = tuple[int, int, int]  # mtime_ns, size, inode: an atomic replace changes at least the inode


@dataclass(frozen=True)
class Change:
    """One path `git status` reports, with the stat of the file as it was read."""

    status: str
    path: str
    stamp: Stamp | None  # None when the file is gone

    @classmethod
    def of(cls, root: pathlib.Path, status: str, path: str) -> "Change":
        try:
            st = os.lstat(root / path)
        except OSError:
            return cls(status, path, None)
        return cls(status, path, (st.st_mtime_ns, st.st_size, st.st_ino))


def _git_out(root: pathlib.Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return done.stdout if done.returncode == 0 else ""


def _status_entries(raw: str) -> Iterator[tuple[str, str]]:
    """(XY, path) per `status --porcelain -z` entry; a rename or copy carries its old path as a second field."""
    fields = iter(p for p in raw.split("\0") if p)
    for field in fields:
        status, path = field[:2], field[3:]
        if "R" in status or "C" in status:
            next(fields, None)
        yield status, path


@dataclass(frozen=True)
class TreeState:
    """The commit heads and the changed paths, as a scan saw them."""

    head: str
    origin: str
    changes: tuple[Change, ...]

    @classmethod
    def read(cls, root: pathlib.Path, origin_ref: str) -> "TreeState":
        raw = _git_out(
            root, "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all"
        )
        changes = tuple(Change.of(root, status, path) for status, path in _status_entries(raw))
        return cls(
            _git_out(root, "rev-parse", "-q", "--verify", "HEAD").strip(),
            _git_out(root, "rev-parse", "-q", "--verify", origin_ref).strip(),
            changes,
        )
