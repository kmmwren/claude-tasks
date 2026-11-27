"""Shared builders for the dependency, unblock, milestone and board tests."""

import contextlib
import pathlib
import time
from collections.abc import Iterator
from types import ModuleType

import init_queue
import pytest
from _concurrency import lock_path, repo_lock, try_lock
from _push_state import PushState


def brief_text(
    bid: str,
    *,
    status: str = "ready",
    blocked_by: str = "[]",
    milestone: str = "",
    tags: str = "[]",
    blockers: str = "[]",
    importance: str = "3",
    autonomy: str = "full",
    probed: bool = True,
    deps_declared: str = "",
    body: str = "## Goal\n\ng\n",
) -> str:
    declared = f"deps_declared: {deps_declared}\n" if deps_declared else ""
    items = [i.strip() for i in blocked_by.strip("[]").split(",") if i.strip()]
    probes = "".join(
        f"blocked_probe: {n}: exit 10\n"
        for n, i in enumerate(items, 1)
        if probed and i.startswith("external:")
    )
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
{declared}{probes}---

{body}
## Execution log

2026-06-01: captured.
"""


def make_queue(
    tmp_path: pathlib.Path,
    briefs: dict[str, dict[str, str]],
    milestones: str | None = None,
    project_name: str = "proj",
) -> pathlib.Path:
    """briefs maps id -> brief_text kwargs (status selects the folder).

    `milestones`, when given, is the body of a project-owned milestones.toml that sits beside
    the queue directory, and tasks.toml is pointed at it. `project_name` is the queue's short name.
    """
    project = tmp_path / project_name
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


def inbox_text(
    bid: str,
    *,
    title: str = "",
    goal: str = "# TODO",
    context: str = "_none recorded_",
    criteria: tuple[str, ...] = (),
    tags: str = "[]",
    parent: str = "",
    importance: str = "3",
    blocked_by: str | None = None,
    status: str = "inbox",
    extra: str = "",
    ticked: bool = False,
) -> str:
    """An inbox brief as add_task files it: stub fields unless given, no dependency line unless `blocked_by`.

    `extra` is frontmatter lines, each ending in a newline (`updated: 2026-10-01`), added verbatim.
    """
    title = title or f"Title of {bid}"
    declaration = "" if blocked_by is None else f"blocked_by: {blocked_by}\n"
    mark = "x" if ticked else " "
    checks = "\n".join(f"- [{mark}] {c}" for c in criteria) or f"- [{mark}] {title} is done."
    return f"""---
id: {bid}
title: {title}
status: {status}
type: todo
importance: {importance}
autonomy: full
domain: work
tags: {tags}
parent: {parent}
source: claude
{declaration}{extra}---

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


def wait_for_pusher_idle(root: pathlib.Path, timeout: float = 60.0, poll: float = 0.05) -> bool:
    """Block until no detached pusher runs and none is pending; False if `timeout` passed first."""
    state = PushState(root)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lock = None if state.is_dirty() else state.try_lock_pusher()
        if lock is not None:
            with lock:
                if not state.is_dirty():
                    return True
        time.sleep(poll)
    return False


@contextlib.contextmanager
def queue_lock_held_elsewhere(
    root: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *modules: ModuleType
) -> Iterator[None]:
    """The queue lock is held by another holder, and each module's `repo_lock` gives up after 50ms."""
    for module in modules:
        monkeypatch.setattr(module, "repo_lock", lambda r: repo_lock(r, timeout=0.05, poll=0.01))
    held = try_lock(lock_path(root))
    assert held is not None
    try:
        yield
    finally:
        held.release()


def installed_launcher(tmp_path: pathlib.Path) -> pathlib.Path:
    """An executable launcher script, so resolution treats it as installed."""
    script = tmp_path / "launcher.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    return script
