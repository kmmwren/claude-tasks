#!/usr/bin/env python3
"""
maintain_state.py: the run record (last successful run per tier) and the runner lease.

Shared state is committed to origin/main under `.state/`. A runner that cannot reach origin keeps
the same two files under the clone's git dir, and the record merges forward on the next read.

Stdlib only.
"""

import abc
import dataclasses
import datetime
import enum
import json
import os
import pathlib
import re
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import NewType, TypeVar

from _concurrency import atomic_write, clone_state_path, require_lock
from git_sync import owns_repo
from origin_push import (
    OriginBusy,
    PushError,
    fetch_origin,
    has_origin,
    push_from_fresh_origin,
    read_origin,
)

STATE_DIR = ".state"
RECORD_PATH = f"{STATE_DIR}/maintain.json"
LEASE_PATH = f"{STATE_DIR}/maintain.lease"
LOCAL_DIR = "maintain"
LOCAL_RECORD = "record.json"
LOCAL_LEASE = "lease.json"
LOCAL_LOCK_WAIT = 5.0
CLOCK_SKEW = datetime.timedelta(minutes=1)  # slack for a peer whose clock runs a little ahead

_DURATION = re.compile(r"(\d+)([smhd])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

T = TypeVar("T")
RunnerName = NewType("RunnerName", str)


def parse_duration(text: str) -> datetime.timedelta:
    """`90s`, `30m`, `4h` or `2d` as a timedelta; anything else is a ValueError for argparse to report."""
    match = _DURATION.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"invalid duration {text!r}: use a number and s, m, h or d, e.g. 4h")
    return datetime.timedelta(seconds=int(match[1]) * _UNIT_SECONDS[match[2]])


def format_age(age: datetime.timedelta) -> str:
    """A short human age: `<1m`, `12m`, `3h`, `2d`."""
    seconds = age.total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return "<1m"


def _stamp(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.UTC).isoformat(timespec="seconds")


