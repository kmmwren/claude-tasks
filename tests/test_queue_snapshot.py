"""A queue is read as its remote's default branch holds it, cached by sha, and degrades to the cache."""

import dataclasses
import datetime
import os
import pathlib
import shutil
import subprocess

import _default_branch
import pytest
import queue_snapshot as qs

from tests.test_git_sync import git, make_repo

QUEUE = "ready"


def commit(clone: pathlib.Path, name: str, text: str = "x", when: str | None = None) -> str:
    """Commit and push one brief, optionally with a fixed commit date; returns the pushed sha."""
    (clone / QUEUE / f"{name}.md").write_text(text)
    git(clone, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else None
    subprocess.run(
        ["git", "-C", str(clone), "commit", "-qm", name], check=True, capture_output=True, env=env
    )
    git(clone, "push", "-q", "origin", "main")
    return git(clone, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def clone(tmp_path: pathlib.Path) -> pathlib.Path:
    path = make_repo(tmp_path)
    commit(path, "first", "pushed text")
    return path


@pytest.fixture
def remote(tmp_path: pathlib.Path, clone: pathlib.Path) -> pathlib.Path:
    return tmp_path / "remote"


@pytest.fixture
def cache(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "cache"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The git subcommands run through queue_snapshot, in order."""
    seen: list[str] = []
    real = qs._git

    def spy(*args: str, **kw: object) -> bytes:
        seen.append(args[0])
        return real(*args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(qs, "_git", spy)
    return seen


def go_offline(remote: pathlib.Path) -> None:
    shutil.move(remote, remote.with_name("elsewhere"))


def test_a_miss_extracts_the_pushed_queue_at_the_remote_head(clone, remote, cache):
    snap = qs.read_queue(clone, QUEUE, cache_dir=cache)

    assert (snap.root / "first.md").read_text() == "pushed text"
    assert snap.sha == git(remote, "rev-parse", "main").stdout.strip()
    assert snap.branch == "main" and snap.stale is False
    assert snap.committed == datetime.datetime.fromtimestamp(
        int(git(remote, "log", "-1", "--format=%ct", "main").stdout), datetime.timezone.utc
    )


def test_local_edits_and_unpushed_commits_are_never_read(clone, cache):
    (clone / QUEUE / "first.md").write_text("dirty")
    (clone / QUEUE / "local.md").write_text("unpushed")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "local only")

    snap = qs.read_queue(clone, QUEUE, cache_dir=cache)

    assert (snap.root / "first.md").read_text() == "pushed text"
    assert not (snap.root / "local.md").exists()


def test_a_hit_asks_only_for_the_head_and_fetches_nothing(clone, cache, calls):
    first = qs.read_queue(clone, QUEUE, cache_dir=cache)
    calls.clear()

    again = qs.read_queue(clone, QUEUE, cache_dir=cache)

    assert again == first
    assert "ls-remote" in calls and not {"fetch", "archive"} & set(calls)


def test_a_new_head_is_fetched_and_archived_beside_the_old_one(clone, cache, calls):
    old = qs.read_queue(clone, QUEUE, cache_dir=cache)
    new_sha = commit(clone, "second")
    calls.clear()

    new = qs.read_queue(clone, QUEUE, cache_dir=cache)

    assert new.sha == new_sha
    assert calls[-4:] == ["ls-remote", "fetch", "log", "archive"]
    assert (new.root / "second.md").exists() and (old.root / "first.md").exists()


def test_a_remote_url_is_read_without_a_clone(remote, cache):
    snap = qs.read_queue(str(remote), QUEUE, cache_dir=cache)

    assert (snap.root / "first.md").read_text() == "pushed text"
    assert snap.branch == "main"


def test_a_trailing_slash_names_the_same_queue(clone, cache):
    assert (qs.read_queue(clone, QUEUE + "/", cache_dir=cache).root / "first.md").exists()


@pytest.mark.parametrize("bad", ["/etc", "../outside", "ready/../../x"])
def test_a_queue_path_leaving_the_repo_is_refused(clone, cache, bad):
    with pytest.raises(ValueError, match="inside the repo"):
        qs.read_queue(clone, bad, cache_dir=cache)


def test_offline_returns_the_newest_cached_snapshot_stale_with_one_warning(
    clone, remote, cache, capfd
):
    commit(clone, "second")
    qs.read_queue(clone, QUEUE, cache_dir=cache)
    newest_sha = commit(clone, "third")
    qs.read_queue(clone, QUEUE, cache_dir=cache)
    go_offline(remote)
    capfd.readouterr()

    snap = qs.read_queue(clone, QUEUE, cache_dir=cache)

    err = capfd.readouterr().err.splitlines()
    assert (snap.sha, snap.stale) == (newest_sha, True)
    assert (snap.root / "third.md").exists()
    assert len(err) == 1 and str(remote) in err[0] and "not reachable" in err[0]


def test_commits_sharing_a_timestamp_still_fall_back_to_the_last_head_seen(clone, remote, cache):
    """Six commits in one second: commit time cannot rank them, the last-seen pointer can."""
    same_second = "2026-03-01T12:00:00+00:00"
    for n in range(6):
        head_sha = commit(clone, f"tied{n}", when=same_second)
        qs.read_queue(clone, QUEUE, cache_dir=cache)
    go_offline(remote)

    snap = qs.read_queue(clone, QUEUE, cache_dir=cache)

    assert (snap.sha, snap.stale) == (head_sha, True)


@pytest.fixture
def two_cached(clone, cache):
    """Two cached heads a week apart: (older sha, newer sha)."""
    older = commit(clone, "older", when="2026-01-01T12:00:00+00:00")
    qs.read_queue(clone, QUEUE, cache_dir=cache)
    newer = commit(clone, "newer", when="2026-01-08T12:00:00+00:00")
    qs.read_queue(clone, QUEUE, cache_dir=cache)
    return older, newer


def pointer_of(cache: pathlib.Path) -> pathlib.Path:
    (pointer,) = cache.glob(f"*/{qs.HEAD_POINTER}")
    return pointer


def test_a_cache_hit_also_records_the_head_as_last_seen(clone, cache, two_cached):
    older, _ = two_cached
    pointer_of(cache).write_text(older)

    qs.read_queue(clone, QUEUE, cache_dir=cache)

    assert pointer_of(cache).read_text() == two_cached[1]


def test_the_pointer_wins_over_commit_time(clone, remote, cache, two_cached):
    older, _ = two_cached
    pointer_of(cache).write_text(older)
    go_offline(remote)

    assert qs.read_queue(clone, QUEUE, cache_dir=cache).sha == older


@pytest.mark.parametrize("pointer", [None, "", "deadbeef"])
def test_a_missing_empty_or_uncached_pointer_falls_back_to_commit_time(
    clone, remote, cache, two_cached, pointer
):
    if pointer is None:
        pointer_of(cache).unlink()
    else:
        pointer_of(cache).write_text(pointer)
    go_offline(remote)

    assert qs.read_queue(clone, QUEUE, cache_dir=cache).sha == two_cached[1]


def test_a_remote_url_offline_also_falls_back_to_the_cache(remote, cache):
    cached = qs.read_queue(str(remote), QUEUE, cache_dir=cache)
    go_offline(remote)

    assert qs.read_queue(str(remote), QUEUE, cache_dir=cache) == dataclasses.replace(
        cached, stale=True
    )


def test_a_timeout_degrades_like_any_other_network_failure(clone, cache, monkeypatch, capfd):
    cached = qs.read_queue(clone, QUEUE, cache_dir=cache)
    real = subprocess.run

    def hang_on_ls_remote(cmd, **kw):
        if "ls-remote" in cmd:
            raise subprocess.TimeoutExpired(cmd, 1)
        return real(cmd, **kw)

    monkeypatch.setattr(qs.subprocess, "run", hang_on_ls_remote)

    assert qs.read_queue(clone, QUEUE, cache_dir=cache).sha == cached.sha
    assert "timed out" in capfd.readouterr().err


def test_offline_with_nothing_cached_raises_one_typed_error(clone, remote, cache):
    go_offline(remote)

    with pytest.raises(qs.SnapshotUnavailable, match="no snapshot is cached") as caught:
        qs.read_queue(clone, QUEUE, cache_dir=cache)

    assert str(remote) in str(caught.value)


def test_a_remote_head_naming_no_branch_is_unavailable(tmp_path, remote, cache):
    git(remote, "branch", "-m", "main", "trunk")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/gone")

    with pytest.raises(qs.SnapshotUnavailable, match="no branch"):
        qs.read_queue(str(remote), QUEUE, cache_dir=cache)


def test_a_clone_whose_default_branch_cannot_be_named_is_unavailable(clone, remote, cache):
    git(clone, "remote", "set-head", "origin", "-d")
    git(clone, "checkout", "-q", "--detach")
    go_offline(remote)
    _default_branch.forget()

    with pytest.raises(qs.SnapshotUnavailable, match="cannot tell which branch"):
        qs.read_queue(clone, QUEUE, cache_dir=cache)


def test_a_clone_without_an_origin_is_unavailable(tmp_path, cache):
    lone = tmp_path / "lone"
    subprocess.run(["git", "init", "-q", str(lone)], check=True)

    with pytest.raises(qs.SnapshotUnavailable, match="no origin"):
        qs.read_queue(lone, QUEUE, cache_dir=cache)


def test_a_queue_missing_at_the_head_is_unavailable_not_a_traceback(clone, cache):
    with pytest.raises(qs.SnapshotUnavailable, match="archive"):
        qs.read_queue(clone, "nothing-here", cache_dir=cache)


def history(clone: pathlib.Path) -> dict[str, str]:
    """Three briefs a week apart, so a cutoff between them is unambiguous."""
    return {
        "old": commit(clone, "old", "old text", "2026-01-01T12:00:00+00:00"),
        "mid": commit(clone, "mid", "mid text", "2026-01-08T12:00:00+00:00"),
        "head": commit(clone, "head", "head text", "2026-01-15T12:00:00+00:00"),
    }


@pytest.mark.parametrize(("days", "expected"), [(10, "old"), (3, "mid"), (0, "head")])
def test_days_before_reads_the_commit_that_old_on_the_same_branch(clone, cache, days, expected):
    shas = history(clone)
    head = qs.read_queue(clone, QUEUE, cache_dir=cache)

    earlier = qs.read_queue_before(clone, QUEUE, head, days, cache_dir=cache)

    assert earlier is not None
    assert (earlier.sha, earlier.branch, earlier.stale) == (shas[expected], "main", False)
    assert (earlier.root / f"{expected}.md").read_text() == f"{expected} text"


def test_days_before_extracts_once_and_never_fetches(clone, cache, calls):
    history(clone)
    head = qs.read_queue(clone, QUEUE, cache_dir=cache)
    calls.clear()

    first = qs.read_queue_before(clone, QUEUE, head, 10, cache_dir=cache)
    second = qs.read_queue_before(clone, QUEUE, head, 10, cache_dir=cache)

    assert first == second
    assert calls.count("archive") == 1 and "fetch" not in calls and "ls-remote" not in calls


def test_days_before_with_no_commit_that_old_is_none(clone, cache):
    history(clone)
    head = qs.read_queue(clone, QUEUE, cache_dir=cache)

    assert qs.read_queue_before(clone, QUEUE, head, 400, cache_dir=cache) is None


def test_days_before_a_stale_head_is_stale(clone, cache):
    history(clone)
    head = dataclasses.replace(qs.read_queue(clone, QUEUE, cache_dir=cache), stale=True)

    earlier = qs.read_queue_before(clone, QUEUE, head, 10, cache_dir=cache)

    assert earlier is not None and earlier.stale is True


def test_days_before_without_the_mirror_is_unavailable(clone, cache, tmp_path):
    head = qs.read_queue(clone, QUEUE, cache_dir=cache)

    with pytest.raises(qs.SnapshotUnavailable, match="rev-list"):
        qs.read_queue_before(clone, QUEUE, head, 1, cache_dir=tmp_path / "empty")


def test_a_rival_publishing_the_same_sha_first_is_not_an_error(clone, cache, monkeypatch):
    real = qs._extract_tar

    def rival_wins(data: bytes, dest: pathlib.Path) -> None:
        real(data, dest)
        sha = git(clone, "rev-parse", "HEAD").stdout.strip()
        shutil.copytree(dest.parent, dest.parent.parent.parent / sha)

    monkeypatch.setattr(qs, "_extract_tar", rival_wins)

    assert (qs.read_queue(clone, QUEUE, cache_dir=cache).root / "first.md").exists()


def test_a_publish_failing_for_any_other_reason_is_raised(clone, cache):
    sha = git(clone, "rev-parse", "HEAD").stdout.strip()
    mirror = qs._Mirror.of(clone, QUEUE, cache)
    mirror.home.mkdir(parents=True)
    (mirror.home / sha).write_text("in the way")

    with pytest.raises(OSError):
        qs.read_queue(clone, QUEUE, cache_dir=cache)


def test_the_cache_root_is_the_argument_then_the_environment_then_a_user_default(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(qs.CACHE_ENV, str(tmp_path / "env"))
    assert qs.cache_root(tmp_path / "arg") == tmp_path / "arg"
    assert qs.cache_root() == tmp_path / "env"

    monkeypatch.delenv(qs.CACHE_ENV)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert qs.cache_root() == tmp_path / ".claude-tasks" / "queue-snapshots"
