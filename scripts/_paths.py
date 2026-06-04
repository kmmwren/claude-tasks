#!/usr/bin/env python3
"""
_paths.py — resolve which queue directory the scripts operate on.

The scripts ship inside the plugin, but the queue is the user's own data living
somewhere else entirely. So "where is the queue?" can't be `__file__.parent.parent`
any more (that points at the plugin). It is resolved, in order:

  1. $CLAUDE_TASKS_DIR — an explicit override (a global personal queue).
  2. The nearest ancestor `.tasks/` directory, found by walking up from the cwd —
     the same trick git uses for `.git`, giving project-scoped queues.
  3. ~/tasks — the default when nothing else is set.

A resolved directory only becomes a *usable* queue once it has a tasks.toml (written
by /tasks-init). require_queue() refuses to operate on an uninitialised directory so a
stray run can never silently scatter folders into the wrong place.
"""
import os
import pathlib
from collections.abc import Mapping

import _compat  # noqa: F401  - version guard; must run before the annotations below evaluate

TASK_DIRS = ("inbox", "ready", "in-progress", "done", "parked")
ENV_VAR = "CLAUDE_TASKS_DIR"
PROJECT_MARKER = ".tasks"
CONFIG_FILE = "tasks.toml"
DEFAULT_ROOT = "~/tasks"


def resolve_root(
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> pathlib.Path:
    """Return the queue root per the documented precedence. Pure given its inputs."""
    env = os.environ if env is None else env
    override = env.get(ENV_VAR)
    if override:
        return pathlib.Path(override).expanduser()
    cwd = pathlib.Path.cwd() if cwd is None else pathlib.Path(cwd)
    for parent in (cwd, *cwd.parents):
        candidate = parent / PROJECT_MARKER
        if candidate.is_dir():
            return candidate
    return pathlib.Path(DEFAULT_ROOT).expanduser()


def is_queue(root: str | os.PathLike[str]) -> bool:
    """True if `root` is an initialised queue (has a tasks.toml)."""
    return (pathlib.Path(root) / CONFIG_FILE).is_file()


def require_queue(root: str | os.PathLike[str]) -> pathlib.Path:
    """Return `root` if it is a queue, else exit with a message pointing at /tasks-init."""
    root = pathlib.Path(root)
    if not is_queue(root):
        raise SystemExit(
            f"No task queue at {root}.\n"
            f"Run /tasks-init (or: python scripts/init_queue.py {root}) to create one."
        )
    return root
