"""
Tests for git_sync.sync_commit staging scope.

sync_commit stages only the briefs the caller names by id (moves and deletions included) and
the pathspecs it passes explicitly; any other dirty file stays unstaged.
"""

import contextlib
import os
import pathlib
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import _concurrency as C
import git_sync
import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


class _FakeProc:
    def __init__(self, rc):
        self.returncode = rc
        self.stdout = ""
        self.stderr = ""


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
    git(clone, "push", "-q", "-u", "origin", "main")
    return clone


def committed_files(repo, ref="HEAD"):
    return set(git(repo, "ls-tree", "-r", "--name-only", ref).stdout.splitlines())


def test_sync_commit_leaves_an_unrelated_dirty_file_unstaged(tmp_path):
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "theirs")
    (repo / "ready" / "theirs.md").write_text("an uncommitted note")
    (repo / "inbox" / "mine.md").write_text("new")
    (repo / "scratch.txt").write_text("untracked")

    git_sync.sync_commit("brief(mine): captured", root=repo, ids=["mine"])

    assert "inbox/mine.md" in committed_files(repo)
    assert git(repo, "show", "HEAD:ready/theirs.md").stdout == "theirs"
    assert "scratch.txt" not in committed_files(repo)
    assert git(repo, "status", "--porcelain").stdout.splitlines() == [
        " M ready/theirs.md",
        "?? scratch.txt",
    ]


def test_sync_commit_stages_explicit_paths(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "note.md").write_text("hi")
    (repo / "other.txt").write_text("not named")

    assert git_sync.sync_commit("test: docs", root=repo, paths=["docs"]) is True

    assert "docs/note.md" in committed_files(repo)
    assert "other.txt" not in committed_files(repo)
    assert git_sync.wait_for_push(repo)
    git(repo, "fetch", "-q", "origin")
    assert "docs/note.md" in committed_files(repo, "origin/main")


def test_sync_commit_paths_respect_gitignore(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text("SECRET=1")

    git_sync.sync_commit("test: gitignore", root=repo, paths=["."])

    assert ".env" not in committed_files(repo)
    assert ".gitignore" in committed_files(repo)


# ── branch coverage: deferral, divergence, recovery, resolver, usage ──────────


def test_sync_deferred_when_lock_held(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(git_sync, "repo_lock", _lock_held)
    git(tmp_path, "init", "-q")
    assert git_sync.sync_commit("x", root=tmp_path) is False
    assert "Re-run git_sync.py" in capsys.readouterr().out


def test_sync_resolves_root_when_none(tmp_path, monkeypatch):
    """root=None must fall back to resolve_root()."""
    called = {}
    monkeypatch.setattr(git_sync, "resolve_root", lambda: called.setdefault("r", tmp_path))
    monkeypatch.setattr(git_sync, "repo_lock", _lock_held)
    git(tmp_path, "init", "-q")
    assert git_sync.sync_commit("x") is False
    assert called["r"] == tmp_path


def _clone_other(tmp_path):
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(tmp_path / "remote"), str(other)], check=True
    )
    for key, value in (("commit.gpgsign", "false"), ("user.email", "o@o.o"), ("user.name", "o")):
        git(other, "config", key, value)
    return other


def _push_from_other(other, name, text):
    (other / name).write_text(text)
    git(other, "add", "-A")
    git(other, "commit", "-qm", f"other: {name}")
    git(other, "push", "-q", "origin", "main")


def test_sync_conflict_is_reported_and_local_commits_are_kept(tmp_path, capsys):
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "shared")
    git(repo, "push", "-q", "origin", "main")
    other = _clone_other(tmp_path)
    _push_from_other(other, "ready/shared.md", "remote edit")
    (repo / "ready" / "shared.md").write_text("local edit")
    (repo / "inbox" / "unrelated.md").write_text("dirty")

    assert (
        git_sync.sync_commit_and_push("brief(shared): edited", root=repo, ids=["shared"]) is False
    )

    out = capsys.readouterr().out
    assert "rebase conflict in ready/shared.md" in out
    assert "brief(shared): edited" in out and "other: ready/shared.md" in out
    assert git(repo, "log", "-1", "--format=%s").stdout.strip() == "brief(shared): edited"
    assert git(repo, "stash", "list").stdout == ""
    assert (repo / "inbox" / "unrelated.md").read_text() == "dirty"
    assert git(repo, "worktree", "list").stdout.count("\n") == 1


def test_sync_recovers_after_remote_moved_keeping_unrelated_edits(tmp_path):
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "theirs")
    git(repo, "push", "-q", "origin", "main")
    other = _clone_other(tmp_path)
    _push_from_other(other, "inbox/from-other.md", "x")
    (repo / "ready" / "theirs.md").write_text("an uncommitted note")
    (repo / "inbox" / "mine.md").write_text("y")

    assert git_sync.sync_commit_and_push("brief(mine): captured", root=repo, ids=["mine"]) is True

    assert git_sync.wait_for_push(repo)
    git(repo, "fetch", "-q", "origin")
    assert {"inbox/mine.md", "inbox/from-other.md"} <= committed_files(repo, "origin/main")
    assert git(repo, "rev-parse", "HEAD").stdout == git(repo, "rev-parse", "origin/main").stdout
    assert (repo / "ready" / "theirs.md").read_text() == "an uncommitted note"


