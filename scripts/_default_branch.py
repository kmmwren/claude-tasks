"""The branch a queue's origin treats as its default, resolved once per clone and never assumed."""

import functools
import pathlib
import re
import subprocess

REMOTE_TIMEOUT = 20.0  # `git remote show` asks the remote, which must not hang a writer
_REMOTE_HEAD = re.compile(r"^\s*HEAD branch:\s*(\S+)\s*$", re.MULTILINE)


class DefaultBranchError(RuntimeError):
    """No source (origin/HEAD, the remote, the checkout) names a branch."""


def git_stdout(root: pathlib.Path, *args: str, timeout: float | None = None) -> str:
    """Git's stdout in `root`; empty when it fails or times out, so the next source is tried."""
    try:
        run = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return ""
    return run.stdout.strip() if run.returncode == 0 else ""


def _from_origin_head(root: pathlib.Path) -> str:
    ref = git_stdout(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return ref.removeprefix("origin/")


def _from_remote(root: pathlib.Path) -> str:
    found = _REMOTE_HEAD.search(
        git_stdout(root, "remote", "show", "origin", timeout=REMOTE_TIMEOUT)
    )
    return "" if found is None or found.group(1) == "(unknown)" else found.group(1)


def _from_checkout(root: pathlib.Path) -> str:
    return git_stdout(root, "symbolic-ref", "--short", "HEAD")


@functools.cache
def _resolve(root: str) -> str:
    path = pathlib.Path(root)
    for source in (_from_origin_head, _from_remote, _from_checkout):
        if branch := source(path):
            return branch
    raise DefaultBranchError(f"cannot tell which branch is the default for {root}")


def default_branch(root: pathlib.Path) -> str:
    """The default branch of `root`'s origin, from the cheapest source that answers; cached per root."""
    return _resolve(str(root.resolve()))


def forget() -> None:
    """Drop every cached answer, for a caller that has just changed a clone's remote."""
    _resolve.cache_clear()
