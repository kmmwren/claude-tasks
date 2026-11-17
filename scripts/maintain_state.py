#!/usr/bin/env python3
"""
maintain_state.py: the run record and the lease that let any trigger run the engine without racing.

The record (`<queue>/.state/maintain.json`) holds the last successful run per tier, and the lease
(`<queue>/.state/maintain.lease`) names the runner currently working. Both are committed to the
queue's git repo, so a laptop timer and a cloud routine, on any git host, read and write one truth.
A queue with no remote keeps the same two files locally.

Leases are taken and released in a scratch worktree cut from origin/main and pushed without
rebasing: a rejected push means the tip moved, so the taker re-reads it and either finds a live
lease (someone else won) or retries on the new tip. The queue's own checkout never carries a lease
commit, so it can never be left diverged from origin by a lost race.

Stdlib only.
"""

import abc
import contextlib
import dataclasses
import datetime
import enum
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import TypeVar

from _concurrency import atomic_write, require_lock
from git_sync import NETWORK_TIMEOUT, owns_repo

STATE_DIR = ".state"
RECORD_PATH = f"{STATE_DIR}/maintain.json"
LEASE_PATH = f"{STATE_DIR}/maintain.lease"
REMOTE_REF = "origin/main"
PUSH_ATTEMPTS = 5  # a busy origin can reject a few times before the tip holds still
LOCAL_LOCK_WAIT = 5.0
DEFAULT_IDENTITY = ("claude-tasks", "claude-tasks@localhost")

_DURATION = re.compile(r"(\d+)([smhd])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

T = TypeVar("T")


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
    steps_skipped: int = 0

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
    runner: str
    outcome: Outcome
    counts: Counts

    def fresh(self, now: datetime.datetime, max_age: datetime.timedelta) -> bool:
        return now - self.finished < max_age

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
        }

    @classmethod
    def from_json(cls, tier: Tier, data: Mapping[str, object]) -> "TierRun":
        return cls(
            tier,
            _moment(data, "started"),
            _moment(data, "finished"),
            _field(data, "runner", str),
            Outcome(_field(data, "outcome", str)),
            Counts.from_json(_field(data, "counts", dict)),
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

    runner: str
    token: str
    started: datetime.datetime
    expires: datetime.datetime

    @classmethod
    def take(cls, runner: str, now: datetime.datetime, ttl: datetime.timedelta) -> "Lease":
        return cls(runner, uuid.uuid4().hex, now, now + ttl)

    def live(self, now: datetime.datetime) -> bool:
        return self.expires > now

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
                _field(data, "runner", str),
                _field(data, "token", str),
                _moment(data, "started"),
                _moment(data, "expires"),
            )
        except (ValueError, TypeError):
            return None


@dataclasses.dataclass(frozen=True)
class LeaseResult:
    """The outcome of asking for the lease: ours, or the live one that is someone else's."""

    lease: Lease


class Acquired(LeaseResult):
    pass


class Held(LeaseResult):
    """Another runner's live lease, so this one stands down."""


class ExchangeUnavailable(Exception):
    """The shared copy of the state could not be read or written; a local file stands in."""


class Exchange(abc.ABC):
    """Where the run record and the lease live, as seen by one runner."""

    @abc.abstractmethod
    def record(self) -> RunRecord: ...

    @abc.abstractmethod
    def acquire(
        self, runner: str, now: datetime.datetime, ttl: datetime.timedelta
    ) -> LeaseResult: ...

    @abc.abstractmethod
    def finish(self, lease: Lease, runs: Sequence[TierRun]) -> None:
        """Record `runs` and release `lease`, if it is still ours."""


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


class LocalExchange(Exchange):
    """The state as plain files under `<queue>/.state`, for a queue with no remote.

    The lease is created with O_EXCL under the queue lock, so two local processes cannot both
    take it, and an expired one is replaced inside that same lock.
    """

    def __init__(self, root: pathlib.Path) -> None:
        self._root = root

    def _path(self, relative: str) -> pathlib.Path:
        return self._root / relative

    def record(self) -> RunRecord:
        return RunRecord.parse(_read(self._path(RECORD_PATH)))

    def acquire(self, runner: str, now: datetime.datetime, ttl: datetime.timedelta) -> LeaseResult:
        lease, path = Lease.take(runner, now, ttl), self._path(LEASE_PATH)
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

    def finish(self, lease: Lease, runs: Sequence[TierRun]) -> None:
        with require_lock(self._root, LOCAL_LOCK_WAIT):
            if runs:
                atomic_write(self._path(RECORD_PATH), self.record().with_runs(runs).to_text())
            held = Lease.parse(_read(self._path(LEASE_PATH)))
            if held is not None and held.token == lease.token:
                self._path(LEASE_PATH).unlink(missing_ok=True)


def _git(
    where: pathlib.Path, *args: str, timeout: float | None = None
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-C", str(where), *args], capture_output=True, text=True, timeout=timeout
    )


