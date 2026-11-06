"""
Tests for git_sync.sync_commit staging scope.

sync_commit sweeps new and modified files (respecting .gitignore) so a deferred write
still lands, and stages a deletion only for the briefs the caller names.
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
        self.stdout = ""


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


@pytest.mark.parametrize("flag", ["--help", "-h", "--path"])
def test_main_flag_as_message_never_commits(monkeypatch, capsys, flag):
    monkeypatch.setattr(git_sync, "sync_commit",
                        lambda *a, **k: pytest.fail("a flag must not become a commit message"))
    with pytest.raises(SystemExit) as exc:
        git_sync.main([flag])
    assert exc.value.code == (0 if flag in ("-h", "--help") else 1)
    assert "Usage:" in capsys.readouterr().err


def test_main_passes_message_to_sync_commit(monkeypatch):
    seen = {}
    monkeypatch.setattr(git_sync, "sync_commit",
                        lambda msg, ids: seen.update(msg=msg, ids=list(ids)))
    git_sync.main(["a commit message", "brief-a"])
    assert seen == {"msg": "a commit message", "ids": ["brief-a"]}


def _seed_brief(repo, status, brief_id):
    (repo / status / f"{brief_id}.md").write_text(brief_id)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"seed {brief_id}")


def test_sync_leaves_another_tools_deletion_unstaged(tmp_path):
    """A brief another tool removed but never committed stays tracked: no silent loss."""
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "theirs")
    (repo / "ready" / "theirs.md").unlink()
    (repo / "inbox" / "mine.md").write_text("new")

    git_sync.sync_commit("brief(mine): captured", root=repo, ids=["mine"])

    files = committed_files(repo)
    assert "inbox/mine.md" in files
    assert "ready/theirs.md" in files


def test_sync_commits_the_named_briefs_move(tmp_path):
    """The caller's own transition (old path removed, new path written) lands whole."""
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "mine")
    (repo / "ready" / "mine.md").rename(repo / "done" / "mine.md")

    git_sync.sync_commit("brief(mine): → done", root=repo, ids=["mine"])

    files = committed_files(repo)
    assert "done/mine.md" in files
    assert "ready/mine.md" not in files


def test_deferred_move_is_completed_by_the_next_writer(tmp_path):
    """A move left on disk by a deferred sync lands whole when another brief syncs."""
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "alpha")
    (repo / "ready" / "alpha.md").rename(repo / "done" / "alpha.md")
    (repo / "inbox" / "beta.md").write_text("new")

    git_sync.sync_commit("brief(beta): captured", root=repo, ids=["beta"])

    files = committed_files(repo)
    assert "done/alpha.md" in files
    assert "ready/alpha.md" not in files


def test_inbox_archive_move_lands_without_ids(tmp_path):
    """enrich's inbox/<f>.md -> inbox/.processed/<f>.md archive is a move, not a loss."""
    repo = make_repo(tmp_path)
    _seed_brief(repo, "inbox", "raw-thought")
    (repo / "inbox" / ".processed").mkdir()
    (repo / "inbox" / "raw-thought.md").rename(repo / "inbox" / ".processed" / "raw-thought.md")

    git_sync.sync_commit("enrich: 1 brief", root=repo)

    files = committed_files(repo)
    assert "inbox/.processed/raw-thought.md" in files
    assert "inbox/raw-thought.md" not in files


def test_unknown_id_warns_and_still_stages_the_valid_one(tmp_path, capsys):
    """One unmatched id must not make git drop the other named deletions."""
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "mine")
    (repo / "ready" / "mine.md").unlink()

    git_sync.sync_commit("brief(mine): removed", root=repo, ids=["ghost", "mine"])

    assert "ready/mine.md" not in committed_files(repo)
    assert "'ghost'" in capsys.readouterr().out


def test_cli_stages_a_named_deletion(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "mine")
    (repo / "ready" / "mine.md").unlink()
    monkeypatch.setattr(git_sync, "resolve_root", lambda: repo)

    git_sync.main(["brief(mine): removed", "mine"])

    assert "ready/mine.md" not in committed_files(repo)


def test_recovery_rebase_keeps_another_tools_unstaged_deletion(tmp_path):
    repo = make_repo(tmp_path)
    _seed_brief(repo, "inbox", "kept-by-other-tool")
    git(repo, "push", "-q", "origin", "main")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(tmp_path / "remote"), str(other)],
                   check=True)
    git(other, "config", "commit.gpgsign", "false")
    git(other, "config", "user.email", "o@o.o")
    git(other, "config", "user.name", "o")
    (other / "inbox" / "from-other-clone.md").write_text("x\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "other clone")
    git(other, "push", "-q", "origin", "main")
    (repo / "inbox" / "kept-by-other-tool.md").unlink()
    (repo / "inbox" / "new-brief.md").write_text("y\n")

    assert git_sync.sync_commit("add: new-brief", root=repo, ids=["new-brief"]) is True

    git(repo, "fetch", "-q", "origin")
    pushed = committed_files(repo, "origin/main")
    assert {"inbox/new-brief.md", "inbox/from-other-clone.md",
            "inbox/kept-by-other-tool.md"} <= pushed
    assert not (repo / "inbox" / "kept-by-other-tool.md").exists()


# ── a brief whose id already lives in another state dir is never re-created ───

def test_sync_refuses_to_recreate_a_done_brief_in_inbox(tmp_path, capsys):
    """A capture that writes inbox/<id>.md for an id already in done/ must not be committed."""
    repo = make_repo(tmp_path)
    (repo / "done" / "merge-queue.md").write_text("closed\n")
    git_sync.sync_commit("test: close", root=repo)
    (repo / "inbox" / "merge-queue.md").write_text("\nnote appended to a moved brief\n")
    (repo / "inbox" / "fresh.md").write_text("unrelated\n")

    git_sync.sync_commit("test: capture", root=repo)

    files = committed_files(repo)
    assert "inbox/merge-queue.md" not in files
    assert "done/merge-queue.md" in files
    assert "inbox/fresh.md" in files  # only the shadowing file is held back
    assert "merge-queue" in capsys.readouterr().out


def test_sync_refuses_id_that_is_done_only_on_origin(tmp_path):
    """A stale checkout cannot see a brief another session closed; the fetched origin can."""
    repo = make_repo(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(tmp_path / "remote"), str(other)],
                   check=True)
    git(other, "config", "user.email", "t@t.t")
    git(other, "config", "user.name", "t")
    (other / "done").mkdir(exist_ok=True)
    (other / "done" / "moved.md").write_text("closed\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "close")
    git(other, "push", "-q", "origin", "main")
    (repo / "inbox" / "moved.md").write_text("\nlate note\n")

    git_sync.sync_commit("test: stale capture", root=repo)

    assert "inbox/moved.md" not in committed_files(repo)
    assert "inbox/moved.md" not in committed_files(repo, "origin/main")
