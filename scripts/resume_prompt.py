#!/usr/bin/env python3
"""
resume_prompt.py: print the prompt that resumes a long-running session in its next session.

Reads the session's open ledger items (with the queue brief each handoff recorded in
metadata.brief and the background agents or tasks still running for it in metadata.agents),
the closed items whose work a handoff moved into a still-open queue brief, and its mission
from the long-running registry, so the prompt is rebuilt from live state at every handoff
rather than written by hand.

Usage:
  python scripts/resume_prompt.py [SESSION_ID]     # defaults to $CLAUDE_CODE_SESSION_ID
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass

OPEN_STATUSES = ("in_progress", "pending")

INSTRUCTIONS = (
    "Each item's queue brief holds its exact state and next step. Read the brief and re-check "
    "live state (gh pr view, git log, worktree status) before acting: anything may have moved "
    "since the handoff. Recreate each item on your own task ledger with metadata.brief set to "
    "its path, then work them in this order. When an item ships, move its brief to done/. "
    "A background agent or task listed under an item was running at the handoff and has since "
    "finished or died: read its output file first and re-run it only when that output is missing. "
    "When you hand off, run /claude-tasks:handoff, which prints the next copy of this prompt."
)

HANDOFF_INSTRUCTIONS = (
    "These items were closed into queue briefs at the handoff. Start each with its command, "
    "which moves the brief to in-progress and works it to done:"
)


def _home() -> pathlib.Path:
    return pathlib.Path.home()


def ledger_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CLAUDE_TASKS_LEDGER_ROOT") or _home() / ".claude" / "tasks")


def registry_path() -> pathlib.Path:
    default = _home() / ".claude" / "long-running-sessions.json"
    return pathlib.Path(os.environ.get("LONG_RUNNING_REGISTRY") or default)


def projects_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CLAUDE_PROJECTS_ROOT") or _home() / ".claude" / "projects")


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
class Background:
    """A background agent or task still running when the session handed off."""

    kind: str
    name: str
    asked: str | None
    output: str | None
    collect: str | None

    @classmethod
    def from_json(cls, row: Mapping[str, object]) -> Background:
        return cls(
            _text(row, "kind") or "agent",
            _text(row, "name") or "(unnamed)",
            _text(row, "asked"),
            _text(row, "output"),
            _text(row, "collect"),
        )

    def line(self) -> str:
        parts = [
            f"  - running {self.kind} {self.name}",
            f"asked: {self.asked}" if self.asked else None,
            f"output {self.output}" if self.output else None,
            f"collect: {self.collect}" if self.collect else None,
        ]
        return "; ".join(p for p in parts if p)


def _backgrounds(meta: Mapping[str, object]) -> tuple[Background, ...]:
    rows = meta.get("agents")
    found = [r for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else []
    return tuple(Background.from_json(r) for r in found)


@dataclass(frozen=True)
class Item:
    id: str
    subject: str
    status: str
    brief: str | None
    blocker: str | None
    agents: tuple[Background, ...] = ()
    handed_to: str | None = None

    @classmethod
    def from_json(cls, row: Mapping[str, object]) -> Item:
        meta = row.get("metadata")
        meta = meta if isinstance(meta, Mapping) else {}
        return cls(
            str(row.get("id", "?")),
            _text(row, "subject") or "(no subject)",
            _text(row, "status") or "pending",
            _text(meta, "brief"),
            _text(meta, "blocker"),
            _backgrounds(meta),
            _text(meta, "handed_to"),
        )

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def order(self) -> tuple[int, bool, int]:
        """In-progress before pending, unblocked before blocked, then ledger order."""
        return (
            OPEN_STATUSES.index(self.status),
            self.blocker is not None,
            int(self.id) if self.id.isdigit() else sys.maxsize,
        )

    def line(self) -> str:
        parts = [
            f"- #{self.id} [{self.status}] {self.subject}",
            f"brief {self.brief}" if self.brief else "no brief yet: file one first",
        ]
        return "; ".join(parts + ([f"blocked: {self.blocker}"] if self.blocker else []))

    def lines(self) -> list[str]:
        """The item's line, then one indented line per background agent or task it waits on."""
        return [self.line(), *(a.line() for a in self.agents)]


def _is_done(path: pathlib.Path) -> bool:
    """In done/ already, or an absolute brief that has since moved there."""
    moved = (
        path.is_absolute()
        and not path.exists()
        and (path.parent.parent / "done" / path.name).exists()
    )
    return "done" in path.parts or moved


@dataclass(frozen=True)
class Handoff:
    """A completed ledger item whose work now lives in an open queue brief."""

    brief: pathlib.Path
    subject: str

    @classmethod
    def from_item(cls, item: Item) -> Handoff | None:
        target = item.handed_to or item.brief
        if item.status != "completed" or not target or not target.endswith(".md"):
            return None
        path = pathlib.Path(target)
        return None if _is_done(path) else cls(path, item.subject)

    def line(self) -> str:
        return (
            f"- /claude-tasks:action-task {self.brief.stem}  ({self.subject}; brief {self.brief})"
        )


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
        return session_id in (
            entry.get("id"),
            *(previous if isinstance(previous, list) else ()),
        ) or (entry.get("job") == session_id[:8])


