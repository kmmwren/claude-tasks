import pathlib
import subprocess

import append_log
import pytest

ID = "demo-brief"


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    main = tmp_path / "main"
    subprocess.run(["git", "clone", "-q", str(origin), str(main)], check=True)
    (main / "ready").mkdir()
    (main / "ready" / f"{ID}.md").write_text(
        f"---\nid: {ID}\n---\n\n## Execution log\n\n2026-01-01: captured.\n"
    )
    git(main, "add", "-A")
    git(main, "commit", "-qm", "base")
    git(main, "push", "-q", "origin", "HEAD:main")
    git(main, "config", "user.name", "t")
    git(main, "config", "user.email", "t@t")
    return origin, main


def test_succeeds_while_main_checkout_writes_are_blocked(repos, monkeypatch):
    origin, main = repos
    real = pathlib.Path.write_text

    def guarded(self, *a, **k):
        # Stand-in for block-main-checkout-writes: any write inside the main checkout is refused.
        if main in self.resolve().parents:
            raise PermissionError(f"blocked main-checkout write: {self}")
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "write_text", guarded)
    with pytest.raises(PermissionError):
        (main / "ready" / f"{ID}.md").write_text("x")  # proves the guard is active
    sha = append_log.append_log(ID, "did a thing", root=main, today="2026-11-06")
    assert git(main, "status", "--porcelain") == ""
    assert "2026-11-06: did a thing" in git(origin, "show", f"{sha}:ready/{ID}.md")


def test_main_checkout_untouched_and_worktree_removed(repos):
    origin, main = repos
    before = (main / "ready" / f"{ID}.md").read_text()
    sha = append_log.append_log(ID, "did a thing", root=main, today="2026-11-06")
    assert (main / "ready" / f"{ID}.md").read_text() == before
    assert git(main, "status", "--porcelain") == ""
    assert git(main, "worktree", "list").count("\n") == 1
    assert git(origin, "show", f"{sha}:ready/{ID}.md").endswith("2026-11-06: did a thing\n")


def test_unknown_id_exits_1(repos):
    _, main = repos
    with pytest.raises(ValueError):
        append_log.append_log("nope", "x", root=main)


