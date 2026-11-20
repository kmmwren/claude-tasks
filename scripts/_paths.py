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

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import dataclasses
import os
import pathlib
import re
import sys
import threading
import tomllib
from collections.abc import Iterable, Iterator, Mapping

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
COMMENT_START = re.compile(r"(?:^|\s)#")
_COMMENT = re.compile(COMMENT_START.pattern + ".*")
# As in YAML, a quoted value runs to its closing quote, so a spaced `#` inside it is text.
_QUOTED = re.compile(r"""\s*(["'])(.*?)\1(?=\s|$)""")


def warn(message: str, prefix: str = "warning") -> None:
    """One line on stderr saying something was ignored; the run carries on."""
    print(f"{prefix}: {message}", file=sys.stderr)


def is_brief(path: str | os.PathLike[str]) -> bool:
    """True if `path` names a task brief: a `.md` file directly inside a state directory."""
    pure = pathlib.PurePath(path)
    return pure.suffix == ".md" and pure.parent.name in TASK_DIRS


_SAID: set[str] = set()
_SAID_LOCK = threading.Lock()


def warn_once(message: str, prefix: str = "warning") -> None:
    """`warn`, unless this message was already said in this process."""
    with _SAID_LOCK:
        repeat = message in _SAID
        _SAID.add(message)
    if not repeat:
        warn(message, prefix)


def forget_warnings() -> None:
    """Let every warning be said again, for a fresh run or test."""
    with _SAID_LOCK:
        _SAID.clear()


def warn_undecodable(shown_as: str, consequence: str) -> None:
    """Name a file that is not UTF-8 once, as `shown_as`, and what was done without it."""
    warn_once(f"{shown_as}: is not valid UTF-8; {consequence}")


@dataclasses.dataclass(frozen=True)
class BriefFile:
    path: pathlib.Path
    text: str


SKIPPED = "that brief is skipped"


def read_briefs(folder: pathlib.Path, shown_as: str | None = None) -> list[BriefFile]:
    """The briefs in `folder` by name, each read once; one that is not UTF-8 is left out and named once."""
    return list(read_folder(folder, shown_as).briefs)


def read_brief(
    path: pathlib.Path, shown_as: str | None = None, consequence: str = SKIPPED
) -> BriefFile | None:
    """The brief at `path`, or None, named once with `consequence`, when it is not UTF-8."""
    try:
        return BriefFile(path, path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        warn_undecodable(shown_as or str(path), consequence)
        return None


@dataclasses.dataclass(frozen=True)
class BriefFolder:
    """A state folder's briefs, and the files in it left out because they are not UTF-8."""

    briefs: tuple[BriefFile, ...]
    undecodable: tuple[pathlib.Path, ...]


def read_folder(
    folder: pathlib.Path, shown_as: str | None = None, consequence: str = SKIPPED
) -> BriefFolder:
    """Every brief in `folder` by name, read once, split into the decoded and the undecodable."""
    read = [
        (path, read_brief(path, f"{shown_as or folder}/{path.name}", consequence))
        for path in sorted(folder.glob("*.md"))
    ]
    return BriefFolder(
        tuple(brief for _, brief in read if brief is not None),
        tuple(path for path, brief in read if brief is None),
    )


@dataclasses.dataclass(frozen=True)
class QueueBriefs:
    """A queue's briefs, each state folder read on first request and then shared by every reader.

    Each roll-up or command builds its own; the lock keeps a folder read once even if a caller's
    threads do share one. `consequence` is what the warning for an undecodable brief says happens.
    """

    root: pathlib.Path
    label: str
    consequence: str = SKIPPED
    _read: dict[str, BriefFolder] = dataclasses.field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _lock: threading.Lock = dataclasses.field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    @classmethod
    def read(
        cls, root: pathlib.Path, shown_as: str | None = None, consequence: str = SKIPPED
    ) -> "QueueBriefs":
        return cls(root, shown_as or str(root), consequence)

    def folder(self, name: str) -> tuple[BriefFile, ...]:
        """The briefs in state folder `name`, read the first time they are asked for."""
        return self._folder(name).briefs

    def within(self, folders: Iterable[str]) -> Iterator[tuple[str, BriefFile]]:
        """Each brief in `folders`, folder by folder, with the folder it sits in."""
        return ((d, brief) for d in folders for brief in self.folder(d))

    def undecodable(self, folders: Iterable[str]) -> list[pathlib.Path]:
        """The files in `folders` left out because they are not UTF-8."""
        return [path for d in folders for path in self._folder(d).undecodable]

    def _folder(self, name: str) -> BriefFolder:
        with self._lock:
            if name not in self._read:
                self._read[name] = read_folder(
                    self.root / name, f"{self.label}/{name}", self.consequence
                )
            return self._read[name]


def read_stdin() -> str:
    """Standard input decoded as UTF-8, whatever encoding the locale gives `sys.stdin`."""
    return sys.stdin.buffer.read().decode("utf-8")


def strip_comment(value: str) -> str:
    """Drop a frontmatter value's inline `#` comment, and a quoted value's quotes. The shared cleaner."""
    quoted = _QUOTED.match(value)
    return quoted.group(2) if quoted else _COMMENT.sub("", value).strip()


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
    cwd = pathlib.Path.cwd() if cwd is None else pathlib.Path(cwd)
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
    return (
        pathlib.Path(env.get(SESSIONS_ENV) or DEFAULT_SESSIONS).expanduser() / f"{session_id}.toml"
    )


def session_root(env: Mapping[str, str]) -> pathlib.Path | None:
    """The queue bound to the session in `env`, or None so resolution falls through."""
    session_id = env.get(SESSION_ENV)
    if not session_id or not _SESSION_ID.fullmatch(session_id):
        return None
    try:
        root = tomllib.loads(binding_path(session_id, env).read_text(encoding="utf-8")).get("root")
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
