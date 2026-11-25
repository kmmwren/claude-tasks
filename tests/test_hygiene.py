"""The hygiene tiers: due logic, capacity, failure isolation and the lease they run under."""

import datetime as dt
import os
import pathlib
import runpy
import sys

import hygiene as hy
import pytest
from claim import Claim, Outcome
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

HOURS = dt.timedelta(hours=1)
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
    reports = hygiene_for(tmp_path, clone, (A, B, J), runner).run(NOW)
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
    hygiene_for(tmp_path, clone, (A,), runner).run(NOW)
    assert runner.timeouts == [pytest.approx(2 * 3600)]


def test_a_second_tick_inside_the_interval_does_nothing_at_all(tmp_path):
    clone = queue_repo(tmp_path)
    hygiene_for(tmp_path, clone, (A, J), Runner()).run(NOW)
    again, before = Runner(), origin_head(clone)
    assert hygiene_for(tmp_path, clone, (A, J), again).run(NOW + HOURS) == ()
    assert again.calls == [] and origin_head(clone) == before


def test_a_record_stamped_by_a_clock_ahead_does_not_hold_the_tiers_off(tmp_path):
    clone = queue_repo(tmp_path)
    LastRun("deterministic", NOW + dt.timedelta(days=3)).write(clone)
    runner = Runner()
    hygiene_for(tmp_path, clone, (A,), runner).run(NOW)
    assert runner.calls == ["a.py"]


def test_the_tiers_are_due_again_after_the_interval(tmp_path):
    clone = queue_repo(tmp_path)
    hygiene_for(tmp_path, clone, (A,), Runner()).run(NOW)
    runner = Runner()
    hygiene_for(tmp_path, clone, (A,), runner).run(NOW + INTERVAL)
    assert runner.calls == ["a.py"]


def test_judgement_is_deferred_untouched_when_no_account_has_capacity(tmp_path, capsys):
    clone, runner = queue_repo(tmp_path), Runner()
    (tmp_path / "switch").mkdir()
    (tmp_path / "switch" / "limit_state.py").write_text(NO_CAPACITY)
    assert hygiene_for(tmp_path, clone, (A, J), runner).run(NOW) == ()
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
    assert hygiene_for(tmp_path, clone, (A, J), runner).run(NOW) == ()
    assert runner.calls == [] and origin_head(clone) == before


def test_a_failed_step_is_reported_and_its_phase_is_not_recorded_but_the_others_still_run(tmp_path):
    clone, runner = queue_repo(tmp_path), Runner(codes={"a.py": 2})
    reports = hygiene_for(tmp_path, clone, (A, B, J), runner).run(NOW)
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
    assert hygiene_for(tmp_path, clone, (B,), Runner(codes={"b.py": 1})).run(NOW) == ()
    assert (
        hygiene_for(tmp_path, clone, (B,), Runner(codes={"b.py": 3})).run(NOW + INTERVAL)[0].kind
        is ReportKind.HYGIENE_FAILED
    )


def test_a_timed_out_step_is_a_failure(tmp_path):
    clone = queue_repo(tmp_path)
    reports = hygiene_for(tmp_path, clone, (A,), Runner(codes={"a.py": hy.TIMED_OUT})).run(NOW)
    assert kinds(reports) == [ReportKind.HYGIENE_FAILED]


def test_a_step_whose_script_is_missing_is_skipped_and_the_phase_remembers_it(tmp_path):
    clone, runner = queue_repo(tmp_path), Runner()
    assert hygiene_for(tmp_path, clone, (A, ROUTE), runner).run(NOW) == ()
    assert runner.calls == ["a.py"]
    assert LastRun.read(clone, "deterministic").reports == ("skipped route",)


def test_a_better_equipped_runner_finishes_what_an_earlier_one_skipped(tmp_path):
    clone = queue_repo(tmp_path)
    hygiene_for(tmp_path, clone, (A, ROUTE), Runner()).run(NOW)
    (tmp_path / "coordinator").mkdir()
    (tmp_path / "coordinator" / "router.py").write_text("")
    runner = Runner()
    equipped = where_for(tmp_path, clone, generic=clone)
    hygiene_for(tmp_path, clone, (A, ROUTE), runner, where=equipped).run(NOW + HOURS)
    assert runner.calls == ["a.py", "router.py"]
    assert LastRun.read(clone, "deterministic").reports == ()


