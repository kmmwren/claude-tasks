"""The roll-up model: typed sections computed from one queue snapshot, each stamped with its commit.

Every fact comes from the queue as its GitHub default branch holds it (see queue_snapshot), so the
same snapshot always yields the same model. Nothing here reads a clone, a PR, Jira or a session.
"""

import dataclasses
import datetime as dt
import enum
import pathlib
import zoneinfo
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

import facts
import project_view as pv
from _dates import parse_date
from _paths import QueueBriefs, known_roots, warn
from config import load_config_or_defaults
from deps import done_ids, load_queue, unmet
from milestones import Milestone
from prioritise import EFFORT_DAYS
from queue_snapshot import QueueSnapshot

SHIPPED_DAYS = 14
# A done brief of these kinds or tags records a conversation, not delivered work.
CONVERSATION_KINDS = frozenset({"question"})
CONVERSATION_TAGS = frozenset({"peer"})
WORD = {**pv.LABEL, "": "Not set by owner"}
SENTENCE_WORD = {**{k: v.lower() for k, v in pv.LABEL.items()}, "": "not yet rated by its owner"}


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


@dataclasses.dataclass(frozen=True)
class AsAt:
    """The commit a section was computed from; `stale` marks a cached copy used while offline."""

    sha: str
    committed: dt.datetime
    stale: bool

    @classmethod
    def of(cls, snapshot: QueueSnapshot) -> "AsAt":
        return cls(snapshot.sha, snapshot.committed, snapshot.stale)

    def table(self) -> dict[str, Any]:
        stamp = self.committed.isoformat(timespec="seconds")
        return {"sha": self.sha, "committed": stamp, "stale": self.stale}


class Stage(enum.Enum):
    NOW = "Now"
    NEXT = "Next"
    DONE = "Done"

    @property
    def key(self) -> str:
        return self.name.lower()

    @classmethod
    def of(cls, status: str) -> "Stage":
        return _STAGE_OF_STATUS[status]


_STAGE_OF_STATUS = {
    "done": Stage.DONE,
    "active": Stage.NOW,
    "not-started": Stage.NEXT,
    "unmapped": Stage.NEXT,
}


# --- Layer 3: briefs -----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BriefSummary:
    id: str
    title: str
    state: str
    waiting: bool
    updated: dt.date | None
    due: dt.date | None
    effort: str
    kind: str
    tags: frozenset[str]
    blockers: tuple[str, ...]
    as_at: AsAt

    @classmethod
    def of(cls, b: dict[str, Any], blockers: tuple[str, ...], as_at: AsAt) -> "BriefSummary":
        return cls(
            b["id"],
            b["title"],
            pv.brief_state(b),
            bool(blockers),
            parse_date(b["updated"]),
            parse_date(b["due"]),
            b["effort"],
            b["type"],
            frozenset(b["tags"]),
            blockers,
            as_at,
        )

    @property
    def ships(self) -> bool:
        return self.state == "done" and not self.is_conversation

    @property
    def is_conversation(self) -> bool:
        return self.kind in CONVERSATION_KINDS or not self.tags.isdisjoint(CONVERSATION_TAGS)

    @property
    def updated_iso(self) -> str:
        return self.updated.isoformat() if self.updated else ""

    def shipped_line(self) -> str:
        return f"{self.title} ({self.updated_iso})"


# --- Layer 2: milestones -------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FlagState:
    brief: str
    name: str
    state: str
    changed_on: dt.date


@dataclasses.dataclass(frozen=True)
class EnvironmentFlags:
    """Every feature flag the milestone's briefs recorded for one environment."""

    environment: str
    flags: tuple[FlagState, ...]


@dataclasses.dataclass(frozen=True)
class BriefEvidence:
    brief: str
    evidence: facts.Evidence


