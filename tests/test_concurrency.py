"""
Concurrency tests for the task queue — the guardrail for "multiple Claude sessions
adding tasks at once corrupt/lose each other's writes".

Reproduces the original failure (concurrent `add_task --commit` colliding on
.git/index.lock and silently dropping briefs) and proves the fix: every brief lands
exactly once, committed and pushed, with no wedged lock and no overwritten files.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import _concurrency as C
import git_sync
import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


# ── helpers ───────────────────────────────────────────────────────────────────


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, **kw)


def briefs_on(repo, ref):
    out = git(repo, "ls-tree", "-r", "--name-only", ref).stdout.splitlines()
    return sorted(f for f in out if f.startswith("inbox/") and f.endswith(".md"))


def _env(repo):
    """Point the scripts at this queue the way a real install does — via the env var."""
    return {**os.environ, "CLAUDE_TASKS_DIR": str(repo)}


def run_add(repo, title, *extra):
    return subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "add_task.py"),
            "--title",
            title,
            "--source",
            "claude",
            "--commit",
            *extra,
        ],
        capture_output=True,
        text=True,
        env=_env(repo),
    )


@pytest.fixture
def queue(tmp_path):
    """A throwaway clone wired like ~/tasks: task dirs, scripts/, and a bare remote."""
    remote, clone = tmp_path / "remote", tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    git(clone, "config", "user.email", "t@t.t")
    git(clone, "config", "user.name", "t")
    git(clone, "config", "commit.gpgsign", "false")
    git(clone, "symbolic-ref", "HEAD", "refs/heads/main")
    for d in C.TASK_DIRS:
        (clone / d).mkdir()
    (clone / "inbox" / ".keep").write_text("")  # something to seed the first commit
    (clone / "tasks.toml").write_text('name = "test-queue"\n')  # makes it a real queue
    shutil.copytree(SCRIPTS, clone / "scripts")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "seed")
    git(clone, "push", "-q", "origin", "main")
    return clone


# ── the headline race ─────────────────────────────────────────────────────────


def test_concurrent_adds_all_land_committed_and_pushed(queue):
    n = 8
    titles = [f"task number {i}" for i in range(n)]
    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(lambda t: run_add(queue, t), titles))

    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    # Every brief committed locally AND present on the remote — none silently dropped.
    assert len(briefs_on(queue, "HEAD")) == n
    assert git_sync.wait_for_push(queue)
    git(queue, "fetch", "-q", "origin")
    assert len(briefs_on(queue, "origin/main")) == n
    # The repo is not wedged: no orphaned index.lock left behind.
    assert not (queue / ".git" / "index.lock").exists()


def test_same_title_concurrent_adds_do_not_overwrite(queue):
    """Two sessions capturing the same title must produce two distinct briefs."""
    n = 6
    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(
            ex.map(
                lambda _: run_add(queue, "identical title here", "--on-duplicate", "file"), range(n)
            )
        )
    assert all(r.returncode == 0 for r in results)
    files = list((queue / "inbox").glob("*.md"))
    assert len(files) == n  # no lost write — distinct -2, -3, … suffixes
    assert len(briefs_on(queue, "HEAD")) == n


def test_sync_does_not_sweep_an_orphan_into_another_briefs_commit(queue):
    """A brief written but never committed stays on disk; only the caller's brief is committed."""
    orphan = queue / "inbox" / "orphan-from-crash.md"
    orphan.write_text("---\nid: orphan-from-crash\n---\n")  # on disk, never committed
    assert run_add(queue, "fresh task").returncode == 0
    committed = briefs_on(queue, "HEAD")
    assert "inbox/orphan-from-crash.md" not in committed
    assert orphan.exists()
    assert any(f.startswith("inbox/fresh-task") for f in committed)


# ── unit-level guarantees the race test relies on ─────────────────────────────


def test_repo_lock_is_mutually_exclusive(tmp_path):
    order = []

    def worker(tag):
        with C.repo_lock(tmp_path, timeout=5) as acquired:
            assert acquired
            order.append(("enter", tag))
            time.sleep(0.2)
            order.append(("exit", tag))

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join()
    t2.join()
    # If exclusive, critical sections never interleave: enter,exit,enter,exit.
    assert order in (
        [("enter", "a"), ("exit", "a"), ("enter", "b"), ("exit", "b")],
        [("enter", "b"), ("exit", "b"), ("enter", "a"), ("exit", "a")],
    )


