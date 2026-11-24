#!/usr/bin/env python3
"""
maintain_state.py: what `maintain` records in the queue repo, so any trigger can read it.

  maintain/last-run/<tier>.json   when that tier last completed a tick, what it claimed and reported
  maintain/runs/<digest>.json     one file per wake already run; written in the claim commit, so a
                                  wake is recorded exactly when its claim is published

One file per tier and per run key means two trackers never write the same path, so their commits
never conflict.

Stdlib only.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import dataclasses
import datetime as dt
import json
import pathlib

from _concurrency import atomic_write
from _paths import warn
from wake import RunKey

STATE_DIR = "maintain"
LAST_RUN_DIR = f"{STATE_DIR}/last-run"
RUNS_DIR = f"{STATE_DIR}/runs"


@dataclasses.dataclass(frozen=True)
class LastRun:
    """The last tick a tier finished: when, which briefs it claimed and what it reported."""

    tier: str
    at: dt.datetime
    claimed: tuple[str, ...] = ()
    reports: tuple[str, ...] = ()

    @staticmethod
    def path(root: pathlib.Path, tier: str) -> pathlib.Path:
        return root / LAST_RUN_DIR / f"{tier}.json"

    @classmethod
    def read(cls, root: pathlib.Path, tier: str) -> "LastRun | None":
        """The tier's record, or None when there is none or it cannot be read (a stale tick is only a retry)."""
        path = cls.path(root, tier)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                tier,
                dt.datetime.fromisoformat(raw["at"]),
                tuple(raw.get("claimed", ())),
                tuple(raw.get("reports", ())),
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as e:
            warn(f"ignoring unreadable {path} ({e})", "maintain")
            return None

    def write(self, root: pathlib.Path) -> None:
        body = {"at": self.at.isoformat(), "claimed": self.claimed, "reports": self.reports}
        atomic_write(self.path(root, self.tier), json.dumps(body, indent=2) + "\n")

    def fresh(self, now: dt.datetime, interval: dt.timedelta) -> bool:
        """True while the record is younger than `interval`; a clock that ran backwards is stale, not fresh."""
        return dt.timedelta(0) <= now - self.at < interval


@dataclasses.dataclass(frozen=True)
class RunLedger:
    """The wakes that have run, by key."""

    root: pathlib.Path

    def path(self, key: RunKey) -> pathlib.Path:
        return self.root / RUNS_DIR / f"{key.slug}.json"

    def has(self, key: RunKey) -> bool:
        return self.path(key).exists()

    def record(self, key: RunKey, holder: str, at: dt.datetime) -> pathlib.Path:
        """Write the wake's file; the claim commit carries it to origin."""
        body = {"key": str(key), "holder": holder, "at": at.isoformat()}
        atomic_write(self.path(key), json.dumps(body, indent=2) + "\n")
        return self.path(key)
