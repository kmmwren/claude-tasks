"""host_land.py: the git wrapper's timeout, the status parser, a held lock, and the command line."""

import pathlib
import subprocess

import host_land
import pytest

from tests.queue_helpers import queue_lock_held_elsewhere
from tests.test_git_sync import git, make_repo


def _nested_queue(tmp_path: pathlib.Path) -> pathlib.Path:
    """A queue inside a host repo, holding one brief the host has not recorded."""
    queue = make_repo(tmp_path) / "project" / ".tasks"
    (queue / "ready").mkdir(parents=True)
    (queue / "ready" / "x.md").write_text("x")
    return queue


def test_a_git_step_that_times_out_raises_a_land_error_naming_it(tmp_path, monkeypatch):
    def hang(*_args, **_kw):
        raise subprocess.TimeoutExpired(["git", "fetch"], 5)

    monkeypatch.setattr(host_land.subprocess, "run", hang)

    with pytest.raises(host_land.LandError, match="^git fetch timed out after 5s$"):
        host_land._git(tmp_path, "fetch", timeout=5)


def test_a_rename_in_the_status_yields_the_new_path_then_the_old_one():
    porcelain = "R  new.md\0old.md\0?? fresh.md\0 M kept.md\0"

    assert list(host_land._status_paths(porcelain)) == ["new.md", "old.md", "fresh.md", "kept.md"]


def test_a_land_while_the_queue_is_locked_defers_and_ships_nothing(tmp_path, monkeypatch, capsys):
    queue = _nested_queue(tmp_path)

    with queue_lock_held_elsewhere(queue, monkeypatch, host_land):
        assert host_land.land_host_queue(queue) is False

    assert capsys.readouterr().out == (
        "  (host land deferred: lock held; your files are safe on disk.)\n"
    )
    assert git(tmp_path / "remote", "branch", "--list", "wip/tasks-project").stdout == ""


def test_main_lands_the_queue_and_exits_zero(tmp_path, capsys):
    queue = _nested_queue(tmp_path)

    with pytest.raises(SystemExit) as stopped:
        host_land.main([str(queue)])

    assert stopped.value.code == 0
    assert capsys.readouterr().out.endswith("landed on origin/wip/tasks-project\n")


def test_main_exits_one_when_the_land_is_deferred(tmp_path, monkeypatch, capsys):
    queue = _nested_queue(tmp_path)

    with queue_lock_held_elsewhere(queue, monkeypatch, host_land):
        with pytest.raises(SystemExit) as stopped:
            host_land.main([str(queue)])

    assert stopped.value.code == 1
    assert capsys.readouterr().out.endswith("deferred\n")


def test_main_refuses_a_directory_that_is_not_a_queue_inside_another_repo(tmp_path):
    with pytest.raises(SystemExit) as stopped:
        host_land.main([str(tmp_path)])

    assert stopped.value.code == f"{tmp_path} is not a queue inside another repo"


def test_main_reads_the_queue_from_the_command_line(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["host_land.py", str(tmp_path)])

    with pytest.raises(SystemExit) as stopped:
        host_land.main()

    assert stopped.value.code == f"{tmp_path} is not a queue inside another repo"


def test_a_land_outside_a_host_repo_defers_without_a_word(tmp_path, capsys):
    assert host_land.land_host_queue(tmp_path) is False
    assert capsys.readouterr().out == ""


def test_a_failed_git_step_defers_the_land_naming_the_step(tmp_path, monkeypatch, capsys):
    queue = _nested_queue(tmp_path)

    def rejected(*_args):
        raise host_land.LandError("git push: rejected")

    monkeypatch.setattr(host_land, "_ship", rejected)

    assert host_land.land_host_queue(queue) is False
    assert capsys.readouterr().out == (
        "  (host land deferred: git push: rejected; your files are safe on disk.)\n"
    )


def test_a_brief_carrying_a_credential_is_held_back_and_counted(tmp_path, capsys):
    queue = _nested_queue(tmp_path)
    (queue / "ready" / "leak.md").write_text("key AKIA" + "A" * 16)

    shipped = host_land.HostQueue.of(queue).shippable()

    assert shipped == ["project/.tasks/ready/x.md"]
    assert "(refused: 1 held back for looking like a credential." in capsys.readouterr().out


def test_a_file_that_has_gone_carries_no_credential(tmp_path):
    queue = host_land.HostQueue.of(_nested_queue(tmp_path))

    assert queue._carries_credential("project/.tasks/ready/vanished.md") is False


def test_mirroring_a_path_the_source_no_longer_holds_removes_it(tmp_path):
    top, wt = tmp_path / "top", tmp_path / "wt"
    top.mkdir()
    (wt / "ready").mkdir(parents=True)
    (wt / "ready" / "gone.md").write_text("old")

    host_land._mirror(top, wt, ["ready/gone.md"])

    assert not (wt / "ready" / "gone.md").exists()


def test_a_leftover_worktree_holding_the_wip_branch_neither_blocks_the_land_nor_moves(tmp_path):
    queue = _nested_queue(tmp_path)
    host = queue.parent.parent
    leftover = tmp_path / "leftover"
    git(host, "worktree", "add", "-q", "-b", "wip/tasks-project", str(leftover))
    before = git(leftover, "rev-parse", "HEAD").stdout

    assert host_land.land_host_queue(queue) is True

    shipped = git(tmp_path / "remote", "ls-tree", "-r", "--name-only", "wip/tasks-project").stdout
    assert "project/.tasks/ready/x.md" in shipped
    assert git(leftover, "rev-parse", "HEAD").stdout == before
    assert git(leftover, "status", "--porcelain").stdout == ""
