"""Tests for routing at promotion: triage asks the router about each brief it promotes and loses none."""

import dataclasses

import pytest
import triage
import triage_route
from git_sync import SyncResult
from triage_route import Placement, RouterCli, RouterPlan

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
        self.landed = 0

    def plan(self):
        self.asked += 1
        return self._plan

    def apply(self, project, ids):
        self.events.append(("apply", project, list(ids)))
        return self.applies

    def land(self):
        self.landed += 1


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


def test_a_brief_the_router_is_unsure_of_stays_in_ready_and_is_left_to_the_routers_judge(
    tmp_path, events
):
    root, report, router, bell = _run(tmp_path, events, TORN)
    assert report.routed == [] and [e[0] for e in events] == ["commit"]
    assert (root / "ready" / "filed.md").exists()
    assert report.alerts == [] and bell.texts == [] and router.landed == 0
    assert not (root / ".triage-alerts.json").exists()


def test_a_brief_for_a_pr_queue_is_routed_like_any_other_and_the_land_is_started(tmp_path, events):
    _, report, router, _ = _run(tmp_path, events, BY_PR)
    assert report.routed == ["filed"] and events[1] == ("apply", "beta", ["filed"])
    assert router.landed == 1


def test_no_needs_a_pr_alert_is_raised(tmp_path, events):
    _, report, _, bell = _run(tmp_path, events, BY_PR)
    assert report.alerts == [] and bell.texts == []


def test_a_routed_brief_starts_a_detached_land(tmp_path, events, monkeypatch):
    log = tmp_path / "router.log"
    started = []

    def fake_spawn(launch, args, reason, log_path, cwd, env=None):
        started.append((launch.argv(args), log_path, cwd))
        return 1

    monkeypatch.setattr(triage_route, "spawn_detached", fake_spawn)
    monkeypatch.setattr(triage_route, "ROUTER_LOG", log)
    script = tmp_path / "router.py"
    script.write_text("")
    root = make_inbox(tmp_path, {"filed": FILED})
    base = triage.Options(root, True, GATE, None, 5, 1, None, "2026-11-25")
    router = RouterCli(script)
    monkeypatch.setattr(RouterCli, "plan", lambda self: DIRECT)
    monkeypatch.setattr(RouterCli, "apply", lambda self, project, ids: True)
    report = triage.run(dataclasses.replace(base, router=router, notify=Bell()))
    assert report.routed == ["filed"]
    assert started == [([triage_route.sys.executable, str(script), "land"], log, tmp_path)]


def test_nothing_is_landed_when_nothing_was_routed(tmp_path, events):
    _, _, router, _ = _run(tmp_path, events, DIRECT, applies=False)
    assert router.landed == 0


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
    assert [d.brief_id for d in report.routes] == ["filed"] and report.alerts == []
    assert events == [] and bell.texts == [] and (root / "inbox" / "filed.md").exists()


def test_no_router_configured_means_no_routing(tmp_path, events):
    root = make_inbox(tmp_path, {"filed": FILED})
    opts = triage.Options(root, True, GATE, None, 5, 1, None, "2026-11-25")
    report = triage.run(opts)
    assert (
        report.routes == [] and report.route_notice == "" and (root / "ready" / "filed.md").exists()
    )