@dataclasses.dataclass(frozen=True)
class Blocker:
    brief: str
    waiting_on: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class MilestoneSummary:
    milestone: Milestone
    stage: Stage
    due: str  # the current forecast, else the baseline; empty when no date was recorded
    baseline: str  # the first promised date
    slip_days: int | None  # current minus baseline; None without a baseline
    late_days: int  # days past the first date with the milestone not done; no grace period
    missed: bool
    percent_done: int  # from ticked criteria, as the project page shows it
    effort_days: float  # open work, from each brief's estimated-effort
    unestimated: int  # open briefs with no usable estimate
    briefs: tuple[BriefSummary, ...]
    blockers: tuple[Blocker, ...]
    flags: tuple[EnvironmentFlags, ...]
    evidence: tuple[BriefEvidence, ...]
    as_at: AsAt

    @property
    def id(self) -> str:
        return self.milestone.id

    @property
    def title(self) -> str:
        return self.milestone.title

    @property
    def waiting_count(self) -> int:
        return sum(b.waiting for b in self.briefs)

    def line(self) -> str:
        notes = self._notes()
        return self.title + (f" ({', '.join(notes)})" if notes else "")

    def _notes(self) -> list[str]:
        if self.stage is Stage.DONE:
            return self._waiting_note()
        due = [f"due {self.due}" + (", missed" if self.missed else "")] if self.due else []
        return [f"{self.percent_done}%", *due, *self._waiting_note()]

    def _waiting_note(self) -> list[str]:
        return [f"{self.waiting_count} waiting"] if self.waiting_count else []


def next_due(milestones: Iterable[MilestoneSummary]) -> MilestoneSummary | None:
    """The open milestone with the earliest date; one without a date never counts."""
    dated = (m for m in milestones if m.due and m.stage is not Stage.DONE)
    return min(dated, key=lambda m: m.due, default=None)


# --- Layer 1: the tile ---------------------------------------------------------------------


def _worst(colours: Iterable[str]) -> str:
    return max(colours, key=pv.RANK.__getitem__, default="")


@dataclasses.dataclass(frozen=True)
class Health:
    owner: str
    reason: str
    set_on: str
    effective: str
    signals: tuple[str, ...]
    stale: bool = False  # the owner's statement is OWNER_STALE_DAYS or more old
    age_days: int | None = None

    @classmethod
    def of(cls, cfg: dict[str, Any], found: list[dict[str, str]], now: dt.datetime) -> "Health":
        details = tuple(s["detail"] for s in found)
        owned = pv.owner_health(cfg, found, now)
        if owned is None:
            return cls("", "", "", _worst(s["floor"] for s in found), details)
        return cls(
            owned["owner"],
            owned["reason"],
            owned["set_on"],
            owned["effective"],
            details,
            owned["owner_stale"],
            owned["age_days"],
        )

    @property
    def colour_word(self) -> str:
        return WORD[self.effective]


@dataclasses.dataclass(frozen=True)
class Tile:
    """Owner health with its floor, the latest recorded build and the project's own links."""

    as_at: AsAt
    health: Health
    build: facts.CiRow | None
    links: tuple[tuple[str, str], ...]


@dataclasses.dataclass(frozen=True)
class Ask:
    question: str
    deadline: str

    @classmethod
    def of(cls, raw: dict[str, Any] | None) -> "Ask | None":
        if not raw or not raw.get("question"):
            return None
        return cls(str(raw["question"]), str(raw.get("deadline", "")))

    def text(self) -> str:
        return self.question + (f" (by {self.deadline})" if self.deadline else "")


# --- Layer 0: the board sentence ------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BoardSentence:
    """One plain sentence a board member can read without the rest of the page."""

    as_at: AsAt
    text: str

    @classmethod
    def of(
        cls, title: str, health: Health, milestones: tuple[MilestoneSummary, ...], as_at: AsAt
    ) -> "BoardSentence":
        done = sum(m.stage is Stage.DONE for m in milestones)
        parts = [f"{title} is {SENTENCE_WORD[health.effective]}"]
        if milestones:
            parts.append(f"{done} of {plural(len(milestones), 'milestone')} done")
        parts += cls._dates(milestones)
        return cls(as_at, "; ".join(parts) + ".")

    @staticmethod
    def _dates(milestones: tuple[MilestoneSummary, ...]) -> list[str]:
        open_ = [m for m in milestones if m.stage is not Stage.DONE]
        missed = [m for m in open_ if m.missed]
        soonest = next_due(milestones)
        notes = [f"{len(missed)} past their date"] if missed else []
        return [*notes, f"next is {soonest.title} due {soonest.due}"] if soonest else notes


# --- shipped work --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ShippedLine:
    brief: str
    title: str
    note: str
    shipped_on: dt.date | None


@dataclasses.dataclass(frozen=True)
class ShippedRelease:
    release: str
    date: dt.date | None
    lines: tuple[ShippedLine, ...]
    as_at: AsAt


