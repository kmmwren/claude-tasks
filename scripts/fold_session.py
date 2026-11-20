#!/usr/bin/env python3
"""
fold_session.py — fold a session-linked queue back into its parent when the session ends.

Every brief outside done/ moves to the same status folder in the queue named by
`[session].fold_to`, leaving a stub whose frontmatter says `moved_to:`. The queue's
lifecycle becomes "folded" and the session's binding is removed, so later runs in that
session resolve as usual. A brief renamed on a name clash gets its id: and the references to it
among the folded briefs rewritten; done briefs the moved ones depend on are copied into the
parent's done/. A brief or facts file the fold reads that is not UTF-8 stops it before anything
is written. Safe to run twice.

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
from _paths import (
    CONFIG_FILE,
    TASK_DIRS,
    BriefFile,
    QueueBriefs,
    binding_path,
    require_queue,
    warn,
)
from config import load_config
from deps import BRIEF_DIRS, Brief, brief_id, fm, load_queue
from facts import facts_path
from git_sync import owns_repo, sync_commit

OPEN_DIRS = tuple(d for d in TASK_DIRS if d != "done")
NOT_FOLDED = "nothing is folded"
_STUB = re.compile(r"\A---\n(?:(?!---\n).*\n)*?moved_to:")
_FRONTMATTER = re.compile(r"\A---\n.*?\n---", re.DOTALL)
_WORD = re.compile(r"(?<![\w:-])[\w-]+")
_DEPENDS_TAG = re.compile(r"(?<=depends:)[\w-]+")
_LIFECYCLE = re.compile(r'(?m)^lifecycle\s*=\s*"session"\s*$')
_FACTS_BRIEF = re.compile(r'(?m)^brief\s*=\s*"[^"\n]*"')


@dataclass(frozen=True)
class Move:
    brief: BriefFile
    dest: pathlib.Path

    @property
    def source(self) -> pathlib.Path:
        return self.brief.path


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

    def open_briefs(self, session: QueueBriefs) -> Iterator[BriefFile]:
        """The session's briefs outside done/ that have not already been folded."""
        for status in OPEN_DIRS:
            yield from (brief for brief in session.folder(status) if not _STUB.match(brief.text))

    def fold(self, env: Mapping[str, str] | None = None) -> list[Move]:
        binding = binding_path(self.id, env)
        with repo_lock(self.root) as acquired:
            if not acquired:
                raise SystemExit(
                    f"Could not take the queue lock for {self.root}; nothing was folded."
                )
            session = QueueBriefs.read(self.root, consequence=NOT_FOLDED)
            parent = QueueBriefs.read(self.fold_to, consequence=NOT_FOLDED)
            self._require_utf8(session, parent)
            moves = [self._reserve(brief) for brief in self.open_briefs(session)]
            renames = {
                _brief_id(m.brief): m.dest.stem for m in moves if m.dest.stem != m.source.stem
            }
            for move in moves:
                self._move(move, renames)
            self._carry_done_prerequisites(moves, session, parent)
            self._mark_folded()
        binding.unlink(missing_ok=True)
        self._sync(moves)
        return moves

    def _require_utf8(self, session: QueueBriefs, parent: QueueBriefs) -> None:
        """Stop before the first write if a file the fold reads is not UTF-8, so no fold is left half done."""
        bad = [
            *session.undecodable(TASK_DIRS),
            *self._undecodable_facts(session),
            *parent.undecodable(BRIEF_DIRS),
        ]
        if bad:
            raise SystemExit(f"Not valid UTF-8, so nothing was folded: {', '.join(map(str, bad))}")

    def _undecodable_facts(self, session: QueueBriefs) -> Iterator[pathlib.Path]:
        """The facts files of the briefs being moved that are not UTF-8; no other facts file is read."""
        for brief in self.open_briefs(session):
            path = facts_path(self.root, _brief_id(brief))
            if path.exists() and not _is_utf8(path):
                yield path

    def _reserve(self, brief: BriefFile) -> Move:
        target = self.fold_to / brief.path.parent.name
        name = reserve_name(target, brief.path.stem, [self.fold_to / d for d in TASK_DIRS])
        return Move(brief, target / f"{name}.md")

    def _move(self, move: Move, renames: Mapping[str, str]) -> None:
        text = move.brief.text
        old_id = _brief_id(move.brief)
        new_id = renames.get(old_id, old_id)
        if renames:
            text = _retarget(text, renames, renames.get(old_id))
        atomic_write(move.dest, text)
        self._carry_facts(old_id, new_id)
        atomic_write(
            move.source,
            f"---\nmoved_to: {move.dest}\n---\n\nFolded when session {self.id} ended.\n",
        )

    def _carry_facts(self, old_id: str, new_id: str) -> None:
        """Move a brief's facts file to the parent under its new id, so the facts follow the brief."""
        source, dest = facts_path(self.root, old_id), facts_path(self.fold_to, new_id)
        if not source.exists():
            return
        if dest.exists():
            warn(f"{dest} already exists; {source} was not carried")
            return
        atomic_write(dest, _retarget_facts(source.read_text(encoding="utf-8"), new_id))
        source.unlink()

    def _carry_done_prerequisites(
        self, moves: list[Move], session: QueueBriefs, parent: QueueBriefs
    ) -> None:
        """Copy done briefs the moved ones depend on, since the parent only counts its own done/ as met."""
        wanted: set[str] = set()
        counted = {b.id for b in load_queue(parent)}
        for move in moves:
            fields = fm(move.dest.read_text(encoding="utf-8"))
            brief = Brief("", "", fields)
            wanted |= brief.declared_ids | brief.legacy_tokens
            # `parent` was read before the moves, so the moved briefs it now counts are added here.
            if move.dest.parent.name in BRIEF_DIRS:
                counted.add(brief_id(fields, move.dest.stem))
        for done in session.folder("done"):
            if _brief_id(done) in wanted - counted:
                atomic_write(self._reserve(done).dest, done.text)

    def _mark_folded(self) -> None:
        config = self.root / CONFIG_FILE
        atomic_write(
            config, _LIFECYCLE.sub('lifecycle = "folded"', config.read_text(encoding="utf-8"))
        )

    def _sync(self, moves: list[Move]) -> None:
        message = f"fold session {self.id}: {len(moves)} brief(s) into {self.fold_to.name}"
        for root in (self.fold_to, self.root):
            if owns_repo(root):
                sync_commit(message, root, paths=(".",))


def _is_utf8(path: pathlib.Path) -> bool:
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _brief_id(brief: BriefFile) -> str:
    return fm(brief.text).get("id", brief.path.stem)


def _retarget(text: str, renames: Mapping[str, str], own_id: str | None) -> str:
    """Rewrite a brief's own `id:` and its dependency references to the ids bumped in this fold."""
    match = _FRONTMATTER.match(text)
    if not match:
        return text
    lines = [_retarget_line(line, renames, own_id) for line in match.group().split("\n")]
    return "\n".join(lines) + text[match.end() :]


def _retarget_facts(text: str, new_id: str) -> str:
    """Point a facts file's `brief` at `new_id`; a file that names no brief is left as it is."""
    return _FACTS_BRIEF.sub(lambda _: f'brief = "{new_id}"', text, count=1)


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
