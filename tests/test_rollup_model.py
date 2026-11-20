"""The roll-up model, read through the real snapshot path: a queue pushed to a bare remote, then built."""

import dataclasses
import datetime as dt
import pathlib
from collections.abc import Mapping
from typing import NamedTuple

import prioritise
import project_view as pv
import pytest
import queue_snapshot as qs
import rollup_model as rm

from tests.remote_queue import PUSHED_AT, publish, snapshot

NOW = dt.datetime(2026, 11, 19, 12, 0, tzinfo=dt.timezone.utc)
SOURCE = "alpha-source"


@dataclasses.dataclass(frozen=True)
class BriefSpec:
    id: str
    folder: str
    milestone: str = ""
    effort: str = ""
    updated: str = "2026-11-18"
    ticked: int = 1
    unticked: int = 0
    blocked_by: str = ""

    def text(self) -> str:
        optional = {
            "milestone": self.milestone,
            "estimated-effort": self.effort,
            "blocked_by": self.blocked_by,
        }
        extra = "".join(f"{key}: {value}\n" for key, value in optional.items() if value)
        criteria = "- [x] done\n" * self.ticked + "- [ ] open\n" * self.unticked
        return (
            f"---\nid: {self.id}\ntitle: {self.id.upper()}\nstatus: {self.folder}\n"
            f"type: feature\ndomain: demo\nupdated: {self.updated}\n{extra}---\n\n"
            f"# {self.id}\n\n## Success criteria\n{criteria}"
        )


@dataclasses.dataclass(frozen=True)
class QueueSpec:
    project: str
    briefs: tuple[BriefSpec, ...] = ()
    files: Mapping[str, str] = dataclasses.field(default_factory=dict)
    tasks: str = 'name = "Demo"\n'

    def write(self, clone: pathlib.Path) -> None:
        queue = clone / ".tasks"
        files = {
            "tasks.toml": self.tasks,
            "project.toml": self.project,
            **{f"{b.folder}/{b.id}.md": b.text() for b in self.briefs},
            **self.files,
        }
        for relative, text in files.items():
            path = queue / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)


def pushed(tmp_path: pathlib.Path, spec: QueueSpec) -> qs.QueueSnapshot:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return snapshot(publish(tmp_path, spec.write), tmp_path / "cache")


def built(tmp_path: pathlib.Path, spec: QueueSpec, now: dt.datetime = NOW) -> rm.Project:
    return rm.build_project(SOURCE, pushed(tmp_path, spec), now)


GREEN = 'colour = "green"\nreason = "On course."\nset_by = "Owner"\nset_on = "{set_on}"\n'


def project_toml(
    *,
    head: str = "",
    slug: str = 'slug = "alpha"',
    health: str = "[health]\n" + GREEN.format(set_on="2026-11-18"),
    tail: str = "",
) -> str:
    return (
        f'{head}\n[project]\n{slug}\ntitle = "Alpha"\noutcome = "Alpha works."\n\n{health}\n{tail}'
    )


MISSED = '[[milestones]]\nid = "m1"\ntitle = "Late"\ndue = "2026-11-01"\n'
OPEN_IN_M1 = BriefSpec("b1", "ready", milestone="m1", ticked=0, unticked=1)

# --- the shared world: every kind of fact the model reads ----------------------------------

WORLD_PROJECT = project_toml(
    head='initiative = "Platform"\n',
    health="[health]\n" + GREEN.format(set_on="2026-11-10"),
    tail="""
[links]
docs = "https://example.test/docs"

[[releases]]
id = "r1"
date = "2026-11-01"

[[releases]]
id = "r2"
date = "2026-12-01"

[ask]
question = "Ship it?"
deadline = "2026-11-25"

[[milestones]]
id = "m1"
title = "Shipped"
plain = "Shipping is done"
due = "2026-11-05"
release = "r1"

[[milestones]]
id = "m2"
title = "Active"
plain = "The active thing"
due = "2026-11-10"

[[milestones]]
id = "m3"
title = "Later"

[[milestones]]
id = "m4"
title = "Empty"
""",
)

