#!/usr/bin/env python3
"""The pick log: every ranked list shown and what was then picked, bumped or skipped.

Append-only JSONL under a per-queue state dir outside the queue repo, so showing a list never costs a
git write. Each `shown` event keeps the list, the filter order, the profile version and the raw judge
output, so learn.py can replay any display against a candidate profile.
"""
from __future__ import annotations

import dataclasses
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import sys
import uuid
from collections.abc import Iterator
from typing import Any, Literal

STATE_ENV = "CLAUDE_TASKS_STATE"
DEFAULT_STATE = "~/.claude-tasks/predictor"
LOG_FILE = "picks.jsonl"
DEFAULT_WINDOW_HOURS = 24.0

ChoiceKind = Literal["pick", "bump", "skip"]
CHOICES: tuple[ChoiceKind, ...] = ("pick", "bump", "skip")


def state_dir(root: pathlib.Path) -> pathlib.Path:
    """One state dir per queue, keyed by its resolved path so two checkouts never share a log."""
    base = pathlib.Path(os.environ.get(STATE_ENV) or DEFAULT_STATE).expanduser()
    return base / hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@dataclasses.dataclass(frozen=True)
class ShownItem:
    id: str
    goals: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Shown:
    scope: str
    profile_version: str
    source: str
    items: tuple[ShownItem, ...]
    filter_order: tuple[str, ...]
    judge_output: tuple[str, ...] = ()
    impression: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex[:12])
    at: str = dataclasses.field(default_factory=_now)
    event: str = "shown"

    def position(self, brief_id: str) -> int | None:
        return next((i for i, item in enumerate(self.items) if item.id == brief_id), None)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Shown:
        items = tuple(ShownItem(i["id"], tuple(i.get("goals", ()))) for i in d["items"])
        return cls(d["scope"], d["profile_version"], d["source"], items, tuple(d["filter_order"]),
                   tuple(d.get("judge_output", ())), d["impression"], d["at"])


@dataclasses.dataclass(frozen=True)
class Choice:
    kind: ChoiceKind
    brief_id: str
    impression: str | None = None
    position: int | None = None
    at: str = dataclasses.field(default_factory=_now)
    event: str = "choice"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Choice:
        return cls(d["kind"], d["brief_id"], d.get("impression"), d.get("position"), d["at"])


class PickLog:
    def __init__(self, root: pathlib.Path) -> None:
        self.path = state_dir(root) / LOG_FILE

    def append(self, event: Shown | Choice) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)  # a shown line can exceed PIPE_BUF, so O_APPEND alone may interleave
            fh.write(json.dumps(dataclasses.asdict(event), sort_keys=True) + "\n")

    def events(self) -> Iterator[Shown | Choice]:
        if not self.path.is_file():
            return
        for line in self.path.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                yield Shown.from_dict(d) if d["event"] == "shown" else Choice.from_dict(d)

    def shown(self) -> list[Shown]:
        return [e for e in self.events() if isinstance(e, Shown)]

    def choices(self) -> list[Choice]:
        return [e for e in self.events() if isinstance(e, Choice)]

    def latest_showing(self, brief_id: str, now: datetime.datetime, window_hours: float) -> Shown | None:
        cutoff = now - datetime.timedelta(hours=window_hours)
        hits = [s for s in self.shown()
                if s.position(brief_id) is not None and datetime.datetime.fromisoformat(s.at) >= cutoff]
        return hits[-1] if hits else None

    def record_choice(self, kind: ChoiceKind, brief_id: str, now: datetime.datetime | None = None,
                      window_hours: float = DEFAULT_WINDOW_HOURS) -> Choice:
        """Log a choice, linked to the latest list that showed the brief within the window, if any."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        shown = self.latest_showing(brief_id, now, window_hours)
        choice = Choice(kind, brief_id, shown.impression if shown else None,
                        shown.position(brief_id) if shown else None, now.isoformat(timespec="seconds"))
        self.append(choice)
        return choice


def note_choice(root: pathlib.Path, kind: ChoiceKind, brief_id: str) -> None:
    """The transition and bump hook: a failed log write warns and never fails the move itself."""
    try:
        PickLog(root).record_choice(kind, brief_id)
    except (OSError, ValueError, KeyError) as exc:
        print(f"warning: pick log not updated ({exc})", file=sys.stderr)
