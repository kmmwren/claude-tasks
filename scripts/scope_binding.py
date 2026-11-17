"""The queue scope a Claude Code session is bound to, kept in its `sessions/<id>.toml` beside `root`.

A session works only the briefs its binding admits, so it never pulls unrelated work from the queue.
The binding is a selector (a `scope.py` scope) and a mode: `fixed` is a set of members chosen when it
was bound and grown only by an addition with a reason, `ongoing` takes whatever the selector admits
next, and `none` pulls nothing. A session with no binding is treated as `none` by its consumer.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import abc
import dataclasses
import datetime
import pathlib
import re
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import _toml
from _paths import binding_path, require_queue
from deps import load_queue
from prioritise import LIVE_DIRS, subject
from scope import IdsScope, Scope, parse_scope

TABLE = "scope_binding"
# An in-progress brief is already someone's work, so a fixed set chosen now leaves it out.
CLAIMABLE_STATUSES = tuple(d for d in LIVE_DIRS if d != "in-progress")

# A slug is joined into an `ids:` selector with commas, so it holds no comma or whitespace.
_SLUG = re.compile(r"[^\s,]+")


class BindingError(ValueError):
    """A binding that is malformed, or a change its mode does not allow."""


def _check_slug(slug: str) -> str:
    if not _SLUG.fullmatch(slug):
        raise BindingError(f"{slug!r} is not a brief slug (no commas or whitespace)")
    return slug


def _valid_slugs(ids: Iterable[str]) -> tuple[str, ...]:
    """The ids a binding can hold; one it cannot is left out with a warning, never written."""
    return tuple(slug for slug in ids if _is_slug(slug))


def _is_slug(slug: str) -> bool:
    valid = bool(_SLUG.fullmatch(slug))
    if not valid:
        print(f"scope_binding: leaving out {slug!r}, not a brief slug", file=sys.stderr)
    return valid


def _selector(text: str) -> str:
    """`text` when it is a scope selector; parse_scope exits on a bad one, which a binding refuses."""
    try:
        parse_scope(text)
    except (SystemExit, ValueError) as err:
        raise BindingError(str(err)) from None
    return text


@dataclasses.dataclass(frozen=True)
class Addition:
    """One brief added to a fixed set after it was chosen, with why and when."""

    slug: str
    reason: str
    at: str

    def __post_init__(self) -> None:
        _check_slug(self.slug)
        if not self.reason.strip():
            raise BindingError(f"adding {self.slug} needs a reason")

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> "Addition":
        try:
            return cls(str(table["slug"]), str(table["reason"]), str(table["at"]))
        except KeyError as missing:
            raise BindingError(f"addition is missing {missing}") from missing


@dataclasses.dataclass(frozen=True)
class Rebind:
    """A fixed binding replaced by a new one: what it admitted, and why it was replaced."""

    selector: str
    mode: str
    reason: str
    at: str
    members: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise BindingError(f"replacing the fixed set {self.selector} needs a reason")

    def to_table(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "members": list(self.members)}

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> "Rebind":
        try:
            selector, mode, reason, at = (
                str(table[k]) for k in ("selector", "mode", "reason", "at")
            )
            return cls(selector, mode, reason, at, _slugs(table.get("members", [])))
        except KeyError as missing:
            raise BindingError(f"rebind is missing {missing}") from missing


class Mode(abc.ABC):
    """How a binding turns its selector into what the session may pull."""

    name = ""

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other)

    def __hash__(self) -> int:
        return hash(type(self))

    @abc.abstractmethod
    def pull_scope(self, binding: "Binding") -> Scope | None: ...

    def members(self, selector: str, root: pathlib.Path) -> tuple[str, ...]:
        """The members to record when the binding is made; only a fixed set has any."""
        return ()

    def added(self, binding: "Binding", addition: Addition) -> "Binding":
        """`binding` grown by `addition`; only a fixed set grows."""
        raise BindingError(f"a {self.name} binding takes no additions; only a fixed one grows")

    def replaced(self, old: "Binding", new: "Binding", reason: str, at: str) -> "Binding":
        """`new` in `old`'s place, keeping the record of earlier replacements."""
        return dataclasses.replace(new, rebinds=old.rebinds)


class FixedMode(Mode):
    name = "fixed"

    def pull_scope(self, binding: "Binding") -> Scope:
        return IdsScope.of(binding.member_slugs)

    def members(self, selector: str, root: pathlib.Path) -> tuple[str, ...]:
        """The open briefs the selector admits now, so later briefs matching it are not pulled."""
        scope = parse_scope(selector)
        queue = require_queue(scope.root(root))
        open_briefs = (b for b in load_queue(queue) if b.status in CLAIMABLE_STATUSES)
        admitted = (b.id for b in open_briefs if scope.admits(subject({**b.fields, "id": b.id})))
        return _valid_slugs(admitted)

    def added(self, binding: "Binding", addition: Addition) -> "Binding":
        if addition.slug in binding.member_slugs:
            raise BindingError(f"{addition.slug} is already in the set")
        return dataclasses.replace(binding, additions=(*binding.additions, addition))

    def replaced(self, old: "Binding", new: "Binding", reason: str, at: str) -> "Binding":
        """A fixed set is replaced only with a reason, kept with what it admitted."""
        rebind = Rebind(old.selector, self.name, reason, at, old.member_slugs)
        return dataclasses.replace(new, rebinds=(*old.rebinds, rebind))