WORLD_BRIEFS = (
    BriefSpec("b1", "done", milestone="m1", updated="2026-11-12"),
    BriefSpec("b2", "done", milestone="m1", updated="2026-10-01"),
    BriefSpec("b3", "in-progress", milestone="m2", effort="m", unticked=1, blocked_by="[b4]"),
    BriefSpec("b4", "ready", milestone="m2", ticked=0, unticked=1),
    BriefSpec("b5", "ready", milestone="m3", effort="l", ticked=0, unticked=1),
    BriefSpec("b6", "done", milestone="m2", effort="s", unticked=1),
    BriefSpec("b7", "done", updated="2026-11-18"),
    BriefSpec("b8", "done", updated="2026-11-17"),
)

WORLD_FILES = {
    "facts/b1.toml": 'brief = "b1"\nrelease = "r1"\nshipped = "Launched"\nshipped_on = "2026-11-13"\n',
    "facts/b7.toml": 'brief = "b7"\nrelease = "r9"\n',
    "facts/b3.toml": """brief = "b3"

[[flag]]
name = "x"
environment = "prod"
state = "on"
changed_on = "2026-11-10"

[[flag]]
name = "x"
environment = "prod"
state = "off"
changed_on = "2026-11-15"

[[flag]]
name = "y"
environment = "prod"
state = "on"
changed_on = "2026-11-12"

[[flag]]
name = "x"
environment = "dev"
state = "on"
changed_on = "2026-11-11"

[[evidence]]
kind = "screenshot"
link = "https://example.test/shot"
caption = "The page"
access = "public"
recorded_on = "2026-11-14"
""",
    "facts/b4.toml": """brief = "b4"

[[evidence]]
kind = "video"
link = "https://example.test/clip"
caption = "The demo"
access = "org"
recorded_on = "2026-11-15"
""",
    "history/due/m2-2026-11-12-aaaaaaaa.toml": (
        'milestone = "m2"\ndue = "2026-11-30"\nset_on = "2026-11-12"\nreason = "Scope grew."\n'
    ),
    "history/ci/2026-11-10-aaaaaaaa.toml": (
        'state = "green"\nlink = "https://example.test/ci/1"\nset_on = "2026-11-10"\n'
    ),
    "history/ci/2026-11-18-bbbbbbbb.toml": (
        'state = "red"\nlink = "https://example.test/ci/2"\nset_on = "2026-11-18"\n'
    ),
}


class World(NamedTuple):
    snapshot: qs.QueueSnapshot
    project: rm.Project

    def milestone(self, id: str) -> rm.MilestoneSummary:
        return next(m for m in self.project.milestones if m.id == id)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    spec = QueueSpec(WORLD_PROJECT, WORLD_BRIEFS, WORLD_FILES)
    snap = pushed(tmp_path_factory.mktemp("world"), spec)
    return World(snap, rm.build_project(SOURCE, snap, NOW))


# --- the project and its stamp -------------------------------------------------------------


def test_project_carries_its_identity_and_the_commit_it_came_from(world: World) -> None:
    project = world.project
    assert (project.source, project.slug, project.title) == (SOURCE, "alpha", "Alpha")
    assert (project.outcome, project.initiative) == ("Alpha works.", "Platform")
    assert project.as_at == rm.AsAt(world.snapshot.sha, world.snapshot.committed, False)
    assert project.as_at.table() == {
        "sha": world.snapshot.sha,
        "committed": PUSHED_AT,
        "stale": False,
    }


def test_every_section_is_stamped_with_the_same_commit(world: World) -> None:
    project = world.project
    stamps = {
        project.board.as_at,
        project.tile.as_at,
        *(m.as_at for m in project.milestones),
        *(b.as_at for m in project.milestones for b in m.briefs),
        *(r.as_at for r in project.shipped_by_release),
        *(b.as_at for b in project.shipped),
    }
    assert stamps == {project.as_at}


def test_a_cached_snapshot_is_stamped_stale(world: World) -> None:
    offline = dataclasses.replace(world.snapshot, stale=True)
    assert rm.build_project(SOURCE, offline, NOW).as_at.table()["stale"] is True


def test_the_same_snapshot_always_gives_the_same_model(world: World) -> None:
    assert rm.build_project(SOURCE, world.snapshot, NOW) == world.project


