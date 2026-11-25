"""Tests for triage_close.py: closing briefs on evidence and duplicates, and reopening them."""

import datetime
import json
import pathlib

import pytest
import triage_close
from class_log import BriefClass, Verdict
from deps import fm
from done_guard import UntickedCriteriaError
from triage_brief import InboxBrief, load_inbox
from triage_close import (
    CLOSING_FIELDS,
    MOVE_LOG,
    CloseRules,
    LogEvent,
    Move,
    MoveLog,
    MoveRecord,
    Reason,
    _demoted,
    apply_moves,
    capped,
    eligible,
    evidence_moves,
    idle_days,
    plan_text,
    reopen,
    settle_moves,
    touched_ids,
    verdict_moves,
)
from triage_evidence import PrStates
from triage_sources import PrState

from tests.queue_helpers import inbox_text, make_inbox

BC = BriefClass
TODAY = datetime.date(2026, 11, 11)
OLD = "created: 2026-10-01\n"
FINISHED = dict(criteria=("It ships.",), ticked=True)


def _brief(bid="b", **kw):
    return InboxBrief(pathlib.Path(f"{bid}.md"), inbox_text(bid, **kw))


def _verdict(bid, cls, reason="because"):
    return Verdict(bid, cls, reason, f"Title of {bid}")


def _moves(briefs, verdicts, dupes=None):
    return verdict_moves(verdicts, {b.id: b for b in briefs}, dupes or {})


@pytest.mark.parametrize(
    "reason, survivor, fields",
    [
        (
            Reason.DUPLICATE,
            "keep",
            {"closed_reason": "duplicate", "duplicate_of": "keep", "closed_from": "inbox"},
        ),
        (
            Reason.EVIDENCE,
            "",
            {"closed_reason": "evidence", "closed_evidence": "obsolete", "closed_from": "inbox"},
        ),
    ],
)
def test_each_reason_records_its_frontmatter(reason, survivor, fields):
    assert Move("b", "T", "inbox", reason, "obsolete", survivor).fields == fields
    assert set(fields) <= set(CLOSING_FIELDS)


def test_a_move_log_line_says_why_and_how_to_undo_it():
    closed = Move("b", "T", "ready", Reason.EVIDENCE, "o/r#1 MERGED")
    assert closed.log == (
        "closed by inbox-triage (evidence): o/r#1 MERGED; `triage.py reopen b` puts it back in ready/"
    )


def test_rules_default_without_a_triage_table(tmp_path):
    assert CloseRules.load(tmp_path) == CloseRules(21, 200)


def test_rules_come_from_the_triage_table(tmp_path):
    (tmp_path / "tasks.toml").write_text("[triage]\nstale_days = 7\nmax_moves = 50\n")
    assert CloseRules.load(tmp_path) == CloseRules(7, 50)


def test_zero_max_moves_switches_closing_off(tmp_path):
    (tmp_path / "tasks.toml").write_text("[triage]\nmax_moves = 0\n")
    assert CloseRules.load(tmp_path).max_moves == 0


@pytest.mark.parametrize(
    "body",
    [
        "triage = 5",
        "[triage]\nstale_days = 0\nmax_moves = -1",
        '[triage]\nstale_days = "x"',
        "[triage]\nstale_days = true",
    ],
)
def test_a_malformed_triage_setting_falls_back_to_its_default(tmp_path, body):
    (tmp_path / "tasks.toml").write_text(body + "\n")
    assert CloseRules.load(tmp_path) == CloseRules(21, 200)


@pytest.mark.parametrize(
    "key", ["assignee: me\n", "closed_reason: evidence\n", "reopened: 2026-11-12\n"]
)
def test_a_claimed_closed_or_reopened_brief_is_not_eligible(key):
    assert not eligible(_brief(extra=key)) and eligible(_brief())


def test_idle_days_counts_from_updated_else_created():
    assert idle_days(_brief(extra=OLD + "updated: 2026-11-01\n"), TODAY) == 10
    assert idle_days(_brief(extra=OLD), TODAY) == 41