def _document(text: str) -> dict[str, object]:
    """The JSON object in `text`; a list or a bare value is as unreadable as bad syntax."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _field(data: Mapping[str, object], key: str, kind: type[T]) -> T:
    value = data.get(key)
    if not isinstance(value, kind):
        raise ValueError(f"{key!r} must be a {kind.__name__}")
    return value


def _names(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    return tuple(str(item) for item in _field(data, key, list))


def _moment(data: Mapping[str, object], key: str) -> datetime.datetime:
    moment = datetime.datetime.fromisoformat(_field(data, key, str))
    if moment.tzinfo is None:
        raise ValueError(f"{key!r} carries no timezone")
    return moment


class Tier(enum.Enum):
    """A group of maintenance steps with its own freshness; the order here is the order they run."""

    DETERMINISTIC = "deterministic"  # no Claude calls, so it needs no credits
    JUDGEMENT = "judgement"  # calls headless Claude

    @property
    def needs_capacity(self) -> bool:
        return self is Tier.JUDGEMENT


class Outcome(enum.Enum):
    OK = "ok"
    FAILED = "failed"
    DEFERRED = "deferred: no capacity"


@dataclasses.dataclass(frozen=True)
class Counts:
    steps_ok: int = 0
    steps_failed: int = 0

    def to_json(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "Counts":
        return cls(**{f.name: _field(data, f.name, int) for f in dataclasses.fields(cls)})


@dataclasses.dataclass(frozen=True)
class TierRun:
    tier: Tier
    started: datetime.datetime
    finished: datetime.datetime
    runner: RunnerName
    outcome: Outcome
    counts: Counts
    skipped: tuple[str, ...] = ()  # steps whose script this environment lacked

    def fresh(self, now: datetime.datetime, max_age: datetime.timedelta) -> bool:
        """A finish time in the future is clock skew, not freshness."""
        return datetime.timedelta(0) <= now - self.finished < max_age

    def describe(self, now: datetime.datetime) -> str:
        """`last run 2026-11-17 09:00Z (35m ago) by laptop`."""
        when = f"{self.finished.astimezone(datetime.UTC):%Y-%m-%d %H:%M}Z"
        return f"last run {when} ({format_age(now - self.finished)} ago) by {self.runner}"

    def to_json(self) -> dict[str, object]:
        return {
            "started": _stamp(self.started),
            "finished": _stamp(self.finished),
            "runner": self.runner,
            "outcome": self.outcome.value,
            "counts": self.counts.to_json(),
            "skipped": list(self.skipped),
        }

    @classmethod
    def from_json(cls, tier: Tier, data: Mapping[str, object]) -> "TierRun":
        return cls(
            tier,
            _moment(data, "started"),
            _moment(data, "finished"),
            RunnerName(_field(data, "runner", str)),
            Outcome(_field(data, "outcome", str)),
            Counts.from_json(_field(data, "counts", dict)),
            _names(data, "skipped"),
        )


@dataclasses.dataclass(frozen=True)
class RunRecord:
    """The last successful run of each tier."""

    runs: Mapping[Tier, TierRun] = dataclasses.field(default_factory=dict)

    def last(self, tier: Tier) -> TierRun | None:
        return self.runs.get(tier)

    def with_runs(self, runs: Iterable[TierRun]) -> "RunRecord":
        """This record with each run in place of its tier's, unless the held one finished later."""
        merged = dict(self.runs)
        for run in runs:
            held = merged.get(run.tier)
            if held is None or run.finished >= held.finished:
                merged[run.tier] = run
        return RunRecord(merged)

    def merged(self, other: "RunRecord") -> "RunRecord":
        return self.with_runs(other.runs.values())

    def to_text(self) -> str:
        tiers = {tier.value: run.to_json() for tier, run in self.runs.items()}
        return json.dumps({"tiers": tiers}, indent=2, sort_keys=True) + "\n"

    @classmethod
    def parse(cls, text: str) -> "RunRecord":
        """The record in `text`; an absent file is empty, and a corrupt one warns and counts as empty."""
        if not text.strip():
            return cls()
        try:
            tiers = _field(_document(text), "tiers", dict)
            return cls(
                {
                    tier: TierRun.from_json(tier, _field(tiers, tier.value, dict))
                    for tier in Tier
                    if tier.value in tiers
                }
            )
        except (ValueError, TypeError) as e:
            print(f"maintain: ignoring an unreadable run record: {e}", file=sys.stderr)
            return cls()


@dataclasses.dataclass(frozen=True)
class Lease:
    """One runner's claim on the queue. The token tells a lease apart from a takeover of the same runner."""

    runner: RunnerName
    token: str
    started: datetime.datetime
    expires: datetime.datetime

    @classmethod
    def take(cls, runner: RunnerName, now: datetime.datetime, ttl: datetime.timedelta) -> "Lease":
        return cls(runner, uuid.uuid4().hex, now, now + ttl)

    def live(self, now: datetime.datetime) -> bool:
        """Unexpired, and not promising longer than its own ttl: a clock far ahead cannot hold it forever."""
        remaining = self.expires - now
        return datetime.timedelta(0) < remaining <= self.expires - self.started + CLOCK_SKEW

    def to_text(self) -> str:
        data = {
            "runner": self.runner,
            "token": self.token,
            "started": _stamp(self.started),
            "expires": _stamp(self.expires),
        }
        return json.dumps(data, indent=2) + "\n"

    @classmethod
    def parse(cls, text: str) -> "Lease | None":
        """The lease in `text`; None for no file or an unreadable one, which anyone may replace."""
        try:
            data = _document(text)
            return cls(
                RunnerName(_field(data, "runner", str)),
                _field(data, "token", str),
                _moment(data, "started"),
                _moment(data, "expires"),
            )
        except (ValueError, TypeError):
            return None


class LeaseResult(abc.ABC):
    """The outcome of asking for the lease: ours, or a reason this runner stands down."""

    @abc.abstractmethod
    def claimed(self) -> Lease | None:
        """The lease when it is ours."""

    @abc.abstractmethod
    def work(self, job: Callable[[Lease], int]) -> int:
        """Run `job` under the lease when it is ours; otherwise say why not, and succeed."""


