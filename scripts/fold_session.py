#!/usr/bin/env python3
"""
fold_session.py — fold a session-linked queue back into its parent when the session ends.

Every brief outside done/ moves to the same status folder in the queue named by
`[session].fold_to`, leaving a stub whose frontmatter says `moved_to:`. The queue's
lifecycle becomes "folded" and the session's binding is removed, so later runs in that
session resolve as usual. Safe to run twice.

Usage:
  python scripts/fold_session.py ~/q/s1
"""
import argparse
import pathlib
import re
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from _concurrency import atomic_write, repo_lock, reserve_name
from _paths import CONFIG_FILE, TASK_DIRS, binding_path, require_queue
from config import load_config
from git_sync import owns_repo, sync_commit

OPEN_DIRS = tuple(d for d in TASK_DIRS if d != "done")
_STUB = re.compile(r"\A---\n(?:.*\n)*?moved_to:")
_LIFECYCLE = re.compile(r'(?m)^lifecycle = "session"$')


@dataclass(frozen=True)
class Move:
    source: pathlib.Path
    dest: pathlib.Path


@dataclass(frozen=True)
class SessionQueue:
    """A queue owned by one Claude Code session, and the queue it folds into."""

    root: pathlib.Path
    id: str
    fold_to: pathlib.Path

    @classmethod
    def load(cls, root: pathlib.Path) -> "SessionQueue":
        session = load_config(require_queue(root)).get("session")
        if not isinstance(session, dict) or not session.get("id") or not session.get("fold_to"):
            raise SystemExit(f"{root} is not session-linked (no [session] in {CONFIG_FILE}).")
        return cls(root, str(session["id"]), require_queue(pathlib.Path(session["fold_to"])))

    def open_briefs(self) -> Iterator[pathlib.Path]:
        for status in OPEN_DIRS:
            for path in sorted((self.root / status).glob("*.md")):
                if not _STUB.match(path.read_text()):
                    yield path

    def fold(self, env: Mapping[str, str] | None = None) -> list[Move]:
        with repo_lock(self.root):
            moves = [self._move(path) for path in self.open_briefs()]
            self._mark_folded()
        binding_path(self.id, env).unlink(missing_ok=True)
        self._sync(moves)
        return moves

    def _move(self, source: pathlib.Path) -> Move:
        target = self.fold_to / source.parent.name
        name = reserve_name(target, source.stem, [self.fold_to / d for d in TASK_DIRS])
        dest = target / f"{name}.md"
        atomic_write(dest, source.read_text())
        atomic_write(source, f"---\nmoved_to: {dest}\n---\n\nFolded when session {self.id} ended.\n")
        return Move(source, dest)

    def _mark_folded(self) -> None:
        config = self.root / CONFIG_FILE
        atomic_write(config, _LIFECYCLE.sub('lifecycle = "folded"', config.read_text()))

    def _sync(self, moves: list[Move]) -> None:
        message = f"fold session {self.id}: {len(moves)} brief(s) into {self.fold_to.name}"
        for root in (self.fold_to, self.root):
            if owns_repo(root):
                sync_commit(message, root)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Fold a session-linked queue into its parent.")
    ap.add_argument("root", help="the session-linked queue")
    a = ap.parse_args(argv)
    moves = SessionQueue.load(pathlib.Path(a.root).expanduser()).fold()
    print(f"Folded {len(moves)} brief(s)")


if __name__ == "__main__":
    main(sys.argv[1:])  # pragma: no cover
