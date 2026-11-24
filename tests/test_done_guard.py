"""A brief never lands in done/ with an open success criterion: move_brief refuses, the commit backstop returns it."""

import pathlib

import git_sync
import pytest
import transition
from done_guard import DoneBackstop, UntickedCriteriaError

from tests.test_git_sync import git, make_repo

OPEN = "- [x] tests green\n- [ ] landed on main"
TICKED = "- [x] tests green\n- [x] landed on main"


def brief(bid: str, criteria: str, status: str = "ready") -> str:
    return (
        f"---\nid: {bid}\ntitle: {bid}\ncreated: 2026-11-01\nupdated: 2026-11-01\nstatus: {status}\n"
        f"type: todo\nimportance: 2\nautonomy: full\nblocked_by: []\n---\n\n## Goal\n\nx\n\n"
        f"## Success criteria\n\n{criteria}\n\n## Context\n\nx\n\n## Execution log\n\n2026-11-01: captured.\n"
    )


@pytest.fixture()
def queue(tmp_path, monkeypatch):
    for d in ("inbox", "ready", "in-progress", "blocked", "parked", "done"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(transition, "sync_commit", lambda *a, **k: None)
    return tmp_path


def put(queue: pathlib.Path, folder: str, bid: str, criteria: str) -> pathlib.Path:
    path = queue / folder / f"{bid}.md"
    path.write_text(brief(bid, criteria, folder))
    return path


def test_move_to_done_refuses_an_open_criterion_and_moves_nothing(queue):
    src = put(queue, "ready", "open-one", OPEN)
    with pytest.raises(UntickedCriteriaError, match="1 unticked success criteria"):
        transition.move_brief("open-one", "done", queue)
    assert src.exists() and not (queue / "done" / "open-one.md").exists()


def test_settle_with_ticks_each_open_criterion_with_the_evidence(queue):
    put(queue, "ready", "merged-one", OPEN)
    transition.move_brief("merged-one", "done", queue, settle_with="PR 12 merged")
    text = (queue / "done" / "merged-one.md").read_text()
    assert "- [x] landed on main (PR 12 merged)" in text and "- [ ]" not in text


def test_settle_with_cannot_claim_a_supersession(queue):
    put(queue, "ready", "dup", OPEN)
    with pytest.raises(ValueError, match="cannot record a supersession"):
        transition.move_brief("dup", "done", queue, settle_with="duplicate of other")


def test_superseded_by_closes_only_when_a_successor_carries_the_criterion(queue):
    put(queue, "ready", "old-one", OPEN)
    put(queue, "ready", "new-one", "- [ ] work landed on main through the merge queue")
    transition.move_brief("old-one", "done", queue, superseded_by=["new-one"])
    assert "superseded: carried to new-one" in (queue / "done" / "old-one.md").read_text()


def test_superseded_by_refuses_an_uncarried_criterion(queue):
    put(queue, "ready", "old-two", "- [ ] rotate the credential vault keys")
    put(queue, "ready", "unrelated", "- [ ] paint the shed")
    with pytest.raises(ValueError, match="not carried"):
        transition.move_brief("old-two", "done", queue, superseded_by=["unrelated"])
    assert (queue / "ready" / "old-two.md").exists()


def test_cli_exposes_settle_with(queue):
    put(queue, "ready", "cli-one", OPEN)
    transition.main(["move", "cli-one", "done", "--root", str(queue), "--no-sync", "--settle-with", "PR 3 merged"])
    assert "(PR 3 merged)" in (queue / "done" / "cli-one.md").read_text()


# --- the backstop: a writer that skipped move_brief ----------------------------------


def seed(repo: pathlib.Path, folder: str, bid: str, criteria: str) -> None:
    (repo / folder / f"{bid}.md").write_text(brief(bid, criteria, folder))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"seed {bid}")


def hand_move(repo: pathlib.Path, folder: str, bid: str, criteria: str) -> None:
    """What a script writing done/ directly does: new file in done/, old one gone."""
    (repo / "done" / f"{bid}.md").write_text(brief(bid, criteria, "done"))
    (repo / folder / f"{bid}.md").unlink()


def test_sweep_returns_an_unsettled_done_brief_to_where_it_came_from(tmp_path, capsys):
    repo = make_repo(tmp_path)
    seed(repo, "in-progress", "sneaky", OPEN)
    hand_move(repo, "in-progress", "sneaky", OPEN)

    git_sync.sweep(repo)

    tracked = set(git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines())
    assert "in-progress/sneaky.md" in tracked and "done/sneaky.md" not in tracked
    text = (repo / "in-progress" / "sneaky.md").read_text()
    assert "status: in-progress" in text
    assert "done-guard: not done, 1 success criteria still open (landed on main)" in text
    assert "was in done/ with 1 open success criteria" in capsys.readouterr().out


def test_sweep_still_commits_settled_and_unrelated_briefs(tmp_path):
    repo = make_repo(tmp_path)
    seed(repo, "ready", "sneaky", OPEN)
    seed(repo, "ready", "clean", TICKED)
    hand_move(repo, "ready", "sneaky", OPEN)
    hand_move(repo, "ready", "clean", TICKED)
    (repo / "inbox" / "fresh.md").write_text(brief("fresh", "- [ ] later", "inbox"))

    git_sync.sweep(repo)

    tracked = set(git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines())
    assert {"done/clean.md", "inbox/fresh.md", "ready/sneaky.md"} <= tracked
    assert "done/sneaky.md" not in tracked


def test_backstop_falls_back_to_ready_when_the_origin_is_unknown(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "done" / "orphan.md").write_text(brief("orphan", OPEN, "done"))

    [reverted] = DoneBackstop(repo, today="2026-11-24").run()

    assert reverted.folder == "ready" and (repo / "ready" / "orphan.md").exists()
    assert not (repo / "done" / "orphan.md").exists()


def test_backstop_leaves_a_brief_already_filed_in_done_alone(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "done" / "old.md").write_text(brief("old", OPEN, "done"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "legacy")
    (repo / "done" / "old.md").write_text(brief("old", OPEN, "done") + "edit\n")

    assert DoneBackstop(repo).run() == []
