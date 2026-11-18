"""Tests for headless.py: the isolated, schema-bound claude -p call."""

import json
import subprocess

import headless
import pytest
from headless import ask_json, command, headless_claude

SCHEMA = {"type": "object"}


@pytest.fixture(autouse=True)
def _fresh_run():
    headless.reset_capacity()
    yield
    headless.reset_capacity()


def _exiting(status, stderr):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        raise subprocess.CalledProcessError(status, cmd, stderr=stderr)

    return run, calls


def test_a_failure_names_its_exit_status_and_reason_not_the_command(monkeypatch, capsys):
    run, _ = _exiting(1, "claude-any: ran as katherine, exit 1\n")
    monkeypatch.setattr(subprocess, "run", run)
    assert ask_json("prompt", SCHEMA, "sonnet", "why") is None
    err = capsys.readouterr().err
    assert "exit 1: claude-any: ran as katherine, exit 1" in err and "--json-schema" not in err


@pytest.mark.parametrize("status", sorted(headless.NO_CAPACITY))
def test_no_capacity_skips_the_rest_of_the_run(monkeypatch, capsys, status):
    run, calls = _exiting(status, "")
    monkeypatch.setattr(subprocess, "run", run)
    assert ask_json("prompt", SCHEMA, "sonnet", "why") is None
    assert ask_json("prompt", SCHEMA, "sonnet", "why") is None
    assert len(calls) == 1 and "skipping this run's remaining calls" in capsys.readouterr().err


def test_a_timeout_is_none_and_never_prints_the_command(monkeypatch, capsys):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(subprocess, "run", slow)
    assert ask_json("prompt", SCHEMA, "sonnet", "why", timeout=5) is None
    err = capsys.readouterr().err
    assert "timed out after 5s" in err and "--json-schema" not in err


def test_reset_capacity_lets_the_next_call_spawn_again(monkeypatch):
    run, calls = _exiting(4, "")
    monkeypatch.setattr(subprocess, "run", run)
    ask_json("prompt", SCHEMA, "sonnet", "why")
    ask_json("prompt", SCHEMA, "sonnet", "why")
    assert len(calls) == 1
    headless.reset_capacity()
    ask_json("prompt", SCHEMA, "sonnet", "why")
    assert len(calls) == 2


def test_an_ordinary_failure_does_not_stop_the_run(monkeypatch):
    run, calls = _exiting(1, "boom")
    monkeypatch.setattr(subprocess, "run", run)
    ask_json("prompt", SCHEMA, "sonnet", "why")
    ask_json("prompt", SCHEMA, "sonnet", "why")
    assert len(calls) == 2


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
    monkeypatch.setattr(
        subprocess, "run", _run_returning(json.dumps({"structured_output": {"a": 1}}))
    )
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
