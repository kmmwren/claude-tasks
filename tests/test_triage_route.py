"""Tests for triage_route.py: reading the router's plan, deciding per brief, and calling its CLI."""

import json
import pathlib

import pytest
import triage_route
from triage_brief import InboxBrief
from triage_route import (
    ROUTER_ENV,
    Decision,
    Outcome,
    RouterCli,
    RouterPlan,
    routes_by_project,
    routes_plan,
)

from tests.queue_helpers import inbox_text

PLAN = json.dumps(
    {
        "moves": [
            {"id": "direct", "project": "alpha", "queue_mode": "direct", "scores": {}},
            {"id": "by-pr", "project": "beta", "queue_mode": "pr", "scores": {}},
        ],
        "ambiguous": [{"id": "torn", "scores": {"alpha": {"points": 1}, "beta": {"points": 2}}}],
        "clusters": {},
        "generic": 0,
    }
)


def _brief(bid):
    return InboxBrief(pathlib.Path(f"{bid}.md"), inbox_text(bid))


def test_a_move_into_a_direct_queue_is_a_route():
    assert RouterPlan.parse(PLAN).decide(_brief("direct")) == Decision(
        "direct", "Title of direct", Outcome.ROUTE, ("alpha",)
    )


def test_a_move_into_a_pr_queue_is_a_route():
    decision = RouterPlan.parse(PLAN).decide(_brief("by-pr"))
    assert decision == Decision("by-pr", "Title of by-pr", Outcome.ROUTE, ("beta",))


def test_an_ambiguous_brief_is_unsure_between_the_projects_it_scored_in():
    decision = RouterPlan.parse(PLAN).decide(_brief("torn"))
    assert decision == Decision("torn", "Title of torn", Outcome.UNSURE, ("alpha", "beta"))
    assert decision.named == "alpha, beta"


def test_a_brief_the_router_has_no_view_on_gets_no_decision():
    plan = RouterPlan.parse(PLAN)
    assert plan.decide(_brief("elsewhere")) is None
    found = plan.decide_all([_brief("elsewhere"), _brief("direct")])
    assert [d.brief_id for d in found] == ["direct"]


def test_routes_are_grouped_for_apply_in_any_mode():
    decisions = RouterPlan.parse(PLAN).decide_all([_brief(b) for b in ("direct", "by-pr", "torn")])
    assert routes_by_project(decisions) == {"alpha": ["direct"], "beta": ["by-pr"]}
    assert routes_plan(decisions).splitlines() == [
        "3 promoted briefs the router has a view on",
        "  direct: route alpha",
        "  by-pr: route beta",
        "  torn: unsure alpha, beta",
    ]


def _fake_router(tmp_path, body):
    script = tmp_path / "router.py"
    script.write_text(f"import sys\n{body}\n")
    return RouterCli(script)


def test_plan_reads_the_routers_json(tmp_path):
    cli = _fake_router(tmp_path, f"print({PLAN!r}) if sys.argv[1:] == ['plan'] else sys.exit(9)")
    assert set(cli.plan().moves) == {"direct", "by-pr"}


def test_apply_passes_the_project_and_ids(tmp_path):
    log = tmp_path / "argv.txt"
    cli = _fake_router(tmp_path, f"open({str(log)!r}, 'w').write(' '.join(sys.argv[1:]))")
    assert cli.apply("beta", ["a", "b"]) and log.read_text() == "apply beta a b"


def test_a_failing_router_warns_and_gives_nothing(tmp_path, capsys):
    cli = _fake_router(tmp_path, "sys.exit(1)")
    assert cli.plan() is None and not cli.apply("beta", ["a"])
    assert "router plan failed" in capsys.readouterr().err


def test_a_named_router_that_is_absent_warns_and_gives_nothing(tmp_path, capsys):
    assert RouterCli(tmp_path / "nowhere.py").plan() is None
    assert "promoted briefs stay in the general queue" in capsys.readouterr().err


@pytest.mark.parametrize("output", ["not json", "{}", "[]"])
def test_a_plan_that_is_not_understood_warns_and_gives_nothing(tmp_path, capsys, output):
    assert _fake_router(tmp_path, f"print({output!r})").plan() is None
    assert "not understood" in capsys.readouterr().err


def test_the_env_names_the_router(tmp_path, monkeypatch):
    monkeypatch.setenv(ROUTER_ENV, str(tmp_path / "mine.py"))
    assert RouterCli.configured() == RouterCli(tmp_path / "mine.py")


def test_with_no_env_the_installed_router_is_used_when_it_exists(tmp_path, monkeypatch):
    monkeypatch.delenv(ROUTER_ENV)
    monkeypatch.setattr(triage_route, "ROUTER_SCRIPT", tmp_path / "router.py")
    assert RouterCli.configured() is None
    (tmp_path / "router.py").write_text("")
    assert RouterCli.configured() == RouterCli(tmp_path / "router.py")
