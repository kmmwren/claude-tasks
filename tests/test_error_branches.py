"""The defensive branches each script takes when git, the lock or the filesystem refuses."""

import contextlib
import os
import subprocess
import time

import _advance
import cascade
import check_deps
import declaration
import git_sync
import host_land
import pytest
import triage_declare
from git_sync import SyncResult
from host_land import LandError

from tests.queue_helpers import make_queue
from tests.test_check_deps import _nested_brief, _unshipped
from tests.test_git_sync import make_repo

TODAY = "2026-06-02"


@contextlib.contextmanager
def _timed_out(root):
    yield False


def _host_queue(tmp_path):
    queue = make_repo(tmp_path) / "project" / ".tasks"
    (queue / "ready").mkdir(parents=True)
    return queue


def test_an_incoming_file_that_cannot_be_removed_is_left_in_place(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "ready").mkdir(exist_ok=True)
    brief = repo / "ready" / "held.md"
    brief.write_text("same", encoding="utf-8")
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "ready/held.md"],
        capture_output=True,
        encoding="utf-8",
    ).stdout.strip()
    os.chmod(brief.parent, 0o555)
    try:
        assert _advance._take_away(repo, "ready/held.md", blob) is None
    finally:
        os.chmod(brief.parent, 0o755)
    assert brief.exists()


def test_a_busy_queue_skips_the_cascade_log_with_a_warning(tmp_path, monkeypatch, capsys):
    root = make_queue(tmp_path, {"open": {}})
    before = (root / "ready" / "open.md").read_text(encoding="utf-8")
    monkeypatch.setattr(cascade, "repo_lock", _timed_out)

    cascade._log(root, "open", TODAY, ["landed"])

    assert "could not log what landed" in capsys.readouterr().err
    assert (root / "ready" / "open.md").read_text(encoding="utf-8") == before


def test_a_host_status_that_fails_reports_nothing_unshipped(tmp_path, monkeypatch):
    queue = _nested_brief(tmp_path, age_hours=7)

    def refuse(self):
        raise LandError("git status: broken")

    monkeypatch.setattr(check_deps.HostQueue, "pending", refuse)

    assert _unshipped(queue) == []


def test_a_deleted_brief_is_not_waiting_to_ship(tmp_path):
    assert check_deps._modified_before(tmp_path / "gone.md", time.time()) is False


def test_declaring_an_unknown_brief_is_refused(tmp_path):
    root = make_queue(tmp_path, {"open": {}})

    with pytest.raises(ValueError, match="no brief with id 'ghost'"):
        declaration.declare_brief("ghost", [], root, today=TODAY)


def test_a_lock_timeout_leaves_the_brief_undeclared(tmp_path, monkeypatch):
    root = make_queue(tmp_path, {"open": {}})
    monkeypatch.setattr(declaration, "repo_lock", _timed_out)

    with pytest.raises(RuntimeError, match="was NOT declared"):
        declaration.declare_brief("open", [], root, today=TODAY)


def test_a_brief_moved_while_waiting_for_the_lock_is_refused(tmp_path, monkeypatch):
    root = make_queue(tmp_path, {"open": {}})

    @contextlib.contextmanager
    def moved(lock_root):
        (lock_root / "ready" / "open.md").unlink()
        yield True

    monkeypatch.setattr(declaration, "repo_lock", moved)

    with pytest.raises(ValueError, match="moved by another actor"):
        declaration.declare_brief("open", [], root, today=TODAY)


@pytest.mark.parametrize(
    ("result", "pushed"), [(SyncResult.HOSTED, True), (SyncResult.DEFERRED, False)]
)
def test_a_hosted_landing_counts_as_pushed_and_a_deferral_does_not(
    tmp_path, monkeypatch, result, pushed
):
    monkeypatch.setattr(git_sync, "_commit_for_push", lambda *args, **kwargs: result)

    assert git_sync.sync_commit_and_push("m", tmp_path) is pushed


def test_a_hung_git_call_fails_the_land(tmp_path, monkeypatch):
    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(host_land.subprocess, "run", hang)

    with pytest.raises(LandError, match="git fetch timed out after 5s"):
        host_land._git(tmp_path, "fetch", timeout=5)


def test_a_rename_reports_both_its_paths():
    assert list(host_land._status_paths("R  new.md\0old.md\0")) == ["new.md", "old.md"]


def test_a_held_lock_defers_the_host_land(tmp_path, monkeypatch, capsys):
    queue = _host_queue(tmp_path)
    monkeypatch.setattr(host_land, "repo_lock", _timed_out)

    assert host_land.land_host_queue(queue) is False
    assert "lock held" in capsys.readouterr().out


def test_the_land_command_refuses_a_directory_outside_any_repo(tmp_path):
    with pytest.raises(SystemExit, match="is not a queue inside another repo"):
        host_land.main([str(tmp_path)])


@pytest.mark.parametrize(
    ("landed", "code", "said"), [(True, 0, "landed on origin/"), (False, 1, "deferred")]
)
def test_the_land_command_says_what_happened(tmp_path, monkeypatch, capsys, landed, code, said):
    queue = _host_queue(tmp_path)
    monkeypatch.setattr(host_land, "land_host_queue", lambda root: landed)

    with pytest.raises(SystemExit) as exit_info:
        host_land.main([str(queue)])

    assert exit_info.value.code == code
    assert said in capsys.readouterr().out


def test_an_inbox_brief_that_cannot_be_declared_is_reported_and_skipped(
    tmp_path, monkeypatch, capsys
):
    def refuse(*args, **kwargs):
        raise ValueError("no brief with id 'x'")

    monkeypatch.setattr(triage_declare, "declare_brief", refuse)

    assert triage_declare._declare("x", tmp_path, TODAY) is False
    assert "x: not declared" in capsys.readouterr().err
