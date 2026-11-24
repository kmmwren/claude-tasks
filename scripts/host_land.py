#!/usr/bin/env python3
"""
host_land.py: ship a queue that lives inside another repo, without touching that repo's checkout.

A project queue (`<repo>/projects/x/.tasks`) has no repo of its own, so `git_sync` cannot commit it
there. `land_host_queue` copies the queue's pending files into a temporary worktree cut from the host's
`origin/<default>`, records them as one commit and pushes it to `wip/tasks-<queue-slug>`. The host's index,
HEAD and working files stay as they were; merging the wip branch is the host's own land.

  python scripts/host_land.py <queue>
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import contextlib
import dataclasses
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator

from _advance import advance_to_origin
from _concurrency import NETWORK_TIMEOUT, repo_lock
from _credentials import carries_credential
from _default_branch import default_branch
from _paths import PROJECT_MARKER
from done_guard import DoneBackstop

DEFAULT_MESSAGE = "queue: sync uncommitted briefs"
WIP_PREFIX = "wip/tasks-"
STATE_PREFIX = ".tasks-"  # the per-clone lock and marks, which sit in the queue when it has no .git


class LandError(RuntimeError):
    """A git step failed; the queue's files are safe on disk and the next land retries."""


def _git(cwd: pathlib.Path, *args: str, timeout: float | None = None) -> str:
    """Git's stdout in `cwd`; a failure or a timeout raises LandError naming the subcommand."""
    try:
        run = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, encoding="utf-8", timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise LandError(f"git {args[0]} timed out after {e.timeout:.0f}s") from e
    if run.returncode != 0:
        raise LandError(f"git {args[0]}: {run.stderr.strip()}")
    return run.stdout


def _status_paths(porcelain: str) -> Iterable[str]:
    """Every path in `status --porcelain -z`; a rename yields both its new and its old path."""
    entries = iter(e for e in porcelain.split("\0") if e)
    for entry in entries:
        yield entry[3:]
        if entry[0] in "RC":
            yield next(entries)


