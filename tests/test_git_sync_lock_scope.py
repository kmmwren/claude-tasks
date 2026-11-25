"""The sweep reads every changed brief before it takes the queue lock, and holds the lock only to stage and commit.

Other writers (`transition.py move`) wait on that lock for 30 s; a sweep that scanned under it
made them time out. A scan the lock-free pass did is reused only while the tree is as it was scanned,
so no commit carries content a check has not read.
"""

import contextlib
import pathlib

import _concurrency as C
import git_sync
from _tree_state import TreeState

from tests.queue_helpers import wait_for_pusher_idle
from tests.test_git_sync import committed_files, git, make_repo

SECRET = "token ghp_" + "c" * 30


def _lock_is_free(root: pathlib.Path) -> bool:
    """A second, non-blocking flock on the queue lock, as a waiting writer would try."""
    held = C.try_lock(C.lock_path(root))
    if held is None:
        return False
    held.release()
    return True


def _record_lock_state_while_running(monkeypatch, root: pathlib.Path) -> dict[str, list[bool]]:
    """Wrap each read-only check so it notes whether the queue lock was free as it ran."""
    seen: dict[str, list[bool]] = {"secret": [], "shadow": []}

    def wrap(name: str, key: str) -> None:
        real = getattr(git_sync, name)

        def checked(*args, **kwargs):
            seen[key].append(_lock_is_free(root))
            return real(*args, **kwargs)

        monkeypatch.setattr(git_sync, name, checked)

    wrap("_secret_bearing", "secret")
    wrap("_shadowing_briefs", "shadow")
    return seen


def test_the_sweep_scans_without_holding_the_queue_lock(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "inbox" / "fresh.md").write_text("ok")
    seen = _record_lock_state_while_running(monkeypatch, repo)

    git_sync.sweep(repo)

    assert wait_for_pusher_idle(repo)
    assert seen == {"secret": [True], "shadow": [True]}
    assert "inbox/fresh.md" in committed_files(repo)


def test_a_scan_taken_before_the_lock_is_not_repeated_under_it(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "inbox" / "fresh.md").write_text("ok")
    seen = _record_lock_state_while_running(monkeypatch, repo)

    git_sync.sweep(repo)

    assert wait_for_pusher_idle(repo)
    assert [len(runs) for runs in seen.values()] == [1, 1]


def _write_between_scan_and_lock(monkeypatch, path: pathlib.Path, text: str) -> None:
    """A writer lands `path` after the lock-free scan but before the sweep holds the lock."""
    real = git_sync.repo_lock

    @contextlib.contextmanager
    def racing(root, **kw):
        path.write_text(text)
        with real(root, **kw) as acquired:
            yield acquired

    monkeypatch.setattr(git_sync, "repo_lock", racing)


def test_a_credential_written_after_the_scan_is_still_held_back(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "inbox" / "fine.md").write_text("ok")
    _write_between_scan_and_lock(monkeypatch, repo / "inbox" / "late-leaky.md", SECRET)

    git_sync.sweep(repo)

    assert wait_for_pusher_idle(repo)
    files = committed_files(repo)
    assert "inbox/fine.md" in files
    assert "inbox/late-leaky.md" not in files


def test_a_credential_added_to_a_scanned_brief_after_the_scan_is_still_held_back(
    tmp_path, monkeypatch
):
    repo = make_repo(tmp_path)
    brief = repo / "inbox" / "edited.md"
    brief.write_text("clean")
    _write_between_scan_and_lock(monkeypatch, brief, SECRET)

    git_sync.sweep(repo)

    assert wait_for_pusher_idle(repo)
    assert "inbox/edited.md" not in committed_files(repo)


def test_a_shadow_filed_after_the_scan_is_still_refused(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "done" / "same-id.md").write_text("done")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed done")
    _write_between_scan_and_lock(monkeypatch, repo / "inbox" / "same-id.md", "again")

    git_sync.sweep(repo)

    assert wait_for_pusher_idle(repo)
    assert "inbox/same-id.md" not in committed_files(repo)


def test_a_stale_scan_is_retaken_not_reused(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "inbox" / "a.md").write_text("one")
    scan = git_sync.Scan.take(repo, (".",))

    (repo / "inbox" / "b.md").write_text(SECRET)
    current = scan.current(repo, (".",))

    assert current is not scan
    assert current.leaks == ("inbox/b.md",)


def test_an_unchanged_tree_keeps_its_scan(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "inbox" / "a.md").write_text("one")
    scan = git_sync.Scan.take(repo, (".",))

    assert scan.current(repo, (".",)) is scan


def test_tree_state_differs_after_an_edit_a_stage_or_a_move(tmp_path):
    repo = make_repo(tmp_path)
    brief = repo / "inbox" / "a.md"
    brief.write_text("one")
    before = TreeState.read(repo, "origin/main")

    assert TreeState.read(repo, "origin/main") == before
    brief.write_text("two")
    edited = TreeState.read(repo, "origin/main")
    assert edited != before
    git(repo, "add", "inbox/a.md")
    staged = TreeState.read(repo, "origin/main")
    assert staged != edited
    git(repo, "mv", "inbox/a.md", "ready/a.md")
    assert [c.path for c in TreeState.read(repo, "origin/main").changes] == ["ready/a.md"]


def test_a_deleted_file_is_a_change_with_no_stamp(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "inbox" / ".keep").unlink()

    (change,) = TreeState.read(repo, "origin/main").changes

    assert (change.path, change.stamp) == ("inbox/.keep", None)
