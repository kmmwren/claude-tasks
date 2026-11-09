import pathlib
import subprocess

import append_log
import pytest

ID = "demo-brief"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    main = tmp_path / "main"
    subprocess.run(["git", "clone", "-q", str(origin), str(main)], check=True)
    (main / "ready").mkdir()
    (main / "ready" / f"{ID}.md").write_text(f"---\nid: {ID}\n---\n\n## Execution log\n\n2026-01-01: captured.\n")
    git(main, "add", "-A")
    git(main, "commit", "-qm", "base")
    git(main, "push", "-q", "origin", "HEAD:main")
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


def test_rejected_push_retries_then_gives_up(repos, capsys):
    origin, main = repos
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    assert append_log.main([ID, "x", "--root", str(main)]) == 2
    assert "still rejected after 4 attempts" in capsys.readouterr().err
    assert git(main, "worktree", "list").count("\n") == 1