def test_sync_pushed_but_checkout_not_advanced_when_an_edit_overlaps(tmp_path, capsys):
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "theirs")
    git(repo, "push", "-q", "origin", "main")
    other = _clone_other(tmp_path)
    _push_from_other(other, "ready/theirs.md", "remote edit")
    (repo / "ready" / "theirs.md").write_text("an uncommitted note")
    (repo / "inbox" / "mine.md").write_text("y")

    assert git_sync.sync_commit_and_push("brief(mine): captured", root=repo, ids=["mine"]) is True

    assert "checkout was not advanced" in capsys.readouterr().out
    assert (repo / "ready" / "theirs.md").read_text() == "an uncommitted note"
    assert git_sync.wait_for_push(repo)
    git(repo, "fetch", "-q", "origin")
    assert "inbox/mine.md" in committed_files(repo, "origin/main")


def test_sync_reports_when_origin_moves_again_during_the_rebase(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    other = _clone_other(tmp_path)
    _push_from_other(other, "inbox/from-other.md", "x")
    (repo / "inbox" / "mine.md").write_text("y")
    real = git_sync._git

    def losing_push(root, *args, **kw):
        if args[:1] == ("push",) and root != repo:
            _push_from_other(other, "inbox/another.md", "z")
        return real(root, *args, **kw)

    monkeypatch.setattr(git_sync, "_git", losing_push)

    assert git_sync.sync_commit_and_push("brief(mine): captured", root=repo, ids=["mine"]) is False
    assert "origin moved again while rebasing" in capsys.readouterr().out


def test_main_usage_error_exits_1(capsys):
    with pytest.raises(SystemExit) as exc:
        git_sync.main([])
    assert exc.value.code == 1
    assert "Usage:" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--help", "-h", "--path"])
def test_main_flag_as_message_never_commits(monkeypatch, capsys, flag):
    monkeypatch.setattr(
        git_sync,
        "sync_commit",
        lambda *a, **k: pytest.fail("a flag must not become a commit message"),
    )
    with pytest.raises(SystemExit) as exc:
        git_sync.main([flag])
    assert exc.value.code == (0 if flag in ("-h", "--help") else 1)
    assert "Usage:" in capsys.readouterr().err


def test_main_passes_message_to_sync_commit(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        git_sync, "sync_commit", lambda msg, ids: seen.update(msg=msg, ids=list(ids))
    )
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


def test_a_move_left_by_another_writer_is_not_swept_into_this_commit(tmp_path):
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "alpha")
    (repo / "ready" / "alpha.md").rename(repo / "done" / "alpha.md")
    (repo / "inbox" / "beta.md").write_text("new")

    git_sync.sync_commit("brief(beta): captured", root=repo, ids=["beta"])

    files = committed_files(repo)
    assert "ready/alpha.md" in files
    assert "done/alpha.md" not in files


def test_inbox_archive_move_lands_when_its_id_is_named(tmp_path):
    """enrich's inbox/<f>.md -> inbox/.processed/<f>.md archive is a move, not a loss."""
    repo = make_repo(tmp_path)
    _seed_brief(repo, "inbox", "raw-thought")
    (repo / "inbox" / ".processed").mkdir()
    (repo / "inbox" / "raw-thought.md").rename(repo / "inbox" / ".processed" / "raw-thought.md")

    git_sync.sync_commit("enrich: 1 brief", root=repo, ids=["raw-thought"])

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


# ── a brief whose id already lives in another state dir is never re-created ───


def test_sync_refuses_to_recreate_a_done_brief_in_inbox(tmp_path, capsys):
    """A capture that writes inbox/<id>.md for an id already in done/ must not be committed."""
    repo = make_repo(tmp_path)
    (repo / "done" / "merge-queue.md").write_text("closed\n")
    git_sync.sync_commit("test: close", root=repo, ids=["merge-queue"])
    (repo / "inbox" / "merge-queue.md").write_text("\nnote appended to a moved brief\n")
    (repo / "inbox" / "fresh.md").write_text("unrelated\n")

    git_sync.sync_commit("test: capture", root=repo, ids=["merge-queue", "fresh"])

    files = committed_files(repo)
    assert "inbox/merge-queue.md" not in files
    assert "done/merge-queue.md" in files
    assert "inbox/fresh.md" in files  # only the shadowing file is held back
    assert "merge-queue" in capsys.readouterr().out


