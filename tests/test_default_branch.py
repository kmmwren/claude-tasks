"""The default branch of a queue's origin is asked for, in order of cost, and never assumed."""

import pathlib
import subprocess

import _default_branch as db
import pytest


def run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True)


def commit_in(repo: pathlib.Path) -> None:
    run("git", "-C", str(repo), "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", "c")  # fmt: skip


@pytest.fixture
def remote(tmp_path) -> pathlib.Path:
    """A bare remote whose default branch is `trunk`, with one commit on it."""
    seed, bare = tmp_path / "seed", tmp_path / "remote.git"
    run("git", "init", "-q", "-b", "trunk", str(seed))
    commit_in(seed)
    run("git", "clone", "-q", "--bare", str(seed), str(bare))
    return bare


@pytest.fixture
def clone(tmp_path, remote) -> pathlib.Path:
    path = tmp_path / "clone"
    run("git", "clone", "-q", str(remote), str(path))
    return path


def test_origin_head_names_the_branch_without_asking_the_remote(clone, monkeypatch):
    monkeypatch.setattr(db, "_from_remote", lambda root: pytest.fail("the remote was asked"))
    assert db.default_branch(clone) == "trunk"


def test_the_remote_is_asked_when_origin_head_is_not_recorded(clone):
    run("git", "-C", str(clone), "remote", "set-head", "origin", "-d")
    assert db._from_origin_head(clone) == ""
    assert db.default_branch(clone) == "trunk"


def test_the_checked_out_branch_answers_when_the_remote_cannot(tmp_path):
    repo = tmp_path / "lonely"
    run("git", "init", "-q", "-b", "feature", str(repo))
    run("git", "-C", str(repo), "remote", "add", "origin", str(tmp_path / "nowhere.git"))
    assert db._from_remote(repo) == ""
    assert db.default_branch(repo) == "feature"


def test_a_remote_with_no_head_branch_is_not_an_answer(tmp_path):
    empty = tmp_path / "empty.git"
    run("git", "init", "-q", "--bare", "-b", "main", str(empty))
    repo = tmp_path / "repo"
    run("git", "init", "-q", "-b", "local", str(repo))
    run("git", "-C", str(repo), "remote", "add", "origin", str(empty))
    assert db._from_remote(repo) == ""
    assert db.default_branch(repo) == "local"


def test_no_source_naming_a_branch_is_an_error_that_names_the_root(tmp_path):
    with pytest.raises(db.DefaultBranchError, match=str(tmp_path.resolve())):
        db.default_branch(tmp_path)


def test_a_git_that_hangs_is_cut_off_and_the_next_source_is_tried(monkeypatch, tmp_path):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", kwargs["timeout"])

    monkeypatch.setattr(db.subprocess, "run", hang)
    assert db.git_stdout(tmp_path, "remote", "show", "origin", timeout=1.0) == ""


def test_only_the_remote_query_carries_a_timeout(clone, monkeypatch):
    seen: dict[str, float | None] = {}
    real = subprocess.run

    def spy(cmd, **kwargs):
        seen[cmd[3]] = kwargs.get("timeout")
        return real(cmd, **kwargs)

    run("git", "-C", str(clone), "remote", "set-head", "origin", "-d")
    monkeypatch.setattr(db.subprocess, "run", spy)
    db.default_branch(clone)
    assert seen["remote"] == db.REMOTE_TIMEOUT and seen["symbolic-ref"] is None


def test_an_answer_is_cached_per_root_until_forgotten(clone, monkeypatch):
    assert db.default_branch(clone) == "trunk"
    run("git", "-C", str(clone), "branch", "-m", "trunk", "renamed")
    run("git", "-C", str(clone), "remote", "set-head", "origin", "-d")
    monkeypatch.setattr(db, "_from_remote", lambda root: "")
    assert db.default_branch(clone) == "trunk"
    db.forget()
    assert db.default_branch(clone) == "renamed"


def test_a_symlinked_root_shares_the_answer_of_the_real_one(clone, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(clone)
    before = db._resolve.cache_info().misses
    db.default_branch(clone)
    db.default_branch(link)
    assert db._resolve.cache_info().misses == before + 1
