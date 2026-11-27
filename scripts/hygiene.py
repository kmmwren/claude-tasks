#!/usr/bin/env python3
"""
hygiene.py: the queue's tidy-up steps, run after a tick's wake step under a hygiene lease.

Steps belong to one of two phases. The deterministic phase needs no Claude credits and always runs;
the judgement phase runs only while an account has capacity, otherwise it is deferred untouched and
the next tick retries. A phase is recorded, in the same commit that releases the lease, only when
every one of its steps succeeded; a failed or lapsed phase is simply due again.

Progress goes to stderr; stdout stays the tick's report lines. Stdlib only.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import contextlib
import dataclasses
import datetime as dt
import enum
import os
import pathlib
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

from _paths import warn
from claim import Claim, Lease, Outcome
from hygiene_lease import HygieneLeaser
from maintain_capacity import has_capacity, switch_home
from maintain_report import Report, ReportKind
from maintain_state import LastRun

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
COORDINATOR_ENV = "CLAUDE_TASKS_COORDINATOR_DIR"
COORDINATOR_DIR = "~/.claude/tools/project-coordinator"
GENERIC_QUEUE_ENV = "WORK_TASKS_DIR"
GENERIC_QUEUE = "~/work-tasks"
TIMED_OUT = 124  # the exit status `timeout(1)` uses, and no step treats it as success
SKIPPED_PREFIX = "skipped "
SUBJECT = "hygiene"

Clock = Callable[[], dt.datetime]


def say(message: str) -> None:
    print(message, file=sys.stderr)


class Phase(enum.Enum):
    """A group of steps with its own freshness; the order here is the order they run."""

    DETERMINISTIC = "deterministic"  # no Claude calls, so it needs no credits
    JUDGEMENT = "judgement"  # calls headless Claude

    @property
    def needs_capacity(self) -> bool:
        return self is Phase.JUDGEMENT


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
    phase: Phase
    home: Home
    script: str
    args: tuple[str, ...] = ()
    ok_codes: frozenset[int] = frozenset({0})

    def command(self, where: Environment) -> list[str] | None:
        path = self.home.locate(self.script, where)
        return None if path is None else [sys.executable, str(path), *self.args]


# One line per step, in the order they run. A new step is a new line here.
STEPS: tuple[Step, ...] = (
    Step("unblock", Phase.DETERMINISTIC, Home.PLUGIN, "unblock.py"),
    Step(
        "dep-check", Phase.DETERMINISTIC, Home.PLUGIN, "check_deps.py", ok_codes=frozenset({0, 1})
    ),
    Step(
        "blocker-recheck",
        Phase.DETERMINISTIC,
        Home.COORDINATOR,
        "blocker_recheck.py",
        ("run", "--all"),
    ),
    Step("route", Phase.DETERMINISTIC, Home.COORDINATOR, "router.py", ("tick", "--no-judge")),
    Step("project-hygiene", Phase.DETERMINISTIC, Home.COORDINATOR, "coordinator.py", ("hygiene",)),
    Step(
        "triage-mechanical", Phase.DETERMINISTIC, Home.PLUGIN, "triage.py", ("promote", "--apply")
    ),
    Step("board", Phase.DETERMINISTIC, Home.PLUGIN, "build_board.py"),
    Step(
        "triage-judge", Phase.JUDGEMENT, Home.PLUGIN, "triage.py", ("promote", "--apply", "--llm")
    ),
    Step("board-after-judge", Phase.JUDGEMENT, Home.PLUGIN, "build_board.py"),
)

StepRunner = Callable[[Sequence[str], Mapping[str, str], float], int]


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    """Kill the step and everything it started, which share the process group it leads."""
    with contextlib.suppress(ProcessLookupError):  # it exited between the poll and the kill
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def subprocess_runner(argv: Sequence[str], env: Mapping[str, str], timeout: float) -> int:
    """Run a step in its own session, so a timeout (or an interrupt) takes its children down with it."""
    proc = subprocess.Popen(argv, env=dict(env), start_new_session=True)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return TIMED_OUT
    finally:
        if proc.poll() is None:
            _kill_group(proc)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclasses.dataclass(frozen=True)
class Engine:
    """What a run touches: the machine's environment, how steps execute, and what time it is."""

    where: Environment
    steps: Sequence[Step]
    runner: StepRunner = subprocess_runner
    clock: Clock = utc_now

    @classmethod
    def for_queue(cls, root: pathlib.Path, clock: Clock = utc_now) -> "Engine":
        """The production wiring: every step in `STEPS`, run as subprocesses on this machine."""
        return cls(Environment.from_process(root), STEPS, clock=clock)

    def steps_of(self, phase: Phase) -> list[Step]:
        return [s for s in self.steps if s.phase is phase]

    def can_run_skipped(self, phase: Phase, skipped: Sequence[str]) -> bool:
        """Whether a step an earlier run skipped has its script here, so this environment can finish the job."""
        return any(
            s.name in skipped and s.command(self.where) is not None for s in self.steps_of(phase)
        )

    def child_env(self) -> dict[str, str]:
        """Every step resolves the queue from the environment, so they all act on the same one."""
        return {**os.environ, "CLAUDE_TASKS_DIR": str(self.where.root)}