def test_sweep_refuses_id_that_is_done_only_on_origin(tmp_path):
    """A stale checkout cannot see a brief another session closed; the sweep's fetch can."""
    repo = make_repo(tmp_path)
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(tmp_path / "remote"), str(other)], check=True
    )
    git(other, "config", "user.email", "t@t.t")
    git(other, "config", "user.name", "t")
    (other / "done").mkdir(exist_ok=True)
    (other / "done" / "moved.md").write_text("closed\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "close")
    git(other, "push", "-q", "origin", "main")
    (repo / "inbox" / "moved.md").write_text("\nlate note\n")

    git_sync.sweep(repo)
    assert git_sync.wait_for_push(repo)

    assert "inbox/moved.md" not in committed_files(repo)
    assert "inbox/moved.md" not in committed_files(repo, "origin/main")


def test_sync_refusal_holds_when_the_id_is_named_explicitly(tmp_path):
    """The documented `git_sync.py <msg> <id>` form must not stage the refused file back in."""
    repo = make_repo(tmp_path)
    (repo / "done" / "closed.md").write_text("closed\n")
    git_sync.sync_commit("test: close", root=repo, ids=["closed"])
    (repo / "inbox" / "closed.md").write_text("\nlate note\n")

    git_sync.sync_commit("test: named capture", root=repo, ids=["closed"])
    assert git_sync.wait_for_push(repo)

    assert "inbox/closed.md" not in committed_files(repo)
    assert "inbox/closed.md" not in committed_files(repo, "origin/main")


def test_deferred_second_move_still_completes_after_an_unpushed_first(tmp_path):
    """A move committed locally but not yet pushed leaves a stale origin path; it is no rival."""
    repo = make_repo(tmp_path)
    (repo / "ready" / "moving.md").write_text("b\n")
    git_sync.sync_commit("test: file", root=repo, ids=["moving"])
    git(repo, "mv", "ready/moving.md", "in-progress/moving.md")
    git(repo, "commit", "-qm", "local move, push deferred")
    (repo / "in-progress" / "moving.md").rename(repo / "done" / "moving.md")
    (repo / "inbox" / "other.md").write_text("unrelated\n")

    git_sync.sync_commit("test: unrelated writer", root=repo, ids=["moving", "other"])

    files = committed_files(repo)
    assert "done/moving.md" in files and "in-progress/moving.md" not in files


# ── sweep: the same helper over the whole tree ────────────────────────────────


def test_sweep_commits_and_pushes_every_uncommitted_change(tmp_path):
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "edited")
    (repo / "ready" / "edited.md").write_text("hand edit")
    (repo / "inbox" / "new.md").write_text("new")

    assert git_sync.sweep(repo) is True

    assert git(repo, "status", "--porcelain").stdout == ""
    assert git_sync.wait_for_push(repo)
    git(repo, "fetch", "-q", "origin")
    assert git(repo, "rev-parse", "HEAD").stdout == git(repo, "rev-parse", "origin/main").stdout
    assert "inbox/new.md" in committed_files(repo, "origin/main")


def test_sweep_keeps_a_credential_bearing_file_local(tmp_path, capsys):
    repo = make_repo(tmp_path)
    (repo / "inbox" / "leaky.md").write_text("token ghp_" + "a" * 30)
    (repo / "inbox" / "fine.md").write_text("ok")

    git_sync.sweep(repo)

    assert git_sync.wait_for_push(repo)
    git(repo, "fetch", "-q", "origin")
    assert "inbox/fine.md" in committed_files(repo, "origin/main")
    assert "inbox/leaky.md" not in committed_files(repo, "origin/main")
    out = capsys.readouterr().out
    assert "1 held back" in out
    assert "leaky" not in out


def test_sweep_unstages_a_credential_bearing_file_someone_already_staged(tmp_path):
    repo = make_repo(tmp_path)
    leaky = repo / "inbox" / "staged-leaky.md"
    leaky.write_text("token ghp_" + "b" * 30)
    git(repo, "add", "inbox/staged-leaky.md")
    (repo / "inbox" / "fine.md").write_text("ok")

    git_sync.sweep(repo)

    assert git_sync.wait_for_push(repo)
    git(repo, "fetch", "-q", "origin")
    assert "inbox/fine.md" in committed_files(repo, "origin/main")
    assert "inbox/staged-leaky.md" not in committed_files(repo, "origin/main")
    assert leaky.is_file()


def test_a_named_sync_never_commits_a_staged_credential(tmp_path):
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "named")
    (repo / "inbox" / "staged-leaky.md").write_text("AKIA" + "A" * 16)
    git(repo, "add", "inbox/staged-leaky.md")

    git_sync.sync_commit("brief(named): x", repo, ids=["named"])

    assert git_sync.wait_for_push(repo)
    git(repo, "fetch", "-q", "origin")
    assert "inbox/staged-leaky.md" not in committed_files(repo, "origin/main")


def test_sweep_commits_a_deleted_brief_without_scanning_it(tmp_path):
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "gone")
    (repo / "ready" / "gone.md").unlink()

    assert git_sync.sweep(repo) is True

    assert git_sync.wait_for_push(repo)
    git(repo, "fetch", "-q", "origin")
    assert "ready/gone.md" not in committed_files(repo, "origin/main")


