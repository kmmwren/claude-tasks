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


@pytest.mark.parametrize(("heading", "field"), [("Goal", "goal"), ("Context", "context"), ("Success criteria", "criteria")])
def test_brief_missing_a_section_names_it(heading, field):
    assert readiness.brief_missing_fields(BRIEF.replace(f"## {heading}", "## Renamed")) == [field]


def test_text_without_sections_misses_everything():
    assert readiness.brief_missing_fields("just a thought\n") == ["goal", "criteria", "context"]


def test_add_task_placeholders_count_as_missing(tmp_path):
    queue = init_queue.init_queue(tmp_path / "q", name="demo")
    add_task.main(["--title", "Capture me", "--goal", "Capture it", "--root", str(queue)])
    (filed,) = (queue / "inbox").glob("*.md")
    assert readiness.brief_missing_fields(filed.read_text()) == ["criteria", "context"]