def test_the_ask_reads_with_its_deadline(world: World) -> None:
    assert world.project.ask is not None
    assert world.project.ask.text() == "Ship it? (by 2026-11-25)"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ({}, None),
        ({"deadline": "2026-11-25"}, None),
        ({"question": "Go?"}, "Go?"),
    ],
)
def test_an_ask_needs_a_question(raw: dict[str, str] | None, expected: str | None) -> None:
    ask = rm.Ask.of(raw)
    assert (ask.text() if ask else None) == expected


# --- milestones ----------------------------------------------------------------------------


def test_milestones_keep_the_order_of_project_toml_and_the_readers_title(world: World) -> None:
    got = [(m.id, m.title, m.milestone.title) for m in world.project.milestones]
    assert got == [
        ("m1", "Shipping is done", "Shipping is done"),
        ("m2", "The active thing", "The active thing"),
        ("m3", "Later", "Later"),
        ("m4", "Empty", "Empty"),
    ]


def test_each_milestone_sits_in_the_stage_its_briefs_put_it(world: World) -> None:
    project = world.project
    assert [m.id for m in project.stage(rm.Stage.DONE)] == ["m1"]
    assert [m.id for m in project.stage(rm.Stage.NOW)] == ["m2"]
    assert [m.id for m in project.stage(rm.Stage.NEXT)] == ["m3", "m4"]


@pytest.mark.parametrize(
    "stage, key, status",
    [
        (rm.Stage.NOW, "now", "active"),
        (rm.Stage.NEXT, "next", "not-started"),
        (rm.Stage.NEXT, "next", "unmapped"),
        (rm.Stage.DONE, "done", "done"),
    ],
)
def test_a_stage_has_a_key_and_comes_from_a_milestone_status(
    stage: rm.Stage, key: str, status: str
) -> None:
    assert (stage.key, rm.Stage.of(status)) == (key, stage)


@pytest.mark.parametrize(
    "id, due, baseline, slip, late, missed, percent",
    [
        ("m1", "2026-11-05", "2026-11-05", 0, 0, False, 100),
        ("m2", "2026-11-30", "2026-11-10", 20, 9, True, 40),
        ("m3", "", "", None, 0, False, 0),
        ("m4", "", "", None, 0, False, 0),
    ],
)
def test_schedule_figures_follow_the_first_promise_and_the_latest_forecast(
    world: World,
    id: str,
    due: str,
    baseline: str,
    slip: int | None,
    late: int,
    missed: bool,
    percent: int,
) -> None:
    m = world.milestone(id)
    assert (m.due, m.baseline, m.slip_days, m.late_days) == (due, baseline, slip, late)
    assert (m.missed, m.percent_done) == (missed, percent)


def test_open_effort_is_summed_and_unsized_briefs_are_counted(world: World) -> None:
    efforts = {m.id: (m.effort_days, m.unestimated) for m in world.project.milestones}
    # m2 holds b3 (m = 2 days), b4 (no estimate) and the unverified b6 (s = half a day).
    assert efforts == {"m1": (0, 0), "m2": (2.5, 1), "m3": (7, 0), "m4": (0, 0)}


def test_a_milestone_counts_its_briefs_by_state_and_names_its_release(world: World) -> None:
    m1, m2 = world.milestone("m1"), world.milestone("m2")
    assert m1.milestone.counts == {"done": 2} and m1.milestone.release == "r1"
    assert m2.milestone.counts == {"active": 1, "queued": 1, "unverified": 1}
    assert m2.milestone.release is None


def test_a_brief_that_waits_is_reported_with_what_it_waits_on(world: World) -> None:
    m2 = world.milestone("m2")
    assert [(b.brief, b.waiting_on) for b in m2.blockers] == [("b3", ("b4",))]
    assert m2.waiting_count == 1
    assert world.milestone("m1").blockers == () and world.milestone("m1").waiting_count == 0
    waiting = {b.id: b.waiting for b in m2.briefs}
    assert waiting == {"b3": True, "b4": False, "b6": False}


def test_flags_group_by_environment_with_the_newest_state_of_each(world: World) -> None:
    states = {
        e.environment: [(f.brief, f.name, f.state, f.changed_on.isoformat()) for f in e.flags]
        for e in world.milestone("m2").flags
    }
    assert states == {
        "dev": [("b3", "x", "on", "2026-11-11")],
        "prod": [("b3", "x", "off", "2026-11-15"), ("b3", "y", "on", "2026-11-12")],
    }
    assert [e.environment for e in world.milestone("m2").flags] == ["dev", "prod"]
    assert world.milestone("m1").flags == ()