def test_a_queue_inside_another_repo_is_never_synced(tmp_path):
    repo = make_repo(tmp_path)
    project_queue = repo / "project" / ".tasks"
    project_queue.mkdir(parents=True)
    (project_queue / "x.md").write_text("x")

    assert git_sync.sync_commit("x", root=project_queue, paths=["."]) is False
    assert "project/.tasks/x.md" not in committed_files(repo)


def test_cli_sweep_flag_syncs_the_named_root(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "inbox" / "cli.md").write_text("cli")

    git_sync.main(["--sweep", str(repo)])

    assert git_sync.wait_for_push(repo)
    git(repo, "fetch", "-q", "origin")
    assert "inbox/cli.md" in committed_files(repo, "origin/main")


# ── coalesced push: one pusher per clone, writes during a push still land ────


def _fake_git_on_path(tmp_path, monkeypatch):
    """A `git` shim on PATH that logs each `push` (and stalls it while SLOW exists) before running git."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    log, slow, started = shim_dir / "pushes.log", shim_dir / "SLOW", shim_dir / "STARTED"
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'sub="$1"; [ "$1" = "-C" ] && sub="$3"\n'
        f'if [ "$sub" = fetch ]; then echo fetch >> "{log}.fetch"; fi\n'
        f'if [ "$sub" = push ]; then echo push >> "{log}"; '
        f'if [ -f "{slow}" ]; then touch "{started}"; sleep 1; fi; fi\n'
        f'exec "{shutil.which("git")}" "$@"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    return log, slow, started


def _push_count(log):
    return len(log.read_text().splitlines()) if log.exists() else 0


def _remote_head(repo):
    return git(tmp_remote(repo), "rev-parse", "main").stdout


def tmp_remote(repo):
    return repo.parent / "remote"


def test_twenty_concurrent_writes_commit_twenty_times_and_push_at_most_twice(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    log, _, _ = _fake_git_on_path(tmp_path, monkeypatch)
    base = int(git(repo, "rev-list", "--count", "HEAD").stdout)
    ids = [f"burst-{i}" for i in range(20)]
    for brief_id in ids:
        (repo / "inbox" / f"{brief_id}.md").write_text(brief_id)
    start = threading.Barrier(len(ids))

    def write(brief_id):
        start.wait()
        return git_sync.sync_commit(f"brief({brief_id}): captured", root=repo, ids=[brief_id])

    with ThreadPoolExecutor(max_workers=len(ids)) as ex:
        assert all(ex.map(write, ids))
    assert git_sync.wait_for_push(repo)

    assert int(git(repo, "rev-list", "--count", "HEAD").stdout) == base + 20
    assert _push_count(log) <= 2
    assert _push_count(log.with_name("pushes.log.fetch")) == 0
    assert _remote_head(repo) == git(repo, "rev-parse", "HEAD").stdout


def test_a_write_during_an_in_flight_push_is_still_pushed(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    log, slow, started = _fake_git_on_path(tmp_path, monkeypatch)
    slow.touch()
    (repo / "inbox" / "first.md").write_text("1")
    assert git_sync.sync_commit("brief(first): captured", root=repo, ids=["first"]) is True
    deadline = time.monotonic() + 10
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert started.exists()
    slow.unlink()
    (repo / "inbox" / "second.md").write_text("2")

    assert git_sync.sync_commit("brief(second): captured", root=repo, ids=["second"]) is True
    assert git_sync.wait_for_push(repo)

    assert _remote_head(repo) == git(repo, "rev-parse", "HEAD").stdout
    assert "inbox/second.md" in committed_files(repo, _remote_head(repo).strip())
    assert _push_count(log) == 2


def test_a_running_pusher_means_a_write_spawns_no_process(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    held = C.try_lock(C.clone_state_path(repo, git_sync.PUSH_LOCK))
    monkeypatch.setattr(
        git_sync, "_spawn_pusher", lambda *a: pytest.fail("spawned a second pusher")
    )
    (repo / "inbox" / "mine.md").write_text("x")

    assert git_sync.sync_commit("brief(mine): captured", root=repo, ids=["mine"]) is True

    assert git_sync._is_dirty(repo)
    assert git_sync.wait_for_push(repo, timeout=0.2) is False
    os.close(held)


def test_pusher_retakes_the_lock_for_a_write_that_landed_as_it_released(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    drains = []

    def drain(root):
        drains.append(root)
        if len(drains) == 1:
            git_sync._mark_dirty(root)  # lands after the drain loop's last check
        else:
            git_sync._take_dirty(root)
        return True

    monkeypatch.setattr(git_sync, "_drain", drain)
    git_sync.main(
        [
            "--push-pending",
            str(repo),
            "--lock-fd",
            str(C.try_lock(C.clone_state_path(repo, git_sync.PUSH_LOCK))),
        ]
    )

    assert drains == [repo, repo]
    assert git_sync.wait_for_push(repo, timeout=1)


def test_pusher_leaves_a_late_write_to_the_pusher_that_took_the_lock(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    rival = []

    def drain(root):
        git_sync._mark_dirty(root)
        return True

    def release(fd):
        os.close(fd)
        rival.append(C.try_lock(C.clone_state_path(repo, git_sync.PUSH_LOCK)))

    monkeypatch.setattr(git_sync, "_drain", drain)
    monkeypatch.setattr(git_sync, "_release", release)
    git_sync._run_pusher(repo, C.try_lock(C.clone_state_path(repo, git_sync.PUSH_LOCK)))

    assert rival[0] is not None and git_sync._is_dirty(repo)
    os.close(rival[0])


def test_push_now_hands_a_write_that_raced_its_release_to_a_new_pusher(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    spawned = []
    monkeypatch.setattr(git_sync, "_drain", lambda root: git_sync._mark_dirty(root) or True)
    monkeypatch.setattr(git_sync, "_spawn_pusher", lambda root, fd: spawned.append(os.close(fd)))

    assert git_sync.push_now(repo) is True
    assert len(spawned) == 1


def test_push_now_defers_when_a_pusher_never_finishes(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    held = C.try_lock(C.clone_state_path(repo, git_sync.PUSH_LOCK))
    monkeypatch.setattr(git_sync, "PUSH_WAIT", 0.1)

    assert git_sync.push_now(repo) is False
    assert "another pusher still running" in capsys.readouterr().out
    os.close(held)


def test_a_rejected_push_defers_when_the_queue_lock_is_held(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    _push_from_other(_clone_other(tmp_path), "inbox/from-other.md", "x")
    (repo / "inbox" / "mine.md").write_text("y")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "local only")
    monkeypatch.setattr(git_sync, "repo_lock", _lock_held)

    assert git_sync._push(repo) is False
    assert "next write or sweep pushes" in capsys.readouterr().out


def test_detached_pusher_output_goes_to_the_push_log(tmp_path):
    repo = make_repo(tmp_path)
    _seed_brief(repo, "ready", "shared")
    git(repo, "push", "-q", "origin", "main")
    _push_from_other(_clone_other(tmp_path), "ready/shared.md", "remote edit")
    (repo / "ready" / "shared.md").write_text("local edit")

    assert git_sync.sync_commit("brief(shared): edited", root=repo, ids=["shared"]) is True
    assert git_sync.wait_for_push(repo)

    assert (
        "rebase conflict in ready/shared.md"
        in C.clone_state_path(repo, git_sync.PUSH_LOG).read_text()
    )


def test_the_next_write_shows_what_the_last_background_push_reported(tmp_path, capsys):
    repo = make_repo(tmp_path)
    C.clone_state_path(repo, git_sync.PUSH_LOG).write_text(
        "  (push deferred: rebase conflict in ready/x.md)"
    )
    (repo / "inbox" / "mine.md").write_text("y")

    assert git_sync.sync_commit_and_push("brief(mine): captured", root=repo, ids=["mine"]) is True

    assert "previous background push reported:" in capsys.readouterr().out
    assert C.clone_state_path(repo, git_sync.PUSH_LOG).read_text() == ""


def test_a_stale_fetch_is_refreshed_and_a_fresh_one_reused(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    log, _, _ = _fake_git_on_path(tmp_path, monkeypatch)
    fetches = log.with_name("pushes.log.fetch")

    git_sync._fetch_if_stale(repo)
    git_sync._fetch_if_stale(repo)
    assert _push_count(fetches) == 1

    stale = time.time() - git_sync.FETCH_MAX_AGE - 1
    os.utime(repo / ".git" / "FETCH_HEAD", (stale, stale))
    git_sync._fetch_if_stale(repo)
    assert _push_count(fetches) == 2


def test_a_write_fetches_nothing_and_a_sweep_fetches_once_a_minute(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    log, _, _ = _fake_git_on_path(tmp_path, monkeypatch)
    fetches = log.with_name("pushes.log.fetch")
    (repo / "inbox" / "quiet.md").write_text("q")

    assert git_sync.sync_commit("brief(quiet): captured", root=repo, ids=["quiet"]) is True
    assert git_sync.wait_for_push(repo)
    assert _push_count(fetches) == 0

    assert git_sync.sweep(repo) is True
    assert git_sync.sweep(repo) is True
    assert _push_count(fetches) == 1


def test_a_sweep_of_a_project_queue_fetches_nothing(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    log, _, _ = _fake_git_on_path(tmp_path, monkeypatch)
    (repo / "project").mkdir()

    assert git_sync.sweep(repo / "project") is False
    assert _push_count(log.with_name("pushes.log.fetch")) == 0


def test_a_write_from_a_worktree_commits_no_pusher_state(tmp_path):
    repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    (wt / "inbox" / "mine.md").write_text("x")

    assert git_sync.sync_commit("brief(mine): captured", root=wt, ids=["mine"]) is True
    assert git_sync.wait_for_push(wt)
    assert git_sync.sweep(wt) is True
    assert git_sync.wait_for_push(wt)

    assert not [p for p in committed_files(wt) if p.startswith(".tasks-")]
    assert C.clone_state_path(wt, git_sync.PUSH_LOCK) == C.clone_state_path(
        repo, git_sync.PUSH_LOCK
    )


def test_fetch_freshness_reads_a_worktree_fetch_head(tmp_path):
    repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "--detach", str(wt))
    git(wt, "fetch", "-q", "origin", "main")

    assert git_sync._fetch_head(wt) == pathlib.Path(
        git(wt, "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD").stdout.strip()
    )
    assert git_sync._fetch_head(wt).exists()


def test_a_pusher_that_cannot_start_leaves_the_commit_for_the_sweep(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)

    monkeypatch.setattr(git_sync.sys, "executable", str(tmp_path / "no-such-python"))
    (repo / "inbox" / "mine.md").write_text("x")

    assert git_sync.sync_commit("brief(mine): captured", root=repo, ids=["mine"]) is True

    assert "inbox/mine.md" in committed_files(repo, "HEAD")
    assert git_sync._is_dirty(repo)
    assert "could not start the background pusher" in capsys.readouterr().out
    held = C.try_lock(C.clone_state_path(repo, git_sync.PUSH_LOCK))
    assert held is not None
    os.close(held)


def test_an_idle_sweep_starts_no_push(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    log, _, _ = _fake_git_on_path(tmp_path, monkeypatch)

    assert git_sync.sweep(repo) is True
    git_sync.wait_for_push(repo, timeout=2)

    assert not git_sync._is_dirty(repo)
    assert _push_count(log) == 0


def test_a_sweep_pushes_commits_a_failed_push_left_behind(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "inbox" / "stranded.md").write_text("x")
    git(repo, "add", "inbox/stranded.md")
    # Committed and never pushed, with no dirty mark: what a failed push leaves behind.
    git(repo, "commit", "-qm", "brief(stranded): captured")

    assert git_sync.sweep(repo) is True
    assert git_sync.wait_for_push(repo)

    assert _remote_head(repo) == git(repo, "rev-parse", "HEAD").stdout


def test_a_clone_without_an_origin_ref_is_treated_as_ahead(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")

    assert git_sync._ahead_of_origin(repo) is True


def test_an_undecodable_push_log_does_not_fail_the_write(tmp_path, capsys):
    repo = make_repo(tmp_path)
    C.clone_state_path(repo, git_sync.PUSH_LOG).write_bytes(b"rejected \xff\xfe\n")

    git_sync._report_last_push(repo)

    assert "rejected" in capsys.readouterr().out


# ── a rebase conflict confined to execution-log appends resolves as their union ──

_BRIEF = "---\nid: b\nstatus: {status}\n---\n\n## Goal\n\n{goal}\n\n## Execution log\n\n{log}"


def _brief(status="inbox", goal="g", log="2026-11-09: captured.\n"):
    return _BRIEF.format(status=status, goal=goal, log=log)


def _clone_of_origin(tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(tmp_path / "remote"), str(other)],
                   check=True)
    for key, value in (("commit.gpgsign", "false"), ("user.email", "o@o.o"), ("user.name", "o")):
        git(other, "config", key, value)
    return other


def _seed_pushed(repo, rel, text):
    (repo / rel).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    git(repo, "push", "-q", "origin", "main")


def _origin_appends(other, rel, line):
    git(other, "pull", "-q", "origin", "main")
    with (other / rel).open("a") as fh:
        fh.write(line)
    git(other, "commit", "-qam", "log line")
    git(other, "push", "-q", "origin", "main")


def _move_to_done_with_log(repo, line):
    git(repo, "mv", "inbox/b.md", "done/b.md")
    (repo / "done" / "b.md").write_text(
        _brief(status="done", log="2026-11-09: captured.\n" + line))


def test_sync_unions_log_lines_when_origin_logged_a_brief_moved_locally(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GIT_EDITOR", "false")  # an interactive editor must never open
    repo = make_repo(tmp_path)
    _seed_pushed(repo, "inbox/b.md", _brief())
    _origin_appends(_clone_of_origin(tmp_path), "inbox/b.md", "2026-11-09: logged on origin.\n")
    _move_to_done_with_log(repo, "2026-11-09: closed locally.\n")

    assert git_sync.sync_commit_and_push("brief(b): done", root=repo, ids=["b"]) is True

    git(repo, "fetch", "-q", "origin")
    merged = git(repo, "show", "origin/main:done/b.md").stdout
    assert "status: done" in merged
    assert merged.endswith("2026-11-09: captured.\n2026-11-09: logged on origin.\n"
                           "2026-11-09: closed locally.\n")
    assert "inbox/b.md" not in committed_files(repo, "origin/main")
    assert git(repo, "status", "-sb").stdout.splitlines()[0] == "## main...origin/main"
    assert capsys.readouterr().err == ""


def test_sync_unions_across_several_replayed_commits(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    _seed_pushed(repo, "inbox/b.md", _brief())
    _origin_appends(_clone_of_origin(tmp_path), "inbox/b.md", "2026-11-11: logged on origin.\n")
    _move_to_done_with_log(repo, "2026-11-09: closed locally.\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "move and log")
    with (repo / "done" / "b.md").open("a") as fh:
        fh.write("2026-11-10: later local line.\n")
    resolved = []
    real = git_sync._resolve_log_conflict

    def spy(root, path):
        resolved.append(path)
        return real(root, path)

    monkeypatch.setattr(git_sync, "_resolve_log_conflict", spy)

    assert git_sync.sync_commit_and_push("brief(b): log", root=repo, paths=["done"]) is True

    git(repo, "fetch", "-q", "origin")
    merged = git(repo, "show", "origin/main:done/b.md").stdout
    assert merged.endswith("2026-11-09: closed locally.\n2026-11-10: later local line.\n"
                           "2026-11-11: logged on origin.\n")
    assert resolved == ["done/b.md", "done/b.md"]


def test_sync_defers_loudly_when_a_conflict_is_outside_the_log(tmp_path, capsys):
    repo = make_repo(tmp_path)
    _seed_pushed(repo, "inbox/b.md", _brief())
    other = _clone_of_origin(tmp_path)
    git(other, "pull", "-q", "origin", "main")
    (other / "inbox" / "b.md").write_text(_brief(goal="origin rewrote the goal"))
    git(other, "commit", "-qam", "edit goal")
    git(other, "push", "-q", "origin", "main")
    (repo / "inbox" / "b.md").write_text(_brief(goal="local rewrote the goal"))

    assert git_sync.sync_commit_and_push("brief(b): edit", root=repo, ids=["b"]) is False

    captured = capsys.readouterr()
    assert "inbox/b.md" in captured.err and "WARNING" in captured.err
    assert "push deferred" in captured.out
    assert git(repo, "worktree", "list").stdout.count("\n") == 1
    assert "local rewrote the goal" in git(repo, "show", "HEAD:inbox/b.md").stdout


def _scripted_rebase(monkeypatch, *, returncodes, in_progress=(True,), conflicted=(), resolves=True):
    """Drive `_rebase_keeping_log_union` with a scripted git; returns the git calls it made."""
    calls = []
    codes = iter(returncodes)

    def fake_git(_root, *args, **_kw):
        calls.append(args)
        return _FakeProc(next(codes))

    progress = iter(in_progress)
    monkeypatch.setattr(git_sync, "_git", fake_git)
    monkeypatch.setattr(git_sync, "_rebase_in_progress", lambda _r: next(progress))
    monkeypatch.setattr(git_sync, "_conflicted", lambda _r: list(conflicted))
    monkeypatch.setattr(git_sync, "_resolve_log_conflict", lambda _r, _p: resolves)
    return calls


def test_a_clean_rebase_reports_nothing_stuck(tmp_path, monkeypatch):
    _scripted_rebase(monkeypatch, returncodes=[0])

    assert git_sync._rebase_keeping_log_union(tmp_path) is None


def test_a_rebase_that_fails_without_stopping_names_nothing(tmp_path, monkeypatch):
    _scripted_rebase(monkeypatch, returncodes=[1], in_progress=[False])

    assert git_sync._rebase_keeping_log_union(tmp_path).paths == []


def test_a_rebase_that_stops_without_a_conflicting_file_names_nothing(tmp_path, monkeypatch):
    _scripted_rebase(monkeypatch, returncodes=[1], conflicted=[])

    assert git_sync._rebase_keeping_log_union(tmp_path).paths == []


def test_a_conflict_outside_the_log_is_returned_for_deferral(tmp_path, monkeypatch):
    calls = _scripted_rebase(monkeypatch, returncodes=[1], conflicted=["done/b.md"], resolves=False)

    assert git_sync._rebase_keeping_log_union(tmp_path).paths == ["done/b.md"]
    assert not any("--continue" in call for call in calls)


def test_resolved_conflicts_continue_the_rebase_until_it_finishes(tmp_path, monkeypatch):
    calls = _scripted_rebase(monkeypatch, returncodes=[1, 0], conflicted=["done/b.md"])

    assert git_sync._rebase_keeping_log_union(tmp_path) is None
    assert calls[-1][-2:] == ("rebase", "--continue")


def test_a_rebase_that_cannot_continue_is_deferred(tmp_path, monkeypatch):
    _scripted_rebase(monkeypatch, returncodes=[1, 1], in_progress=[True, False],
                     conflicted=["done/b.md"])

    assert git_sync._rebase_keeping_log_union(tmp_path).paths == []


def test_integrate_warns_when_git_names_no_conflicting_file(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(git_sync, "_rebase_keeping_log_union", lambda _wt: git_sync._Stuck([], "fatal: no such ref"))

    assert git_sync._integrate_remote(repo) is False

    captured = capsys.readouterr()
    assert "rebase failed: fatal: no such ref" in captured.err and "WARNING" in captured.err
    assert "push deferred" in captured.out


def test_rebase_in_progress_tracks_the_rebase_directory(tmp_path):
    repo = make_repo(tmp_path)
    assert git_sync._rebase_in_progress(repo) is False
    (repo / ".git" / "rebase-merge").mkdir()
    assert git_sync._rebase_in_progress(repo) is True


@pytest.mark.parametrize("path", ["scripts/tool.py", "elsewhere/b.md"])
def test_only_task_dir_briefs_are_auto_resolved(tmp_path, path):
    assert git_sync._resolve_log_conflict(tmp_path, path) is False


def test_unresolvable_when_a_merge_stage_is_missing(tmp_path):
    repo = make_repo(tmp_path)
    assert git_sync._resolve_log_conflict(repo, "inbox/never-conflicted.md") is False


def test_a_failed_git_add_leaves_the_conflict_unresolved(tmp_path, monkeypatch):
    text = _brief()
    (tmp_path / "done").mkdir()
    monkeypatch.setattr(git_sync, "_stage_text", lambda _r, _s, _p: text)
    monkeypatch.setattr(git_sync, "_toplevel", lambda _r: tmp_path)
    monkeypatch.setattr(git_sync, "_git", lambda _r, *a, **_k: _FakeProc(1))

    assert git_sync._resolve_log_conflict(tmp_path, "done/b.md") is False


def test_a_missing_repo_top_leaves_the_conflict_unresolved(tmp_path, monkeypatch):
    text = _brief()
    monkeypatch.setattr(git_sync, "_stage_text", lambda _r, _s, _p: text)
    monkeypatch.setattr(git_sync, "_toplevel", lambda _r: None)

    assert git_sync._resolve_log_conflict(tmp_path, "done/b.md") is False


def test_conflicted_lists_unmerged_paths_split_on_nul(tmp_path, monkeypatch):
    listing = _FakeProc(0)
    listing.stdout = "done/b.md\0inbox/café.md\0"
    monkeypatch.setattr(git_sync, "_git", lambda _r, *a, **_k: listing)

    assert git_sync._conflicted(tmp_path) == ["done/b.md", "inbox/café.md"]


def test_a_stuck_rebase_without_files_or_error_text_still_says_why():
    assert git_sync._Stuck([], "").describe() == "rebase failed: git reported no conflicting file"


def test_a_stage_that_is_not_utf8_leaves_the_conflict_unresolved(tmp_path, monkeypatch):
    def undecodable(*_a, **_k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(git_sync, "_git", undecodable)

    assert git_sync._stage_text(tmp_path, 2, "done/b.md") is None


# ── 2026-11-10 incident (work-tasks 88d805f): adjacent appends across a local rename ──


def _other_rewrites(other, rel, old, new):
    git(other, "pull", "-q", "origin", "main")
    path = other / rel
    path.write_text(path.read_text().replace(old, new))
    git(other, "commit", "-qam", "other edit")
    git(other, "push", "-q", "origin", "main")


def test_adjacent_log_appends_and_a_local_rename_keep_both_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_EDITOR", "false")
    repo = make_repo(tmp_path)
    _seed_pushed(repo, "inbox/b.md", _brief(log="2026-11-10: captured.\n"))
    _other_rewrites(
        _clone_of_origin(tmp_path),
        "inbox/b.md",
        "captured.\n",
        "captured.\n2026-11-10 (remote): pushed line.\n",
    )
    (repo / "blocked").mkdir()
    git(repo, "mv", "inbox/b.md", "blocked/b.md")
    moved = repo / "blocked" / "b.md"
    moved.write_text(
        moved.read_text().replace("status: inbox", "status: blocked")
        + "2026-11-10 (local): local line.\n"
    )

    assert git_sync.sync_commit_and_push("brief(b): blocked", root=repo, ids=["b"]) is True

    git(repo, "fetch", "-q", "origin")
    assert git(repo, "rev-parse", "HEAD").stdout == git(repo, "rev-parse", "origin/main").stdout
    text = git(repo, "show", "origin/main:blocked/b.md").stdout
    assert "(remote): pushed line." in text and "(local): local line." in text
    assert "status: blocked" in text
    assert git(repo, "status", "--porcelain").stdout == ""


def test_a_frontmatter_conflict_still_aborts_and_defers(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_EDITOR", "false")
    repo = make_repo(tmp_path)
    _seed_pushed(repo, "inbox/b.md", _brief())
    _other_rewrites(_clone_of_origin(tmp_path), "inbox/b.md", "status: inbox", "status: ready")
    brief = repo / "inbox" / "b.md"
    brief.write_text(brief.read_text().replace("status: inbox", "status: parked"))

    assert git_sync.sync_commit_and_push("brief(b): parked", root=repo, ids=["b"]) is False

    assert not git_sync._rebase_in_progress(repo)
    assert "status: parked" in brief.read_text()
    git(repo, "fetch", "-q", "origin")
    assert git(repo, "rev-parse", "HEAD").stdout != git(repo, "rev-parse", "origin/main").stdout
