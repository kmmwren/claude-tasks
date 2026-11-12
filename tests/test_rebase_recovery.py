"""A todo-less rebase left in the live queue tree is cleared by the next sync, and its autostash is kept."""

import os
import pathlib
import time

import _rebase_recovery
import git_sync
from _rebase_recovery import REBASE_SETTLE, STASH_MESSAGE

from tests.test_git_sync import (
    _brief,
    _clone_of_origin,
    _origin_appends,
    _seed_pushed,
    git,
    make_repo,
)

SETTLED = REBASE_SETTLE * 2


def _age(state: pathlib.Path, seconds: float) -> None:
    then = time.time() - seconds
    for entry in (*state.iterdir(), state):
        os.utime(entry, (then, then))


def _abandon_autostash_only(repo: pathlib.Path, age: float = SETTLED) -> str:
    """The 2026-11-11 wedge: a killed `pull --rebase --autostash` left rebase-merge holding only `autostash`."""
    (repo / "inbox" / "wip.md").write_text("original wip")
    git(repo, "add", "inbox/wip.md")
    git(repo, "commit", "-qm", "wip")
    git(repo, "push", "-q", "origin", "main")
    (repo / "inbox" / "wip.md").write_text("uncommitted wip")
    sha = git(repo, "stash", "create").stdout.strip()
    git(repo, "checkout", "--", "inbox/wip.md")
    state = repo / ".git" / "rebase-merge"
    state.mkdir()
    (state / "autostash").write_text(sha + "\n")
    _age(state, age)
    return sha


def _stuck_mid_rebase(repo: pathlib.Path) -> None:
    """A rebase stopped on a conflict: rebase-merge holds a todo, i.e. real unfinished steps."""
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
    _age(repo / ".git" / "rebase-merge", SETTLED)


def test_a_sync_quits_a_todo_less_rebase_and_stores_its_autostash(tmp_path, capsys):
    repo = make_repo(tmp_path)
    sha = _abandon_autostash_only(repo)
    (repo / "inbox" / "new.md").write_text("new")

    assert git_sync.sync_commit_and_push("brief(new): captured", root=repo, ids=["new"]) is True

    assert not (repo / ".git" / "rebase-merge").exists()
    assert git(repo, "pull", "-q", "--rebase", "origin", "main").returncode == 0
    stashes = git(repo, "stash", "list", "--format=%H %gs").stdout.splitlines()
    assert len(stashes) == 1 and stashes[0].startswith(f"{sha} {STASH_MESSAGE} ")
    assert git(repo, "show", f"{sha}:inbox/wip.md").stdout == "uncommitted wip"
    git(repo, "fetch", "-q", "origin")
    assert "inbox/new.md" in git(repo, "ls-tree", "-r", "--name-only", "origin/main").stdout
    assert "cleared a stale rebase-merge" in capsys.readouterr().out


def test_a_todo_less_rebase_without_an_autostash_is_quit_without_a_stash(tmp_path):
    repo = make_repo(tmp_path)
    state = repo / ".git" / "rebase-merge"
    state.mkdir()
    (state / "head-name").write_text("refs/heads/main\n")
    _age(state, SETTLED)

    (note,) = _rebase_recovery.recover_stale_rebase(repo)

    assert "cleared a stale rebase-merge" in note
    assert not state.exists()
    assert git(repo, "stash", "list").stdout == ""


def test_a_young_todo_less_rebase_is_left_for_the_git_still_writing_it(tmp_path):
    repo = make_repo(tmp_path)
    _abandon_autostash_only(repo, age=1)

    assert _rebase_recovery.recover_stale_rebase(repo) == []

    assert (repo / ".git" / "rebase-merge" / "autostash").is_file()
    assert git(repo, "stash", "list").stdout == ""


def test_a_rebase_with_a_todo_is_reported_and_left_alone(tmp_path):
    repo = make_repo(tmp_path)
    _stuck_mid_rebase(repo)

    (note,) = _rebase_recovery.recover_stale_rebase(repo)

    assert note.startswith("WARNING: a rebase with unfinished steps is stuck")
    assert (repo / ".git" / "rebase-merge" / "git-rebase-todo").exists()


def test_an_autostash_that_cannot_be_stored_is_never_discarded(tmp_path):
    repo = make_repo(tmp_path)
    state = repo / ".git" / "rebase-merge"
    state.mkdir()
    (state / "autostash").write_text("0" * 40 + "\n")
    _age(state, SETTLED)

    (note,) = _rebase_recovery.recover_stale_rebase(repo)

    assert "left a stale rebase-merge in place" in note
    assert (state / "autostash").is_file()


def test_a_failed_quit_removes_the_state_once_the_autostash_is_stored(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    sha = _abandon_autostash_only(repo)
    real_git = _rebase_recovery._git

    def quit_fails(root, *args):
        if args[:2] == ("rebase", "--quit"):
            return real_git(root, "rebase", "--no-such-flag")
        return real_git(root, *args)

    monkeypatch.setattr(_rebase_recovery, "_git", quit_fails)

    (note,) = _rebase_recovery.recover_stale_rebase(repo)

    assert "cleared a stale rebase-merge" in note
    assert not (repo / ".git" / "rebase-merge").exists()
    assert sha in git(repo, "stash", "list", "--format=%H").stdout


def test_a_tree_with_no_rebase_state_reports_nothing(tmp_path):
    assert _rebase_recovery.recover_stale_rebase(make_repo(tmp_path)) == []


def test_two_sessions_appending_adjacent_log_lines_both_survive_a_sync(tmp_path, monkeypatch):
    """2026-11-10: one append committed locally, one pushed from another clone; the sync must keep both."""
    monkeypatch.setenv("GIT_EDITOR", "false")
    repo = make_repo(tmp_path)
    _seed_pushed(repo, "inbox/b.md", _brief())
    _origin_appends(_clone_of_origin(tmp_path), "inbox/b.md", "2026-11-10: appended on origin.\n")
    (repo / "inbox" / "b.md").write_text(
        _brief(log="2026-11-09: captured.\n2026-11-10: appended locally.\n")
    )

    assert git_sync.sync_commit_and_push("brief(b): logged", root=repo, ids=["b"]) is True

    git(repo, "fetch", "-q", "origin")
    log = git(repo, "show", "origin/main:inbox/b.md").stdout.split("## Execution log\n\n")[1]
    assert log.splitlines() == [
        "2026-11-09: captured.",
        "2026-11-10: appended on origin.",
        "2026-11-10: appended locally.",
    ]
    assert git(repo, "status", "--porcelain").stdout == ""
