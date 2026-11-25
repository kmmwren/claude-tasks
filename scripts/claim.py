#!/usr/bin/env python3
"""
claim.py: take a brief with a git-pushed lease, so two runners never run the same wake.

A claim is a compare-and-set on origin. It is built in a scratch worktree cut from origin's
default branch: the lease goes into the brief's frontmatter, the brief moves to in-progress/ and
the wake's run record is written, all in one commit. A plain (never rebased, never forced) push
publishes it; a rejected push means another runner moved origin first, and the claim is dropped.
Only a published claim may be run.

Stdlib only. The checkout is advanced afterwards by fast-forward only, so a local commit the
pusher has not sent yet is never discarded.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import dataclasses
import datetime as dt
import enum
import pathlib
import subprocess
from collections.abc import Callable, Mapping

from _concurrency import NETWORK_TIMEOUT, atomic_write, repo_lock
from _dates import parse_moment
from _default_branch import default_branch
from _paths import warn
from deps import fm
from git_sync import scratch_worktree
from maintain_state import RunLedger
from origin_push import lost_race
from transition import find_brief, move_brief, set_frontmatter_fields
from wake import RunKey, Wake

LEASE_HOLDER = "lease_holder"
LEASE_EXPIRES = "lease_expires"
LEASE_ATTEMPTS = "lease_attempts"
WAKE_KEY = "wake_key"
IN_PROGRESS = "in-progress"
# A laptop clock can drift or jump ahead by minutes; a lease promising more time than this has not
# been taken yet and is treated as lapsed.
HOLDER_CLOCK_AHEAD = dt.timedelta(minutes=10)
REFRESH_LOCK_WAIT = 5.0
EPOCH = dt.datetime.min.replace(tzinfo=dt.timezone.utc)

Fetch = Callable[[pathlib.Path, str], bool]


@dataclasses.dataclass(frozen=True)
class Lease:
    """Who holds a brief, until when, and how many times it has been claimed for this wake."""

    holder: str
    expires: dt.datetime
    attempts: int

    @classmethod
    def from_fields(cls, fields: Mapping[str, str]) -> "Lease | None":
        """The lease a brief carries; None when it carries none, and one that cannot be read counts as expired."""
        holder = fields.get(LEASE_HOLDER, "")
        if not holder:
            return None
        expires = parse_moment(fields.get(LEASE_EXPIRES, ""))
        attempts = fields.get(LEASE_ATTEMPTS, "")
        if expires is None or not attempts.isdigit():
            warn(
                f"unreadable lease on a brief held by {holder}; treating it as expired", "maintain"
            )
        return cls(holder, expires or EPOCH, int(attempts) if attempts.isdigit() else 0)

    def expired(self, now: dt.datetime) -> bool:
        return self.expires <= now

    def live(self, now: dt.datetime, ttl: dt.timedelta) -> bool:
        """Unexpired and no further ahead than a ttl plus a holder's clock drift; an absurd promise is not live."""
        return now < self.expires <= now + ttl + HOLDER_CLOCK_AHEAD

    def fields(self) -> dict[str, str]:
        return {
            LEASE_HOLDER: self.holder,
            LEASE_EXPIRES: self.expires.isoformat(),
            LEASE_ATTEMPTS: str(self.attempts),
        }

    def renewed(self, holder: str, now: dt.datetime, ttl: dt.timedelta) -> "Lease":
        return Lease(holder, now + ttl, self.attempts + 1)

    @classmethod
    def first(cls, holder: str, now: dt.datetime, ttl: dt.timedelta) -> "Lease":
        return cls(holder, now + ttl, 1)


@dataclasses.dataclass(frozen=True)
class Target:
    """A brief to claim: where this checkout saw it and the wake it is claimed for."""

    brief_id: str
    folder: str
    key: RunKey

    @property
    def reclaim(self) -> bool:
        """An in-progress brief is only ever claimed again because its lease ran out."""
        return self.folder == IN_PROGRESS


class Outcome(enum.Enum):
    CLAIMED = "claimed"
    LOST = "lost"  # another runner published first, or the brief changed under us
    REFUSED = "refused"  # the remote declined the push (permissions, protection, a hook); retrying cannot help
    FAILED = "failed"  # nothing was published for another reason


@dataclasses.dataclass(frozen=True)
class Claim:
    outcome: Outcome
    detail: str
    lease: Lease | None = None

    @property
    def won(self) -> bool:
        return self.outcome is Outcome.CLAIMED


class ClaimLost(Exception):
    """Origin no longer holds the brief as the claim saw it."""


def _git(where: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-C", str(where), *args], capture_output=True, text=True, timeout=NETWORK_TIMEOUT
    )


def fetch_origin(root: pathlib.Path, branch: str) -> bool:
    try:
        return _git(root, "fetch", "-q", "origin", branch).returncode == 0
    except subprocess.TimeoutExpired:
        return False


