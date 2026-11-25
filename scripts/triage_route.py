#!/usr/bin/env python3
"""
triage_route.py: ask the project router whether a just-promoted brief belongs to a project.

Her words (capture-stays-fast-hygiene-runs-async, 2026-11-17): "I agree with the project routing
going from inbox to ready and ideally also routing for inbox". Routing therefore runs here, in the
async triage at promotion, and never in the capture path. The router owns the scoring, the registry
and the move; this module only reads its `plan` and calls its `apply`, so there is one router.

A brief the router places in a direct-mode queue is routed. One bound for a queue that takes
briefs only by PR is left in the general queue with an alert, because opening that PR is not
triage's job; one the router cannot place is left there with an alert too. Triage never loses
work (2026-11-17): nothing is dropped or parked silently, and a router that is absent or fails
leaves every brief where it is.

Stdlib only.
"""

import dataclasses
import enum
import json
import os
import pathlib
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from _paths import warn
from triage_brief import InboxBrief

ROUTER_ENV = "CLAUDE_TASKS_ROUTER"
ROUTER_SCRIPT = pathlib.Path.home() / ".claude" / "tools" / "project-coordinator" / "router.py"
ROUTER_TIMEOUT_S = 300
DIRECT = "direct"


class Outcome(enum.StrEnum):
    ROUTE = "route"
    NEEDS_PR = "needs a PR"
    UNSURE = "unsure"


@dataclasses.dataclass(frozen=True)
class Decision:
    """What the router said about one promoted brief; `projects` is where it goes, or where it might."""

    brief_id: str
    title: str
    outcome: Outcome
    projects: tuple[str, ...]

    @property
    def named(self) -> str:
        return ", ".join(self.projects)


@dataclasses.dataclass(frozen=True)
class Placement:
    project: str
    queue_mode: str


@dataclasses.dataclass(frozen=True)
class RouterPlan:
    """The router's `plan` output, keyed by brief id: the briefs it would move and the ones it is unsure of."""

    moves: Mapping[str, Placement]
    ambiguous: Mapping[str, tuple[str, ...]]

    @classmethod
    def parse(cls, text: str) -> "RouterPlan":
        doc = json.loads(text)
        moves = {r["id"]: Placement(r["project"], r["queue_mode"]) for r in doc["moves"]}
        ambiguous = {r["id"]: tuple(r["scores"]) for r in doc["ambiguous"]}
        return cls(moves, ambiguous)

    def decide(self, brief: InboxBrief) -> Decision | None:
        """None when the router has no view: the brief is already routed, judged, or belongs nowhere."""
        move = self.moves.get(brief.id)
        if move:
            outcome = Outcome.ROUTE if move.queue_mode == DIRECT else Outcome.NEEDS_PR
            return Decision(brief.id, brief.title, outcome, (move.project,))
        if brief.id in self.ambiguous:
            return Decision(brief.id, brief.title, Outcome.UNSURE, self.ambiguous[brief.id])
        return None

    def decide_all(self, briefs: Iterable[InboxBrief]) -> list[Decision]:
        found = (self.decide(b) for b in briefs)
        return [d for d in found if d is not None]


class Router(Protocol):
    def plan(self) -> RouterPlan | None: ...

    def apply(self, project: str, ids: Sequence[str]) -> bool: ...


@dataclasses.dataclass(frozen=True)
class RouterCli:
    """The router's command line; every failure warns and returns None or False, never raises."""

    script: pathlib.Path

    @classmethod
    def configured(cls) -> "RouterCli | None":
        """The router $CLAUDE_TASKS_ROUTER names, else the installed one; None when neither is there."""
        named = os.environ.get(ROUTER_ENV)
        if named:
            return cls(pathlib.Path(named))
        return cls(ROUTER_SCRIPT) if ROUTER_SCRIPT.exists() else None

    def _run(self, *args: str) -> str | None:
        command = [sys.executable, str(self.script), *args]
        try:
            done = subprocess.run(
                command, capture_output=True, text=True, check=True, timeout=ROUTER_TIMEOUT_S
            )
        except (OSError, subprocess.SubprocessError) as err:
            _unrouted(f"router {args[0]} failed: {err}")
            return None
        return done.stdout

    def plan(self) -> RouterPlan | None:
        out = self._run("plan")
        if out is None:
            return None
        try:
            return RouterPlan.parse(out)
        except (ValueError, KeyError, TypeError) as err:
            _unrouted(f"router plan was not understood: {err!r}")
            return None

    def apply(self, project: str, ids: Sequence[str]) -> bool:
        return self._run("apply", project, *ids) is not None


def _unrouted(why: str) -> None:
    warn(f"{why}; promoted briefs stay in the general queue", prefix="triage")


def routes_by_project(decisions: Iterable[Decision]) -> dict[str, list[str]]:
    """The briefs to hand the router, grouped by the direct-mode project each belongs to."""
    grouped: dict[str, list[str]] = {}
    for d in decisions:
        if d.outcome is Outcome.ROUTE:
            grouped.setdefault(d.projects[0], []).append(d.brief_id)
    return grouped


def routes_plan(decisions: Sequence[Decision]) -> str:
    """The routing a dry run would do: one line per decision."""
    lines = [f"{len(decisions)} promoted briefs the router has a view on"]
    return "\n".join(lines + [f"  {d.brief_id}: {d.outcome} {d.named}" for d in decisions])