@pytest.mark.parametrize("extra", ["", "created: soon\n"])
def test_idle_days_is_unknown_without_a_date(extra):
    assert idle_days(_brief(extra=extra), TODAY) is None


def test_a_fingerprinted_duplicate_moves_pointing_at_the_survivor():
    [move] = _moves([_brief("a")], [_verdict("a", BC.DUPLICATE, "same as keep")], {"a": "keep"})
    assert (move.reason, move.survivor, move.detail, move.source) == (
        Reason.DUPLICATE,
        "keep",
        "same as keep",
        "inbox",
    )


def test_a_model_called_duplicate_the_fingerprint_does_not_show_is_not_closed():
    assert _moves([_brief("a")], [_verdict("a", BC.DUPLICATE)], {}) == []


@pytest.mark.parametrize("cls", [BC.DROP, BC.NEEDS_INFO, BC.READY])
def test_judgement_verdicts_never_close_anything(cls):
    assert _moves([_brief("a", extra=OLD)], [_verdict("a", cls)], {"other": "x"}) == []


def test_an_ineligible_duplicate_is_never_moved():
    brief = _brief("a", extra="closed_reason: duplicate\n")
    assert _moves([brief], [_verdict("a", BC.DUPLICATE)], {"a": "keep"}) == []


def _states(table):
    return PrStates(lambda repo, number: table.get((repo, number)))


def test_evidence_moves_cover_each_eligible_finished_brief_in_the_folder():
    finished = _brief("fin", criteria=("Done.",), ticked=True)
    claimed = _brief("claimed", criteria=("Done.",), ticked=True, extra="assignee: me\n")
    open_pr = _brief("open", title="Merge o/r#9", context="o/r#9")
    merged = _brief("merged", title="Merge o/r#7", context="o/r#7")
    states = _states({("o/r", 7): PrState.MERGED, ("o/r", 9): PrState.OPEN})
    moves = evidence_moves([finished, claimed, open_pr, merged], "ready", states)
    assert [(m.brief_id, m.source, m.detail) for m in moves] == [
        ("fin", "ready", "every success criterion is ticked"),
        ("merged", "ready", "o/r#7 MERGED"),
    ]


def test_the_cap_spends_its_budget_on_evidence_first():
    moves = [
        Move("u", "T", "inbox", Reason.DUPLICATE, "x", "k"),
        Move("e2", "T", "ready", Reason.EVIDENCE, "x"),
        Move("e1", "T", "ready", Reason.EVIDENCE, "x"),
    ]
    kept, deferred = capped(moves, 2)
    assert [m.brief_id for m in kept] == ["e1", "e2"] and deferred == 1


def test_under_the_cap_nothing_is_deferred():
    kept, deferred = capped([Move("e", "T", "ready", Reason.EVIDENCE, "x")], 5)
    assert len(kept) == 1 and deferred == 0


def _closed_fields(root, folder, bid):
    return fm((root / folder / f"{bid}.md").read_text())


def _rows(root):
    return [json.loads(line) for line in (root / "closed.jsonl").read_text().splitlines()]


def test_apply_closes_to_done_with_frontmatter_a_log_line_and_an_audit_row(tmp_path):
    root = make_inbox(tmp_path, {})
    (root / "ready" / "a.md").write_text(inbox_text("a", status="ready", **FINISHED))
    move = Move("a", "Title of a", "ready", Reason.EVIDENCE, "o/r#1 MERGED")
    assert apply_moves([move], root, "2026-11-11") == [move]
    fields = _closed_fields(root, "done", "a")
    assert (fields["status"], fields["closed_reason"], fields["closed_evidence"]) == (
        "done",
        "evidence",
        "o/r#1 MERGED",
    )
    assert (
        "2026-11-11: closed by inbox-triage (evidence): o/r#1 MERGED"
        in (root / "done" / "a.md").read_text()
    )
    [row] = _rows(root)
    assert {k: row[k] for k in ("id", "from", "to", "reason", "evidence")} == {
        "id": "a",
        "from": "ready",
        "to": "done",
        "reason": "evidence",
        "evidence": "o/r#1 MERGED",
    }
    assert row["ts"].endswith("+00:00")