class OngoingMode(Mode):
    name = "ongoing"

    def pull_scope(self, binding: "Binding") -> Scope:
        return parse_scope(binding.selector)


class NoneMode(Mode):
    name = "none"

    def pull_scope(self, binding: "Binding") -> None:
        return None


MODES: dict[str, Mode] = {mode.name: mode for mode in (FixedMode(), OngoingMode(), NoneMode())}


def parse_mode(name: str) -> Mode:
    try:
        return MODES[name]
    except KeyError:
        raise BindingError(f"mode must be one of {', '.join(MODES)}, not {name!r}") from None


@dataclasses.dataclass(frozen=True)
class Binding:
    selector: str
    mode: Mode
    members: tuple[str, ...] = ()
    additions: tuple[Addition, ...] = ()
    rebinds: tuple[Rebind, ...] = ()

    @classmethod
    def bind(cls, selector: str, mode: Mode, root: pathlib.Path) -> "Binding":
        """A new binding; a fixed one records the members its selector admits in `root` today."""
        return cls(_selector(selector), mode, mode.members(selector, root))

    @property
    def member_slugs(self) -> tuple[str, ...]:
        return (*self.members, *(a.slug for a in self.additions))

    def pull_scope(self) -> Scope | None:
        """What the session may pull, or None when it may pull nothing."""
        return self.mode.pull_scope(self)

    def added(self, slug: str, reason: str, at: str) -> "Binding":
        """This binding grown by one brief; refused unless its mode is fixed and the slug is new."""
        return self.mode.added(self, Addition(slug, reason, at))

    def replaced_by(self, new: "Binding", reason: str, at: str) -> "Binding":
        """`new` in this binding's place; its mode decides whether that needs a reason."""
        return self.mode.replaced(self, new, reason, at)

    def to_table(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "mode": self.mode.name,
            "members": list(self.members),
            "additions": [dataclasses.asdict(a) for a in self.additions],
            "rebinds": [r.to_table() for r in self.rebinds],
        }

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> "Binding":
        try:
            return cls(
                _selector(_text(table["selector"])),
                parse_mode(_text(table["mode"])),
                _slugs(table.get("members", [])),
                tuple(Addition.from_table(a) for a in _list(table.get("additions", []))),
                tuple(Rebind.from_table(r) for r in _list(table.get("rebinds", []))),
            )
        except KeyError as missing:
            raise BindingError(f"[{TABLE}] is missing {missing}") from missing


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise BindingError(f"expected a string, got {value!r}")
    return value


def _list(value: Any) -> Sequence[Any]:
    if not isinstance(value, list):
        raise BindingError(f"expected a list, got {value!r}")
    return value


def _slugs(value: Any) -> tuple[str, ...]:
    return tuple(_check_slug(_text(v)) for v in _list(value))


def require_brief(selector: str, slug: str, root: pathlib.Path) -> None:
    """Refuse a slug that names no brief in the selector's queue, which would pull nothing."""
    queue = require_queue(parse_scope(selector).root(root))
    if slug not in {b.id for b in load_queue(queue)}:
        raise BindingError(f"{slug} is not a brief in {queue}")


def now() -> str:
    """The moment an addition is recorded, UTC and to the second."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def read(session_id: str, env: Mapping[str, str] | None = None) -> Binding | None:
    """The session's binding; None when it has no file or none in it, and a malformed one raises."""
    return _read_at(binding_path(session_id, env))


def _read_at(path: pathlib.Path) -> Binding | None:
    try:
        table = _toml.read(path).get(TABLE)
    except tomllib.TOMLDecodeError as err:
        raise BindingError(f"{path} is not valid TOML: {err}") from err
    if table is None:
        return None
    if not isinstance(table, Mapping):
        raise BindingError(f"[{TABLE}] must be a table")
    return Binding.from_table(table)


def write(session_id: str, binding: Binding, env: Mapping[str, str] | None = None) -> pathlib.Path:
    """Store the binding in the session's file, keeping `root` and every other key in it."""
    return change(session_id, lambda _existing: binding, env)[1]


def change(
    session_id: str,
    transform: Callable[[Binding | None], Binding],
    env: Mapping[str, str] | None = None,
) -> tuple[Binding, pathlib.Path]:
    """Read the session's binding, transform it and store the result under one lock, so no change is lost."""
    path = binding_path(session_id, env)
    with _toml.locked(path):
        binding = transform(_read_at(path))
        _toml.merge(path, {TABLE: binding.to_table()})
    return binding, path
