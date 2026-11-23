"""Tests for how headless.py finds the claude binary and tallies a run's calls."""

import subprocess

import headless
from headless import DirectLaunch, ask_json

SCHEMA = {"type": "object"}


def _no_claude_on_path(monkeypatch, home):
    monkeypatch.setattr(headless.shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(home))


def test_a_scheduled_job_without_claude_on_path_finds_the_installed_binary(monkeypatch, tmp_path):
    _no_claude_on_path(monkeypatch, tmp_path)
    binary = tmp_path / ".claude" / "bin" / "claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert DirectLaunch().argv([]) == [str(binary), "-p"]


def test_claude_on_path_wins_over_the_fallbacks(monkeypatch):
    monkeypatch.setattr(headless.shutil, "which", lambda name: "/opt/bin/claude")
    assert headless.claude_program() == "/opt/bin/claude"


def test_no_claude_anywhere_keeps_the_bare_name_so_the_failure_names_it(monkeypatch, tmp_path):
    _no_claude_on_path(monkeypatch, tmp_path)
    assert headless.claude_program() == "claude"


def test_the_tally_counts_every_call_and_each_failure(monkeypatch):
    answers = iter([None, '{"structured_output": {"ok": 1}}'])

    def run(cmd, **kw):
        answer = next(answers)
        if answer is None:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=answer)

    monkeypatch.setattr(subprocess, "run", run)
    ask_json("prompt", SCHEMA, "sonnet", "why", launch=DirectLaunch("claude"))
    assert headless.TALLY.all_failed
    ask_json("prompt", SCHEMA, "sonnet", "why", launch=DirectLaunch("claude"))
    assert (headless.TALLY.calls, headless.TALLY.failed) == (2, 1)
    assert not headless.TALLY.all_failed


def test_a_run_with_no_calls_has_not_failed():
    headless.reset_capacity()
    assert not headless.TALLY.all_failed
