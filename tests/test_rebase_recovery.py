"""A rebase abandoned in the live queue tree is cleared by the next sync, and its autostash is kept."""
import os
import pathlib
import time
import types

import _rebase_recovery
import git_sync
import pytest
from _concurrency import LOCK_TIMEOUT

from tests.test_git_sync import _brief, _clone_of_origin, _origin_appends, _seed_pushed, git, make_repo


def _abandon_autostash_only(repo: pathlib.Path, age: float = LOCK_TIMEOUT * 4) -> str:
    """The 2026-11-11 wedge: a killed `pull --rebase --autostash` left rebase-merge holding only `autostash`."""
    (repo / "inbox" / "wip.md").write_text("original wip")
    git(repo, "add", "inbox/wip.md")
    git(repo, "commit", "-qm", "wip")
    (repo / "inbox" / "wip.md").write_text("uncommitted wip")
    sha = git(repo, "stash", "create").stdout.strip()
    git(repo, "checkout", "--", "inbox/wip.md")
    state = repo / ".git" / "rebase-merge"
    state.mkdir()
    (state / "autostash").write_text(sha + "\n")
    _age(state, age)
    return sha


def _age(state: pathlib.Path, seconds: float) -> None:
    then = time.time() - seconds
    for entry in (*state.iterdir(), state):
        os.utime(entry, (then, then))


_REAL_GIT_BUSY = _rebase_recovery._git_busy


@pytest.fixture(autouse=True)
def _no_git_running(monkeypatch):
    monkeypatch.setattr(_rebase_recovery, "_git_busy", lambda: False)


def test_a_sync_clears_a_stale_autostash_only_rebase_and_keeps_the_stash(tmp_path, capsys):
    repo = make_repo(tmp_path)
    sha = _abandon_autostash_only(repo)
    (repo / "inbox" / "new.md").write_text("new")

    assert git_sync.sync_commit("brief(new): captured", root=repo, ids=["new"]) is True

    assert not (repo / ".git" / "rebase-merge").exists()
    git(repo, "fetch", "-q", "origin")
    assert "inbox/new.md" in git(repo, "ls-tree", "-r", "--name-only", "origin/main").stdout
    stashes = git(repo, "stash", "list", "--format=%H %gs").stdout
    assert f"{sha} " in stashes and "rescued autostash" in stashes
    assert git(repo, "show", f"{sha}:inbox/wip.md").stdout == "uncommitted wip"
    assert "cleared a stale rebase-merge" in capsys.readouterr().out


def test_a_young_rebase_state_is_left_for_its_owner(tmp_path):
    repo = make_repo(tmp_path)
    _abandon_autostash_only(repo, age=1)

    assert _rebase_recovery.recover_stale_rebase(repo) == []

    assert (repo / ".git" / "rebase-merge" / "autostash").is_file()
    assert git(repo, "stash", "list").stdout == ""


def test_a_rebase_state_is_left_while_a_git_process_runs(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    _abandon_autostash_only(repo)
    monkeypatch.setattr(_rebase_recovery, "_git_busy", lambda: True)

    assert _rebase_recovery.recover_stale_rebase(repo) == []

    assert (repo / ".git" / "rebase-merge" / "autostash").is_file()


def test_an_autostash_that_cannot_be_stored_is_never_discarded(tmp_path):
    repo = make_repo(tmp_path)
    state = repo / ".git" / "rebase-merge"
    state.mkdir()
    (state / "autostash").write_text("0" * 40 + "\n")
    _age(state, LOCK_TIMEOUT * 4)

    (note,) = _rebase_recovery.recover_stale_rebase(repo)

    assert "left a stale rebase-merge in place" in note
    assert (state / "autostash").is_file()


def test_a_stale_rebase_with_real_progress_is_aborted_not_deleted(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "inbox" / "c.md").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "c")
    git(repo, "branch", "side")
    (repo / "inbox" / "c.md").write_text("main side\n")
    git(repo, "commit", "-qam", "main edit")
    git(repo, "checkout", "-q", "side")
    (repo / "inbox" / "c.md").write_text("side edit\n")
    git(repo, "commit", "-qam", "side edit")
    assert git(repo, "rebase", "main").returncode != 0
    _age(repo / ".git" / "rebase-merge", LOCK_TIMEOUT * 4)

    (note,) = _rebase_recovery.recover_stale_rebase(repo)

    assert "cleared a stale rebase-merge" in note
    assert not (repo / ".git" / "rebase-merge").exists()
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "side"


def test_a_tree_with_no_rebase_state_reports_nothing(tmp_path):
    assert _rebase_recovery.recover_stale_rebase(make_repo(tmp_path)) == []


@pytest.mark.parametrize("table,busy", [("zsh\ngit -C /q pull --rebase\n", True), ("zsh\ngit status\n", False)])
def test_the_busy_check_reads_the_process_table(monkeypatch, table, busy):
    monkeypatch.setattr(_rebase_recovery.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=table))

    assert _REAL_GIT_BUSY() is busy


@pytest.mark.parametrize("command,busy", [
    ("git pull --rebase --autostash", True),
    ("/usr/bin/git -C /x rebase origin/main", True),
    ("/usr/libexec/git-core/git-rebase --merge", True),
    ("git status", False),
    ("python3 git_sync.py --sweep", False),
])
def test_only_a_running_pull_rebase_or_merge_counts_as_busy(command, busy):
    assert bool(_rebase_recovery._GIT_BUSY.search(command)) is busy


def test_two_sessions_appending_adjacent_log_lines_both_survive_a_sync(tmp_path, monkeypatch):
    """2026-11-10: one append committed locally, one pushed from another clone; the sync must keep both."""
    monkeypatch.setenv("GIT_EDITOR", "false")
    repo = make_repo(tmp_path)
    _seed_pushed(repo, "inbox/b.md", _brief())
    _origin_appends(_clone_of_origin(tmp_path), "inbox/b.md", "2026-11-10: appended on origin.\n")
    (repo / "inbox" / "b.md").write_text(_brief(log="2026-11-09: captured.\n2026-11-10: appended locally.\n"))

    assert git_sync.sync_commit("brief(b): logged", root=repo, ids=["b"]) is True

    git(repo, "fetch", "-q", "origin")
    log = git(repo, "show", "origin/main:inbox/b.md").stdout.split("## Execution log\n\n")[1]
    assert log.splitlines() == ["2026-11-09: captured.", "2026-11-10: appended on origin.",
                                "2026-11-10: appended locally."]
    assert git(repo, "status", "--porcelain").stdout == ""
