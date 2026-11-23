"""prose_dependency: a wait or approval stated only in prose is reported; history, quotes and carried waits are not."""

import check_deps
import pytest

from tests.queue_helpers import make_queue

PROSE = "prose-dependency"
APPROVED = "approved-needs-input"


@pytest.fixture(autouse=True)
def _decisions(tmp_path, monkeypatch):
    directory = tmp_path / "decisions"
    directory.mkdir()
    monkeypatch.setenv("CLAUDE_DECISIONS_DIR", str(directory))
    return directory


def _kinds(tmp_path, body, **kwargs):
    """Finding kinds reported for one brief `b` carrying `body` (other briefs: `other`, `done-one`)."""
    briefs = {"other": {}, "done-one": {"status": "done"}, "b": {"body": body, **kwargs}}
    root = make_queue(tmp_path, briefs)
    return [
        p.kind for p in check_deps.check(root) if p.brief == "b" and p.kind in (PROSE, APPROVED)
    ]


@pytest.mark.parametrize("status", ["ready", "in-progress"])
def test_a_wait_stated_only_in_prose_is_reported(tmp_path, status):
    body = "## Goal\n\nacme-api#5464 after #5449 merges\n"
    assert _kinds(tmp_path, body, status=status) == [PROSE]


def test_the_same_wait_carried_by_blocked_by_is_not_reported(tmp_path):
    body = "## Goal\n\nacme-api#5464 after #5449 merges\n"
    blocked = "[external:acme-api#5449 merges]"
    assert _kinds(tmp_path, body, blocked_by=blocked) == []


@pytest.mark.parametrize(
    "sentence",
    [
        "Raise it after #5449 is merged.",
        "Raise it after other merges.",
        "This waits for other before it starts.",
        "It is gated on #119 and must not start earlier.",
        "Blocked on her approval before the next step.",
        "Currently awaiting her go-ahead.",
        "- [ ] #5008 deployed after #942 merged, before #499",
    ],
)
def test_future_and_unmet_waits_are_reported(tmp_path, sentence):
    assert _kinds(tmp_path, f"## Goal\n\n{sentence}\n") == [PROSE]


@pytest.mark.parametrize(
    "sentence",
    [
        "Landed after #5449 merged and was reverted.",
        "After org/repo#41 merged, the gate named stale ids.",
        "It started after other merged, as planned.",
        "2026-11-10: parked, waits for #5449.",
        "Option A is built and gated on #119: a deployment PR needs her ask.",
        'The wait "#5464 after #5449 merges" lived only in prose.',
        "The wait `#5464 after #5449 merges` lived only in prose.",
    ],
)
def test_history_mechanisms_and_quoted_examples_are_not_reported(tmp_path, sentence):
    assert _kinds(tmp_path, f"## Goal\n\n{sentence}\n") == []


def test_an_execution_log_line_is_history(tmp_path):
    body = "## Goal\n\ng\n\n## Execution log\n\nparked: waits for #5449 and after #5449 merges.\n"
    assert _kinds(tmp_path, body) == []


def test_a_pr_the_brief_reports_merged_is_no_longer_a_wait(tmp_path):
    body = (
        "## Context\n\n[repo#1017](https://example.test/pull/1017) merged at noon.\n\n"
        "## Success criteria\n\n- [ ] the loop is confirmed stopped after #1017 merged\n"
    )
    assert _kinds(tmp_path, body) == []


@pytest.mark.parametrize("autonomy", ["needs-input", "blocked"])
def test_a_human_wait_is_carried_by_the_autonomy_field(tmp_path, autonomy):
    body = (
        "## Goal\n\nBlocked on Dana's approval before the next step.\n"
        "Deletion waits for sign-off per decision 2026-11-11-no-delete-without-signoff.\n"
    )
    assert _kinds(tmp_path, body, autonomy=autonomy) == []


def test_a_decision_already_made_is_not_a_pending_decision(tmp_path):
    body = (
        "## Goal\n\n- 11 item-8: blocked by decision 2026-10-02-module-preview-after-dbt.\n"
        "The script waits for the vault readiness first (decision 2026-11-09-wait-for-vault.md).\n"
    )
    assert _kinds(tmp_path, body, autonomy="full") == []


@pytest.mark.parametrize(
    "sentence",
    [
        "Her ask is the approval for this change.",
        "Dana gave the go-ahead on 2026-11-20.",
        "Approved by Dana.",
    ],
)
def test_an_approval_recorded_in_prose_on_a_needs_input_brief_is_reported(tmp_path, sentence):
    assert _kinds(tmp_path, f"## Goal\n\n{sentence}\n", autonomy="needs-input") == [APPROVED]


def test_the_same_approval_on_a_full_autonomy_brief_is_fine(tmp_path):
    assert _kinds(tmp_path, "## Goal\n\nHer ask is the approval for this change.\n") == []


@pytest.mark.parametrize(
    "sentence",
    [
        "- [ ] Dana has approved the trim",
        "Do not start until Dana has approved.",
        'She said "her ask is the approval" is not enough.',
    ],
)
def test_an_approval_still_to_come_does_not_make_needs_input_stale(tmp_path, sentence):
    assert _kinds(tmp_path, f"## Goal\n\n{sentence}\n", autonomy="needs-input") == []


def test_a_cited_decision_that_approves_the_work_is_reported(tmp_path, _decisions):
    (_decisions / "2026-11-20-ship-it.md").write_text(
        "**Decision:** Dana gave the go-ahead to ship.\n"
    )
    body = "## Context\n\nSee 2026-11-20-ship-it for the call.\n"
    assert _kinds(tmp_path, body, autonomy="needs-input") == [APPROVED]


def test_a_blocked_brief_is_not_inspected(tmp_path):
    body = "## Goal\n\nWaits for #5449 and after #5449 merges.\n"
    assert _kinds(tmp_path, body, status="blocked", blocked_by="[other]") == []
