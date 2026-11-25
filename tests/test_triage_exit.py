"""Tests for triage.py's exit status: a failed run is not healthy, and one skipped for capacity is retryable."""

import json
import subprocess

import pytest
import triage

from tests.queue_helpers import inbox_text, make_inbox


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(triage, "LiveSources", lambda root: {"o/r#1"}.__contains__)


def test_main_exits_nonzero_when_every_headless_call_fails(tmp_path, capsys, monkeypatch, offline):
    def missing(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory", "claude")

    monkeypatch.setattr(subprocess, "run", missing)
    root = make_inbox(tmp_path, {"thin": inbox_text("thin", title="Fix o/r#1")})
    with pytest.raises(SystemExit, match="headless claude calls failed"):
        triage.main(["promote", "--root", str(root), "--llm"])
    report = json.loads(capsys.readouterr().out)
    assert report["llm_calls"] > 0 and report["llm_failed"] == report["llm_calls"]


def test_main_exits_75_when_the_no_capacity_latch_skipped_the_run(
    tmp_path, capsys, monkeypatch, offline
):
    def no_quota(cmd, **kw):
        raise subprocess.CalledProcessError(2, cmd, stderr="claude-any: no account has quota\n")

    monkeypatch.setattr(subprocess, "run", no_quota)
    root = make_inbox(tmp_path, {"thin": inbox_text("thin", title="Fix o/r#1")})
    with pytest.raises(SystemExit) as stop:
        triage.main(["promote", "--root", str(root), "--llm"])
    assert stop.value.code == triage.EX_TEMPFAIL == 75
    assert "no headless capacity" in capsys.readouterr().err


def test_main_without_llm_exits_cleanly(tmp_path, capsys, offline):
    triage.main(["promote", "--root", str(make_inbox(tmp_path, {"bare": inbox_text("bare")}))])
    assert json.loads(capsys.readouterr().out)["llm_calls"] == 0
