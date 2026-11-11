"""Tests for triage.py: the dry-run and apply passes over a whole inbox."""

import json

import pytest
import triage
from triage_enrich import Fill

from tests.queue_helpers import inbox_text, make_inbox

READY = dict(goal="Ship it.", criteria=("It ships.",))
LIVE = {"o/r#1", "o/r#2"}.__contains__
GATE = triage.Gate(LIVE)
PEERS = {
    f"peer-a-{h}": inbox_text(f"peer-a-{h}", title=f"Peer a ({h})", tags="[peer]")
    for h in ("0a0a0a0a", "1b1b1b1b")
}
QUEUE = {
    **PEERS,
    "filed": inbox_text("filed", context="Per o/r#2.", **READY),
    "thin": inbox_text("thin", title="Fix o/r#1"),
    "bare": inbox_text("bare"),
    "raw": "a note\n",
}


def _fill(briefs):
    return {b.id: Fill("Ship.", ("Done.",), "Start at o/r#1.") for b in briefs}


@pytest.fixture
def synced(monkeypatch):
    calls = []
    monkeypatch.setattr(
        triage, "sync_commit", lambda msg, root, ids: calls.append(sorted(ids)) or True
    )
    return calls


def _opts(root, apply, predict=_fill):
    return triage.Options(
        root, apply, GATE, predict, batch_size=5, jobs=1, limit=None, today="2026-11-11"
    )


def test_dry_run_counts_without_writing(tmp_path, synced):
    root = make_inbox(tmp_path, QUEUE)
    report = triage.run(_opts(root, apply=False))
    assert (report.inbox_before, report.collapsed_children, report.promoted_as_filed) == (6, 2, 1)
    assert (report.enrich_candidates, report.promoted_enriched) == (1, 1)
    assert report.inbox_after == 6 and not report.synced and synced == []


def test_apply_collapses_promotes_and_commits_once(tmp_path, synced):
    root = make_inbox(tmp_path, QUEUE)
    report = triage.run(_opts(root, apply=True))
    assert (report.ready_after - report.ready_before, report.inbox_after) == (2, 3)
    assert synced == [["filed", "peer-a-0a0a0a0a", "peer-a-1b1b1b1b", "peer-a-batch", "thin"]]
    assert sorted(p.stem for p in (root / "inbox").glob("*.md")) == ["bare", "peer-a-batch", "raw"]


def test_a_promotion_refused_by_the_move_counts_as_changed_underfoot(tmp_path, synced, monkeypatch):
    def refuse(*_):
        raise ValueError("moved elsewhere")

    monkeypatch.setattr(triage, "promote", refuse)
    report = triage.run(_opts(make_inbox(tmp_path, {"filed": QUEUE["filed"]}), apply=True))
    assert report.changed_underfoot == 1 and not report.synced


def test_progress_logs_each_batch(tmp_path, capsys):
    root = make_inbox(tmp_path, {"thin": QUEUE["thin"]})
    GATE.enrich_all(triage.load_inbox(root), triage._progress(_fill), 5, 1)
    assert "batch thin..: 1/1 filled" in capsys.readouterr().err


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(triage, "LiveSources", lambda root: LIVE)


def test_main_prints_the_dry_run_report(tmp_path, capsys, offline):
    root = make_inbox(tmp_path, {"filed": QUEUE["filed"]})
    triage.main(["promote", "--root", str(root)])
    assert json.loads(capsys.readouterr().out)["promoted_as_filed"] == 1


def test_main_wires_the_llm_backend(tmp_path, capsys, monkeypatch, offline):
    monkeypatch.setattr(triage, "claude_predict", lambda model: _fill)
    triage.main(["promote", "--root", str(make_inbox(tmp_path, {"thin": QUEUE["thin"]})), "--llm"])
    assert json.loads(capsys.readouterr().out)["promoted_enriched"] == 1
