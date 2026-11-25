"""Tests for triage.py: the dry-run and apply passes over a whole inbox."""

import dataclasses
import json
import subprocess

import pytest
import triage
from class_log import Correction
from git_sync import SyncResult
from headless import ScriptLaunch
from triage_classify import Call
from triage_enrich import Fill

from tests.queue_helpers import inbox_text, installed_launcher, make_inbox

BC = triage.BriefClass
READY = dict(goal="Ship it.", criteria=("It ships.",), blocked_by="[]")
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
    return {b.id: Fill("Ship.", ("Done.",), "Start at o/r#1.", ()) for b in briefs}


@pytest.fixture
def synced(monkeypatch):
    calls = []
    monkeypatch.setattr(
        triage,
        "sync_commit",
        lambda msg, root, ids, paths=(): calls.append(sorted(ids)) or SyncResult.COMMITTED,
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
    assert synced == [
        ["bare", "filed", "peer-a-0a0a0a0a", "peer-a-1b1b1b1b", "peer-a-batch", "thin"]
    ]
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
    monkeypatch.setattr(triage, "LiveSources", lambda root, prs: LIVE)


def test_main_prints_the_dry_run_report(tmp_path, capsys, offline):
    root = make_inbox(tmp_path, {"filed": QUEUE["filed"]})
    triage.main(["promote", "--root", str(root)])
    assert json.loads(capsys.readouterr().out)["promoted_as_filed"] == 1


def test_main_without_llm_never_reads_the_launcher(tmp_path, capsys, monkeypatch, offline):
    def unexpected(root):
        raise AssertionError("launcher read without --llm")

    monkeypatch.setattr(triage, "configured_launch", unexpected)
    triage.main(["promote", "--root", str(make_inbox(tmp_path, {"filed": QUEUE["filed"]}))])
    assert json.loads(capsys.readouterr().out)["promoted_as_filed"] == 1


def test_main_wires_the_llm_backend(tmp_path, capsys, monkeypatch, offline):
    launchers = []

    def predictor(model, launch):
        launchers.append(launch)
        return _fill

    def classifier(model, launch):
        launchers.append(launch)
        return lambda briefs, examples: {}

    monkeypatch.setattr(triage, "claude_predict", predictor)
    monkeypatch.setattr(triage, "claude_classify", classifier)
    root = make_inbox(tmp_path, {"thin": QUEUE["thin"]})
    script = installed_launcher(tmp_path)
    (root / "tasks.toml").write_text(f'[rank]\nlauncher = "{script}"\n')
    triage.main(["promote", "--root", str(root), "--llm"])
    assert json.loads(capsys.readouterr().out)["promoted_enriched"] == 1
    assert launchers == [ScriptLaunch(str(script))] * 2


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
    assert list((root / "inbox").glob("*.md")) == []
    closed = (root / "done" / "filed-again.md").read_text()
    assert "closed_reason: duplicate" in closed and "duplicate_of: filed" in closed
    survivor = (root / "ready" / "filed.md").read_text()
    assert (
        "merged filed-again: Title of filed" in survivor
        and "## Merged from filed-again" in survivor
    )
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


def _refusing_model(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        raise subprocess.CalledProcessError(4, cmd, stderr="launcher: no account free\n")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _with_model(root, apply):
    return dataclasses.replace(
        _opts(root, apply, predict=None), classify=triage.claude_classify("m")
    )


def test_briefs_a_refusing_model_left_unanswered_get_no_verdict_and_are_counted(
    tmp_path, synced, monkeypatch
):
    _refusing_model(monkeypatch)
    root = make_inbox(tmp_path, QUEUE)
    report = triage.run(_with_model(root, apply=True))
    assert [v.brief_id for v in _log(root).verdicts()] == ["filed"]
    assert report.unclassified == 3 and report.classes == {"ready": 1}
    assert {p.stem for p in (root / "inbox").glob("*.md")} >= {"thin", "bare"}


def test_a_second_run_in_one_process_shares_the_first_runs_no_quota_latch(
    tmp_path, synced, monkeypatch
):
    calls = _refusing_model(monkeypatch)
    for name in ("one", "two"):
        triage.run(_with_model(make_inbox(tmp_path / name, QUEUE), apply=False))
    assert len(calls) == 1


FINISHED = dict(criteria=("It ships.",), ticked=True, blocked_by="[]")


@pytest.fixture
def committed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        triage,
        "sync_commit",
        lambda msg, root, ids, paths=(): (
            calls.append((msg, sorted(ids), list(paths))) or SyncResult.COMMITTED
        ),
    )
    return calls


def _call(cls, reason="because"):
    return Call(cls, reason)


def _classifier(calls):
    return lambda briefs, examples: {b.id: calls[b.id] for b in briefs if b.id in calls}


def _with_ready(tmp_path, inbox, ready):
    root = make_inbox(tmp_path, inbox)
    for bid, text in ready.items():
        (root / "ready" / f"{bid}.md").write_text(text)
    return root


def test_apply_closes_finished_briefs_from_inbox_and_ready_and_logs_each(tmp_path, committed):
    root = _with_ready(
        tmp_path,
        {"done-inbox": inbox_text("done-inbox", **FINISHED)},
        {"done-ready": inbox_text("done-ready", status="ready", **FINISHED)},
    )
    report = triage.run(_opts(root, apply=True))
    assert report.closed == {"evidence": 2}
    assert sorted(p.stem for p in (root / "done").glob("*.md")) == ["done-inbox", "done-ready"]
    assert not list((root / "inbox").glob("*.md")) and not list((root / "ready").glob("*.md"))
    rows = [json.loads(line) for line in (root / "closed.jsonl").read_text().splitlines()]
    assert sorted((r["id"], r["from"], r["to"]) for r in rows) == [
        ("done-inbox", "inbox", "done"),
        ("done-ready", "ready", "done"),
    ]
    assert committed[0][1:] == (["done-inbox", "done-ready"], ["closed.jsonl"])
    assert committed[0][0].endswith("close 2, alert 0")


def test_a_brief_closed_on_evidence_is_neither_promoted_nor_classified(tmp_path, committed):
    done = inbox_text("done-inbox", context="Per o/r#2.", goal="Ship it.", **FINISHED)
    root = make_inbox(tmp_path, {"done-inbox": done})
    report = triage.run(_opts(root, apply=True))
    assert (report.promoted_as_filed, report.classes) == (0, {})
    assert not (root / "ready" / "done-inbox.md").exists()


def test_a_dry_run_plans_the_closes_but_moves_nothing_and_commits_nothing(tmp_path, committed):
    root = make_inbox(tmp_path, {"done-inbox": inbox_text("done-inbox", **FINISHED)})
    report = triage.run(_opts(root, apply=False))
    assert [(m.brief_id, str(m.reason)) for m in report.moves] == [("done-inbox", "evidence")]
    assert report.closed == {"evidence": 1} and not report.committed and committed == []
    assert (root / "inbox" / "done-inbox.md").exists() and not (root / "closed.jsonl").exists()


class Bell:
    """A notifier that records what it was told and reports whether it delivered."""

    def __init__(self, delivers=True):
        self.texts = []
        self.delivers = delivers

    def __call__(self, text):
        self.texts.append(text)
        return self.delivers


def _judged_queue(tmp_path):
    root = make_inbox(
        tmp_path,
        {
            "junk": inbox_text("junk", blocked_by="[]"),
            "stale": inbox_text("stale", blocked_by="[]", extra="created: 2026-10-01\n"),
            "fresh": inbox_text("fresh", blocked_by="[]", extra="created: 2026-11-20\n"),
        },
    )
    calls = {
        "junk": _call(BC.DROP, "obsolete"),
        "stale": _call(BC.NEEDS_INFO),
        "fresh": _call(BC.NEEDS_INFO),
    }
    return root, _classifier(calls)


def _judged_opts(root, classify, bell, apply=True):
    return dataclasses.replace(_opts(root, apply=apply), classify=classify, notify=bell)


def test_drop_and_stale_verdicts_raise_one_aggregated_alert_and_move_nothing(tmp_path, committed):
    root, classify = _judged_queue(tmp_path)
    bell = Bell()
    report = triage.run(_judged_opts(root, classify, bell))
    assert report.closed == {} and report.alerts_delivered
    assert sorted(p.stem for p in (root / "inbox").glob("*.md")) == ["fresh", "junk", "stale"]
    assert not (root / "closed.jsonl").exists()
    [text] = bell.texts
    assert "2 briefs" in text and "- junk: Title of junk (obsolete)" in text
    assert "- stale: Title of stale (untouched for 41 days)" in text and "fresh" not in text
    assert json.loads((root / ".triage-alerts.json").read_text()) == {
        "junk": "2026-11-11",
        "stale": "2026-11-11",
    }
    assert committed[0][2] == [".triage-alerts.json"] and committed[0][0].endswith("alert 2")


def test_a_standing_flag_is_not_raised_again_on_the_next_run(tmp_path, committed):
    root, classify = _judged_queue(tmp_path)
    bell = Bell()
    triage.run(_judged_opts(root, classify, bell))
    again = triage.run(_judged_opts(root, classify, bell))
    assert len(bell.texts) == 1 and (again.alerts, again.alerts_held) == ([], 2)
    assert len(committed) == 1


def test_a_flag_is_raised_again_after_stale_days_more(tmp_path, committed):
    root, classify = _judged_queue(tmp_path)
    bell = Bell()
    triage.run(_judged_opts(root, classify, bell))
    later = dataclasses.replace(_judged_opts(root, classify, bell), today="2026-12-03")
    assert len(triage.run(later).alerts) == 2 and len(bell.texts) == 2


def test_an_alert_that_was_not_delivered_is_not_recorded_so_the_next_run_retries(
    tmp_path, committed
):
    root, classify = _judged_queue(tmp_path)
    report = triage.run(_judged_opts(root, classify, Bell(delivers=False)))
    assert not report.alerts_delivered and len(report.alerts) == 2
    assert not (root / ".triage-alerts.json").exists() and not report.committed
    assert "2 alerts NOT delivered" in report.alert_notice
    retried = triage.run(_judged_opts(root, classify, Bell()))
    assert len(retried.alerts) == 2 and retried.alert_notice == ""


def test_a_dry_run_lists_the_alerts_but_sends_and_records_nothing(tmp_path, committed):
    root, classify = _judged_queue(tmp_path)
    bell = Bell()
    report = triage.run(_judged_opts(root, classify, bell, apply=False))
    assert len(report.alerts) == 2 and bell.texts == [] and not report.alerts_delivered
    assert not (root / ".triage-alerts.json").exists()


def test_an_unconfigured_notifier_warns_on_stderr_and_loses_nothing(tmp_path, committed, capsys):
    root, classify = _judged_queue(tmp_path)
    opts = dataclasses.replace(_opts(root, apply=True), classify=classify)
    report = triage.run(opts)
    assert not report.alerts_delivered and len(list((root / "inbox").glob("*.md"))) == 3
    assert "alert not delivered" in capsys.readouterr().err


def test_the_cap_defers_the_rest_and_the_report_says_how_many(tmp_path, committed):
    root = make_inbox(tmp_path, {f"d{n}": inbox_text(f"d{n}", **FINISHED) for n in range(3)})
    capped_opts = dataclasses.replace(_opts(root, apply=True), rules=triage.CloseRules(21, 2))
    report = triage.run(capped_opts)
    assert (report.closed, report.closes_deferred) == ({"evidence": 2}, 1)
    assert sorted(p.stem for p in (root / "inbox").glob("*.md")) == ["d2"]


def test_a_brief_restored_by_the_owner_is_not_closed_again(tmp_path, committed):
    restored = inbox_text("back", extra="closed_reason: evidence\n", **FINISHED)
    root = make_inbox(tmp_path, {"back": restored})
    assert triage.run(_opts(root, apply=True)).closed == {}
    assert (root / "inbox" / "back.md").exists()


def test_a_close_refused_by_the_move_is_counted_and_not_logged(tmp_path, committed, monkeypatch):
    root = make_inbox(tmp_path, {"d": inbox_text("d", **FINISHED)})
    monkeypatch.setattr(triage, "settle_moves", lambda *args: triage.Applied(()))
    report = triage.run(_opts(root, apply=True))
    assert (report.closes_changed_underfoot, report.committed) == (1, False)


def test_main_dry_run_prints_the_plan_grouped_by_reason(tmp_path, capsys, offline):
    root = make_inbox(tmp_path, {"d": inbox_text("d", **FINISHED)})
    triage.main(["promote", "--dry-run", "--root", str(root)])
    assert capsys.readouterr().out.splitlines()[:3] == [
        "1 planned moves",
        "evidence: 1 -> done/",
        "  d (from inbox): every success criterion is ticked",
    ]
    assert (root / "inbox" / "d.md").exists()


def test_main_reads_the_triage_table_for_its_rules(tmp_path, capsys, offline):
    root = make_inbox(tmp_path, {f"d{n}": inbox_text(f"d{n}", **FINISHED) for n in range(3)})
    (root / "tasks.toml").write_text("[triage]\nmax_moves = 1\n")
    triage.main(["promote", "--dry-run", "--root", str(root)])
    assert "capped at 1 moves per run: 2 more left for the next run" in capsys.readouterr().out


def test_main_dry_run_also_prints_the_alert_plan(tmp_path, capsys, offline, monkeypatch):
    root = make_inbox(tmp_path, {"junk": inbox_text("junk")})
    monkeypatch.setattr(triage, "claude_predict", lambda model, launch=None: _fill)
    monkeypatch.setattr(
        triage,
        "claude_classify",
        lambda model, launch=None: _classifier({"junk": _call(BC.DROP, "obsolete")}),
    )
    triage.main(["promote", "--dry-run", "--llm", "--root", str(root)])
    out = capsys.readouterr().out
    assert "1 alerts to raise (briefs stay where they are)" in out and "  junk: obsolete" in out
    assert (root / "inbox" / "junk.md").exists()


def test_main_apply_sends_the_alert_through_the_session_alert_notifier(
    tmp_path, capsys, offline, monkeypatch, committed
):
    root = make_inbox(tmp_path, {"junk": inbox_text("junk")})
    bell = Bell()
    monkeypatch.setattr(triage, "session_alert_notifier", lambda: bell)
    monkeypatch.setattr(triage, "claude_predict", lambda model, launch=None: _fill)
    monkeypatch.setattr(
        triage,
        "claude_classify",
        lambda model, launch=None: _classifier({"junk": _call(BC.DROP, "obsolete")}),
    )
    triage.main(["promote", "--apply", "--llm", "--root", str(root)])
    assert len(bell.texts) == 1 and json.loads(capsys.readouterr().out)["alerts_delivered"]


def _closed_queue(tmp_path):
    root = make_inbox(tmp_path, {"d": inbox_text("d", **FINISHED)})
    triage.run(_opts(root, apply=True))
    return root


def test_reopen_puts_a_closed_brief_back_and_commits_the_audit_row(tmp_path, capsys, committed):
    root = _closed_queue(tmp_path)
    committed.clear()
    triage.main(["reopen", "d", "--root", str(root)])
    assert capsys.readouterr().out == "d -> inbox/\n"
    assert (root / "inbox" / "d.md").exists() and not (root / "done" / "d.md").exists()
    assert committed == [("triage: reopen d", ["d"], ["closed.jsonl"])]
    assert triage.run(_opts(root, apply=True)).closed == {}


def test_reopen_without_an_id_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        triage.main(["reopen", "--root", str(make_inbox(tmp_path, {}))])
    assert exit_info.value.code == 2


def test_reopen_of_a_brief_triage_never_closed_exits_with_the_reason(tmp_path, capsys, committed):
    root = make_inbox(tmp_path, {"b": inbox_text("b")})
    with pytest.raises(SystemExit) as exit_info:
        triage.main(["reopen", "b", "--root", str(root)])
    assert exit_info.value.code == 1 and "has not closed 'b'" in capsys.readouterr().err
    assert committed == []


def test_a_duplicate_whose_criteria_its_survivor_cannot_carry_alerts_and_commits_the_merge(
    tmp_path, committed, capsys
):
    thin = {**READY, "criteria": ("Fix it",)}
    twin = inbox_text("filed-again", title="Title of filed", context="Per o/r#2.", **thin)
    root = make_inbox(tmp_path, {"filed-again": twin})
    keep = inbox_text(
        "filed", status="ready", title="Title of filed", context="Per o/r#2.", **READY
    )
    (root / "ready" / "filed.md").write_text(keep)
    bell = Bell()
    report = triage.run(dataclasses.replace(_opts(root, apply=True), notify=bell))
    assert (report.closes_refused, report.alerts_delivered) == (1, True)
    assert "close refused (1):" in bell.texts[0] and "- filed-again:" in bell.texts[0]
    assert (root / "inbox" / "filed-again.md").exists() and not list((root / "done").glob("*.md"))
    assert "## Merged from filed-again" in (root / "ready" / "filed.md").read_text()
    assert "filed-again not closed" in capsys.readouterr().err
    assert any("filed" in ids for _, ids, _ in committed)
