"""Tests for triage.py's steady-state cost: classify only what changed, stop at no quota, declare what waits on nothing."""

import dataclasses
import json
import subprocess

import headless
import pytest
import triage
from deps import fm
from git_sync import SyncResult

from tests.queue_helpers import inbox_text, make_inbox

LIVE = {"o/r#1"}.__contains__
GATE = triage.Gate(LIVE)
WAITING = "This waits for o/r#9 to land before anyone starts."


@pytest.fixture
def synced(monkeypatch):
    calls = []
    monkeypatch.setattr(
        triage,
        "sync_commit",
        lambda msg, root, ids, paths=(): calls.append(sorted(ids)) or SyncResult.COMMITTED,
    )
    return calls


def _opts(root, apply=True, **kw):
    base = triage.Options(
        root, apply, GATE, None, batch_size=5, jobs=1, limit=None, today="2026-11-23"
    )
    return dataclasses.replace(base, **kw)


class _Model:
    """A classifier that records which briefs it was asked about."""

    def __init__(self):
        self.asked = []

    def __call__(self, briefs, examples):
        self.asked.append([b.id for b in briefs])
        return {b.id: triage.Call(triage.BriefClass.DROP, "stale") for b in briefs}


def _front(root, bid):
    return fm((root / "inbox" / f"{bid}.md").read_text())


def test_a_second_run_over_an_unchanged_inbox_asks_the_model_nothing(tmp_path, synced):
    root = make_inbox(tmp_path, {"a": inbox_text("a"), "b": inbox_text("b", title="Other")})
    model = _Model()
    triage.run(_opts(root, classify=model))
    triage.run(_opts(root, classify=model))
    assert model.asked == [["a", "b"]]


def test_an_edited_or_new_brief_is_asked_about_and_the_rest_are_not(tmp_path, synced):
    root = make_inbox(tmp_path, {"a": inbox_text("a"), "b": inbox_text("b", title="Other")})
    model = _Model()
    triage.run(_opts(root, classify=model))
    path = root / "inbox" / "a.md"
    path.write_text(path.read_text() + "\nMore detail.\n")
    (root / "inbox" / "c.md").write_text(inbox_text("c", title="Third"))
    triage.run(_opts(root, classify=model))
    assert model.asked[1:] == [["a", "c"]]


def test_a_dry_run_records_nothing_so_the_next_run_asks_again(tmp_path, synced):
    root = make_inbox(tmp_path, {"a": inbox_text("a")})
    model = _Model()
    triage.run(_opts(root, apply=False, classify=model))
    triage.run(_opts(root, apply=False, classify=model))
    assert model.asked == [["a"], ["a"]]


def _no_quota(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        raise subprocess.CalledProcessError(2, cmd, stderr="claude-any: no account has quota\n")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _llm(root):
    return _opts(
        root,
        predict=triage.claude_predict("m"),
        classify=triage.claude_classify("m"),
        batch_size=1,
    )


def test_no_quota_in_the_fill_skips_every_classify_call(tmp_path, synced, monkeypatch):
    calls = _no_quota(monkeypatch)
    briefs = {"thin": inbox_text("thin", title="Fix o/r#1")}
    briefs |= {f"b{i}": inbox_text(f"b{i}", title=f"T{i}") for i in range(3)}
    triage.run(_llm(make_inbox(tmp_path, briefs)))
    assert len(calls) == 1


def test_no_quota_in_one_queue_skips_the_calls_of_the_next_queue_in_the_process(
    tmp_path, synced, monkeypatch
):
    calls = _no_quota(monkeypatch)
    for name in ("one", "two"):
        queue = make_inbox(tmp_path / name, {"thin": inbox_text("thin", title="Fix o/r#1")})
        triage.run(_llm(queue))
    assert len(calls) == 1 and headless.TALLY.skipped > 0


def test_main_says_once_that_no_quota_ended_the_calls_and_reports_how_many_it_skipped(
    tmp_path, capsys, monkeypatch
):
    _no_quota(monkeypatch)
    monkeypatch.setattr(triage, "LiveSources", lambda root, prs=None: LIVE)
    briefs = {f"b{i}": inbox_text(f"b{i}", title=f"Fix o/r#1 {i}") for i in range(4)}
    root = make_inbox(tmp_path, briefs)
    with pytest.raises(SystemExit):
        triage.main(["promote", "--root", str(root), "--llm", "--batch-size", "1"])
    captured = capsys.readouterr()
    assert captured.err.count("no headless capacity") == 1
    assert json.loads(captured.out)["llm_skipped"] > 0


def test_an_inbox_brief_that_waits_on_nothing_is_declared_in_place(tmp_path, synced):
    root = make_inbox(tmp_path, {"bare": inbox_text("bare")})
    report = triage.run(_opts(root))
    assert _front(root, "bare")["blocked_by"] == "[]"
    assert "declared blocked_by: []" in (root / "inbox" / "bare.md").read_text()
    assert (report.declared_empty, report.held_for_fill) == (1, 0)
    assert synced == [["bare"]]


def test_a_brief_whose_prose_waits_is_left_for_the_fill_and_the_reason_is_logged(
    tmp_path, synced, capsys
):
    root = make_inbox(tmp_path, {"waits": inbox_text("waits", context=WAITING)})
    report = triage.run(_opts(root))
    assert "blocked_by" not in _front(root, "waits")
    assert (report.declared_empty, report.held_for_fill) == (0, 1)
    assert "waits: not declared, prose says" in capsys.readouterr().err


def test_a_brief_that_already_declares_is_not_rewritten(tmp_path, synced):
    declared = inbox_text("done", blocked_by="[external:vendor reply]")
    root = make_inbox(tmp_path, {"done": declared})
    report = triage.run(_opts(root))
    assert (root / "inbox" / "done.md").read_text() == declared
    assert report.declared_empty == 0


def test_a_dry_run_declares_nothing_but_counts_what_it_would(tmp_path, synced):
    root = make_inbox(tmp_path, {"bare": inbox_text("bare")})
    before = (root / "inbox" / "bare.md").read_text()
    report = triage.run(_opts(root, apply=False))
    assert (root / "inbox" / "bare.md").read_text() == before
    assert report.declared_empty == 1 and synced == []


def test_a_declared_brief_that_now_passes_the_gate_is_promoted_in_the_same_run(tmp_path, synced):
    ready = inbox_text("ready", goal="Ship it.", criteria=("It ships.",), context="Per o/r#1.")
    root = make_inbox(tmp_path, {"ready": ready})
    report = triage.run(_opts(root))
    assert report.promoted_as_filed == 1 and (root / "ready" / "ready.md").exists()