@dataclasses.dataclass(frozen=True)
class Acquired(LeaseResult):
    lease: Lease

    def claimed(self) -> Lease | None:
        return self.lease

    def work(self, job: Callable[[Lease], int]) -> int:
        return job(self.lease)


class StandDown(LeaseResult, abc.ABC):
    def claimed(self) -> Lease | None:
        return None

    def work(self, job: Callable[[Lease], int]) -> int:
        print(self.reason())
        return 0

    @abc.abstractmethod
    def reason(self) -> str: ...


@dataclasses.dataclass(frozen=True)
class Held(StandDown):
    """Another runner's live lease."""

    lease: Lease

    def reason(self) -> str:
        return f"leased by {self.lease.runner} until {self.lease.expires:%H:%M}Z"


@dataclasses.dataclass(frozen=True)
class Contended(StandDown):
    """Origin kept moving, so no runner can be told apart as the holder; the next trigger retries."""

    detail: str

    def reason(self) -> str:
        return f"another runner is taking the lease ({self.detail})"


class ExchangeUnavailable(Exception):
    """The shared copy of the state could not be read or written; a local file stands in."""


class Exchange(abc.ABC):
    """Where the run record and the lease live, as seen by one runner."""

    @abc.abstractmethod
    def record(self) -> RunRecord: ...

    @abc.abstractmethod
    def acquire(
        self, runner: RunnerName, now: datetime.datetime, ttl: datetime.timedelta
    ) -> LeaseResult: ...

    @abc.abstractmethod
    def finish(self, lease: Lease, runs: Sequence[TierRun]) -> None:
        """Record `runs` and release `lease`, if it is still ours."""


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def local_path(root: pathlib.Path, name: str) -> pathlib.Path:
    """A local state file under the clone's git dir, so it is never committed or reset away."""
    return clone_state_path(root, f"{LOCAL_DIR}/{name}")