DUP_BODY = dict(goal="Unique goal of the twin.", context="Twin context.")


def _duplicate_queue(tmp_path, keep_status="inbox"):
    root = make_inbox(tmp_path, {"a": inbox_text("a", **DUP_BODY)})
    keep = inbox_text("keep", status=keep_status, goal="Survivor goal.")
    (root / keep_status / "keep.md").write_text(keep)
    return root, Move("a", "Title of a", "inbox", Reason.DUPLICATE, "same as keep", "keep")


def test_a_duplicates_body_is_appended_to_the_survivor_before_it_closes(tmp_path):
    root, move = _duplicate_queue(tmp_path)
    assert apply_moves([move], root, "2026-11-11") == [move]
    survivor = (root / "inbox" / "keep.md").read_text()
    assert "## Merged from a\n" in survivor
    assert "Unique goal of the twin." in survivor and "Twin context." in survivor
    assert "### Goal" in survivor and survivor.count("\n## Goal\n") == 1
    assert survivor.index("## Merged from a") < survivor.index("## Execution log")
    assert survivor.rstrip().endswith("2026-11-11: merged a: Title of a")
    assert "captured." not in survivor.split("## Merged from a")[1].split("## Execution log")[0]
    closed = _closed_fields(root, "done", "a")
    assert (closed["closed_reason"], closed["duplicate_of"]) == ("duplicate", "keep")
    assert touched_ids([move]) == ["a", "keep"]


def test_a_survivor_without_an_execution_log_gets_the_body_at_its_end(tmp_path):
    root, move = _duplicate_queue(tmp_path)
    keep = root / "inbox" / "keep.md"
    keep.write_text(keep.read_text().split("## Execution log")[0])
    apply_moves([move], root, "2026-11-11")
    survivor = keep.read_text()
    assert survivor.index("## Merged from a") < survivor.index("## Execution log")


def test_merging_twice_appends_the_body_once(tmp_path):
    root, move = _duplicate_queue(tmp_path)
    apply_moves([move], root, "2026-11-11")
    closed = (root / "done" / "a.md").read_text().replace("status: done", "status: inbox")
    stripped = [ln for ln in closed.splitlines() if not ln.startswith(CLOSING_FIELDS)]
    (root / "inbox" / "a.md").write_text("\n".join(stripped) + "\n")
    (root / "done" / "a.md").unlink()
    apply_moves([move], root, "2026-11-12")
    assert (root / "inbox" / "keep.md").read_text().count("## Merged from a") == 1


def test_the_body_follows_a_survivor_that_has_since_moved(tmp_path):
    root, move = _duplicate_queue(tmp_path, keep_status="ready")
    apply_moves([move], root, "2026-11-11")
    assert "Unique goal of the twin." in (root / "ready" / "keep.md").read_text()


def test_a_duplicate_whose_survivor_is_gone_stays_open_so_nothing_is_lost(tmp_path):
    root = make_inbox(tmp_path, {"a": inbox_text("a")})
    done = apply_moves([Move("a", "T", "inbox", Reason.DUPLICATE, "x", "gone")], root, "2026-11-11")
    assert done == [] and (root / "inbox" / "a.md").exists()
    assert not (root / "done" / "a.md").exists() and not (root / "closed.jsonl").exists()


def test_a_brief_that_left_its_folder_since_it_was_read_is_skipped_and_unlogged(tmp_path):
    root = make_inbox(tmp_path, {})
    (root / "ready" / "a.md").write_text(inbox_text("a", status="ready"))
    done = apply_moves([Move("a", "T", "inbox", Reason.EVIDENCE, "x")], root, "2026-11-11")
    assert (
        done == [] and (root / "ready" / "a.md").exists() and not (root / "closed.jsonl").exists()
    )


