"""unblock.py — blocked briefs return to ready/ when every blocker is done."""
import json

import pytest
import unblock
from deps import fm

from tests.queue_helpers import make_queue

TODAY = "2026-06-02"


def _read(root, folder, bid):
    return (root / folder / f"{bid}.md").read_text()


def test_brief_auto_unblocks_when_its_blocker_is_done(tmp_path):
    root = make_queue(tmp_path, {
        "dep": {"status": "done"},
        "waiter": {"status": "blocked", "blocked_by": "[dep]"},
    })
    assert unblock.release_unblocked(root, today=TODAY) == ["waiter"]
    text = _read(root, "ready", "waiter")
    assert fm(text)["status"] == "ready"
    assert fm(text)["blocked_by"] == "[]"
    assert "unblocked: dep done" in text
    assert not (root / "blocked" / "waiter.md").exists()


def test_brief_stays_blocked_while_any_blocker_is_open(tmp_path):
    root = make_queue(tmp_path, {
        "a": {"status": "done"}, "b": {"status": "in-progress"},
        "waiter": {"status": "blocked", "blocked_by": "[a, b]"},
    })
    assert unblock.release_unblocked(root, today=TODAY) == []
    assert (root / "blocked" / "waiter.md").exists()


def test_external_blocker_never_auto_resolves(tmp_path):
    root = make_queue(tmp_path, {
        "a": {"status": "done"},
        "waiter": {"status": "blocked", "blocked_by": "[a, external:vendor reply]"},
    })
    assert unblock.release_unblocked(root, today=TODAY) == []


def test_blocked_brief_with_no_reason_is_left_alone(tmp_path):
    root = make_queue(tmp_path, {"orphan": {"status": "blocked"}})
    assert unblock.release_unblocked(root, today=TODAY) == []


def test_unmet_depends_tag_keeps_a_brief_blocked_even_if_blocked_by_is_done(tmp_path):
    root = make_queue(tmp_path, {
        "a": {"status": "done"}, "c": {"status": "ready"},
        "waiter": {"status": "blocked", "blocked_by": "[a]", "tags": "[depends:c]"},
    })
    assert unblock.release_unblocked(root, today=TODAY) == []


def test_release_order_is_priority_order(tmp_path):
    root = make_queue(tmp_path, {
        "dep": {"status": "done"},
        "low": {"status": "blocked", "blocked_by": "[dep]", "importance": "4"},
        "high": {"status": "blocked", "blocked_by": "[dep]", "importance": "1"},
        "mid": {"status": "blocked", "blocked_by": "[dep]", "importance": "3"},
    })
    assert unblock.release_unblocked(root, today=TODAY) == ["high", "mid", "low"]


def test_dry_run_reports_without_moving(tmp_path):
    root = make_queue(tmp_path, {
        "dep": {"status": "done"}, "w": {"status": "blocked", "blocked_by": "[dep]"}})
    assert unblock.release_unblocked(root, today=TODAY, dry_run=True) == ["w"]
    assert (root / "blocked" / "w.md").exists()


def test_a_brief_moved_by_another_actor_is_skipped(tmp_path, monkeypatch):
    root = make_queue(tmp_path, {
        "dep": {"status": "done"}, "w": {"status": "blocked", "blocked_by": "[dep]"}})

    def lost_race(*a, **k):
        raise ValueError("claimed by another actor")
    monkeypatch.setattr(unblock, "move_brief", lost_race)
    assert unblock.release_unblocked(root, today=TODAY) == []


def test_resolve_external_clears_only_that_blocker(tmp_path):
    root = make_queue(tmp_path, {
        "w": {"status": "blocked", "blocked_by": "[external:login, external:budget]"}})
    assert unblock.resolve_external(root, "w", "login", today=TODAY) is False
    text = _read(root, "blocked", "w")
    assert fm(text)["blocked_by"] == "[external:budget]"
    assert "cleared external:login" in text


def test_resolve_external_accepts_the_prefixed_form(tmp_path):
    root = make_queue(tmp_path, {"w": {"status": "blocked", "blocked_by": "[external:login]"}})
    assert unblock.resolve_external(root, "w", "external:login", today=TODAY) is True
    text = _read(root, "ready", "w")
    assert fm(text)["blocked_by"] == "[]"
    assert "unblocked: external:login cleared" in text


