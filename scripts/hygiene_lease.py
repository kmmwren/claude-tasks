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
import enum
import functools
import json
import pathlib
import time
from collections.abc import Callable, Sequence

from _concurrency import atomic_write
from _paths import warn
from claim import Claim, Claimer, Lease, Outcome
from maintain_state import LastRun
from origin_push import Edit, OriginBusy, PushError, PushRefused, push_from_fresh_origin

HYGIENE_LEASE = "maintain/hygiene-lease.json"
RELEASE_ATTEMPTS = 3
RELEASE_PAUSE = 2.0


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


class Standing(enum.Enum):
    """Whether a lease can be published for the queue, which decides where records live."""

    SHARED = "shared"  # the lease and the records are published to origin
    LOCAL = "local"  # no origin: records stay in this checkout and there is no one to race
    NESTED = (
        "nested"  # below its repo root, a published lease would sit where the records are not read
    )


@dataclasses.dataclass(frozen=True)
class HygieneLeaser:
    """Takes and releases the hygiene lease of one queue for one holder."""

    claimer: Claimer
    pause: Callable[[float], None] = time.sleep

    @functools.cached_property
    def standing(self) -> Standing:
        if not self.claimer.has_origin():
            return Standing.LOCAL
        return Standing.SHARED if self.claimer.owns_repo() else Standing.NESTED

    def acquire(self, now: dt.datetime) -> Claim:
        if self.standing is Standing.NESTED:
            return Claim(
                Outcome.REFUSED,
                "the queue is not the root of its git repo, so it cannot hold the hygiene lease",
            )
        if self.standing is Standing.LOCAL:
            warn(
                "the queue has no origin; tidying up under a local record, without a lease",
                "maintain",
            )
            return Claim(
                Outcome.CLAIMED, "local", Lease.first(self.claimer.holder, now, self.claimer.ttl)
            )
        edit = TakeLease(self.claimer.holder, now, self.claimer.ttl)
        claim = self._publish(edit, f"maintain: {self.claimer.holder} takes the hygiene lease")
        return dataclasses.replace(claim, lease=edit.taken) if claim.won else claim

    def release(self, lease: Lease, records: Sequence[LastRun]) -> Claim:
        """Publish the records and let the lease go; a failed publish is tried again a few times."""
        if self.standing is Standing.LOCAL:
            for record in records:
                record.write(self.claimer.root)
            return Claim(Outcome.CLAIMED, "recorded locally")
        edit = ReleaseLease(lease, tuple(records))
        message = f"maintain: {self.claimer.holder} finishes hygiene"
        released = self._publish(edit, message)
        for attempt in range(1, RELEASE_ATTEMPTS):
            if released.outcome is not Outcome.FAILED:
                break
            self.pause(RELEASE_PAUSE * attempt)
            released = self._publish(edit, message)
        return released

    def refresh(self) -> bool:
        """Bring the checkout up to date with origin; True too when there is no origin to bring it up to."""
        return self.standing is not Standing.SHARED or self.claimer.refresh()

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
