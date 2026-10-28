"""milestones.py — optional, project-owned; inert unless tasks.toml names a file."""
import json

import milestones
import pytest

from tests.queue_helpers import MILESTONES, make_queue

BRIEFS = {
    "a": {"status": "done", "milestone": "M1"},
    "b": {"status": "in-progress", "milestone": "M1"},
    "c": {"status": "blocked", "milestone": "M2", "blocked_by": "[external:x]"},
    "d": {},
}


def test_queue_without_the_setting_is_unaffected(tmp_path):
    root = make_queue(tmp_path, BRIEFS)
    assert milestones.milestones_file(root) is None
    assert milestones.milestone_titles(root) is None
    assert milestones.progress(root) is None


def test_progress_counts_by_status_in_file_order(tmp_path):
    root = make_queue(tmp_path, BRIEFS, milestones=MILESTONES)
    rows = milestones.progress(root)
    assert [(m.id, m.title, m.done, m.total) for m in rows] == [
        ("M1", "First", 1, 2), ("M2", "Second", 0, 1)]
    assert rows[0].counts == {"done": 1, "in-progress": 1}


def test_only_the_milestone_table_keys_are_the_contract(tmp_path):
    body = 'title = "Project"\n[milestone.X]\nanything = 1\n[milestone.Y]\ntitle = "Why"\n'
    root = make_queue(tmp_path, {}, milestones=body)
    assert milestones.milestone_titles(root) == {"X": "X", "Y": "Why"}


def test_absolute_path_is_honoured(tmp_path):
    root = make_queue(tmp_path, {})
    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text(MILESTONES)
    cfg = root / "tasks.toml"
    cfg.write_text(cfg.read_text() + f'\nmilestones = "{elsewhere}"\n')
    assert list(milestones.milestone_titles(root)) == ["M1", "M2"]


@pytest.mark.parametrize("body", ["not = [valid", "title = 'no table'", "milestone = 3"])
def test_unusable_file_raises_a_clear_error(tmp_path, body):
    root = make_queue(tmp_path, {}, milestones=body)
    with pytest.raises(milestones.MilestonesError):
        milestones.milestone_titles(root)


def test_missing_file_raises_a_clear_error(tmp_path):
    root = make_queue(tmp_path, {}, milestones=MILESTONES)
    (root.parent / "milestones.toml").unlink()
    with pytest.raises(milestones.MilestonesError, match="cannot read"):
        milestones.progress(root)


def test_non_table_milestone_entries_fall_back_to_the_id(tmp_path):
    root = make_queue(tmp_path, {}, milestones="[milestone]\nM1 = 1\n")
    assert milestones.milestone_titles(root) == {"M1": "M1"}


def test_cli_text_json_and_unconfigured(tmp_path, capsys):
    root = make_queue(tmp_path / "a", BRIEFS, milestones=MILESTONES)
    milestones.main(["progress", "--root", str(root)])
    assert "M1" in capsys.readouterr().out
    milestones.main(["progress", "--root", str(root), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["done"] == 1
    plain = make_queue(tmp_path / "b", BRIEFS)
    milestones.main(["progress", "--root", str(plain)])
    assert "does not use milestones" in capsys.readouterr().out


def test_cli_reports_a_bad_file_as_an_error(tmp_path):
    root = make_queue(tmp_path, {}, milestones="x = [")
    with pytest.raises(SystemExit, match="error"):
        milestones.main(["progress", "--root", str(root)])
