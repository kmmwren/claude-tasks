"""Tests for triage_alerts.py: judgement calls raise one aggregated alert and move nothing."""

import datetime
import json
import pathlib

import pytest
from class_log import BriefClass, Verdict
from triage_alerts import (
    ALERT_STATE,
    Alert,
    AlertLedger,
    AlertReason,
    alert_text,
    alerts_plan,
    routing_alerts,
    session_alert_notifier,
    undelivered,
    verdict_alerts,
)
from triage_brief import InboxBrief
from triage_close import CloseRules
from triage_route import Decision, Outcome

from tests.queue_helpers import inbox_text

BC = BriefClass
TODAY = datetime.date(2026, 11, 11)
RULES = CloseRules(stale_days=21, max_moves=200)
OLD = "created: 2026-10-01\n"


def _brief(bid="b", **kw):
    return InboxBrief(pathlib.Path(f"{bid}.md"), inbox_text(bid, **kw))


def _verdict(bid, cls, reason="because"):
    return Verdict(bid, cls, reason, f"Title of {bid}")


def _alerts(briefs, verdicts, rules=RULES):
    return verdict_alerts(verdicts, {b.id: b for b in briefs}, rules, TODAY)


def test_needs_info_untouched_past_the_ttl_is_flagged_not_moved():
    [alert] = _alerts([_brief("a", extra=OLD)], [_verdict("a", BC.NEEDS_INFO)])
    assert alert == Alert("a", "Title of a", AlertReason.STALE, "untouched for 41 days")


def test_needs_info_exactly_at_the_ttl_is_not_yet_stale():
    on_ttl = f"created: {TODAY - datetime.timedelta(days=21)}\n"
    assert _alerts([_brief("a", extra=on_ttl)], [_verdict("a", BC.NEEDS_INFO)]) == []


def test_needs_info_with_no_date_or_a_recent_one_is_left_alone():
    briefs = [_brief("a"), _brief("b", extra="updated: 2026-11-10\n")]
    assert _alerts(briefs, [_verdict("a", BC.NEEDS_INFO), _verdict("b", BC.NEEDS_INFO)]) == []


def test_the_ttl_is_the_configured_one():
    brief = _brief("a", extra=OLD)
    assert _alerts([brief], [_verdict("a", BC.NEEDS_INFO)], CloseRules(stale_days=60)) == []


def test_a_drop_verdict_is_flagged_with_the_models_reason():
    [alert] = _alerts([_brief("a")], [_verdict("a", BC.DROP, "obsolete")])
    assert (alert.reason, alert.detail) == (AlertReason.DROP, "obsolete")


@pytest.mark.parametrize("cls", [BC.READY, BC.DUPLICATE])
def test_other_verdicts_raise_nothing(cls):
    assert _alerts([_brief("a", extra=OLD)], [_verdict("a", cls)]) == []


@pytest.mark.parametrize(
    "key", ["assignee: me\n", "closed_reason: evidence\n", "reopened: 2026-11-12\n"]
)
def test_a_claimed_or_restored_brief_is_never_flagged(key):
    assert _alerts([_brief("a", extra=key + OLD)], [_verdict("a", BC.DROP)]) == []


def _alert(bid, reason=AlertReason.STALE, detail="untouched for 30 days"):
    return Alert(bid, f"Title of {bid}", reason, detail)


def test_a_new_flag_is_due_and_a_recent_one_is_held(tmp_path):
    ledger = AlertLedger(tmp_path)
    ledger.save([_alert("a")], {"a"}, TODAY - datetime.timedelta(days=21))
    due, held = AlertLedger(tmp_path).due([_alert("a"), _alert("b")], TODAY, 21)
    assert [a.brief_id for a in due] == ["b"] and [a.brief_id for a in held] == ["a"]


def test_a_flag_is_raised_again_once_stale_days_more_have_passed(tmp_path):
    AlertLedger(tmp_path).save([_alert("a")], {"a"}, TODAY - datetime.timedelta(days=22))
    due, held = AlertLedger(tmp_path).due([_alert("a")], TODAY, 21)
    assert [a.brief_id for a in due] == ["a"] and held == []


def test_saving_keeps_held_dates_records_deliveries_and_forgets_cleared_briefs(tmp_path):
    earlier = TODAY - datetime.timedelta(days=5)
    ledger = AlertLedger(tmp_path)
    ledger.save([_alert("held"), _alert("gone")], {"held", "gone"}, earlier)
    ledger.save([_alert("held"), _alert("new"), _alert("undelivered")], {"new"}, TODAY)
    stored = json.loads((tmp_path / ALERT_STATE).read_text())
    assert stored == {"held": earlier.isoformat(), "new": TODAY.isoformat()}
    assert AlertLedger(tmp_path).raised == {"held": earlier, "new": TODAY}


