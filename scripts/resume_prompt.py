#!/usr/bin/env python3
"""
resume_prompt.py: print the prompt that resumes a long-running session in its next session.

Reads the session's open ledger items (with the queue brief each handoff recorded in
metadata.brief) and its mission from the long-running registry, so the prompt is rebuilt from
live state at every handoff rather than written by hand.

Usage:
  python scripts/resume_prompt.py [SESSION_ID]     # defaults to $CLAUDE_CODE_SESSION_ID
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from collections.abc import Mapping
from dataclasses import dataclass

OPEN_STATUSES = ("in_progress", "pending")

INSTRUCTIONS = (
    "Each item's queue brief holds its exact state and next step. Read the brief and re-check "
    "live state (gh pr view, git log, worktree status) before acting: anything may have moved "
    "since the handoff. Recreate each item on your own task ledger with metadata.brief set to "
    "its path, then work them in this order. When an item ships, move its brief to done/. "
    "When you hand off, run /claude-tasks:handoff, which prints the next copy of this prompt."
)


def _home() -> pathlib.Path:
    return pathlib.Path.home()


def ledger_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CLAUDE_TASKS_LEDGER_ROOT") or _home() / ".claude" / "tasks")


def registry_path() -> pathlib.Path:
    default = _home() / ".claude" / "long-running-sessions.json"
    return pathlib.Path(os.environ.get("LONG_RUNNING_REGISTRY") or default)


def jobs_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("LONG_RUNNING_JOBS") or _home() / ".claude" / "jobs")


def _read_json(path: pathlib.Path) -> Mapping[str, object]:
    """The file's JSON object, or empty (with a warning when unreadable) so one bad file never sinks the prompt."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as err:
        print(f"resume_prompt: skipping unreadable {path}: {err}", file=sys.stderr)
        return {}
    return data if isinstance(data, Mapping) else {}


def _text(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class Item:
    id: str
    subject: str
    status: str
    brief: str | None
    blocker: str | None

    @classmethod
    def from_json(cls, row: Mapping[str, object]) -> Item:
        meta = row.get("metadata")
        meta = meta if isinstance(meta, Mapping) else {}
        return cls(str(row.get("id", "?")), _text(row, "subject") or "(no subject)",
                   _text(row, "status") or "pending", _text(meta, "brief"), _text(meta, "blocker"))

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def order(self) -> tuple[int, bool, int]:
        """In-progress before pending, unblocked before blocked, then ledger order."""
        return (OPEN_STATUSES.index(self.status), self.blocker is not None,
                int(self.id) if self.id.isdigit() else sys.maxsize)

    def line(self) -> str:
        parts = [f"- #{self.id} [{self.status}] {self.subject}",
                 f"brief {self.brief}" if self.brief else "no brief yet: file one first"]
        return "; ".join(parts + ([f"blocked: {self.blocker}"] if self.blocker else []))


@dataclass(frozen=True)
class Mission:
    name: str
    statement: str

    @classmethod
    def lookup(cls, session_id: str, registry: pathlib.Path, jobs: pathlib.Path) -> Mission | None:
        """The roster entry's mission, else a pinned session's job name with no statement."""
        return cls._from_roster(session_id, registry) or cls._from_job(session_id, jobs)

    @classmethod
    def _from_roster(cls, session_id: str, registry: pathlib.Path) -> Mission | None:
        if not registry.is_file():
            return None
        sessions = _read_json(registry).get("sessions")
        rows = [s for s in sessions if isinstance(s, Mapping)] if isinstance(sessions, list) else []
        match = next((s for s in rows if cls._owns(s, session_id)), None)
        return cls(_text(match, "name") or "", _text(match, "mission") or "") if match else None

    @classmethod
    def _from_job(cls, session_id: str, jobs: pathlib.Path) -> Mission | None:
        state = jobs / session_id[:8] / "state.json"
        name = _text(_read_json(state), "name") if state.is_file() else None
        return cls(name, "") if name else None

    @staticmethod
    def _owns(entry: Mapping[str, object], session_id: str) -> bool:
        previous = entry.get("previous_ids")
        return session_id in (entry.get("id"), *(previous if isinstance(previous, list) else ())) or (
            entry.get("job") == session_id[:8])


def open_items(session_id: str, root: pathlib.Path) -> list[Item]:
    items = (Item.from_json(_read_json(p)) for p in (root / session_id).glob("*.json"))
    return sorted((i for i in items if i.is_open), key=lambda i: i.order)


def header(session_id: str, mission: Mission | None) -> str:
    if mission is None:
        return f"Resume the open work of session {session_id}."
    named = f'Resume long-running session "{mission.name}" (was {session_id}).'
    return f"{named} Mission: {mission.statement}" if mission.statement else named


def render(session_id: str, mission: Mission | None, items: list[Item]) -> str:
    body = "\n".join(i.line() for i in items) or "- (no open items: seed the next one from the mission)"
    return f"{header(session_id, mission)}\n\n{INSTRUCTIONS}\n\n{body}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the prompt that resumes a long-running session.")
    parser.add_argument("session_id", nargs="?", default=os.environ.get("CLAUDE_CODE_SESSION_ID"))
    args = parser.parse_args(argv)
    if not args.session_id:
        parser.error("no SESSION_ID given and $CLAUDE_CODE_SESSION_ID is unset")
    mission = Mission.lookup(args.session_id, registry_path(), jobs_root())
    print(render(args.session_id, mission, open_items(args.session_id, ledger_root())), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
