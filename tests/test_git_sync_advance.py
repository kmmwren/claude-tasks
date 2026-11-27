"""`_advance_checkout` moves a checkout past briefs it already holds untracked, and loses nothing.

Origin carries briefs the checkout wrote itself by another route, so they sit untracked at the same paths.
An identical copy is cleared so the reset can bring it in; a different one, or a refused reset, leaves the
checkout as it was.
"""

import os
import pathlib

import git_sync
from _advance import Change, _take_away
from _concurrency import Deadline

from tests.test_git_sync import _clone_other, git, make_repo
from tests.test_git_sync import _push_from_other as _push_file

BRIEF = "ready/held.md"


def _push_from_other(other: pathlib.Path, name: str, text: str) -> None:
    """Origin gains `name`; its folder is made first because the clone holds no empty directories."""
    (other / name).parent.mkdir(parents=True, exist_ok=True)
    _push_file(other, name, text)


def _origin_moves(tmp_path: pathlib.Path, name: str = BRIEF, text: str = "same") -> None:
    """Origin gains `name`, which the checkout has not fetched."""
    _push_from_other(_clone_other(tmp_path), name, text)


def _advance(repo: pathlib.Path) -> None:
    git_sync._advance_checkout(repo, Deadline.in_(60))


def _head(repo: pathlib.Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def _origin_head(repo: pathlib.Path) -> str:
    return git(repo, "rev-parse", "origin/main").stdout.strip()


def test_an_untracked_brief_identical_to_the_incoming_one_no_longer_blocks_the_advance(tmp_path):
    repo = make_repo(tmp_path)
    _origin_moves(tmp_path)
    (repo / BRIEF).write_text("same")

    _advance(repo)

    assert _head(repo) == _origin_head(repo)
    assert git(repo, "ls-files", BRIEF).stdout.strip() == BRIEF
    assert (repo / BRIEF).read_text() == "same"
    assert git(repo, "status", "--porcelain").stdout == ""


def test_an_untracked_brief_that_differs_blocks_the_advance_and_is_never_touched(tmp_path, capsys):
    repo = make_repo(tmp_path)
    before = _head(repo)
    _origin_moves(tmp_path)
    (repo / BRIEF).write_text("my own words")

    _advance(repo)

    assert _head(repo) == before
    assert (repo / BRIEF).read_text() == "my own words"
    assert "checkout was not advanced" in capsys.readouterr().out


def test_an_identical_brief_is_written_back_when_a_tracked_edit_still_blocks_the_reset(
    tmp_path, capsys
):
    repo = make_repo(tmp_path)
    (repo / "ready" / "theirs.md").write_text("v1")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "theirs")
    git(repo, "push", "-q", "origin", "main")
    before = _head(repo)
    other = _clone_other(tmp_path)
    _push_from_other(other, "ready/theirs.md", "v2")
    _push_from_other(other, BRIEF, "same")
    (repo / "ready" / "theirs.md").write_text("an uncommitted note")
    (repo / BRIEF).write_text("same")
    os.chmod(repo / BRIEF, 0o755)

    _advance(repo)

    assert _head(repo) == before
    assert (repo / "ready" / "theirs.md").read_text() == "an uncommitted note"
    assert (repo / BRIEF).read_text() == "same"
    assert (repo / BRIEF).stat().st_mode & 0o777 == 0o755
    assert "?? " + BRIEF in git(repo, "status", "--porcelain").stdout
    assert "checkout was not advanced" in capsys.readouterr().out


def test_a_brief_still_in_head_but_dropped_from_the_index_is_left_alone(tmp_path, capsys):
    repo = make_repo(tmp_path)
    (repo / BRIEF).write_text("v1")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "brief")
    git(repo, "push", "-q", "origin", "main")
    before = _head(repo)
    _origin_moves(tmp_path, BRIEF, "v2")
    (repo / BRIEF).write_text("v2")
    git(repo, "rm", "-q", "--cached", BRIEF)

    _advance(repo)

    assert _head(repo) == before
    assert (repo / BRIEF).read_text() == "v2"
    assert "checkout was not advanced" in capsys.readouterr().out


def test_an_untracked_file_origin_does_not_hold_survives_the_advance(tmp_path):
    repo = make_repo(tmp_path)
    _origin_moves(tmp_path)
    (repo / BRIEF).write_text("same")
    (repo / "inbox" / "mine.md").write_text("mine")

    _advance(repo)

    assert _head(repo) == _origin_head(repo)
    assert (repo / "inbox" / "mine.md").read_text() == "mine"
    assert git(repo, "ls-files", "inbox/mine.md").stdout == ""


def test_a_file_that_cannot_be_removed_is_left_where_it_is(tmp_path):
    repo = make_repo(tmp_path)
    (repo / BRIEF).parent.mkdir(exist_ok=True)
    (repo / BRIEF).write_text("same")
    blob = git(repo, "hash-object", BRIEF).stdout.strip()
    (repo / "ready").chmod(0o555)
    try:
        assert _take_away(repo, Change(BRIEF, None, blob)) is None
    finally:
        (repo / "ready").chmod(0o755)
    assert (repo / BRIEF).read_text() == "same"
