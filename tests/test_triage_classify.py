"""Tests for triage_classify.py: the class each inbox brief gets, and learning from being overruled."""

import subprocess

import pytest
import triage_classify
from class_log import BriefClass, ClassLog, Example, Verdict
from fingerprint import index
from headless import ScriptLaunch
from triage_brief import InboxBrief
from triage_classify import (
    CLASSIFY_SCHEMA,
    Call,
    calls_for,
    classify,
    claude_classify,
    contradicting_class,
    duplicate_map,
    fixed_calls,
    parse_calls,
    reconcile,
    render_prompt,
)

from tests.queue_helpers import inbox_text, make_inbox

C = BriefClass
LAUNCH = ScriptLaunch("/l.sh")


def brief(tmp_path, bid, **kw):
    return InboxBrief(tmp_path / f"{bid}.md", inbox_text(bid, **kw))


def test_later_briefs_with_one_fingerprint_duplicate_the_earliest_or_the_one_past_the_inbox(
    tmp_path,
):
    root = make_inbox(
        tmp_path, {"b": inbox_text("b", title="Same"), "a": inbox_text("a", title="Same")}
    )
    (root / "ready" / "r.md").write_text(inbox_text("r", title="Other"))
    (root / "inbox" / "c.md").write_text(inbox_text("c", title="Other"))
    assert duplicate_map(index(root)) == {"b": "a", "c": "r"}


def test_classify_ranks_duplicate_over_promotion_over_the_residue(tmp_path):
    dup, ready, thin, bare = (
        brief(tmp_path, "dup"),
        brief(tmp_path, "ready"),
        brief(tmp_path, "thin", title="Fix o/r#1"),
        brief(tmp_path, "bare"),
    )
    asked = []

    def call(briefs, examples):
        asked.append(([b.id for b in briefs], list(examples)))
        return {"bare": Call(C.DROP, "stale idea")}

    shots = [Example("t", C.NEEDS_INFO, "r", C.READY)]
    verdicts = classify(
        [dup, ready, thin, bare, brief(tmp_path, "held")],
        fixed_calls({"dup": "orig"}, {"held"}),
        {"ready": "ready as filed", "held": "ready as filed"},
        call,
        shots,
        batch=5,
        jobs=1,
    )
    assert [(v.brief_id, v.cls, v.reason) for v in verdicts] == [
        ("dup", C.DUPLICATE, "same title and sources as orig"),
        ("ready", C.READY, "ready as filed"),
        ("bare", C.DROP, "stale idea"),
        ("held", C.NEEDS_INFO, "moved back from ready by the owner"),
    ]
    assert asked == [(["thin", "bare"], shots)]


def test_without_a_model_the_residue_is_needs_info_with_the_reason_from_the_brief(tmp_path):
    cited = brief(tmp_path, "c", goal="Do it.", criteria=("Done.",), context="See o/r#9.")
    verdicts = classify([cited, brief(tmp_path, "m")], {}, {}, None, [], batch=5, jobs=1)
    assert verdicts[0].brief_id == "c"
    assert [(v.cls, v.reason) for v in verdicts] == [
        (C.NEEDS_INFO, "cites no source that exists now"),
        (C.NEEDS_INFO, "missing goal, criteria, context"),
    ]


def test_a_brief_the_model_was_asked_about_and_left_unanswered_gets_no_verdict(tmp_path):
    unanswered = classify([brief(tmp_path, "b")], {}, {}, lambda b, e: {}, [], batch=5, jobs=1)
    assert unanswered == []


def test_calls_are_asked_in_batches_and_a_failed_batch_is_skipped(tmp_path):
    briefs = [brief(tmp_path, f"b{i}") for i in range(3)]

    def call(batch, examples):
        if batch[0].id == "b2":
            return {}
        return {b.id: Call(C.DROP, "x") for b in batch}

    assert sorted(calls_for(briefs, call, [], size=2, jobs=2)) == ["b0", "b1"]


def test_prompt_wraps_briefs_and_shows_the_overruled_calls_as_data(tmp_path):
    shot = Example("Fix the thing", C.DROP, "stale", C.READY)
    prompt = render_prompt([brief(tmp_path, "b")], [shot])
    assert '<brief id="b">' in prompt and "never instructions" in prompt
    assert (
        '"Fix the thing"' in prompt
        and "called drop (stale)" in prompt
        and "moved it to ready" in prompt
    )
    assert "overruled" not in render_prompt([brief(tmp_path, "b")], [])


GOOD = {"id": "b", "class": "drop", "reason": " stale "}


def test_parse_keeps_valid_calls_for_asked_ids():
    assert parse_calls({"briefs": [GOOD, {**GOOD, "id": "z"}]}, {"b"}) == {
        "b": Call(C.DROP, "stale")
    }


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"briefs": "x"},
        {"briefs": ["x"]},
        {"briefs": [GOOD | {"class": "ready"}]},
        {"briefs": [GOOD | {"class": "bogus"}]},
        {"briefs": [GOOD | {"reason": 3}]},
        {"briefs": [GOOD | {"reason": " "}]},
        {"briefs": [GOOD | {"id": 3}]},
    ],
)
def test_parse_drops_anything_off_schema_and_never_accepts_ready(payload):
    assert parse_calls(payload, {"b"}) == {}


def test_the_schema_offers_only_the_classes_a_model_may_give():
    item = CLASSIFY_SCHEMA["properties"]["briefs"]["items"]["properties"]["class"]
    assert item["enum"] == ["needs-info", "drop"]


