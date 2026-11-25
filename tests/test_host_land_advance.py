"""A nested queue's host checkout advances after its wip branch merges, only when it is on its default branch.

The host keeps the landed briefs untracked, so once the wip branch is merged the next land has to clear
identical copies before the host can move to origin's default branch.
"""

import pathlib

import host_land

from tests.test_git_sync import _clone_other, git, make_repo

BRIEF = "project/.tasks/ready/x.md"


def _host_with_merged_wip(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """A host whose queue landed x.md on the wip branch, which origin's default branch has since merged."""
    repo = make_repo(tmp_path)
    queue = repo / "project" / ".tasks"
    (queue / "ready").mkdir(parents=True)
    (queue / "ready" / "x.md").write_text("x")
    assert host_land.land_host_queue(queue)
    other = _clone_other(tmp_path)
    git(other, "fetch", "-q", "origin", "wip/tasks-project")
    git(other, "merge", "-q", "--ff-only", "FETCH_HEAD")
    git(other, "push", "-q", "origin", "main")
    return repo, queue


def _head(repo: pathlib.Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def test_the_host_on_its_default_branch_advances_past_briefs_it_holds_untracked(tmp_path):
    repo, queue = _host_with_merged_wip(tmp_path)
    (queue / "ready" / "y.md").write_text("y")

    assert host_land.land_host_queue(queue)

    assert _head(repo) == git(repo, "rev-parse", "origin/main").stdout.strip()
    assert git(repo, "ls-files", BRIEF).stdout.strip() == BRIEF
    assert (repo / BRIEF).read_text() == "x"
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "project/.tasks/ready")
    assert untracked.stdout.split() == ["project/.tasks/ready/y.md"]
    wip = git(tmp_path / "remote", "ls-tree", "-r", "--name-only", "wip/tasks-project")
    assert "project/.tasks/ready/y.md" in wip.stdout.split()


def test_a_host_on_another_branch_is_not_moved(tmp_path):
    repo, queue = _host_with_merged_wip(tmp_path)
    git(repo, "checkout", "-q", "-b", "feature")
    before = _head(repo)

    assert host_land.land_host_queue(queue)

    assert _head(repo) == before
    assert git(repo, "branch", "--show-current").stdout.strip() == "feature"
    assert git(repo, "ls-files", BRIEF).stdout == ""
    assert (repo / BRIEF).read_text() == "x"


def test_a_host_with_a_local_commit_origin_lacks_keeps_it(tmp_path):
    repo, queue = _host_with_merged_wip(tmp_path)
    (repo / "notes.md").write_text("local work")
    git(repo, "add", "notes.md")
    git(repo, "commit", "-qm", "local work")
    before = _head(repo)

    assert host_land.land_host_queue(queue)

    assert _head(repo) == before
    assert (repo / BRIEF).read_text() == "x"


def test_a_refused_advance_still_lands_and_leaves_the_host_as_it_was(tmp_path, capsys):
    repo, queue = _host_with_merged_wip(tmp_path)
    before = _head(repo)
    (queue / "ready" / "x.md").write_text("changed after the merge")

    assert host_land.land_host_queue(queue)

    assert _head(repo) == before
    assert (repo / BRIEF).read_text() == "changed after the merge"
    assert "host checkout not advanced" in capsys.readouterr().out


def test_a_brief_landing_in_done_with_an_open_criterion_ships_from_where_it_came(tmp_path):
    repo = make_repo(tmp_path)
    queue = repo / "project" / ".tasks"
    (queue / "done").mkdir(parents=True)
    (queue / "done" / "z.md").write_text(
        "---\nid: z\nstatus: done\n---\n\n## Success criteria\n\n- [ ] findings fixed\n"
    )

    assert host_land.land_host_queue(queue)

    wip = git(
        tmp_path / "remote", "ls-tree", "-r", "--name-only", "wip/tasks-project"
    ).stdout.split()
    assert "project/.tasks/ready/z.md" in wip and "project/.tasks/done/z.md" not in wip
