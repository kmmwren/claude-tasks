"""`transition.py move`: qualified blockers, the cross-queue release on done, and the re-pick."""

import os

import pytest
import transition
from deps import fm

from tests.queue_helpers import brief_text, make_queue

TODAY = "2026-06-02"


def _move(root, *args):
    transition.main(["move", *args, "--root", str(root), "--no-sync", "--today", TODAY])


@pytest.fixture
def queues(tmp_path, monkeypatch):
    alpha = make_queue(tmp_path, {"dep": {"status": "in-progress"}}, project_name="alpha")
    beta = make_queue(tmp_path, {"w": {}}, project_name="beta")
    monkeypatch.setenv("CLAUDE_TASKS_ROOTS", os.pathsep.join([str(alpha), str(beta)]))
    return alpha, beta


def test_done_prints_the_pickers_top_runnable_brief(tmp_path, capsys):
    root = make_queue(
        tmp_path,
        {"dep": {"status": "in-progress"}, "low": {"importance": "5"}, "high": {"importance": "1"}},
    )
    _move(root, "dep", "done")
    assert "next: high" in capsys.readouterr().out


def test_done_says_when_nothing_is_runnable(tmp_path, capsys):
    root = make_queue(tmp_path, {"dep": {"status": "in-progress"}})
    _move(root, "dep", "done")
    assert "next: nothing runnable" in capsys.readouterr().out


def test_a_released_brief_is_a_candidate_for_the_pick(tmp_path, capsys):
    root = make_queue(
        tmp_path,
        {"dep": {"status": "in-progress"}, "freed": {"status": "blocked", "blocked_by": "[dep]"}},
    )
    _move(root, "dep", "done")
    assert "next: freed" in capsys.readouterr().out


def test_blocked_accepts_a_qualified_blocker_that_resolves(queues):
    _, beta = queues
    _move(beta, "w", "blocked", "--blocked-by", "alpha:dep")
    assert fm((beta / "blocked" / "w.md").read_text())["blocked_by"] == "[alpha:dep]"


@pytest.mark.parametrize("ref", ["nowhere:dep", "alpha:gone"])
def test_blocked_refuses_a_qualified_blocker_naming_nothing(queues, ref):
    _, beta = queues
    with pytest.raises(SystemExit):
        _move(beta, "w", "blocked", "--blocked-by", ref)


def test_done_releases_and_commits_a_waiter_in_another_queue(queues, monkeypatch, capsys):
    alpha, beta = queues
    (beta / "ready" / "w.md").unlink()
    (beta / "blocked" / "w.md").write_text(
        brief_text("w", status="blocked", blocked_by="[alpha:dep]")
    )
    commits = []
    monkeypatch.setattr(
        transition, "sync_commit", lambda msg, r=None, ids=(): commits.append((r, list(ids)))
    )
    transition.main(["move", "dep", "done", "--root", str(alpha), "--today", TODAY])
    assert "unblocked beta:w" in capsys.readouterr().out
    assert (beta / "ready" / "w.md").exists()
    assert (beta, ["w"]) in commits
