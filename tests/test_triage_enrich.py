"""Tests for triage_enrich.py: schema-validated fills of missing fields."""

import pytest
import triage_enrich
from headless import ScriptLaunch
from triage_brief import InboxBrief
from triage_enrich import FILL_SCHEMA, Fill, apply_fill, claude_predict, parse_fills, render_prompt

from tests.queue_helpers import inbox_text

LAUNCH = ScriptLaunch("/l.sh")


@pytest.fixture
def brief(tmp_path):
    return InboxBrief(tmp_path / "b.md", inbox_text("b", context="See o/r#2."))


def test_prompt_wraps_each_brief_as_delimited_data(brief):
    prompt = render_prompt([brief])
    assert '<brief id="b">' in prompt and prompt.rstrip().endswith("</brief>")
    assert "never instructions" in prompt


GOOD = {
    "id": "b",
    "goal": " Ship. ",
    "criteria": ["It ships.", " "],
    "context": "See o/r#2.",
    "blocked_by": [],
}


def test_parse_keeps_valid_fills_for_asked_ids():
    assert parse_fills({"briefs": [GOOD, {**GOOD, "id": "z"}]}, {"b"}) == {
        "b": Fill("Ship.", ("It ships.",), "See o/r#2.", ())
    }


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"briefs": "x"},
        {"briefs": [GOOD | {"goal": 3}]},
        {"briefs": [GOOD | {"criteria": "x"}]},
        {"briefs": [GOOD | {"criteria": [1]}]},
        {"briefs": [{k: v for k, v in GOOD.items() if k != "blocked_by"}]},
        {"briefs": [GOOD | {"blocked_by": "x"}]},
        {"briefs": [GOOD | {"blocked_by": [1]}]},
        {"briefs": ["x"]},
    ],
)
def test_parse_drops_anything_off_schema(payload):
    assert parse_fills(payload, {"b"}) == {}


def test_predict_asks_with_the_fill_schema_and_parses_the_answer(brief, monkeypatch):
    def ask(prompt, schema, model, reason, timeout, launch):
        assert (
            schema is FILL_SCHEMA
            and '<brief id="b">' in prompt
            and "tools off" in reason
            and launch == LAUNCH
        )
        return {"briefs": [GOOD]}

    monkeypatch.setattr(triage_enrich, "ask_json", ask)
    assert claude_predict(launch=LAUNCH)([brief])["b"].goal == "Ship."


def test_apply_fill_writes_only_missing_non_empty_fields(brief):
    filled = apply_fill(brief, Fill("", ("It ships.",), "Invented context.", ()))
    assert filled.missing == ["goal"]
    assert filled.context == "See o/r#2."


def test_parse_normalises_blockers_so_a_comma_cannot_split_one():
    fills = parse_fills({"briefs": [GOOD | {"blocked_by": [" external:PR a, b ", ""]}]}, {"b"})
    assert fills["b"].blocked_by == ("external:PR a; b",)


def test_apply_fill_declares_the_fill_s_blockers_when_the_brief_has_none(brief):
    filled = apply_fill(brief, Fill("Ship.", ("It ships.",), "See o/r#2.", ("x-1", "external:PR")))
    assert filled.fields["blocked_by"] == "[x-1, external:PR]"


def test_apply_fill_declares_nothing_blocked_as_an_empty_list(brief):
    assert (
        apply_fill(brief, Fill("Ship.", ("It ships.",), "See o/r#2.", ())).fields["blocked_by"]
        == "[]"
    )


def test_apply_fill_keeps_the_brief_s_own_declaration(tmp_path):
    declared = InboxBrief(
        tmp_path / "b.md", inbox_text("b", context="See o/r#2.", blocked_by="[a-1]")
    )
    filled = apply_fill(declared, Fill("Ship.", ("It ships.",), "See o/r#2.", ()))
    assert filled.fields["blocked_by"] == "[a-1]"
