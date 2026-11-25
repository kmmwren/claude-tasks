"""The hygiene tiers: due logic, capacity, failure isolation and the lease they run under."""

import datetime as dt
import os
import pathlib
import runpy
import subprocess
import sys
import time

import hygiene as hy
import hygiene_lease as hl
import pytest
from claim import Claim, Lease, Outcome
from hygiene import Engine, Environment, Home, Hygiene, Phase, PhaseOutcome, Step
from hygiene_lease import HYGIENE_LEASE, HygieneLeaser
from maintain_report import ReportKind
from maintain_state import LastRun

from tests.maintain_helpers import (
    NOW,
    claimer_for,
    origin_files,
    origin_head,
    queue_repo,
    second_clone,
)
from tests.test_git_sync import git

HOURS = dt.timedelta(hours=1)
LEASE = Lease("laptop@h:1", NOW + 2 * HOURS, 1)
INTERVAL = 4 * HOURS
A = Step("a", Phase.DETERMINISTIC, Home.PLUGIN, "a.py")
B = Step("b", Phase.DETERMINISTIC, Home.PLUGIN, "b.py", ok_codes=frozenset({0, 1}))
ROUTE = Step("route", Phase.DETERMINISTIC, Home.COORDINATOR, "router.py")
J = Step("j", Phase.JUDGEMENT, Home.PLUGIN, "j.py")
NO_CAPACITY = "class S:\n    accounts = {'x': 1}\n\n    def free(self, now=None):\n        return []\n\n\ndef load():\n    return S()\n"


class Runner:
    """A step runner that records what ran, answers with set exit codes and can act mid-step."""

    def __init__(self, codes=None, on_call=None):
        self.calls: list[str] = []
        self.codes = codes or {}
        self.on_call = on_call
        self.env: dict[str, str] = {}
        self.timeouts: list[float] = []

    def __call__(self, argv, env, timeout):
        name = pathlib.Path(argv[1]).name
        self.calls.append(name)
        self.env, self.timeouts = dict(env), [*self.timeouts, timeout]
        if self.on_call:
            self.on_call(name)
        return self.codes.get(name, 0)


def where_for(tmp_path, root, generic=None):
    return Environment(
        root, tmp_path / "coordinator", generic or tmp_path / "generic", tmp_path / "switch"
    )


def hygiene_for(tmp_path, clone, steps, runner, clock=None, where=None, holder="laptop@h:1", **kw):
    engine = Engine(where or where_for(tmp_path, clone), steps, runner, clock or (lambda: NOW))
    return Hygiene(clone, INTERVAL, engine, HygieneLeaser(claimer_for(clone, holder, **kw)))


def kinds(reports):
    return [r.kind for r in reports]


def lease_published(clone):
    return HYGIENE_LEASE in origin_files(clone)


def test_only_judgement_needs_capacity():
    assert [p.needs_capacity for p in Phase] == [False, True]


def test_the_environment_reads_its_places_from_the_process_environment(tmp_path):
    env = {
        hy.COORDINATOR_ENV: "~/c",
        hy.GENERIC_QUEUE_ENV: str(tmp_path / "g"),
        "CLAUDE_SWITCH_HOME": str(tmp_path / "s"),
    }
    where = Environment.from_process(tmp_path, env)
    assert where.coordinator_dir == pathlib.Path("~/c").expanduser()
    assert where.generic_queue == tmp_path / "g" and where.switch_home == tmp_path / "s"


def test_the_environment_defaults_to_the_users_own_places(tmp_path):
    where = Environment.from_process(tmp_path, {})
    assert where.generic_queue == pathlib.Path(hy.GENERIC_QUEUE).expanduser()
    assert where.coordinator_dir == pathlib.Path(hy.COORDINATOR_DIR).expanduser()


def test_the_process_environment_is_used_when_none_is_given(tmp_path, monkeypatch):
    monkeypatch.setenv(hy.GENERIC_QUEUE_ENV, str(tmp_path / "g"))
    assert Environment.from_process(tmp_path).generic_queue == tmp_path / "g"


