"""Shared builders for the dependency, unblock, milestone and board tests."""
import pathlib

import init_queue


def brief_text(bid: str, *, status: str = "ready", blocked_by: str = "[]", milestone: str = "",
               tags: str = "[]", blockers: str = "[]", importance: str = "3",
               autonomy: str = "full", probed: bool = True) -> str:
    items = [i.strip() for i in blocked_by.strip("[]").split(",") if i.strip()]
    probes = "".join(f"blocked_probe: {n}: exit 10\n" for n, i in enumerate(items, 1)
                     if probed and i.startswith("external:"))
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
{probes}---

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


def inbox_text(bid: str, *, title: str = "", goal: str = "# TODO", context: str = "_none recorded_",
               criteria: tuple[str, ...] = (), tags: str = "[]", parent: str = "", importance: str = "3") -> str:
    """An inbox brief as add_task files it: stub fields unless given."""
    title = title or f"Title of {bid}"
    checks = "\n".join(f"- [ ] {c}" for c in criteria) or f"- [ ] {title} is done."
    return f"""---
id: {bid}
title: {title}
status: inbox
type: todo
importance: {importance}
autonomy: full
domain: work
tags: {tags}
parent: {parent}
source: claude
---

## Goal

{goal}

## Context

{context}

## Success criteria

{checks}

## Execution log

2026-11-01: captured.
"""


def make_inbox(tmp_path: pathlib.Path, briefs: dict[str, str]) -> pathlib.Path:
    """A queue whose inbox/ holds `briefs` (id -> full text)."""
    root = init_queue.init_queue(tmp_path / "proj" / ".tasks", name="demo")
    for bid, text in briefs.items():
        (root / "inbox" / f"{bid}.md").write_text(text)
    return root
