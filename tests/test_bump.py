"""Tests for bump.py: importance is only ever raised, with a recorded reason."""
import datetime

import bump
import pytest

NOW = datetime.datetime(2026, 11, 5, 9, 30)


def brief(root, importance=3):
    for folder in bump.FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    path = root / "ready" / "b1.md"
    path.write_text(f"---\ntags: [pr:repo#1]\nimportance: {importance}\n---\nbody\n")
    return path


def test_ref_maps_to_pr_tag_and_literal_tag_is_kept():
    assert (bump.tag_for("repo#1"), bump.tag_for("fdo")) == ("pr:repo#1", "fdo")


def test_bump_raises_importance_and_appends_reason_section(tmp_path):
    path = brief(tmp_path)
    assert bump.bump(tmp_path, "repo#1", 1, "blocks demo", NOW) == "bumped b1: importance 3 -> 1"
    assert "importance: 1" in path.read_text()
    assert "## Bump\n- 2026-11-05 09:30: importance 3 -> 1. blocks demo" in path.read_text()


def test_second_bump_appends_to_existing_section(tmp_path):
    path = brief(tmp_path, importance=4)
    bump.bump(tmp_path, "repo#1", 3, "a", NOW)
    bump.bump(tmp_path, "repo#1", 2, "b", NOW)
    assert path.read_text().count("## Bump") == 1
    assert path.read_text().rstrip().endswith("importance 3 -> 2. b")


def test_never_lowers_importance(tmp_path):
    brief(tmp_path, importance=1)
    assert bump.bump(tmp_path, "repo#1", 2, "x", NOW).startswith("unchanged")


def test_missing_tag_exits(tmp_path):
    brief(tmp_path)
    with pytest.raises(SystemExit, match="no open brief tagged other"):
        bump.bump(tmp_path, "other", 1, "x", NOW)


def test_main_bumps_via_root(tmp_path, monkeypatch, capsys):
    brief(tmp_path)
    monkeypatch.setattr(bump, "require_queue", lambda root: root)
    bump.main(["repo#1", "--importance", "2", "--reason", "r", "--root", str(tmp_path)])
    assert "importance 3 -> 2" in capsys.readouterr().out