def test_a_plugin_script_is_always_available_and_a_coordinator_one_only_for_the_generic_queue(
    tmp_path,
):
    (tmp_path / "coordinator").mkdir()
    (tmp_path / "coordinator" / "router.py").write_text("")
    project = where_for(tmp_path, tmp_path / "queue")
    generic = where_for(tmp_path, tmp_path / "generic")
    assert Home.PLUGIN.locate("unblock.py", project) == hy.SCRIPTS_DIR / "unblock.py"
    assert Home.COORDINATOR.locate("router.py", project) is None
    assert Home.COORDINATOR.locate("router.py", generic) == tmp_path / "coordinator" / "router.py"
    assert Home.COORDINATOR.locate("missing.py", generic) is None


def test_a_step_command_is_the_script_with_its_args_or_none(tmp_path):
    step = Step("s", Phase.DETERMINISTIC, Home.PLUGIN, "x.py", ("--all",))
    where = where_for(tmp_path, tmp_path)
    assert step.command(where) == [sys.executable, str(hy.SCRIPTS_DIR / "x.py"), "--all"]
    assert ROUTE.command(where) is None


def test_the_real_step_table_runs_deterministic_before_judgement_and_names_real_plugin_scripts():
    real = runpy.run_path(hy.__file__)
    order = list(real["Phase"])
    phases = [step.phase for step in real["STEPS"]]
    assert phases == sorted(phases, key=order.index)
    plugin = [step for step in real["STEPS"] if step.home is real["Home"].PLUGIN]
    assert plugin and all((hy.SCRIPTS_DIR / step.script).is_file() for step in plugin)


def test_the_production_engine_runs_the_step_table_as_subprocesses(tmp_path, monkeypatch):
    monkeypatch.setattr(hy, "STEPS", (A,))
    engine = Engine.for_queue(tmp_path, clock=lambda: NOW)
    assert engine.steps == (A,) and engine.runner is hy.subprocess_runner and engine.clock() == NOW


def test_the_child_env_points_every_step_at_the_same_queue(tmp_path):
    engine = Engine(where_for(tmp_path, tmp_path), (A,))
    assert engine.child_env()["CLAUDE_TASKS_DIR"] == str(tmp_path)
    assert engine.child_env()["PATH"] == os.environ["PATH"]


def test_a_skipped_step_can_be_run_only_where_its_script_exists(tmp_path):
    (tmp_path / "coordinator").mkdir()
    (tmp_path / "coordinator" / "router.py").write_text("")
    here = Engine(where_for(tmp_path, tmp_path / "q"), (A, ROUTE))
    there = Engine(where_for(tmp_path, tmp_path / "generic"), (A, ROUTE))
    assert not here.can_run_skipped(Phase.DETERMINISTIC, ["route"])
    assert there.can_run_skipped(Phase.DETERMINISTIC, ["route"])
    assert not there.can_run_skipped(Phase.DETERMINISTIC, [])


def test_the_subprocess_runner_returns_the_exit_status_and_124_on_a_timeout():
    run = hy.subprocess_runner
    assert run([sys.executable, "-c", "raise SystemExit(3)"], os.environ, 30) == 3
    assert (
        run([sys.executable, "-c", "import time; time.sleep(30)"], os.environ, 0.3) == hy.TIMED_OUT
    )


def test_the_clock_is_utc():
    assert hy.utc_now().utcoffset() == dt.timedelta(0)


def test_a_due_tick_runs_every_step_and_records_both_phases_with_the_lease_released(tmp_path):
    clone, runner = queue_repo(tmp_path), Runner()
    reports = hygiene_for(tmp_path, clone, (A, B, J), runner).run(NOW).reports
    assert reports == () and runner.calls == ["a.py", "b.py", "j.py"]
    assert runner.env["CLAUDE_TASKS_DIR"] == str(clone)
    assert {*origin_files(clone)} >= {
        "maintain/last-run/deterministic.json",
        "maintain/last-run/judgement.json",
    }
    assert not lease_published(clone)
    assert LastRun.read(clone, "judgement").at == NOW


