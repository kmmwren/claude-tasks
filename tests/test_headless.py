"""Tests for headless.py: the isolated, schema-bound claude -p call."""
import json
import subprocess

import pytest
from headless import ask_json, command, headless_claude

SCHEMA = {"type": "object"}


def test_uses_the_wrapper_only_when_installed(tmp_path, monkeypatch):
    wrapper = tmp_path / "claude-any.sh"
    monkeypatch.setenv("CLAUDE_ANY_BIN", str(wrapper))
    assert headless_claude() == ["claude", "-p"]
    wrapper.write_text("#!/bin/sh\n")
    assert headless_claude() == [str(wrapper)]


def test_command_turns_off_tools_mcp_settings_and_sessions():
    cmd = command("sonnet", SCHEMA)
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--setting-sources") + 1] == ""
    assert {"--strict-mcp-config", "--no-session-persistence"} <= set(cmd)
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == SCHEMA


def _run_returning(stdout):
    def run(cmd, **kw):
        assert kw["env"]["CLAUDE_LOCAL_OK"] == "read-only-research:why" and kw["input"] == "prompt"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout)
    return run


def test_returns_the_structured_output(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _run_returning(json.dumps({"structured_output": {"a": 1}})))
    assert ask_json("prompt", SCHEMA, "sonnet", "why") == {"a": 1}


@pytest.mark.parametrize("stdout", ["not json", "[]"])
def test_bad_output_is_none(monkeypatch, stdout):
    monkeypatch.setattr(subprocess, "run", _run_returning(stdout))
    assert ask_json("prompt", SCHEMA, "sonnet", "why") is None


def test_a_failed_call_is_none_and_says_so(monkeypatch, capsys):
    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(3, cmd)
    monkeypatch.setattr(subprocess, "run", boom)
    assert ask_json("prompt", SCHEMA, "sonnet", "why") is None
    assert "headless call failed" in capsys.readouterr().err