def test_a_move_the_lock_holder_pulled_away_is_skipped(tmp_path, monkeypatch):
    root = make_inbox(tmp_path, {"a": inbox_text("a")})
    monkeypatch.setattr(
        "triage_close.move_brief", lambda *a, **k: (_ for _ in ()).throw(ValueError("raced"))
    )
    assert apply_moves([Move("a", "T", "inbox", Reason.EVIDENCE, "x")], root, "2026-11-11") == []


def test_an_unknown_brief_is_skipped(tmp_path):
    root = make_inbox(tmp_path, {})
    assert (
        apply_moves([Move("ghost", "T", "inbox", Reason.EVIDENCE, "x")], root, "2026-11-11") == []
    )


def test_closing_from_ready_works_and_keeps_the_brief_restorable(tmp_path):
    root = make_inbox(tmp_path, {})
    (root / "ready" / "a.md").write_text(inbox_text("a", status="ready", **FINISHED))
    apply_moves([Move("a", "T", "ready", Reason.EVIDENCE, "o/r#1 MERGED")], root, "2026-11-11")
    assert (root / "done" / "a.md").exists() and not (root / "ready" / "a.md").exists()
    assert load_inbox(root) == []


def test_move_log_appends_one_line_per_move(tmp_path):
    root = make_inbox(tmp_path, {})
    log = MoveLog(root)
    log.append(Move("a", "T", "inbox", Reason.DUPLICATE, "x", "k"))
    log.append(Move("b", "T", "ready", Reason.EVIDENCE, "y"))
    assert [r["id"] for r in _rows(root)] == ["a", "b"]


def test_headings_in_a_merged_body_sit_below_the_merge_heading():
    assert _demoted("# One\n## Two\n### Three\ntext ## not a heading") == (
        "### One\n### Two\n#### Three\ntext ## not a heading"
    )


ENRICHED = dict(
    goal="Ship it.", context="Per o/r#2.", criteria=("It ships.",), ticked=True, blocked_by="[]"
)


def _closed_on_evidence(tmp_path):
    root = make_inbox(tmp_path, {})
    (root / "ready" / "a.md").write_text(inbox_text("a", status="ready", **ENRICHED))
    apply_moves([Move("a", "T", "ready", Reason.EVIDENCE, "ticked")], root, "2026-11-11")
    return root


def test_reopen_puts_a_closed_brief_back_where_it_came_from_and_logs_it(tmp_path):
    root = _closed_on_evidence(tmp_path)
    assert reopen("a", root, "2026-11-12") == "ready"
    assert (root / "ready" / "a.md").exists() and not (root / "done" / "a.md").exists()
    text = (root / "ready" / "a.md").read_text()
    assert "2026-11-12: reopened by its owner; triage had closed it (evidence)" in text
    assert _rows(root)[-1]["reason"] == "reopened"
    fields = InboxBrief(root / "ready" / "a.md", text).fields
    assert [fields.get(k) for k in CLOSING_FIELDS] == [""] * len(CLOSING_FIELDS)
    assert fields["reopened"] == "2026-11-12" and fields["status"] == "ready"
    assert not eligible(InboxBrief(root / "ready" / "a.md", text))


def test_a_reopened_brief_cannot_be_reopened_again(tmp_path):
    root = _closed_on_evidence(tmp_path)
    reopen("a", root, "2026-11-12")
    with pytest.raises(ValueError, match="already reopened"):
        reopen("a", root, "2026-11-12")


def test_reopen_refuses_a_brief_triage_never_moved(tmp_path):
    root = make_inbox(tmp_path, {"b": inbox_text("b")})
    with pytest.raises(ValueError, match="has not closed"):
        reopen("b", root, "2026-11-12")
    MoveLog(root).append(Move("b", "T", "inbox", Reason.EVIDENCE, "x"))
    with pytest.raises(ValueError, match="no longer in done/"):
        reopen("b", root, "2026-11-12")


