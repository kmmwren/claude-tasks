"""Typed work signals: what kind of leverage a brief carries, matched from `[priority.signals]`.

A signal is matched by any of its matchers (tags, types, fields, min_leverage); tasks.toml merges
over the defaults per matcher, so `[]` or `min_leverage = 0` drops one. Each scope kind
weights the signals differently through `[priority.scope_weights]`, so a project ranks shipping and
unblocking above multipliers while the whole queue ranks multipliers first.
"""

from __future__ import annotations

import abc
import dataclasses
import enum
from collections.abc import Callable, Mapping
from typing import Any

EMPTY_VALUES = ("", "[]", "false", "none")


class Signal(enum.Enum):
    MULTIPLIER = "multiplier"
    SHIPS_PR = "ships-pr"
    CLEARS_BLOCKER = "clears-blocker"


@dataclasses.dataclass(frozen=True)
class Subject:
    """The parts of a brief a matcher reads."""

    fields: Mapping[str, Any]
    tags: frozenset[str]
    leverage: int

    @property
    def project(self) -> str:
        return str(self.fields.get("project", "") or "").strip()

    def has_field(self, name: str) -> bool:
        return str(self.fields.get(name, "") or "").strip().lower() not in EMPTY_VALUES


class Matcher(abc.ABC):
    @abc.abstractmethod
    def matches(self, subject: Subject) -> bool: ...


@dataclasses.dataclass(frozen=True)
class TagMatcher(Matcher):
    tags: frozenset[str]

    def matches(self, subject: Subject) -> bool:
        return bool(self.tags & subject.tags)


@dataclasses.dataclass(frozen=True)
class TypeMatcher(Matcher):
    types: frozenset[str]

    def matches(self, subject: Subject) -> bool:
        return str(subject.fields.get("type", "") or "").strip() in self.types


@dataclasses.dataclass(frozen=True)
class FieldMatcher(Matcher):
    names: frozenset[str]

    def matches(self, subject: Subject) -> bool:
        return any(subject.has_field(n) for n in self.names)


@dataclasses.dataclass(frozen=True)
class LeverageMatcher(Matcher):
    """`min_leverage = 0` switches it off, since every brief has leverage of at least zero."""

    minimum: int

    def matches(self, subject: Subject) -> bool:
        return self.minimum > 0 and subject.leverage >= self.minimum


def _strings(raw: Any) -> frozenset[str]:
    if not isinstance(raw, list):
        raise TypeError(f"expected a list of strings, got {raw!r}")
    return frozenset(str(v) for v in raw)


MATCHERS: dict[str, Callable[[Any], Matcher]] = {
    "tags": lambda raw: TagMatcher(_strings(raw)),
    "types": lambda raw: TypeMatcher(_strings(raw)),
    "fields": lambda raw: FieldMatcher(_strings(raw)),
    "min_leverage": lambda raw: LeverageMatcher(int(raw)),
}


@dataclasses.dataclass(frozen=True)
class SignalRule:
    signal: Signal
    matchers: tuple[Matcher, ...]

    @classmethod
    def from_table(cls, name: str, table: Mapping[str, Any]) -> SignalRule:
        unknown = set(table) - set(MATCHERS)
        if unknown:
            raise ValueError(f"signal {name}: unknown matcher(s) {sorted(unknown)}")
        return cls(Signal(name), tuple(MATCHERS[k](v) for k, v in sorted(table.items())))

    def matches(self, subject: Subject) -> bool:
        return any(m.matches(subject) for m in self.matchers)


@dataclasses.dataclass(frozen=True)
class SignalWeights:
    """The rules plus each scope kind's weight per signal."""

    rules: tuple[SignalRule, ...]
    by_scope: Mapping[str, Mapping[Signal, float]]

    @classmethod
    def from_tables(
        cls, signals: Mapping[str, Any], scope_weights: Mapping[str, Any]
    ) -> SignalWeights:
        return cls(
            tuple(SignalRule.from_table(n, t) for n, t in sorted(signals.items())),
            {
                kind: {Signal(s): float(w) for s, w in table.items()}
                for kind, table in scope_weights.items()
            },
        )

    def values(self, subject: Subject, scope_kind: str) -> dict[str, float]:
        """Each signal the brief carries and its weight under this scope kind, by signal name."""
        weights = self.by_scope.get(scope_kind, {})
        return {
            r.signal.value: weights.get(r.signal, 0.0) for r in self.rules if r.matches(subject)
        }