def test_evidence_is_listed_against_the_brief_that_recorded_it(world: World) -> None:
    got = sorted(
        (e.brief, e.evidence.kind, e.evidence.link) for e in world.milestone("m2").evidence
    )
    assert got == [
        ("b3", "screenshot", "https://example.test/shot"),
        ("b4", "video", "https://example.test/clip"),
    ]
    assert world.milestone("m3").evidence == ()


def test_a_milestone_reads_as_one_line(world: World) -> None:
    lines = {m.id: m.line() for m in world.project.milestones}
    assert lines == {
        "m1": "Shipping is done",
        "m2": "The active thing (40%, due 2026-11-30, missed, 1 waiting)",
        "m3": "Later (0%)",
        "m4": "Empty (0%)",
    }


def test_a_done_milestone_line_still_names_what_waits(world: World) -> None:
    done = dataclasses.replace(world.milestone("m1"), briefs=world.milestone("m2").briefs)
    assert done.line() == "Shipping is done (1 waiting)"


def test_a_milestone_with_a_future_first_date_is_not_late(tmp_path: pathlib.Path) -> None:
    tail = '[[milestones]]\nid = "m1"\ntitle = "Soon"\ndue = "2026-12-25"\n'
    spec = QueueSpec(project_toml(tail=tail), (OPEN_IN_M1,))
    m = built(tmp_path, spec).milestones[0]
    assert (m.late_days, m.missed, m.slip_days, m.due) == (0, False, 0, "2026-12-25")


def test_a_milestone_with_a_first_date_but_no_forecast_shows_the_first_date(
    tmp_path: pathlib.Path,
) -> None:
    m = built(tmp_path, QueueSpec(project_toml(tail=MISSED), (OPEN_IN_M1,))).milestones[0]
    assert (m.due, m.baseline, m.slip_days, m.late_days, m.missed) == (
        "2026-11-01",
        "2026-11-01",
        0,
        18,
        True,
    )


