#!/usr/bin/env python3
"""
hygiene_lease.py: the lease one runner holds while it tidies the queue, published like a claim.

The lease is a file in the queue repo, taken and released by a commit pushed from a scratch
worktree. Each attempt re-reads the lease at origin's tip, so a push that lost a race is decided
again against what the winner wrote. Releasing carries the finished phases' last-run records in
the same commit, so a record reaches origin exactly when its lease is let go.

Stdlib only.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import dataclasses
import datetime as dt
import json
import pathlib
from collections.abc import Sequence

from _concurrency import atomic_write
from _paths import warn
from claim import Claim, Claimer, Lease, Outcome
from maintain_state import LastRun
from origin_push import Edit, OriginBusy, PushError, PushRefused, push_from_fresh_origin

HYGIENE_LEASE = "maintain/hygiene-lease.json"


class LeaseHeld(Exception):
    """Origin shows the lease with someone else, or no longer with us."""


def read_lease(worktree: pathlib.Path) -> Lease | None:
    """The lease in `worktree`; None when there is none, and one that cannot be read is as good as none."""
    path = worktree / HYGIENE_LEASE
    try:
        raw = json.loads(path.read_text())
        return Lease.from_fields({key: str(value) for key, value in raw.items()})
    except FileNotFoundError:
        return None
    except (OSError, ValueError, AttributeError) as e:
        warn(f"ignoring unreadable {path} ({e})", "maintain")
        return None


@dataclasses.dataclass
class TakeLease:
    """The edit that takes the lease unless a live one is held; `taken` is what the last attempt wrote."""

    holder: str
    now: dt.datetime
    ttl: dt.timedelta
    taken: Lease | None = None

    def __call__(self, worktree: pathlib.Path) -> bool:
        held = read_lease(worktree)
        if held is not None and held.live(self.now, self.ttl):
            raise LeaseHeld(f"held by {held.holder} until {held.expires.isoformat()}")
        self.taken = Lease.first(self.holder, self.now, self.ttl)
        atomic_write(worktree / HYGIENE_LEASE, json.dumps(self.taken.fields(), indent=2) + "\n")
        return True


@dataclasses.dataclass(frozen=True)
class ReleaseLease:
    """The edit that lets our lease go and writes the finished phases' records beside it."""

    lease: Lease
    records: tuple[LastRun, ...]

    def __call__(self, worktree: pathlib.Path) -> bool:
        if read_lease(worktree) != self.lease:
            raise LeaseHeld("the lease is no longer ours; another runner took it over")
        (worktree / HYGIENE_LEASE).unlink()
        for record in self.records:
            record.write(worktree)
        return True


@dataclasses.dataclass(frozen=True)
class HygieneLeaser:
    """Takes and releases the hygiene lease of one queue for one holder."""

    claimer: Claimer

    def acquire(self, now: dt.datetime) -> Claim:
        edit = TakeLease(self.claimer.holder, now, self.claimer.ttl)
        claim = self._publish(edit, f"maintain: {self.claimer.holder} takes the hygiene lease")
        return dataclasses.replace(claim, lease=edit.taken) if claim.won else claim

    def release(self, lease: Lease, records: Sequence[LastRun]) -> Claim:
        edit = ReleaseLease(lease, tuple(records))
        return self._publish(edit, f"maintain: {self.claimer.holder} finishes hygiene")

    def refresh(self) -> bool:
        return self.claimer.refresh()

    def _publish(self, edit: Edit, message: str) -> Claim:
        """The outcome of publishing `edit`; never raises, because hygiene must not end a tick."""
        try:
            push_from_fresh_origin(self.claimer.root, edit, message)
        except (LeaseHeld, OriginBusy) as e:
            return Claim(Outcome.LOST, str(e))
        except PushRefused as e:
            return Claim(Outcome.REFUSED, str(e))
        except (PushError, OSError) as e:
            return Claim(Outcome.FAILED, f"{type(e).__name__}: {e}")
        return Claim(Outcome.CLAIMED, "published")
