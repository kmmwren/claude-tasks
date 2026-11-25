"""Tests for routing at promotion: triage asks the router about each brief it promotes and loses none."""

import dataclasses
import json

import pytest
import triage
from git_sync import SyncResult
from triage_route import Placement, RouterPlan

from tests.queue_helpers import inbox_text, make_inbox

READY = dict(goal="Ship it.", criteria=("It ships.",), blocked_by="[]")
GATE = triage.Gate({"o/r#2"}.__contains__)
FILED = inbox_text("filed", context="Per o/r#2.", **READY)
NOTHING = RouterPlan({}, {})
DIRECT = RouterPlan({"filed": Placement("alpha", "direct")}, {})
BY_PR = RouterPlan({"filed": Placement("beta", "pr")}, {})
TORN = RouterPlan({}, {"filed": ("alpha", "beta")})


class FakeRouter:
    """Answers `plan` with a canned plan and records every call in `events`, shared with the commit."""

    def __init__(self, events, plan, applies=True):
        self.events, self._plan, self.applies = events, plan, applies
        self.asked = 0

    def plan(self):
        self.asked += 1
        return self._plan

    def apply(self, project, ids):
        self.events.append(("apply", project, list(ids)))
        return self.applies


class Bell:
    def __init__(self):
        self.texts = []

    def __call__(self, text):
        self.texts.append(text)
        return True


@pytest.fixture
def events(monkeypatch):
    log = []
    monkeypatch.setattr(
        triage,
        "sync_commit",
        lambda msg, root, ids, paths=(): (
            log.append(("commit", sorted(ids))) or SyncResult.COMMITTED
        ),
    )
    return log


def _run(tmp_path, events, plan, *, apply=True, applies=True, queue=None):
    root = make_inbox(tmp_path, {"filed": FILED} if queue is None else queue)
    router, bell = FakeRouter(events, plan, applies), Bell()
    base = triage.Options(
        root, apply, GATE, None, batch_size=5, jobs=1, limit=None, today="2026-11-25"
    )
    opts = dataclasses.replace(base, router=router, notify=bell)
    return root, triage.run(opts), router, bell


def test_a_promoted_brief_that_clearly_belongs_to_a_project_is_routed_after_triage_commits(
    tmp_path, events
):
    root, report, _, bell = _run(tmp_path, events, DIRECT)
    assert report.routed == ["filed"] and report.alerts == [] and bell.texts == []
    assert [e[0] for e in events] == ["commit", "apply"]
    assert events[1] == ("apply", "alpha", ["filed"])
    assert (root / "ready" / "filed.md").exists()


def test_a_brief_the_router_is_unsure_of_stays_in_ready_and_raises_an_alert(tmp_path, events):
    root, report, _, bell = _run(tmp_path, events, TORN)
    assert report.routed == [] and [e[0] for e in events] == ["commit"]
    assert (root / "ready" / "filed.md").exists()
    [text] = bell.texts
    assert (
        "routing unsure (1):" in text
        and "- filed: Title of filed (could belong to alpha, beta)" in text
    )
    assert json.loads((root / ".triage-alerts.json").read_text()) == {"filed": "2026-11-25"}


def test_a_brief_for_a_pr_only_queue_stays_in_ready_and_the_alert_names_the_gap(tmp_path, events):
    root, report, _, bell = _run(tmp_path, events, BY_PR)
    assert report.routed == [] and [e[0] for e in events] == ["commit"]
    assert (root / "ready" / "filed.md").exists()
    assert "route needs a PR (1):" in bell.texts[0] and "no PR is opened here" in bell.texts[0]


def test_a_brief_already_in_a_project_or_judged_generic_is_promoted_with_no_route_or_alert(
    tmp_path, events
):
    root, report, router, bell = _run(tmp_path, events, NOTHING)
    assert router.asked == 1 and report.routes == [] and report.routed == [] and bell.texts == []
    assert [e[0] for e in events] == ["commit"] and (root / "ready" / "filed.md").exists()


def test_with_nothing_promoted_the_router_is_not_asked(tmp_path, events):
    _, _, router, _ = _run(tmp_path, events, DIRECT, queue={"bare": inbox_text("bare")})
    assert router.asked == 0


def test_an_unavailable_router_leaves_the_promoted_brief_in_ready_and_says_so(tmp_path, events):
    root, report, _, _ = _run(tmp_path, events, None)
    assert report.routed == [] and "router unavailable" in report.route_notice
    assert (root / "ready" / "filed.md").exists()


def test_a_route_the_router_could_not_make_leaves_the_brief_and_says_so(tmp_path, events):
    root, report, _, _ = _run(tmp_path, events, DIRECT, applies=False)
    assert report.routed == [] and "could not route to alpha" in report.route_notice
    assert (root / "ready" / "filed.md").exists()


def test_a_dry_run_shows_the_decisions_and_alerts_but_moves_and_sends_nothing(tmp_path, events):
    root, report, _, bell = _run(tmp_path, events, TORN, apply=False)
    assert [d.brief_id for d in report.routes] == ["filed"] and len(report.alerts) == 1
    assert events == [] and bell.texts == [] and (root / "inbox" / "filed.md").exists()


def test_no_router_configured_means_no_routing(tmp_path, events):
    root = make_inbox(tmp_path, {"filed": FILED})
    opts = triage.Options(root, True, GATE, None, 5, 1, None, "2026-11-25")
    report = triage.run(opts)
    assert (
        report.routes == [] and report.route_notice == "" and (root / "ready" / "filed.md").exists()
    )