def test_a_milestone_title_that_is_not_text_is_left_out_and_the_project_still_shows(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tail = '[[milestones]]\nid = "bad"\ntitle = "Bad"\nplain = 5\n\n' + MISSED
    project = built(tmp_path, QueueSpec(project_toml(tail=tail), (OPEN_IN_M1,)))
    assert [m.id for m in project.milestones] == ["m1"]
    assert "bad" not in project.board.text
    err = capsys.readouterr().err
    assert f"{SOURCE}: " in err and "milestone bad plain or title must be text" in err
    assert "that milestone is left out" in err


def test_a_milestone_that_lists_its_briefs_still_reads_but_is_warned_about(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tail = '[[milestones]]\nid = "m1"\ntitle = "Old"\nbriefs = ["b1"]\n'
    spec = QueueSpec(project_toml(tail=tail), (BriefSpec("b1", "ready", ticked=0, unticked=1),))
    milestone = built(tmp_path, spec).milestones[0]
    assert [b.id for b in milestone.briefs] == ["b1"]
    assert "lists briefs, which is deprecated" in capsys.readouterr().err


# --- day and week boundaries ---------------------------------------------------------------


def at(day: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{day}T12:00:00+00:00")


@pytest.mark.parametrize(
    "today, late, missed",
    [("2026-11-09", 0, False), ("2026-11-10", 0, False), ("2026-11-11", 1, True)],
)
def test_lateness_counts_from_the_first_day_past_the_first_date(
    world: World, today: str, late: int, missed: bool
) -> None:
    project = rm.build_project(SOURCE, world.snapshot, at(today))
    m2 = next(m for m in project.milestones if m.id == "m2")
    assert (m2.late_days, m2.missed) == (late, missed)


@pytest.mark.parametrize("today, age, stale", [("2026-11-16", 6, False), ("2026-11-17", 7, True)])
def test_owner_health_goes_stale_on_the_seventh_day(
    world: World, today: str, age: int, stale: bool
) -> None:
    health = rm.build_project(SOURCE, world.snapshot, at(today)).health
    assert (health.age_days, health.stale) == (age, stale)


@pytest.mark.parametrize("today, listed", [("2026-11-26", True), ("2026-11-27", False)])
def test_a_brief_stays_shipped_for_fourteen_days(world: World, today: str, listed: bool) -> None:
    shipped = rm.build_project(SOURCE, world.snapshot, at(today)).shipped
    assert ("b1" in [b.id for b in shipped]) is listed


# --- the queue's own time zone -------------------------------------------------------------


def zone_spec(tasks: str) -> QueueSpec:
    tail = '[[milestones]]\nid = "m1"\ntitle = "Edge"\ndue = "2026-11-19"\n'
    return QueueSpec(project_toml(tail=tail), (OPEN_IN_M1,), tasks=tasks)


def test_a_queue_in_an_earlier_zone_is_a_day_later_than_utc(tmp_path: pathlib.Path) -> None:
    spec = zone_spec('name = "Demo"\ntimezone = "Pacific/Auckland"\n')
    m = built(tmp_path, spec).milestones[0]
    assert (m.late_days, m.missed) == (1, True)


def test_a_queue_with_no_zone_uses_utc(tmp_path: pathlib.Path) -> None:
    m = built(tmp_path, zone_spec('name = "Demo"\n')).milestones[0]
    assert (m.late_days, m.missed) == (0, False)


def test_an_unknown_zone_is_named_and_utc_is_used(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    m = built(tmp_path, zone_spec('timezone = "Nowhere/Land"\n')).milestones[0]
    assert (m.late_days, m.missed) == (0, False)
    assert "timezone 'Nowhere/Land' is not a known zone; using UTC" in capsys.readouterr().err


def test_an_unreadable_tasks_toml_falls_back_to_utc_and_says_which_file(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    m = built(tmp_path, zone_spec("timezone = [unterminated\n")).milestones[0]
    assert (m.late_days, m.missed) == (0, False)
    assert "tasks.toml (" in capsys.readouterr().err


# --- health --------------------------------------------------------------------------------


def test_owner_health_is_capped_by_the_automatic_checks_and_says_so(world: World) -> None:
    health = world.project.health
    assert (health.owner, health.effective, health.set_on) == ("green", "red", "2026-11-10")
    assert (health.stale, health.age_days) == (True, 9)
    assert any(
        "latest build recorded in the queue is red (2026-11-18)" in s for s in health.signals
    )
    assert any("past its date and not done: m2" in s for s in health.signals)
    assert (health.colour_word, health.reason) == ("Red", "On course.")


def test_the_tile_holds_the_health_the_latest_build_and_the_projects_links(world: World) -> None:
    tile = world.project.tile
    assert tile.health is world.project.health
    assert tile.build is not None and tile.build.state == "red"
    assert tile.build.link == "https://example.test/ci/2"
    assert tile.links == (("docs", "https://example.test/docs"),)


def test_a_green_build_adds_no_signal_and_is_still_on_the_tile(tmp_path: pathlib.Path) -> None:
    files = {"history/ci/2026-11-18-aaaaaaaa.toml": CI_GREEN}
    project = built(tmp_path, QueueSpec(project_toml(), files=files))
    assert project.tile.build is not None and project.tile.build.state == "green"
    assert project.health.signals == ()
    assert project.health.effective == "green"


CI_GREEN = 'state = "green"\nlink = "https://example.test/ci/3"\nset_on = "2026-11-18"\n'


def test_a_queue_with_no_build_recorded_has_no_build_on_its_tile(tmp_path: pathlib.Path) -> None:
    project = built(tmp_path, QueueSpec(project_toml()))
    assert project.tile.build is None and project.tile.links == ()


def test_the_newest_health_row_is_the_owners_current_word(tmp_path: pathlib.Path) -> None:
    older = "[health]\n" + GREEN.format(set_on="2026-11-01").replace("green", "amber")
    row = GREEN.format(set_on="2026-11-18")
    files = {"history/health/2026-11-18-aaaaaaaa.toml": row}
    health = built(tmp_path, QueueSpec(project_toml(health=older), files=files)).health
    assert (health.owner, health.set_on, health.stale) == ("green", "2026-11-18", False)
    assert (health.reason, health.effective) == ("On course.", "green")


def test_a_statement_with_no_date_never_goes_stale_and_is_capped_without_one(
    tmp_path: pathlib.Path,
) -> None:
    undated = '[health]\ncolour = "green"\nreason = "Fine"\n'
    spec = QueueSpec(project_toml(health=undated, tail=MISSED), (OPEN_IN_M1,))
    health = built(tmp_path, spec).health
    assert (health.age_days, health.stale, health.set_on) == (None, False, "")
    assert (health.owner, health.effective) == ("green", "red")


def test_an_undated_statement_that_stands_says_it_was_set_by_the_owner(
    tmp_path: pathlib.Path,
) -> None:
    undated = '[health]\ncolour = "green"\nreason = "Fine"\n'
    health = built(tmp_path, QueueSpec(project_toml(health=undated))).health
    assert (health.owner, health.effective, health.set_on) == ("green", "green", "")


def test_without_an_owner_word_the_floor_still_colours_the_tile(tmp_path: pathlib.Path) -> None:
    spec = QueueSpec(project_toml(health="", tail=MISSED), (OPEN_IN_M1,))
    project = built(tmp_path, spec)
    health = project.health
    assert (health.owner, health.effective) == ("", "red")
    assert health.colour_word == "Red" and health.signals
    assert project.board.text.startswith("Alpha is red; 0 of 1 milestone done; 1 past their date")


def test_with_no_owner_word_and_nothing_wrong_the_tile_says_not_set(tmp_path: pathlib.Path) -> None:
    project = built(tmp_path, QueueSpec(project_toml(health="")))
    health = project.health
    assert (health.effective, health.signals) == ("", ())
    assert health.colour_word == "Not set by owner"
    assert project.board.text == "Alpha is not yet rated by its owner."


# --- the board sentence --------------------------------------------------------------------


def test_the_board_sentence_reads_on_its_own(world: World) -> None:
    assert world.project.board.text == (
        "Alpha is red; 1 of 4 milestones done; 1 past their date;"
        " next is The active thing due 2026-11-30."
    )


def test_the_board_sentence_picks_the_earliest_open_date(tmp_path: pathlib.Path) -> None:
    tail = (
        '[[milestones]]\nid = "m1"\ntitle = "Later"\ndue = "2026-12-20"\n\n'
        '[[milestones]]\nid = "m2"\ntitle = "Sooner"\ndue = "2026-12-01"\n'
    )
    briefs = (OPEN_IN_M1, dataclasses.replace(OPEN_IN_M1, id="b2", milestone="m2"))
    text = built(tmp_path, QueueSpec(project_toml(tail=tail), briefs)).board.text
    assert text == "Alpha is green; 0 of 2 milestones done; next is Sooner due 2026-12-01."


def test_the_board_sentence_with_every_milestone_done_names_no_date(tmp_path: pathlib.Path) -> None:
    tail = '[[milestones]]\nid = "m1"\ntitle = "Only"\ndue = "2026-11-01"\n'
    done = BriefSpec("b1", "done", milestone="m1")
    text = built(tmp_path, QueueSpec(project_toml(tail=tail), (done,))).board.text
    assert text == "Alpha is green; 1 of 1 milestone done."


# --- briefs and shipped work ---------------------------------------------------------------


def test_a_brief_summary_carries_its_dates_effort_and_waiting(world: World) -> None:
    b3 = next(b for b in world.milestone("m2").briefs if b.id == "b3")
    assert (b3.title, b3.state, b3.waiting, b3.effort, b3.kind) == (
        "B3",
        "active",
        True,
        "m",
        "feature",
    )
    assert (b3.updated, b3.updated_iso, b3.due) == (dt.date(2026, 11, 18), "2026-11-18", None)
    assert b3.blockers == ("b4",)
    assert b3.shipped_line() == "B3 (2026-11-18)"


def test_a_brief_with_no_update_date_has_an_empty_date(world: World) -> None:
    b3 = next(b for b in world.milestone("m2").briefs if b.id == "b3")
    bare = dataclasses.replace(b3, updated=None)
    assert (bare.updated_iso, bare.shipped_line()) == ("", "B3 ()")


def test_shipped_lists_the_briefs_done_in_the_last_fortnight_newest_first(world: World) -> None:
    assert [(b.id, b.updated_iso) for b in world.project.shipped] == [
        ("b7", "2026-11-18"),
        ("b8", "2026-11-17"),
        ("b1", "2026-11-12"),
    ]


def test_shipped_by_release_groups_by_the_release_a_brief_names_else_its_milestones(
    world: World,
) -> None:
    got = [
        (r.release, r.date, [(line.brief, line.note, line.shipped_on) for line in r.lines])
        for r in world.project.shipped_by_release
    ]
    assert got == [
        (
            "r1",
            dt.date(2026, 11, 1),
            [
                ("b1", "Launched", dt.date(2026, 11, 13)),
                ("b2", "", dt.date(2026, 10, 1)),
            ],
        ),
        ("r9", None, [("b7", "", dt.date(2026, 11, 18))]),
    ]


def test_a_release_with_nothing_shipped_in_it_is_left_out(world: World) -> None:
    assert "r2" not in [r.release for r in world.project.shipped_by_release]


# --- the slug ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug, expected, warned",
    [
        ('slug = "named"', "named", False),
        ("", SOURCE, False),
        ("slug = 5", SOURCE, True),
        ('slug = "  "', SOURCE, True),
    ],
)
def test_the_slug_is_the_projects_own_text_else_the_source_label(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    slug: str,
    expected: str,
    warned: bool,
) -> None:
    project = built(tmp_path, QueueSpec(project_toml(slug=slug)))
    assert project.slug == expected
    assert ("[project] slug must be text" in capsys.readouterr().err) is warned


# --- a project it cannot read --------------------------------------------------------------


@pytest.mark.parametrize(
    "project, message",
    [
        ("", "[project] is required"),
        ("project = 5\n", "[project] is required"),
        (project_toml().replace("Alpha works.", ""), "[project] outcome is required"),
        (project_toml().replace('title = "Alpha"', "title = 5"), "[project] title must be text"),
        (project_toml(head="health = 5", health=""), "[health] is required"),
        (project_toml(health='[health]\ncolour = "blue"\nreason = "x"'), "colour must be"),
        (project_toml(health='[health]\ncolour = "green"'), "[health] reason is required"),
        (project_toml(head='ask = "later"'), "[ask] is a single table"),
    ],
)
def test_a_project_it_cannot_read_raises_for_the_caller_to_skip(
    tmp_path: pathlib.Path, project: str, message: str
) -> None:
    with pytest.raises(pv.ProjectConfigError, match=message.replace("[", r"\[")):
        built(tmp_path, QueueSpec(project))


def test_a_queue_with_no_project_file_raises(tmp_path: pathlib.Path) -> None:
    spec = QueueSpec("")
    snap = pushed(tmp_path, spec)
    (snap.root / "project.toml").unlink()
    with pytest.raises(pv.ProjectConfigError, match="does not exist"):
        rm.build_project(SOURCE, snap, NOW)


def test_a_config_error_from_the_milestone_rows_is_not_swallowed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> list[dict[str, object]]:
        raise pv.ProjectConfigError("milestones: refused")

    monkeypatch.setattr(pv, "milestone_rows", refuse)
    with pytest.raises(pv.ProjectConfigError, match="refused"):
        rm.build_project(SOURCE, world.snapshot, NOW)


def test_milestones_that_are_not_a_list_are_named_and_left_out(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = built(tmp_path, QueueSpec(project_toml(head='milestones = "soon"')))
    assert project.milestones == ()
    assert "must be [[milestones]] tables" in capsys.readouterr().err


def test_a_milestone_row_without_a_text_id_is_named_and_left_out(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = built(tmp_path, QueueSpec(project_toml(tail='[[milestones]]\ntitle = "No id"\n')))
    assert project.milestones == ()
    assert "row 1 needs a text id" in capsys.readouterr().err


# --- the model is read from the extracted snapshot, so ranking must work there too ----------


def test_prioritise_ranks_an_extracted_snapshot_with_no_git_and_no_scripts(world: World) -> None:
    root = world.snapshot.root
    assert not (root / ".git").exists() and not (root / "scripts").exists()
    ranked = prioritise.rank(root, today=dt.date(2026, 11, 19))
    assert sorted(b["id"] for b in ranked) == ["b4", "b5"]
