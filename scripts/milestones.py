#!/usr/bin/env python3
"""
milestones.py — optional milestone progress, read from a file the PROJECT owns.

Milestones belong to a project, not to claude-tasks; most queues (a personal queue, a team
queue) have none. So this is inert unless tasks.toml sets

    milestones = "docs/milestones.toml"    # relative to the directory holding the queue

and only then is a brief's `milestone:` field validated against that file and progress
grouped by it. The plugin's whole contract with the file is that the keys of its
`[milestone]` table are the milestone ids, in display order, each optionally with a
`title` and a `release` (the release it ships in). Every other field (dates, owners, exit
criteria, status) is the project's own and is ignored here. Which briefs belong to a milestone
is stored once, in each brief's `milestone:` frontmatter; a `briefs = [...]` list is ignored.

  [milestone.M2]
  title = "Pipeline served end to end"
  release = "2026.12"

  python scripts/milestones.py progress [--json]
"""

import argparse
import dataclasses
import json
import pathlib
import tomllib
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from _paths import require_queue, resolve_root
from config import load_config
from deps import Brief, load_queue


class MilestonesError(Exception):
    """tasks.toml names a milestones file that cannot be read as the contract describes."""


T = TypeVar("T")


@dataclasses.dataclass(frozen=True)
class MilestoneSpec:
    """A milestone as the project file declares it; `release` is None when not set."""

    id: str
    title: str
    release: str | None = None

    @classmethod
    def parse(cls, key: str, table: Any) -> "MilestoneSpec":
        raw = table if isinstance(table, dict) else {}
        release = str(raw.get("release") or "").strip()
        return cls(key, str(raw.get("title", key)), release or None)


@dataclasses.dataclass(frozen=True)
class Milestone:
    id: str
    title: str
    counts: dict[str, int]
    release: str | None = None

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def done(self) -> int:
        return self.counts.get("done", 0)


def milestones_file(root: pathlib.Path) -> pathlib.Path | None:
    """The configured milestones file, or None when the queue does not use milestones."""
    configured = load_config(root).get("milestones")
    if not configured:
        return None
    path = pathlib.Path(str(configured)).expanduser()
    return path if path.is_absolute() else root.resolve().parent / path


def members(briefs: Iterable[T], milestone_id: str, milestone_of: Callable[[T], str]) -> list[T]:
    """The briefs whose frontmatter names the milestone: the one place membership is stored."""
    return [b for b in briefs if milestone_of(b) == milestone_id]


def milestone_specs(root: pathlib.Path) -> list[MilestoneSpec] | None:
    """Milestones in file order; None when unconfigured. Raises MilestonesError."""
    path = milestones_file(root)
    if path is None:
        return None
    try:
        table = tomllib.loads(path.read_text()).get("milestone", {})
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise MilestonesError(f"cannot read milestones file {path}: {e}") from e
    if not isinstance(table, dict) or not table:
        raise MilestonesError(f"{path} has no [milestone.<id>] tables")
    return [MilestoneSpec.parse(str(k), v) for k, v in table.items()]


def milestone_titles(root: pathlib.Path) -> dict[str, str] | None:
    """Milestone id -> title in file order; None when unconfigured. Raises MilestonesError."""
    specs = milestone_specs(root)
    return None if specs is None else {m.id: m.title for m in specs}


def _of_brief(brief: Brief) -> str:
    return brief.milestone


def _counts(briefs: Iterable[Brief]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for b in briefs:
        counts[b.status] = counts.get(b.status, 0) + 1
    return counts


def progress(root: pathlib.Path, queue: list[Brief] | None = None) -> list[Milestone] | None:
    """Per-milestone counts by status; None when the queue does not use milestones."""
    specs = milestone_specs(root)
    if specs is None:
        return None
    queue = load_queue(root) if queue is None else queue
    return [
        Milestone(m.id, m.title, _counts(members(queue, m.id, _of_brief)), m.release) for m in specs
    ]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Milestone progress for a queue that uses them.")
    ap.add_argument("command", choices=["progress"])
    ap.add_argument("--root", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = require_queue(pathlib.Path(a.root) if a.root else resolve_root())
    try:
        rows = progress(root)
    except MilestonesError as e:
        raise SystemExit(f"error: {e}") from e
    if rows is None:
        print("this queue does not use milestones (no `milestones` in tasks.toml)")
        return
    if a.json:
        print(
            json.dumps(
                [
                    {
                        "id": m.id,
                        "title": m.title,
                        "release": m.release,
                        "done": m.done,
                        "total": m.total,
                        "counts": m.counts,
                    }
                    for m in rows
                ],
                indent=2,
            )
        )
        return
    for m in rows:
        ships = f"  ships in {m.release}" if m.release else ""
        print(f"{m.id:<8} {m.done}/{m.total}  {m.title}{ships}  {m.counts}")


if __name__ == "__main__":
    main()  # pragma: no cover
