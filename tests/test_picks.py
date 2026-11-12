"""Tests for picks.py and log_pick.py: the pick log lives outside the repo and links choices to lists."""
import datetime
import pathlib

import bump
import log_pick
import picks
import transition
from picks import Choice, PickLog, Shown, ShownItem

from tests.queue_helpers import make_queue

NOW = datetime.datetime(2026, 11, 11, 12, 0, tzinfo=datetime.timezone.utc)


def shown(ids, impression="imp1", at="2026-11-11T11:00:00+00:00"):
    return Shown("all", "v1", "judge", tuple(ShownItem(i, ("G",)) for i in ids), tuple(ids), ("raw",),
                 impression, at)


def test_state_dir_is_one_per_queue_under_the_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(picks.STATE_ENV, str(tmp_path / "s"))
    first, second = picks.state_dir(tmp_path / "q1"), picks.state_dir(tmp_path / "q2")
    assert (first.parent, first != second, len(first.name)) == (tmp_path / "s", True, 12)


def test_state_dir_defaults_to_the_home_state_tree(monkeypatch):
    monkeypatch.delenv(picks.STATE_ENV)
    assert picks.state_dir(pathlib.Path("/q")).parent == pathlib.Path("~/.claude-tasks/predictor").expanduser()


def test_events_round_trip_in_order(tmp_path):
    log = PickLog(tmp_path)
    s, c = shown(["a", "b"]), Choice("pick", "b", "imp1", 1, "2026-11-11T11:05:00+00:00")
    log.append(s)
    log.append(c)
    assert (list(log.events()), log.shown(), log.choices()) == ([s, c], [s], [c])


def test_a_missing_log_has_no_events_and_blank_lines_are_skipped(tmp_path):
    log = PickLog(tmp_path)
    assert list(log.events()) == []
    log.append(shown(["a"]))
    with log.path.open("a") as fh:
        fh.write("\n")
    assert len(list(log.events())) == 1


def test_position_is_none_for_a_brief_not_shown():
    assert (shown(["a", "b"]).position("b"), shown(["a"]).position("z")) == (1, None)


def test_a_choice_links_to_the_latest_list_that_showed_the_brief(tmp_path):
    log = PickLog(tmp_path)
    log.append(shown(["a", "b"], impression="old", at="2026-11-11T09:00:00+00:00"))
    log.append(shown(["c", "b"], impression="new"))
    assert log.record_choice("pick", "b", NOW) == Choice("pick", "b", "new", 1, "2026-11-11T12:00:00+00:00")


def test_a_choice_outside_the_window_or_never_shown_is_off_list(tmp_path):
    log = PickLog(tmp_path)
    log.append(shown(["a"], at="2026-11-10T09:00:00+00:00"))
    assert (log.record_choice("pick", "a", NOW).impression, log.record_choice("skip", "z", NOW).position) == (None, None)
    assert len(log.choices()) == 2


def test_record_choice_stamps_now_in_utc(tmp_path):
    assert PickLog(tmp_path).record_choice("bump", "a").at.endswith("+00:00")


def test_note_choice_records(tmp_path):
    picks.note_choice(tmp_path, "pick", "a")
    assert [c.brief_id for c in PickLog(tmp_path).choices()] == ["a"]


def test_note_choice_warns_instead_of_failing_the_move(tmp_path, monkeypatch, capsys):
    (tmp_path / "file").write_text("a file where the state dir should be")
    monkeypatch.setenv(picks.STATE_ENV, str(tmp_path / "file"))
    picks.note_choice(tmp_path, "pick", "a")
    assert capsys.readouterr().err.startswith("warning: pick log not updated (")


def test_moving_a_brief_to_in_progress_logs_a_pick(tmp_path):
    root = make_queue(tmp_path, {"a": {}, "b": {}})
    PickLog(root).append(shown(["b", "a"], at=datetime.datetime.now(datetime.timezone.utc).isoformat()))
    transition.main(["move", "a", "in-progress", "--root", str(root), "--no-sync"])
    transition.main(["move", "b", "parked", "--root", str(root), "--no-sync"])
    assert [(c.kind, c.brief_id, c.position) for c in PickLog(root).choices()] == [("pick", "a", 1)]


def test_bumping_a_brief_logs_a_bump(tmp_path):
    for folder in bump.FOLDERS:
        (tmp_path / folder).mkdir()
    (tmp_path / "ready" / "b1.md").write_text("---\ntags: [x]\nimportance: 3\n---\nbody\n")
    bump.bump(tmp_path, "x", 1, "urgent")
    assert [(c.kind, c.brief_id) for c in PickLog(tmp_path).choices()] == [("bump", "b1")]


def test_log_pick_cli_reports_the_position(tmp_path, capsys):
    root = make_queue(tmp_path, {"a": {}})
    PickLog(root).append(shown(["z", "a"], impression="imp9", at=datetime.datetime.now(datetime.timezone.utc).isoformat()))
    log_pick.main(["skip", "a", "--root", str(root)])
    log_pick.main(["pick", "q", "--root", str(root)])
    assert capsys.readouterr().out == "skip a (position 2 of imp9)\npick q (off-list)\n"
