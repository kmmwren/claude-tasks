"""git_sync for a queue whose origin does not call its default branch `main`, and the credential scan.

Every push, fetch, reset and recovery hint names the branch origin reports; a signed or token-bearing
link counts as a credential because it expires and must never sit in git history.
"""

import pathlib
import subprocess

import git_sync
import host_land
import pytest
from _default_branch import default_branch

from tests.queue_helpers import wait_for_pusher_idle
from tests.test_git_sync import _seed_brief, committed_files, git, make_repo

BRANCH = "trunk"


def trunk_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """`make_repo`, with the remote and the clone calling their default branch `trunk`."""
    repo, remote = make_repo(tmp_path), tmp_path / "remote"
    git(repo, "branch", "-m", "main", BRANCH)
    git(repo, "push", "-q", "origin", BRANCH)
    git(remote, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
    git(repo, "push", "-q", "origin", "--delete", "main")
    git(repo, "fetch", "-q", "--prune", "origin")
    git(repo, "branch", "-q", "-u", f"origin/{BRANCH}")
    git(repo, "remote", "set-head", "origin", BRANCH)
    return repo


def other_clone(tmp_path: pathlib.Path) -> pathlib.Path:
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", "-b", BRANCH, str(tmp_path / "remote"), str(other)], check=True
    )
    for key, value in (("commit.gpgsign", "false"), ("user.email", "o@o.o"), ("user.name", "o")):
        git(other, "config", key, value)
    return other


def push_from(other: pathlib.Path, name: str, text: str) -> None:
    (other / name).write_text(text)
    git(other, "add", "-A")
    git(other, "commit", "-qm", f"other: {name}")
    git(other, "push", "-q", "origin", BRANCH)


def remote_branches(tmp_path: pathlib.Path) -> list[str]:
    out = git(tmp_path / "remote", "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return [b for b in out.stdout.split() if not b.startswith("wip")]


def test_the_fixture_remote_has_no_main(tmp_path):
    repo = trunk_repo(tmp_path)
    assert default_branch(repo) == BRANCH and remote_branches(tmp_path) == [BRANCH]


def test_a_write_is_pushed_to_the_branch_origin_names(tmp_path):
    repo = trunk_repo(tmp_path)
    (repo / "inbox" / "mine.md").write_text("y")

    assert git_sync.sync_commit_and_push("brief(mine): captured", root=repo, ids=["mine"]) is True

    git(repo, "fetch", "-q", "origin")
    assert "inbox/mine.md" in committed_files(repo, f"origin/{BRANCH}")
    assert remote_branches(tmp_path) == [BRANCH]


def test_a_rejected_push_rebases_onto_that_branch_and_advances_the_checkout(tmp_path):
    repo = trunk_repo(tmp_path)
    push_from(other_clone(tmp_path), "inbox/from-other.md", "x")
    (repo / "inbox" / "mine.md").write_text("y")

    assert git_sync.sync_commit_and_push("brief(mine): captured", root=repo, ids=["mine"]) is True

    assert wait_for_pusher_idle(repo)
    git(repo, "fetch", "-q", "origin")
    assert {"inbox/mine.md", "inbox/from-other.md"} <= committed_files(repo, f"origin/{BRANCH}")
    assert (
        git(repo, "rev-parse", "HEAD").stdout == git(repo, "rev-parse", f"origin/{BRANCH}").stdout
    )
    assert remote_branches(tmp_path) == [BRANCH]


def test_a_new_brief_that_origin_already_filed_elsewhere_is_refused(tmp_path, capsys):
    repo = trunk_repo(tmp_path)
    other = other_clone(tmp_path)
    (other / "done").mkdir(exist_ok=True)
    push_from(other, "done/dup.md", "done")
    git(repo, "fetch", "-q", "origin")
    (repo / "inbox" / "dup.md").write_text("again")

    git_sync.sync_commit("brief(dup): captured", root=repo, ids=["dup"])

    assert "would re-create an id already filed in another state" in capsys.readouterr().out
    assert "inbox/dup.md" not in committed_files(repo)


def test_a_conflict_report_and_recovery_steps_name_the_default_branch(tmp_path, capsys):
    repo = trunk_repo(tmp_path)
    _seed_brief(repo, "ready", "shared")
    git(repo, "push", "-q", "origin", BRANCH)
    push_from(other_clone(tmp_path), "ready/shared.md", "remote edit")
    (repo / "ready" / "shared.md").write_text("local edit")

    assert (
        git_sync.sync_commit_and_push("brief(shared): edited", root=repo, ids=["shared"]) is False
    )

    out = capsys.readouterr().out
    assert f"never the checked-out {BRANCH}" in out
    assert f"git rebase origin/{BRANCH}" in out and f"git push origin HEAD:{BRANCH}" in out
    assert "origin/main" not in out and "HEAD:main" not in out


def test_the_fetch_timeout_notices_name_the_branch_to_fetch_and_reset(
    tmp_path, monkeypatch, capsys
):
    repo = trunk_repo(tmp_path)

    def hang(root, deadline):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(git_sync, "_fetch_origin", hang)
    monkeypatch.setattr(git_sync, "_fetch_head", lambda root: None)
    git_sync._fetch_if_stale(repo)
    git_sync._advance_checkout(repo, git_sync.Deadline.in_(5))

    out = capsys.readouterr().out
    assert "using the remote branch as last fetched" in out
    assert f"git fetch origin {BRANCH} && git reset --keep origin/{BRANCH}" in out


def test_a_checkout_that_cannot_advance_says_which_branch_to_reset_to(tmp_path, capsys):
    repo = trunk_repo(tmp_path)
    _seed_brief(repo, "ready", "theirs")
    git(repo, "push", "-q", "origin", BRANCH)
    push_from(other_clone(tmp_path), "ready/theirs.md", "remote edit")
    (repo / "ready" / "theirs.md").write_text("an uncommitted note")
    (repo / "inbox" / "mine.md").write_text("y")

    assert git_sync.sync_commit_and_push("brief(mine): captured", root=repo, ids=["mine"]) is True

    assert f"then run: git reset --keep origin/{BRANCH}" in capsys.readouterr().out


# --- the caller already holds the lock ---------------------------------------------------


def test_a_caller_holding_the_lock_commits_without_taking_it_again(tmp_path, monkeypatch):
    repo = trunk_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "note.md").write_text("hi")

    def never(*args, **kwargs):
        raise AssertionError("the queue lock was taken a second time")

    monkeypatch.setattr(git_sync, "repo_lock", never)

    result = git_sync.sync_commit("test: docs", root=repo, paths=["docs"], lock_held=True)
    assert result is git_sync.SyncResult.COMMITTED

    assert "docs/note.md" in committed_files(repo)
    assert wait_for_pusher_idle(repo)