def test_resolve_last_external_keeps_brief_blocked_if_a_depends_tag_is_open(tmp_path):
    root = make_queue(tmp_path, {
        "open": {},
        "w": {"status": "blocked", "blocked_by": "[external:login]", "tags": "[depends:open]"}})
    assert unblock.resolve_external(root, "w", "login", today=TODAY) is False
    assert (root / "blocked" / "w.md").exists()


def test_resolve_external_rejects_unknown_blocker_and_unknown_brief(tmp_path):
    root = make_queue(tmp_path, {"w": {"status": "blocked", "blocked_by": "[external:login]"}})
    with pytest.raises(ValueError, match="no blocker"):
        unblock.resolve_external(root, "w", "nope")
    with pytest.raises(ValueError, match="no brief"):
        unblock.resolve_external(root, "ghost", "login")


def test_resolve_external_reports_a_lock_timeout(tmp_path, monkeypatch):
    import contextlib
    root = make_queue(tmp_path, {"w": {"status": "blocked", "blocked_by": "[external:login]"}})

    @contextlib.contextmanager
    def never(_root):
        yield False
    monkeypatch.setattr(unblock, "repo_lock", never)
    with pytest.raises(RuntimeError, match="lock"):
        unblock.resolve_external(root, "w", "login")


def test_cli_releases_and_commits(tmp_path, monkeypatch, capsys):
    root = make_queue(tmp_path, {
        "dep": {"status": "done"}, "w": {"status": "blocked", "blocked_by": "[dep]"}})
    commits = []
    monkeypatch.setattr(unblock, "sync_commit", lambda msg, r: commits.append(msg))
    unblock.main(["--root", str(root)])
    assert "released: w" in capsys.readouterr().out
    assert commits == ["unblock: released w"]


def test_cli_with_nothing_to_do_does_not_commit(tmp_path, monkeypatch, capsys):
    root = make_queue(tmp_path, {"a": {}})
    monkeypatch.setattr(unblock, "sync_commit", lambda *a: pytest.fail("committed"))
    unblock.main(["--root", str(root)])
    assert "nothing to release" in capsys.readouterr().out


def test_cli_dry_run_json_and_no_sync(tmp_path, monkeypatch, capsys):
    root = make_queue(tmp_path, {
        "dep": {"status": "done"}, "w": {"status": "blocked", "blocked_by": "[dep]"}})
    monkeypatch.setattr(unblock, "sync_commit", lambda *a: pytest.fail("committed"))
    unblock.main(["--root", str(root), "--dry-run", "--json"])
    assert json.loads(capsys.readouterr().out) == {"released": ["w"], "dry_run": True}
    unblock.main(["--root", str(root), "--no-sync"])
    assert "released: w" in capsys.readouterr().out


def test_cli_dry_run_text(tmp_path, capsys):
    root = make_queue(tmp_path, {
        "dep": {"status": "done"}, "w": {"status": "blocked", "blocked_by": "[dep]"}})
    unblock.main(["--root", str(root), "--dry-run"])
    assert "would release: w" in capsys.readouterr().out


def test_cli_resolve_then_release_commits_both(tmp_path, monkeypatch, capsys):
    root = make_queue(tmp_path, {"w": {"status": "blocked", "blocked_by": "[external:login]"}})
    commits = []
    monkeypatch.setattr(unblock, "sync_commit", lambda msg, r: commits.append(msg))
    unblock.main(["--root", str(root), "resolve", "w", "login"])
    assert commits == ["unblock: cleared an external blocker on w; released w"]
    assert (root / "ready" / "w.md").exists()


def test_cli_resolve_with_remaining_blocker_commits_the_clear_only(tmp_path, monkeypatch):
    root = make_queue(tmp_path, {
        "w": {"status": "blocked", "blocked_by": "[external:a, external:b]"}})
    commits = []
    monkeypatch.setattr(unblock, "sync_commit", lambda msg, r: commits.append(msg))
    unblock.main(["--root", str(root), "resolve", "w", "a"])
    assert commits == ["unblock: cleared an external blocker on w"]


def test_cli_resolve_error_exits_nonzero(tmp_path, capsys):
    root = make_queue(tmp_path, {"a": {}})
    with pytest.raises(SystemExit) as e:
        unblock.main(["--root", str(root), "resolve", "ghost", "x"])
    assert e.value.code == 1
    assert "no brief" in capsys.readouterr().err
