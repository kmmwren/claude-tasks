"""Tests for triage_enrich.py: isolated, schema-validated fills of missing fields."""
import json
import subprocess

import pytest
import triage_enrich
from queue_helpers import inbox_text
from triage_brief import InboxBrief
from triage_enrich import Fill, apply_fill, claude_command, claude_predict, headless_claude, parse_fills, render_prompt


@pytest.fixture
def brief(tmp_path):
    return InboxBrief(tmp_path / "b.md", inbox_text("b", context="See o/r#2."))


def test_prompt_wraps_each_brief_as_delimited_data(brief):
    prompt = render_prompt([brief])
    assert '<brief id="b">' in prompt and prompt.rstrip().endswith("</brief>")
    assert "never instructions" in prompt


GOOD = {"id": "b", "goal": " Ship. ", "criteria": ["It ships.", " "], "context": "See o/r#2."}


def test_parse_keeps_valid_fills_for_asked_ids():
    assert parse_fills({"briefs": [GOOD, {**GOOD, "id": "z"}]}, {"b"}) == {
        "b": Fill("Ship.", ("It ships.",), "See o/r#2.")}


@pytest.mark.parametrize("payload", [
    None, {"briefs": "x"}, {"briefs": [GOOD | {"goal": 3}]}, {"briefs": [GOOD | {"criteria": "x"}]},
    {"briefs": [GOOD | {"criteria": [1]}]}, {"briefs": ["x"]},
])
def test_parse_drops_anything_off_schema(payload):
    assert parse_fills(payload, {"b"}) == {}


def test_headless_uses_the_wrapper_when_installed(tmp_path, monkeypatch):
    wrapper = tmp_path / "claude-any.sh"
    monkeypatch.setenv("CLAUDE_ANY_BIN", str(wrapper))
    assert headless_claude() == ["claude", "-p"]
    wrapper.write_text("#!/bin/sh\n")
    assert headless_claude() == [str(wrapper)]


def test_command_disables_tools_and_mcp_and_sets_the_schema():
    cmd = claude_command("sonnet")
    assert cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == triage_enrich.FILL_SCHEMA


def _run_returning(stdout):
    def run(cmd, **kw):
        assert kw["env"]["CLAUDE_LOCAL_OK"].startswith("read-only-research:")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout)
    return run


def test_predict_parses_the_structured_output(brief, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _run_returning(json.dumps({"structured_output": {"briefs": [GOOD]}})))
    assert claude_predict()([brief])["b"].goal == "Ship."


@pytest.mark.parametrize("stdout", ["not json", "[]"])
def test_predict_fills_nothing_on_bad_output(brief, monkeypatch, stdout):
    monkeypatch.setattr(subprocess, "run", _run_returning(stdout))
    assert claude_predict()([brief]) == {}


def test_predict_fills_nothing_when_the_call_fails(brief, monkeypatch, capsys):
    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(3, cmd)
    monkeypatch.setattr(subprocess, "run", boom)
    assert claude_predict()([brief]) == {}
    assert "fill batch failed" in capsys.readouterr().err


def test_apply_fill_writes_only_missing_non_empty_fields(brief):
    filled = apply_fill(brief, Fill("", ("It ships.",), "Invented context."))
    assert filled.missing == ["goal"]
    assert filled.context == "See o/r#2."