def test_a_caller_holding_the_lock_still_never_commits_a_credential(tmp_path, capsys):
    repo = trunk_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "link.md").write_text("see https://b.example.test/x?token=abc123")

    git_sync.sync_commit("test: docs", root=repo, paths=["docs"], lock_held=True)

    assert "docs/link.md" not in committed_files(repo)
    assert "1 held back for looking like a credential" in capsys.readouterr().out


def test_a_nested_queue_lands_on_the_wip_branch_of_a_trunk_origin_without_retaking_the_lock(
    tmp_path, monkeypatch
):
    repo = trunk_repo(tmp_path)
    queue = repo / "project" / ".tasks"
    queue.mkdir(parents=True)
    (queue / "x.md").write_text("x")
    monkeypatch.setattr(host_land, "repo_lock", lambda *a, **k: pytest.fail("lock retaken"))

    result = git_sync.sync_commit("x", root=queue, paths=["."], lock_held=True)

    assert result is git_sync.SyncResult.HOSTED
    wip = git(tmp_path / "remote", "ls-tree", "-r", "--name-only", "wip/tasks-project")
    assert "project/.tasks/x.md" in wip.stdout.split()
    assert "project/.tasks/x.md" not in committed_files(repo)


# --- which repo holds a queue ------------------------------------------------------------


def test_the_host_repo_is_named_by_its_directory(tmp_path):
    repo = trunk_repo(tmp_path)
    inside = repo / "project" / ".tasks"
    inside.mkdir(parents=True)
    assert git_sync.host_repo_name(inside) == "clone"
    assert git_sync.host_repo_name(repo) == "clone"


def test_a_directory_in_no_repo_has_no_host_repo(tmp_path):
    assert git_sync.host_repo_name(tmp_path) is None


# --- credentials and signed links --------------------------------------------------------

SIGNED = [
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "ghp_abcdefghijklmnopqrstuvwx",
    "xo" + "xb-1234567890-abcdef",
    "-----BEGIN RSA PRIVATE " + "KEY-----",
    "eyJhbGciOiJIUzI1NiJ9abcdef.abcdefghijkl.abcdefghijkl",
    "https://b.s3.example.test/k?X-Amz-Signature=abc123",
    "https://b.s3.example.test/k?a=1&x-amz-credential=AKIA%2F2026",
    "https://acct.blob.example.test/c/f?sv=2022&se=2026-12-01&sp=r&sig=abc%3D",
    "https://acct.blob.example.test/c/f?sp=r&sig=abc",
    "https://d.cdn.example.test/p?Signature=abc&Key-Pair-Id=K1",
    "https://d.cdn.example.test/p?Key-Pair-Id=K1",
    "https://d.cdn.example.test/p?a=1&Policy=eyJ",
    "https://api.example.test/v1?token=abc123",
    "https://api.example.test/v1?a=1&access_token=abc123",
    "https://api.example.test/v1?a=1&ACCESS_TOKEN=abc123",
]
PLAIN = [
    "https://example.test/page?id=3&sort=asc",
    "https://example.test/doc#token",
    "a bare token = 'x' in prose",
    "?sig=abc without any expiry parameter",
    "https://example.test/c?sv=2022&sig=abc",
    "the signature= word with no query marker",
    "",
]


@pytest.mark.parametrize("text", SIGNED)
def test_a_credential_or_signed_link_is_recognised(text):
    assert git_sync.carries_credential(text) is True


@pytest.mark.parametrize("text", PLAIN)
def test_an_ordinary_link_or_sentence_is_not_a_credential(text):
    assert git_sync.carries_credential(text) is False


def test_a_file_with_a_signed_link_stays_local_in_a_sweep(tmp_path, capsys):
    repo = trunk_repo(tmp_path)
    (repo / "inbox" / "leak.md").write_text(
        "proof: https://b.s3.example.test/k?X-Amz-Signature=abc"
    )
    (repo / "inbox" / "fine.md").write_text("proof: https://example.test/k")

    git_sync.sweep(repo)

    assert "inbox/fine.md" in committed_files(repo) and "inbox/leak.md" not in committed_files(repo)
    assert "1 held back for looking like a credential" in capsys.readouterr().out