def _items(session_id: str, root: pathlib.Path) -> list[Item]:
    rows = (_read_json(p) for p in sorted((root / session_id).glob("*.json")))
    return [Item.from_json(r) for r in rows if r]


def open_items(session_id: str, root: pathlib.Path) -> list[Item]:
    return sorted((i for i in _items(session_id, root) if i.is_open), key=lambda i: i.order)


def _blocks(line: str) -> list[Mapping[str, object]]:
    try:
        content = json.loads(line).get("message", {}).get("content")
    except (ValueError, AttributeError):
        return []
    return [b for b in content if isinstance(b, Mapping)] if isinstance(content, list) else []


def _accepted(
    call: Mapping[str, object], result: Mapping[str, object]
) -> Mapping[str, object] | None:
    """The call's input with its task id, or None when a gate refused it."""
    created = re.search(r"Task #(\d+) created", str(result.get("content")))
    task_id = call.get("taskId") or (created.group(1) if created else None)
    if result.get("is_error") or not task_id:
        return None
    return {**call, "taskId": task_id}


def _accepted_updates(transcript: pathlib.Path) -> list[Mapping[str, object]]:
    """TaskCreate and TaskUpdate inputs the harness accepted, in order."""
    sent: dict[object, Mapping[str, object]] = {}
    accepted: list[Mapping[str, object]] = []
    for line in transcript.read_text(errors="replace").splitlines():
        if "TaskUpdate" not in line and "TaskCreate" not in line and "tool_result" not in line:
            continue
        for b in _blocks(line):
            if b.get("type") == "tool_use" and b.get("name") in ("TaskCreate", "TaskUpdate"):
                call = b.get("input")
                sent[b.get("id")] = call if isinstance(call, Mapping) else {}
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in sent:
                if update := _accepted(sent.pop(b.get("tool_use_id")), b):
                    accepted.append(update)
    return accepted


def closed_items(session_id: str, projects: pathlib.Path) -> list[Item]:
    """Ledger items as the transcript last set them: Claude Code deletes a completed item's file."""
    transcript = next(iter(sorted(projects.glob(f"*/{session_id}.jsonl"))), None)
    rows: dict[str, dict[str, object]] = {}
    metadata: dict[str, dict[str, object]] = {}
    for update in _accepted_updates(transcript) if transcript else []:
        task_id = str(update.get("taskId"))
        row = rows.setdefault(task_id, {"id": task_id})
        row.update({k: update[k] for k in ("subject", "status") if isinstance(update.get(k), str)})
        meta = update.get("metadata")
        metadata.setdefault(task_id, {}).update(meta if isinstance(meta, Mapping) else {})
    return [Item.from_json({**row, "metadata": metadata[tid]}) for tid, row in rows.items()]


def handoffs(
    session_id: str, root: pathlib.Path, projects: pathlib.Path | None = None
) -> list[Handoff]:
    items = [*_items(session_id, root), *closed_items(session_id, projects or projects_root())]
    found = {h.brief: h for h in (Handoff.from_item(i) for i in items) if h}
    return list(found.values())


def header(session_id: str, mission: Mission | None) -> str:
    if mission is None:
        return f"Resume the open work of session {session_id}."
    named = f'Resume long-running session "{mission.name}" (was {session_id}).'
    return f"{named} Mission: {mission.statement}" if mission.statement else named


def _handoff_section(handed: list[Handoff]) -> str:
    return "\n".join([HANDOFF_INSTRUCTIONS, *(h.line() for h in handed)]) if handed else ""


def render(
    session_id: str, mission: Mission | None, items: list[Item], handed: list[Handoff] | None = None
) -> str:
    open_lines = "\n".join(line for i in items for line in i.lines())
    sections = [s for s in (open_lines, _handoff_section(handed or [])) if s]
    body = "\n\n".join(sections) or "- (no open items: seed the next one from the mission)"
    return f"{header(session_id, mission)}\n\n{INSTRUCTIONS}\n\n{body}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the prompt that resumes a long-running session."
    )
    parser.add_argument("session_id", nargs="?", default=os.environ.get("CLAUDE_CODE_SESSION_ID"))
    args = parser.parse_args(argv)
    if not args.session_id:
        parser.error("no SESSION_ID given and $CLAUDE_CODE_SESSION_ID is unset")
    mission = Mission.lookup(args.session_id, registry_path(), jobs_root())
    root = ledger_root()
    items, handed = open_items(args.session_id, root), handoffs(args.session_id, root)
    print(render(args.session_id, mission, items, handed), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
