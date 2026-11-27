"""The maintain tick: freshness, due briefs, capabilities, claims, idempotency and the launcher call."""

import dataclasses
import datetime as dt
import pathlib
import subprocess
import time

import claim as claim_mod
import hygiene as hy
import maintain as maintain_mod
import pytest
from claim import Claim, Claimer, Outcome
from headless import ScriptLaunch
from hygiene import Home, Phase, Step
from hygiene_lease import HygieneLeaser
from maintain import Candidate, Maintainer, Report, ReportKind, default_holder, now_utc
from maintain_config import Tier, TierSettings
from maintain_state import LastRun
from wake import Due, Reading, RunKey

from tests.maintain_helpers import (
    FUTURE,
    NOW,
    PAST,
    add_brief,
    idle_engine,
    maintainer,
    origin_files,
    origin_head,
    queue_repo,
    second_clone,
    settings_for,
)
from tests.queue_helpers import installed_launcher
from tests.test_git_sync import git

MINUTE = dt.timedelta(minutes=1)
TTL = dt.timedelta(hours=2)


def spawned_ids(spawned) -> list[str]:
    return [
        args[-1].split("\n")[0].removeprefix(maintain_mod.ACTION_SKILL).strip()
        for args, _ in spawned
    ]


def test_a_fresh_last_run_exits_at_once_without_touching_git(tmp_path, monkeypatch):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    LastRun("laptop", NOW - MINUTE).write(clone)
    before, spawned = origin_head(clone), []

    def boom(*args, **kwargs):
        raise AssertionError(f"git was run: {args}")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert maintainer(clone, spawned).tick().ran is False
    monkeypatch.undo()
    assert spawned == [] and origin_head(clone) == before
    assert LastRun.read(clone, "laptop").at == NOW - MINUTE


def test_a_stale_last_run_runs_the_tick_and_records_it(tmp_path):
    clone = queue_repo(tmp_path)
    LastRun("laptop", NOW - dt.timedelta(minutes=6)).write(clone)
    result = maintainer(clone, []).tick()
    assert result.ran and LastRun.read(clone, "laptop").at == NOW


def test_the_cloud_tier_waits_an_hour_by_default(tmp_path):
    clone = queue_repo(tmp_path)
    LastRun("cloud", NOW - dt.timedelta(minutes=30)).write(clone)
    assert maintainer(clone, [], Tier.CLOUD).tick().ran is False
    LastRun("cloud", NOW - dt.timedelta(minutes=61)).write(clone)
    assert maintainer(clone, [], Tier.CLOUD).tick().ran is True


