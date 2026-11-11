"""Scope-aware signal weighting in prioritise.py: exact orders per scope, both scorers, and the CLI."""

import datetime
import json

import init_queue
import prioritise
import pytest
import signals
from scope import AllScope, ProjectScope, SessionScope, TagScope, parse_scope
from signals import Signal, SignalRule, Subject

TODAY = datetime.date(2026, 6, 2)


def _brief(bid, *, importance="3", tags="[]", extra="", status="ready", blockers="[]"):
    return f"""---
id: {bid}
title: Title of {bid}
status: {status}
type: todo
importance: {importance}
autonomy: full
estimated-effort: m
tags: {tags}
blockers: {blockers}
{extra}---

## Goal

g
"""


QUEUE = {
    "mult": _brief("mult", tags="[multiplier, wave, project:lite]"),
    "pr": _brief("pr", tags="[pr, wave, project:lite]"),
    "unblock": _brief("unblock", importance="4", tags="[wave]", extra="project: lite\nclears: x\n"),
    "plain": _brief("plain", tags="[wave, project:lite]"),
    "other": _brief("other", importance="1", tags="[elsewhere]"),
}


@pytest.fixture
def root(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    for bid, text in QUEUE.items():
        (root / "ready" / f"{bid}.md").write_text(text)
    return root


def _ids(items):
    return [b["id"] for b in items]


@pytest.mark.parametrize(
    "scope, scorer, expected",
    [
        (AllScope(""), prioritise.Scorer.ADDITIVE, ["mult", "pr", "unblock", "other", "plain"]),
        (AllScope(""), prioritise.Scorer.WSJF, ["other", "mult", "pr", "unblock", "plain"]),
        (TagScope("wave"), prioritise.Scorer.ADDITIVE, ["mult", "pr", "unblock", "plain"]),
        (TagScope("wave"), prioritise.Scorer.WSJF, ["mult", "pr", "unblock", "plain"]),
        (
            SessionScope("s"),
            prioritise.Scorer.ADDITIVE,
            ["mult", "pr", "unblock", "other", "plain"],
        ),
        (SessionScope("s"), prioritise.Scorer.WSJF, ["other", "mult", "pr", "unblock", "plain"]),
        (ProjectScope("lite"), prioritise.Scorer.ADDITIVE, ["pr", "unblock", "mult", "plain"]),
        (ProjectScope("lite"), prioritise.Scorer.WSJF, ["pr", "unblock", "mult", "plain"]),
    ],
)
def test_exact_order_per_scope(root, scope, scorer, expected):
    assert _ids(prioritise.rank(root, today=TODAY, scope=scope, scorer=scorer)) == expected


def test_project_scope_scores_shipping_and_unblocking_above_multipliers(root):
    ranked = {b["id"]: b for b in prioritise.rank(root, today=TODAY, scope=ProjectScope("lite"))}
    assert ranked["pr"]["_score_additive"] == 220 + 2000
    assert ranked["unblock"]["_score_additive"] == 120 + 2000
    assert ranked["mult"]["_score_additive"] == 220 + 500
    assert ranked["pr"]["_score_wsjf"] == round((300 + 2000) / 3, 1)
    assert ranked["pr"]["_why"]["signals"] == {"ships-pr": 2000}
    assert ranked["pr"]["_why"]["scope"] == "project:lite"


def test_all_scope_scores_multipliers_first(root):
    ranked = {b["id"]: b for b in prioritise.rank(root, today=TODAY)}
    assert ranked["mult"]["_score_additive"] == 220 + 1500
    assert ranked["mult"]["_why"]["signals"] == {"multiplier": 1500}
    assert ranked["plain"]["_why"]["signals"] == {}


def test_the_fleet_blocker_lane_stays_above_every_signal(root, tmp_path):
    blockers = tmp_path / "blockers"
    blockers.mkdir()
    record = {"id": "down", "summary": "Hooks down", "status": "active", "probe": "true"}
    (blockers / "down.json").write_text(json.dumps(record))
    (root / "tasks.toml").write_text(f'name = "demo"\nfleet_blockers_dir = "{blockers}"\n')
    (root / "ready" / "fix.md").write_text(_brief("fix", importance="5", extra="clears: down\n"))
    ranked = prioritise.rank(root, today=TODAY, scorer=prioritise.Scorer.WSJF)
    assert ranked[0]["id"] == "fix" and ranked[0]["_lane"] is prioritise.Lane.FLEET_BLOCKER


def test_ids_rank_named_briefs_wherever_they_are_live(root):
    (root / "in-progress" / "doing.md").write_text(_brief("doing", tags="[pr]"))
    (root / "done" / "gone.md").write_text(_brief("gone", tags="[multiplier]"))
    ranked = prioritise.rank(
        root, today=TODAY, scope=SessionScope("s"), ids=frozenset({"doing", "plain", "gone"})
    )
    assert _ids(ranked) == ["doing", "plain"]


def test_a_waiting_brief_gives_its_blocker_the_clears_blocker_signal(root):
    (root / "ready" / "waiter.md").write_text(_brief("waiter", blockers="[plain]"))
    ranked = {b["id"]: b for b in prioritise.rank(root, today=TODAY)}
    assert ranked["plain"]["_why"]["signals"] == {"clears-blocker": 800}


def test_tasks_toml_overrides_one_matcher_and_one_weight(root):
    (root / "tasks.toml").write_text(
        'name = "demo"\n[priority.signals.multiplier]\ntags = ["wave"]\n'
        "[priority.scope_weights.all]\nmultiplier = 10\n"
    )
    ranked = {b["id"]: b for b in prioritise.rank(root, today=TODAY)}
    assert ranked["plain"]["_why"]["signals"] == {"multiplier": 10}
    assert ranked["pr"]["_why"]["signals"] == {"multiplier": 10, "ships-pr": 800}


def test_a_bad_signal_table_falls_back_to_the_defaults(root, capsys):
    (root / "tasks.toml").write_text(
        'name = "demo"\n[priority.signals.multiplier]\nlabels = ["x"]\n'
    )
    ranked = {b["id"]: b for b in prioritise.rank(root, today=TODAY)}
    assert ranked["mult"]["_why"]["signals"] == {"multiplier": 1500}
    assert "unknown matcher(s) ['labels']" in capsys.readouterr().err


def _subject(fields=None, tags=(), leverage=0):
    return Subject(fields or {}, frozenset(tags), leverage)


@pytest.mark.parametrize(
    "table, subject, expected",
    [
        ({"types": ["bug"]}, _subject({"type": "bug"}), True),
        ({"types": ["bug"]}, _subject({"type": "todo"}), False),
        ({"fields": ["pr"]}, _subject({"pr": "https://x/pull/1"}), True),
        ({"fields": ["pr"]}, _subject({"pr": "[]"}), False),
        ({"fields": ["pr"]}, _subject({"pr": None}), False),
        ({"min_leverage": 2}, _subject(leverage=2), True),
        ({"min_leverage": 2}, _subject(leverage=1), False),
        ({"min_leverage": 0}, _subject(leverage=5), False),
        ({"tags": []}, _subject(tags=("pr",)), False),
    ],
)
def test_matchers(table, subject, expected):
    assert SignalRule.from_table("ships-pr", table).matches(subject) is expected


def test_a_matcher_list_must_be_a_list():
    with pytest.raises(TypeError, match="expected a list of strings"):
        SignalRule.from_table("multiplier", {"tags": "multiplier"})


def test_an_unknown_scope_kind_weighs_nothing():
    weights = signals.SignalWeights.from_tables({"multiplier": {"tags": ["m"]}}, {})
    assert weights.values(_subject(tags=("m",)), "all") == {"multiplier": 0.0}


def test_subject_reads_project_from_the_field():
    assert _subject({"project": " lite "}).project == "lite"
    assert ProjectScope("lite").admits(_subject({"project": "lite"}))
    assert not ProjectScope("lite").admits(_subject({"project": "other"}))
    assert Signal("clears-blocker") is Signal.CLEARS_BLOCKER


def test_parse_scope_is_shared():
    assert parse_scope("project:lite").key == "project:lite"


def test_cli_scope_root_and_ids(root, capsys):
    prioritise.main(["--scope", "project:lite", "--root", str(root), "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["pr", "unblock", "mult", "plain"]
    assert rows[0]["why"]["signals"] == {"ships-pr": 2000} and rows[0]["schema_version"] == 3
    assert rows[0]["status"] == "ready"
    prioritise.main(["--scope", "session:s", "--root", str(root), "--ids", "plain, pr", "--json"])
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == ["pr", "plain"]


def test_cli_text_names_each_signal(root, capsys):
    prioritise.main(["--root", str(root), "--top", "1"])
    assert "+multiplier 1500" in capsys.readouterr().out


def test_cli_session_scope_uses_the_bound_queue(root, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_TASKS_SESSIONS", str(tmp_path / "sessions"))
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "s1.toml").write_text(f'root = "{root}"\n')
    prioritise.main(["--scope", "session:s1", "--json", "--top", "1"])
    assert json.loads(capsys.readouterr().out)[0]["id"] == "mult"
