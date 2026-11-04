"""
Tests for git_sync.sync_commit staging scope.

sync_commit stages the whole working tree (`git add --all`, respecting .gitignore), so a
root-level file or a new top-level dir is committed rather than silently left dirty — the
self-healing sweep these tests pin.
"""
import contextlib
import pathlib
import shutil
import subprocess

import git_sync
import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


class _FakeProc:
    def __init__(self, rc):
        self.returncode = rc


@contextlib.contextmanager
def _lock_acquired(_root, **_kw):
    yield True


@contextlib.contextmanager
def _lock_held(_root, **_kw):
    yield False


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, **kw)


def make_repo(tmp_path):
    """A throwaway clone wired like ~/tasks: task dirs, scripts/, and a bare remote."""
    remote, clone = tmp_path / "remote", tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    git(clone, "config", "user.email", "t@t.t")
    git(clone, "config", "user.name", "t")
    git(clone, "config", "commit.gpgsign", "false")
    git(clone, "symbolic-ref", "HEAD", "refs/heads/main")
    for d in ("inbox", "ready", "in-progress", "done", "parked"):
        (clone / d).mkdir()
    (clone / "inbox" / ".keep").write_text("")
    shutil.copytree(SCRIPTS, clone / "scripts")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "seed")
    git(clone, "push", "-q", "origin", "main")
    return clone


def committed_files(repo, ref="HEAD"):
    return set(git(repo, "ls-tree", "-r", "--name-only", ref).stdout.splitlines())


def test_sync_commit_stages_root_level_file(tmp_path):
    """A root-level file (not under a task dir) must be committed + pushed."""
    repo = make_repo(tmp_path)
    (repo / "actions.jsonl").write_text('{"op":"x"}\n')

    assert git_sync.sync_commit("test: root file", root=repo) is True

    assert "actions.jsonl" in committed_files(repo)
    git(repo, "fetch", "-q", "origin")
    assert "actions.jsonl" in committed_files(repo, "origin/main")


def test_sync_commit_stages_new_top_level_dir(tmp_path):
    """New files under a non-task dir (e.g. docs/) get committed too."""
    repo = make_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "note.md").write_text("hi")

    git_sync.sync_commit("test: docs", root=repo)

    assert "docs/note.md" in committed_files(repo)


def test_sync_commit_respects_gitignore(tmp_path):
    """--all must not sweep in gitignored secrets."""
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text(".env\nstate/\n")
    (repo / ".env").write_text("SECRET=1")
    (repo / "state").mkdir()
    (repo / "state" / "x.json").write_text("{}")

    git_sync.sync_commit("test: gitignore", root=repo)

    files = committed_files(repo)
    assert ".env" not in files
    assert not any(f.startswith("state/") for f in files)
    assert ".gitignore" in files  # the .gitignore itself is tracked


# ── branch coverage: deferral, divergence, recovery, resolver, usage ──────────

def test_sync_deferred_when_lock_held(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(git_sync, "repo_lock", _lock_held)
    assert git_sync.sync_commit("x", root=tmp_path) is False
    assert "deferred" in capsys.readouterr().out


def test_sync_resolves_root_when_none(tmp_path, monkeypatch):
    """root=None must fall back to resolve_root()."""
    called = {}
    monkeypatch.setattr(git_sync, "resolve_root", lambda: called.setdefault("r", tmp_path))
    monkeypatch.setattr(git_sync, "repo_lock", _lock_held)
    assert git_sync.sync_commit("x") is False
    assert called["r"] == tmp_path


def test_sync_push_deferred_on_divergence(tmp_path, monkeypatch, capsys):
    """push fails, pull --rebase fails → abort, return False."""
    monkeypatch.setattr(git_sync, "repo_lock", _lock_acquired)

    def fake_git(_root, *args, **_kw):
        return _FakeProc(1 if args[0] in ("push", "pull") else 0)

    monkeypatch.setattr(git_sync, "_git", fake_git)
    assert git_sync.sync_commit("x", root=tmp_path) is False
    assert "push deferred" in capsys.readouterr().out


def test_sync_push_recovers_after_rebase(tmp_path, monkeypatch):
    """push fails, pull --rebase succeeds, second push succeeds → True."""
    monkeypatch.setattr(git_sync, "repo_lock", _lock_acquired)
    pushes = {"n": 0}

    def fake_git(_root, *args, **_kw):
        if args[0] == "push":
            pushes["n"] += 1
            return _FakeProc(1 if pushes["n"] == 1 else 0)
        return _FakeProc(0)  # add, commit, pull all succeed

    monkeypatch.setattr(git_sync, "_git", fake_git)
    assert git_sync.sync_commit("x", root=tmp_path) is True
    assert pushes["n"] == 2


def test_main_usage_error_exits_1(capsys):
    with pytest.raises(SystemExit) as exc:
        git_sync.main([])
    assert exc.value.code == 1
    assert "Usage:" in capsys.readouterr().err


def test_main_passes_message_to_sync_commit(monkeypatch):
    seen = {}
    monkeypatch.setattr(git_sync, "sync_commit", lambda msg: seen.setdefault("msg", msg))
    git_sync.main(["a commit message"])
    assert seen["msg"] == "a commit message"
