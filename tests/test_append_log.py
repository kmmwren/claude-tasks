"""append_log.py: the log line lands on origin/main and the main checkout is never written."""
import subprocess

import append_log
import pytest

from tests.queue_helpers import brief_text

BRIEF = "demo-brief"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def committer(repo):
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "commit.gpgsign", "false")


@pytest.fixture
def clone(tmp_path):
    remote, work = tmp_path / "remote", tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    committer(work)
    git(work, "checkout", "-q", "-b", "main")
    (work / "tasks.toml").write_text('name = "demo"\n')
    for folder in append_log.TASK_DIRS:
        (work / folder).mkdir()
        (work / folder / ".keep").write_text("")
    (work / "parked" / f"{BRIEF}.md").write_text(brief_text(BRIEF, status="parked"))
    git(work, "add", "-A")
    git(work, "commit", "-qm", "seed")
    git(work, "push", "-q", "origin", "main")
    return work


def remote_brief(repo):
    git(repo, "fetch", "-q", "origin", "main")
    return git(repo, "show", f"origin/main:parked/{BRIEF}.md")


def test_line_lands_on_origin_main(clone):
    append_log.append_log(BRIEF, "did a thing", clone, today="2026-11-06")
    assert remote_brief(clone).endswith("2026-06-01: captured.\n2026-11-06: did a thing")


def test_section_created_when_absent(clone):
    path = clone / "parked" / f"{BRIEF}.md"
    path.write_text("---\nid: demo-brief\n---\n\n## Goal\n\ng\n")
    git(clone, "commit", "-qam", "strip log")
    git(clone, "push", "-q", "origin", "main")
    append_log.append_log(BRIEF, "first", clone, today="2026-11-06")
    assert remote_brief(clone).endswith("## Execution log\n\n2026-11-06: first")


def test_main_checkout_untouched(clone):
    head = git(clone, "rev-parse", "HEAD")
    append_log.append_log(BRIEF, "did a thing", clone)
    assert git(clone, "status", "--porcelain") == ""
    assert git(clone, "rev-parse", "HEAD") == head
    assert "did a thing" not in (clone / "parked" / f"{BRIEF}.md").read_text()
    assert git(clone, "worktree", "list").count("\n") == 0


def test_unknown_brief_exits_nonzero(clone, capsys):
    with pytest.raises(SystemExit) as exc:
        append_log.main(["nope", "x", "--root", str(clone)])
    assert exc.value.code == 1
    assert "no brief 'nope'" in capsys.readouterr().err


def test_concurrent_push_is_retried(clone, tmp_path, monkeypatch):
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote"), str(other)], check=True)
    committer(other)
    real_git = append_log._git
    raced = []

    def racing_git(root, *args, **kw):
        # Land a competing commit just before our first push, so that push is rejected.
        if args[0] == "push" and not raced:
            raced.append(1)
            (other / "racer.txt").write_text("x")
            git(other, "add", "-A")
            git(other, "commit", "-qm", "racer")
            git(other, "push", "-q", "origin", "main")
        return real_git(root, *args, **kw)

    monkeypatch.setattr(append_log, "_git", racing_git)
    append_log.append_log(BRIEF, "after race", clone, today="2026-11-06")
    assert raced
    assert "after race" in remote_brief(clone)
    assert "racer.txt" in git(clone, "ls-tree", "--name-only", "origin/main")
