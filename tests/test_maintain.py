"""The maintain entry point: freshness, the lease, the two tiers, capacity and dry runs."""

import datetime
import pathlib
import subprocess
import sys
from collections.abc import Mapping, Sequence

import maintain as m
import pytest
from maintain_state import Counts, Lease, LocalExchange, RunRecord, Tier, open_exchange

from tests.maintain_helpers import (  # noqa: F401
    HOUR,
    T0,
    clone,
    isolated_git,
    make_origin,
    tier_run,
)

pytestmark = pytest.mark.usefixtures("isolated_git")

TTL = datetime.timedelta(minutes=30)
NO_CAPACITY = """
class Store:
    accounts = {'a': 1}

    def free(self, now=None):
        return []


def load():
    return Store()
"""


class FakeClock:
    def __init__(self, now: datetime.datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime.datetime:
        return self.now

    def advance(self, by: datetime.timedelta) -> None:
        self.now += by


class FakeRunner:
    """Records every step it is asked to run; any argv containing a `fail` marker exits 1."""

    def __init__(
        self,
        clock: FakeClock,
        fail: Sequence[str] = (),
        takes: datetime.timedelta = datetime.timedelta(0),
        codes: Mapping[str, int] | None = None,
    ) -> None:
        self.clock, self.fail, self.takes, self.codes = clock, fail, takes, codes or {}
        self.calls: list[tuple[list[str], dict[str, str], float]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], timeout: float) -> int:
        self.calls.append((list(argv), dict(env), timeout))
        self.clock.advance(self.takes)
        line = " ".join(argv[1:])
        return next(
            (code for marker, code in self.codes.items() if marker in line),
            int(any(marker in line for marker in self.fail)),
        )

    def scripts(self) -> list[str]:
        return [" ".join([pathlib.Path(argv[1]).name, *argv[2:]]) for argv, _, _ in self.calls]


def where(
    tmp_path: pathlib.Path,
    root: pathlib.Path | None = None,
    generic: pathlib.Path | None = None,
    switch: pathlib.Path | None = None,
) -> m.Environment:
    return m.Environment(
        root or tmp_path / "queue",
        tmp_path / "coordinator",
        generic or tmp_path / "generic",
        switch or tmp_path / "no-switch",
    )


def request(max_age: datetime.timedelta | None = None, dry_run: bool = False) -> m.Request:
    return m.Request("laptop", max_age, TTL, dry_run)


def no_capacity(tmp_path: pathlib.Path) -> pathlib.Path:
    home = tmp_path / "switch"
    home.mkdir()
    (home / "limit_state.py").write_text(NO_CAPACITY)
    return home