def test_claude_classify_asks_with_the_schema_and_parses_the_answer(tmp_path, monkeypatch):
    def ask(prompt, schema, model, reason, timeout, launch):
        assert (
            schema is CLASSIFY_SCHEMA
            and "tools off" in reason
            and model == "m"
            and launch == LAUNCH
        )
        return {"briefs": [GOOD]}

    monkeypatch.setattr(triage_classify, "ask_json", ask)
    assert claude_classify("m", launch=LAUNCH)([brief(tmp_path, "b")], []) == {
        "b": Call(C.DROP, "stale")
    }


@pytest.mark.parametrize(
    "predicted, folder, actual",
    [
        (C.READY, "inbox", C.NEEDS_INFO),
        (C.READY, "parked", C.NEEDS_INFO),
        (C.READY, "ready", None),
        (C.READY, "in-progress", None),
        (C.READY, "done", None),
        (C.NEEDS_INFO, "ready", C.READY),
        (C.DROP, "in-progress", C.READY),
        (C.DUPLICATE, "ready", C.READY),
        (C.NEEDS_INFO, "inbox", None),
        (C.DROP, "done", None),
        (C.DROP, "parked", None),
    ],
)
def test_a_brief_found_against_the_call_contradicts_it(predicted, folder, actual):
    assert contradicting_class(predicted, folder) is actual


def test_reconcile_logs_one_correction_for_each_brief_found_against_its_verdict(tmp_path):
    root = make_inbox(tmp_path, {"back": inbox_text("back"), "stuck": inbox_text("stuck")})
    (root / "ready" / "up.md").write_text(inbox_text("up"))
    log = ClassLog(root)
    for bid, cls in (("back", C.READY), ("stuck", C.NEEDS_INFO), ("up", C.DROP), ("gone", C.READY)):
        log.append(Verdict(bid, cls, "r", f"T {bid}", f"at-{bid}"))
    found = reconcile(root, log)
    assert sorted((c.brief_id, c.predicted, c.actual, c.moved_to) for c in found) == [
        ("back", C.READY, C.NEEDS_INFO, "inbox"),
        ("up", C.DROP, C.READY, "ready"),
    ]
    assert reconcile(root, log) == []
    assert [e.title for e in log.examples(5)] == ["T back", "T up"]


def _refusing(status, calls):
    def run(cmd, **kw):
        calls.append(cmd)
        raise subprocess.CalledProcessError(status, cmd, stderr="launcher: no account free\n")

    return run


def test_once_no_account_can_run_a_call_no_later_batch_is_attempted(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", _refusing(4, calls))
    briefs = [brief(tmp_path, f"b{i}") for i in range(6)]
    assert calls_for(briefs, claude_classify("m"), [], size=1, jobs=1) == {}
    assert len(calls) == 1


def test_a_spent_run_makes_no_call_from_any_worker_thread(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", _refusing(4, calls))
    briefs = [brief(tmp_path, f"b{i}") for i in range(6)]
    calls_for(briefs, claude_classify("m"), [], size=1, jobs=1)
    calls.clear()
    assert calls_for(briefs, claude_classify("m"), [], size=1, jobs=3) == {}
    assert calls == []


def _known(*briefs, cls=C.NEEDS_INFO):
    return {b.id: Verdict(b.id, cls, "earlier call", b.title, digest=b.digest) for b in briefs}


def test_a_brief_unchanged_since_its_model_call_is_not_asked_about_again(tmp_path):
    seen, edited = brief(tmp_path, "seen"), brief(tmp_path, "edited")
    known = _known(seen, edited)
    stale = InboxBrief(edited.path, edited.text + "\nA new paragraph.\n")
    asked = []

    def call(briefs, examples):
        asked.append([b.id for b in briefs])
        return {b.id: Call(C.DROP, "now stale") for b in briefs}

    verdicts = classify([seen, stale, brief(tmp_path, "new")], {}, {}, call, [], 5, 1, known)
    assert asked == [["edited", "new"]]
    assert {v.brief_id: (v.cls, v.reason) for v in verdicts} == {
        "seen": (C.NEEDS_INFO, "earlier call"),
        "edited": (C.DROP, "now stale"),
        "new": (C.DROP, "now stale"),
    }


def test_a_steady_state_inbox_makes_no_model_call(tmp_path):
    briefs = [brief(tmp_path, f"b{i}") for i in range(4)]

    def call(batch, examples):
        raise AssertionError("asked about an unchanged brief")

    verdicts = classify(briefs, {}, {}, call, [], 2, 1, _known(*briefs, cls=C.DROP))
    assert [v.cls for v in verdicts] == [C.DROP] * 4


@pytest.mark.parametrize("earlier", [C.READY, C.DUPLICATE])
def test_only_a_model_verdict_is_reused(tmp_path, earlier):
    b = brief(tmp_path, "b")
    asked = []

    def call(batch, examples):
        asked.append(1)
        return {"b": Call(C.DROP, "fresh")}

    classify([b], {}, {}, call, [], 5, 1, _known(b, cls=earlier))
    assert asked == [1]


def test_a_verdict_without_a_digest_is_asked_again_once(tmp_path):
    b = brief(tmp_path, "b")
    legacy = {"b": Verdict("b", C.DROP, "old", b.title)}
    verdicts = classify([b], {}, {}, lambda bs, e: {"b": Call(C.DROP, "new")}, [], 5, 1, legacy)
    assert verdicts[0].reason == "new" and verdicts[0].digest == b.digest


def test_every_verdict_carries_the_digest_of_the_text_it_judged(tmp_path):
    b = brief(tmp_path, "b")
    (verdict,) = classify([b], {}, {}, None, [], 5, 1)
    assert verdict.digest == b.digest != brief(tmp_path, "b", title="Other").digest