def test_each_step_is_given_only_the_time_left_on_the_lease(tmp_path):
    clone, runner = queue_repo(tmp_path), Runner()
    hygiene_for(tmp_path, clone, (A,), runner).run(NOW).reports
    assert runner.timeouts == [pytest.approx(2 * 3600)]


def test_a_second_tick_inside_the_interval_does_nothing_at_all(tmp_path):
    clone = queue_repo(tmp_path)
    hygiene_for(tmp_path, clone, (A, J), Runner()).run(NOW).reports
    again, before = Runner(), origin_head(clone)
    assert hygiene_for(tmp_path, clone, (A, J), again).run(NOW + HOURS).reports == ()
    assert again.calls == [] and origin_head(clone) == before


def test_a_record_stamped_by_a_clock_ahead_does_not_hold_the_tiers_off(tmp_path):
    clone = queue_repo(tmp_path)
    LastRun("deterministic", NOW + dt.timedelta(days=3)).write(clone)
    runner = Runner()
    hygiene_for(tmp_path, clone, (A,), runner).run(NOW).reports
    assert runner.calls == ["a.py"]


def test_the_tiers_are_due_again_after_the_interval(tmp_path):
    clone = queue_repo(tmp_path)
    hygiene_for(tmp_path, clone, (A,), Runner()).run(NOW).reports
    runner = Runner()
    hygiene_for(tmp_path, clone, (A,), runner).run(NOW + INTERVAL).reports
    assert runner.calls == ["a.py"]


def test_judgement_is_deferred_untouched_when_no_account_has_capacity(tmp_path, capsys):
    clone, runner = queue_repo(tmp_path), Runner()
    (tmp_path / "switch").mkdir()
    (tmp_path / "switch" / "limit_state.py").write_text(NO_CAPACITY)
    assert hygiene_for(tmp_path, clone, (A, J), runner).run(NOW).reports == ()
    assert (
        runner.calls == ["a.py"] and "judgement: deferred, no capacity" in capsys.readouterr().err
    )
    files = origin_files(clone)
    assert (
        "maintain/last-run/deterministic.json" in files
        and "maintain/last-run/judgement.json" not in files
    )


def test_when_only_judgement_is_due_and_there_is_no_capacity_nothing_is_published(tmp_path):
    clone, runner = queue_repo(tmp_path), Runner()
    LastRun("deterministic", NOW).write(clone)
    (tmp_path / "switch").mkdir()
    (tmp_path / "switch" / "limit_state.py").write_text(NO_CAPACITY)
    before = origin_head(clone)
    assert hygiene_for(tmp_path, clone, (A, J), runner).run(NOW).reports == ()
    assert runner.calls == [] and origin_head(clone) == before


def test_a_failed_step_is_reported_and_its_phase_is_not_recorded_but_the_others_still_run(tmp_path):
    clone, runner = queue_repo(tmp_path), Runner(codes={"a.py": 2})
    reports = hygiene_for(tmp_path, clone, (A, B, J), runner).run(NOW).reports
    assert runner.calls == ["a.py", "b.py", "j.py"]
    assert kinds(reports) == [ReportKind.HYGIENE_FAILED]
    assert reports[0].brief_id == "deterministic" and "1 ok, 1 failed" in reports[0].detail
    files = origin_files(clone)
    assert (
        "maintain/last-run/deterministic.json" not in files
        and "maintain/last-run/judgement.json" in files
    )


def test_an_exit_status_a_step_declares_acceptable_is_not_a_failure(tmp_path):
    clone = queue_repo(tmp_path)
    assert hygiene_for(tmp_path, clone, (B,), Runner(codes={"b.py": 1})).run(NOW).reports == ()
    assert (
        hygiene_for(tmp_path, clone, (B,), Runner(codes={"b.py": 3}))
        .run(NOW + INTERVAL)
        .reports[0]
        .kind
        is ReportKind.HYGIENE_FAILED
    )


