"""
_log_conflict.py: finish a scratch-worktree rebase whose only conflicts are Execution-log appends.

`append_log.py` pushes a log line to origin while a local writer moves or logs the same brief, so
the replayed commit and origin both edit the end of one file. `Rebase` resolves such a file as the
union of both sides (`_log_merge`). A union built on an origin/main that has since moved is never
published: the fast-forward-only push refuses it and the caller reports the divergence.
"""

import enum
import os
import pathlib
import subprocess
from dataclasses import dataclass

from _log_merge import merge_log_appends
from _paths import is_brief


class Verdict(enum.Enum):
    RESOLVED = "resolved"
    CONFLICT = "conflict"  # the sides differ by more than log appends, so a human must merge


class Stage(enum.IntEnum):
    """The index stage git keeps for each version of a conflicted file."""

    BASE = 1
    UPSTREAM = 2
    REPLAYED = 3


@dataclass(frozen=True)
class RebaseResult:
    completed: bool
    conflict: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        """Why the rebase was abandoned, as a clause for a divergence report."""
        if self.conflict:
            return f"rebase conflict in {', '.join(self.conflict)}"
        return "rebase failed without a conflicting file"


def _run(worktree: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """Run git in `worktree` with the commit editor suppressed, so `rebase --continue` never waits."""
    env = {**os.environ, "GIT_EDITOR": "true"}
    return subprocess.run(
        ["git", "-C", str(worktree), *args], capture_output=True, text=True, env=env
    )


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
        merged = self._merged_log(path) if is_brief(path) else None
        if merged is None:
            return Verdict.CONFLICT
        (self.worktree / path).write_text(merged)
        return (
            Verdict.RESOLVED
            if _run(self.worktree, "add", "--", path).returncode == 0
            else Verdict.CONFLICT
        )

    def _merged_log(self, path: str) -> str | None:
        """The union of both sides' log appends, or None if a side changed anything else."""
        base, upstream, replayed = (self._stage_text(stage, path) for stage in Stage)
        if base is None or upstream is None or replayed is None:
            return None
        return merge_log_appends(base, upstream, replayed)

    def _stage_text(self, stage: Stage, path: str) -> str | None:
        """A conflicted file as it stands at `stage`, or None when git has no such version."""
        shown = _run(self.worktree, "show", f":{int(stage)}:./{path}")
        return shown.stdout if shown.returncode == 0 else None

    def _conflicted(self) -> list[str]:
        return _run(self.worktree, "diff", "--name-only", "--diff-filter=U").stdout.split()

    @staticmethod
    def _failure(verdicts: dict[str, Verdict]) -> RebaseResult:
        return RebaseResult(
            False, conflict=tuple(path for path, v in verdicts.items() if v is Verdict.CONFLICT)
        )