class StepOutcome(enum.Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    LAPSED = "lapsed"  # the lease had expired, so the step was not started


class PhaseOutcome(enum.Enum):
    OK = "ok"
    FAILED = "failed"
    LAPSED = "lease lapsed"  # the run outlived its lease, so another runner may hold the queue


def _execute(step: Step, argv: list[str], engine: Engine, remaining: float) -> StepOutcome:
    code = engine.runner(argv, engine.child_env(), remaining)
    if code in step.ok_codes:
        say(f"  {step.name}: ok")
        return StepOutcome.OK
    say(f"  {step.name}: failed (exit {code})")
    return StepOutcome.FAILED


def _run_step(step: Step, engine: Engine, lease: Lease) -> StepOutcome:
    """Run one step within the lease; once the lease has lapsed nothing more starts."""
    now = engine.clock()
    if lease.expired(now):
        say(f"  {step.name}: not started (the lease expired)")
        return StepOutcome.LAPSED
    argv = step.command(engine.where)
    if argv is None:
        say(f"  {step.name}: skipped ({step.script} is not available here)")
        return StepOutcome.SKIPPED
    return _execute(step, argv, engine, (lease.expires - now).total_seconds())


def _run_steps(
    steps: Sequence[Step], engine: Engine, lease: Lease
) -> list[tuple[Step, StepOutcome]]:
    """Every step in order, even after a failure; a lapsed lease ends the run."""
    done: list[tuple[Step, StepOutcome]] = []
    for step in steps:
        done.append((step, _run_step(step, engine, lease)))
        if done[-1][1] is StepOutcome.LAPSED:
            break
    return done


def _phase_outcome(outcomes: Sequence[StepOutcome]) -> PhaseOutcome:
    if StepOutcome.LAPSED in outcomes:
        return PhaseOutcome.LAPSED
    return PhaseOutcome.FAILED if StepOutcome.FAILED in outcomes else PhaseOutcome.OK


@dataclasses.dataclass(frozen=True)
class PhaseRun:
    """What one phase did: its outcome and counts come from the steps it ran."""

    phase: Phase
    outcome: PhaseOutcome
    steps_ok: int
    steps_failed: int
    skipped: tuple[str, ...]

    @property
    def recordable(self) -> bool:
        """Only a phase whose every step succeeded counts as a run another trigger may rely on."""
        return self.outcome is PhaseOutcome.OK

    def last_run(self, at: dt.datetime) -> LastRun:
        """The record that makes this phase fresh; skipped steps are kept so a better-equipped runner can finish."""
        return LastRun(
            self.phase.value, at, (), tuple(f"{SKIPPED_PREFIX}{n}" for n in self.skipped)
        )

    def summary(self) -> str:
        return (
            f"{self.outcome.value} ({self.steps_ok} ok, {self.steps_failed} failed, "
            f"{len(self.skipped)} skipped)"
        )


def run_phase(phase: Phase, engine: Engine, lease: Lease) -> PhaseRun:
    """Every step of `phase`, its outcome and counts taken from what the steps did."""
    say(f"{phase.value}: running")
    done = _run_steps(engine.steps_of(phase), engine, lease)
    outcomes = [outcome for _, outcome in done]
    skipped = tuple(step.name for step, outcome in done if outcome is StepOutcome.SKIPPED)
    return PhaseRun(
        phase,
        _phase_outcome(outcomes),
        outcomes.count(StepOutcome.OK),
        outcomes.count(StepOutcome.FAILED),
        skipped,
    )


@dataclasses.dataclass(frozen=True)
class HygieneResult:
    """What a hygiene run leaves for the tick: the reports, and the phases deferred for want of credits."""

    reports: tuple[Report, ...] = ()
    deferred: tuple[Phase, ...] = ()


def skipped_in(last: LastRun) -> tuple[str, ...]:
    return tuple(
        r.removeprefix(SKIPPED_PREFIX) for r in last.reports if r.startswith(SKIPPED_PREFIX)
    )


@dataclasses.dataclass(frozen=True)
class Hygiene:
    """The tidy-up of one queue: what is due, whether credits allow it, and the lease it runs under."""

    root: pathlib.Path
    interval: dt.timedelta
    engine: Engine
    leaser: HygieneLeaser

    def run(self, now: dt.datetime) -> HygieneResult:
        """Run the due phases that can run now; the reports name what needs a person.

        What is due is read again once the queue is current and again once the lease is held, so a
        phase another runner has just recorded is not run twice.
        """
        if not self._due(now):
            return HygieneResult()
        self._refresh()
        runnable, deferred = self._split_by_capacity(self._due(now), now)
        if not runnable:
            return HygieneResult((), deferred)
        claim = self.leaser.acquire(now)
        if claim.lease is None:
            return HygieneResult(self._not_taken(claim), deferred)
        self._refresh()
        still_due = self._due(now)
        reports = self._under_lease([p for p in runnable if p in still_due], claim.lease)
        return HygieneResult(reports, deferred)

    def _refresh(self) -> None:
        if not self.leaser.refresh():
            warn("could not bring the queue up to date with origin; working as it is", "maintain")

    def _due(self, now: dt.datetime) -> list[Phase]:
        return [p for p in Phase if self.engine.steps_of(p) and self._is_due(p, now)]

    def _is_due(self, phase: Phase, now: dt.datetime) -> bool:
        """No run recent enough, or the last one skipped a step this environment can run."""
        last = LastRun.read(self.root, phase.value)
        if last is None or not last.fresh(now, self.interval):
            return True
        return self.engine.can_run_skipped(phase, skipped_in(last))

    def _split_by_capacity(
        self, due: Sequence[Phase], now: dt.datetime
    ) -> tuple[list[Phase], tuple[Phase, ...]]:
        """The due phases that can run now, and the credit-hungry ones deferred untouched for want of credits."""
        usable = not any(p.needs_capacity for p in due) or has_capacity(
            self.engine.where.switch_home, now
        )
        deferred = tuple(p for p in due if p.needs_capacity and not usable)
        for phase in deferred:
            say(f"{phase.value}: deferred, no capacity")
        return [p for p in due if p not in deferred], deferred

    def _not_taken(self, claim: Claim) -> tuple[Report, ...]:
        if claim.outcome is Outcome.REFUSED:
            say(f"ERROR: hygiene lease not taken, {claim.detail}; standing down")
            return (Report(ReportKind.HYGIENE_REFUSED, SUBJECT, claim.detail),)
        say(f"hygiene not run: {claim.detail}")
        return ()

    def _under_lease(self, runnable: Sequence[Phase], lease: Lease) -> tuple[Report, ...]:
        runs: list[PhaseRun] = []
        try:
            self._run_phases(runnable, lease, runs)
        finally:
            settled = self._settle(lease, runs)
        failed = (
            Report(ReportKind.HYGIENE_FAILED, r.phase.value, r.summary())
            for r in runs
            if r.outcome is PhaseOutcome.FAILED
        )
        return (*settled, *failed)

    def _run_phases(self, runnable: Sequence[Phase], lease: Lease, runs: list[PhaseRun]) -> None:
        """Run each phase in turn, adding each result as it completes so a later crash cannot lose it."""
        for phase in runnable:
            runs.append(run_phase(phase, self.engine, lease))
            if runs[-1].outcome is PhaseOutcome.LAPSED:
                break

    def _settle(self, lease: Lease, runs: Sequence[PhaseRun]) -> tuple[Report, ...]:
        """Record the successful phases and release the lease, unless it lapsed: it may be another runner's now."""
        now = self.engine.clock()
        if lease.expired(now):
            say(f"ERROR: hygiene lease expired at {lease.expires:%H:%M}Z; this run is not recorded")
            return (Report(ReportKind.HYGIENE_LAPSED, SUBJECT, "lease expired; not recorded"),)
        records = [r.last_run(now) for r in runs if r.recordable]
        released = self.leaser.release(lease, records)
        if released.won:
            self.leaser.refresh()
            return ()
        return self._not_released(released, lease)

    def _not_released(self, released: Claim, lease: Lease) -> tuple[Report, ...]:
        if released.outcome is Outcome.REFUSED:
            say(f"ERROR: hygiene results not published, {released.detail}")
            return (Report(ReportKind.HYGIENE_REFUSED, SUBJECT, released.detail),)
        warn(
            f"hygiene results not published ({released.detail}); any lease of ours stays on origin "
            f"until it lapses at {lease.expires:%H:%M}Z, and the phases run again after that",
            "maintain",
        )
        return ()