@pytest.mark.parametrize("body", ["", "not json", "[1]", '{"a": 5}', '{"a": "soon"}'])
def test_an_unreadable_ledger_means_nothing_was_alerted(tmp_path, body):
    (tmp_path / ALERT_STATE).write_text(body)
    assert AlertLedger(tmp_path).raised == {}


def test_a_missing_ledger_means_nothing_was_alerted(tmp_path):
    assert AlertLedger(tmp_path).raised == {}


def test_the_alert_names_the_count_each_brief_and_that_none_moved():
    text = alert_text([_alert("s1"), _alert("d1", AlertReason.DROP, "obsolete"), _alert("s2")], 21)
    lines = text.splitlines()
    assert lines[0].startswith("Tell Katherine, she decides")
    assert "3 briefs" in lines[0] and "none was moved" in lines[0] and "21 more days" in lines[0]
    assert lines[1:] == [
        "stale needs-info (2):",
        "- s1: Title of s1 (untouched for 30 days)",
        "- s2: Title of s2 (untouched for 30 days)",
        "model-suggested drop (1):",
        "- d1: Title of d1 (obsolete)",
    ]


def test_the_dry_run_plan_lists_the_alerts_and_the_held_count():
    plan = alerts_plan([_alert("s1"), _alert("d1", AlertReason.DROP, "obsolete")], 2)
    assert plan.splitlines() == [
        "2 alerts to raise (briefs stay where they are)",
        "stale needs-info: 1",
        "  s1: untouched for 30 days",
        "model-suggested drop: 1",
        "  d1: obsolete",
        "2 more already alerted recently",
    ]


def test_an_empty_alert_plan_says_so():
    assert alerts_plan([], 0) == "0 alerts to raise (briefs stay where they are)"


def _scripts(tmp_path, body):
    (tmp_path / "session_alerts.py").write_text(body)
    return tmp_path


def _make(tmp_path, body):
    folder = tmp_path / "scripts"
    folder.mkdir()
    return _scripts(folder, body)


POSTER = (
    "import json, os\n"
    "def post(channel, project, kind, text):\n"
    "    open(os.environ['ALERT_SINK'], 'a').write(json.dumps([channel, project, kind, text]))\n"
)


def test_the_notifier_posts_one_record_to_the_triage_channel(tmp_path, monkeypatch):
    sink = tmp_path / "sink.json"
    monkeypatch.setenv("ALERT_SINK", str(sink))
    notify = session_alert_notifier(_make(tmp_path, POSTER))
    assert notify("hello") is True
    assert json.loads(sink.read_text()) == [
        "triage",
        "claude-tasks",
        "triage-needs-decision",
        "hello",
    ]


def test_a_missing_alert_script_warns_on_stderr_and_reports_not_delivered(tmp_path, capsys):
    assert session_alert_notifier(tmp_path / "nowhere")("hello") is False
    assert "alert not delivered" in capsys.readouterr().err


@pytest.mark.parametrize(
    "body",
    [
        "raise ImportError('missing dependency')\n",
        "def broken(:\n",
        "x = 1\n",
        "def post(*a):\n    raise OSError('disk full')\n",
        "def post(*a):\n    raise ValueError('bad payload')\n",
        "raise SystemExit(2)\n",
        "import sys\ndef post(*a):\n    sys.exit('no credentials')\n",
    ],
)
def test_a_broken_alert_script_warns_instead_of_raising(tmp_path, capsys, body):
    assert session_alert_notifier(_make(tmp_path, body))("hello") is False
    assert "alert not delivered" in capsys.readouterr().err


def test_the_default_notifier_warns_and_delivers_nothing(capsys):
    assert undelivered("hello") is False
    assert "no notifier configured" in capsys.readouterr().err


def test_a_brief_the_router_left_in_the_general_queue_is_flagged_by_why():
    unsure = Decision("a", "Title of a", Outcome.UNSURE, ("alpha", "beta"))
    needs_pr = Decision("b", "Title of b", Outcome.NEEDS_PR, ("beta",))
    routed = Decision("c", "Title of c", Outcome.ROUTE, ("alpha",))
    assert routing_alerts([unsure, needs_pr, routed]) == [
        Alert("a", "Title of a", AlertReason.ROUTE_UNSURE, "could belong to alpha, beta"),
        Alert(
            "b",
            "Title of b",
            AlertReason.ROUTE_NEEDS_PR,
            "belongs to beta, whose queue takes briefs by PR; no PR is opened here",
        ),
    ]