@dataclasses.dataclass(frozen=True)
class Project:
    source: str
    slug: str
    title: str
    outcome: str
    initiative: str
    as_at: AsAt
    board: BoardSentence
    tile: Tile
    ask: Ask | None
    milestones: tuple[MilestoneSummary, ...]
    shipped: tuple[BriefSummary, ...]
    shipped_by_release: tuple[ShippedRelease, ...]
    today: dt.date  # in the queue's own timezone

    @property
    def health(self) -> Health:
        return self.tile.health

    def stage(self, stage: Stage) -> tuple[MilestoneSummary, ...]:
        return tuple(m for m in self.milestones if m.stage is stage)

    def next_due(self) -> MilestoneSummary | None:
        return next_due(self.milestones)


# --- reading a snapshot --------------------------------------------------------------------


def _zone(settings: dict[str, Any], label: str) -> zoneinfo.ZoneInfo:
    """The queue's own `timezone`, UTC when unset or unusable."""
    name = str(settings["timezone"])
    try:
        return zoneinfo.ZoneInfo(name)
    except (KeyError, ValueError):  # an unknown zone name is a KeyError subclass
        warn(f"{label}/tasks.toml: timezone {name!r} is not a known zone; using UTC")
        return zoneinfo.ZoneInfo("UTC")


def open_blockers(queue: QueueBriefs) -> dict[str, tuple[str, ...]]:
    """Open briefs with a blocker still unmet, by the rule the rest of the plugin uses."""
    briefs = load_queue(queue)
    done = done_ids(queue, known_roots(queue.root))
    waiting = {b.id: tuple(unmet(b, done)) for b in briefs if b.status != "done"}
    return {brief: blockers for brief, blockers in waiting.items() if blockers}


def _slug(table: dict[str, Any], queue: pathlib.Path, fallback: str) -> str:
    """The project's own slug when it is text; otherwise the source label, saying so."""
    slug = table.get("slug")
    if isinstance(slug, str) and slug.strip():
        return slug
    if slug is not None:
        warn(f"{queue / 'project.toml'}: [project] slug must be text; using the source label")
    return fallback


@dataclasses.dataclass(frozen=True)
class QueueRead:
    """Everything one snapshot holds that a roll-up uses, read once."""

    label: str
    files: QueueBriefs
    as_at: AsAt
    now: dt.datetime  # in the queue's own timezone
    settings: dict[str, Any]  # tasks.toml
    cfg: dict[str, Any]  # project.toml
    briefs: list[dict[str, Any]]
    blockers: dict[str, tuple[str, ...]]
    recorded: Mapping[str, facts.BriefFacts]
    schedules: Mapping[str, facts.Schedule]
    ci: facts.CiRow | None
    meta: facts.ProjectMeta

    @classmethod
    def load(cls, label: str, snapshot: QueueSnapshot, now: dt.datetime) -> "QueueRead":
        files = QueueBriefs.read(snapshot.root, shown_as=label)
        queue = files.root
        settings = load_config_or_defaults(queue, "rollup", f"{label}/tasks.toml")
        cfg = pv.read_project(queue)
        return cls(
            label,
            files,
            AsAt.of(snapshot),
            now.astimezone(_zone(settings, label)),
            settings,
            cfg,
            pv.project_briefs(files),
            open_blockers(files),
            facts.load_facts(queue),
            facts.load_schedules(queue),
            facts.current_ci(queue),
            facts.load_project_meta(queue),
        )

    @property
    def queue(self) -> pathlib.Path:
        return self.files.root

    @property
    def today(self) -> dt.date:
        return self.now.date()

    def facts_of(self, brief: str) -> facts.BriefFacts:
        return self.recorded.get(brief, facts.BriefFacts(brief))


