"""The scopes a ranking runs over: all | tag:<t> | project:<slug> | session:<id>.

prioritise.py filters and weights by scope, and rank.py passes its scope through, so both read
one parser. A scope's `name` is also the key of its `[priority.scope_weights]` table.
"""

from __future__ import annotations

import abc
import os
import pathlib
from typing import Protocol

from _paths import SESSION_ENV, session_root


class Scoped(Protocol):
    """What a scope reads from a brief to decide whether it belongs."""

    @property
    def tags(self) -> frozenset[str]: ...

    @property
    def project(self) -> str: ...


class Scope(abc.ABC):
    name = ""

    def __init__(self, value: str) -> None:
        self.value = value

    @property
    def key(self) -> str:
        return f"{self.name}:{self.value}" if self.value else self.name

    def root(self, default: pathlib.Path) -> pathlib.Path:
        return default

    @abc.abstractmethod
    def admits(self, brief: Scoped) -> bool: ...


class AllScope(Scope):
    name = "all"

    def admits(self, brief: Scoped) -> bool:
        return True


class TagScope(Scope):
    name = "tag"

    def admits(self, brief: Scoped) -> bool:
        return self.value in brief.tags


class ProjectScope(Scope):
    name = "project"

    def admits(self, brief: Scoped) -> bool:
        return brief.project == self.value or f"project:{self.value}" in brief.tags


class SessionScope(AllScope):
    """Everything in the queue a session is bound to (its `sessions/<id>.toml`)."""

    name = "session"

    def root(self, default: pathlib.Path) -> pathlib.Path:
        bound = session_root({**os.environ, SESSION_ENV: self.value})
        if bound is None:
            raise SystemExit(f"session {self.value} is not bound to a queue")
        return bound


SCOPES: dict[str, type[Scope]] = {
    cls.name: cls for cls in (AllScope, TagScope, ProjectScope, SessionScope)
}
SCOPE_HELP = "all | tag:<t> | project:<slug> | session:<id>"


def parse_scope(text: str) -> Scope:
    name, _, value = text.partition(":")
    if name not in SCOPES or (name != "all" and not value):
        raise SystemExit(
            f"--scope must be all, tag:<t>, project:<slug> or session:<id>, not {text!r}"
        )
    return SCOPES[name](value)