def test_reopen_refuses_a_brief_that_has_vanished(tmp_path):
    root = make_inbox(tmp_path, {})
    MoveLog(root).append(Move("ghost", "T", "inbox", Reason.EVIDENCE, "x"))
    with pytest.raises(ValueError, match="no longer in done/"):
        reopen("ghost", root, "2026-11-12")


def test_the_plan_is_grouped_by_reason_with_counts_and_names_the_cap():
    moves = [
        Move("e1", "T", "ready", Reason.EVIDENCE, "o/r#1 MERGED"),
        Move("d1", "T", "inbox", Reason.DUPLICATE, "same as k", "k"),
    ]
    assert plan_text(moves, 4, 3) == "\n".join(
        [
            "2 planned moves",
            "evidence: 1 -> done/",
            "  e1 (from ready): o/r#1 MERGED",
            "duplicate: 1 -> done/",
            "  d1 (from inbox): same as k",
            "capped at 3 moves per run: 4 more left for the next run",
        ]
    )


def test_an_empty_plan_says_so_without_a_cap_line():
    assert plan_text([], 0, 200) == "0 planned moves"


def _ready_queue(tmp_path, *ids):
    root = make_inbox(tmp_path, {})
    for bid in ids:
        (root / "ready" / f"{bid}.md").write_text(inbox_text(bid, status="ready", **ENRICHED))
    return root


def _closing(*ids):
    return [Move(i, "T", "ready", Reason.EVIDENCE, "ticked") for i in ids]


def _failing_on(monkeypatch, brief_id, error):
    real = triage_close.move_brief

    def move(bid, *args, **kwargs):
        if bid == brief_id:
            raise error
        return real(bid, *args, **kwargs)

    monkeypatch.setattr("triage_close.move_brief", move)


def test_a_lock_timeout_on_the_second_of_three_moves_is_skipped_and_the_rest_are_logged(
    tmp_path, monkeypatch, capsys
):
    root = _ready_queue(tmp_path, "a", "b", "c")
    _failing_on(monkeypatch, "b", RuntimeError("timed out waiting for the queue lock"))
    done = apply_moves(_closing("a", "b", "c"), root, "2026-11-11")
    assert [m.brief_id for m in done] == ["a", "c"]
    assert [r["id"] for r in _rows(root)] == ["a", "c"]
    assert (root / "ready" / "b.md").exists() and (root / "done" / "c.md").exists()
    assert "b not closed" in capsys.readouterr().err
    assert reopen("a", root, "2026-11-12") == "ready"


def test_an_unexpected_failure_on_a_later_move_leaves_the_earlier_moves_logged(
    tmp_path, monkeypatch
):
    root = _ready_queue(tmp_path, "a", "b", "c")
    _failing_on(monkeypatch, "c", OSError("disk full"))
    with pytest.raises(OSError):
        apply_moves(_closing("a", "b", "c"), root, "2026-11-11")
    assert [r["id"] for r in _rows(root)] == ["a", "b"]
    assert reopen("b", root, "2026-11-12") == "ready"


def test_a_lock_timeout_while_merging_leaves_the_duplicate_open(tmp_path, monkeypatch, capsys):
    root, move = _duplicate_queue(tmp_path)

    def timed_out(*args, **kwargs):
        raise TimeoutError("queue lock not acquired")

    monkeypatch.setattr("triage_close.require_lock", timed_out)
    assert apply_moves([move], root, "2026-11-11") == []
    assert (root / "inbox" / "a.md").exists() and not (root / "closed.jsonl").exists()
    assert "a not closed" in capsys.readouterr().err


def test_reopen_works_from_the_brief_alone_when_its_log_row_was_never_written(tmp_path):
    root = _closed_on_evidence(tmp_path)
    (root / "closed.jsonl").unlink()
    assert reopen("a", root, "2026-11-12") == "ready"
    assert _rows(root)[-1]["reason"] == "reopened"
    assert (root / "ready" / "a.md").exists()