def test_a_second_tick_right_after_the_first_does_nothing(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    spawned = []
    assert maintainer(clone, spawned).tick().claimed == ("b1",)
    assert maintainer(clone, spawned).tick().ran is False
    assert len(spawned) == 1


def test_a_past_not_before_is_claimed_and_run_and_a_future_one_is_left(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "due", not_before=PAST)
    add_brief(clone, "later", not_before=FUTURE)
    spawned = []
    result = maintainer(clone, spawned).tick()
    assert result.claimed == ("due",) and spawned_ids(spawned) == ["due"]
    files = origin_files(clone)
    assert "in-progress/due.md" in files and "ready/later.md" in files


def test_a_due_wake_waits_for_its_unmet_dependencies(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "first", folder="done")
    add_brief(clone, "after-done", not_before=PAST, blocked_by="[first]")
    add_brief(clone, "after-open", not_before=PAST, blocked_by="[second]")
    add_brief(clone, "after-today", not_before=PAST, blocked_by="[date:2026-11-26]")
    add_brief(clone, "after-later", not_before=PAST, blocked_by="[date:2026-11-27]")
    assert maintainer(clone, []).tick().claimed == ("after-done", "after-today")


def test_an_unreadable_queue_leaves_id_dependencies_unmet_with_a_warning(
    tmp_path, monkeypatch, capsys
):
    clone = queue_repo(tmp_path)
    add_brief(clone, "first", folder="done")
    add_brief(clone, "after-done", not_before=PAST, blocked_by="[first]")
    add_brief(clone, "free", not_before=PAST)

    def unreadable(root, roots=()):
        raise PermissionError(13, "Permission denied", str(root))

    monkeypatch.setattr(maintain_mod, "done_ids", unreadable)
    assert maintainer(clone, []).tick().claimed == ("free",)
    assert "id dependencies count as unmet this tick" in capsys.readouterr().err


def test_a_datetime_with_an_offset_is_due_at_its_utc_instant(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "soon", not_before="2026-11-26T10:30:00+02:00")  # 08:30Z
    add_brief(clone, "not-yet", not_before="2026-11-26T10:30:00-02:00")  # 12:30Z
    assert maintainer(clone, []).tick().claimed == ("soon",)


def test_the_launcher_is_handed_the_action_skill_the_brief_and_the_queue(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    spawned = []
    maintainer(clone, spawned).tick()
    ((args, env),) = spawned
    assert args[0] == "--dangerously-skip-permissions"
    assert args[-1].startswith("/claude-tasks:action-task b1\n")
    assert "already claimed" in args[-1]
    assert env == {"CLAUDE_TASKS_DIR": str(clone), "CLAUDE_ANY_CALLER": "claude-tasks-maintain"}


def test_a_probe_that_exits_zero_makes_a_brief_due(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", wake_probe="exit 0")
    assert maintainer(clone, []).tick().claimed == ("b1",)


def test_a_probe_that_exits_ten_is_not_yet(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", wake_probe="exit 10")
    result = maintainer(clone, []).tick()
    assert result.claimed == () and "ready/b1.md" in origin_files(clone)


def test_a_probe_that_fails_is_logged_and_not_due(tmp_path, capsys):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", wake_probe="echo nope >&2; exit 3")
    assert maintainer(clone, []).tick().claimed == ()
    assert "wake_probe of b1 failed (exit 3: nope)" in capsys.readouterr().err


def test_a_brief_past_valid_until_is_reported_overdue_and_not_run(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST, valid_until="2026-11-26T08:00:00Z")
    spawned = []
    result = maintainer(clone, spawned).tick()
    assert spawned == [] and [str(r) for r in result.reports] == [
        "overdue b1: past valid_until; not run"
    ]
    assert LastRun.read(clone, "laptop").reports == ("overdue b1: past valid_until; not run",)


def test_a_brief_the_tier_cannot_serve_is_skipped_quietly_before_escalate_after(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST, needs="[aws]", escalate_after="2026-11-27")
    spawned = []
    result = maintainer(clone, spawned, Tier.CLOUD).tick()
    assert spawned == [] and result.reports == ()


def test_an_unserved_brief_past_escalate_after_is_reported_per_capability(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST, needs="[aws, slack]", escalate_after="2026-11-26")
    spawned = []
    result = maintainer(clone, spawned, Tier.CLOUD).tick()
    assert spawned == []
    assert [str(r) for r in result.reports] == [
        "overdue-needs-aws b1: needs aws",
        "overdue-needs-slack b1: needs slack",
    ]


def test_the_laptop_serves_every_need_and_a_configured_cloud_serves_its_own(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "uses-aws", not_before=PAST, needs="[aws]")
    add_brief(clone, "uses-slack", not_before=PAST, needs="[slack]")
    base = settings_for(clone)
    cloud = dataclasses.replace(
        base,
        tiers={
            **base.tiers,
            Tier.CLOUD: TierSettings(dt.timedelta(minutes=60), frozenset({"slack"})),
        },
    )
    assert maintainer(clone, [], Tier.CLOUD, settings=cloud).tick().claimed == ("uses-slack",)
    assert maintainer(clone, [], Tier.LAPTOP, settings=cloud).tick().claimed == ("uses-aws",)


def test_a_brief_that_needs_the_laptop_is_not_served_by_the_cloud(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST, **{"requires-local": "true"})
    assert maintainer(clone, [], Tier.CLOUD).tick().claimed == ()


def test_a_brief_that_wants_a_person_is_not_run_unattended(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST, autonomy="ask")
    assert maintainer(clone, []).tick().claimed == ()


def test_a_wake_that_already_ran_is_not_run_again_by_a_later_tick_or_the_other_tier(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    spawned = []
    assert maintainer(clone, spawned).tick().claimed == ("b1",)
    (clone / "ready").mkdir(exist_ok=True)  # git removed the emptied folder with the claim
    git(clone, "mv", "in-progress/b1.md", "ready/b1.md")
    git(clone, "commit", "-qam", "returned to ready")
    git(clone, "push", "-q", "origin", "main")
    later = NOW + dt.timedelta(hours=3)
    base = settings_for(clone)
    everything = dataclasses.replace(
        base,
        tiers={**base.tiers, Tier.CLOUD: TierSettings(dt.timedelta(minutes=60), frozenset({"*"}))},
    )
    assert maintainer(clone, spawned, now=later).tick().claimed == ()
    other = second_clone(tmp_path)
    assert (
        maintainer(other, spawned, Tier.CLOUD, now=later, settings=everything).tick().claimed == ()
    )
    assert len(spawned) == 1


def test_bumping_the_generation_lets_a_finished_wake_fire_again(tmp_path):
    clone = queue_repo(tmp_path)
    path = add_brief(clone, "b1", not_before=PAST)
    spawned = []
    maintainer(clone, spawned).tick()
    (clone / "ready").mkdir(exist_ok=True)
    moved = clone / "ready" / "b1.md"
    git(clone, "mv", "in-progress/b1.md", "ready/b1.md")
    moved.write_text(moved.read_text().replace("wake_key:", "wake_generation: 1\nwake_key:"))
    git(clone, "commit", "-qam", "next generation")
    git(clone, "push", "-q", "origin", "main")
    maintainer(clone, spawned, now=NOW + dt.timedelta(hours=3)).tick()
    assert len(spawned) == 2 and path.parent.name == "ready"


def test_an_expired_lease_is_reclaimed_and_a_live_one_is_left(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    spawned = []
    maintainer(clone, spawned).tick()
    live = maintainer(clone, spawned, now=NOW + dt.timedelta(minutes=30)).tick()
    assert live.claimed == () and len(spawned) == 1
    lapsed = maintainer(clone, spawned, now=NOW + TTL + MINUTE).tick()
    assert lapsed.claimed == ("b1",) and len(spawned) == 2
    assert "lease_attempts: 2" in (clone / "in-progress" / "b1.md").read_text()


def test_a_brief_that_keeps_losing_its_lease_is_reported_after_max_attempts(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    settings = dataclasses.replace(settings_for(clone), max_attempts=1)
    spawned = []
    maintainer(clone, spawned, settings=settings).tick()
    result = maintainer(clone, spawned, now=NOW + TTL + MINUTE, settings=settings).tick()
    assert len(spawned) == 1
    assert [r.kind for r in result.reports] == [ReportKind.EXHAUSTED]


def test_an_in_progress_brief_with_no_lease_is_left_to_its_owner(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", "in-progress", not_before=PAST)
    spawned = []
    assert maintainer(clone, spawned).tick().claimed == () and spawned == []


def test_a_tick_claims_at_most_the_configured_number(tmp_path):
    clone = queue_repo(tmp_path)
    for name in ("a", "b", "c", "d"):
        add_brief(clone, name, not_before=PAST)
    settings = dataclasses.replace(settings_for(clone), max_claims=2)
    spawned = []
    assert maintainer(clone, spawned, settings=settings).tick().claimed == ("a", "b")


def test_an_inbox_brief_that_is_not_enriched_cannot_be_claimed_and_is_reported(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", "inbox", not_before=PAST)
    result = maintainer(clone, []).tick()
    assert result.claimed == () and [r.kind for r in result.reports] == [ReportKind.NOT_CLAIMED]


def test_a_malformed_brief_warns_and_the_rest_still_run(tmp_path, capsys):
    clone = queue_repo(tmp_path)
    add_brief(clone, "bad", not_before="next tuesday")
    add_brief(clone, "good", not_before=PAST)
    assert maintainer(clone, []).tick().claimed == ("good",)
    assert "skipping bad.md" in capsys.readouterr().err


def test_a_brief_that_cannot_be_read_is_skipped_with_a_warning(tmp_path, monkeypatch, capsys):
    clone = queue_repo(tmp_path)
    add_brief(clone, "good", not_before=PAST)
    locked = add_brief(clone, "locked", not_before=PAST)
    original = pathlib.Path.read_text

    def read_text(self, *args, **kwargs):
        if self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    assert maintainer(clone, []).tick().claimed == ("good",)
    assert "locked.md" in capsys.readouterr().err


def test_a_lost_claim_is_reported_and_nothing_is_run(tmp_path, monkeypatch):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    monkeypatch.setattr(Claimer, "claim", lambda self, target, now: Claim(Outcome.LOST, "beaten"))
    spawned = []
    result = maintainer(clone, spawned).tick()
    assert spawned == [] and [str(r) for r in result.reports] == ["not-claimed b1: beaten"]


def test_a_launcher_that_cannot_start_is_reported_and_the_claim_stands(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)

    def broken(*args, **kwargs):
        raise OSError("no such launcher")

    result = maintainer(clone, [], spawn=broken).tick()
    assert result.claimed == ("b1",)
    assert [r.kind for r in result.reports] == [ReportKind.SPAWN_FAILED]
    assert "in-progress/b1.md" in origin_files(clone)


def test_with_no_launcher_nothing_is_claimed_and_the_tick_is_still_recorded(tmp_path, capsys):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    tick = Maintainer(clone, Tier.LAPTOP, settings_for(clone), clock=lambda: NOW)
    assert tick.tick().claimed == ()
    assert "no launcher is configured" in capsys.readouterr().err
    assert "ready/b1.md" in origin_files(clone) and LastRun.read(clone, "laptop") is not None


def test_a_configured_launcher_script_is_the_one_used(tmp_path, monkeypatch):
    clone = queue_repo(tmp_path)
    launcher = installed_launcher(tmp_path)
    monkeypatch.setenv("CLAUDE_TASKS_LAUNCHER", str(launcher))
    assert maintain_mod._launcher(clone) == ScriptLaunch(str(launcher))


def test_the_launcher_script_really_receives_the_run_detached(tmp_path, monkeypatch):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    seen = tmp_path / "seen.txt"
    stub = tmp_path / "claude-any.sh"
    stub.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > {seen}\nprintf "%s" "$CLAUDE_ANY_CALLER" > {seen}.caller\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_TASKS_LAUNCHER", str(stub))
    result = Maintainer(clone, Tier.LAPTOP, settings_for(clone), clock=lambda: NOW).tick()
    assert result.claimed == ("b1",)
    deadline = time.monotonic() + 10
    while not (tmp_path / "seen.txt.caller").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    lines = seen.read_text().splitlines()
    assert (
        lines[0] == "--dangerously-skip-permissions" and lines[1] == "/claude-tasks:action-task b1"
    )
    assert (tmp_path / "seen.txt.caller").read_text() == "claude-tasks-maintain"


def test_a_queue_that_cannot_be_refreshed_still_works_from_the_checkout(tmp_path, capsys):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    offline = Claimer(clone, "h", TTL, fetch=lambda root, branch: False)
    result = maintainer(clone, [], claimer=offline).tick()
    assert result.claimed == ()
    err = capsys.readouterr().err
    assert "could not bring the queue up to date" in err


def test_the_holder_names_the_tier_the_host_and_the_process():
    holder = default_holder(Tier.CLOUD)
    assert holder.startswith("cloud@") and holder.rsplit(":", 1)[1].isdigit()


def test_the_clock_is_utc():
    assert now_utc().utcoffset() == dt.timedelta(0)


def test_a_report_line_names_the_capability_when_it_has_one():
    assert (
        str(Report(ReportKind.OVERDUE_NEEDS, "b1", "needs aws", "aws"))
        == "overdue-needs-aws b1: needs aws"
    )
    assert str(Report(ReportKind.CLAIMED, "b1")) == "claimed b1"


def test_the_candidate_carries_its_target_and_wake():
    from claim import Target
    from wake import Wake

    wake = Wake("b1", None, "x", None, None, (), 0, True)
    candidate = Candidate(Target("b1", "ready", wake.key), wake)
    assert candidate.target.key == RunKey("b1", "probe", 0)


def test_main_runs_a_tick_and_prints_the_overdue_lines(tmp_path, monkeypatch, capsys):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before="2020-01-01", valid_until="2020-01-02")
    monkeypatch.setattr(maintain_mod, "_launcher", lambda root: ScriptLaunch("/stub"))
    assert maintain_mod.main(["--tier", "laptop", "--root", str(clone)]) == 0
    assert capsys.readouterr().out.strip() == "overdue b1: past valid_until; not run"


def test_main_never_crashes_a_scheduled_tick(tmp_path, monkeypatch, capsys):
    clone = queue_repo(tmp_path)

    def explode(self):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(Maintainer, "tick", explode)
    assert maintain_mod.main(["--tier", "cloud", "--root", str(clone)]) == 1
    assert "tick failed: RuntimeError: kaboom" in capsys.readouterr().err


def test_main_refuses_a_directory_that_is_not_a_queue(tmp_path):
    with pytest.raises(SystemExit):
        maintain_mod.main(["--tier", "laptop", "--root", str(tmp_path)])


def test_main_needs_a_tier():
    with pytest.raises(SystemExit):
        maintain_mod.main([])


def stub_tick(monkeypatch) -> list[str]:
    calls: list[str] = []

    def tick(self):
        calls.append(self.tier.value)
        return maintain_mod.TickResult(ran=True)

    monkeypatch.setattr(Maintainer, "tick", tick)
    return calls


def cloud_args(clone, *extra: str) -> list[str]:
    return ["--tier", "cloud", "--root", str(clone), *extra]


def test_skip_if_fresh_exits_zero_when_another_tier_ran_within_the_window(
    tmp_path, monkeypatch, capsys
):
    clone = queue_repo(tmp_path)
    LastRun("laptop", now_utc() - 5 * MINUTE).write(clone)
    calls = stub_tick(monkeypatch)
    assert maintain_mod.main(cloud_args(clone, "--skip-if-fresh", "60")) == 0
    assert capsys.readouterr().out.startswith("fresh, skipping")
    assert calls == [] and LastRun.read(clone, "cloud") is None


def test_skip_if_fresh_runs_the_tick_when_every_record_is_older_than_the_window(
    tmp_path, monkeypatch, capsys
):
    clone = queue_repo(tmp_path)
    LastRun("laptop", now_utc() - 61 * MINUTE).write(clone)
    calls = stub_tick(monkeypatch)
    assert maintain_mod.main(cloud_args(clone, "--skip-if-fresh", "60")) == 0
    assert calls == ["cloud"] and "skipping" not in capsys.readouterr().out


def test_without_skip_if_fresh_another_tiers_record_does_not_stop_the_tick(tmp_path, monkeypatch):
    clone = queue_repo(tmp_path)
    LastRun("laptop", now_utc() - MINUTE).write(clone)
    calls = stub_tick(monkeypatch)
    assert maintain_mod.main(cloud_args(clone)) == 0
    assert calls == ["cloud"]


def test_skip_if_fresh_needs_a_positive_number_of_minutes(tmp_path):
    clone = queue_repo(tmp_path)
    with pytest.raises(SystemExit):
        maintain_mod.main(cloud_args(clone, "--skip-if-fresh", "0"))


def test_never_due_readings_are_not_claimed(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", wake_probe="x")
    seen = []
    result = maintainer(
        clone, [], probe=lambda command: (seen.append(command), Reading(Due.NOT_YET, ""))[1]
    ).tick()
    assert seen == ["x"] and result.claimed == ()
    assert claim_mod.IN_PROGRESS == "in-progress"


def hygiene_engine(clone, runner, *steps):
    from hygiene import Engine

    return Engine(idle_engine(clone).where, steps, runner, lambda: NOW)


def test_the_tick_runs_the_tidy_up_after_the_wake_step_and_records_both(tmp_path):
    from tests.test_hygiene import A, Runner

    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    runner = Runner()
    result = maintainer(clone, [], engine=hygiene_engine(clone, runner, A)).tick()
    assert result.claimed == ("b1",) and runner.calls == ["a.py"]
    files = origin_files(clone)
    assert {"in-progress/b1.md", "maintain/last-run/deterministic.json"} <= files
    assert LastRun.read(clone, "laptop").claimed == ("b1",)


def test_what_the_tidy_up_needs_a_person_for_joins_the_tick_reports_and_its_record(tmp_path):
    from tests.test_hygiene import A, Runner

    clone = queue_repo(tmp_path)
    result = maintainer(
        clone, [], engine=hygiene_engine(clone, Runner(codes={"a.py": 2}), A)
    ).tick()
    assert [r.kind for r in result.reports] == [ReportKind.HYGIENE_FAILED]
    assert LastRun.read(clone, "laptop").reports[0].startswith("hygiene-failed deterministic:")


def test_a_crash_in_the_tidy_up_never_unrecords_the_wake_step(tmp_path):
    from tests.test_hygiene import A, Runner

    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)

    def crash(name):
        raise RuntimeError("tidy-up exploded")

    engine = hygiene_engine(clone, Runner(on_call=crash), A)
    with pytest.raises(RuntimeError, match="tidy-up exploded"):
        maintainer(clone, [], engine=engine).tick()
    assert LastRun.read(clone, "laptop").claimed == ("b1",) and "in-progress/b1.md" in origin_files(
        clone
    )


def test_with_no_engine_given_the_tick_runs_the_production_steps_as_subprocesses(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        hy, "STEPS", (Step("unblock", Phase.DETERMINISTIC, Home.PLUGIN, "unblock.py"),)
    )
    clone = queue_repo(tmp_path)
    assert maintainer(clone, [], engine=None).tick().reports == ()
    assert "maintain/last-run/deterministic.json" in origin_files(clone)


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (ReportKind.HYGIENE_FAILED, 1),
        (ReportKind.HYGIENE_LAPSED, 1),
        (ReportKind.HYGIENE_REFUSED, 1),
        (ReportKind.OVERDUE, 0),
    ],
)
def test_a_hygiene_report_that_needs_a_person_fails_the_exit_status(kind, status):
    assert maintain_mod.TickResult(True, reports=(Report(kind, "x"),)).exit_status == status


def test_a_deferred_phase_is_a_retry_later_status_and_a_failure_outranks_it():
    deferred = maintain_mod.TickResult(True, deferred=("judgement",))
    failed = dataclasses.replace(deferred, reports=(Report(ReportKind.HYGIENE_FAILED, "x"),))
    assert deferred.exit_status == maintain_mod.EX_TEMPFAIL == 75
    assert failed.exit_status == 1
    assert maintain_mod.TickResult(ran=False).exit_status == 0


def step_script(tmp_path, code):
    """A hygiene step that is a real script exiting with `code`."""
    script = tmp_path / f"step{code}.py"
    script.write_text(f"raise SystemExit({code})\n")
    return Step("probe", Phase.DETERMINISTIC, Home.PLUGIN, str(script))


def run_main(clone, monkeypatch, *steps):
    monkeypatch.setattr(hy, "STEPS", steps)
    monkeypatch.setattr(maintain_mod, "_launcher", lambda root: ScriptLaunch("/stub"))
    return maintain_mod.main(["--tier", "laptop", "--root", str(clone)])


def test_main_exits_zero_after_a_clean_tick_and_when_the_next_is_skipped_as_fresh(
    tmp_path, monkeypatch
):
    clone = queue_repo(tmp_path)
    assert run_main(clone, monkeypatch, step_script(tmp_path, 0)) == 0
    assert run_main(clone, monkeypatch, step_script(tmp_path, 3)) == 0


def test_main_exits_non_zero_and_prints_the_report_when_a_hygiene_step_fails(
    tmp_path, monkeypatch, capsys
):
    clone = queue_repo(tmp_path)
    assert run_main(clone, monkeypatch, step_script(tmp_path, 3)) == 1
    assert capsys.readouterr().out.startswith("hygiene-failed deterministic:")


def test_main_exits_zero_when_another_runner_holds_the_hygiene_lease(tmp_path, monkeypatch):
    clone = queue_repo(tmp_path)
    HygieneLeaser(Claimer(second_clone(tmp_path), "cloud@h:2", TTL)).acquire(now_utc())
    assert run_main(clone, monkeypatch, step_script(tmp_path, 0)) == 0
    assert "maintain/last-run/deterministic.json" not in origin_files(clone)


def test_main_exits_75_when_the_judgement_phase_is_deferred_for_want_of_credits(
    tmp_path, monkeypatch
):
    from tests.test_hygiene import NO_CAPACITY

    switch = tmp_path / "switch"
    switch.mkdir()
    (switch / "limit_state.py").write_text(NO_CAPACITY)
    monkeypatch.setenv("CLAUDE_SWITCH_HOME", str(switch))
    judge = dataclasses.replace(step_script(tmp_path, 0), phase=Phase.JUDGEMENT)
    clone = queue_repo(tmp_path)
    assert run_main(clone, monkeypatch, step_script(tmp_path, 0), judge) == 75


def test_a_brief_moved_away_mid_scan_is_skipped_and_the_rest_still_run(
    tmp_path, vanish_on_first_read
):
    clone = queue_repo(tmp_path)
    ghost = add_brief(clone, "a-ghost", not_before=PAST)
    add_brief(clone, "b-good", not_before=PAST)
    vanish_on_first_read(ghost)
    assert maintainer(clone, []).tick().claimed == ("b-good",)