def _milestone(
    read: QueueRead, row: dict[str, Any], by_id: Mapping[str, BriefSummary]
) -> MilestoneSummary:
    mine = tuple(by_id[i] for i in row["briefs"] if i in by_id)
    title = pv.require_text(row["reader_title"], f"milestone {row['id']} plain or title")
    stage = Stage.of(row["status"])
    schedule = read.schedules.get(row["id"])
    baseline = schedule.baseline if schedule else None
    open_ = [b for b in mine if b.state != "done"]
    sized = [EFFORT_DAYS[b.effort] for b in open_ if b.effort in EFFORT_DAYS]
    late = (read.today - baseline).days if baseline and stage is not Stage.DONE else 0
    return MilestoneSummary(
        Milestone(row["id"], title, dict(Counter(b.state for b in mine)), row["release"] or None),
        stage,
        row["due"]
        if schedule is None or schedule.current is None
        else schedule.current.isoformat(),
        baseline.isoformat() if baseline else "",
        schedule.slip_days if schedule else None,
        max(late, 0),
        row["missed"],
        row["percent_done"],
        sum(sized),
        len(open_) - len(sized),
        mine,
        tuple(Blocker(b.id, b.blockers) for b in mine if b.blockers),
        _flags(read, [b.id for b in mine]),
        tuple(BriefEvidence(i, e) for b in mine for i in [b.id] for e in read.facts_of(i).evidence),
        read.as_at,
    )


def _flags(read: QueueRead, briefs: list[str]) -> tuple[EnvironmentFlags, ...]:
    states = [
        (flag.environment, FlagState(i, flag.name, flag.state, flag.changed_on))
        for i in briefs
        for flag in read.facts_of(i).current_flags
    ]
    environments = sorted({environment for environment, _ in states})
    return tuple(
        EnvironmentFlags(env, tuple(s for e, s in states if e == env)) for env in environments
    )


def _milestones(
    read: QueueRead, rows: list[dict[str, Any]], by_id: Mapping[str, BriefSummary]
) -> tuple[MilestoneSummary, ...]:
    """One milestone with a title that is not text is left out; the project still shows."""
    kept = []
    for row in rows:
        try:
            kept.append(_milestone(read, row, by_id))
        except pv.ProjectConfigError as e:
            warn(f"{read.label}: {e}; that milestone is left out")
    return tuple(kept)


def _shipped(briefs: Iterable[BriefSummary], today: dt.date) -> tuple[BriefSummary, ...]:
    since = today - dt.timedelta(days=SHIPPED_DAYS)
    done = [b for b in briefs if b.ships and b.updated and b.updated >= since]
    return tuple(sorted(done, key=lambda b: b.updated_iso, reverse=True))


def _shipped_by_release(
    read: QueueRead, summaries: Iterable[BriefSummary], rows: list[dict[str, Any]]
) -> tuple[ShippedRelease, ...]:
    """Done briefs grouped by the release they name, else their milestone's; declared releases first."""
    milestone_release = {i: row["release"] for row in rows for i in row["briefs"]}
    lines: dict[str, list[ShippedLine]] = {}
    for b in summaries:
        recorded = read.facts_of(b.id)
        release = recorded.release or milestone_release.get(b.id)
        if b.ships and release:
            line = ShippedLine(
                b.id, b.title, recorded.shipped or "", recorded.shipped_on or b.updated
            )
            lines.setdefault(release, []).append(line)
    dates = {r.id: r.date for r in read.meta.releases}
    order = [*dates, *sorted(set(lines) - set(dates))]
    return tuple(
        ShippedRelease(r, dates.get(r), tuple(lines[r]), read.as_at) for r in order if r in lines
    )


def project_of(read: QueueRead) -> Project:
    """The whole model for one snapshot read; raises ProjectConfigError for a project it cannot read."""
    rows = pv.milestone_rows(
        pv.listed_milestones(read.queue, read.cfg.get("milestones", [])), read.briefs, read.today
    )
    summaries = [
        BriefSummary.of(b, read.blockers.get(b["id"], ()), read.as_at) for b in read.briefs
    ]
    by_id = {b.id: b for b in summaries}
    signals = pv.Signals(now=read.now)
    floor = pv.computed_floor(rows, read.briefs, signals, read.ci)
    health = Health.of(read.cfg, floor, read.now)
    milestones = _milestones(read, rows, by_id)
    project = read.cfg["project"]
    return Project(
        read.label,
        _slug(project, read.queue, read.label),
        project["title"],
        project["outcome"],
        read.meta.initiative or "",
        read.as_at,
        BoardSentence.of(project["title"], health, milestones, read.as_at),
        Tile(read.as_at, health, read.ci, read.meta.links),
        Ask.of(read.cfg.get("ask")),
        milestones,
        _shipped(summaries, read.today),
        _shipped_by_release(read, summaries, rows),
        read.today,
    )
