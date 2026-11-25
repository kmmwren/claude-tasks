"""The scopes a ranking runs over: all | tag:<t>,... | project:<slug> | milestone:<id>,... | session:<id> | ids:<slug>,...

prioritise.py filters and weights by scope, and rank.py passes its scope through, so both read
one parser. A scope's `name` is also the key of its `[priority.scope_weights]` table.
"""

from __future__ import annotations

import abc
import os
import pathlib
from collections.abc import Iterable
from typing import Protocol, Self

from _paths import SESSION_ENV, session_root


class Scoped(Protocol):
    """What a scope reads from a brief to decide whether it belongs."""

    @property
    def id(self) -> str: ...

    @property
    def tags(self) -> frozenset[str]: ...

    @property
    def project(self) -> str: ...

    @property
    def milestone(self) -> str: ...


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


class ListScope(Scope):
    """A scope naming several values, any one of which admits a brief."""

    SEPARATOR = ","

    @classmethod
    def of(cls, values: Iterable[str]) -> Self:
        return cls(cls.SEPARATOR.join(values))

    @property
    def values(self) -> frozenset[str]:
        parts = (part.strip() for part in self.value.split(self.SEPARATOR))
        return frozenset(part for part in parts if part)


class TagScope(ListScope):
    name = "tag"

    def admits(self, brief: Scoped) -> bool:
        return bool(self.values & brief.tags)


class ProjectScope(Scope):
    name = "project"

    def admits(self, brief: Scoped) -> bool:
        return brief.project == self.value or f"project:{self.value}" in brief.tags


class MilestoneScope(ListScope):
    name = "milestone"

    def admits(self, brief: Scoped) -> bool:
        return brief.milestone in self.values


class IdsScope(ListScope):
    """Exactly the named briefs, so a session bound to a fixed set can pull nothing else."""

    name = "ids"

    @property
    def slugs(self) -> frozenset[str]:
        return self.values

    def admits(self, brief: Scoped) -> bool:
        return brief.id in self.slugs


class AllOfScope(Scope):
    """The briefs every part admits, weighted as the kind of slice it stands for."""

    def __init__(self, parts: Iterable[Scope], weighs_as: str = AllScope.name) -> None:
        self.parts = tuple(parts)
        self.name = weighs_as
        super().__init__("+".join(p.key for p in self.parts))

    @property
    def key(self) -> str:
        return self.value or self.name

    def admits(self, brief: Scoped) -> bool:
        return all(p.admits(brief) for p in self.parts)


class SessionScope(AllScope):
    """Everything in the queue a session is bound to (its `sessions/<id>.toml`)."""

    name = "session"

    def root(self, default: pathlib.Path) -> pathlib.Path:
        bound = session_root({**os.environ, SESSION_ENV: self.value})
        if bound is None:
            raise SystemExit(f"session {self.value} is not bound to a queue")
        return bound


SCOPES: dict[str, type[Scope]] = {
    cls.name: cls
    for cls in (AllScope, TagScope, ProjectScope, MilestoneScope, SessionScope, IdsScope)
}
SCOPE_HELP = (
    "all | tag:<t>,... | project:<slug> | milestone:<id>,... | session:<id> | ids:<slug>,..."
)


def parse_scope(text: str) -> Scope:
    name, _, value = text.partition(":")
    if name not in SCOPES or (name != "all" and not value):
        raise SystemExit(f"--scope must be one of {SCOPE_HELP}, not {text!r}")
    return SCOPES[name](value)
