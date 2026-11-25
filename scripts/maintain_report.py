#!/usr/bin/env python3
"""
maintain_report.py: the lines a tick prints, one per brief or hygiene phase that needs a person.

Stdlib only.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import dataclasses
import enum


class ReportKind(enum.Enum):
    OVERDUE = "overdue"
    OVERDUE_NEEDS = "overdue-needs"
    EXHAUSTED = "exhausted"
    CLAIMED = "claimed"
    NOT_CLAIMED = "not-claimed"
    SPAWN_FAILED = "spawn-failed"
    HYGIENE_FAILED = "hygiene-failed"
    HYGIENE_LAPSED = "hygiene-lapsed"
    HYGIENE_REFUSED = "hygiene-refused"


@dataclasses.dataclass(frozen=True)
class Report:
    """One line about one brief (or, for hygiene kinds, one phase); the capability makes `overdue-needs` into `overdue-needs-<cap>`."""

    kind: ReportKind
    brief_id: str
    detail: str = ""
    capability: str = ""

    def __str__(self) -> str:
        label = f"{self.kind.value}-{self.capability}" if self.capability else self.kind.value
        return f"{label} {self.brief_id}" + (f": {self.detail}" if self.detail else "")
