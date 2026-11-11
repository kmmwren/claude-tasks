#!/usr/bin/env python3
"""
milestones.py — optional milestone progress, read from a file the PROJECT owns.

Milestones belong to a project, not to claude-tasks; most queues (a personal queue, a team
queue) have none. So this is inert unless tasks.toml sets

    milestones = "docs/milestones.toml"    # relative to the directory holding the queue

and only then is a brief's `milestone:` field validated against that file and progress
grouped by it. The plugin's whole contract with the file is that the keys of its
`[milestone]` table are the milestone ids, in display order, each optionally with a
`title`. Every other field (dates, owners, exit criteria, status) is the project's own and
is ignored here.

  [milestone.M2]
  title = "Pipeline served end to end"

  python scripts/milestones.py progress [--json]
"""
import argparse
import dataclasses
import json
import pathlib
import tomllib

from _paths import require_queue, resolve_root
from config import load_config
from deps import Brief, load_queue


class MilestonesError(Exception):
    """tasks.toml names a milestones file that cannot be read as the contract describes."""


@dataclasses.dataclass(frozen=True)
class Milestone:
    id: str
    title: str
    counts: dict[str, int]

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


def milestone_titles(root: pathlib.Path) -> dict[str, str] | None:
    """Milestone id -> title in file order; None when unconfigured. Raises MilestonesError."""
    path = milestones_file(root)
    if path is None:
        return None
    try:
        table = tomllib.loads(path.read_text()).get("milestone", {})
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise MilestonesError(f"cannot read milestones file {path}: {e}") from e
    if not isinstance(table, dict) or not table:
        raise MilestonesError(f"{path} has no [milestone.<id>] tables")
    return {str(k): str(v.get("title", k)) if isinstance(v, dict) else str(k)
            for k, v in table.items()}


def progress(root: pathlib.Path, queue: list[Brief] | None = None) -> list[Milestone] | None:
    """Per-milestone counts by status; None when the queue does not use milestones."""
    titles = milestone_titles(root)
    if titles is None:
        return None
    queue = load_queue(root) if queue is None else queue
    out: list[Milestone] = []
    for mid, title in titles.items():
        counts: dict[str, int] = {}
        for b in queue:
            if b.milestone == mid:
                counts[b.status] = counts.get(b.status, 0) + 1
        out.append(Milestone(mid, title, counts))
    return out


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
        print(json.dumps([{"id": m.id, "title": m.title, "done": m.done, "total": m.total,
                           "counts": m.counts} for m in rows], indent=2))
        return
    for m in rows:
        print(f"{m.id:<8} {m.done}/{m.total}  {m.title}  {m.counts}")


if __name__ == "__main__":
    main()  # pragma: no cover
