"""Tests for _log_conflict.Rebase: union log-only conflicts, abort on any other conflict."""

import pathlib
import subprocess

import pytest
from _log_conflict import Rebase, RebaseResult, Stage, Verdict

BRIEF = "---\nid: b\nstatus: {status}\n---\n\n## Goal\n\n{goal}\n\n## Execution log\n\n{log}"
CAPTURED = "2026-11-09: captured.\n"


def git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def brief(status="inbox", goal="g", log=CAPTURED):
    return BRIEF.format(status=status, goal=goal, log=log)


def clone(remote, where):
    subprocess.run(["git", "clone", "-q", str(remote), str(where)], check=True)
    git(where, "symbolic-ref", "HEAD", "refs/heads/main")
    for key, value in (("commit.gpgsign", "false"), ("user.email", "t@t.t"), ("user.name", "t")):
        git(where, "config", key, value)
    return where


class Queue:
    """A local clone and a second writer sharing one bare origin that holds inbox/b.md."""

    def __init__(self, tmp_path: pathlib.Path) -> None:
        remote = tmp_path / "remote"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        self.repo = clone(remote, tmp_path / "repo")
        for state_dir in ("inbox", "done"):
            (self.repo / state_dir).mkdir()
        self.write(self.repo, "inbox/b.md", brief())
        self.commit(self.repo, "seed")
        git(self.repo, "push", "-q", "origin", "main")
        self.other = clone(remote, tmp_path / "other")

    @staticmethod
    def write(repo, rel, text):
        (repo / rel).write_text(text)

    @staticmethod
    def commit(repo, message):
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", message)

    def origin_appends(self, line, rel="inbox/b.md"):
        with (self.other / rel).open("a") as fh:
            fh.write(line)
        self.commit(self.other, "log line")
        git(self.other, "push", "-q", "origin", "main")

    def move_locally(self, line):
        git(self.repo, "mv", "inbox/b.md", "done/b.md")
        self.write(self.repo, "done/b.md", brief("done", log=CAPTURED + line))
        self.commit(self.repo, "move")

    def scratch(self, tmp_path) -> pathlib.Path:
        """A detached worktree of the local HEAD with origin/main freshly fetched."""
        git(self.repo, "fetch", "-q", "origin")
        wt = tmp_path / "wt"
        git(self.repo, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
        return wt


@pytest.fixture
def queue(tmp_path):
    return Queue(tmp_path)


def test_union_resolves_a_log_append_against_a_local_move(queue, tmp_path):
    queue.origin_appends("2026-11-09: logged on origin.\n")
    queue.move_locally("2026-11-09: closed locally.\n")
    wt = queue.scratch(tmp_path)

    assert Rebase.onto_origin(wt).run() == RebaseResult(True)

    merged = (wt / "done" / "b.md").read_text()
    assert "status: done" in merged
    assert merged.endswith(
        CAPTURED + "2026-11-09: logged on origin.\n2026-11-09: closed locally.\n"
    )
    assert not (wt / "inbox" / "b.md").exists()


def test_a_conflict_outside_the_log_is_reported_not_unioned(queue, tmp_path):
    queue.write(queue.other, "inbox/b.md", brief(goal="origin rewrote the goal"))
    queue.commit(queue.other, "edit goal")
    git(queue.other, "push", "-q", "origin", "main")
    queue.write(queue.repo, "inbox/b.md", brief(goal="local rewrote the goal"))
    queue.commit(queue.repo, "edit goal")

    result = Rebase.onto_origin(queue.scratch(tmp_path)).run()

    assert result == RebaseResult(False, conflict=("inbox/b.md",))
    assert result.reason == "rebase conflict in inbox/b.md"


def test_a_rebase_that_fails_without_a_conflict_says_so(queue, tmp_path):
    result = Rebase(queue.scratch(tmp_path), "not-a-commit").run()

    assert result == RebaseResult(False)
    assert result.reason == "rebase failed without a conflicting file"


@pytest.mark.parametrize("path", ["scripts/tool.py", "elsewhere/b.md", "inbox/never-conflicted.md"])
def test_only_a_conflicted_task_brief_is_resolved(queue, tmp_path, path):
    assert Rebase.onto_origin(queue.scratch(tmp_path)).resolve(path) is Verdict.CONFLICT


def test_the_stages_are_git_s_base_upstream_and_replayed_slots():
    assert [int(stage) for stage in (Stage.BASE, Stage.UPSTREAM, Stage.REPLAYED)] == [1, 2, 3]


def test_a_later_replayed_commit_that_conflicts_after_an_earlier_one_resolved(
    queue, tmp_path, monkeypatch
):
    queue.origin_appends("2026-11-09: logged on origin.\n")
    queue.move_locally("2026-11-09: closed locally.\n")
    with (queue.repo / "done" / "b.md").open("a") as fh:
        fh.write("2026-11-10: second local commit.\n")
    queue.commit(queue.repo, "second")
    wt = queue.scratch(tmp_path)

    assert Rebase.onto_origin(wt).run() == RebaseResult(True)

    assert (
        (wt / "done" / "b.md")
        .read_text()
        .endswith(
            CAPTURED
            + "2026-11-09: logged on origin.\n2026-11-09: closed locally.\n"
            + "2026-11-10: second local commit.\n"
        )
    )
    assert git(wt, "rev-list", "--count", "origin/main..HEAD").strip() == "2"


def test_a_brief_that_resolves_alongside_a_file_that_conflicts_aborts_as_a_conflict(
    queue, tmp_path
):
    queue.write(queue.repo, "inbox/c.md", brief().replace("id: b", "id: c"))
    queue.commit(queue.repo, "second brief")
    git(queue.repo, "push", "-q", "origin", "main")
    git(queue.other, "pull", "-q", "origin", "main")
    queue.write(queue.other, "inbox/c.md", brief(goal="origin goal").replace("id: b", "id: c"))
    with (queue.other / "inbox" / "b.md").open("a") as fh:
        fh.write("2026-11-09: logged on origin.\n")
    queue.commit(queue.other, "log b and edit c")
    git(queue.other, "push", "-q", "origin", "main")
    queue.write(queue.repo, "inbox/c.md", brief(goal="local goal").replace("id: b", "id: c"))
    with (queue.repo / "inbox" / "b.md").open("a") as fh:
        fh.write("2026-11-09: logged locally.\n")
    queue.commit(queue.repo, "log b and edit c locally")
    wt = queue.scratch(tmp_path)

    result = Rebase.onto_origin(wt).run()

    assert result == RebaseResult(False, conflict=("inbox/c.md",))
    assert git(wt, "status", "--porcelain") == ""
    assert "logged locally" in git(wt, "show", "HEAD:inbox/b.md")
    assert "logged on origin" not in git(wt, "show", "HEAD:inbox/b.md")
