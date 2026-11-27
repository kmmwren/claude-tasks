"""_rescue.py: each way a rescue can fail leaves the checkout untouched and says why."""

import pathlib
import subprocess

import _rescue
import host_land
import pytest
from _rescue import Advance

from tests.test_git_sync import git
from tests.test_host_land_advance import BRIEF, _head, _host_with_merged_wip, _today


def _blocked_host(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """A host behind origin whose brief carries an edit that blocks the advance."""
    repo, queue = _host_with_merged_wip(tmp_path)
    (queue / "ready" / "x.md").write_text("changed after the merge")
    return repo, queue


def test_a_git_step_that_times_out_raises_a_rescue_error_naming_it(tmp_path, monkeypatch):
    def hang(*_args, **_kw):
        raise subprocess.TimeoutExpired(["git", "push"], 120)

    monkeypatch.setattr(_rescue.subprocess, "run", hang)

    with pytest.raises(_rescue.RescueError, match="^git push timed out after 120s$"):
        _rescue._git(tmp_path, "push", timeout=120)


def test_a_failing_git_step_raises_a_rescue_error_with_gits_own_words(tmp_path):
    with pytest.raises(_rescue.RescueError, match="^git status: fatal: not a git repository"):
        _rescue._git(tmp_path, "status")


def test_a_file_that_is_absent_has_no_blob_on_disk(tmp_path):
    assert _rescue._blob_on_disk(tmp_path, "gone.md") is None


def test_discarding_a_tracked_edit_restores_what_head_holds(tmp_path):
    repo, _queue = _host_with_merged_wip(tmp_path)
    tracked = repo / "seed.md"
    tracked.write_text("head's words")
    git(repo, "add", "seed.md")
    git(repo, "commit", "-qm", "seed")
    tracked.write_text("an edit")
    (repo / "scratch.md").write_text("untracked")

    _rescue._discard(repo, ["seed.md", "scratch.md"])

    assert tracked.read_text() == "head's words"
    assert not (repo / "scratch.md").exists()


def test_a_checkout_that_git_refuses_raises_a_rescue_error(tmp_path):
    repo, _queue = _host_with_merged_wip(tmp_path)
    tracked = repo / "seed.md"
    tracked.write_text("head's words")
    git(repo, "add", "seed.md")
    git(repo, "commit", "-qm", "seed")
    tracked.write_text("an edit")
    (repo / ".git" / "index.lock").write_text("")

    with pytest.raises(_rescue.RescueError, match="^git checkout: fatal: Unable to create"):
        _rescue._discard(repo, ["seed.md"])

    assert tracked.read_text() == "an edit"


def test_a_path_gone_from_the_checkout_is_removed_from_the_rescue_copy(tmp_path):
    root, copy = tmp_path / "root", tmp_path / "copy"
    root.mkdir()
    copy.mkdir()
    (copy / "gone.md").write_text("still here")
    (root / "kept.md").write_text("kept")

    _rescue._mirror(root, copy, ["gone.md", "kept.md"])

    assert not (copy / "gone.md").exists()
    assert (copy / "kept.md").read_text() == "kept"


def test_a_tag_origin_already_holds_gets_the_next_free_suffix(tmp_path):
    repo, _queue = _blocked_host(tmp_path)
    assert host_land.land_host_queue(_queue)
    stem = f"clone-{_today()}"

    assert _rescue._free_tag(repo, stem) == f"superseded/rescue/{stem}-2"


def test_a_push_origin_does_not_confirm_is_refused(tmp_path, monkeypatch):
    repo, _queue = _blocked_host(tmp_path)
    real, listings = _rescue._git, []

    def silent_origin(root, *args, **kw):
        if args[0] == "ls-remote":
            listings.append(args)
            return "" if len(listings) > 1 else real(root, *args, **kw)
        return real(root, *args, **kw)

    monkeypatch.setattr(_rescue, "_git", silent_origin)

    with pytest.raises(_rescue.RescueError, match="^origin does not hold [0-9a-f]{9} on tag "):
        _rescue._push_rescue(repo, [BRIEF], "stem")


def test_a_rescue_that_fails_before_origin_holds_the_edits_leaves_the_host_as_it_was(
    tmp_path, monkeypatch, capsys
):
    repo, queue = _blocked_host(tmp_path)
    before = _head(repo)

    def refuse(*_args):
        raise _rescue.RescueError("git push: remote hung up")

    monkeypatch.setattr(_rescue, "_push_rescue", refuse)

    assert host_land.land_host_queue(queue)

    assert _head(repo) == before
    assert (queue / "ready" / "x.md").read_text() == "changed after the merge"
    assert capsys.readouterr().out.endswith(
        "  (host checkout not advanced to origin/main: git push: remote hung up)\n"
    )


def test_an_advance_git_still_refuses_after_the_rescue_is_reported_as_such(tmp_path, monkeypatch):
    repo, _queue = _host_with_merged_wip(tmp_path)
    monkeypatch.setattr(_rescue, "advance_to_origin", lambda *_a: False)

    assert _rescue.advance_or_rescue(repo, "main", _today()) == Advance(
        False, None, "reset --keep still refused after the rescue"
    )


def test_a_detached_host_is_off_its_default_branch_and_is_not_advanced(tmp_path):
    repo, queue = _host_with_merged_wip(tmp_path)
    git(repo, "checkout", "-q", "--detach")
    host = host_land.HostQueue.of(queue)
    assert host is not None

    assert host_land.level_host(host) == (host_land.Standing.OFF_DEFAULT, None)


def test_a_host_behind_origin_is_advanced_by_level_host(tmp_path):
    repo, queue = _host_with_merged_wip(tmp_path)
    host = host_land.HostQueue.of(queue)
    assert host is not None

    assert host_land.level_host(host) == (host_land.Standing.BEHIND, Advance(True))
    assert _head(repo) == git(repo, "rev-parse", "origin/main").stdout.strip()
