"""Tests for readiness.py: the fields a brief needs before it leaves inbox/."""

import add_task
import init_queue
import pytest
import readiness

BRIEF = """---
id: a-brief
title: A brief
---

## Goal

Do the thing.

## Context

Because.

## Success criteria

- [ ] The thing is done well.
- [x] A second check.

## Constraints

none
"""


def test_missing_ready_fields_names_every_empty_field_in_order():
    assert readiness.missing_ready_fields("  ", [], " ") == ["goal", "criteria", "context"]


def test_missing_ready_fields_is_empty_when_all_present():
    assert readiness.missing_ready_fields("g", ["c"], "x") == []


def test_complete_brief_has_nothing_missing():
    assert readiness.brief_missing_fields(BRIEF) == []


@pytest.mark.parametrize(
    ("heading", "field"),
    [("Goal", "goal"), ("Context", "context"), ("Success criteria", "criteria")],
)
def test_brief_missing_a_section_names_it(heading, field):
    assert readiness.brief_missing_fields(BRIEF.replace(f"## {heading}", "## Renamed")) == [field]


def test_text_without_sections_misses_everything():
    assert readiness.brief_missing_fields("just a thought\n") == ["goal", "criteria", "context"]


def test_add_task_placeholders_count_as_missing(tmp_path):
    queue = init_queue.init_queue(tmp_path / "q", name="demo")
    add_task.main(["--title", "Capture me", "--goal", "Capture it", "--root", str(queue)])
    (filed,) = (queue / "inbox").glob("*.md")
    assert readiness.brief_missing_fields(filed.read_text()) == ["criteria", "context"]


@pytest.mark.parametrize("title", ["Capture me", "Review PR #42", "Trailing space "])
def test_add_task_defaults_all_count_as_missing_whatever_the_title(tmp_path, title):
    queue = init_queue.init_queue(tmp_path / "q", name="demo")
    add_task.main(["--title", title, "--root", str(queue)])
    (filed,) = (queue / "inbox").glob("*.md")
    assert readiness.brief_missing_fields(filed.read_text()) == ["goal", "criteria", "context"]


@pytest.mark.parametrize(
    "goal, context, criteria",
    [
        ("# TODO", "Because.", "- [ ] Done."),
        ("Ship.", "TBD", "- [ ] Done."),
        ("Ship.", "Because.", "<!-- one checkable line per outcome -->"),
        ("Ship.", "Because.", "- [ ] TODO"),
    ],
)
def test_stub_and_comment_only_fields_count_as_missing(goal, context, criteria):
    text = f"## Goal\n\n{goal}\n\n## Context\n\n{context}\n\n## Success criteria\n\n{criteria}\n"
    assert len(readiness.brief_missing_fields(text)) == 1


def test_criteria_marks_pairs_each_real_criterion_with_its_tick():
    assert readiness.criteria_marks(BRIEF) == [
        ("The thing is done well.", False),
        ("A second check.", True),
    ]


def test_all_criteria_ticked_needs_every_one_ticked():
    assert not readiness.all_criteria_ticked(BRIEF)
    assert readiness.all_criteria_ticked(BRIEF.replace("[ ]", "[x]"))


def test_all_criteria_ticked_is_false_with_no_real_criterion():
    assert not readiness.all_criteria_ticked(
        "---\ntitle: T\n---\n\n## Success criteria\n\n- [x] T is done.\n"
    )
    assert not readiness.all_criteria_ticked("## Goal\n\ng\n")
