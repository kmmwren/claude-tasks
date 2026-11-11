"""Tests for seed_profile.py: a DRAFT profile from goals, north stars and recent decisions."""
import json
import subprocess

import pytest
import seed_profile
import taste
from taste import Goal

from tests.queue_helpers import make_queue

NORTH_STARS = "# North stars\n\n## NS-1 An instinct-style assistant\n\ntext\n\n## NS-12 AI native |  composable\n## Not a star\n"


@pytest.fixture
def sources(tmp_path):
    goals, decisions = tmp_path / "goals", tmp_path / "decisions"
    goals.mkdir()
    decisions.mkdir()
    (goals / "fdo.json").write_text(json.dumps({"slug": "fdo", "title": "Fast data onboarding", "top_priority": True,
                                                "definition_of_done": "packs live |\n everywhere"}))
    (goals / "rel.json").write_text(json.dumps({"slug": "rel", "title": "Relationships"}))
    (goals / "broken.json").write_text("{not json")
    (decisions / "2026-11-10-older.md").write_text("# Older choice\n")
    (decisions / "2026-11-11-ranker-design.md").write_text("no heading here\n")
    (decisions / "undated.md").write_text("# Undated\n")
    root = make_queue(tmp_path, {})
    (root / "tasks.toml").write_text((root / "tasks.toml").read_text() + (
        f'\n[profile]\ngoals_dir = "{goals}"\ndecisions_dir = "{decisions}"\ndecisions_limit = 2\n'))
    return root


def test_draft_weights_the_top_priority_goal_highest_and_skips_broken_files(sources, capsys):
    profile = seed_profile.draft(sources, "", today="2026-11-11")
    assert profile.goals == (Goal("Fast data onboarding", 1.0, ("fdo", "project:fdo"), "packs live / everywhere"),
                             Goal("Relationships", 0.5, ("rel", "project:rel"), ""))
    assert capsys.readouterr().err.startswith("warning: skipped goal broken.json:")


def test_draft_takes_recent_decision_titles_as_patterns(sources):
    assert seed_profile.draft(sources, "").patterns == ("Settled: Undated", "Settled 2026-11-11: ranker design")


def test_draft_reads_north_star_headings_as_values(sources):
    profile = seed_profile.draft(sources, "o/r:docs/ns.md", fetch=lambda spec: NORTH_STARS)
    assert profile.values == ("NS-1 An instinct-style assistant", "NS-12 AI native / composable")
    assert (profile.draft, profile.dislikes) == (True, seed_profile.DISLIKES)


def test_north_stars_come_from_config_when_no_flag_is_given(sources):
    (sources / "tasks.toml").write_text((sources / "tasks.toml").read_text() + 'north_stars = "o/r:ns.md"\n')
    seen = []
    seed_profile.draft(sources, None, fetch=lambda spec: seen.append(spec) or "")
    assert seen == ["o/r:ns.md"]


def test_an_unreadable_north_star_doc_is_skipped_with_a_warning(sources, capsys):
    def fail(spec):
        raise subprocess.CalledProcessError(1, "gh")
    assert seed_profile.draft(sources, "o/r:x", fetch=fail).values == ()
    assert "warning: north stars not read from o/r:x" in capsys.readouterr().err


def test_gh_fetch_reads_the_raw_file_through_gh(monkeypatch):
    calls = []
    monkeypatch.setattr(seed_profile.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, "body", ""))
    assert seed_profile.gh_fetch("Org/repo:docs/ns.md") == "body"
    assert calls == [["gh", "api", "repos/Org/repo/contents/docs/ns.md", "-H", "Accept: application/vnd.github.raw"]]


def test_seed_writes_the_draft_and_refuses_to_overwrite_without_force(sources):
    profile = seed_profile.draft(sources, "", today="2026-11-11")
    path = seed_profile.seed(sources, profile)
    assert taste.load_profile(sources) == profile
    with pytest.raises(SystemExit, match="exists; edit it, or re-seed with --force"):
        seed_profile.seed(sources, profile)
    assert seed_profile.seed(sources, profile, force=True) == path


def test_main_reports_what_it_drafted(sources, capsys):
    seed_profile.main(["--root", str(sources), "--north-stars", "", "--today", "2026-11-11"])
    assert capsys.readouterr().out.startswith(f"drafted {taste.profile_path(sources)}: 2 goals, 0 values, 2 patterns.")
