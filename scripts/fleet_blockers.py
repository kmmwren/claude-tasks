#!/usr/bin/env python3
"""
fleet_blockers.py — which fleet-wide blockers are open, read from records a queue does not own.

A fleet blocker stops every session, so work that clears one outranks everything else. The
records live in a directory tasks.toml names (`fleet_blockers_dir`), one `<id>.json` each:

    {"id", "summary", "raised_at", "raised_by", "owner", "probe", "evidence",
     "status": "active|cleared|unverified", "checked_at"}

A brief declares `clears: <id>` (or a list of ids). While the record is active or unverified,
prioritise.py puts the brief in the fleet-blocker lane. An unset directory turns the feature off,
and an unreadable directory or record warns once and is skipped, so ranking never fails on it.
"""
import dataclasses
import enum
import json
import pathlib
import sys
from collections.abc import Mapping
from typing import Any

from config import load_config
from deps import parse_list

CONFIG_KEY = "fleet_blockers_dir"


class BlockerStatus(enum.Enum):
    ACTIVE = "active"
    CLEARED = "cleared"
    UNVERIFIED = "unverified"

    @property
    def is_open(self) -> bool:
        return self is not BlockerStatus.CLEARED


@dataclasses.dataclass(frozen=True)
class FleetBlocker:
    id: str
    summary: str
    status: BlockerStatus

    @classmethod
    def parse(cls, raw: object, stem: str) -> "FleetBlocker":
        """Raises ValueError for anything that is not a record with a known status."""
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
        return cls(str(raw.get("id") or stem), str(raw.get("summary") or ""),
                   BlockerStatus(raw.get("status")))

    def label(self) -> str:
        return f"{self.id}: {self.summary}" if self.summary else self.id


@dataclasses.dataclass(frozen=True)
class FleetBlockers:
    """The open blockers by id; empty when the feature is off or its records are unreadable."""
    open: dict[str, FleetBlocker]

    @classmethod
    def for_queue(cls, root: pathlib.Path) -> "FleetBlockers":
        configured = str(load_config(root).get(CONFIG_KEY) or "")
        if not configured:
            return cls({})
        path = pathlib.Path(configured).expanduser()
        return cls.read(path if path.is_absolute() else root.resolve().parent / path)

    @classmethod
    def read(cls, directory: pathlib.Path) -> "FleetBlockers":
        if not directory.is_dir():
            cls._warn(f"{CONFIG_KEY} {directory} is not a readable directory; using no fleet blockers")
            return cls({})
        parsed, skipped = [], []
        for f in sorted(directory.glob("*.json")):
            try:
                parsed.append(FleetBlocker.parse(json.loads(f.read_text()), f.stem))
            except (OSError, ValueError) as err:
                skipped.append(f"{f.name} ({err})")
        if skipped:
            cls._warn(f"skipping unreadable fleet-blockers records: {', '.join(skipped)}")
        return cls({b.id: b for b in parsed if b.status.is_open})

    @staticmethod
    def _warn(message: str) -> None:
        print(f"prioritise: {message}", file=sys.stderr)

    def clearing(self, brief: Mapping[str, Any]) -> FleetBlocker | None:
        """The first open blocker the brief's `clears:` names, if any."""
        return next((self.open[i] for i in parse_list(brief.get("clears", "")) if i in self.open), None)