@pytest.fixture
def queue(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "queue"
    root.mkdir()
    return root


def test_both_tiers_run_and_are_recorded_when_nothing_is_recorded(
    tmp_path: pathlib.Path, queue: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    runner = FakeRunner(clock)
    exchange = LocalExchange(queue)
    assert m.maintain(request(), m.Engine(where(tmp_path), runner, clock), exchange) == 0
    assert runner.scripts() == [
        "unblock.py",
        "check_deps.py",
        "triage.py promote --apply",
        "build_board.py",
        "triage.py promote --apply --llm",
        "build_board.py",
    ]
    record = exchange.record()
    assert record.last(Tier.DETERMINISTIC) is not None
    assert record.last(Tier.DETERMINISTIC).counts == Counts(4, 0, 2)  # type: ignore[union-attr]
    assert record.last(Tier.JUDGEMENT) is not None
    assert not (queue / ".state" / "maintain.lease").exists()
    out = capsys.readouterr().out
    assert "route: skipped (router.py is not available here)" in out
    assert "deterministic: ok (4 ok, 0 failed, 2 skipped)" in out


def test_steps_see_the_queue_and_the_remaining_lease_as_their_timeout(
    tmp_path: pathlib.Path, queue: pathlib.Path
) -> None:
    clock = FakeClock()
    runner = FakeRunner(clock, takes=datetime.timedelta(minutes=1))
    m.maintain(request(), m.Engine(where(tmp_path), runner, clock), LocalExchange(queue))
    assert all(env["CLAUDE_TASKS_DIR"] == str(queue) for _, env, _ in runner.calls)
    assert [t for _, _, t in runner.calls][:2] == [1800.0, 1740.0]


def test_a_recent_enough_run_makes_the_tier_fresh_and_nothing_runs(
    tmp_path: pathlib.Path, queue: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock(T0 + HOUR)
    exchange = LocalExchange(queue)
    exchange.finish(
        Lease.take("cloud", T0, TTL), [tier_run(), tier_run(Tier.JUDGEMENT, runner="cloud")]
    )
    runner = FakeRunner(clock)
    code = m.maintain(request(4 * HOUR), m.Engine(where(tmp_path), runner, clock), exchange)
    assert code == 0
    assert runner.calls == []
    out = capsys.readouterr().out
    assert "deterministic: fresh, last run 2026-11-17 09:00Z (1h ago) by laptop" in out
    assert "judgement: fresh, last run 2026-11-17 09:00Z (1h ago) by cloud" in out
    assert not (queue / ".state" / "maintain.lease").exists()


def test_only_the_stale_tier_runs(tmp_path: pathlib.Path, queue: pathlib.Path) -> None:
    clock = FakeClock(T0 + 5 * HOUR)
    exchange = LocalExchange(queue)
    exchange.finish(
        Lease.take("laptop", T0, TTL), [tier_run(finished=T0 + 4 * HOUR), tier_run(Tier.JUDGEMENT)]
    )
    runner = FakeRunner(clock)
    m.maintain(request(2 * HOUR), m.Engine(where(tmp_path), runner, clock), exchange)
    assert runner.scripts() == ["triage.py promote --apply --llm", "build_board.py"]


def test_the_judgement_tier_is_deferred_untouched_when_no_account_has_capacity(
    tmp_path: pathlib.Path, queue: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    runner = FakeRunner(clock)
    exchange = LocalExchange(queue)
    engine = m.Engine(where(tmp_path, switch=no_capacity(tmp_path)), runner, clock)
    assert m.maintain(request(), engine, exchange) == 0
    assert "triage.py promote --apply --llm" not in runner.scripts()
    assert exchange.record().last(Tier.JUDGEMENT) is None
    assert exchange.record().last(Tier.DETERMINISTIC) is not None
    assert "judgement: deferred: no capacity" in capsys.readouterr().out


def test_deferring_the_only_due_tier_takes_no_lease_and_writes_nothing(
    tmp_path: pathlib.Path, queue: pathlib.Path
) -> None:
    clock = FakeClock(T0 + HOUR)
    exchange = LocalExchange(queue)
    exchange.finish(Lease.take("laptop", T0, TTL), [tier_run()])
    state = sorted(p.name for p in (queue / ".state").iterdir())
    runner = FakeRunner(clock)
    engine = m.Engine(where(tmp_path, switch=no_capacity(tmp_path)), runner, clock)
    assert m.maintain(request(4 * HOUR), engine, exchange) == 0
    assert runner.calls == []
    assert sorted(p.name for p in (queue / ".state").iterdir()) == state


def test_a_failed_step_fails_its_tier_without_stopping_the_rest_or_recording_it(
    tmp_path: pathlib.Path, queue: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    runner = FakeRunner(clock, fail=["unblock.py"])
    exchange = LocalExchange(queue)
    assert m.maintain(request(), m.Engine(where(tmp_path), runner, clock), exchange) == 1
    assert "build_board.py" in runner.scripts()
    record = exchange.record()
    assert record.last(Tier.DETERMINISTIC) is None
    assert record.last(Tier.JUDGEMENT) is not None
    out = capsys.readouterr().out
    assert "unblock: failed (exit 1)" in out
    assert "deterministic: failed (3 ok, 1 failed, 2 skipped)" in out


def test_a_failed_tier_is_retried_by_the_next_trigger(
    tmp_path: pathlib.Path, queue: pathlib.Path
) -> None:
    clock = FakeClock()
    exchange = LocalExchange(queue)
    broken = FakeRunner(clock, fail=["unblock.py"])
    m.maintain(request(4 * HOUR), m.Engine(where(tmp_path), broken, clock), exchange)
    healthy = FakeRunner(clock)
    m.maintain(request(4 * HOUR), m.Engine(where(tmp_path), healthy, clock), exchange)
    assert healthy.scripts() == [
        "unblock.py",
        "check_deps.py",
        "triage.py promote --apply",
        "build_board.py",
    ]


def test_check_deps_exiting_1_means_it_found_problems_not_that_it_failed(
    tmp_path: pathlib.Path, queue: pathlib.Path
) -> None:
    clock = FakeClock()
    runner = FakeRunner(clock, codes={"check_deps.py": 1})
    exchange = LocalExchange(queue)
    assert m.maintain(request(), m.Engine(where(tmp_path), runner, clock), exchange) == 0
    assert exchange.record().last(Tier.DETERMINISTIC) is not None


def test_a_timed_out_step_is_a_failure(tmp_path: pathlib.Path, queue: pathlib.Path) -> None:
    clock = FakeClock()
    runner = FakeRunner(clock, codes={"unblock.py": m.TIMED_OUT})
    assert (
        m.maintain(request(), m.Engine(where(tmp_path), runner, clock), LocalExchange(queue)) == 1
    )


def test_a_step_that_cannot_start_before_the_lease_expires_is_failed_not_run(
    tmp_path: pathlib.Path, queue: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    runner = FakeRunner(clock, takes=datetime.timedelta(minutes=31))
    code = m.maintain(request(), m.Engine(where(tmp_path), runner, clock), LocalExchange(queue))
    assert code == 1
    assert len(runner.calls) == 1
    assert "failed (the lease expired before it started)" in capsys.readouterr().out


def test_another_runners_live_lease_stands_this_one_down(
    tmp_path: pathlib.Path, queue: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    exchange = LocalExchange(queue)
    exchange.acquire("cloud", T0, TTL)
    runner = FakeRunner(clock)
    assert m.maintain(request(), m.Engine(where(tmp_path), runner, clock), exchange) == 0
    assert runner.calls == []
    assert "leased by cloud until 09:30Z" in capsys.readouterr().out


def test_a_runner_that_finished_while_this_one_waited_makes_the_tiers_fresh(
    tmp_path: pathlib.Path, queue: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class FinishedMeanwhile(LocalExchange):
        reads = 0

        def record(self) -> RunRecord:
            self.reads += 1
            if self.reads == 1:
                return RunRecord()
            return RunRecord().with_runs(
                [tier_run(runner="cloud"), tier_run(Tier.JUDGEMENT, runner="cloud")]
            )

    clock = FakeClock(T0 + HOUR)
    runner = FakeRunner(clock)
    exchange = FinishedMeanwhile(queue)
    assert m.maintain(request(4 * HOUR), m.Engine(where(tmp_path), runner, clock), exchange) == 0
    assert runner.calls == []
    assert "fresh, last run 2026-11-17 09:00Z (1h ago) by cloud" in capsys.readouterr().out
    assert not (queue / ".state" / "maintain.lease").exists()


def test_a_crash_mid_run_still_releases_the_lease(
    tmp_path: pathlib.Path, queue: pathlib.Path
) -> None:
    def crash(argv: Sequence[str], env: Mapping[str, str], timeout: float) -> int:
        raise RuntimeError("boom")

    engine = m.Engine(where(tmp_path), crash, FakeClock())
    with pytest.raises(RuntimeError, match="boom"):
        m.maintain(request(), engine, LocalExchange(queue))
    assert not (queue / ".state" / "maintain.lease").exists()


def test_a_dry_run_prints_the_plan_and_changes_nothing(
    tmp_path: pathlib.Path, queue: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = FakeClock()
    runner = FakeRunner(clock)
    code = m.maintain(
        request(dry_run=True), m.Engine(where(tmp_path), runner, clock), LocalExchange(queue)
    )
    out = capsys.readouterr().out
    assert code == 0
    assert runner.calls == []
    assert not (queue / ".state").exists()
    assert "deterministic: would run" in out
    assert "  unblock: unblock.py" in out
    assert "  triage-judge: triage.py promote --apply --llm" in out
    assert "  route: skipped (router.py is not available here)" in out


def test_a_dry_run_leaves_a_deferred_tier_out_of_the_plan(
    tmp_path: pathlib.Path, queue: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = m.Engine(where(tmp_path, switch=no_capacity(tmp_path)), FakeRunner(FakeClock()))
    m.maintain(request(dry_run=True), engine, LocalExchange(queue))
    out = capsys.readouterr().out
    assert "judgement: deferred: no capacity" in out
    assert "judgement: would run" not in out


def test_coordinator_steps_run_for_the_generic_queue_when_their_scripts_exist(
    tmp_path: pathlib.Path, queue: pathlib.Path
) -> None:
    coordinator = tmp_path / "coordinator"
    coordinator.mkdir()
    for script in ("blocker_recheck.py", "router.py"):
        (coordinator / script).write_text("")
    clock = FakeClock()
    runner = FakeRunner(clock)
    engine = m.Engine(where(tmp_path, generic=queue), runner, clock)
    m.maintain(request(), engine, LocalExchange(queue))
    assert runner.scripts()[2:4] == ["blocker_recheck.py run --all", "router.py tick --no-judge"]
    assert str(coordinator / "router.py") in runner.calls[3][0]


def test_home_locates_scripts_for_the_queue_and_machine_at_hand(
    tmp_path: pathlib.Path, queue: pathlib.Path
) -> None:
    coordinator = tmp_path / "coordinator"
    coordinator.mkdir()
    (coordinator / "router.py").write_text("")
    project = where(tmp_path)
    generic = where(tmp_path, generic=queue)
    assert m.Home.PLUGIN.locate("unblock.py", project) == m.SCRIPTS_DIR / "unblock.py"
    assert m.Home.COORDINATOR.locate("router.py", project) is None
    assert m.Home.COORDINATOR.locate("router.py", generic) == coordinator / "router.py"
    assert m.Home.COORDINATOR.locate("missing.py", generic) is None


def test_environment_defaults_and_overrides() -> None:
    home = pathlib.Path("~").expanduser()
    default = m.Environment.from_process(pathlib.Path("/q"), {})
    assert default.coordinator_dir == home / ".claude/tools/project-coordinator"
    assert default.generic_queue == home / "work-tasks"
    assert default.switch_home == home / ".claude-switch"
    custom = m.Environment.from_process(
        pathlib.Path("/q"),
        {
            "CLAUDE_TASKS_COORDINATOR_DIR": "/c",
            "WORK_TASKS_DIR": "/w",
            "CLAUDE_SWITCH_HOME": "/s",
        },
    )
    assert (custom.coordinator_dir, custom.generic_queue, custom.switch_home) == (
        pathlib.Path("/c"),
        pathlib.Path("/w"),
        pathlib.Path("/s"),
    )
    assert custom.is_generic_queue() is False
    assert m.Environment.from_process(
        pathlib.Path("/w"), {"WORK_TASKS_DIR": "/w"}
    ).is_generic_queue()


def test_environment_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORK_TASKS_DIR", "/from-process")
    assert m.Environment.from_process(pathlib.Path("/q")).generic_queue == pathlib.Path(
        "/from-process"
    )


def test_the_registry_is_sound() -> None:
    names = [s.name for s in m.STEPS]
    assert len(names) == len(set(names))
    assert {s.tier for s in m.STEPS} == set(Tier)
    assert [s.tier for s in m.STEPS] == sorted((s.tier for s in m.STEPS), key=list(Tier).index)
    for step in m.STEPS:
        if step.home is m.Home.PLUGIN:
            assert (m.SCRIPTS_DIR / step.script).is_file(), step.name
        assert step.ok_codes >= {0}


def test_a_step_shows_as_its_script_and_arguments() -> None:
    step = m.Step("x", Tier.DETERMINISTIC, m.Home.PLUGIN, "unblock.py", ("a", "--b"))
    assert step.shown() == "unblock.py a --b"
    command = step.command(where(pathlib.Path("/t")))
    assert command == [sys.executable, str(m.SCRIPTS_DIR / "unblock.py"), "a", "--b"]


def test_the_child_environment_names_the_queue_and_keeps_the_rest(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEEP_ME", "1")
    env = m.Engine(where(tmp_path)).child_env()
    assert env["CLAUDE_TASKS_DIR"] == str(tmp_path / "queue")
    assert env["KEEP_ME"] == "1"


def test_utc_now_is_timezone_aware() -> None:
    assert m.utc_now().utcoffset() == datetime.timedelta(0)


def test_the_real_runner_returns_the_exit_status_and_124_on_timeout() -> None:
    env: dict[str, str] = {}
    assert m.subprocess_runner([sys.executable, "-c", "raise SystemExit(3)"], env, 30) == 3
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    assert m.subprocess_runner(sleeper, env, 0.2) == m.TIMED_OUT


def make_queue(root: pathlib.Path) -> pathlib.Path:
    root.mkdir()
    (root / "tasks.toml").write_text('name = "T"\n')
    return root


def test_main_dry_runs_the_given_queue(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_SWITCH_HOME", str(tmp_path / "none"))
    root = make_queue(tmp_path / "q")
    code = m.main(["--root", str(root), "--runner", "laptop", "--dry-run", "--if-older-than", "4h"])
    assert code == 0
    assert "deterministic: would run" in capsys.readouterr().out
    assert not (root / ".state").exists()


def test_main_resolves_the_queue_when_none_is_given(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_SWITCH_HOME", str(tmp_path / "none"))
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(make_queue(tmp_path / "q")))
    assert m.main(["--dry-run", "--lease-ttl", "10m"]) == 0
    assert "would run" in capsys.readouterr().out


def test_main_refuses_something_that_is_not_a_queue(tmp_path: pathlib.Path) -> None:
    with pytest.raises(SystemExit, match="No task queue"):
        m.main(["--root", str(tmp_path / "nowhere"), "--dry-run"])


def test_main_rejects_a_duration_it_cannot_read() -> None:
    with pytest.raises(SystemExit):
        m.main(["--if-older-than", "soon"])


def runner_on(tmp_path: pathlib.Path, origin: pathlib.Path, name: str) -> pathlib.Path:
    return clone(tmp_path, origin, name)


def origin_record(origin: pathlib.Path) -> RunRecord:
    shown = subprocess.run(
        ["git", "-C", str(origin), "show", "main:.state/maintain.json"],
        capture_output=True,
        text=True,
    )
    return RunRecord.parse(shown.stdout if shown.returncode == 0 else "")


def test_a_second_runner_sees_the_first_runners_run_and_does_nothing(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    laptop, cloud = runner_on(tmp_path, origin, "laptop"), runner_on(tmp_path, origin, "cloud")
    clock = FakeClock()
    first = FakeRunner(clock)
    ran = m.maintain(
        m.Request("laptop", 4 * HOUR, TTL, False),
        m.Engine(where(tmp_path, root=laptop), first, clock),
        open_exchange(laptop),
    )
    clock.advance(HOUR)
    capsys.readouterr()
    second = FakeRunner(clock)
    skipped = m.maintain(
        m.Request("cloud", 4 * HOUR, TTL, False),
        m.Engine(where(tmp_path, root=cloud), second, clock),
        open_exchange(cloud),
    )
    out = capsys.readouterr().out
    assert (ran, skipped) == (0, 0)
    assert len(first.calls) == 6
    assert second.calls == []
    assert "deterministic: fresh, last run 2026-11-17 09:00Z (1h ago) by laptop" in out
    assert origin_record(origin).last(Tier.JUDGEMENT) is not None


def test_a_runner_that_starts_while_another_is_working_stands_down(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    laptop, cloud = runner_on(tmp_path, origin, "laptop"), runner_on(tmp_path, origin, "cloud")
    clock = FakeClock()
    cloud_runner, cloud_codes = FakeRunner(clock), []

    class Interrupting(FakeRunner):
        def __call__(self, argv: Sequence[str], env: Mapping[str, str], timeout: float) -> int:
            if not self.calls:
                engine = m.Engine(where(tmp_path, root=cloud), cloud_runner, clock)
                request = m.Request("cloud", 4 * HOUR, TTL, False)
                cloud_codes.append(m.maintain(request, engine, open_exchange(cloud)))
            return super().__call__(argv, env, timeout)

    laptop_runner = Interrupting(clock)
    m.maintain(
        m.Request("laptop", 4 * HOUR, TTL, False),
        m.Engine(where(tmp_path, root=laptop), laptop_runner, clock),
        open_exchange(laptop),
    )
    assert cloud_codes == [0]
    assert cloud_runner.calls == []
    assert len(laptop_runner.calls) == 6
    assert "leased by laptop until 09:30Z" in capsys.readouterr().out
    after = origin_record(origin)
    assert after.last(Tier.DETERMINISTIC).runner == "laptop"  # type: ignore[union-attr]
    shown = subprocess.run(
        ["git", "-C", str(origin), "show", "main:.state/maintain.lease"], capture_output=True
    )
    assert shown.returncode != 0
    assert after.last(Tier.JUDGEMENT) is not None
