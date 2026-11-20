"""A project queue pushed to a bare remote, so roll-up tests read it the way users do: from the default branch."""

import os
import pathlib
import subprocess
from collections.abc import Callable

import queue_snapshot as qs

from tests.test_git_sync import git, make_repo

QUEUE_PATH = ".tasks"
PUSHED_AT = "2026-11-19T09:00:00+00:00"


def push(clone: pathlib.Path, message: str, when: str = PUSHED_AT) -> str:
    """Commit everything in `clone` at a fixed date and push it; returns the pushed sha."""
    git(clone, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(
        ["git", "-C", str(clone), "commit", "-qm", message],
        check=True,
        capture_output=True,
        env=env,
    )
    git(clone, "push", "-q", "origin", "main")
    return str(git(clone, "rev-parse", "HEAD").stdout.strip())


def publish(
    tmp_path: pathlib.Path, build: Callable[[pathlib.Path], object], when: str = PUSHED_AT
) -> pathlib.Path:
    """Run `build` on a fresh clone (it writes `<clone>/.tasks`), push it, and return the bare remote."""
    clone = make_repo(tmp_path)
    build(clone)
    push(clone, "queue", when)
    return tmp_path / "remote"


def snapshot(remote: pathlib.Path, cache: pathlib.Path) -> qs.QueueSnapshot:
    """The pushed queue as the roll-up reads it."""
    return qs.read_queue(remote, QUEUE_PATH, cache_dir=cache)
