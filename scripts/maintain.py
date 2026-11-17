#!/usr/bin/env python3
"""
maintain.py: the one entry point that keeps a queue tidy, whatever triggers it.

    maintain.py                          # run both tiers now
    maintain.py --if-older-than 4h       # a trigger that costs nothing when a run is recent enough
    maintain.py --runner cloud --dry-run # print what would run, change nothing

Any timer, cloud routine or hook may call it. The last successful run of each tier is recorded in
the queue's git repo (`maintain_state`), so a redundant trigger sees the record and exits after a
fetch; a lease taken the same way keeps two runners from working at once. The deterministic tier
makes no Claude calls and always runs; the judgement tier calls headless Claude, and is deferred
untouched when no account has credits, so the next trigger retries it.

Stdlib only.
"""

import argparse
import dataclasses
import datetime
import enum
import os
import pathlib
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

from _paths import require_queue, resolve_root
from maintain_capacity import has_capacity, switch_home
from maintain_state import (
    Counts,
    Exchange,
    Held,
    Outcome,
    RunRecord,
    Tier,
    TierRun,
    open_exchange,
    parse_duration,
)

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
COORDINATOR_ENV = "CLAUDE_TASKS_COORDINATOR_DIR"
COORDINATOR_DIR = "~/.claude/tools/project-coordinator"
GENERIC_QUEUE_ENV = "WORK_TASKS_DIR"
GENERIC_QUEUE = "~/work-tasks"
DEFAULT_LEASE_TTL = datetime.timedelta(minutes=30)
TIMED_OUT = 124  # the exit status `timeout(1)` uses, and no step treats it as success


@dataclasses.dataclass(frozen=True)
class Environment:
    """Where the queue and the optional personal tooling live on this machine."""

    root: pathlib.Path
    coordinator_dir: pathlib.Path
    generic_queue: pathlib.Path
    switch_home: pathlib.Path

    @classmethod
    def from_process(
        cls, root: pathlib.Path, env: Mapping[str, str] | None = None
    ) -> "Environment":
        env = os.environ if env is None else env
        return cls(
            root,
            pathlib.Path(env.get(COORDINATOR_ENV) or COORDINATOR_DIR).expanduser(),
            pathlib.Path(env.get(GENERIC_QUEUE_ENV) or GENERIC_QUEUE).expanduser(),
            switch_home(env),
        )

    def is_generic_queue(self) -> bool:
        return self.root.resolve() == self.generic_queue.resolve()


class Home(enum.Enum):
    """Which tree a step's script lives in."""

    PLUGIN = "plugin"
    COORDINATOR = "coordinator"  # personal tooling: on the laptop, and only for the generic queue

    def locate(self, script: str, where: Environment) -> pathlib.Path | None:
        """The script's path, or None when it is not available for this queue on this machine."""
        if self is Home.PLUGIN:
            return SCRIPTS_DIR / script
        if not where.is_generic_queue():
            return None  # its registry routes the generic queue's briefs, not a project queue's
        path = where.coordinator_dir / script
        return path if path.is_file() else None


@dataclasses.dataclass(frozen=True)
class Step:
    """One existing script run against the queue; `ok_codes` are the exit statuses that are not failures."""

    name: str
    tier: Tier
    home: Home
    script: str
    args: tuple[str, ...] = ()
    ok_codes: frozenset[int] = frozenset({0})

    def command(self, where: Environment) -> list[str] | None:
        path = self.home.locate(self.script, where)
        return None if path is None else [sys.executable, str(path), *self.args]

    def shown(self) -> str:
        return " ".join([self.script, *self.args])


