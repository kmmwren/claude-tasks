"""Tests for bump.py: a brief found by tag only ever has its importance raised, with the reason recorded."""
import datetime

import bump
import init_queue
import pytest

NOW = datetime.datetime(2026, 11, 5, 9, 30)


@pytest.fixture
def queue(tmp_path):
    root = init_queue.init_queue(tmp_path / "q")
    (root / "ready" / "fix.md").write_text("---\nid: fix\nimportance: 3\ntags: [pr:repo#42, fdo]\n---\nbody\n")
    return root


def test_a_ref_matches_its_pr_tag(queue):
    assert bump.bump(queue, "repo#42", 1, "blocks the demo", NOW) == "bumped fix: importance 3 -> 1"
    text = (queue / "ready" / "fix.md").read_text()
    assert "importance: 1\n" in text
    assert text.endswith("## Bump\n- 2026-11-05 09:30: importance 3 -> 1. blocks the demo\n")


def test_a_literal_tag_matches(queue):
    assert bump.bump(queue, "fdo", 2, "why", NOW).startswith("bumped fix")


def test_a_second_bump_appends_to_the_same_section(queue):
    bump.bump(queue, "fdo", 2, "first", NOW)
    bump.bump(queue, "fdo", 1, "second", NOW)
    text = (queue / "ready" / "fix.md").read_text()
    assert text.count("## Bump") == 1
    assert text.endswith("importance 2 -> 1. second\n")


def test_importance_is_never_lowered(queue):
    assert bump.bump(queue, "fdo", 4, "why", NOW).startswith("unchanged")
    assert "importance: 3\n" in (queue / "ready" / "fix.md").read_text()


def test_an_unknown_tag_exits(queue):
    with pytest.raises(SystemExit, match="no open brief tagged pr:other#1"):
        bump.bump(queue, "other#1", 1, "why")


def test_main_takes_an_explicit_root(queue, capsys):
    bump.main(["fdo", "--importance", "1", "--reason", "why", "--root", str(queue)])
    assert "bumped fix: importance 3 -> 1" in capsys.readouterr().out


def test_main_resolves_the_root_without_one(queue, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(queue))
    bump.main(["fdo", "--importance", "2", "--reason", "why"])
    assert "importance 3 -> 2" in capsys.readouterr().out
