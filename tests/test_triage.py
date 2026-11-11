"""Tests for triage.py: the dry-run and apply passes over a whole inbox."""

import dataclasses
import json

import pytest
import triage
from class_log import Correction
from triage_enrich import Fill

from tests.queue_helpers import inbox_text, make_inbox

BC = triage.BriefClass
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
    assert report.inbox_after == 6 and not report.committed and synced == []


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
    assert report.changed_underfoot == 1 and not report.committed


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
    monkeypatch.setattr(triage, "claude_classify", lambda model: lambda briefs, examples: {})
    triage.main(["promote", "--root", str(make_inbox(tmp_path, {"thin": QUEUE["thin"]})), "--llm"])
    assert json.loads(capsys.readouterr().out)["promoted_enriched"] == 1


def _log(root):
    return triage.ClassLog(root)


def test_apply_logs_a_verdict_for_every_inbox_brief_and_ready_only_for_those_moved(
    tmp_path, synced
):
    root = make_inbox(tmp_path, QUEUE)
    report = triage.run(_opts(root, apply=True))
    classes = {v.brief_id: v.cls for v in _log(root).verdicts()}
    assert classes == {
        "filed": "ready",
        "thin": "ready",
        "bare": "needs-info",
        "peer-a-batch": "needs-info",
    }
    assert report.classes == {"ready": 2, "needs-info": 2}
    assert {v["id"] for v in report.verdicts} == set(classes)


def test_dry_run_reports_verdicts_and_logs_nothing(tmp_path, synced):
    root = make_inbox(tmp_path, QUEUE)
    report = triage.run(_opts(root, apply=False))
    assert report.classes["ready"] == 2 and not _log(root).path.exists()


def test_a_repeat_of_an_earlier_brief_is_a_duplicate_and_is_not_promoted(tmp_path, synced):
    twin = inbox_text("filed-again", title="Title of filed", context="Per o/r#2.", **READY)
    root = make_inbox(tmp_path, {"filed": QUEUE["filed"], "filed-again": twin})
    report = triage.run(_opts(root, apply=True))
    assert (report.duplicates, report.promoted_as_filed) == (1, 1)
    assert [p.stem for p in (root / "inbox").glob("*.md")] == ["filed-again"]
    assert [(v.brief_id, v.cls) for v in _log(root).verdicts()] == [
        ("filed-again", "duplicate"),
        ("filed", "ready"),
    ]


def test_a_promotion_the_owner_moved_back_is_logged_and_not_promoted_again(tmp_path, synced):
    root = make_inbox(tmp_path, {"filed": QUEUE["filed"]})
    log = _log(root)
    log.append(triage.Verdict("filed", BC.READY, "ready as filed", "Title of filed", "t0"))
    report = triage.run(_opts(root, apply=True))
    assert report.corrections_logged == 1 and report.promoted_as_filed == 0
    assert (root / "inbox" / "filed.md").exists()
    assert [(c.predicted, c.actual) for c in log.corrections()] == [("ready", "needs-info")]
    assert log.latest()["filed"].reason == "moved back from ready by the owner"


def test_a_dry_run_does_not_reconcile(tmp_path, synced):
    root = make_inbox(tmp_path, {"filed": QUEUE["filed"]})
    _log(root).append(triage.Verdict("filed", BC.READY, "r", "T", "t0"))
    assert triage.run(_opts(root, apply=False)).corrections_logged == 0


def test_the_classifier_is_shown_earlier_corrections(tmp_path, synced):
    root = make_inbox(tmp_path, {"bare": QUEUE["bare"]})
    log = _log(root)
    log.append(triage.Verdict("old", BC.DROP, "stale", "Old one", "t0"))
    log.append(Correction("old", "t0", BC.DROP, BC.READY, "ready", "t1"))
    seen = []

    def classify(briefs, examples):
        seen.append(([b.id for b in briefs], [e.title for e in examples]))
        return {"bare": triage.Call(BC.DROP, "obsolete")}

    opts = dataclasses.replace(_opts(root, apply=True), classify=classify)
    assert triage.run(opts).classes == {"drop": 1} and seen == [(["bare"], ["Old one"])]