def test_a_timed_out_step_is_a_failure(tmp_path):
    clone = queue_repo(tmp_path)
    reports = (
        hygiene_for(tmp_path, clone, (A,), Runner(codes={"a.py": hy.TIMED_OUT})).run(NOW).reports
    )
    assert kinds(reports) == [ReportKind.HYGIENE_FAILED]


def test_a_step_whose_script_is_missing_is_skipped_and_the_phase_remembers_it(tmp_path):
    clone, runner = queue_repo(tmp_path), Runner()
    assert hygiene_for(tmp_path, clone, (A, ROUTE), runner).run(NOW).reports == ()
    assert runner.calls == ["a.py"]
    assert LastRun.read(clone, "deterministic").reports == ("skipped route",)


def test_a_better_equipped_runner_finishes_what_an_earlier_one_skipped(tmp_path):
    clone = queue_repo(tmp_path)
    hygiene_for(tmp_path, clone, (A, ROUTE), Runner()).run(NOW).reports
    (tmp_path / "coordinator").mkdir()
    (tmp_path / "coordinator" / "router.py").write_text("")
    runner = Runner()
    equipped = where_for(tmp_path, clone, generic=clone)
    hygiene_for(tmp_path, clone, (A, ROUTE), runner, where=equipped).run(NOW + HOURS).reports
    assert runner.calls == ["a.py", "router.py"]
    assert LastRun.read(clone, "deterministic").reports == ()


def test_a_lease_held_by_another_runner_stands_us_down_quietly(tmp_path, capsys):
    clone = queue_repo(tmp_path)
    HygieneLeaser(claimer_for(second_clone(tmp_path), "cloud@h:2")).acquire(NOW)
    runner = Runner()
    assert hygiene_for(tmp_path, clone, (A,), runner).run(NOW).reports == ()
    assert runner.calls == [] and "hygiene not run: held by cloud@h:2" in capsys.readouterr().err


def test_a_push_the_remote_refuses_is_reported_so_a_person_can_fix_the_permission(tmp_path, capsys):
    clone = queue_repo(tmp_path)
    hook = tmp_path / "remote" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'policy says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    runner = Runner()
    reports = hygiene_for(tmp_path, clone, (A,), runner).run(NOW).reports
    assert kinds(reports) == [ReportKind.HYGIENE_REFUSED] and "policy says no" in reports[0].detail
    assert runner.calls == [] and "standing down" in capsys.readouterr().err


def test_results_the_remote_refuses_on_release_are_reported_not_applied_locally(tmp_path):
    clone = queue_repo(tmp_path)

    def decline_now(name):
        hook = tmp_path / "remote" / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'late policy' >&2\nexit 1\n")
        hook.chmod(0o755)

    reports = hygiene_for(tmp_path, clone, (A,), Runner(on_call=decline_now)).run(NOW).reports
    assert kinds(reports) == [ReportKind.HYGIENE_REFUSED] and "late policy" in reports[0].detail
    assert not (clone / "maintain" / "last-run" / "deterministic.json").exists()


def test_results_that_lose_the_lease_before_release_are_dropped_with_a_warning(tmp_path, capsys):
    clone = queue_repo(tmp_path)
    thief = HygieneLeaser(claimer_for(second_clone(tmp_path), "cloud@h:2"))
    steal = lambda name: thief.acquire(NOW + 3 * HOURS)  # noqa: E731
    reports = hygiene_for(tmp_path, clone, (A,), Runner(on_call=steal)).run(NOW).reports
    assert reports == () and "hygiene results not published" in capsys.readouterr().err
    assert "maintain/last-run/deterministic.json" not in origin_files(clone)


def test_a_lease_that_lapses_between_steps_stops_the_work_and_is_not_recorded_or_released(
    tmp_path, capsys
):
    clone, now = queue_repo(tmp_path), [NOW]
    runner = Runner(on_call=lambda name: now.__setitem__(0, NOW + 3 * HOURS))
    reports = hygiene_for(tmp_path, clone, (A, B, J), runner, clock=lambda: now[0]).run(NOW).reports
    assert runner.calls == ["a.py"] and kinds(reports) == [ReportKind.HYGIENE_LAPSED]
    assert "b: not started (the lease expired)" in capsys.readouterr().err
    assert lease_published(clone) and "maintain/last-run/deterministic.json" not in origin_files(
        clone
    )


