"""
_log_conflict.py: finish a scratch-worktree rebase whose only conflicts are Execution-log appends.

`append_log.py` pushes a log line to origin while a local writer moves or logs the same brief, so
the replayed commit and origin both edit the end of one file. `Rebase` resolves such a file as the
union of both sides (`_log_merge`), but only while the snapshot it rebases onto is still current:
once origin/main carries a newer version of the brief, the union is refused and the rebase aborts.
"""

import enum
import os
import pathlib
import subprocess
from dataclasses import dataclass

from _log_merge import merge_log_appends
from _paths import TASK_DIRS


class Verdict(enum.Enum):
    RESOLVED = "resolved"
    CONFLICT = "conflict"  # the sides differ by more than log appends, so a human must merge
    STALE = "stale"  # origin/main holds a newer version of the brief than the rebase's snapshot


@dataclass(frozen=True)
class RebaseResult:
    completed: bool
    conflict: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        """Why the rebase was abandoned, as a clause for a divergence report."""
        parts = []
        if self.conflict:
            parts.append(f"rebase conflict in {', '.join(self.conflict)}")
        if self.stale:
            parts.append(f"origin/main changed {', '.join(self.stale)} since the rebase began")
        return "; ".join(parts) or "rebase failed without a conflicting file"


def _run(worktree: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """Run git in `worktree` with the commit editor suppressed, so `rebase --continue` never waits."""
    env = {**os.environ, "GIT_EDITOR": "true"}
    return subprocess.run(
        ["git", "-C", str(worktree), *args], capture_output=True, text=True, env=env
    )


def _is_brief(path: str) -> bool:
    pure = pathlib.PurePath(path)
    return pure.suffix == ".md" and pure.parent.name in TASK_DIRS


@dataclass(frozen=True)
class Rebase:
    """A rebase of `worktree` onto the commit `onto`, pinned so a later fetch cannot move it."""

    worktree: pathlib.Path
    onto: str

    @classmethod
    def onto_origin(cls, worktree: pathlib.Path) -> "Rebase":
        return cls(worktree, _run(worktree, "rev-parse", "origin/main").stdout.strip())

    def run(self) -> RebaseResult:
        """Replay the local commits, resolving log-only conflicts; abort and say why otherwise."""
        step = _run(self.worktree, "rebase", self.onto)
        while step.returncode != 0:
            verdicts = {path: self.resolve(path) for path in self._conflicted()}
            if not verdicts or any(v is not Verdict.RESOLVED for v in verdicts.values()):
                _run(self.worktree, "rebase", "--abort")
                return self._failure(verdicts)
            step = _run(self.worktree, "rebase", "--continue")
        return RebaseResult(True)

    def resolve(self, path: str) -> Verdict:
        """Resolve a conflicted brief as the union of both sides' log appends, if that is safe."""
        merged = self._merged_log(path) if _is_brief(path) else None
        if merged is None:
            return Verdict.CONFLICT
        if self.snapshot_is_stale(path):
            return Verdict.STALE
        (self.worktree / path).write_text(merged)
        return Verdict.RESOLVED if _run(self.worktree, "add", "--", path).returncode == 0 else Verdict.CONFLICT

    def snapshot_is_stale(self, path: str) -> bool:
        """True when origin/main changed this brief, under any state dir, after `onto`.

        The brief is matched by file name, because a local move to another state dir would hide
        origin's edit from a path-limited log. An unreadable diff counts as stale.
        """
        diff = _run(self.worktree, "diff", "--name-only", self.onto, "origin/main")
        name = pathlib.PurePath(path).name
        return diff.returncode != 0 or any(pathlib.PurePath(p).name == name for p in diff.stdout.split())

    def _merged_log(self, path: str) -> str | None:
        """The union of both sides' log appends, or None if a side changed anything else."""
        base, ours, theirs = (self._stage_text(stage, path) for stage in (1, 2, 3))
        if base is None or ours is None or theirs is None:
            return None
        return merge_log_appends(base, ours, theirs)

    def _stage_text(self, stage: int, path: str) -> str | None:
        """A conflicted file at merge stage 1 (base), 2 (upstream) or 3 (replayed commit)."""
        shown = _run(self.worktree, "show", f":{stage}:./{path}")
        return shown.stdout if shown.returncode == 0 else None

    def _conflicted(self) -> list[str]:
        return _run(self.worktree, "diff", "--name-only", "--diff-filter=U").stdout.split()

    @staticmethod
    def _failure(verdicts: dict[str, Verdict]) -> RebaseResult:
        def named(kind: Verdict) -> tuple[str, ...]:
            return tuple(path for path, verdict in verdicts.items() if verdict is kind)

        return RebaseResult(False, conflict=named(Verdict.CONFLICT), stale=named(Verdict.STALE))