@dataclasses.dataclass(frozen=True)
class HostQueue:
    """A queue directory and the repo that holds it."""

    root: pathlib.Path
    top: pathlib.Path

    @classmethod
    def of(cls, root: pathlib.Path) -> "HostQueue | None":
        """The queue's host repo; None when `root` is in no repo or is the top of its own."""
        try:
            top = pathlib.Path(_git(root, "rev-parse", "--show-toplevel").strip()).resolve()
        except LandError:
            return None
        return None if top == root.resolve() else cls(root.resolve(), top)

    @property
    def rel(self) -> pathlib.Path:
        return self.root.relative_to(self.top)

    @property
    def slug(self) -> str:
        """The queue's place in the host, `projects/x/.tasks` becoming `projects-x`."""
        parts = [p for p in self.rel.parts if p != PROJECT_MARKER] or [self.top.name]
        return re.sub(r"[^A-Za-z0-9]+", "-", "-".join(parts)).strip("-") or "queue"

    @property
    def branch(self) -> str:
        return f"{WIP_PREFIX}{self.slug}"

    def pending(self) -> list[str]:
        """Host-relative paths under the queue that the host has not recorded: new, changed or gone."""
        out = _git(
            self.top,
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
            "--",
            f":(literal){self.rel.as_posix()}",
        )
        paths = {
            p for p in _status_paths(out) if not pathlib.PurePath(p).name.startswith(STATE_PREFIX)
        }
        return sorted(paths)

    def return_unsettled_done(self, root: pathlib.Path) -> None:
        """Send a brief this land would ship into done/ with an open criterion back where it came from."""
        pending = set(self.pending())
        DoneBackstop(root).run(lambda rel: (self.rel / rel).as_posix() in pending)

    def shippable(self) -> list[str]:
        """`pending` less the files that carry a credential, which stay local."""
        pending = self.pending()
        held = [p for p in pending if self._carries_credential(p)]
        if held:  # a count, not names: the paths come from scanning credential-bearing files
            print(
                f"  (refused: {len(held)} held back for looking like a credential. "
                "Left unshipped; `git status` lists them. Remove the secret, then re-sync.)"
            )
        return [p for p in pending if p not in held]

    def _carries_credential(self, rel: str) -> bool:
        try:
            return carries_credential((self.top / rel).read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            return False  # deleted or unreadable: nothing to leak


@contextlib.contextmanager
def _worktree(queue: HostQueue, start: str) -> Iterator[pathlib.Path]:
    """A throwaway checkout of `start` on the queue's wip branch, upstream set; removed on exit."""
    branch = queue.branch
    with tempfile.TemporaryDirectory(prefix="host-land-") as tmp:
        wt = pathlib.Path(tmp) / "wt"
        _git(queue.top, "worktree", "add", "-q", "--no-track", "-B", branch, str(wt), start)
        try:
            _git(wt, "config", f"branch.{branch}.remote", "origin")
            _git(wt, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
            yield wt
        finally:
            subprocess.run(
                ["git", "-C", str(queue.top), "worktree", "remove", "--force", str(wt)],
                capture_output=True,
            )


def _mirror(top: pathlib.Path, wt: pathlib.Path, paths: Iterable[str]) -> None:
    """Make `wt` hold what `top` holds at each path: copied in when present, removed when gone."""
    for rel in paths:
        source, dest = top / rel, wt / rel
        if source.exists() or source.is_symlink():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest, follow_symlinks=False)
        else:
            dest.unlink(missing_ok=True)


def _record(wt: pathlib.Path, message: str) -> bool:
    """Commit what was mirrored; False when origin's branch already holds all of it."""
    _git(wt, "add", "--all")
    if not _git(wt, "diff", "--cached", "--name-only").strip():
        return False
    _git(wt, "commit", "-q", "-m", message)
    return True


def _ship(queue: HostQueue, paths: list[str], message: str) -> None:
    """The wip branch is rebuilt from origin's default each time and only this pushes it, so the push forces."""
    default = default_branch(queue.top)
    _git(queue.top, "fetch", "-q", "origin", default, timeout=NETWORK_TIMEOUT)
    with _worktree(queue, f"origin/{default}") as wt:
        _mirror(queue.top, wt, paths)
        if _record(wt, message):
            ref = f"HEAD:refs/heads/{queue.branch}"
            _git(wt, "push", "-q", "--force", "origin", ref, timeout=NETWORK_TIMEOUT)


def _on_default_and_behind(queue: HostQueue) -> bool:
    """True when the host sits on its default branch holding no commit that origin's copy lacks.

    A checkout on another branch, or ahead of origin, is somebody's work in progress and is never moved.
    """
    default = default_branch(queue.top)
    try:
        if _git(queue.top, "symbolic-ref", "--short", "-q", "HEAD").strip() != default:
            return False
        _git(queue.top, "merge-base", "--is-ancestor", "HEAD", f"origin/{default}")
    except LandError:
        return False  # detached, or HEAD holds commits origin lacks
    return True


def _advance_host(queue: HostQueue) -> None:
    """Bring the host checkout up to origin's default once the wip branch has merged there; never raises."""
    if not _on_default_and_behind(queue):
        return
    default = default_branch(queue.top)
    if not advance_to_origin(queue.top, default):
        print(
            "  (host checkout not advanced: uncommitted edits overlap incoming changes; "
            f"commit or move them, then run: git reset --keep origin/{default})"
        )


def _locked(root: pathlib.Path, lock_held: bool) -> contextlib.AbstractContextManager[bool]:
    return contextlib.nullcontext(True) if lock_held else repo_lock(root)


def land_host_queue(
    root: pathlib.Path, message: str = DEFAULT_MESSAGE, *, lock_held: bool = False
) -> bool:
    """Ship the queue's pending files to the host's `wip/tasks-<slug>` branch; True when none is left behind.

    False means deferred (no host repo, lock held, or a git step failed) and the files stay on disk.
    `lock_held` means the caller already holds the queue lock, which cannot be taken twice.
    """
    queue = HostQueue.of(root)
    if queue is None:
        return False
    with _locked(root, lock_held) as acquired:
        if not acquired:
            print("  (host land deferred: lock held; your files are safe on disk.)")
            return False
        try:
            queue.return_unsettled_done(root)
            paths = queue.shippable()
            if paths:
                _ship(queue, paths, message)
        except LandError as e:
            print(f"  (host land deferred: {e}; your files are safe on disk.)")
            return False
        if paths:
            _advance_host(queue)
    return True


def main(argv: list[str] | None = None) -> None:
    root = pathlib.Path((sys.argv[1:] if argv is None else argv)[0]).expanduser()
    queue = HostQueue.of(root)
    if queue is None:
        sys.exit(f"{root} is not a queue inside another repo")
    ok = land_host_queue(root)
    print(f"landed on origin/{queue.branch}" if ok else "deferred")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()  # pragma: no cover