def test_a_lease_that_lapses_during_the_last_step_is_not_recorded(tmp_path):
    clone, now = queue_repo(tmp_path), [NOW]
    runner = Runner(on_call=lambda name: now.__setitem__(0, NOW + 3 * HOURS))
    reports = hygiene_for(tmp_path, clone, (A,), runner, clock=lambda: now[0]).run(NOW).reports
    assert kinds(reports) == [ReportKind.HYGIENE_LAPSED]
    assert lease_published(clone) and "maintain/last-run/deterministic.json" not in origin_files(
        clone
    )


def test_a_crash_in_judgement_still_records_the_finished_deterministic_phase(tmp_path):
    clone = queue_repo(tmp_path)

    def crash(name):
        if name == "j.py":
            raise RuntimeError("judge exploded")

    with pytest.raises(RuntimeError, match="judge exploded"):
        hygiene_for(tmp_path, clone, (A, J), Runner(on_call=crash)).run(NOW).reports
    files = origin_files(clone)
    assert (
        "maintain/last-run/deterministic.json" in files
        and "maintain/last-run/judgement.json" not in files
    )
    assert HYGIENE_LEASE not in files


def test_a_queue_that_cannot_be_brought_up_to_date_warns_and_still_runs(tmp_path, capsys):
    clone, runner = queue_repo(tmp_path), Runner()
    hygiene_for(tmp_path, clone, (A,), runner, fetch=lambda root, branch: False).run(NOW).reports
    assert runner.calls == ["a.py"]
    assert "could not bring the queue up to date" in capsys.readouterr().err


def test_a_phase_with_no_steps_is_never_due(tmp_path):
    clone, runner = queue_repo(tmp_path), Runner()
    before = origin_head(clone)
    assert hygiene_for(tmp_path, clone, (), runner).run(NOW).reports == ()
    assert origin_head(clone) == before


def test_a_phase_run_summarises_what_its_steps_did(tmp_path):
    engine = Engine(
        where_for(tmp_path, tmp_path), (A, ROUTE, B), Runner(codes={"b.py": 7}), lambda: NOW
    )
    lease = HygieneLeaser(claimer_for(queue_repo(tmp_path), "h")).acquire(NOW).lease
    run = hy.run_phase(Phase.DETERMINISTIC, engine, lease)
    assert run.outcome is PhaseOutcome.FAILED and not run.recordable
    assert run.summary() == "failed (1 ok, 1 failed, 1 skipped)"
    assert run.last_run(NOW) == LastRun("deterministic", NOW, (), ("skipped route",))
    assert hy.skipped_in(run.last_run(NOW)) == ("route",)


def test_not_taken_and_not_released_distinguish_refusal_from_a_race(capsys):
    h = Hygiene.__new__(Hygiene)
    assert h._not_taken(Claim(Outcome.FAILED, "boom")) == ()
    assert h._not_released(Claim(Outcome.FAILED, "boom"), LEASE) == ()
    assert "hygiene not run: boom" in capsys.readouterr().err
    assert kinds(h._not_taken(Claim(Outcome.REFUSED, "no"))) == [ReportKind.HYGIENE_REFUSED]
    refused = h._not_released(Claim(Outcome.REFUSED, "no"), LEASE)
    assert kinds(refused) == [ReportKind.HYGIENE_REFUSED]


def local_queue(tmp_path):
    solo = tmp_path / "solo"
    solo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(solo)], check=True)
    return solo


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_timed_out_step_takes_the_children_it_started_with_it(tmp_path):
    pidfile = tmp_path / "child.pid"
    parent = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(60)\n"
    )
    assert hy.subprocess_runner([sys.executable, "-c", parent], os.environ, 5) == hy.TIMED_OUT
    child = int(pidfile.read_text())
    try:
        deadline = time.monotonic() + 10
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive(child)
    finally:
        if alive(child):
            os.kill(child, 9)


