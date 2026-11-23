"""Move a checkout onto origin's default branch past briefs it already holds untracked.

A queue's writes reach origin by another route (the wip branch of a nested queue, a rebase in a scratch
worktree), so once that lands the checkout still holds the same files untracked and git refuses to overwrite
them. An untracked file whose content equals the incoming blob is safe to take out of the way, because the
reset brings it straight back; one that differs is never touched.
"""

import dataclasses
import os
import pathlib
import subprocess


@dataclasses.dataclass(frozen=True)
class Held:
    """An untracked file taken out of the way, with what putting it back needs."""

    rel: str
    blob: str
    mode: int


def _run(root: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True)


def _entries(root: pathlib.Path, *args: str) -> list[str]:
    """The NUL-separated entries git prints; empty when the command fails (an unborn HEAD, say)."""
    run = _run(root, *args)
    return [e.decode() for e in run.stdout.split(b"\0") if e] if run.returncode == 0 else []


def _incoming_blobs(root: pathlib.Path, ref: str) -> dict[str, str]:
    """Path to blob id for every file in `ref`'s tree."""
    parsed = (e.partition("\t") for e in _entries(root, "ls-tree", "-r", "-z", ref))
    return {path: meta.split()[2] for meta, _, path in parsed if meta.split()[1] == "blob"}


def _same_content(root: pathlib.Path, rel: str, blob: str) -> bool:
    """True when the file on disk hashes to `blob`, under the same filters a commit would apply."""
    run = _run(root, "hash-object", "--", rel)
    return run.returncode == 0 and run.stdout.decode().strip() == blob


def _candidates(root: pathlib.Path, ref: str) -> dict[str, str]:
    """Untracked files that HEAD lacks and `ref` holds, with the blob `ref` holds there."""
    incoming = _incoming_blobs(root, ref)
    in_head = set(_entries(root, "ls-tree", "-r", "-z", "--name-only", "HEAD"))
    untracked = _entries(root, "ls-files", "-z", "--others", "--exclude-standard")
    return {rel: incoming[rel] for rel in untracked if rel in incoming and rel not in in_head}


def _take_away(root: pathlib.Path, rel: str, blob: str) -> Held | None:
    """Remove the file when its content is the incoming blob's; None when it differs or cannot go."""
    path = root / rel
    if path.is_symlink() or not _same_content(root, rel, blob):
        return None
    try:
        mode = path.stat().st_mode & 0o777
        path.unlink()
    except OSError:
        return None
    return Held(rel, blob, mode)


def _put_back(root: pathlib.Path, held: Held) -> None:
    """Write the file again from the blob it was equal to."""
    path = root / held.rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_run(root, "cat-file", "blob", held.blob).stdout)
    os.chmod(path, held.mode)


def advance_to_origin(root: pathlib.Path, branch: str) -> bool:
    """`reset --keep origin/<branch>` after clearing untracked copies of incoming files; False when it refuses.

    A refusal puts every cleared file back, so the checkout is exactly as it was.
    """
    ref = f"origin/{branch}"
    cleared = [
        held
        for rel, blob in _candidates(root, ref).items()
        if (held := _take_away(root, rel, blob)) is not None
    ]
    if _run(root, "reset", "--keep", ref).returncode == 0:
        return True
    for held in cleared:
        _put_back(root, held)
    return False