class LocalExchange(Exchange):
    """The state as plain files in the clone's git dir, for a queue with no remote.

    The lease is created with O_EXCL under the queue lock, so two local processes cannot both
    take it, and an expired one is replaced inside that same lock.
    """

    def __init__(self, root: pathlib.Path) -> None:
        self._root = root

    def _path(self, name: str) -> pathlib.Path:
        return local_path(self._root, name)

    def record(self) -> RunRecord:
        return RunRecord.parse(_read(self._path(LOCAL_RECORD)))

    def acquire(
        self, runner: RunnerName, now: datetime.datetime, ttl: datetime.timedelta
    ) -> LeaseResult:
        lease, path = Lease.take(runner, now, ttl), self._path(LOCAL_LEASE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with require_lock(self._root, LOCAL_LOCK_WAIT):
            held = Lease.parse(_read(path))
            if held is not None and held.live(now):
                return Held(held)
            path.unlink(missing_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w") as handle:
                handle.write(lease.to_text())
        return Acquired(lease)

    def keep(self, runs: Sequence[TierRun]) -> None:
        """Record `runs` here without touching the lease, so a later read of origin merges them in."""
        with require_lock(self._root, LOCAL_LOCK_WAIT):
            self._merge(runs)

    def _merge(self, runs: Sequence[TierRun]) -> None:
        if runs:
            atomic_write(self._path(LOCAL_RECORD), self.record().with_runs(runs).to_text())

    def finish(self, lease: Lease, runs: Sequence[TierRun]) -> None:
        with require_lock(self._root, LOCAL_LOCK_WAIT):
            self._merge(runs)
            held = Lease.parse(_read(self._path(LOCAL_LEASE)))
            if held is not None and held.token == lease.token:
                self._path(LOCAL_LEASE).unlink(missing_ok=True)


def _unavailable(error: PushError) -> ExchangeUnavailable:
    return ExchangeUnavailable(str(error))


def _live_lease(worktree: pathlib.Path, now: datetime.datetime) -> Lease | None:
    held = Lease.parse(_read(worktree / LEASE_PATH))
    return held if held is not None and held.live(now) else None


class GitExchange(Exchange):
    """The state on origin/main, written from a scratch worktree so the queue's checkout is untouched."""

    def __init__(self, root: pathlib.Path, local: LocalExchange) -> None:
        self._root = root
        self._local = local

    def record(self) -> RunRecord:
        """origin's record merged with any a run left in this checkout while origin was unreachable."""
        try:
            fetch_origin(self._root)
        except PushError as e:
            raise _unavailable(e) from e
        return RunRecord.parse(read_origin(self._root, RECORD_PATH)).merged(self._local.record())

    def _publish(self, edit: Callable[[pathlib.Path], bool], message: str) -> None:
        try:
            push_from_fresh_origin(self._root, edit, message)
        except OriginBusy:
            raise
        except PushError as e:
            raise _unavailable(e) from e

    def acquire(
        self, runner: RunnerName, now: datetime.datetime, ttl: datetime.timedelta
    ) -> LeaseResult:
        lease = Lease.take(runner, now, ttl)
        rival: Lease | None = None

        def edit(worktree: pathlib.Path) -> bool:
            nonlocal rival
            rival = _live_lease(worktree, now)
            if rival is not None:
                return False
            (worktree / STATE_DIR).mkdir(exist_ok=True)
            (worktree / LEASE_PATH).write_text(lease.to_text())
            return True

        try:
            self._publish(edit, f"maintain: {runner} leases")
        except OriginBusy as e:
            return Contended(str(e))
        return Acquired(lease) if rival is None else Held(rival)

    def finish(self, lease: Lease, runs: Sequence[TierRun]) -> None:
        def edit(worktree: pathlib.Path) -> bool:
            held = Lease.parse(_read(worktree / LEASE_PATH))
            if held is not None and held.token == lease.token:
                (worktree / LEASE_PATH).unlink()
            if runs:
                record = RunRecord.parse(_read(worktree / RECORD_PATH)).with_runs(runs)
                (worktree / STATE_DIR).mkdir(exist_ok=True)
                (worktree / RECORD_PATH).write_text(record.to_text())
            return True

        try:
            self._publish(edit, f"maintain: {lease.runner} finished")
        except OriginBusy as e:
            raise _unavailable(e) from e


class FallbackExchange(Exchange):
    """Prefers the shared exchange, and keeps working from local files once it is unreachable.

    An offline laptop still maintains its queue; the record it leaves is merged in later. Once a
    remote lease is held, the run stays remote: a failure then never swaps in a local lease.
    """

    def __init__(self, shared: Exchange, local: LocalExchange) -> None:
        self._active: Exchange = shared
        self._local = local
        self._remote_lease: Lease | None = None

    def _call(self, operation: Callable[[Exchange], T]) -> T:
        try:
            return operation(self._active)
        except ExchangeUnavailable as e:
            if self._active is self._local:
                raise
            print(f"maintain: shared state unavailable ({e}); using local files", file=sys.stderr)
            self._active = self._local
            return operation(self._active)

    def record(self) -> RunRecord:
        return self._call(lambda exchange: exchange.record())

    def acquire(
        self, runner: RunnerName, now: datetime.datetime, ttl: datetime.timedelta
    ) -> LeaseResult:
        leased = self._call(lambda exchange: exchange.acquire(runner, now, ttl))
        if self._active is not self._local:
            self._remote_lease = leased.claimed()
        return leased

    def finish(self, lease: Lease, runs: Sequence[TierRun]) -> None:
        if lease == self._remote_lease:
            self._finish_remote(lease, runs)
        else:
            self._call(lambda exchange: exchange.finish(lease, runs))

    def _finish_remote(self, lease: Lease, runs: Sequence[TierRun]) -> None:
        try:
            self._active.finish(lease, runs)
        except ExchangeUnavailable as e:
            print(f"maintain: lease not released ({e}); it expires on its own", file=sys.stderr)
            self._local.keep(runs)


def open_exchange(root: pathlib.Path) -> Exchange:
    """The git exchange for a queue that is its own repo with an origin, else plain local files."""
    local = LocalExchange(root)
    if owns_repo(root) and has_origin(root):
        return FallbackExchange(GitExchange(root, local), local)
    return local
