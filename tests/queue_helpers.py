"""Shared builders for the dependency, unblock, milestone and board tests."""
import pathlib

import init_queue


def brief_text(bid: str, *, status: str = "ready", blocked_by: str = "[]", milestone: str = "",
               tags: str = "[]", blockers: str = "[]", importance: str = "3",
               autonomy: str = "full") -> str:
    return f"""---
id: {bid}
title: Title of {bid}
status: {status}
type: todo
importance: {importance}
autonomy: {autonomy}
estimated-effort: m
tags: {tags}
blockers: {blockers}
blocked_by: {blocked_by}
milestone: {milestone}
---

## Goal

g

## Execution log

2026-06-01: captured.
"""


def make_queue(tmp_path: pathlib.Path, briefs: dict[str, dict[str, str]],
               milestones: str | None = None) -> pathlib.Path:
    """briefs maps id -> brief_text kwargs (status selects the folder).

    `milestones`, when given, is the body of a project-owned milestones.toml that sits beside
    the queue directory, and tasks.toml is pointed at it.
    """
    project = tmp_path / "proj"
    root = init_queue.init_queue(project / ".tasks", name="demo")
    for bid, kwargs in briefs.items():
        status = kwargs.get("status", "ready")
        (root / status / f"{bid}.md").write_text(brief_text(bid, **kwargs))
    if milestones is not None:
        (project / "milestones.toml").write_text(milestones)
        cfg = root / "tasks.toml"
        cfg.write_text(cfg.read_text() + '\nmilestones = "milestones.toml"\n')
    return root


MILESTONES = """
[milestone.M1]
title = "First"
owner = "someone"      # project-owned field, ignored by the plugin

[milestone.M2]
title = "Second"
"""
