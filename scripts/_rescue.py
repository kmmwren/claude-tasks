"""Move a checkout onto origin's default branch even when uncommitted edits block `reset --keep`.

A queue inside a host repo is written in place and landed on a wip branch, so the host's working files
stay modified for good and the checkout falls further behind origin each day. `advance_or_rescue` first
tries the plain advance; when git refuses it, the blocking edits are copied to a pushed
`superseded/rescue/<repo>-<date>` tag, taken out of the way only once origin holds them, and the advance
runs again. Nothing is deleted that origin does not already hold.

The ref is a `superseded/*` tag, not a branch, because a host repo's pre-push hook may run its full test
gate for every branch push and exempt that tag namespace as the designed backup path.
"""

import dataclasses
import pathlib
import shutil
import subprocess
import tempfile
from collections.abc import Iterable

from _advance import _entries, _incoming_blobs, _run, advance_to_origin

RESCUE_PREFIX = "superseded/rescue/"
PUSH_TIMEOUT = 120.0


class RescueError(RuntimeError):
    """A step failed before the edits were safe on origin, so the checkout was left untouched."""


@dataclasses.dataclass(frozen=True)
class Rescue:
    """The edits that went to the tag `tag` at `sha`."""

    tag: str
    sha: str
    paths: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Advance:
    """What one attempt to level a checkout with origin did."""

    advanced: bool
    rescue: Rescue | None = None
    detail: str = ""


def _git(root: pathlib.Path, *args: str, timeout: float | None = None) -> str:
    """Git's stdout in `root`; a failure or a timeout raises RescueError naming the subcommand."""
    try:
        run = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise RescueError(f"git {args[0]} timed out after {e.timeout:.0f}s") from e
    if run.returncode != 0:
        raise RescueError(f"git {args[0]}: {run.stderr.strip()}")
    return run.stdout


def _blocking(root: pathlib.Path, ref: str) -> list[str]:
    """Paths edited in the working tree that `ref` also changes: what makes `reset --keep` refuse."""
    changed = set(_entries(root, "diff", "--name-only", "--no-renames", "-z", "HEAD", ref))
    edited = set(_entries(root, "diff", "--name-only", "--no-renames", "-z", "HEAD"))
    edited |= set(_entries(root, "ls-files", "-z", "--others", "--exclude-standard"))
    return sorted(edited & changed)


def _blob_on_disk(root: pathlib.Path, rel: str) -> str | None:
    """The blob id of the file as a commit would record it; None when it is absent or unreadable."""
    path = root / rel
    if not (path.exists() or path.is_symlink()):
        return None
    run = _run(root, "hash-object", "--", rel)
    return run.stdout.decode().strip() if run.returncode == 0 else None


def _blob_in(root: pathlib.Path, sha: str, rel: str) -> str | None:
    run = _run(root, "rev-parse", "--verify", "-q", f"{sha}:{rel}")
    return run.stdout.decode().strip() if run.returncode == 0 else None


def _discard(root: pathlib.Path, rels: list[str]) -> None:
    """Return each path to HEAD's version, or delete it when HEAD has none."""
    in_head = set(_entries(root, "ls-tree", "-r", "-z", "--name-only", "HEAD"))
    for rel in (r for r in rels if r not in in_head):
        (root / rel).unlink(missing_ok=True)
    if tracked := [r for r in rels if r in in_head]:
        run = subprocess.run(
            [
                "git", "-C", str(root), "checkout", "HEAD",
                "--pathspec-from-file=-", "--pathspec-file-nul",
            ],
            input="\0".join(tracked).encode(),
            capture_output=True,
        )
        if run.returncode != 0:
            raise RescueError(f"git checkout: {run.stderr.decode().strip()}")


def _mirror(root: pathlib.Path, wt: pathlib.Path, rels: Iterable[str]) -> None:
    """Make `wt` hold what `root` holds at each path: copied in when present, removed when gone."""
    for rel in rels:
        source, dest = root / rel, wt / rel
        if source.exists() or source.is_symlink():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.unlink(missing_ok=True)
            shutil.copy2(source, dest, follow_symlinks=False)
        else:
            dest.unlink(missing_ok=True)


def _free_tag(root: pathlib.Path, stem: str) -> str:
    """`superseded/rescue/<stem>`, or the first `-2`, `-3`... beside it that origin does not hold."""
    pattern = f"refs/tags/{RESCUE_PREFIX}{stem}*"
    listed = _git(root, "ls-remote", "--tags", "origin", pattern, timeout=PUSH_TIMEOUT)
    held = {line.split("refs/tags/", 1)[1] for line in listed.splitlines()}
    name, n = f"{RESCUE_PREFIX}{stem}", 1
    while name in held:
        n += 1
        name = f"{RESCUE_PREFIX}{stem}-{n}"
    return name


def _push_rescue(root: pathlib.Path, rels: list[str], stem: str) -> Rescue:
    """Commit the working copies of `rels` on HEAD in a throwaway worktree and push; origin must hold the sha."""
    tag = _free_tag(root, stem)
    with tempfile.TemporaryDirectory(prefix="rescue-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        _git(root, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
        try:
            _mirror(root, wt, rels)
            _git(wt, "add", "--all")
            _git(wt, "commit", "-q", "-m", f"rescue: {len(rels)} uncommitted edits from {root.name}")
            sha = _git(wt, "rev-parse", "HEAD").strip()
            _git(wt, "push", "-q", "origin", f"{sha}:refs/tags/{tag}", timeout=PUSH_TIMEOUT)
        finally:
            _run(root, "worktree", "remove", "--force", str(wt))
    if sha not in _git(root, "ls-remote", "--tags", "origin", f"refs/tags/{tag}", timeout=PUSH_TIMEOUT):
        raise RescueError(f"origin does not hold {sha[:9]} on tag {tag} after the push")
    return Rescue(tag, sha, tuple(rels))


def _unchanged_since(root: pathlib.Path, rescue: Rescue) -> list[str]:
    """The rescued paths still exactly as they were copied, so safe to take out of the way."""
    return [
        rel for rel in rescue.paths if _blob_on_disk(root, rel) == _blob_in(root, rescue.sha, rel)
    ]


def _redundant(root: pathlib.Path, ref: str, rels: list[str]) -> list[str]:
    """Blocking paths whose working copy is already what `ref` holds, so origin has them."""
    incoming = _incoming_blobs(root, ref)
    return [rel for rel in rels if _blob_on_disk(root, rel) == incoming.get(rel)]


def advance_or_rescue(root: pathlib.Path, branch: str, today: str) -> Advance:
    """Advance onto origin/<branch>; if edits block it, rescue them to a pushed tag first. Never raises.

    The caller has fetched and checked that HEAD is on `branch` and an ancestor of origin's copy.
    """
    if advance_to_origin(root, branch):
        return Advance(True)
    ref, rescue = f"origin/{branch}", None
    try:
        blocking = _blocking(root, ref)
        same = _redundant(root, ref, blocking)
        _discard(root, same)
        if rest := [r for r in blocking if r not in same]:
            rescue = _push_rescue(root, rest, f"{root.name}-{today}")
            _discard(root, _unchanged_since(root, rescue))
    except RescueError as e:
        return Advance(False, rescue, str(e))
    if advance_to_origin(root, branch):
        return Advance(True, rescue)
    return Advance(False, rescue, "reset --keep still refused after the rescue")
