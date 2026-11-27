"""A nested queue's host checkout advances onto the commit it landed, only when it is on its default branch.

The host holds the landed briefs as uncommitted files, so the advance has to clear the copies that already
match origin before the host can move to origin's default branch.
"""

import pathlib

import host_land

from tests.test_git_sync import _clone_other, _push_from_other, git, make_repo
from tests.test_host_land_default import dirty

BRIEF = "project/.tasks/ready/x.md"
BLOCKING = "notes.md"


def _host_with_queue(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    repo = make_repo(tmp_path)
    queue = repo / "project" / ".tasks"
    (queue / "ready").mkdir(parents=True)
    (queue / "ready" / "x.md").write_text("x")
    return repo, queue


def _host_behind_origin_holding_its_brief(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    """A host behind origin's default branch, which already holds the brief the host has uncommitted."""
    repo, queue = _host_with_queue(tmp_path)
    other = _clone_other(tmp_path)
    (other / BRIEF).parent.mkdir(parents=True)
    _push_from_other(other, BRIEF, "x")
    return repo, queue


def _host_with_a_blocking_edit(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """A host behind origin, holding an uncommitted edit to a file origin has since changed."""
    repo, queue = _host_behind_origin_holding_its_brief(tmp_path)
    _push_from_other(tmp_path / "other", BLOCKING, "theirs")
    (repo / BLOCKING).write_text("mine, not yet committed")
    return repo, queue


def _today() -> str:
    return host_land._today()


def _head(repo: pathlib.Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def _origin(repo: pathlib.Path) -> str:
    return git(repo, "rev-parse", "origin/main").stdout.strip()


def test_the_host_on_its_default_branch_advances_onto_what_it_landed(tmp_path):
    repo, queue = _host_with_queue(tmp_path)

    assert host_land.land_host_queue(queue)

    assert _head(repo) == _origin(repo)
    assert git(repo, "ls-files", BRIEF).stdout.strip() == BRIEF
    assert (repo / BRIEF).read_text() == "x"
    assert dirty(repo) == ""


def test_a_host_on_another_branch_is_not_moved(tmp_path):
    repo, queue = _host_with_queue(tmp_path)
    git(repo, "checkout", "-q", "-b", "feature")
    before = _head(repo)

    assert host_land.land_host_queue(queue)

    assert _head(repo) == before
    assert git(repo, "branch", "--show-current").stdout.strip() == "feature"
    assert git(repo, "ls-files", BRIEF).stdout == ""
    assert (repo / BRIEF).read_text() == "x"


def test_a_host_with_a_local_commit_origin_lacks_keeps_it(tmp_path):
    repo, queue = _host_with_queue(tmp_path)
    (repo / "notes.md").write_text("local work")
    git(repo, "add", "notes.md")
    git(repo, "commit", "-qm", "local work")
    before = _head(repo)

    assert host_land.land_host_queue(queue)

    assert _head(repo) == before
    assert (repo / BRIEF).read_text() == "x"


def test_an_edit_that_blocks_the_advance_is_rescued_to_a_pushed_tag_and_the_brief_still_lands(
    tmp_path, capsys
):
    repo, queue = _host_with_a_blocking_edit(tmp_path)
    (queue / "ready" / "y.md").write_text("y")

    assert host_land.land_host_queue(queue)

    assert _head(repo) == _origin(repo)
    assert (repo / BLOCKING).read_text() == "theirs"
    rescued = git(
        tmp_path / "remote", "show", f"superseded/rescue/clone-{_today()}:{BLOCKING}"
    ).stdout
    assert rescued == "mine, not yet committed"
    assert "rescued to tag superseded/rescue/clone-" in capsys.readouterr().out
    landed = git(tmp_path / "remote", "ls-tree", "-r", "--name-only", "main").stdout.split()
    assert "project/.tasks/ready/y.md" in landed


def test_a_brief_landing_in_done_with_an_open_criterion_ships_from_where_it_came(tmp_path):
    repo = make_repo(tmp_path)
    queue = repo / "project" / ".tasks"
    (queue / "done").mkdir(parents=True)
    (queue / "done" / "z.md").write_text(
        "---\nid: z\nstatus: done\n---\n\n## Success criteria\n\n- [ ] findings fixed\n"
    )

    assert host_land.land_host_queue(queue)

    landed = git(tmp_path / "remote", "ls-tree", "-r", "--name-only", "main").stdout.split()
    assert "project/.tasks/ready/z.md" in landed and "project/.tasks/done/z.md" not in landed
