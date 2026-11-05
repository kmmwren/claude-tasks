#!/usr/bin/env python3
"""
_paths.py — resolve which queue directory the scripts operate on.

The scripts ship inside the plugin, but the queue is the user's own data living
somewhere else entirely. So "where is the queue?" can't be `__file__.parent.parent`
any more (that points at the plugin). It is resolved, in order:

  1. $CLAUDE_TASKS_DIR — an explicit override (a global personal queue).
  2. The queue bound to this Claude Code session ($CLAUDE_CODE_SESSION_ID), registered
     as <id>.toml in $CLAUDE_TASKS_SESSIONS (default ~/.claude-tasks/sessions), so a
     long-running session files into its own queue until fold_session.py folds it back.
  3. The nearest ancestor `.tasks/` directory, found by walking up from the cwd —
     the same trick git uses for `.git`, giving project-scoped queues.
  4. ~/tasks — the default when nothing else is set.

A resolved directory only becomes a *usable* queue once it has a tasks.toml (written
by /tasks-init). require_queue() refuses to operate on an uninitialised directory so a
stray run can never silently scatter folders into the wrong place.
"""
import os
import pathlib
import re
import tomllib
from collections.abc import Mapping

TASK_DIRS = ("inbox", "ready", "in-progress", "blocked", "done", "parked")
ENV_VAR = "CLAUDE_TASKS_DIR"
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
SESSIONS_ENV = "CLAUDE_TASKS_SESSIONS"
PROJECT_MARKER = ".tasks"
CONFIG_FILE = "tasks.toml"
DEFAULT_ROOT = "~/tasks"
DEFAULT_SESSIONS = "~/.claude-tasks/sessions"

# A session id becomes a filename, so anything beyond this set (a `/`, `..`) is refused.
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")

# A frontmatter comment starts at a `#` that opens the value or is spaced off from it, so a
# `#` inside a value (issue#42, [a#b]) is never mistaken for one.
_COMMENT = re.compile(r"(?:^|\s)#.*")


def strip_comment(value: str) -> str:
    """Drop a frontmatter value's inline `#` comment. The shared cleaner for every parser."""
    return _COMMENT.sub("", value).strip()


def resolve_root(
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> pathlib.Path:
    """Return the queue root per the documented precedence. Pure given its inputs."""
    env = os.environ if env is None else env
    override = env.get(ENV_VAR)
    if override:
        return pathlib.Path(override).expanduser()
    bound = session_root(env)
    if bound is not None:
        return bound
    cwd = None
    for parent in (cwd, *cwd.parents):
        candidate = parent / PROJECT_MARKER
        if candidate.is_dir():
            return candidate
    return pathlib.Path(DEFAULT_ROOT).expanduser()


def binding_path(session_id: str, env: Mapping[str, str] | None = None) -> pathlib.Path:
    """Where the binding of `session_id` to its queue lives."""
    env = os.environ if env is None else env
    if not _SESSION_ID.fullmatch(session_id):
        raise ValueError(f"invalid session id {session_id!r}")
    return pathlib.Path(env.get(SESSIONS_ENV) or DEFAULT_SESSIONS).expanduser() / f"{session_id}.toml"


def session_root(env: Mapping[str, str]) -> pathlib.Path | None:
    """The queue bound to the session in `env`, or None so resolution falls through."""
    session_id = env.get(SESSION_ENV)
    if not session_id or not _SESSION_ID.fullmatch(session_id):
        return None
    try:
        root = tomllib.loads(binding_path(session_id, env).read_text()).get("root")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(root, str) or not root:
        return None
    path = pathlib.Path(root).expanduser()
    return path if is_queue(path) else None


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
