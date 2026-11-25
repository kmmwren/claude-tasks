#!/usr/bin/env python3
"""
origin_push.py: land one commit on origin's default branch from a scratch worktree cut at its tip.

The caller's checkout is never written. `edit` changes files in the worktree and returns False to
publish nothing. A push that lost a race to another writer starts again from the new tip with
jittered backoff; any other refusal fails at once, so a declined push is never retried.

Stdlib only.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import pathlib
import random
import re
import subprocess
import tempfile
import time
from collections.abc import Callable

from _concurrency import NETWORK_TIMEOUT
from _default_branch import default_branch

PUSH_ATTEMPTS = 8
IDENTITY = ("-c", "user.name=claude-tasks", "-c", "user.email=noreply@anthropic.com")
# A ref status line saying another writer won the race to update the branch, so a fresh retry can
# land. A server-side ref-lock clash reports "[remote rejected] ... (failed)" or, from git 2.54,
# "(reference already exists)"; hook output on "remote:" lines is ignored, so a declined push
# never matches.
LOST_RACE = re.compile(
    r"^ ! \[(?:rejected\]|remote rejected\] \S+ -> \S+ "
    r"\((?:failed|reference already exists|cannot lock ref[^)]*|incorrect old value[^)]*)\))",
    re.MULTILINE,
)

Edit = Callable[[pathlib.Path], bool]


class PushError(RuntimeError):
    """Nothing was published: git failed, or the remote refused the push."""


class PushRefused(PushError):
    """The remote answered the push with a refusal (permissions, protection, a hook), not a lost race."""


class OriginBusy(PushError):
    """Every attempt lost its race: the branch kept moving."""


class RaceLost(Exception):
    """One attempt's push lost to another writer; the next starts from the new tip."""


def lost_race(push_stderr: str) -> bool:
    return LOST_RACE.search(push_stderr) is not None


def _run(
    repo: pathlib.Path, *args: str, timeout: float | None = None
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout
    )


def _network(repo: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """A git call that talks to origin; a hung remote fails the push instead of hanging it."""
    try:
        return _run(repo, *args, timeout=NETWORK_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise PushError(f"git {args[0]} timed out after {NETWORK_TIMEOUT:.0f}s") from e


def fetch_origin(root: pathlib.Path, branch: str) -> None:
    fetched = _network(root, "fetch", "-q", "origin", branch)
    if fetched.returncode != 0:
        raise PushError(f"git fetch origin {branch} failed: {fetched.stderr.strip()}")


def _backoff(attempt: int) -> None:
    time.sleep(random.uniform(0, min(0.25 * 2**attempt, 4.0)))


def _head(wt: pathlib.Path) -> str:
    return _run(wt, "rev-parse", "HEAD").stdout.strip()


def _stage(wt: pathlib.Path, edit: Edit) -> bool:
    """Run `edit` and stage its changes; whether there is anything to commit."""
    if not edit(wt):
        return False
    added = _run(wt, "add", "--all")
    if added.returncode != 0:
        raise PushError(f"git add failed: {added.stderr.strip()}")
    return bool(_run(wt, "status", "--porcelain").stdout.strip())


def _commit(wt: pathlib.Path, message: str) -> None:
    commit = _run(wt, *IDENTITY, "commit", "-q", "-m", message)
    if commit.returncode != 0:
        raise PushError(f"commit failed: {commit.stdout.strip()} {commit.stderr.strip()}")


def _push_head(wt: pathlib.Path, branch: str) -> None:
    push = _network(wt, "push", "origin", f"HEAD:{branch}")
    if push.returncode == 0:
        return
    if lost_race(push.stderr):
        raise RaceLost
    raise PushRefused(f"push refused: {push.stderr.strip()}")


def _try_push(wt: pathlib.Path, branch: str, edit: Edit, message: str) -> str:
    """Edit, commit and push in `wt`; the pushed sha, or the tip's when there was nothing to publish."""
    if _stage(wt, edit):
        _commit(wt, message)
        _push_head(wt, branch)
    return _head(wt)


def _attempt(root: pathlib.Path, wt: pathlib.Path, branch: str, edit: Edit, message: str) -> str:
    """One try from the fetched tip, in a scratch worktree that is always removed."""
    added = _run(root, "worktree", "add", "--detach", str(wt), f"origin/{branch}")
    if added.returncode != 0:
        raise PushError(f"worktree add failed: {added.stderr.strip()}")
    try:
        return _try_push(wt, branch, edit, message)
    finally:
        _run(root, "worktree", "remove", "--force", str(wt))


def push_from_fresh_origin(root: pathlib.Path, edit: Edit, message: str) -> str:
    """Each attempt starts again from the latest tip, so a lost race costs only a retry."""
    branch = default_branch(root)
    with tempfile.TemporaryDirectory(prefix="origin-push-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        for attempt in range(PUSH_ATTEMPTS):
            fetch_origin(root, branch)
            try:
                return _attempt(root, wt, branch, edit, message)
            except RaceLost:
                _backoff(attempt)
        raise OriginBusy(f"push to origin/{branch} still rejected after {PUSH_ATTEMPTS} attempts")