def test_reopen_of_a_duplicate_without_a_log_row_uses_the_brief_frontmatter(tmp_path):
    root, move = _duplicate_queue(tmp_path)
    apply_moves([move], root, "2026-11-11")
    (root / "closed.jsonl").unlink()
    assert reopen("a", root, "2026-11-12") == "inbox"
    assert not _closed_fields(root, "inbox", "a").get("duplicate_of")


def test_the_move_log_reads_back_a_typed_record(tmp_path):
    root = make_inbox(tmp_path, {})
    log = MoveLog(root)
    log.append(Move("a", "T", "inbox", Reason.DUPLICATE, "x", "k"))
    assert log.last("a") == MoveRecord("a", "inbox", "done", LogEvent.DUPLICATE, "x")
    assert log.last("zz") is None


@pytest.mark.parametrize("folder", ["done", "in-progress"])
def test_a_duplicate_is_not_merged_into_a_survivor_that_is_closed_or_claimed(tmp_path, folder):
    root, move = _duplicate_queue(tmp_path, keep_status=folder)
    before = (root / folder / "keep.md").read_text()
    assert apply_moves([move], root, "2026-11-11") == []
    assert (root / folder / "keep.md").read_text() == before
    assert (root / "inbox" / "a.md").exists() and not (root / "closed.jsonl").exists()


def test_a_duplicate_is_not_merged_into_a_survivor_that_carries_an_assignee(tmp_path):
    root, move = _duplicate_queue(tmp_path, keep_status="ready")
    keep = root / "ready" / "keep.md"
    keep.write_text(keep.read_text().replace("source: claude\n", "source: claude\nassignee: me\n"))
    assert apply_moves([move], root, "2026-11-11") == []
    assert "Merged from" not in keep.read_text() and (root / "inbox" / "a.md").exists()


def test_a_survivor_closed_earlier_in_the_same_run_takes_no_merge(tmp_path):
    root = make_inbox(
        tmp_path, {"a": inbox_text("a", **DUP_BODY), "keep": inbox_text("keep", **FINISHED)}
    )
    finish = Move("keep", "T", "inbox", Reason.EVIDENCE, "ticked")
    twin = Move("a", "Title of a", "inbox", Reason.DUPLICATE, "same as keep", "keep")
    assert apply_moves([finish, twin], root, "2026-11-11") == [finish]
    assert "Merged from" not in (root / "done" / "keep.md").read_text()
    assert (root / "inbox" / "a.md").exists()


@pytest.mark.parametrize(
    "edit",
    [("- [x]", "- [ ]"), ("source: claude\n", "source: claude\nassignee: me\n")],
    ids=["criterion unticked", "assignee set"],
)
def test_a_brief_edited_since_it_was_read_is_not_closed(tmp_path, edit):
    root = _ready_queue(tmp_path, "a")
    path = root / "ready" / "a.md"
    path.write_text(path.read_text().replace(*edit))
    assert apply_moves(_closing("a"), root, "2026-11-11") == []
    assert path.exists() and not (root / "closed.jsonl").exists()


def test_a_duplicate_claimed_since_it_was_read_is_neither_merged_nor_closed(tmp_path):
    root, move = _duplicate_queue(tmp_path)
    twin = root / "inbox" / "a.md"
    twin.write_text(twin.read_text().replace("source: claude\n", "source: claude\nassignee: me\n"))
    assert apply_moves([move], root, "2026-11-11") == []
    assert "Merged from" not in (root / "inbox" / "keep.md").read_text() and twin.exists()


def test_a_close_on_pr_state_is_rechecked_against_the_runs_pr_states(tmp_path):
    root = make_inbox(tmp_path, {"a": inbox_text("a", title="Merge o/r#7", context="o/r#7")})
    move = Move("a", "Merge o/r#7", "inbox", Reason.EVIDENCE, "o/r#7 MERGED")
    assert apply_moves([move], root, "2026-11-11") == []
    merged = _states({("o/r", 7): PrState.MERGED})
    assert apply_moves([move], root, "2026-11-11", merged) == [move]