def test_a_phase_another_runner_recorded_since_this_checkout_was_cut_is_not_run_again(tmp_path):
    clone = queue_repo(tmp_path)
    other = hygiene_for(tmp_path, second_clone(tmp_path), (A,), Runner(), holder="cloud@h:2")
    other.run(NOW)
    assert LastRun.read(clone, "deterministic") is None
    runner = Runner()
    assert hygiene_for(tmp_path, clone, (A,), runner).run(NOW + HOURS).reports == ()
    assert runner.calls == [] and not lease_published(clone)


def test_a_phase_recorded_while_the_lease_was_being_taken_is_not_run_again(tmp_path):
    clone = queue_repo(tmp_path)
    other = hygiene_for(tmp_path, second_clone(tmp_path), (A, J), Runner(), holder="cloud@h:2")

    class Racing(HygieneLeaser):
        def acquire(self, now):
            other.run(NOW)
            return super().acquire(now)

    runner = Runner()
    engine = Engine(where_for(tmp_path, clone), (A, J), runner, lambda: NOW)
    racing = Racing(claimer_for(clone, "laptop@h:1"))
    assert Hygiene(clone, INTERVAL, engine, racing).run(NOW).reports == ()
    assert runner.calls == [] and not lease_published(clone)
    assert LastRun.read(clone, "judgement").at == NOW


def test_a_queue_below_its_repo_root_is_refused_not_leased_at_the_wrong_path(tmp_path, capsys):
    clone = queue_repo(tmp_path)
    nested = clone / "queue"
    nested.mkdir()
    runner, before = Runner(), origin_head(clone)
    reports = hygiene_for(tmp_path, nested, (A,), runner).run(NOW).reports
    assert kinds(reports) == [ReportKind.HYGIENE_REFUSED] and "not the root" in reports[0].detail
    assert runner.calls == [] and origin_head(clone) == before
    assert "standing down" in capsys.readouterr().err


def test_a_queue_with_no_origin_runs_its_steps_under_a_local_record_and_warns(tmp_path, capsys):
    solo, runner = local_queue(tmp_path), Runner()
    assert hygiene_for(tmp_path, solo, (A, B), runner).run(NOW).reports == ()
    assert runner.calls == ["a.py", "b.py"]
    assert LastRun.read(solo, "deterministic").at == NOW
    assert not (solo / HYGIENE_LEASE).exists()
    assert "no origin" in capsys.readouterr().err
    again = Runner()
    hygiene_for(tmp_path, solo, (A, B), again).run(NOW + HOURS)
    assert again.calls == []


def test_a_local_queue_still_reports_a_failed_step_and_does_not_record_it(tmp_path):
    solo = local_queue(tmp_path)
    reports = hygiene_for(tmp_path, solo, (A,), Runner(codes={"a.py": 2})).run(NOW).reports
    assert kinds(reports) == [ReportKind.HYGIENE_FAILED]
    assert LastRun.read(solo, "deterministic") is None


def test_judgement_deferred_for_want_of_credits_is_part_of_the_result(tmp_path):
    clone = queue_repo(tmp_path)
    (tmp_path / "switch").mkdir()
    (tmp_path / "switch" / "limit_state.py").write_text(NO_CAPACITY)
    result = hygiene_for(tmp_path, clone, (A, J), Runner()).run(NOW)
    assert result.deferred == (Phase.JUDGEMENT,) and result.reports == ()


def test_a_release_that_keeps_failing_warns_where_the_lease_stays_and_is_tried_a_few_times(
    tmp_path, capsys, monkeypatch
):
    clone = queue_repo(tmp_path)
    monkeypatch.setattr(hl, "RELEASE_PAUSE", 0.0)
    gone = str(tmp_path / "gone")
    runner = Runner(on_call=lambda name: git(clone, "remote", "set-url", "origin", gone))
    assert hygiene_for(tmp_path, clone, (A,), runner).run(NOW).reports == ()
    err = capsys.readouterr().err
    assert "hygiene results not published" in err
    assert "any lease of ours stays on origin until it lapses at 11:00Z" in err
