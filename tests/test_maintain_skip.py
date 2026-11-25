"""`maintain --skip-if-succeeded-within`: a cloud routine leaves the work to a run that recently succeeded on origin."""

import datetime as dt

import hygiene as hy
import maintain as maintain_mod
import pytest
from headless import ScriptLaunch
from hygiene import Phase
from maintain_config import Tier
from maintain_state import LastRun

from tests.maintain_helpers import (
    NOW,
    PAST,
    add_brief,
    maintainer,
    origin_files,
    origin_head,
    push_all,
    queue_repo,
    second_clone,
)
from tests.test_hygiene import A, Runner
from tests.test_maintain import hygiene_engine, spawned_ids, step_script

HOUR = dt.timedelta(hours=1)
WINDOW = 4 * HOUR


def publish_phase_records(clone, age, phases=tuple(Phase), now=NOW):
    """What a successful hygiene run leaves on origin: one record per phase it completed."""
    for phase in phases:
        LastRun(phase.value, now - age).write(clone)
    push_all(clone, "phase records")


def cloud_tick(clone, spawned, **overrides):
    return maintainer(
        clone, spawned, Tier.CLOUD, skip_if_succeeded_within=WINDOW, **overrides
    ).tick()


def test_a_recent_success_on_origin_runs_nothing_even_from_a_checkout_that_predates_it(tmp_path):
    laptop = queue_repo(tmp_path)
    add_brief(laptop, "b1", not_before=PAST)
    cloud = second_clone(tmp_path, "cloud")
    publish_phase_records(laptop, HOUR)
    before, spawned = origin_head(cloud), []
    result = cloud_tick(cloud, spawned)
    assert result == maintain_mod.TickResult(ran=False) and result.exit_status == 0
    assert spawned == [] and LastRun.read(cloud, "cloud") is None
    assert origin_head(cloud) == before and "in-progress/b1.md" not in origin_files(cloud)


def test_a_missing_record_runs_normally(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    spawned = []
    result = cloud_tick(clone, spawned)
    assert result.ran and result.claimed == ("b1",) and spawned_ids(spawned) == ["b1"]


def test_an_old_success_runs_normally(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    publish_phase_records(clone, WINDOW + HOUR)
    spawned = []
    assert cloud_tick(clone, spawned).claimed == ("b1",) and spawned_ids(spawned) == ["b1"]


def test_a_phase_that_never_completed_is_not_a_success(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    publish_phase_records(clone, HOUR, phases=(Phase.DETERMINISTIC,))
    spawned = []
    assert cloud_tick(clone, spawned).claimed == ("b1",) and spawned_ids(spawned) == ["b1"]


def test_a_recent_failure_leaves_no_record_and_runs_normally(tmp_path):
    clone = queue_repo(tmp_path)
    failing = hygiene_engine(clone, Runner(codes={"a.py": 2}), A)
    failed = maintainer(clone, [], engine=failing).tick()
    assert [r.kind for r in failed.reports] == [hy.ReportKind.HYGIENE_FAILED]
    assert LastRun.read(clone, "laptop") is not None  # the failed tick itself is recorded locally
    add_brief(clone, "b1", not_before=PAST)
    spawned = []
    assert cloud_tick(clone, spawned).claimed == ("b1",) and spawned_ids(spawned) == ["b1"]


def test_without_the_option_a_recent_success_changes_nothing(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    publish_phase_records(clone, HOUR)
    spawned = []
    assert maintainer(clone, spawned, Tier.CLOUD).tick().claimed == ("b1",)


def test_a_queue_with_no_origin_is_judged_on_its_own_checkout(tmp_path):
    root = tmp_path / "solo"
    root.mkdir()
    (root / "tasks.toml").write_text("")
    for phase in Phase:
        LastRun(phase.value, NOW - HOUR).write(root)
    assert cloud_tick(root, []) == maintain_mod.TickResult(ran=False)


def test_main_exits_zero_and_runs_nothing_when_a_recent_success_is_on_origin(
    tmp_path, monkeypatch, capsys
):
    laptop = queue_repo(tmp_path)
    cloud = second_clone(tmp_path, "cloud")
    publish_phase_records(laptop, dt.timedelta(minutes=1), now=maintain_mod.now_utc())
    monkeypatch.setattr(maintain_mod, "_launcher", lambda root: ScriptLaunch("/stub"))
    monkeypatch.setattr(hy, "STEPS", (step_script(tmp_path, 3),))
    argv = ["--tier", "cloud", "--root", str(cloud), "--skip-if-succeeded-within", "4h"]
    assert maintain_mod.main(argv) == 0
    assert LastRun.read(cloud, "cloud") is None and capsys.readouterr().out == ""


@pytest.mark.parametrize("text", ["soon", "4", "0h", "-1h", "1.5h", ""])
def test_main_refuses_a_window_that_is_not_a_duration(tmp_path, text, capsys):
    clone = queue_repo(tmp_path)
    with pytest.raises(SystemExit) as stop:
        maintain_mod.main(
            ["--tier", "cloud", "--root", str(clone), f"--skip-if-succeeded-within={text}"]
        )
    assert stop.value.code == 2 and "not a duration" in capsys.readouterr().err