def _network(where: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """A git call that talks to origin; a hung remote is as unavailable as a refused one."""
    try:
        return _git(where, *args, timeout=NETWORK_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise ExchangeUnavailable(f"git {args[0]} timed out after {NETWORK_TIMEOUT:.0f}s") from e


def _identity(where: pathlib.Path) -> list[str]:
    """`-c` overrides for a clone with no committer configured, as a fresh cloud checkout has."""
    if _git(where, "config", "user.email").returncode == 0:
        return []
    name, email = DEFAULT_IDENTITY
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


class GitExchange(Exchange):
    """The state on origin/main, written from a scratch worktree so the queue's checkout is untouched."""

    def __init__(self, root: pathlib.Path, local: LocalExchange) -> None:
        self._root = root
        self._local = local

    def _fetch(self) -> None:
        fetched = _network(self._root, "fetch", "-q", "origin", "main")
        if fetched.returncode != 0:
            raise ExchangeUnavailable(f"git fetch failed: {fetched.stderr.strip()}")

    def _remote_text(self, relative: str) -> str:
        shown = _git(self._root, "show", f"{REMOTE_REF}:{relative}")
        return shown.stdout if shown.returncode == 0 else ""

    def record(self) -> RunRecord:
        """origin's record merged with any a run left in this checkout while origin was unreachable."""
        self._fetch()
        return RunRecord.parse(self._remote_text(RECORD_PATH)).merged(self._local.record())

    @contextlib.contextmanager
    def _tip(self) -> Iterator[pathlib.Path]:
        """A detached worktree at origin/main's tip, removed afterwards."""
        with tempfile.TemporaryDirectory(prefix="maintain-") as tmp:
            worktree = pathlib.Path(tmp) / "wt"
            added = _git(self._root, "worktree", "add", "-q", "--detach", str(worktree), REMOTE_REF)
            if added.returncode != 0:
                raise ExchangeUnavailable(f"no worktree at {REMOTE_REF}: {added.stderr.strip()}")
            try:
                yield worktree
            finally:
                _git(self._root, "worktree", "remove", "--force", str(worktree))

    def _publish(self, worktree: pathlib.Path, message: str) -> bool:
        """Commit the state change and push it as a fast-forward; False when origin moved first."""
        _git(worktree, "add", "--all", "--", STATE_DIR)
        if not _git(worktree, "status", "--porcelain").stdout.strip():
            return True
        committed = _git(worktree, *_identity(worktree), "commit", "-q", "-m", message)
        if committed.returncode != 0:
            raise ExchangeUnavailable(f"git commit failed: {committed.stderr.strip()}")
        pushed = _network(worktree, "push", "-q", "origin", "HEAD:main")
        if pushed.returncode not in (
            0,
            1,
        ):  # 1 is a rejected non-fast-forward; the rest are failures
            raise ExchangeUnavailable(f"git push failed: {pushed.stderr.strip()}")
        return pushed.returncode == 0

    def _retry(self, attempt: Callable[[pathlib.Path], T | None]) -> T:
        """Run `attempt` on a fresh origin tip until it yields a result, or origin never holds still."""
        for _ in range(PUSH_ATTEMPTS):
            self._fetch()
            with self._tip() as worktree:
                result = attempt(worktree)
            if result is not None:
                return result
        raise ExchangeUnavailable(f"origin moved {PUSH_ATTEMPTS} times while publishing")

    def acquire(self, runner: str, now: datetime.datetime, ttl: datetime.timedelta) -> LeaseResult:
        lease = Lease.take(runner, now, ttl)

        def attempt(worktree: pathlib.Path) -> LeaseResult | None:
            held = Lease.parse(_read(worktree / LEASE_PATH))
            if held is not None and held.live(now):
                return Held(held)
            (worktree / STATE_DIR).mkdir(exist_ok=True)
            (worktree / LEASE_PATH).write_text(lease.to_text())
            if not self._publish(worktree, f"maintain: {runner} leases"):
                return None
            return Acquired(lease)

        return self._retry(attempt)

    def finish(self, lease: Lease, runs: Sequence[TierRun]) -> None:
        def attempt(worktree: pathlib.Path) -> bool | None:
            record = RunRecord.parse(_read(worktree / RECORD_PATH)).with_runs(runs)
            held = Lease.parse(_read(worktree / LEASE_PATH))
            if held is not None and held.token == lease.token:
                (worktree / LEASE_PATH).unlink()
            if runs:
                (worktree / STATE_DIR).mkdir(exist_ok=True)
                (worktree / RECORD_PATH).write_text(record.to_text())
            return True if self._publish(worktree, f"maintain: {lease.runner} finished") else None

        self._retry(attempt)


class FallbackExchange(Exchange):
    """Prefers the shared exchange, and keeps working from local files once it is unreachable.

    An offline laptop still maintains its queue; the record it leaves is merged in later.
    """

    def __init__(self, shared: Exchange, local: Exchange) -> None:
        self._active, self._local = shared, local

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

    def acquire(self, runner: str, now: datetime.datetime, ttl: datetime.timedelta) -> LeaseResult:
        return self._call(lambda exchange: exchange.acquire(runner, now, ttl))

    def finish(self, lease: Lease, runs: Sequence[TierRun]) -> None:
        self._call(lambda exchange: exchange.finish(lease, runs))


def _has_origin(root: pathlib.Path) -> bool:
    return _git(root, "remote", "get-url", "origin").returncode == 0


def open_exchange(root: pathlib.Path) -> Exchange:
    """The git exchange for a queue that is its own repo with an origin, else plain local files."""
    local = LocalExchange(root)
    if owns_repo(root) and _has_origin(root):
        return FallbackExchange(GitExchange(root, local), local)
    return local
