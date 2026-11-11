"""Tests for triage_brief.py: the inbox brief model and its pure edits."""
from triage_brief import GENERATED_BY, InboxBrief, load_inbox

from tests.queue_helpers import inbox_text, make_inbox


def _brief(tmp_path, **kwargs):
    return InboxBrief(tmp_path / "b.md", inbox_text("b", **kwargs))


def test_facts_come_from_the_text(tmp_path):
    b = _brief(tmp_path, title="Fix ABC-1", tags="[x, a]", context="See o/r#2.")
    assert (b.id, b.title, b.tags, b.context) == ("b", "Fix ABC-1", ("a", "x"), "See o/r#2.")
    assert b.sources == {"ABC-1", "o/r#2"}
    assert b.missing == ["goal", "criteria"]


def test_a_brief_without_frontmatter_or_title_is_raw(tmp_path):
    assert _brief(tmp_path).structured
    assert not InboxBrief(tmp_path / "r.md", "Land o/r#36 next.\n").structured


def test_load_inbox_leaves_raw_captures_out(tmp_path):
    root = make_inbox(tmp_path, {"a": inbox_text("a"), "raw": "just a note\n"})
    assert [b.id for b in load_inbox(root)] == ["a"]


def test_with_field_sets_or_adds_a_frontmatter_key(tmp_path):
    b = _brief(tmp_path).with_field("parent", "p").with_field("new-key", "v")
    assert (b.fields["parent"], b.fields["new-key"]) == ("p", "v")


def test_with_section_replaces_the_body_and_marks_it_generated(tmp_path):
    b = _brief(tmp_path).with_section("Goal", "Ship it.")
    assert "## Goal\n\nShip it.\n\n## Context" in b.text
    assert b.fields["generated-by"] == GENERATED_BY


def test_with_section_appends_a_missing_section(tmp_path):
    b = _brief(tmp_path).with_section("Constraints", "None.")
    assert b.text.endswith("## Constraints\n\nNone.\n")