def test_a_lease_held_by_another_runner_stands_us_down_quietly(tmp_path, capsys):
    clone = queue_repo(tmp_path)
    HygieneLeaser(claimer_for(second_clone(tmp_path), "cloud@h:2")).acquire(NOW)
    runner = Runner()
    assert hygiene_for(tmp_path, clone, (A,), runner).run(NOW) == ()
    assert runner.calls == [] and "hygiene not run: held by cloud@h:2" in capsys.readouterr().err


def test_a_push_the_remote_refuses_is_reported_so_a_person_can_fix_the_permission(tmp_path, capsys):
    clone = queue_repo(tmp_path)
    hook = tmp_path / "remote" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'policy says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    runner = Runner()
    reports = hygiene_for(tmp_path, clone, (A,), runner).run(NOW)
    assert kinds(reports) == [ReportKind.HYGIENE_REFUSED] and "policy says no" in reports[0].detail
    assert runner.calls == [] and "standing down" in capsys.readouterr().err


def test_results_the_remote_refuses_on_release_are_reported_not_applied_locally(tmp_path):
    clone = queue_repo(tmp_path)

    def decline_now(name):
        hook = tmp_path / "remote" / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'late policy' >&2\nexit 1\n")
        hook.chmod(0o755)

    reports = hygiene_for(tmp_path, clone, (A,), Runner(on_call=decline_now)).run(NOW)
    assert kinds(reports) == [ReportKind.HYGIENE_REFUSED] and "late policy" in reports[0].detail
    assert not (clone / "maintain" / "last-run" / "deterministic.json").exists()


def test_results_that_lose_the_lease_before_release_are_dropped_with_a_warning(tmp_path, capsys):
    clone = queue_repo(tmp_path)
    thief = HygieneLeaser(claimer_for(second_clone(tmp_path), "cloud@h:2"))
    steal = lambda name: thief.acquire(NOW + 3 * HOURS)  # noqa: E731
    reports = hygiene_for(tmp_path, clone, (A,), Runner(on_call=steal)).run(NOW)
    assert reports == () and "hygiene results not published" in capsys.readouterr().err
    assert "maintain/last-run/deterministic.json" not in origin_files(clone)


def test_a_lease_that_lapses_between_steps_stops_the_work_and_is_not_recorded_or_released(
    tmp_path, capsys
):
    clone, now = queue_repo(tmp_path), [NOW]
    runner = Runner(on_call=lambda name: now.__setitem__(0, NOW + 3 * HOURS))
    reports = hygiene_for(tmp_path, clone, (A, B, J), runner, clock=lambda: now[0]).run(NOW)
    assert runner.calls == ["a.py"] and kinds(reports) == [ReportKind.HYGIENE_LAPSED]
    assert "b: not started (the lease expired)" in capsys.readouterr().err
    assert lease_published(clone) and "maintain/last-run/deterministic.json" not in origin_files(
        clone
    )


def test_a_lease_that_lapses_during_the_last_step_is_not_recorded(tmp_path):
    clone, now = queue_repo(tmp_path), [NOW]
    runner = Runner(on_call=lambda name: now.__setitem__(0, NOW + 3 * HOURS))
    reports = hygiene_for(tmp_path, clone, (A,), runner, clock=lambda: now[0]).run(NOW)
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
        hygiene_for(tmp_path, clone, (A, J), Runner(on_call=crash)).run(NOW)
    files = origin_files(clone)
    assert (
        "maintain/last-run/deterministic.json" in files
        and "maintain/last-run/judgement.json" not in files
    )
    assert HYGIENE_LEASE not in files


def test_a_queue_that_cannot_be_brought_up_to_date_warns_and_still_runs(tmp_path, capsys):
    clone, runner = queue_repo(tmp_path), Runner()
    hygiene_for(tmp_path, clone, (A,), runner, fetch=lambda root, branch: False).run(NOW)
    assert runner.calls == ["a.py"]
    assert "could not bring the queue up to date" in capsys.readouterr().err


def test_a_phase_with_no_steps_is_never_due(tmp_path):
    clone, runner = queue_repo(tmp_path), Runner()
    before = origin_head(clone)
    assert hygiene_for(tmp_path, clone, (), runner).run(NOW) == ()
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
    assert h._not_released(Claim(Outcome.FAILED, "boom")) == ()
    assert "hygiene not run: boom" in capsys.readouterr().err
    assert kinds(h._not_taken(Claim(Outcome.REFUSED, "no"))) == [ReportKind.HYGIENE_REFUSED]
    assert kinds(h._not_released(Claim(Outcome.REFUSED, "no"))) == [ReportKind.HYGIENE_REFUSED]
