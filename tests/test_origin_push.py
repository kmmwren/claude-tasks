"""origin_push against a real bare origin: what counts as a lost race, and what is a refusal."""

import pathlib
import subprocess
import types

import origin_push
import pytest

from tests.maintain_helpers import origin_head, second_clone
from tests.test_git_sync import git, make_repo


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(origin_push, "time", types.SimpleNamespace(sleep=lambda seconds: None))


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    return make_repo(tmp_path)


def hold_main_ref_lock(tmp_path: pathlib.Path) -> None:
    (tmp_path / "remote" / "refs" / "heads" / "main.lock").write_text("")


def decline_every_push(tmp_path: pathlib.Path, message: str) -> None:
    hook = tmp_path / "remote" / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\necho '{message}' >&2\nexit 1\n")
    hook.chmod(0o755)


def real_push_stderr(tmp_path: pathlib.Path) -> str:
    """What git itself prints when a push to origin's main is refused."""
    work = second_clone(tmp_path, "pusher")
    (work / "note.md").write_text("n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "note")
    pushed = subprocess.run(
        ["git", "-C", str(work), "push", "origin", "HEAD:main"], capture_output=True, text=True
    )
    assert pushed.returncode != 0
    return pushed.stderr


def write_note(worktree: pathlib.Path) -> bool:
    (worktree / "note.md").write_text("n")
    return True


def test_a_real_ref_lock_clash_is_a_lost_race(tmp_path: pathlib.Path, root: pathlib.Path) -> None:
    hold_main_ref_lock(tmp_path)
    assert origin_push.lost_race(real_push_stderr(tmp_path))


def test_a_real_hook_declined_push_is_not_a_lost_race(
    tmp_path: pathlib.Path, root: pathlib.Path
) -> None:
    decline_every_push(tmp_path, "cannot lock ref (failed); reference already exists")
    stderr = real_push_stderr(tmp_path)
    assert "remote: cannot lock ref" in stderr
    assert not origin_push.lost_race(stderr)


def test_a_real_non_fast_forward_push_is_a_lost_race(
    tmp_path: pathlib.Path, root: pathlib.Path
) -> None:
    other = second_clone(tmp_path, "winner")
    (other / "w.md").write_text("w")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "w")
    git(other, "push", "-q", "origin", "main")
    (root / "l.md").write_text("l")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "l")
    pushed = subprocess.run(
        ["git", "-C", str(root), "push", "origin", "HEAD:main"], capture_output=True, text=True
    )
    assert origin_push.lost_race(pushed.stderr)


def test_a_push_that_lands_returns_the_published_sha(root: pathlib.Path) -> None:
    sha = origin_push.push_from_fresh_origin(root, write_note, "note")
    assert sha == origin_head(root)


def test_an_edit_with_nothing_to_publish_pushes_nothing(root: pathlib.Path) -> None:
    before = origin_head(root)
    origin_push.push_from_fresh_origin(root, lambda worktree: False, "nothing")
    assert origin_head(root) == before


def test_an_edit_that_changes_no_file_pushes_nothing(root: pathlib.Path) -> None:
    before = origin_head(root)
    origin_push.push_from_fresh_origin(root, lambda worktree: True, "no change")
    assert origin_head(root) == before


def test_a_ref_lock_held_through_every_attempt_leaves_origin_busy(
    tmp_path: pathlib.Path, root: pathlib.Path
) -> None:
    hold_main_ref_lock(tmp_path)
    with pytest.raises(origin_push.OriginBusy, match="still rejected"):
        origin_push.push_from_fresh_origin(root, write_note, "note")


def test_a_push_the_remote_refuses_is_refused_and_tried_once(
    tmp_path: pathlib.Path, root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decline_every_push(tmp_path, "protected branch")
    monkeypatch.setattr(
        origin_push, "time", types.SimpleNamespace(sleep=lambda s: pytest.fail("retried"))
    )
    with pytest.raises(origin_push.PushRefused, match=r"(?s)push refused: .*protected branch"):
        origin_push.push_from_fresh_origin(root, write_note, "note")


def test_a_refusal_is_a_push_error_but_a_busy_origin_is_not_a_refusal() -> None:
    assert issubclass(origin_push.PushRefused, origin_push.PushError)
    assert not issubclass(origin_push.OriginBusy, origin_push.PushRefused)


def failing(step: tuple[str, ...], stderr: str):  # type: ignore[no-untyped-def]
    """A stand-in for `_run` that fails the git call starting with `step` and runs the rest."""
    real = origin_push._run

    def run(
        repo: pathlib.Path, *args: str, timeout: float | None = None
    ) -> "subprocess.CompletedProcess[str]":
        if any(args[i : i + len(step)] == step for i in range(len(args))):
            return subprocess.CompletedProcess(args, 1, "", stderr)
        return real(repo, *args, timeout=timeout)

    return run


def test_a_failed_git_add_fails_the_push_instead_of_publishing_a_partial_tree(
    root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = origin_head(root)
    monkeypatch.setattr(origin_push, "_run", failing(("add", "--all"), "index.lock exists"))
    with pytest.raises(origin_push.PushError, match="git add failed: index.lock exists"):
        origin_push.push_from_fresh_origin(root, write_note, "note")
    assert origin_head(root) == before


def test_a_failed_commit_fails_the_push(
    root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(origin_push, "_run", failing(("commit", "-q"), "no identity"))
    with pytest.raises(origin_push.PushError, match="commit failed"):
        origin_push.push_from_fresh_origin(root, write_note, "note")


def test_a_worktree_that_cannot_be_cut_fails_the_push(
    root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(origin_push, "_run", failing(("worktree", "add"), "no space"))
    with pytest.raises(origin_push.PushError, match="worktree add failed: no space"):
        origin_push.push_from_fresh_origin(root, write_note, "note")


def test_a_fetch_that_fails_fails_the_push(tmp_path: pathlib.Path, root: pathlib.Path) -> None:
    git(root, "remote", "set-url", "origin", str(tmp_path / "gone"))
    with pytest.raises(origin_push.PushError, match="git fetch origin main failed"):
        origin_push.push_from_fresh_origin(root, write_note, "note")


def test_a_hung_remote_fails_the_push_instead_of_hanging_it(
    root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(
        repo: pathlib.Path, *args: str, timeout: float | None = None
    ) -> "subprocess.CompletedProcess[str]":
        raise subprocess.TimeoutExpired(args, timeout or 0)

    monkeypatch.setattr(origin_push, "_run", run)
    with pytest.raises(origin_push.PushError, match="timed out"):
        origin_push.fetch_origin(root, "main")


def test_the_backoff_between_attempts_is_jittered_and_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(origin_push, "time", types.SimpleNamespace(sleep=slept.append))
    for attempt in (0, 1, 20):
        origin_push._backoff(attempt)
    assert len(slept) == 3 and all(0 <= s <= 4.0 for s in slept) and slept[0] <= 0.25
