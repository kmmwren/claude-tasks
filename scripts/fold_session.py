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
from deps import fm
from git_sync import owns_repo, sync_commit

OPEN_DIRS = tuple(d for d in TASK_DIRS if d != "done")
_STUB = re.compile(r"\A---\n(?:(?!---\n).*\n)*?moved_to:")
_FRONTMATTER = re.compile(r"\A---\n.*?\n---", re.DOTALL)
_WORD = re.compile(r"(?<![\w:-])[\w-]+")
_DEPENDS_TAG = re.compile(r"(?<=depends:)[\w-]+")
_LIFECYCLE = re.compile(r'(?m)^lifecycle\s*=\s*"session"\s*$')


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
        binding = binding_path(self.id, env)
        with repo_lock(self.root) as acquired:
            if not acquired:
                raise SystemExit(f"Could not take the queue lock for {self.root}; nothing was folded.")
            moves = [self._reserve(path) for path in self.open_briefs()]
            renames = {_brief_id(m.source): m.dest.stem for m in moves if m.dest.stem != m.source.stem}
            for move in moves:
                self._move(move, renames)
            self._mark_folded()
        binding.unlink(missing_ok=True)
        self._sync(moves)
        return moves

    def _reserve(self, source: pathlib.Path) -> Move:
        target = self.fold_to / source.parent.name
        name = reserve_name(target, source.stem, [self.fold_to / d for d in TASK_DIRS])
        return Move(source, target / f"{name}.md")

    def _move(self, move: Move, renames: Mapping[str, str]) -> None:
        text = move.source.read_text()
        if renames:
            text = _retarget(text, renames, renames.get(_brief_id(move.source)))
        atomic_write(move.dest, text)
        atomic_write(move.source, f"---\nmoved_to: {move.dest}\n---\n\nFolded when session {self.id} ended.\n")

    def _mark_folded(self) -> None:
        config = self.root / CONFIG_FILE
        atomic_write(config, _LIFECYCLE.sub('lifecycle = "folded"', config.read_text()))

    def _sync(self, moves: list[Move]) -> None:
        message = f"fold session {self.id}: {len(moves)} brief(s) into {self.fold_to.name}"
        for root in (self.fold_to, self.root):
            if owns_repo(root):
                sync_commit(message, root)


def _brief_id(path: pathlib.Path) -> str:
    return fm(path.read_text()).get("id", path.stem)


def _retarget(text: str, renames: Mapping[str, str], own_id: str | None) -> str:
    """Rewrite a brief's own `id:` and its dependency references to the ids bumped in this fold."""
    match = _FRONTMATTER.match(text)
    if not match:
        return text
    lines = [_retarget_line(line, renames, own_id) for line in match.group().split("\n")]
    return "\n".join(lines) + text[match.end():]


def _retarget_line(line: str, renames: Mapping[str, str], own_id: str | None) -> str:
    key, sep, value = line.partition(":")
    def swap(m: re.Match[str]) -> str:
        return renames.get(m.group(), m.group())

    match key.strip():
        case "id" if own_id:
            return f"{key}{sep} {own_id}"
        case "blocked_by" | "blockers":
            return f"{key}{sep}{_WORD.sub(swap, value)}"
        case "tags":
            return f"{key}{sep}{_DEPENDS_TAG.sub(swap, value)}"
    return line


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Fold a session-linked queue into its parent.")
    ap.add_argument("root", help="the session-linked queue")
    a = ap.parse_args(argv)
    moves = SessionQueue.load(pathlib.Path(a.root).expanduser()).fold()
    print(f"Folded {len(moves)} brief(s)")


if __name__ == "__main__":
    main(sys.argv[1:])  # pragma: no cover