def test_fenced_code_in_a_merged_body_keeps_its_hashes():
    body = "# One\n```bash\n# comment\n## also\n```\n## Two\n~~~\n# tilde\n~~~\n###### six"
    assert _demoted(body) == (
        "### One\n```bash\n# comment\n## also\n```\n### Two\n~~~\n# tilde\n~~~\n###### six"
    )


def test_a_body_is_merged_when_the_survivor_only_holds_a_longer_ids_merge(tmp_path):
    root = make_inbox(tmp_path, {"fix-login": inbox_text("fix-login", **DUP_BODY)})
    held = inbox_text("keep", goal="Survivor goal.") + "\n## Merged from fix-login-page\n\nOther.\n"
    (root / "inbox" / "keep.md").write_text(held)
    move = Move("fix-login", "Title of fix-login", "inbox", Reason.DUPLICATE, "same", "keep")
    assert apply_moves([move], root, "2026-11-11") == [move]
    survivor = (root / "inbox" / "keep.md").read_text()
    assert "\n## Merged from fix-login\n" in survivor and "Unique goal of the twin." in survivor


def test_move_log_reasons_are_log_events_and_keep_the_strings_written_today(tmp_path):
    root = make_inbox(tmp_path, {})
    MoveLog(root).append(Move("a", "T", "inbox", Reason.EVIDENCE, "x"))
    MoveLog(root).append_reopen("a", "done", "inbox")
    assert [r["reason"] for r in _rows(root)] == ["evidence", "reopened"]
    record = MoveLog(root).last("a")
    assert record is not None and record.reason is LogEvent.REOPENED


def test_a_row_with_an_unknown_reason_is_skipped_with_a_warning(tmp_path, capsys):
    root = make_inbox(tmp_path, {})
    MoveLog(root).append(Move("a", "T", "inbox", Reason.EVIDENCE, "x"))
    with (root / MOVE_LOG).open("a") as handle:
        handle.write(json.dumps({"id": "a", "from": "inbox", "to": "done", "reason": "?"}) + "\n")
    record = MoveLog(root).last("a")
    assert record is not None and record.reason is LogEvent.EVIDENCE
    assert f"{MOVE_LOG} line 2 unreadable, skipped" in capsys.readouterr().err


def test_a_torn_log_row_does_not_stop_reopen_from_the_brief_itself(tmp_path, capsys):
    root = _closed_on_evidence(tmp_path)
    (root / MOVE_LOG).write_text('{"id": "a", "from": "rea\n')
    assert reopen("a", root, "2026-11-12") == "ready"
    assert "line 1 unreadable, skipped" in capsys.readouterr().err


def test_a_torn_row_is_skipped_and_the_rows_around_it_still_read(tmp_path):
    root = make_inbox(tmp_path, {})
    log = MoveLog(root)
    log.append(Move("a", "T", "inbox", Reason.EVIDENCE, "x"))
    with (root / MOVE_LOG).open("a") as handle:
        handle.write('{"id": "b", "fr\n\n')
    log.append(Move("c", "T", "inbox", Reason.EVIDENCE, "y"))
    assert [log.last(i) is not None for i in "abc"] == [True, False, True]


@pytest.mark.parametrize("reason", [Reason.EVIDENCE, Reason.DUPLICATE])
def test_a_brief_with_no_frontmatter_is_left_open_and_the_survivor_untouched(
    tmp_path, capsys, reason
):
    root = make_inbox(tmp_path, {})
    (root / "inbox" / "a.md").write_text("# Bare note\n\nNo block at all.\n")
    keep = inbox_text("keep", goal="Survivor goal.")
    (root / "inbox" / "keep.md").write_text(keep)
    move = Move("a", "Bare", "inbox", reason, "x", "keep" if reason is Reason.DUPLICATE else None)
    assert apply_moves([move], root, "2026-11-11") == []
    assert (root / "inbox" / "a.md").exists() and not (root / "done" / "a.md").exists()
    assert (root / "inbox" / "keep.md").read_text() == keep and not (root / MOVE_LOG).exists()
    assert "a not closed" in capsys.readouterr().err


