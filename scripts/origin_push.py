#!/usr/bin/env python3
"""
origin_push.py: land one commit on origin/main from a scratch worktree cut at its tip.

The caller's checkout is never written. `edit` changes files in the worktree and returns False to
publish nothing. A push that lost a race to another writer starts again from the new tip with
jittered backoff; any other refusal fails at once, so a declined push is never retried.
"""

import pathlib
import random
import re
import subprocess
import tempfile
import time
from collections.abc import Callable

from git_sync import NETWORK_TIMEOUT, _git

PUSH_ATTEMPTS = 8
IDENTITY = ("-c", "user.name=claude-tasks", "-c", "user.email=noreply@anthropic.com")
# A ref status line saying another writer won the race to update main, so a fresh retry can land.
# A server-side ref-lock clash reports "[remote rejected] ... (failed)"; hook output on "remote:"
# lines is ignored, so a declined push never matches.
LOST_RACE = re.compile(
    r"^ ! \[(?:rejected\]|remote rejected\] \S+ -> \S+ "
    r"\((?:failed|cannot lock ref[^)]*|incorrect old value[^)]*)\))",
    re.MULTILINE,
)

Edit = Callable[[pathlib.Path], bool]


class PushError(RuntimeError):
    """Nothing was published: git failed, or the remote refused the push."""


class OriginBusy(PushError):
    """Every attempt lost its race: origin/main kept moving."""


class RaceLost(Exception):
    """One attempt's push lost to another writer; the next starts from the new tip."""


def _run(
    repo: pathlib.Path, *args: str, timeout: float | None = None
) -> "subprocess.CompletedProcess[str]":
    return _git(repo, *args, capture_output=True, text=True, timeout=timeout)


def _network(repo: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """A git call that talks to origin; a hung remote fails the push instead of hanging it."""
    try:
        return _run(repo, *args, timeout=NETWORK_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise PushError(f"git {args[0]} timed out after {NETWORK_TIMEOUT:.0f}s") from e


def has_origin(root: pathlib.Path) -> bool:
    return _run(root, "remote", "get-url", "origin").returncode == 0


def fetch_origin(root: pathlib.Path) -> None:
    fetched = _network(root, "fetch", "-q", "origin", "main")
    if fetched.returncode != 0:
        raise PushError(f"git fetch origin main failed: {fetched.stderr.strip()}")


def read_origin(root: pathlib.Path, relative: str) -> str:
    """`relative` as of the last fetch of origin/main; empty when it is not there."""
    shown = _run(root, "show", f"origin/main:{relative}")
    return shown.stdout if shown.returncode == 0 else ""


def _backoff(attempt: int) -> None:
    time.sleep(random.uniform(0, min(0.25 * 2**attempt, 4.0)))


def _head(wt: pathlib.Path) -> str:
    return _run(wt, "rev-parse", "HEAD").stdout.strip()


def _try_push(wt: pathlib.Path, edit: Edit, message: str) -> str:
    """Edit, commit and push in `wt`; the pushed sha, or the tip's when there was nothing to publish."""
    if not edit(wt):
        return _head(wt)
    _run(wt, "add", "--all")
    if not _run(wt, "status", "--porcelain").stdout.strip():
        return _head(wt)
    commit = _run(wt, *IDENTITY, "commit", "-q", "-m", message)
    if commit.returncode != 0:
        raise PushError(f"commit failed: {commit.stdout.strip()} {commit.stderr.strip()}")
    push = _network(wt, "push", "origin", "HEAD:main")
    if push.returncode == 0:
        return _head(wt)
    if LOST_RACE.search(push.stderr):
        raise RaceLost
    raise PushError(f"push refused: {push.stderr.strip()}")


def push_from_fresh_origin(root: pathlib.Path, edit: Edit, message: str) -> str:
    """Each attempt starts again from the latest origin/main, so a lost race costs only a retry."""
    with tempfile.TemporaryDirectory(prefix="origin-push-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        for attempt in range(PUSH_ATTEMPTS):
            fetch_origin(root)
            added = _run(root, "worktree", "add", "--detach", str(wt), "origin/main")
            if added.returncode != 0:
                raise PushError(f"worktree add failed: {added.stderr.strip()}")
            try:
                return _try_push(wt, edit, message)
            except RaceLost:
                pass
            finally:
                _run(root, "worktree", "remove", "--force", str(wt))
            _backoff(attempt)
        raise OriginBusy(f"push to origin/main still rejected after {PUSH_ATTEMPTS} attempts")
