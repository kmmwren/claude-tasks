#!/usr/bin/env python3
"""The classification log: what triage called each inbox brief and where the call was later overruled.

Append-only JSONL beside the pick log, outside the queue repo. A `verdict` row is written when a
brief's class changes; a `correction` row when the brief is later found somewhere the verdict says
it should not be. Corrections carry only the verdict they overrule, so the title and reason come
from that verdict and nothing is stored twice.
"""

import dataclasses
import datetime
import enum
import fcntl
import json
import pathlib
from collections.abc import Iterator
from typing import Any

from picks import state_dir

__all__ = ["BriefClass", "ClassLog", "Correction", "Example", "Verdict", "state_dir"]

LOG_FILE = "classes.jsonl"


class BriefClass(enum.StrEnum):
    READY = "ready"
    NEEDS_INFO = "needs-info"
    DUPLICATE = "duplicate"
    DROP = "drop"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@dataclasses.dataclass(frozen=True)
class Verdict:
    brief_id: str
    cls: BriefClass
    reason: str
    title: str
    at: str = dataclasses.field(default_factory=_now)
    event: str = "verdict"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Verdict":
        return cls(d["brief_id"], BriefClass(d["cls"]), d["reason"], d["title"], d["at"])


@dataclasses.dataclass(frozen=True)
class Correction:
    brief_id: str
    verdict_at: str
    predicted: BriefClass
    actual: BriefClass
    moved_to: str
    at: str = dataclasses.field(default_factory=_now)
    event: str = "correction"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Correction":
        return cls(
            d["brief_id"],
            d["verdict_at"],
            BriefClass(d["predicted"]),
            BriefClass(d["actual"]),
            d["moved_to"],
            d["at"],
        )


@dataclasses.dataclass(frozen=True)
class Example:
    """One overruled call, as the next classification run is shown it."""

    title: str
    predicted: BriefClass
    reason: str
    actual: BriefClass


class ClassLog:
    def __init__(self, root: pathlib.Path) -> None:
        self.path = state_dir(root) / LOG_FILE

    def append(self, event: Verdict | Correction) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps(dataclasses.asdict(event), sort_keys=True) + "\n")

    def events(self) -> Iterator[Verdict | Correction]:
        if not self.path.is_file():
            return
        for line in self.path.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                yield Verdict.from_dict(d) if d["event"] == "verdict" else Correction.from_dict(d)

    def verdicts(self) -> list[Verdict]:
        return [e for e in self.events() if isinstance(e, Verdict)]

    def corrections(self) -> list[Correction]:
        return [e for e in self.events() if isinstance(e, Correction)]

    def latest(self) -> dict[str, Verdict]:
        return {v.brief_id: v for v in self.verdicts()}

    def record_verdict(self, verdict: Verdict) -> bool:
        """Log the verdict unless it repeats the brief's current class; history is change-only."""
        current = self.latest().get(verdict.brief_id)
        if current is not None and current.cls is verdict.cls:
            return False
        self.append(verdict)
        return True

    def record_correction(self, correction: Correction) -> bool:
        """Log the correction unless that verdict was already overruled."""
        if (correction.brief_id, correction.verdict_at) in {
            (c.brief_id, c.verdict_at) for c in self.corrections()
        }:
            return False
        self.append(correction)
        return True

    def uncorrected(self) -> list[Verdict]:
        """Each brief's current verdict, unless it has been overruled already."""
        done = {(c.brief_id, c.verdict_at) for c in self.corrections()}
        return [v for v in self.latest().values() if (v.brief_id, v.at) not in done]

    def sent_back(self) -> set[str]:
        """Briefs whose promotion the owner has undone."""
        return {c.brief_id for c in self.corrections() if c.predicted is BriefClass.READY}

    def examples(self, limit: int) -> list[Example]:
        """The newest `limit` overruled calls, oldest first, joined to the verdicts they overrule."""
        by_key = {(v.brief_id, v.at): v for v in self.verdicts()}
        found = [
            (c, by_key[(c.brief_id, c.verdict_at)])
            for c in self.corrections()
            if (c.brief_id, c.verdict_at) in by_key
        ]
        return [Example(v.title, c.predicted, v.reason, c.actual) for c, v in found[-limit:]]