def test_an_evidence_close_ticks_the_criteria_with_its_evidence_note(tmp_path):
    root = make_inbox(tmp_path, {})
    unticked = {**ENRICHED, "ticked": False, "title": "Merge o/r#1", "context": "o/r#1"}
    (root / "ready" / "a.md").write_text(inbox_text("a", status="ready", **unticked))
    move = Move("a", "Merge o/r#1", "ready", Reason.EVIDENCE, "o/r#1 MERGED")
    merged = _states({("o/r", 1): PrState.MERGED})
    assert apply_moves([move], root, "2026-11-11", merged) == [move]
    assert "- [x] It ships." in (root / "done" / "a.md").read_text()


def test_a_tilde_fence_holding_backtick_lines_stays_one_block():
    body = "# One\n~~~\n```\n# inside\n```\n# still inside\n~~~\n## Two"
    assert _demoted(body) == "### One\n~~~\n```\n# inside\n```\n# still inside\n~~~\n### Two"


def test_a_closed_reason_the_brief_carries_but_no_event_knows_is_not_reopened(tmp_path):
    root = _closed_on_evidence(tmp_path)
    (root / MOVE_LOG).unlink()
    path = root / "done" / "a.md"
    path.write_text(path.read_text().replace("closed_reason: evidence", "closed_reason: odd"))
    with pytest.raises(ValueError, match="has not closed"):
        reopen("a", root, "2026-11-12")


def _thin_twin(tmp_path):
    root = make_inbox(tmp_path, {"a": inbox_text("a", criteria=("Fix it",), **DUP_BODY)})
    (root / "inbox" / "keep.md").write_text(inbox_text("keep", goal="Survivor goal."))
    return root, Move("a", "Title of a", "inbox", Reason.DUPLICATE, "same as keep", "keep")


def test_a_duplicate_with_a_criterion_no_survivor_can_carry_stays_open_but_its_merge_counts(
    tmp_path, capsys
):
    root, move = _thin_twin(tmp_path)
    applied = settle_moves([move], root, "2026-11-11")
    [attempt] = applied.attempts
    assert (attempt.landed, attempt.merged) == (False, True)
    assert "keep does not carry" in attempt.refusal and applied.touched == ["keep"]
    assert applied.refused == [attempt] and applied.landed == []
    assert (root / "inbox" / "a.md").exists() and not (root / MOVE_LOG).exists()
    assert "## Merged from a" in (root / "inbox" / "keep.md").read_text()
    assert "a not closed" in capsys.readouterr().err


def test_a_repeated_refused_duplicate_merges_its_body_once(tmp_path):
    root, move = _thin_twin(tmp_path)
    settle_moves([move], root, "2026-11-11")
    settle_moves([move], root, "2026-11-12")
    assert (root / "inbox" / "keep.md").read_text().count("## Merged from a") == 1


def test_a_close_refused_over_unticked_criteria_is_reported_not_swallowed(
    tmp_path, monkeypatch, capsys
):
    root = make_inbox(tmp_path, {"a": inbox_text("a", criteria=("Ship it.",))})
    refused = UntickedCriteriaError("a", ["- [ ] Ship it."])
    monkeypatch.setattr(triage_close, "_to_done", lambda *args: (_ for _ in ()).throw(refused))
    move = Move("a", "Title of a", "inbox", Reason.EVIDENCE, "x")
    [attempt] = settle_moves([move], root, "2026-11-11").attempts
    assert (attempt.landed, attempt.merged, attempt.refusal) == (
        False,
        False,
        "success criteria still unticked",
    )
    assert "a not closed" in capsys.readouterr().err
