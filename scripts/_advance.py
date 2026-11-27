"""Move a checkout onto origin's default branch past files it already holds in their landed state.

A queue's writes reach origin by another route (a nested queue's commit on the host's default branch, a
rebase in a scratch worktree), so once that lands the checkout still holds the same files as untracked,
modified or deleted, and git refuses to overwrite them. A file whose on-disk state equals the incoming
state is safe to take out of the way, because the reset brings it straight back; one that differs, or that
is staged, is never touched.
"""

import dataclasses
import os
import pathlib
import subprocess

ABSENT = "0" * 40  # the object id `diff --raw` prints for a side that lacks the file
REGULAR_MODES = {"100644", "100755"}  # a symlink or submodule is left to git


@dataclasses.dataclass(frozen=True)
class Change:
    """A file `HEAD` and the incoming ref differ at; a side that lacks the file has no blob."""

    rel: str
    old: str | None
    new: str | None


@dataclasses.dataclass(frozen=True)
class Held:
    """A file taken out of the way, with what putting it back needs."""

    change: Change
    mode: int


def _run(root: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True)


def _entries(root: pathlib.Path, *args: str) -> list[str]:
    """The NUL-separated entries git prints; empty when the command fails (an unborn HEAD, say)."""
    run = _run(root, *args)
    return [e.decode() for e in run.stdout.split(b"\0") if e] if run.returncode == 0 else []


def _blob(oid: str) -> str | None:
    return None if oid == ABSENT else oid


def _changes(root: pathlib.Path, ref: str) -> list[Change]:
    """Every regular file `HEAD` and `ref` differ at, which is all a `reset --keep` would rewrite."""
    entries = iter(
        _entries(root, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", "HEAD", ref)
    )
    changes = []
    for meta, rel in zip(entries, entries):
        old_mode, new_mode, old, new, _status = meta.lstrip(":").split()
        if {old_mode, new_mode} - REGULAR_MODES - {"000000"}:
            continue
        changes.append(Change(rel, _blob(old), _blob(new)))
    return changes


def _same_content(root: pathlib.Path, rel: str, blob: str) -> bool:
    """True when the file on disk hashes to `blob`, under the same filters a commit would apply."""
    run = _run(root, "hash-object", "--", rel)
    return run.returncode == 0 and run.stdout.decode().strip() == blob


def _is_landed(root: pathlib.Path, change: Change) -> bool:
    """True when the file on disk is already in the incoming state: those bytes, or gone."""
    path = root / change.rel
    if path.is_symlink():
        return False
    if change.new is None:
        return not path.exists()
    return path.is_file() and _same_content(root, change.rel, change.new)


def _candidates(root: pathlib.Path, ref: str) -> list[Change]:
    """Files already in their incoming state that `reset --keep` would refuse, none of them staged."""
    staged = set(_entries(root, "diff", "--cached", "--name-only", "-z"))
    return [c for c in _changes(root, ref) if c.rel not in staged and _is_landed(root, c)]


def _mode(path: pathlib.Path) -> int:
    return path.stat().st_mode & 0o777 if path.exists() else 0


def _take_away(root: pathlib.Path, change: Change) -> Held | None:
    """Put the file back to what HEAD holds (or remove it when HEAD lacks it); None when it cannot go."""
    path = root / change.rel
    held = Held(change, _mode(path))
    if change.old is not None:
        restored = _run(root, "checkout-index", "-f", "-u", "--", change.rel)
        return held if restored.returncode == 0 else None
    try:
        path.unlink()
    except OSError:
        return None
    return held


def _put_back(root: pathlib.Path, held: Held) -> None:
    """Restore the landed state the file was in: its incoming bytes, or gone."""
    path = root / held.change.rel
    if held.change.new is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_run(root, "cat-file", "blob", held.change.new).stdout)
    os.chmod(path, held.mode)


def advance_to_origin(root: pathlib.Path, branch: str) -> bool:
    """`reset --keep origin/<branch>` after clearing files already in their incoming state; False when it refuses.

    A refusal puts every cleared file back, so the checkout is exactly as it was.
    """
    ref = f"origin/{branch}"
    cleared = [
        held for change in _candidates(root, ref) if (held := _take_away(root, change)) is not None
    ]
    if _run(root, "reset", "--keep", ref).returncode == 0:
        return True
    for held in cleared:
        _put_back(root, held)
    return False
