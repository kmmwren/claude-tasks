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
    (goals / "fdo.json").write_text(
        json.dumps(
            {
                "slug": "fdo",
                "title": "Fast data onboarding",
                "top_priority": True,
                "definition_of_done": "packs live |\n everywhere",
            }
        )
    )
    (goals / "rel.json").write_text(json.dumps({"slug": "rel", "title": "Relationships"}))
    (goals / "broken.json").write_text("{not json")
    (decisions / "2026-11-10-older.md").write_text("# Older choice\n")
    (decisions / "2026-11-11-ranker-design.md").write_text("no heading here\n")
    (decisions / "undated.md").write_text("# Undated\n")
    root = make_queue(tmp_path, {})
    (root / "tasks.toml").write_text(
        (root / "tasks.toml").read_text()
        + (
            f'\n[profile]\ngoals_dir = "{goals}"\ndecisions_dir = "{decisions}"\ndecisions_limit = 2\n'
        )
    )
    return root


def test_draft_weights_the_top_priority_goal_highest_and_skips_broken_files(sources, capsys):
    profile = seed_profile.draft(sources, "", today="2026-11-11")
    assert profile.goals == (
        Goal("Fast data onboarding", 1.0, ("fdo", "project:fdo"), "packs live / everywhere"),
        Goal("Relationships", 0.5, ("rel", "project:rel"), ""),
    )
    assert capsys.readouterr().err.startswith("warning: skipped goal broken.json:")


def test_draft_takes_recent_decision_titles_as_patterns(sources):
    assert seed_profile.draft(sources, "").patterns == (
        "Settled: Undated",
        "Settled 2026-11-11: ranker design",
    )


def test_draft_reads_north_star_headings_as_values(sources):
    profile = seed_profile.draft(sources, "o/r:docs/ns.md", fetch=lambda spec: NORTH_STARS)
    assert profile.values == ("NS-1 An instinct-style assistant", "NS-12 AI native / composable")
    assert (profile.draft, profile.dislikes) == (True, seed_profile.DISLIKES)


def test_north_stars_come_from_config_when_no_flag_is_given(sources):
    (sources / "tasks.toml").write_text(
        (sources / "tasks.toml").read_text() + 'north_stars = "o/r:ns.md"\n'
    )
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
    monkeypatch.setattr(
        seed_profile.subprocess,
        "run",
        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "body", ""),
    )
    assert seed_profile.gh_fetch("Org/repo:docs/ns.md") == "body"
    assert calls == [
        [
            "gh",
            "api",
            "repos/Org/repo/contents/docs/ns.md",
            "-H",
            "Accept: application/vnd.github.raw",
        ]
    ]


def test_seed_writes_the_draft_and_refuses_to_overwrite_without_force(sources):
    profile = seed_profile.draft(sources, "", today="2026-11-11")
    path = seed_profile.seed(sources, profile)
    assert taste.load_profile(sources) == profile
    with pytest.raises(SystemExit, match="exists; edit it, or re-seed with --force"):
        seed_profile.seed(sources, profile)
    assert seed_profile.seed(sources, profile, force=True) == path


def test_main_reports_what_it_drafted(sources, capsys):
    seed_profile.main(["--root", str(sources), "--north-stars", "", "--today", "2026-11-11"])
    assert capsys.readouterr().out.startswith(
        f"drafted {taste.profile_path(sources)}: 2 goals, 0 values, 2 patterns."
    )


def finished(root, name, tags):
    (root / "done").mkdir(exist_ok=True)
    (root / "done" / f"{name}.md").write_text(f"---\nid: {name}\ntags: [{tags}]\n---\n\nbody\n")


def test_draft_learns_the_tags_the_owner_keeps_finishing_from_done_history(sources):
    for n in range(3):
        finished(sources, f"d{n}", "fdo, project:lite")
    finished(sources, "e", "fdo, one-off")
    finished(sources, "f", "")
    (sources / "done" / "unreadable.md").mkdir()
    patterns = seed_profile.draft(sources, "").patterns
    assert patterns[2:] == (
        "Often finished: fdo (4 briefs)",
        "Often finished: project:lite (3 briefs)",
    )


def test_done_history_patterns_are_capped_and_need_a_repeat(sources):
    (sources / "tasks.toml").write_text(
        (sources / "tasks.toml").read_text() + "done_tags_limit = 1\ndone_tags_min = 3\n"
    )
    for n in range(3):
        finished(sources, f"d{n}", "a, b")
    finished(sources, "x", "b, c")
    assert seed_profile.draft(sources, "").patterns[2:] == ("Often finished: b (4 briefs)",)


def test_draft_learns_the_tags_the_queue_config_weights_up(sources):
    (sources / "tasks.toml").write_text(
        (sources / "tasks.toml").read_text()
        + '[priority]\ntag_weights = { "tier:quick" = 100, "tier:human" = 0, "later" = -5,'
        ' "tier:smoke" = 50, "odd" = "x" }\n'
    )
    assert seed_profile.draft(sources, "").patterns[2:] == (
        "Prioritised by tag: tier:quick (weight 100)",
        "Prioritised by tag: tier:smoke (weight 50)",
    )


def test_a_queue_with_no_history_or_weights_adds_no_learned_patterns(sources):
    assert len(seed_profile.draft(sources, "").patterns) == 2


def test_north_stars_default_to_the_lite_north_stars_doc():
    assert seed_profile.DEFAULTS["north_stars"] == (
        "Panaseer/ai-knowledge-mm:experiments/panaseer-lite/docs/north-stars.md"
    )


def test_the_default_north_stars_seed_the_values_when_nothing_overrides_them(tmp_path):
    root = make_queue(tmp_path, {})
    (root / "tasks.toml").write_text(
        (root / "tasks.toml").read_text()
        + f'\n[profile]\ngoals_dir = "{tmp_path}/none"\ndecisions_dir = "{tmp_path}/none"\n'
    )
    seen = []
    profile = seed_profile.draft(
        root, None, fetch=lambda spec: seen.append(spec) or "## NS-1 First\n## NS-2 Second\n"
    )
    assert seen == [seed_profile.DEFAULTS["north_stars"]]
    assert profile.values == ("NS-1 First", "NS-2 Second")


def test_main_never_overwrites_an_existing_profile_and_does_not_even_fetch(
    sources, monkeypatch
):
    path = taste.profile_path(sources)
    path.parent.mkdir(exist_ok=True)
    path.write_text("## Goals\n- Mine | weight 1\n")
    monkeypatch.setattr(
        seed_profile, "gh_fetch", lambda spec: pytest.fail("fetched for an existing profile")
    )
    with pytest.raises(SystemExit, match="exists; edit it, or re-seed with --force"):
        seed_profile.main(["--root", str(sources)])
    assert path.read_text() == "## Goals\n- Mine | weight 1\n"
