"""Tests for taste.py: the human-edited profile parses, renders and versions predictably."""
import pytest
import taste
from taste import Goal, Profile

TEXT = """---
draft: false
updated: 2026-11-01
---
# Priorities

Free prose the parser ignores.

## Goals
- Fast data onboarding | weight 1 | match fdo, project:fdo | packs live
- Relationships | weight 0.5
- Bare goal

## Values
- NS-1 Says what to do next

## Dislikes
- Busywork

## Patterns
- Settled 2026-11-11: rank, then filter

## Unknown
- ignored
"""


def test_parse_reads_goals_with_weights_matches_and_notes():
    assert taste.parse(TEXT).goals == (
        Goal("Fast data onboarding", 1.0, ("fdo", "project:fdo"), "packs live"),
        Goal("Relationships", 0.5),
        Goal("Bare goal", 1.0),
    )


def test_parse_reads_lists_and_frontmatter():
    p = taste.parse(TEXT)
    assert (p.values, p.dislikes, p.patterns, p.draft, p.updated) == (
        ("NS-1 Says what to do next",), ("Busywork",), ("Settled 2026-11-11: rank, then filter",),
        False, "2026-11-01")


def test_a_profile_without_frontmatter_is_a_draft():
    assert taste.parse("## Goals\n- A\n") == Profile(goals=(Goal("A"),), draft=True, updated="")


def test_goal_note_keeps_extra_pipes():
    assert Goal.parse("A | weight 2 | one | two") == Goal("A", 2.0, (), "one | two")


def test_a_non_numeric_weight_is_a_profile_error():
    with pytest.raises(taste.ProfileError, match="goal 'A': weight 'high' is not a number"):
        Goal.parse("A | weight high")


def test_render_round_trips():
    profile = taste.parse(TEXT)
    assert taste.parse(taste.render(profile)) == profile


def test_render_marks_a_draft_and_keeps_empty_sections():
    out = taste.render(Profile(goals=(Goal("A"),), updated="2026-11-11"))
    assert out.startswith("---\ndraft: true\nupdated: 2026-11-11\n---\n# Priorities\n\n> DRAFT seeded")
    assert out.endswith("\n## Goals\n- A | weight 1\n\n## Values\n\n## Dislikes\n\n## Patterns\n")


def test_render_of_a_settled_profile_has_no_draft_banner():
    assert "DRAFT" not in taste.render(Profile(draft=False))


def test_version_ignores_prose_and_follows_content():
    profile = taste.parse(TEXT)
    assert taste.parse(TEXT.replace("Free prose", "Other prose")).version == profile.version
    assert profile.with_weights({"Relationships": 0.6}, profile.updated).version != profile.version
    assert len(profile.version) == 12


def test_with_weights_changes_only_named_goals_and_stamps_the_date():
    profile = taste.parse(TEXT).with_weights({"Relationships": 0.6}, "2026-12-01")
    assert profile.weights == {"Fast data onboarding": 1.0, "Relationships": 0.6, "Bare goal": 1.0}
    assert profile.updated == "2026-12-01"


def test_a_goal_matches_a_tag_or_a_token_in_the_text():
    goal = Goal("FDO", match=("fdo", "project:fdo"))
    assert [goal.matches(frozenset({"project:fdo"}), ""), goal.matches(frozenset(), "the FDO demo"),
            goal.matches(frozenset({"x"}), "other")] == [True, True, False]


def test_matched_goals_names_every_matching_goal():
    assert taste.parse(TEXT).matched_goals(frozenset({"fdo"}), "") == ("Fast data onboarding",)


def test_rubric_lists_goals_heaviest_first_then_the_lists():
    profile = Profile(goals=(Goal("Light", 0.5), Goal("Heavy", 2.0, note="why")),
                      values=("v",), dislikes=("d",), patterns=("p",))
    assert profile.rubric() == ("Goals (weight = importance):\n- Heavy (weight 2) why\n- Light (weight 0.5)\n"
                                "Values:\n- v\nDislikes:\n- d\nPatterns:\n- p")


def test_load_profile_is_none_until_seeded(tmp_path):
    assert taste.load_profile(tmp_path) is None
    taste.profile_path(tmp_path).parent.mkdir()
    taste.profile_path(tmp_path).write_text(TEXT)
    assert taste.load_profile(tmp_path) == taste.parse(TEXT)