@dataclasses.dataclass(frozen=True)
class Claimer:
    """Claims briefs of one queue for one holder."""

    root: pathlib.Path
    holder: str
    ttl: dt.timedelta
    fetch: Fetch = fetch_origin

    def claim(self, target: Target, now: dt.datetime) -> Claim:
        """Publish a claim on `target`; never raises, because one bad brief must not end a tick."""
        if not self.owns_repo():
            return Claim(
                Outcome.FAILED, "the queue is not the root of its git repo, so it cannot be claimed"
            )
        branch = default_branch(self.root)
        if not self.fetch(self.root, branch):
            return Claim(Outcome.FAILED, "could not fetch origin")
        try:
            return self._claim(target, branch, now)
        except ClaimLost as e:
            return Claim(Outcome.LOST, str(e))
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as e:
            return Claim(Outcome.FAILED, f"{type(e).__name__}: {e}")

    def _claim(self, target: Target, branch: str, now: dt.datetime) -> Claim:
        with scratch_worktree(self.root, f"origin/{branch}") as wt:
            if not wt.is_dir():
                return Claim(Outcome.FAILED, f"could not cut a worktree from origin/{branch}")
            lease = self._stage(wt, target, now)
            self._commit(wt, target)
            published = self._publish(wt, branch)
        if not published.won:
            return published
        self._advance_checkout()
        return Claim(Outcome.CLAIMED, f"claimed until {lease.expires.isoformat()}", lease)

    def _stage(self, wt: pathlib.Path, target: Target, now: dt.datetime) -> Lease:
        path = find_brief(target.brief_id, wt)
        if path is None or path.parent.name != target.folder:
            raise ClaimLost(f"{target.brief_id} is no longer in {target.folder}/ on origin")
        text = path.read_text()
        lease = self._lease_for(wt, target, text, now)
        atomic_write(
            path, set_frontmatter_fields(text, {**lease.fields(), WAKE_KEY: str(target.key)})
        )
        move_brief(
            target.brief_id, IN_PROGRESS, wt, log=f"claimed by {self.holder} for wake {target.key}"
        )
        if not target.reclaim:
            RunLedger(wt).record(target.key, self.holder, now)
        return lease

    def _lease_for(self, wt: pathlib.Path, target: Target, text: str, now: dt.datetime) -> Lease:
        """The new lease, after checking that origin still shows what this claim was decided on."""
        held = Lease.from_fields(fm(text))
        if target.reclaim:
            if held is None or held.live(now, self.ttl):
                raise ClaimLost(f"{target.brief_id} holds a live lease on origin")
            return held.renewed(self.holder, now, self.ttl)
        wake = Wake.parse(target.brief_id, text)
        if wake is None or wake.key != target.key:
            raise ClaimLost(f"the wake of {target.brief_id} changed on origin")
        if RunLedger(wt).has(target.key):
            raise ClaimLost(f"wake {target.key} already ran")
        return Lease.first(self.holder, now, self.ttl)

    def _commit(self, wt: pathlib.Path, target: Target) -> None:
        _git(wt, "add", "-A")
        done = _git(wt, "commit", "-q", "-m", f"maintain: {self.holder} claims {target.brief_id}")
        if done.returncode != 0:
            raise RuntimeError(f"commit failed: {done.stderr.strip()[-200:]}")

    def _publish(self, wt: pathlib.Path, branch: str) -> Claim:
        pushed = _git(wt, "push", "-q", "origin", f"HEAD:{branch}")
        if pushed.returncode == 0:
            return Claim(Outcome.CLAIMED, "published")
        why = " ".join(pushed.stderr.split())[:240]
        outcome = Outcome.LOST if lost_race(pushed.stderr) else Outcome.REFUSED
        return Claim(outcome, f"push refused: {why}")

    def owns_repo(self) -> bool:
        """A claim edits the repo's tree, so the queue's folders must sit at its top level."""
        top = _git(self.root, "rev-parse", "--show-toplevel").stdout.strip()
        return bool(top) and pathlib.Path(top).resolve() == self.root.resolve()

    def has_origin(self) -> bool:
        return _git(self.root, "remote", "get-url", "origin").returncode == 0

    def refresh(self) -> bool:
        """Fast-forward the checkout to origin; a diverged or dirty one is left for the sweep."""
        branch = default_branch(self.root)
        if not self.fetch(self.root, branch):
            return False
        with repo_lock(self.root, REFRESH_LOCK_WAIT) as held:
            if not held:
                warn("the queue lock is busy; the checkout was not advanced to origin", "maintain")
                return False
            return _git(self.root, "merge", "--ff-only", "-q", f"origin/{branch}").returncode == 0

    def _advance_checkout(self) -> None:
        if not self.refresh():
            warn("claim published, but the checkout was not fast-forwarded to origin", "maintain")
