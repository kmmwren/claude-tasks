#!/usr/bin/env python3
"""
wake.py: when a brief asks to run later, and what makes it due.

A brief names its wake in frontmatter: `not_before:` (a date or ISO datetime) and/or
`wake_probe:` (a shell command), `valid_until:` and `escalate_after:` (moments), `needs:`
(capabilities such as laptop, aws, slack) and `wake_generation:` (bump it to let a finished
wake fire again). A brief naming neither `not_before` nor `wake_probe` is not a wake brief.

The probe exit contract is the coordinator's blocker probe: 0 means due, 10 means not yet,
anything else is an error that is logged and never read as due.

Stdlib only.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import dataclasses
import datetime as dt
import enum
import hashlib
import pathlib
import subprocess
from collections.abc import Callable, Mapping

from _dates import parse_moment
from _paths import warn
from deps import fm, parse_list

PROBE_DUE_EXIT = 0
PROBE_NOT_YET_EXIT = 10
PROBE_TIMEOUT_S = 120
LAPTOP = "laptop"
# `[maintain.<tier>] capabilities = ["*"]` declares a tier that serves every `needs:`.
EVERYTHING = "*"
PROBE_TRIGGER = "probe"

Shell = Callable[[str, pathlib.Path], "subprocess.CompletedProcess[str]"]


class MalformedWake(ValueError):
    """A brief whose wake fields cannot be read; the brief is skipped, never the tick."""


class Due(enum.Enum):
    DUE = "due"
    NOT_YET = "not-yet"
    ERROR = "error"


@dataclasses.dataclass(frozen=True)
class Reading:
    """One probe's outcome and the evidence a log line needs."""

    due: Due
    detail: str


def shell(command: str, cwd: pathlib.Path) -> "subprocess.CompletedProcess[str]":
    """Run `command` with stdin closed, so a probe that prompts fails fast instead of hanging a scheduled tick."""
    return subprocess.run(
        ["bash", "-c", command],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=PROBE_TIMEOUT_S,
        stdin=subprocess.DEVNULL,
    )


@dataclasses.dataclass(frozen=True)
class ProbeRunner:
    """Runs a `wake_probe:` in the queue and reads its exit status by the probe contract."""

    cwd: pathlib.Path
    run: Shell = shell

    def __call__(self, command: str) -> Reading:
        try:
            done = self.run(command, self.cwd)
        except (OSError, subprocess.SubprocessError) as e:
            return Reading(Due.ERROR, f"{type(e).__name__}: {e}")
        if done.returncode == PROBE_DUE_EXIT:
            return Reading(Due.DUE, f"exit {PROBE_DUE_EXIT}")
        if done.returncode == PROBE_NOT_YET_EXIT:
            return Reading(Due.NOT_YET, f"exit {PROBE_NOT_YET_EXIT}")
        tail = (done.stderr or "").strip().splitlines()[-1:] or ["no stderr"]
        return Reading(Due.ERROR, f"exit {done.returncode}: {tail[0][:200]}")


@dataclasses.dataclass(frozen=True)
class RunKey:
    """One wake of one brief: its id, what triggered it and its generation."""

    brief_id: str
    trigger: str
    generation: int

    def __str__(self) -> str:
        return f"{self.brief_id}|{self.trigger}|g{self.generation}"

    @property
    def slug(self) -> str:
        """A filename-safe digest of the key, because a trigger holds colons."""
        return hashlib.sha256(str(self).encode()).hexdigest()[:20]


def _moment(fields: Mapping[str, str], key: str) -> dt.datetime | None:
    raw = fields.get(key, "")
    if not raw:
        return None
    moment = parse_moment(raw)
    if moment is None:
        raise MalformedWake(f"{key}: {raw!r} is not an ISO date or datetime")
    return moment


def _generation(fields: Mapping[str, str]) -> int:
    raw = fields.get("wake_generation", "")
    try:
        return int(raw) if raw else 0
    except ValueError:
        raise MalformedWake(f"wake_generation: {raw!r} is not a whole number") from None


def _needs(fields: Mapping[str, str]) -> tuple[str, ...]:
    needs = parse_list(fields.get("needs", ""))
    local = fields.get("requires-local", "").lower() == "true"
    return tuple(dict.fromkeys([*needs, *([LAPTOP] if local else [])]))


@dataclasses.dataclass(frozen=True)
class Wake:
    brief_id: str
    not_before: dt.datetime | None
    probe: str | None
    valid_until: dt.datetime | None
    escalate_after: dt.datetime | None
    needs: tuple[str, ...]
    generation: int
    unattended: bool

    @classmethod
    def parse(cls, brief_id: str, text: str) -> "Wake | None":
        """The brief's wake, or None when it names no trigger; raises MalformedWake on a field it cannot read."""
        fields = fm(text)
        not_before, probe = _moment(fields, "not_before"), fields.get("wake_probe") or None
        if not_before is None and probe is None:
            return None
        return cls(
            brief_id,
            not_before,
            probe,
            _moment(fields, "valid_until"),
            _moment(fields, "escalate_after"),
            _needs(fields),
            _generation(fields),
            fields.get("autonomy", "") in ("full", ""),
        )

    @property
    def key(self) -> RunKey:
        trigger = self.not_before.isoformat() if self.not_before else PROBE_TRIGGER
        return RunKey(self.brief_id, trigger, self.generation)

    def lapsed(self, now: dt.datetime) -> bool:
        return self.valid_until is not None and self.valid_until <= now

    def escalated(self, now: dt.datetime) -> bool:
        return self.escalate_after is not None and self.escalate_after <= now

    def missing(self, capabilities: frozenset[str]) -> tuple[str, ...]:
        """The `needs:` a tier with `capabilities` cannot serve."""
        if EVERYTHING in capabilities:
            return ()
        return tuple(need for need in self.needs if need not in capabilities)

    def due(self, now: dt.datetime, probe: Callable[[str], Reading]) -> Due:
        """Due once `not_before` has passed or the probe clears; the probe only runs when the date has not decided it."""
        if self.not_before is not None and self.not_before <= now:
            return Due.DUE
        if self.probe is None:
            return Due.NOT_YET
        reading = probe(self.probe)
        if reading.due is Due.ERROR:
            warn(f"wake_probe of {self.brief_id} failed ({reading.detail}); not due", "maintain")
        return reading.due