def test_queue_in_repo_subdirectory(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    main = tmp_path / "main"
    subprocess.run(["git", "clone", "-q", str(origin), str(main)], check=True)
    (main / ".tasks" / "ready").mkdir(parents=True)
    (main / ".tasks" / "ready" / f"{ID}.md").write_text("---\nid: demo-brief\n---\n")
    git(main, "add", "-A")
    git(main, "commit", "-qm", "base")
    git(main, "push", "-q", "origin", "HEAD:main")
    sha = append_log.append_log(ID, "nested", root=main / ".tasks", today="2026-11-06")
    shown = git(origin, "show", f"{sha}:.tasks/ready/{ID}.md")
    assert "## Execution log\n\n2026-11-06: nested\n" in shown


def test_cli_exit_codes(repos, capsys):
    _, main = repos
    assert append_log.main([ID, "via cli", "--root", str(main)]) == 0
    assert "logged to demo-brief" in capsys.readouterr().out
    assert append_log.main(["nope", "x", "--root", str(main)]) == 1


def _failing(real, fragment):
    """Wrap append_log._run so any git call whose args contain `fragment` fails."""

    def run(repo, *args):
        if fragment in args:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
        return real(repo, *args)

    return run


@pytest.mark.parametrize("fragment, message", [("fetch", "fetch"), ("worktree", "worktree add")])
def test_git_setup_failure_is_a_runtime_error(repos, monkeypatch, fragment, message):
    _, main = repos
    monkeypatch.setattr(append_log, "_run", _failing(append_log._run, fragment))
    with pytest.raises(RuntimeError, match=message):
        append_log.append_log(ID, "x", root=main)


def test_commit_failure_is_a_runtime_error(repos):
    _, main = repos
    hook = main / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(RuntimeError, match="commit failed"):
        append_log.append_log(ID, "x", root=main)


def test_push_that_always_loses_the_race_gives_up_naming_the_line(repos, capsys, monkeypatch):
    _, main = repos
    monkeypatch.setattr(append_log, "_try_push", lambda *a: None)
    monkeypatch.setattr(append_log, "_backoff", lambda attempt: None)
    assert append_log.main([ID, "keep me", "--root", str(main)]) == 2
    err = capsys.readouterr().err
    assert f"still rejected after {append_log.PUSH_ATTEMPTS} attempts" in err and "keep me" in err
    assert git(main, "worktree", "list").count("\n") == 1


PEER = "peer-memory-abc12345"


def _file_uncommitted_peer_brief(main):
    (main / "inbox").mkdir(exist_ok=True)
    (main / "inbox" / f"{PEER}.md").write_text(
        f"---\nid: {PEER}\n---\n\n## Execution log\n\n2026-01-01: filed.\n"
    )


def test_brief_uncommitted_in_main_checkout_is_logged_and_leaves_sync_working(repos):
    origin, main = repos
    _file_uncommitted_peer_brief(main)
    sha = append_log.append_log(PEER, "no action needed", root=main, today="2026-11-10")
    assert "2026-11-10: no action needed" in git(origin, "show", f"{sha}:inbox/{PEER}.md")
    assert f"?? inbox/{PEER}.md" not in git(main, "status", "--porcelain")
    git(main, "pull", "-q", "--rebase", "origin", "main")
    assert (main / "inbox" / f"{PEER}.md").read_text().endswith("2026-11-10: no action needed\n")


def test_deferred_filing_of_uncommitted_brief_is_a_runtime_error(repos, monkeypatch):
    _, main = repos
    _file_uncommitted_peer_brief(main)
    monkeypatch.setattr(append_log, "sync_commit_and_push", lambda *a, **k: False)
    with pytest.raises(RuntimeError, match="uncommitted"):
        append_log.append_log(PEER, "x", root=main)


def test_staged_but_uncommitted_brief_is_committed_then_logged(repos):
    origin, main = repos
    _file_uncommitted_peer_brief(main)
    git(main, "add", f"inbox/{PEER}.md")
    sha = append_log.append_log(PEER, "staged only", root=main, today="2026-11-10")
    assert "2026-11-10: staged only" in git(origin, "show", f"{sha}:inbox/{PEER}.md")


def test_relative_root_sees_a_committed_brief_as_committed(repos, monkeypatch, tmp_path):
    _, main = repos
    monkeypatch.chdir(tmp_path)

    def refuse(*a, **k):
        raise AssertionError("a committed brief must not be re-filed")

    monkeypatch.setattr(append_log, "sync_commit_and_push", refuse)
    assert append_log.main([ID, "relative root", "--root", main.name]) == 0


def test_filing_that_commits_nothing_is_a_runtime_error(repos, monkeypatch):
    _, main = repos
    _file_uncommitted_peer_brief(main)
    monkeypatch.setattr(append_log, "sync_commit_and_push", lambda *a, **k: True)
    with pytest.raises(RuntimeError, match="could not commit"):
        append_log.append_log(PEER, "x", root=main)


def test_busy_queue_lock_still_pushes_and_says_so(repos, capsys, monkeypatch):
    origin, main = repos
    monkeypatch.setattr(append_log, "LOCK_WAIT", 0.2)
    with append_log.repo_lock(main) as held:
        assert held
        sha = append_log.append_log(ID, "lock was busy", root=main, today="2026-11-10")
    assert "queue lock busy" in capsys.readouterr().err
    assert "2026-11-10: lock was busy" in git(origin, "show", f"{sha}:ready/{ID}.md")


@pytest.mark.parametrize("attempt, cap", [(0, 0.25), (2, 1.0), (10, 4.0)])
def test_backoff_sleeps_at_most_the_capped_doubling(monkeypatch, attempt, cap):
    slept = []
    monkeypatch.setattr(append_log.random, "uniform", lambda lo, hi: (lo, hi)[1])
    monkeypatch.setattr(append_log.time, "sleep", slept.append)
    append_log._backoff(attempt)
    assert slept == [cap]


def _push_from_peer(origin, tmp_path, n):
    peer = tmp_path / f"peer-clone-{n}"
    if not peer.exists():
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(peer)], check=True, capture_output=True
        )
    git(peer, "pull", "-q", "--rebase", "origin", "main")
    (peer / f"racer-{n}.txt").write_text("x")
    git(peer, "add", "-A")
    git(peer, "commit", "-qm", f"racer {n}")
    git(peer, "push", "-q", "origin", "HEAD:main")


def test_main_checkout_behind_with_dirty_tree_still_logs(repos, tmp_path):
    origin, main = repos
    for n in range(4):
        _push_from_peer(origin, tmp_path, n)
    (main / "ready" / f"{ID}.md").unlink()
    _file_uncommitted_peer_brief(main)
    git(main, "rm", "-q", "--cached", f"ready/{ID}.md")
    git(main, "reset", "-q")
    sha = append_log.append_log(ID, "logged while behind", root=main, today="2026-11-10")
    assert "2026-11-10: logged while behind" in git(origin, "show", f"{sha}:ready/{ID}.md")


def test_push_that_keeps_losing_races_still_lands_the_line(repos, tmp_path, monkeypatch):
    origin, main = repos
    real, raced = append_log._run, []

    def racing(repo, *args):
        if args[:1] == ("push",) and len(raced) < 5:
            _push_from_peer(origin, tmp_path, len(raced))
            raced.append(1)
        return real(repo, *args)

    monkeypatch.setattr(append_log, "_run", racing)
    monkeypatch.setattr(append_log, "_backoff", lambda attempt: None)
    sha = append_log.append_log(ID, "after five lost races", root=main, today="2026-11-10")
    assert "2026-11-10: after five lost races" in git(origin, "show", f"{sha}:ready/{ID}.md")
    assert git(origin, "log", "--format=%s", "main").count("racer") == 5


def test_push_declined_for_another_reason_fails_at_once_with_the_line(repos, capsys, monkeypatch):
    origin, main = repos
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'policy says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    pushes = []
    real = append_log._run
    monkeypatch.setattr(
        append_log,
        "_run",
        lambda repo, *a: (a[:1] == ("push",) and pushes.append(1), real(repo, *a))[1],
    )
    assert append_log.main([ID, "keep me", "--root", str(main)]) == 2
    err = capsys.readouterr().err
    assert "policy says no" in err and "keep me" in err
    assert len(pushes) == 1
    assert git(main, "worktree", "list").count("\n") == 1