def test_repo_lock_times_out_to_false_when_held(tmp_path):
    held = threading.Event()
    release = threading.Event()

    def holder():
        with C.repo_lock(tmp_path) as acquired:
            assert acquired
            held.set()
            release.wait(2)

    th = threading.Thread(target=holder)
    th.start()
    assert held.wait(1)
    with C.repo_lock(tmp_path, timeout=0.3, poll=0.05) as acquired:
        assert acquired is False  # couldn't get it; caller defers sync, file stays safe
    release.set()
    th.join()


def test_lock_path_prefers_git_dir(tmp_path):
    assert C.lock_path(tmp_path) == tmp_path / ".tasks-sync.lock"
    (tmp_path / ".git").mkdir()
    assert C.lock_path(tmp_path) == tmp_path / ".git" / ".tasks-sync.lock"


def test_atomic_write_replaces_and_leaves_no_temp(tmp_path):
    target = tmp_path / "sub" / "out.md"
    C.atomic_write(target, "first")
    assert target.read_text() == "first"
    C.atomic_write(target, "second")
    assert target.read_text() == "second"
    # No leftover .tmp siblings.
    assert [p.name for p in (tmp_path / "sub").iterdir()] == ["out.md"]


def test_reserve_name_bumps_on_same_dir_collision(tmp_path):
    d = tmp_path / "inbox"
    a = C.reserve_name(d, "thing", [d])
    b = C.reserve_name(d, "thing", [d])
    assert {a, b} == {"thing", "thing-2"}
    assert (d / "thing.md").exists() and (d / "thing-2.md").exists()


def test_reserve_name_keeps_ids_globally_unique_across_dirs(tmp_path):
    inbox, ready = tmp_path / "inbox", tmp_path / "ready"
    ready.mkdir(parents=True)
    (ready / "thing.md").write_text("")  # id already used elsewhere
    name = C.reserve_name(inbox, "thing", [inbox, ready])
    assert name == "thing-2"
    assert (inbox / "thing-2.md").exists()


def test_reserve_name_concurrent_callers_never_collide(tmp_path):
    d = tmp_path / "inbox"
    n = 20
    with ThreadPoolExecutor(max_workers=n) as ex:
        names = list(ex.map(lambda _: C.reserve_name(d, "x", [d]), range(n)))
    assert len(set(names)) == n  # every caller got a distinct name
    assert len(list(d.glob("*.md"))) == n


# ── git_sync races with add_task ──────────────────────────────────────────────


def run_git_sync(repo, msg):
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / "git_sync.py"), msg, "test-transition"],
        capture_output=True,
        text=True,
        env=_env(repo),
    )


def test_git_sync_races_add_task_both_land(queue):
    """A status-transition commit (via git_sync) racing add_task --commit: both land."""
    # Pre-write a brief for the status-transition side to commit
    ip_dir = queue / "in-progress"
    ip_dir.mkdir(exist_ok=True)
    transition_brief = ip_dir / "test-transition.md"
    transition_brief.write_text("---\nid: test-transition\nstatus: in-progress\n---\n")

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_add = ex.submit(run_add, queue, "new-concurrent-task")
        f_sync = ex.submit(run_git_sync, queue, "brief(test-transition): started")

    r_add = f_add.result()
    r_sync = f_sync.result()
    assert r_add.returncode == 0, f"add_task failed: {r_add.stderr}"
    assert r_sync.returncode == 0, f"git_sync failed: {r_sync.stderr}"

    # Both sets of changes are committed (either in the same commit via sweep or separate ones)
    log = git(queue, "log", "--format=%H", "--all").stdout.splitlines()
    assert len(log) >= 2  # at least 2 commits beyond the seed

    all_tree_files = git(queue, "ls-tree", "-r", "--name-only", "HEAD").stdout
    assert "in-progress/test-transition.md" in all_tree_files
    assert any(f.startswith("inbox/new-concurrent-task") for f in all_tree_files.splitlines())
    assert not (queue / ".git" / "index.lock").exists()


def test_git_sync_usage_error_exits_1(queue):
    result = subprocess.run(
        [sys.executable, str(queue / "scripts" / "git_sync.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "Usage:" in result.stderr


def test_require_lock_raises_rather_than_running_unlocked(tmp_path):
    with C.repo_lock(tmp_path) as acquired:
        assert acquired
        with pytest.raises(TimeoutError), C.require_lock(tmp_path, timeout=0.05):
            pass  # pragma: no cover
