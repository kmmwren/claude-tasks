"""A nested queue's write reaches origin's default branch, so the host's files match origin.

Only a refusal from the default branch (a protected branch, a failing pre-push or pre-receive gate, a
rebase that cannot be resolved) sends the commit to the queue's wip branch, and the land says so.
"""

import pathlib
import stat

import host_land

from tests.test_git_sync import _clone_other, _push_from_other, committed_files, git, make_repo

BRIEF = "project/.tasks/ready/x.md"
WIP = "wip/tasks-project"


def _host_with_queue(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    repo = make_repo(tmp_path)
    queue = repo / "project" / ".tasks"
    (queue / "ready").mkdir(parents=True)
    (queue / "ready" / "x.md").write_text("x")
    return repo, queue


def _remote_files(tmp_path: pathlib.Path, ref: str = "main") -> set[str]:
    return committed_files(tmp_path / "remote", ref)


def dirty(repo: pathlib.Path) -> str:
    """The host's `git status`, less the queue lock a land leaves in the queue."""
    lines = git(repo, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    return "\n".join(line for line in lines if ".tasks-" not in line)


def _wip_branches(tmp_path: pathlib.Path) -> list[str]:
    return git(tmp_path / "remote", "branch", "--list", "wip/*").stdout.split()


def _refuse_main(tmp_path: pathlib.Path) -> None:
    """The remote declines every push to main, as a protected branch does."""
    hook = tmp_path / "remote" / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        '  [ "$ref" = refs/heads/main ] && { echo "main is protected" >&2; exit 1; }\n'
        "done\n"
        "exit 0\n"
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def _race_main(monkeypatch, tmp_path: pathlib.Path, names: list[str]) -> list[str]:
    """Each push to main finds origin moved first, by a commit adding the next of `names`; returns the pushes."""
    other, pushes, real = _clone_other(tmp_path), [], host_land._push

    def racing(wt, ref, *flags):
        if ref == "refs/heads/main":
            pushes.append(ref)
            if names:
                _push_from_other(other, names.pop(0), "theirs")
        return real(wt, ref, *flags)

    monkeypatch.setattr(host_land, "_push", racing)
    return pushes


def test_a_nested_queue_write_lands_on_the_default_branch_and_the_host_matches_origin(tmp_path):
    repo, queue = _host_with_queue(tmp_path)

    assert host_land.land_host_queue(queue, "brief(x): captured")

    assert BRIEF in _remote_files(tmp_path)
    assert _wip_branches(tmp_path) == []
    assert git(tmp_path / "remote", "log", "-1", "--format=%s", "main").stdout.strip() == (
        "brief(x): captured"
    )
    assert dirty(repo) == ""
    assert git(repo, "rev-parse", "HEAD").stdout == git(repo, "rev-parse", "origin/main").stdout


def test_a_tracked_brief_edited_in_the_host_lands_and_the_host_advances_past_it(tmp_path):
    repo, queue = _host_with_queue(tmp_path)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed queue")
    git(repo, "push", "-q", "origin", "main")
    _push_from_other(_clone_other(tmp_path), "elsewhere.md", "newer origin")
    (queue / "ready" / "x.md").write_text("edited in place")

    assert host_land.land_host_queue(queue)

    assert git(tmp_path / "remote", "show", f"main:{BRIEF}").stdout == "edited in place"
    assert git(repo, "rev-parse", "HEAD").stdout == git(repo, "rev-parse", "origin/main").stdout
    assert dirty(repo) == ""
    assert (repo / "elsewhere.md").read_text() == "newer origin"


def test_a_queue_brief_deleted_in_the_host_is_deleted_on_the_default_branch(tmp_path):
    repo, queue = _host_with_queue(tmp_path)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed queue")
    git(repo, "push", "-q", "origin", "main")
    (queue / "ready" / "x.md").unlink()

    assert host_land.land_host_queue(queue)

    assert BRIEF not in _remote_files(tmp_path)
    assert dirty(repo) == ""


def test_a_push_that_loses_a_race_rebases_onto_the_new_tip_and_still_lands_on_the_default_branch(
    tmp_path, monkeypatch
):
    repo, queue = _host_with_queue(tmp_path)
    pushes = _race_main(monkeypatch, tmp_path, ["theirs.md"])

    assert host_land.land_host_queue(queue)

    assert len(pushes) == 2
    assert {BRIEF, "theirs.md"} <= _remote_files(tmp_path)
    assert _wip_branches(tmp_path) == []
    assert git(repo, "rev-parse", "HEAD").stdout == git(repo, "rev-parse", "origin/main").stdout


def test_a_default_branch_that_keeps_moving_is_given_up_on_after_a_bounded_number_of_pushes(
    tmp_path, monkeypatch, capsys
):
    _, queue = _host_with_queue(tmp_path)
    pushes = _race_main(monkeypatch, tmp_path, [f"theirs{n}.md" for n in range(9)])

    assert host_land.land_host_queue(queue)

    assert len(pushes) == host_land.PUSH_ATTEMPTS
    assert BRIEF in _remote_files(tmp_path, WIP) and BRIEF not in _remote_files(tmp_path)
    assert f"parked on origin/{WIP}" in capsys.readouterr().out


def test_a_default_branch_that_refuses_the_push_takes_the_wip_branch_and_says_so(
    tmp_path, capsys
):
    repo, queue = _host_with_queue(tmp_path)
    _refuse_main(tmp_path)
    head = git(repo, "rev-parse", "HEAD").stdout

    assert host_land.land_host_queue(queue)

    assert BRIEF in _remote_files(tmp_path, WIP) and BRIEF not in _remote_files(tmp_path)
    out = capsys.readouterr().out
    assert f"host land parked on origin/{WIP}" in out and "main is protected" in out
    assert git(repo, "rev-parse", "HEAD").stdout == head
    assert (repo / BRIEF).read_text() == "x"


def test_a_race_whose_rebase_conflicts_takes_the_wip_branch(tmp_path, monkeypatch, capsys):
    _, queue = _host_with_queue(tmp_path)
    other = _clone_other(tmp_path)
    (other / "project" / ".tasks" / "ready").mkdir(parents=True)
    real, raced = host_land._push, []

    def conflicting(wt, ref, *flags):
        if ref == "refs/heads/main" and not raced:
            raced.append(ref)
            _push_from_other(other, BRIEF, "someone else's brief")
        return real(wt, ref, *flags)

    monkeypatch.setattr(host_land, "_push", conflicting)

    assert host_land.land_host_queue(queue)

    assert git(tmp_path / "remote", "show", f"main:{BRIEF}").stdout == "someone else's brief"
    assert git(tmp_path / "remote", "show", f"{WIP}:{BRIEF}").stdout == "x"
    assert "rebase conflict" in capsys.readouterr().out