# One line per step, in the order they run. A new step is a new line here.
STEPS: tuple[Step, ...] = (
    Step("unblock", Tier.DETERMINISTIC, Home.PLUGIN, "unblock.py"),
    Step("dep-check", Tier.DETERMINISTIC, Home.PLUGIN, "check_deps.py", ok_codes=frozenset({0, 1})),
    Step(
        "blocker-recheck",
        Tier.DETERMINISTIC,
        Home.COORDINATOR,
        "blocker_recheck.py",
        ("run", "--all"),
    ),
    Step("route", Tier.DETERMINISTIC, Home.COORDINATOR, "router.py", ("tick", "--no-judge")),
    Step("triage-mechanical", Tier.DETERMINISTIC, Home.PLUGIN, "triage.py", ("promote", "--apply")),
    Step("board", Tier.DETERMINISTIC, Home.PLUGIN, "build_board.py"),
    Step("triage-judge", Tier.JUDGEMENT, Home.PLUGIN, "triage.py", ("promote", "--apply", "--llm")),
    Step("board-after-judge", Tier.JUDGEMENT, Home.PLUGIN, "build_board.py"),
)

StepRunner = Callable[[Sequence[str], Mapping[str, str], float], int]
Clock = Callable[[], datetime.datetime]


def subprocess_runner(argv: Sequence[str], env: Mapping[str, str], timeout: float) -> int:
    try:
        return subprocess.run(argv, env=dict(env), timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        return TIMED_OUT


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@dataclasses.dataclass(frozen=True)
class Engine:
    """What a run touches: the machine's environment, how steps execute, and what time it is."""

    where: Environment
    runner: StepRunner = subprocess_runner
    clock: Clock = utc_now
    steps: Sequence[Step] = STEPS

    def steps_of(self, tier: Tier) -> list[Step]:
        return [s for s in self.steps if s.tier is tier]

    def child_env(self) -> dict[str, str]:
        """Every step resolves the queue from the environment, so they all act on the same one."""
        return {**os.environ, "CLAUDE_TASKS_DIR": str(self.where.root)}


class StepOutcome(enum.Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


def _run_step(step: Step, engine: Engine, deadline: datetime.datetime) -> StepOutcome:
    """Run one step within the lease; a step that cannot start before it expires is failed, not run."""
    argv = step.command(engine.where)
    if argv is None:
        print(f"  {step.name}: skipped ({step.script} is not available here)")
        return StepOutcome.SKIPPED
    remaining = (deadline - engine.clock()).total_seconds()
    if remaining <= 0:
        print(f"  {step.name}: failed (the lease expired before it started)")
        return StepOutcome.FAILED
    code = engine.runner(argv, engine.child_env(), remaining)
    if code in step.ok_codes:
        print(f"  {step.name}: ok")
        return StepOutcome.OK
    print(f"  {step.name}: failed (exit {code})")
    return StepOutcome.FAILED


@dataclasses.dataclass(frozen=True)
class TierResult:
    tier: Tier
    outcome: Outcome
    counts: Counts
    run: TierRun | None  # set only for a run worth recording

    def summary(self) -> str:
        c = self.counts
        return (
            f"{self.tier.value}: {self.outcome.value} "
            f"({c.steps_ok} ok, {c.steps_failed} failed, {c.steps_skipped} skipped)"
        )


def run_tier(tier: Tier, engine: Engine, runner: str, deadline: datetime.datetime) -> TierResult:
    """Every step of `tier` in order; a failed step does not stop the rest, but the tier is not recorded."""
    started = engine.clock()
    print(f"{tier.value}: running")
    outcomes = [_run_step(step, engine, deadline) for step in engine.steps_of(tier)]
    counts = Counts(
        steps_ok=outcomes.count(StepOutcome.OK),
        steps_failed=outcomes.count(StepOutcome.FAILED),
        steps_skipped=outcomes.count(StepOutcome.SKIPPED),
    )
    if counts.steps_failed:
        return TierResult(tier, Outcome.FAILED, counts, None)
    run = TierRun(tier, started, engine.clock(), runner, Outcome.OK, counts)
    return TierResult(tier, Outcome.OK, counts, run)


@dataclasses.dataclass(frozen=True)
class Request:
    runner: str
    max_age: datetime.timedelta | None
    lease_ttl: datetime.timedelta
    dry_run: bool


def _stale(
    record: RunRecord, now: datetime.datetime, max_age: datetime.timedelta | None
) -> list[Tier]:
    """Tiers with no successful run recent enough; with no `max_age` every tier is due."""
    return [
        tier
        for tier in Tier
        if max_age is None or (last := record.last(tier)) is None or not last.fresh(now, max_age)
    ]


def _announce_fresh(record: RunRecord, tiers: Sequence[Tier], now: datetime.datetime) -> None:
    for tier in tiers:
        last = record.last(tier)
        if last is not None:
            print(f"{tier.value}: fresh, {last.describe(now)}")


def _with_capacity(due: Sequence[Tier], engine: Engine) -> list[Tier]:
    """The due tiers that can run now; a credit-hungry tier with none to spend is deferred untouched."""
    usable = has_capacity(engine.where.switch_home, engine.clock())
    runnable = [t for t in due if usable or not t.needs_capacity]
    for tier in (t for t in due if t not in runnable):
        print(f"{tier.value}: {Outcome.DEFERRED.value}")
    return runnable


def _print_plan(tiers: Sequence[Tier], engine: Engine) -> None:
    for tier in tiers:
        print(f"{tier.value}: would run")
        for step in engine.steps_of(tier):
            available = step.command(engine.where) is not None
            note = step.shown() if available else f"skipped ({step.script} is not available here)"
            print(f"  {step.name}: {note}")


def _work(
    request: Request,
    engine: Engine,
    exchange: Exchange,
    runnable: Sequence[Tier],
    deadline: datetime.datetime,
) -> list[TierResult]:
    """Run the tiers still due once the lease is ours; a runner that finished first makes them fresh."""
    now, record = engine.clock(), exchange.record()
    due = _stale(record, now, request.max_age)
    _announce_fresh(record, [t for t in runnable if t not in due], now)
    return [run_tier(t, engine, request.runner, deadline) for t in runnable if t in due]


def maintain(request: Request, engine: Engine, exchange: Exchange) -> int:
    """Exit status 1 when a tier failed; being fresh, leased elsewhere or deferred are all 0."""
    now, record = engine.clock(), exchange.record()
    due = _stale(record, now, request.max_age)
    _announce_fresh(record, [t for t in Tier if t not in due], now)
    runnable = _with_capacity(due, engine)
    if request.dry_run:
        _print_plan(runnable, engine)
        return 0
    if not runnable:
        return 0
    leased = exchange.acquire(request.runner, now, request.lease_ttl)
    if isinstance(leased, Held):
        print(f"leased by {leased.lease.runner} until {leased.lease.expires:%H:%M}Z")
        return 0
    results: list[TierResult] = []
    try:
        results = _work(request, engine, exchange, runnable, leased.lease.expires)
    finally:
        exchange.finish(leased.lease, [r.run for r in results if r.run is not None])
    for result in results:
        print(result.summary())
    return int(any(r.outcome is Outcome.FAILED for r in results))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run a queue's maintenance tiers once, without racing another runner."
    )
    ap.add_argument("--root", type=pathlib.Path, help="the queue (default: the resolved queue)")
    ap.add_argument(
        "--runner", default=socket.gethostname(), help="free-text label, e.g. laptop or cloud"
    )
    ap.add_argument(
        "--if-older-than",
        type=parse_duration,
        metavar="DURATION",
        help="skip a tier whose last successful run is newer than this, e.g. 4h",
    )
    ap.add_argument(
        "--lease-ttl",
        type=parse_duration,
        default=DEFAULT_LEASE_TTL,
        metavar="DURATION",
        help="how long a lease holds before another runner may take it over (default 30m)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print the steps that would run and change nothing"
    )
    args = ap.parse_args(argv)
    root = require_queue(args.root or resolve_root())
    request = Request(args.runner, args.if_older_than, args.lease_ttl, args.dry_run)
    return maintain(request, Engine(Environment.from_process(root)), open_exchange(root))


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover
