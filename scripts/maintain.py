#!/usr/bin/env python3
"""
maintain.py: the one entry point that wakes due briefs, whatever triggers it.

  python scripts/maintain.py --tier laptop|cloud [--root PATH] [--skip-if-succeeded-within DURATION]

A tick reads its tier's last-run record and exits at once while it is younger than the tier's
interval. With `--skip-if-succeeded-within 4h` it also exits at once when origin holds a hygiene
record younger than that for every phase, which is how a cloud routine leaves the work to a laptop
that has just run it. Otherwise it finds the briefs that are due (`not_before` passed or `wake_probe` clear),
skips those its tier cannot serve (`needs:`), claims each with a git-pushed lease, hands the claimed
brief to a detached headless run through the launcher, and records the tick. A LaunchAgent, a cloud
routine or a person can call it; nothing here assumes the laptop.

Report lines go to stdout, one per brief that needs a person: `overdue`, `overdue-needs-<cap>`.
A malformed brief or a failing probe warns and is skipped. The exit status is 0 for a clean tick or one
skipped because a run is fresh or another runner holds the lease, 75 (EX_TEMPFAIL) when the judgement
phase was deferred for want of credits, and 1 when hygiene failed, lapsed or was refused or the tick crashed.

Stdlib only.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import argparse
import dataclasses
import datetime as dt
import functools
import os
import pathlib
import socket
import sys
from collections.abc import Callable, Iterator

from _dates import parse_duration
from _paths import require_queue, resolve_root, warn
from claim import IN_PROGRESS, Claim, Claimer, Lease, Target
from deps import Brief, brief_id, done_ids, fm, unmet
from headless import Launch, ScriptLaunch, launcher_script, rank_table, spawn_detached
from hygiene import Engine, Hygiene, HygieneResult, succeeded_within
from hygiene_lease import HygieneLeaser
from maintain_config import MaintainSettings, Tier
from maintain_report import Report, ReportKind
from maintain_state import LastRun, RunLedger
from picks import state_dir
from wake import Due, MalformedWake, ProbeRunner, Reading, Wake

WAKE_FOLDERS = ("ready", "inbox")
CALLER = "claude-tasks-maintain"
ACTION_SKILL = "/claude-tasks:action-task"
SPAWN_REASON = "scheduled-wake"
RUN_LOG_DIR = "maintain-runs"
EX_TEMPFAIL = 75  # sysexits.h: skipped, the caller should retry later
EX_FAILED = 1
FAILING_REPORTS = frozenset(
    {ReportKind.HYGIENE_FAILED, ReportKind.HYGIENE_LAPSED, ReportKind.HYGIENE_REFUSED}
)

Clock = Callable[[], dt.datetime]
Spawn = Callable[..., int]
Probe = Callable[[str], Reading]


@dataclasses.dataclass(frozen=True)
class Candidate:
    """A brief this tier may claim now."""

    target: Target
    wake: Wake


@dataclasses.dataclass(frozen=True)
class TickResult:
    ran: bool
    claimed: tuple[str, ...] = ()
    reports: tuple[Report, ...] = ()
    deferred: tuple[str, ...] = ()

    @property
    def exit_status(self) -> int:
        """What a scheduler sees: failed, skipped to retry later, or fine (including skipped as fresh)."""
        if any(r.kind in FAILING_REPORTS for r in self.reports):
            return EX_FAILED
        return EX_TEMPFAIL if self.deferred else 0


def default_holder(tier: Tier) -> str:
    """Who a lease names: the tier, this host and this process."""
    return f"{tier.value}@{socket.gethostname().split('.')[0]}:{os.getpid()}"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclasses.dataclass
class Scanner:
    """Finds the briefs a tick may claim and the ones it must report instead."""

    root: pathlib.Path
    settings: MaintainSettings
    tier: Tier
    probe: Probe
    reports: list[Report] = dataclasses.field(default_factory=list)

    def candidates(self, now: dt.datetime) -> Iterator[Candidate]:
        for folder in (*WAKE_FOLDERS, IN_PROGRESS):
            for path in sorted((self.root / folder).glob("*.md")):
                yield from self._candidate(folder, path, now)

    def _candidate(self, folder: str, path: pathlib.Path, now: dt.datetime) -> Iterator[Candidate]:
        try:
            text = path.read_text()
            ident = brief_id(fm(text), path.stem)
            wake = Wake.parse(ident, text)
            if wake is not None and self._claimable(
                Brief(ident, folder, fm(text)), text, wake, now
            ):
                yield Candidate(Target(ident, folder, wake.key), wake)
        except (OSError, MalformedWake) as e:
            warn(f"skipping {path.name}: {e}", "maintain")

    @functools.cached_property
    def _done(self) -> set[str]:
        try:
            return done_ids(self.root)
        except OSError as e:
            warn(
                f"cannot read the queue ({e}); id dependencies count as unmet this tick", "maintain"
            )
            return set()

    def _claimable(self, brief: Brief, text: str, wake: Wake, now: dt.datetime) -> bool:
        if wake.lapsed(now):
            self.reports.append(
                Report(ReportKind.OVERDUE, wake.brief_id, "past valid_until; not run")
            )
            return False
        if not self._served(wake, now):
            return False
        if brief.status == IN_PROGRESS:
            return self._reclaimable(text, wake, now)
        return (
            wake.unattended
            and not unmet(brief, self._done, now.date())
            and not RunLedger(self.root).has(wake.key)
            and wake.due(now, self.probe) is Due.DUE
        )

    def _served(self, wake: Wake, now: dt.datetime) -> bool:
        """False when this tier lacks a capability the brief needs; the report asks for it once `escalate_after` passes."""
        missing = wake.missing(self.settings.tiers[self.tier].capabilities)
        if missing and wake.escalated(now):
            self.reports.extend(
                Report(ReportKind.OVERDUE_NEEDS, wake.brief_id, f"needs {cap}", cap)
                for cap in missing
            )
        return not missing

    def _reclaimable(self, text: str, wake: Wake, now: dt.datetime) -> bool:
        lease = Lease.from_fields(fm(text))
        if lease is None or lease.live(now, self.settings.lease_ttl):
            return False
        if lease.attempts >= self.settings.max_attempts:
            self.reports.append(
                Report(
                    ReportKind.EXHAUSTED,
                    wake.brief_id,
                    f"{lease.attempts} attempts, last held by {lease.holder}",
                )
            )
            return False
        return True


@dataclasses.dataclass
class Maintainer:
    """One tier's tick over one queue."""

    root: pathlib.Path
    tier: Tier
    settings: MaintainSettings
    holder: str = ""
    clock: Clock = now_utc
    spawn: Spawn = spawn_detached
    probe: Probe | None = None
    claimer: Claimer | None = None
    launch: Launch | None = None
    engine: Engine | None = None
    skip_if_succeeded_within: dt.timedelta | None = None

    def tick(self) -> TickResult:
        """Run one tick, wake step then hygiene; returns at once while the tier's last run is fresh or a success on origin is recent enough."""
        now = self.clock()
        interval = self.settings.tiers[self.tier].interval
        last = LastRun.read(self.root, self.tier.value)
        if last is not None and last.fresh(now, interval):
            return TickResult(ran=False)
        if self._succeeded_recently(now):
            return TickResult(ran=False)
        result = self._work(now)
        try:
            result = self._with_hygiene(result, self._hygiene(now))
        finally:
            record = LastRun(self.tier.value, now, result.claimed, tuple(map(str, result.reports)))
            record.write(self.root, self.clock())
        return result

    def _succeeded_recently(self, now: dt.datetime) -> bool:
        """Whether origin, not only this checkout, holds a recent success; a failed refresh reads the checkout."""
        window = self.skip_if_succeeded_within
        if window is None:
            return False
        if not HygieneLeaser(self._claimer()).refresh():
            warn(
                "could not bring the queue up to date with origin; reading the checkout", "maintain"
            )
        return succeeded_within(self.root, now, window)

    @staticmethod
    def _with_hygiene(result: TickResult, hygiene: HygieneResult) -> TickResult:
        return dataclasses.replace(
            result,
            reports=(*result.reports, *hygiene.reports),
            deferred=tuple(p.value for p in hygiene.deferred),
        )

    def _claimer(self) -> Claimer:
        return self.claimer or Claimer(
            self.root, self.holder or default_holder(self.tier), self.settings.lease_ttl
        )

    def _hygiene(self, now: dt.datetime) -> HygieneResult:
        """The tidy-up steps after the wake step, under their own lease; a crash there never unrecords the wake step."""
        engine = self.engine or Engine.for_queue(self.root, self.clock)
        leaser = HygieneLeaser(self._claimer())
        return Hygiene(self.root, self.settings.hygiene_interval, engine, leaser).run(now)

    def _work(self, now: dt.datetime) -> TickResult:
        launch = self.launch or _launcher(self.root)
        if launch is None:
            warn(
                "no launcher is configured ([rank] launcher or $CLAUDE_TASKS_LAUNCHER); nothing can be run",
                "maintain",
            )
            return TickResult(ran=True)
        claimer = self._claimer()
        if not claimer.refresh():
            warn(
                "could not bring the queue up to date with origin; working from the checkout as it is",
                "maintain",
            )
        scanner = Scanner(self.root, self.settings, self.tier, self.probe or ProbeRunner(self.root))
        claimed, reports = self._claim_and_run(claimer, launch, list(scanner.candidates(now)), now)
        return TickResult(True, tuple(claimed), (*scanner.reports, *reports))

    def _claim_and_run(
        self, claimer: Claimer, launch: Launch, candidates: list[Candidate], now: dt.datetime
    ) -> tuple[list[str], list[Report]]:
        claimed: list[str] = []
        reports: list[Report] = []
        for candidate in candidates:
            if len(claimed) >= self.settings.max_claims:
                break
            claim = claimer.claim(candidate.target, now)
            report = self._after_claim(candidate, claim, launch)
            reports.append(report)
            if claim.won:
                claimed.append(candidate.target.brief_id)
        return claimed, [r for r in reports if r.kind is not ReportKind.CLAIMED]

    def _after_claim(self, candidate: Candidate, claim: Claim, launch: Launch) -> Report:
        ident = candidate.target.brief_id
        if not claim.won:
            return Report(ReportKind.NOT_CLAIMED, ident, claim.detail)
        try:
            self._spawn(candidate, launch)
        except OSError as e:
            return Report(
                ReportKind.SPAWN_FAILED,
                ident,
                f"{type(e).__name__}: {e}; the lease will lapse and it will be retried",
            )
        return Report(ReportKind.CLAIMED, ident, claim.detail)

    def _spawn(self, candidate: Candidate, launch: Launch) -> int:
        """Start the headless run for a published claim; the child outlives this tick."""
        ident = candidate.target.brief_id
        log = state_dir(self.root) / RUN_LOG_DIR / f"{candidate.target.key.slug}.log"
        env = {"CLAUDE_TASKS_DIR": str(self.root), "CLAUDE_ANY_CALLER": CALLER}
        args = (*self.settings.run_args, f"{ACTION_SKILL} {ident}\n\n{_context(candidate)}")
        return self.spawn(launch, args, SPAWN_REASON, log, self.root, env)


def _context(candidate: Candidate) -> str:
    return (
        f"This run was started by `maintain` because the wake {candidate.target.key} of brief "
        f"{candidate.target.brief_id} is due; the brief is already claimed and sits in in-progress/. "
        "Do the brief, then move it to done/ (or back with a log line saying what is left)."
    )


def _launcher(root: pathlib.Path) -> Launch | None:
    """The configured launcher script; the unattended run never falls back to bare `claude -p`."""
    script = launcher_script(rank_table(root).get("launcher"))
    return ScriptLaunch(str(script)) if script else None


def _duration(text: str) -> dt.timedelta:
    try:
        return parse_duration(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wake the briefs that are due, then record the tick.")
    ap.add_argument("--tier", required=True, choices=[t.value for t in Tier])
    ap.add_argument("--root", help="queue directory (default: the resolved queue)")
    ap.add_argument(
        "--skip-if-succeeded-within",
        type=_duration,
        metavar="DURATION",
        help="exit 0 with nothing run when every hygiene phase was recorded on origin within this "
        "span, such as 30m, 4h or 2d",
    )
    a = ap.parse_args(argv)
    root = require_queue(pathlib.Path(a.root) if a.root else resolve_root())
    try:
        maintainer = Maintainer(
            root,
            Tier(a.tier),
            MaintainSettings.load(root),
            skip_if_succeeded_within=a.skip_if_succeeded_within,
        )
        result = maintainer.tick()
    except Exception as e:  # noqa: BLE001  - a scheduled tick reports and retries next time; it never crashes
        warn(f"tick failed: {type(e).__name__}: {e}", "maintain")
        return EX_FAILED
    for report in result.reports:
        print(report)
    return result.exit_status


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
